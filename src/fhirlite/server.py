"""HTTP interface: serves a store as a FHIR RESTful endpoint at <base>/fhir (search, and writes when writable).

GET (and HEAD) on [base]/metadata, [base]/[type]?..., [base]/[type]/[id],
[base]/[compartment]/[id]/[type]?... and [base]?... (whole-system search); POST
.../_search with a form-encoded body runs the same searches. A writable store also takes POST
[type] (create), PUT [type]/[id] (update or create) and DELETE [type]/[id], with If-Match and
Prefer: return. Other methods answer 405 with an OperationOutcome. A request that accepts no JSON
format answers 406. Reads carry ETag and Last-Modified. Read-only engines (one SQLite connection
each) are pooled across request threads and share one result cache. A writable store is served by
one engine, one request at a time.
Standard library only.
"""
from __future__ import annotations

import json
import queue
import re
import socket
import threading
from datetime import datetime
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .search.engine import Engine


PREFIX = "/fhir"
MAX_BODY = 1 << 20
MAX_WRITE_BODY = 64 << 20
ASCII_LENGTH = re.compile(r"[0-9]{1,18}")
# response type for each acceptable media range (generic types are echoed back, R4 Content Types)
JSON_TYPES = {"application/fhir+json": "application/fhir+json", "application/json": "application/json",
              "text/json": "text/json", "application/json+fhir": "application/json+fhir",
              "application/*": "application/fhir+json", "*/*": "application/fhir+json", "json": "application/fhir+json"}
FORMAT_TYPES = {"json": "application/fhir+json", "application/fhir+json": "application/fhir+json",
                "application/json": "application/json", "text/json": "text/json",
                "application/json+fhir": "application/json+fhir"}


def _outcome(code, diagnostics):
    return {"resourceType": "OperationOutcome", "issue": [{"severity": "error", "code": code, "diagnostics": diagnostics}]}


def negotiate(accept):
    """Content type for an Accept header (server-driven negotiation with q-values and the
    fhirVersion parameter), or None when no JSON type is acceptable."""
    if not accept or not accept.strip():
        return "application/fhir+json"
    best = None
    for item in accept.split(","):
        fields = [f.strip() for f in item.split(";")]
        media, params = fields[0].lower(), {}
        for f in fields[1:]:
            k, _, v = f.partition("=")
            params[k.strip().lower()] = v.strip().strip('"')
        if media not in JSON_TYPES:
            continue
        try:
            q = float(params.get("q", "1"))
        except ValueError:
            q = 0.0
        version = params.get("fhirversion")
        if q <= 0 or (version and not version.startswith("4.0")):
            continue
        if best is None or q > best[0]:
            best = (q, JSON_TYPES[media])
    return best[1] if best else None


def preference(header, name):
    """Value of one RFC 7240 preference (names case-insensitive, values may be quoted)."""
    for item in (header or "").split(","):
        key, _, value = item.strip().partition("=")
        if key.strip().lower() == name:
            return value.strip().strip('"').lower()
    return None


def allowed_methods(path, writable=False):
    route = path.split("?", 1)[0].rstrip("/")
    if route.endswith("/_search"):
        return "POST"
    if writable:
        segs = [s for s in route[len(PREFIX):].split("/") if s]
        if len(segs) == 1 and not segs[0].startswith(("_", "$")) and segs[0] != "metadata":
            return "GET, HEAD, POST"
        if len(segs) == 2 and not segs[1].startswith(("_", "$")):
            return "GET, HEAD, PUT, DELETE"
    return "GET, HEAD"


def serve(db_path, host="127.0.0.1", port=0, public_base=None, default_handling="strict",
          default_count=None, max_count=None, cors_origin=None, writable=False, definitions=None, packages=(),
          clock=None):
    """Returns (server, base_url). public_base: the base URL clients see (behind a proxy);
    default http://host:port/fhir. default_handling: unknown-parameter handling when a request
    sends no Prefer header (strict = 400, lenient = ignore and report). writable: also create,
    update and delete (needs the definitions and packages the store was built with)."""
    pool, cache = queue.LifoQueue(), {}
    lock = threading.Lock()
    writer = None

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "fhirlite"

        def log_message(self, fmt, *args):  # quiet by default
            pass

        def setup(self):
            super().setup()
            # Headers and body go out in separate writes; without this, Nagle plus
            # delayed ACK adds ~40 ms to every response.
            self.request.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

        def do_GET(self):
            self._answer(head=False)

        def do_HEAD(self):
            self._answer(head=True)

        def _not_allowed(self):
            self._send(405, _outcome("not-supported", f"{self.command} is not supported: this endpoint is read-only "
                                                       "FHIR RESTful Search (GET, HEAD, POST .../_search)"),
                       extra={"Allow": allowed_methods(self.path)})

        do_PATCH = _not_allowed

        def do_PUT(self):
            if not writer:
                self._not_allowed()
                return
            body = self._read_body(MAX_WRITE_BODY)
            if body is not None:
                self._answer(head=False, body=body, method="PUT")

        def do_DELETE(self):
            if writer:
                self._answer(head=False, method="DELETE")
            else:
                self._not_allowed()

        def do_OPTIONS(self):
            if cors_origin and self.headers.get("Origin"):        # CORS preflight (opt-in)
                self.send_response(204)
                self.send_header("Access-Control-Allow-Origin", cors_origin)
                self.send_header("Access-Control-Allow-Methods", "GET, HEAD, POST, PUT, DELETE" if writer else "GET, HEAD, POST")
                self.send_header("Access-Control-Allow-Headers", "Accept, Content-Type, Prefer, If-Match")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self._not_allowed()

        def do_POST(self):
            search = self.path.split("?", 1)[0].rstrip("/").endswith("_search")
            body = self._read_body(MAX_BODY if search or not writer else MAX_WRITE_BODY)
            if body is not None:
                self._answer(head=False, body=body, method="POST")

        def _read_body(self, limit):
            """The request body, or None after answering a framing error. A request whose body
            length is unusable is answered and the connection closed: the body cannot be skipped
            safely, so the stream is not reused."""
            raw = self.headers.get("Content-Length")
            if raw is None:
                if self.headers.get("Transfer-Encoding"):
                    self.close_connection = True
                    self._send(411, _outcome("structure", f"{self.command} needs a Content-Length "
                                                          "(chunked bodies are not supported)"))
                    return None
                return b""
            if not ASCII_LENGTH.fullmatch(raw.strip()):   # ASCII 0-9 only (str.isdigit accepts e.g. superscripts)
                self.close_connection = True
                self._send(400, _outcome("structure", f"Invalid Content-Length header: {raw!r}"))
                return None
            length = int(raw.strip())
            if length > limit:
                self.close_connection = True
                self._send(413, _outcome("too-costly", f"Body larger than {limit} bytes"))
                return None
            try:
                return self.rfile.read(length) if length else b""
            except OSError:
                self.close_connection = True
                return None

        def _answer(self, head, body=None, method="GET"):
            if not (self.path == PREFIX or self.path.startswith((PREFIX + "/", PREFIX + "?"))):
                self._send(404, _outcome("not-found", f"Unknown path {self.path}; the FHIR base is {url}"), head=head)
                return
            content_type = negotiate(self.headers.get("Accept"))
            if content_type is None:
                self._send(406, _outcome("not-supported", "Only JSON (application/fhir+json, FHIR 4.0) is supported"),
                           head=head)
                return
            fmt = re.search(r"[?&]_format=([^&]*)", self.path)
            if fmt:
                from urllib.parse import unquote_plus
                content_type = FORMAT_TYPES.get(unquote_plus(fmt.group(1)).split(";")[0].strip().lower().replace(" ", "+"),
                                                content_type)
            self.content_type = content_type
            deleted = None
            if writer:
                lock.acquire()
                engine = writer
            else:
                try:
                    engine = pool.get_nowait()
                except queue.Empty:
                    engine = Engine(db_path, base_url=url, cache=cache, default_count=default_count, max_count=max_count)
            try:
                prefer = preference(self.headers.get("Prefer"), "handling")
                handling = prefer if prefer in ("strict", "lenient") else default_handling
                path = self.path[len(PREFIX):] or "/"
                content = self.headers.get("Content-Type") or ""
                if method == "PUT":
                    status, result = engine.put(path, body, content, self.headers.get("If-Match"))
                elif method == "DELETE":
                    status, result = engine.delete(path, self.headers.get("If-Match"))
                    deleted = engine.writer.deleted if status == 204 else None
                elif body is None:
                    status, result = engine.get(path, handling)
                else:
                    status, result = engine.post(path, body, content, handling)
            except Exception as exc:              # never drop the connection without an answer
                status, result = 500, _outcome("exception", f"Internal error: {type(exc).__name__}")
            finally:
                if writer:
                    lock.release()
                else:
                    pool.put(engine)
            body = result
            extra = {"Allow": allowed_methods(self.path, bool(writer))} if status == 405 else {}
            if method == "DELETE" and status == 204:
                if deleted:
                    extra["ETag"] = f'W/"{deleted["versionId"]}"'
                    extra["Last-Modified"] = format_datetime(datetime.fromisoformat(deleted["lastUpdated"]), usegmt=True)
                self._send(204, None, extra=extra)
                return
            meta = body.get("meta") or {}
            if status == 200 and body.get("resourceType") == "CapabilityStatement":
                import hashlib
                tag = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:16]
                extra["ETag"] = f'W/"{tag}"'
            if status in (200, 201) and body.get("resourceType") not in ("Bundle", "OperationOutcome", "CapabilityStatement"):
                if meta.get("versionId"):
                    extra["ETag"] = f'W/"{meta["versionId"]}"'
                if meta.get("lastUpdated"):
                    try:
                        extra["Last-Modified"] = format_datetime(datetime.fromisoformat(meta["lastUpdated"]), usegmt=True)
                    except ValueError:
                        pass
            if method in ("POST", "PUT") and status in (200, 201) and body.get("resourceType") not in (
                    "Bundle", "OperationOutcome"):
                if status == 201:
                    extra["Location"] = f"{url}/{body['resourceType']}/{body['id']}/_history/{meta['versionId']}"
                returned = preference(self.headers.get("Prefer"), "return")
                if returned == "minimal":
                    body = None
                elif returned == "operationoutcome":
                    body = {"resourceType": "OperationOutcome", "issue": [{
                        "severity": "information", "code": "informational",
                        "diagnostics": f"{body['resourceType']}/{body['id']} stored as version {meta['versionId']}"}]}
            self._send(status, body, head=head, extra=extra)

        def _send(self, status, body, head=False, extra=None):
            data = b"" if body is None else json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", f"{getattr(self, 'content_type', 'application/fhir+json')};charset=utf-8")
            if cors_origin and self.headers.get("Origin"):
                self.send_header("Access-Control-Allow-Origin", cors_origin)
            self.send_header("Content-Length", str(len(data)))
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            if not head:
                self.wfile.write(data)

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    url = (public_base or f"http://{host}:{server.server_address[1]}{PREFIX}").rstrip("/")
    server.engine = None
    if writable:
        writer = server.engine = Engine(db_path, base_url=url, default_count=default_count, max_count=max_count,
                                        writable=True, definitions=definitions, packages=packages, clock=clock)
    return server, url
