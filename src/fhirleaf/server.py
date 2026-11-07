"""HTTP interface: serves a store as a read-only FHIR RESTful Search endpoint at <base>/fhir.

GET (and HEAD) on [base]/metadata, [base]/[type]?..., [base]/[type]/[id],
[base]/[compartment]/[id]/[type]?... and [base]?... (whole-system search); POST
.../_search with a form-encoded body runs the same searches. Other methods answer 405
with an OperationOutcome; a request that accepts no JSON format answers 406. Reads carry ETag and Last-Modified. Engines (one SQLite
connection each) are pooled across request threads and share one result cache.
Standard library only.
"""
from __future__ import annotations

import json
import queue
import re
import socket
from datetime import datetime
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .search import Engine

PREFIX = "/fhir"
MAX_BODY = 1 << 20
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


def allowed_methods(path):
    return "POST" if path.split("?", 1)[0].rstrip("/").endswith("/_search") else "GET, HEAD"


def serve(db_path, host="127.0.0.1", port=0, public_base=None, default_handling="strict",
          default_count=None, max_count=None, cors_origin=None):
    """Returns (server, base_url). public_base: the base URL clients see (behind a proxy);
    default http://host:port/fhir. default_handling: unknown-parameter handling when a request
    sends no Prefer header (strict = 400, lenient = ignore and report)."""
    pool, cache = queue.LifoQueue(), {}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "fhirleaf"

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

        do_PUT = do_DELETE = do_PATCH = _not_allowed

        def do_OPTIONS(self):
            if cors_origin and self.headers.get("Origin"):        # CORS preflight (opt-in)
                self.send_response(204)
                self.send_header("Access-Control-Allow-Origin", cors_origin)
                self.send_header("Access-Control-Allow-Methods", "GET, HEAD, POST")
                self.send_header("Access-Control-Allow-Headers", "Accept, Content-Type, Prefer")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self._not_allowed()

        def do_POST(self):
            # A request whose body length is unusable is answered and the connection closed:
            # the body cannot be skipped safely, so the stream is not reused.
            raw = self.headers.get("Content-Length")
            if raw is None:
                if self.headers.get("Transfer-Encoding"):
                    self.close_connection = True
                    self._send(411, _outcome("structure", "POST _search needs a Content-Length (chunked bodies are not supported)"))
                    return
                length = 0
            elif ASCII_LENGTH.fullmatch(raw.strip()):   # ASCII 0-9 only (str.isdigit accepts e.g. superscripts)
                length = int(raw.strip())
            else:
                self.close_connection = True
                self._send(400, _outcome("structure", f"Invalid Content-Length header: {raw!r}"))
                return
            if length > MAX_BODY:
                self.close_connection = True
                self._send(413, _outcome("too-costly", f"Search body larger than {MAX_BODY} bytes"))
                return
            try:
                body = self.rfile.read(length) if length else b""
            except OSError:
                self.close_connection = True
                return
            self._answer(head=False, body=body)

        def _answer(self, head, body=None):
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
            try:
                engine = pool.get_nowait()
            except queue.Empty:
                engine = Engine(db_path, base_url=url, cache=cache, default_count=default_count, max_count=max_count)
            try:
                prefer = preference(self.headers.get("Prefer"), "handling")
                handling = prefer if prefer in ("strict", "lenient") else default_handling
                path = self.path[len(PREFIX):] or "/"
                if body is None:
                    status, result = engine.get(path, handling)
                else:
                    status, result = engine.post(path, body, self.headers.get("Content-Type") or "", handling)
            except Exception as exc:              # never drop the connection without an answer
                status, result = 500, _outcome("exception", f"Internal error: {type(exc).__name__}")
            finally:
                pool.put(engine)
            body = result
            extra = {"Allow": allowed_methods(self.path)} if status == 405 else {}
            meta = body.get("meta") or {}
            if status == 200 and body.get("resourceType") == "CapabilityStatement":
                import hashlib
                tag = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:16]
                extra["ETag"] = f'W/"{tag}"'
            if status == 200 and body.get("resourceType") not in ("Bundle", "OperationOutcome", "CapabilityStatement"):
                if meta.get("versionId"):
                    extra["ETag"] = f'W/"{meta["versionId"]}"'
                if meta.get("lastUpdated"):
                    try:
                        extra["Last-Modified"] = format_datetime(datetime.fromisoformat(meta["lastUpdated"]), usegmt=True)
                    except ValueError:
                        pass
            self._send(status, body, head=head, extra=extra)

        def _send(self, status, body, head=False, extra=None):
            data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
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
    return server, url
