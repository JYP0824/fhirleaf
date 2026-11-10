"""FHIR RESTful Search over a store: type, compartment and whole-system search, read, metadata, paging."""
from __future__ import annotations

import base64
import json
import math
import re
import sqlite3
import uuid
import zlib
from datetime import datetime, timezone
from itertools import count
from pathlib import Path
from urllib.parse import parse_qsl, quote, unquote, urlsplit

from ..model.values import (date_range, normalize, number_bounds, phonetic_codes, resolve_timezone, split_escaped,
                            split_or, split_prefix, unescape)
from ..store.extract import TABLES as INDEX_TABLES
from ..store.schema import FORMAT
from .errors import SearchError, operation_outcome
from .text import fts_query, parse_text_query


CONTROL = frozenset({"_count", "_sort", "_include", "_revinclude", "_summary", "_elements", "_total", "_format",
                     "_pretty", "_contained", "_containedType"})
TABLE = {"token": "sp_token", "reference": "sp_ref", "date": "sp_date", "string": "sp_string",
         "quantity": "sp_quantity", "number": "sp_number", "uri": "sp_uri", "composite": "sp_composite",
         "position": "sp_position"}
SORT_COLUMN = {"date": "low", "token": "code", "string": "norm", "quantity": "low", "number": "value",
               "reference": "target_id", "uri": "uri"}
RANK = {"_id": 0, "reference": 1, "chain": 2, "composite": 3, "token": 3, "has": 4, "date": 4,
        "quantity": 5, "number": 5, "string": 5, "uri": 6}
RESULT_CACHE_MAX, RESULT_CACHE_ENTRIES = 200000, 64
SUBSETTED = {"system": "http://terminology.hl7.org/CodeSystem/v3-ObservationValue", "code": "SUBSETTED",
             "display": "Resource encoded in summary mode"}






CONTROL_VALUES = {"_summary": {"true", "false", "text", "data", "count"}, "_total": {"none", "estimate", "accurate"},
                  "_contained": {"false", "true", "both"}, "_containedType": {"container", "contained"},
                  "_pretty": {"true", "false"}}
JSON_FORMATS = {"json", "application/json", "application/fhir+json", "application/json+fhir", "text/json"}
SINGLE_CONTROLS = ("_count", "_summary", "_total", "_contained", "_containedType", "_format", "_pretty")
READ_PARAMETERS = {"_format", "_pretty", "_summary", "_elements"}
CHOICE_TYPES = {"Address", "Age", "Annotation", "Attachment", "Base64Binary", "Boolean", "Canonical", "Code",
                "CodeableConcept", "Coding", "ContactDetail", "ContactPoint", "Contributor", "Count", "DataRequirement",
                "Date", "DateTime", "Decimal", "Distance", "Dosage", "Duration", "Expression", "HumanName", "Id",
                "Identifier", "Instant", "Integer", "Markdown", "Meta", "Money", "Oid", "ParameterDefinition", "Period",
                "PositiveInt", "Quantity", "Range", "Ratio", "Reference", "RelatedArtifact", "SampledData", "Signature",
                "String", "Time", "Timing", "TriggerDefinition", "UnsignedInt", "Uri", "Url", "UsageContext", "Uuid"}
CONTROL_MODIFIERS = {"_include": {"iterate", "recurse"}, "_revinclude": {"iterate", "recurse"}}
REASONS = {200: "OK", 201: "Created", 204: "No Content"}


def _version_tags(value):
    """If-Match ETags ('2', '"2"', 'W/"2"', or a comma-separated list) -> {'2', ...}; None stays None."""
    if value is None:
        return None
    tags = set()
    for item in value.split(","):
        item = item.strip()
        tags.add((item[2:] if item.startswith("W/") else item).strip('"'))
    return tags


def _control_problem(name, values):
    """Why a result parameter cannot be honoured, or None."""
    base, _, modifier = name.partition(":")
    if modifier and modifier not in CONTROL_MODIFIERS.get(base, ()):
        return f'Unsupported modifier on result parameter "{name}"'
    allowed = CONTROL_VALUES.get(base)
    for value in values:
        if allowed is not None and value.split(";")[0].strip() not in allowed:
            return f'Unsupported value for {base}: "{value}" (supported: {", ".join(sorted(allowed))})'
    return None


def _any(crits):
    """OR of criteria (compartment membership)."""
    cond = "(" + " OR ".join(c.cond for c in crits) + ")"
    args = [a for c in crits for a in c.args]
    if all(c.driver for c in crits):
        return Crit(cond, args, " UNION ".join(c.driver for c in crits),
                    [a for c in crits for a in c.driver_args], min(c.rank for c in crits))
    return Crit(cond, args)


class Crit:
    """One search criterion: an EXISTS-style condition on alias `{a}`, and optionally a
    positive pid subquery usable to drive the search."""

    def __init__(self, cond, args=(), driver=None, driver_args=(), rank=9):
        self.cond, self.args, self.driver, self.driver_args, self.rank = cond, list(args), driver, list(driver_args), rank

    def on(self, alias):
        return self.cond.replace("{a}", alias), self.args










def distance_km(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * 6371.0088 * math.asin(min(1.0, math.sqrt(a)))


class Engine:
    def __init__(self, path, base_url="http://localhost/fhir", cache=None, default_count=None, max_count=None,
                 writable=False, definitions=None, packages=(), clock=None):
        """default_count / max_count override the page sizes stored at load time.
        writable: also create, update and delete (needs the definitions and packages the store was
        built with). A read-only connection opens the file immutable unless a writer left a WAL."""
        self.path = Path(path).resolve()
        if not self.path.is_file():
            raise ValueError(f"No store at {self.path}")
        self.writable, self.writer, self._overrides = bool(writable), None, (default_count, max_count)
        self.base = base_url.rstrip("/")
        self._results = cache if cache is not None else {}
        if writable:
            if definitions is None:
                raise ValueError("A writable connection needs the definitions the store was built with")
            self.db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
            self.db.execute("PRAGMA busy_timeout = 10000")
        elif Path(str(self.path) + "-wal").exists():          # a writer is (or was) active: read its commits
            self.db = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, check_same_thread=False)
        else:
            self.db = sqlite3.connect(self.path.as_uri() + "?mode=ro&immutable=1", uri=True, check_same_thread=False)
        self.live = writable or Path(str(self.path) + "-wal").exists()
        self.db.create_function("distance_km", 4, distance_km, deterministic=True)
        self._state()
        if writable:
            self.db.execute("PRAGMA journal_mode = WAL")
            from ..store.write import Writer
            self.writer = Writer(self, definitions, packages, clock)
            self._state()
        self._data_version = self.db.execute("PRAGMA data_version").fetchone()[0]
        self._writes = 0

    def close(self):
        """Close the connection. A writable one first folds its WAL back into the file, so the store
        can again be opened immutable."""
        if self.writable:
            try:
                self._optimize()
                self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                self.db.execute("PRAGMA journal_mode = DELETE")
            except sqlite3.OperationalError:      # other connections are open: the last writer to close folds it
                pass
        self.db.close()

    def _optimize(self):
        """Refresh query-planner statistics that writes made stale: ANALYZE a table that has rows but no
        statistics (a store that started empty has none) or grew more than tenfold since. Statistics a
        load computed for unchanged tables are kept."""
        known = {}
        for table, stat in self.db.execute("SELECT tbl, stat FROM sqlite_stat1 WHERE idx IS NOT NULL"):
            known[table] = max(known.get(table, 0), int(stat.split()[0]))
        tables = ["resources", "written", "versions"] + [f"sp_{t}" for t in INDEX_TABLES]
        stale = [t for t in tables if (rows := self.db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])
                 and (t not in known or rows > 10 * known[t])]
        for table in stale:
            self.db.execute(f"ANALYZE {table}")
        if stale:
            self._results.clear()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _sync(self):
        """Another connection committed: reload the stored state and drop cached results."""
        if self.live:
            version = self.db.execute("PRAGMA data_version").fetchone()[0]
            if version != self._data_version:
                self._data_version = version
                self._state()
                self._results.clear()

    def _state(self):
        meta = {k: json.loads(v) for k, v in self.db.execute("SELECT key, value FROM meta")}
        if meta.get("version") != FORMAT:
            raise ValueError(f"Store format {meta.get('version')} is not {FORMAT}; rebuild it with this fhirlite")
        self.meta, self.settings = meta, dict(meta["settings"])
        default_count, max_count = self._overrides
        for key, value in (("default_count", default_count), ("max_count", max_count)):
            if value is not None:
                if int(value) < 1:
                    raise ValueError(f"{key} must be at least 1")
                self.settings[key] = int(value)
        self.tz = resolve_timezone(self.settings["timezone"])
        self.history_enabled = bool(self.settings.get("writable"))
        self.has_written = bool(self.db.execute("SELECT 1 FROM written LIMIT 1").fetchone())
        self.loaded = set(meta["resource_counts"])
        self.compartments = meta.get("compartments", {})
        self.fhir_types = set(meta.get("fhir_resource_types") or meta["resource_counts"])
        self.local_bases = sorted({self.base, *(b.rstrip("/") for b in self.settings.get("local_bases", []))},
                                  key=len, reverse=True)
        self.params, self.by_id = {}, {}
        for pid, rtype, code, ptype, targets, comps, usage, any_or, any_and in self.db.execute(
                "SELECT id, type, code, ptype, targets, components, usage, multiple_or, multiple_and FROM params"):
            p = {"id": pid, "type": rtype, "code": code, "ptype": ptype, "targets": json.loads(targets),
                 "components": json.loads(comps), "usage": usage, "multiple_or": any_or, "multiple_and": any_and}
            self.params[(rtype, code)] = self.by_id[pid] = p
        self.capability = json.loads(json.dumps(meta["capability"]))
        self.capability["implementation"]["url"] = self.base
        self.capability["implementation"]["description"] = re.sub(
            r"default page size \d+, maximum \d+",
            f"default page size {self.settings['default_count']}, maximum {self.settings['max_count']}",
            self.capability["implementation"].get("description", ""))
        # The CapabilityStatement is authoritative: only what it lists is accepted.
        self.known = {res["type"]: {sp["name"] for sp in res.get("searchParam", [])}
                      for rest in self.capability.get("rest", []) for res in rest.get("resource", [])}
        self.last_updated = date_range(self.settings["last_updated"], self.tz)[:2]
        self._ids = {}
        self._contained = "false"
        self._alias = count(1)

    # ---- lookups -------------------------------------------------------
    def sid(self, value):
        if value not in self._ids:
            row = self.db.execute("SELECT id FROM strings WHERE value = ?", (value,)).fetchone()
            self._ids[value] = row[0] if row else -1
        return self._ids[value]

    def alias(self, prefix="t"):
        return f"{prefix}{next(self._alias)}"

    def param(self, rtype, code):
        known = self.known.get(rtype, set())
        if code not in known:
            raise SearchError(400, f'Unknown or unsupported search parameter "{code}" for resource type "{rtype}". '
                                   f"Supported parameters: [{', '.join(sorted(known))}]", "not-supported")
        return self.params.get((rtype, code))

    def _type(self, rtype):
        if rtype not in self.known:
            raise SearchError(404, f'Resource type "{rtype}" is not supported by this server. '
                                   f"Supported types: [{', '.join(sorted(self.known))}]", "not-supported")
        return rtype

    # ---- request entry point --------------------------------------------
    def _check_multiple(self, rtype, name, value, repeats):
        """The definition's multipleOr / multipleAnd (false: one value per parameter / one occurrence)."""
        if name.startswith("_has:") or "." in name:
            return
        code = name.split(":", 1)[0]
        p = self.params.get((rtype, code))
        if not p or code not in self.known.get(rtype, ()):
            return
        if p.get("multiple_or") == 0 and len(split_or(value)) > 1:
            raise SearchError(400, f"{code} allows one value per parameter (multipleOr = false); repeat the parameter instead")
        repeats[code] = repeats.get(code, 0) + 1
        if p.get("multiple_and") == 0 and repeats[code] > 1:
            raise SearchError(400, f"{code} may appear only once (multipleAnd = false)")

    def _is_element(self, rtype, name):
        """A top-level element name, or a choice element with its type (value or valueQuantity)."""
        if name in self.meta.get("elements", {}).get(rtype, ()):
            return True
        return any(name.startswith(c) and name[len(c):][:1].isupper() and name[len(c):] in CHOICE_TYPES
                   for c in self.meta.get("choice_elements", {}).get(rtype, ()))

    def post(self, path, body=b"", content_type="application/x-www-form-urlencoded", handling="strict"):
        """POST [base]/[type]/_search, [base]/_search or [base]/[compartment]/[id]/[type]/_search:
        the same search as GET, with parameters from the URL and the form-encoded body (in that order).
        On a writable connection POST [type] with a JSON resource body is create."""
        try:
            parts = urlsplit(path)
            route = parts.path.rstrip("/")
            if not route.endswith("_search") or (route != "_search" and not route.endswith("/_search")):
                segs = [unquote(s) for s in route.strip("/").split("/") if s]
                if self.writable and len(segs) == 1 and segs[0] != "metadata" and not segs[0].startswith(("$", "_")):
                    if parts.query:
                        raise SearchError(400, "Conditional create is not supported", "not-supported")
                    return self._write(lambda: self.writer.create(self._type(segs[0]), self._body(body, content_type)))
                raise SearchError(405, "Only POST .../_search (search) is supported" + (
                    "" if self.writable else ": this store is opened read-only"), "not-supported")
            if body and content_type.split(";")[0].strip().lower() != "application/x-www-form-urlencoded":
                raise SearchError(415, "POST _search needs an application/x-www-form-urlencoded body", "not-supported")
            text = body.decode("utf-8") if isinstance(body, bytes) else (body or "")
            query = "&".join(q for q in (parts.query, text.strip()) if q)
            target = route[: -len("_search")].rstrip("/") or "/"
            segs = [x for x in target.strip("/").split("/") if x]
            if not (len(segs) in (0, 1, 3)) or segs[:1] == ["metadata"] or "_history" in segs:
                raise SearchError(400, "POST _search applies to [base], [type] and [compartment]/[id]/[type] only",
                                  "not-supported")
        except SearchError as exc:
            return exc.status, operation_outcome(exc)
        except UnicodeDecodeError:
            return 400, operation_outcome(SearchError(400, "POST _search body is not UTF-8"))
        return self.get(target + (f"?{query}" if query else ""), handling)

    def get(self, path, handling="strict"):
        """Answer one GET (path relative to the base). handling: 'strict' rejects unknown
        search parameters; 'lenient' ignores them and reports them in an outcome entry."""
        try:
            self._sync()
            return 200, self._get(path, handling)
        except SearchError as exc:
            return exc.status, operation_outcome(exc)

    # ---- writes (writable connections) ----------------------------------------------------
    def put(self, path, body, content_type="application/fhir+json", if_match=None):
        """PUT [type]/[id]: update, or create with this id. if_match: a version ('2' or W/"2")."""
        def op():
            rtype, rid = self._instance(path)
            return self.writer.update(rtype, rid, self._body(body, content_type), _version_tags(if_match))
        return self._write(op)

    def delete(self, path, if_match=None):
        """DELETE [type]/[id]: (204, None). The deletion's versionId and lastUpdated are in writer.deleted."""
        def op():
            rtype, rid = self._instance(path)
            return self.writer.delete(rtype, rid, _version_tags(if_match))
        return self._write(op)

    def _write(self, op):
        try:
            if not self.writable:
                raise SearchError(405, "This store is opened read-only", "not-supported")
            self._sync()
            self.db.execute("BEGIN IMMEDIATE")
            try:
                self.writer.begin()
                status, body = op()
                self.writer.finish()
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
        except SearchError as exc:
            return exc.status, operation_outcome(exc)
        self._results.clear()
        self._ids.clear()
        self.has_written = True
        self._writes += 1
        if self._writes == 100 or self._writes % 1000 == 0:
            self._optimize()
        self.meta["resource_counts"] = self.writer.counts
        if self.writer.changed:
            self._state()
        return status, body

    def _instance(self, path):
        parts = urlsplit(path)
        if parts.query:
            raise SearchError(400, "Conditional update and delete are not supported", "not-supported")
        segs = [unquote(s) for s in parts.path.strip("/").split("/") if s]
        if len(segs) != 2 or segs[1].startswith(("_", "$")):
            raise SearchError(405, "PUT and DELETE apply to [type]/[id] only", "not-supported")
        return self._type(segs[0]), segs[1]

    @staticmethod
    def _body(body, content_type):
        if isinstance(body, dict):
            return body
        media = (content_type or "application/fhir+json").split(";")[0].strip().lower()
        if media not in JSON_FORMATS:
            raise SearchError(415, f"The body must be JSON (application/fhir+json), not {media}", "not-supported")
        try:
            return json.loads(body.decode("utf-8") if isinstance(body, bytes) else body)
        except (UnicodeDecodeError, ValueError) as exc:
            raise SearchError(400, f"The body is not valid JSON: {exc}", "structure") from None

    def _get(self, path, handling):
        parts = urlsplit(path)
        for name, value in parse_qsl(parts.query, keep_blank_values=True):
            # an unescaped '+' in application/fhir+json arrives as a space after form decoding
            if name == "_format" and value and value.split(";")[0].strip().lower().replace(" ", "+") not in JSON_FORMATS:
                raise SearchError(406, f'Unsupported _format "{value}": only JSON is supported', "not-supported")
        segments = [unquote(s) for s in parts.path.strip("/").split("/") if s]
        if any(s.startswith("$") for s in segments):
            raise SearchError(400, "Operations are not supported (read-only search endpoint)", "not-supported")
        if "_history" in segments and not (len(segments) == 4 and segments[2] == "_history"):
            if self.history_enabled and len(segments) == 3 and segments[2] == "_history":
                return self.history(self._type(segments[0]), segments[1], parts.query, handling)
            if self.history_enabled:
                raise SearchError(400, "Type and system history are not supported (instance history only)",
                                  "not-supported")
            raise SearchError(400, "The history interaction is not supported (read-only search store; every resource "
                                   "has version 1)", "not-supported")
        if not segments:
            pairs = dict(parse_qsl(parts.query, keep_blank_values=True))
            if pairs.get("_page"):
                return self.page(pairs, handling)
            return self.search(None, parts.query, 0, handling=handling)        # whole-system search
        if segments == ["metadata"]:
            return self.capability
        if len(segments) == 1:
            return self.search(self._type(segments[0]), parts.query, 0, handling=handling)
        if len(segments) == 2:
            return self._read_params(self.read(self._type(segments[0]), segments[1]), parts.query, handling)
        if len(segments) == 4 and segments[2] == "_history":                      # vread
            return self._read_params(self.vread(self._type(segments[0]), segments[1], segments[3]), parts.query, handling)
        if len(segments) == 3:                                                    # compartment search
            comp, cid, rtype = self._type(segments[0]), segments[1], segments[2]
            if comp not in self.compartments:
                raise SearchError(400, f"{comp} is not a compartment on this server")
            if rtype == "*":                                                      # every type in the compartment
                return self.search(None, parts.query, 0, handling=handling, compartment=[comp, cid])
            return self.search(self._type(rtype), parts.query, 0, handling=handling, compartment=[comp, cid])
        raise SearchError(400, f"Unsupported request path: {parts.path}")

    def read(self, rtype, rid):
        row = self.db.execute("SELECT r.json FROM resources r WHERE r.type_id = ? AND r.id = ? AND r.container IS NULL",
                              (self.sid(rtype), rid)).fetchone()
        if not row:
            last = self.db.execute("SELECT json IS NULL FROM versions WHERE type_id = ? AND id = ? "
                                   "ORDER BY version DESC LIMIT 1", (self.sid(rtype), rid)).fetchone()
            if last and last[0]:
                raise SearchError(410, f"Resource {rtype}/{rid} was deleted", "deleted")
            raise SearchError(404, f"Resource {rtype}/{rid} is not known", "not-found")
        return json.loads(zlib.decompress(row[0]))

    def _read_params(self, resource, query, handling):
        """read / vread take _summary, _elements, _format and _pretty; anything else is an
        error under strict handling and ignored under lenient."""
        controls = {}
        for name, value in parse_qsl(query, keep_blank_values=True):
            if value == "":
                continue
            if name not in READ_PARAMETERS:
                if handling == "lenient":
                    continue
                raise SearchError(400, f'Parameter "{name}" is not supported on read (only _summary, _elements, '
                                       f'_format, _pretty)', "not-supported")
            controls.setdefault(name, []).append(value)
        for name, values in controls.items():
            problem = _control_problem(name, values)
            if problem or (name == "_summary" and values[-1] == "count"):
                raise SearchError(400, problem or "_summary=count does not apply to read", "not-supported")
        summary = (controls.get("_summary") or [None])[-1]
        elements = [e for v in controls.get("_elements", []) for e in v.split(",") if e]
        if summary and elements:
            raise SearchError(400, "_summary and _elements cannot be used together")
        for name in elements:
            if not self._is_element(resource["resourceType"], name):
                raise SearchError(400, f'_elements: "{name}" is not an element of {resource["resourceType"]}')
        if summary in ("true", "text", "data") or elements:
            return self.subset(resource, summary, elements)
        return resource

    def vread(self, rtype, rid, vid):
        try:
            resource = self.read(rtype, rid)
        except SearchError as exc:
            if exc.status != 410:
                raise
            resource = None
        if resource is not None and (resource.get("meta") or {}).get("versionId") == vid:
            return resource
        if vid.isdigit():
            row = self.db.execute("SELECT json FROM versions WHERE type_id = ? AND id = ? AND version = ?",
                                  (self.sid(rtype), rid, int(vid))).fetchone()
            if row and row[0] is None:
                raise SearchError(410, f"Version {vid} of {rtype}/{rid} is its deletion", "deleted")
            if row:
                return json.loads(zlib.decompress(row[0]))
        raise SearchError(404, f"Version {vid} of {rtype}/{rid} is not known", "not-found")

    def history(self, rtype, rid, query, handling):
        """GET [type]/[id]/_history: every version, newest first (_count, _since)."""
        controls = {}
        for name, value in parse_qsl(query, keep_blank_values=True):
            if value == "":
                continue
            if name not in ("_count", "_since", "_format", "_pretty", "_offset"):
                if handling == "lenient":
                    continue
                raise SearchError(400, f'Parameter "{name}" is not supported on history (only _count, _since, '
                                       f'_format, _pretty)', "not-supported")
            if name in controls:
                raise SearchError(400, f"{name} may appear only once on history")
            controls[name] = value
        size = self.page_size(controls.get("_count"))
        try:
            offset = int(controls.get("_offset", "0"))
            since = date_range(controls["_since"], self.tz)[0] if "_since" in controls else None
        except ValueError as exc:
            raise SearchError(400, f"Invalid history parameter: {exc}") from None
        if offset < 0:
            raise SearchError(400, "Invalid history page")
        tid, items = self.sid(rtype), []
        row = self.db.execute("SELECT pid, json FROM resources WHERE type_id = ? AND id = ? AND container IS NULL",
                              (tid, rid)).fetchone()
        if row:
            resource = json.loads(zlib.decompress(row[1]))
            method, status = self.db.execute("SELECT method, status FROM written WHERE pid = ?",
                                             (row[0],)).fetchone() or ("PUT", 201)
            items.append((int(resource["meta"]["versionId"]), resource["meta"]["lastUpdated"], method, status, resource))
        for version, updated, method, status, blob in self.db.execute(
                "SELECT version, updated, method, status, json FROM versions WHERE type_id = ? AND id = ?", (tid, rid)):
            items.append((version, updated, method, status, json.loads(zlib.decompress(blob)) if blob else None))
        if not items:
            raise SearchError(404, f"Resource {rtype}/{rid} is not known", "not-found")
        items.sort(key=lambda item: -item[0])
        if since is not None:
            items = [i for i in items if date_range(i[1], self.tz)[0] >= since]
        entries = []
        for version, updated, method, status, resource in (items[offset:offset + size] if size else []):
            entry = {"fullUrl": f"{self.base}/{rtype}/{rid}"}
            if resource is not None:
                entry["resource"] = resource
            entry["request"] = {"method": method, "url": rtype if method == "POST" else f"{rtype}/{rid}"}
            entry["response"] = {"status": f"{status} {REASONS.get(status, '')}".strip(), "etag": f'W/"{version}"',
                                 "lastModified": updated}
            entries.append(entry)

        def link(relation, start):
            q = [f"_count={size}"] + ([f"_since={quote(controls['_since'], safe=':')}"] if since is not None else [])
            q += [f"_offset={start}"] if start else []
            return {"relation": relation, "url": f"{self.base}/{rtype}/{rid}/_history?" + "&".join(q)}

        links = [link("self", offset)]
        if size and offset + size < len(items):
            links.append(link("next", offset + size))
        if offset > 0:
            links.append(link("previous", max(0, offset - size)))
        bundle = {"resourceType": "Bundle", "id": str(uuid.uuid4()),
                  "meta": {"lastUpdated": datetime.now(timezone.utc).isoformat(timespec="milliseconds")},
                  "type": "history", "total": len(items), "link": links}
        if entries:
            bundle["entry"] = entries
        return bundle

    def page(self, pairs, handling):
        try:
            rtype, query, compartment = json.loads(zlib.decompress(base64.urlsafe_b64decode(pairs["_page"].encode())))
            offset = int(pairs.get("_offset", "0"))
            if not ((rtype is None or isinstance(rtype, str)) and isinstance(query, str) and offset >= 0
                    and (compartment is None or (isinstance(compartment, list) and len(compartment) == 2
                                                 and all(isinstance(x, str) for x in compartment)))):
                raise ValueError("malformed page state")
        except Exception as exc:                       # any tampering is a client error, never a crash
            raise SearchError(400, "Invalid page link") from exc
        return self.search(rtype, query, offset, count_override=pairs.get("_count"), handling=handling,
                           compartment=compartment, paged=True)

    def _page_link(self, relation, state, offset, size):
        token = base64.urlsafe_b64encode(zlib.compress(json.dumps(state).encode())).decode()
        return {"relation": relation, "url": f"{self.base}?_page={token}&_offset={offset}&_count={size}"}

    # ---- search -----------------------------------------------------------
    def search(self, rtype, query, offset, count_override=None, handling="strict", compartment=None, paged=False):
        """Type search (rtype), whole-system search (rtype None; _type or every type) or
        compartment search (compartment = [type, id])."""
        try:
            pairs = parse_qsl(query, keep_blank_values=True, strict_parsing=bool(query))
        except ValueError as exc:
            raise SearchError(400, f"Invalid search query: {exc}") from exc
        controls, criteria = {}, []
        types_hint = [rtype] if rtype else None
        for name, value in pairs:
            if value == "":
                continue                                   # R4: empty parameters are ignored
            if name.split(":", 1)[0] in CONTROL:
                controls.setdefault(name, []).append(value)
            elif name == "_type" and rtype is None:
                controls.setdefault("_type", []).append(value)
            else:
                criteria.append((name, value))
        # Result parameters the store cannot honour are never silently dropped: an error under
        # handling=strict, an ignored-parameter warning under lenient.
        dropped = []
        for name in list(controls):
            problem = _control_problem(name, controls[name])
            if problem:
                if handling != "lenient":
                    raise SearchError(400, problem, "not-supported")
                dropped.append(name)
                del controls[name]
        size = self.page_size(count_override or (controls.get("_count") or [None])[-1])
        if controls.get("_summary") and controls.get("_elements"):
            raise SearchError(400, "_summary and _elements cannot be used together")
        if (controls.get("_summary") or [None])[-1] == "text" and any(k.split(":")[0] in ("_include", "_revinclude")
                                                                     for k in controls):
            raise SearchError(400, "_include and _revinclude cannot be mixed with _summary=text")
        contained = (controls.get("_contained") or ["false"])[-1]
        container_mode = (controls.get("_containedType") or ["container"])[-1] == "container"
        if controls.get("_containedType") and contained == "false":
            raise SearchError(400, "_containedType needs _contained=true or _contained=both")
        if contained != "false" and container_mode and controls.get("_sort"):
            raise SearchError(400, "_sort cannot be combined with returning container resources (_containedType=container)")
        self._contained = contained
        for name in (e for v in controls.get("_elements", []) for e in v.split(",") if e):
            if not any(self._is_element(t, name) for t in (types_hint or self.known)):
                raise SearchError(400, f'_elements: "{name}" is not an element of {" or ".join(types_hint or ["the searched types"])}')
        if rtype is None:
            types = set(self.known)
            for v in controls.get("_type", []):            # repeated _type parameters are AND
                types &= {self._type(t) for t in v.split(",") if t}
            if compartment:
                types &= {t for t, codes in self.compartments.get(compartment[0], {}).items() if codes}
            types = sorted(types)
            if not types:
                return self._empty_bundle()
        else:
            types = [rtype]
        self._alias = count(1)          # same query -> same SQL, so later pages hit the result cache
        plans, ignored = [], []
        for t in types:
            crits, repeats = [], {}
            for name, value in criteria:
                self._check_multiple(t, name, value, repeats)
                try:
                    crits.append(self.criterion(t, name, value))
                except SearchError as exc:
                    if handling != "lenient" or exc.code != "not-supported":
                        raise
                    ignored.append(name)
            if compartment:
                crits.append(self.compartment_crit(t, *compartment))
            plans.append((t, crits))
        sort_keys = []
        for v in controls.get("_sort", []):
            for key in (k for k in v.split(",") if k):
                name = key.lstrip("-")
                if name not in ("_id", "_lastUpdated") and name not in self.known.get(types[0], ()):
                    if handling != "lenient":
                        self.param(types[0], name)          # raises the unknown-parameter error
                    ignored.append("_sort")
                    continue
                sort_keys.append(key)
        if "_sort" in controls:
            controls["_sort"] = [",".join(sort_keys)] if sort_keys else []
        ignored = sorted(set(ignored) | set(dropped))
        self.check_includes(types, controls)
        if rtype is None and any(k.lstrip("-") not in ("_id", "_lastUpdated")
                                 for v in controls.get("_sort", []) for k in v.split(",") if k):
            raise SearchError(400, "Whole-system search can sort only by _id or _lastUpdated")
        order = self.order(types[0], controls.get("_sort", []))
        pids, total = self.run(plans, order, size, offset,
                               project="COALESCE(r.container, r.pid)" if contained != "false" and container_mode else None)
        summary = (controls.get("_summary") or [None])[-1]
        bundle = {"resourceType": "Bundle", "id": str(uuid.uuid4()),
                  "meta": {"lastUpdated": datetime.now(timezone.utc).isoformat(timespec="milliseconds")},
                  "type": "searchset"}
        # _count=0 is answered like _summary=count: the total, no entries.
        if (controls.get("_total") or [None])[-1] != "none" or summary == "count" or size == 0:
            bundle["total"] = total
        # self: the parameters actually used (R4 conformance): no empty or ignored ones, the
        # last value of single-valued result parameters, _count as applied
        effective, seen_single = [], set()
        for n, v in reversed(pairs):
            base = n.split(":", 1)[0]
            if v == "" or n in ignored or base in ignored:
                continue
            if base in SINGLE_CONTROLS:
                if base in seen_single:
                    continue
                seen_single.add(base)
                if base == "_count":
                    v = str(size)
            if base == "_sort":
                if not controls.get("_sort") or not controls["_sort"][0]:
                    continue
                v = controls["_sort"][0]
                if "_sort" in seen_single:
                    continue
                seen_single.add("_sort")
            effective.append((n, v))
        used = "&".join(f"{quote(n, safe=':._-')}={quote(v, safe='|:/$,._-')}" for n, v in reversed(effective))
        path = (f"/{compartment[0]}/{compartment[1]}/{rtype or '*'}" if compartment else f"/{rtype}" if rtype else "")
        links = [{"relation": "self", "url": f"{self.base}{path}" + (f"?{used}" if used else "")}]
        state = [rtype, query, compartment]
        if paged:
            links = [self._page_link("self", state, offset, size)]
        if summary != "count" and size:
            if offset + size < total:
                links.append(self._page_link("next", state, offset + size, size))
            if offset > 0:
                links.append(self._page_link("previous", state, max(0, offset - size), size))
        bundle["link"] = links
        entries = []
        if summary != "count" and pids:
            matches = self.fetch(pids)
            included = self.include(matches, controls)
            elements = [e for v in controls.get("_elements", []) for e in v.split(",") if e]
            entries = [self.entry(r, "match", summary, elements) for r in matches]
            entries += [self.entry(r, "include", None, []) for r in included]
        if ignored:
            entries.append({"resource": {"resourceType": "OperationOutcome", "issue": [
                {"severity": "warning", "code": "not-supported",
                 "diagnostics": f"Ignored unknown or unsupported search parameters: {', '.join(ignored)}"}]},
                "search": {"mode": "outcome"}})
        if entries:
            bundle["entry"] = entries
        return bundle

    def _empty_bundle(self):
        return {"resourceType": "Bundle", "id": str(uuid.uuid4()),
                "meta": {"lastUpdated": datetime.now(timezone.utc).isoformat(timespec="milliseconds")},
                "type": "searchset", "total": 0}

    def page_size(self, value):
        if value is None:
            return self.settings["default_count"]
        try:
            size = int(value)
        except ValueError as exc:
            raise SearchError(400, f'Invalid _count value: "{value}"') from exc
        if size < 0:
            raise SearchError(400, f'Invalid _count value: "{value}"')
        return min(size, self.settings["max_count"])

    def _where(self, rtype, crits):
        conds, args = [f"r.type_id = {self.sid(rtype)}"], []
        scope = {"false": "r.container IS NULL", "true": "r.container IS NOT NULL"}.get(self._contained)
        if scope:
            conds.append(scope)
        driver = min((c for c in crits if c.driver), key=lambda c: c.rank, default=None)
        if driver:
            conds.append(f"r.pid IN ({driver.driver})")
            args += driver.driver_args
        for c in crits:
            if c is not driver:
                sql, a = c.on("r")
                conds.append(sql)
                args += a
        return " AND ".join(conds), args

    def run(self, plans, order, size, offset, project=None):
        parts, args = [], []
        for rtype, crits in plans:
            where, a = self._where(rtype, crits)
            parts.append(where)
            args += a
        where = " OR ".join(f"({w})" for w in parts) if len(parts) > 1 else parts[0]
        # Search-result cache: an ordered result list is computed once and later pages
        # slice it. Very large results page with LIMIT/OFFSET instead.
        key = (where, tuple(args), order, project)
        cached = self._results.get(key)
        # project: return containers of matched contained resources (one entry per container, store order)
        select = (f"SELECT {project} AS k FROM resources r WHERE {where} GROUP BY k ORDER BY MIN(r.pid)" if project
                  else f"SELECT r.pid FROM resources r WHERE {where} ORDER BY {order}")
        if cached is None:
            counter = f"SELECT COUNT(DISTINCT {project}) FROM resources r WHERE {where}" if project else \
                f"SELECT COUNT(*) FROM resources r WHERE {where}"
            total = self.db.execute(counter, args).fetchone()[0]
            if total <= RESULT_CACHE_MAX:
                cached = [row[0] for row in self.db.execute(select, args)]
                self._results[key] = cached
                while len(self._results) > RESULT_CACHE_ENTRIES:
                    self._results.pop(next(iter(self._results)))
        if cached is not None:
            return cached[offset:offset + size] if size else [], len(cached)
        if not size or offset >= total:
            return [], total
        return [row[0] for row in self.db.execute(select + " LIMIT ? OFFSET ?", args + [size, offset])], total

    def compartment_crit(self, rtype, comp, cid):
        """Membership of `comp/cid`'s compartment: any of the CompartmentDefinition's parameters
        for this type references it ({def}: the compartment resource itself)."""
        codes = self.compartments.get(comp, {}).get(rtype)
        if not codes:
            raise SearchError(400, f"{rtype} is not in the {comp} compartment")
        crits = [self.id_crit(rtype, None, cid) if code == "{def}" else self.reference_crit(self.params[(rtype, code)], None, f"{comp}/{cid}")
                 for code in codes if code == "{def}" or (rtype, code) in self.params]
        if not crits:
            raise SearchError(400, f"{rtype} is not in the {comp} compartment")
        return _any(crits)

    def fetch(self, pids):
        if not pids:
            return []
        rows = {pid: (rid, body) for pid, rid, body in
                self.db.execute(f"SELECT pid, id, json FROM resources WHERE pid IN ({','.join('?' * len(pids))})", pids)}
        return [(pid, json.loads(zlib.decompress(rows[pid][1])), rows[pid][0]) for pid in pids if pid in rows]

    def entry(self, item, mode, summary, elements):
        resource, rowid = item[1], (item[2] if len(item) > 2 else "")
        if summary in ("true", "text", "data") or elements:
            resource = self.subset(resource, summary, elements)
        # a contained resource is identified by its container's URL and the local id
        full = f"{self.base}/{rowid}" if "#" in rowid else f"{self.base}/{resource['resourceType']}/{resource['id']}"
        return {"fullUrl": full, "resource": resource, "search": {"mode": mode}}

    def subset(self, resource, summary, elements):
        rtype = resource["resourceType"]
        keep_always = {"resourceType", "id", "meta"}
        if summary == "data":
            out = {k: v for k, v in resource.items() if k != "text"}
        else:
            if summary == "true":
                names = set(self.meta["summary_elements"].get(rtype, []))
            elif summary == "text":
                names = {"text"} | set(self.meta["mandatory_elements"].get(rtype, []))
            else:
                names = set(elements) | set(self.meta["mandatory_elements"].get(rtype, []))
            out = {k: v for k, v in resource.items()
                   if k in keep_always or k in names or any(k.startswith(n) and k[len(n):][:1].isupper() for n in names)}
        meta = dict(out.get("meta", {}))
        meta["tag"] = list(meta.get("tag", [])) + [SUBSETTED]
        out["meta"] = meta
        return out

    # ---- includes -----------------------------------------------------------
    def include(self, matches, controls):
        # :iterate follows new resources until nothing new is found; `seen` (store rows) ends cycles.
        seen = {item[0] for item in matches}
        out = []
        frontier = matches
        specs = [(k, v) for k, values in controls.items() for v in values if k.split(":")[0] == "_include"]
        rev = [(k, v) for k, values in controls.items() for v in values if k.split(":")[0] == "_revinclude"]
        depth = 0
        while frontier and (specs or rev):
            found = []
            for key, value in specs:
                if depth and not key.endswith((":iterate", ":recurse")):
                    continue
                found += self.forward(value, frontier)
            for key, value in rev:
                if depth and not key.endswith((":iterate", ":recurse")):
                    continue
                found += self.reverse(value, frontier)
            fresh = []
            for item in found:
                if item[0] not in seen:
                    seen.add(item[0])
                    fresh.append(item)
            out += fresh
            frontier = fresh
            depth += 1
        return out

    def check_includes(self, types, controls):
        """Reject _include/_revinclude values that cannot apply to this search (instead of
        silently returning nothing): unknown parameters, a source or target type that does not
        fit the searched type, or a wrong target type. :iterate values may start from included
        resources, so only their syntax is checked."""
        for key, values in controls.items():
            base, _, modifier = key.partition(":")
            if base not in ("_include", "_revinclude"):
                continue
            reverse = base == "_revinclude"
            for value in values:
                ps, target = self._include_params(value, reverse)
                if ps is None:
                    continue
                name = "_revinclude" if reverse else "_include"
                source = value.split(":")[0]
                reach = {t for p in ps for t in p["targets"]}
                if target and target not in reach:
                    raise SearchError(400, f'{name}={value}: no {source} reference parameter of this name can reference {target}')
                if modifier:
                    continue
                if not reverse and source not in types:
                    raise SearchError(400, f'_include={value}: source type {source} is not the searched type')
                if reverse and not set(types) & ({target} if target else reach):
                    raise SearchError(400, f'_revinclude={value}: {source} cannot reference {" or ".join(types)}')

    def _include_params(self, value, reverse):
        """(reference parameters, target type) of an include value; None = every reference
        parameter ('*'). 'Type:*' is every reference parameter of Type."""
        if value == "*":
            return None, None
        parts = value.split(":")
        bad = SearchError(400, f'Invalid {"_revinclude" if reverse else "_include"} parameter value: "{value}"')
        if len(parts) not in (2, 3):
            raise bad
        target = parts[2] if len(parts) == 3 else None
        if parts[1] == "*":
            ps = [p for (t, c), p in self.params.items()
                  if t == parts[0] and p["ptype"] == "reference" and c in self.known.get(t, ())]
            if not ps:
                raise bad
            return ps, target
        p = self.params.get((parts[0], parts[1]))
        if p is None or p["ptype"] != "reference" or parts[1] not in self.known.get(parts[0], ()):
            raise bad
        return [p], target

    def forward(self, value, frontier):
        ps, target = self._include_params(value, False)
        pids = [pid for pid, *_ in frontier]
        if ps is None:
            types = {r["resourceType"] for _, r, *_ in frontier}
            ids = [q["id"] for (t, c), q in self.params.items()
                   if t in types and q["ptype"] == "reference" and c in self.known.get(t, ())]
        else:
            pids = [pid for pid, r, *_ in frontier if r["resourceType"] == ps[0]["type"]]
            ids = [p["id"] for p in ps]
        if not pids or not ids:
            return []
        rows = self.db.execute(
            f"SELECT DISTINCT s.value, t.target_id FROM sp_ref t JOIN strings s ON s.id = t.target_type "
            f"WHERE t.pid IN ({','.join('?' * len(pids))}) AND t.param IN ({','.join(map(str, ids))})", pids).fetchall()
        rows = [r for r in rows if target is None or r[0] == target]
        return self.by_keys(rows)

    def reverse(self, value, frontier):
        ps, target = self._include_params(value, True)
        # contained resources cannot be referenced from outside their container
        keys = [(r["resourceType"], r["id"]) for _, r, *rest in frontier
                if (target is None or r["resourceType"] == target) and "#" not in (rest[0] if rest else "")]
        if not keys:
            return []
        found = []
        for rtype in {k[0] for k in keys}:
            ids = [k[1] for k in keys if k[0] == rtype]
            # '*': every listed reference parameter (stored references are not limited to declared targets)
            params = ps if ps is not None else [q for (t, c), q in self.params.items()
                                                 if q["ptype"] == "reference" and c in self.known.get(t, ())]
            rows = self.db.execute(
                f"SELECT DISTINCT t.pid FROM sp_ref t JOIN resources rr ON rr.pid = t.pid AND rr.container IS NULL "
                f"WHERE t.param IN ({','.join(str(q['id']) for q in params)}) AND t.target_type = ? "
                f"AND t.target_id IN ({','.join('?' * len(ids))}) ORDER BY t.pid", [self.sid(rtype)] + ids).fetchall()
            found += [r[0] for r in rows]
        return self.fetch(sorted(set(found)))

    def by_keys(self, keys):
        pids = []
        for rtype, rid in keys:
            row = self.db.execute("SELECT pid FROM resources WHERE type_id = ? AND id = ? AND container IS NULL",
                                  (self.sid(rtype), rid)).fetchone()
            if row:
                pids.append(row[0])
        return self.fetch(sorted(set(pids)))

    # ---- ordering ------------------------------------------------------------
    def order(self, rtype, values):
        terms = []
        for value in values:
            for key in value.split(","):
                if not key:
                    continue
                desc, name = key.startswith("-"), key.lstrip("-")
                if name == "_id":
                    expr = "r.id"
                elif name == "_lastUpdated":
                    if not self.has_written:     # all equal: the load instant
                        continue
                    expr = f"COALESCE((SELECT w.low FROM written w WHERE w.pid = r.pid), {self.last_updated[0]})"
                else:
                    p = self.param(rtype, name)
                    column = SORT_COLUMN.get(p["ptype"])
                    if column is None:
                        raise SearchError(400, f"Cannot sort on {p['ptype']} parameter {name}")
                    agg = "MAX" if desc else "MIN"
                    expr = f"(SELECT {agg}(s.{column}) FROM {TABLE[p['ptype']]} s WHERE s.pid = r.pid AND s.param = {p['id']})"
                # Resources without a value for the sort parameter come last in either direction.
                terms.append(f"({expr}) IS NULL, {expr} {'DESC' if desc else 'ASC'}")
        return ", ".join(terms + ["r.pid ASC"])

    # ---- criteria -----------------------------------------------------------
    def criterion(self, rtype, name, value):
        if name.startswith("_has:"):
            return self.has(rtype, name, value)
        head, dot, rest = name.partition(".")
        code, _, modifier = head.partition(":")
        if dot:
            return self.chain(rtype, code, modifier or None, rest, value)
        if code == "_id":
            return self.id_crit(rtype, modifier, value)
        if code == "_list":
            return self.list_crit(rtype, modifier, value)
        if code == "_lastUpdated":
            return self.last_updated_crit(modifier, value)
        p = self.param(rtype, code)
        modifier = modifier or None
        if code in ("_content", "_text"):
            return self.fulltext_crit(code, modifier, value)
        if modifier == "missing":
            if p["ptype"] == "composite":
                raise SearchError(400, f"Modifiers are not used on composite parameters ({code})")
            if p["ptype"] == "special":
                return self.missing({**p, "ptype": "position"}, value)
            return self.missing(p, value)
        if p["ptype"] == "special":
            return self.near_crit(p, modifier, value)
        builder = getattr(self, f"{p['ptype']}_crit", None)
        if builder is None:
            raise SearchError(400, f"Search parameter type {p['ptype']} ({code}) is not supported")
        return builder(p, modifier, value)

    def _exists(self, table, p, where, args, rank, negate=False):
        t = self.alias()
        where_t = re.sub(r"\bt\.", f"{t}.", where)
        cond = f"{'NOT ' if negate else ''}EXISTS (SELECT 1 FROM {table} {t} WHERE {t}.pid = {{a}}.pid AND {t}.param = {p['id']} AND ({where_t}))"
        driver = None if negate else f"SELECT {t}.pid FROM {table} {t} WHERE {t}.param = {p['id']} AND ({where_t})"
        return Crit(cond, args, driver, args, rank)

    def missing(self, p, value):
        if value not in ("true", "false"):
            raise SearchError(400, f'Invalid :missing value "{value}"')
        # A reference may be stored as a relative, absolute/canonical or identifier-only reference.
        tables = ["sp_ref", "sp_ref_url", "sp_ref_ident"] if p["ptype"] == "reference" else [TABLE[p["ptype"]]]
        parts = []
        for table in tables:
            t = self.alias()
            parts.append(f"EXISTS (SELECT 1 FROM {table} {t} WHERE {t}.pid = {{a}}.pid AND {t}.param = {p['id']})")
        cond = "(" + " OR ".join(parts) + ")"
        return Crit(("NOT " + cond) if value == "true" else cond)

    def id_crit(self, rtype, modifier, value):
        if modifier:
            raise SearchError(400, f"Unsupported modifier :{modifier} for _id")
        ids = [unescape(v) for v in split_or(value)]
        marks = ",".join("?" * len(ids))
        return Crit(f"{{a}}.id IN ({marks})", ids,
                    f"SELECT pid FROM resources WHERE type_id = {self.sid(rtype)} AND id IN ({marks})", ids, RANK["_id"])

    def last_updated_crit(self, modifier, value):
        if modifier:
            raise SearchError(400, f"Unsupported modifier :{modifier} for _lastUpdated")
        t_low, t_high = self.last_updated
        ok, clauses, args = False, [], []
        for v in split_or(value):
            prefix, low, high = self._date_bounds(v)
            approx = self._approx(low, high)
            ok = ok or date_eval(prefix, t_low, t_high, low, high, approx)
            sql, a = _date_sql(prefix, "w.low", "w.high", low, high, approx)
            clauses.append(sql)
            args += a
        if not self.has_written:                 # every resource carries the load instant
            return Crit("1" if ok else "0")
        # Written resources have their own lastUpdated (table `written`); loaded ones the load instant.
        w, where = self.alias("w"), " OR ".join(clauses)
        where_w = re.sub(r"\bw\.", f"{w}.", where)
        written = f"EXISTS (SELECT 1 FROM written {w} WHERE {w}.pid = {{a}}.pid AND ({where_w}))"
        if not ok:
            return Crit(written, args, f"SELECT {w}.pid FROM written {w} WHERE ({where_w})", args, RANK["date"])
        return Crit(f"({written} OR NOT EXISTS (SELECT 1 FROM written {w}x WHERE {w}x.pid = {{a}}.pid))", args)

    def _date_bounds(self, value):
        prefix, text = split_prefix(value)
        try:
            low, high, _ = date_range(text, self.tz)
        except ValueError as exc:
            raise SearchError(400, f'Invalid date/time value: "{text}"') from exc
        return prefix, low, high

    def _approx(self, low, high):
        """ap window: the value range widened by 10% of its distance from the store's 'now'
        (its load instant, so answers do not drift with wall-clock time). A store opened for writing
        uses the current time, from the writer's clock when this connection writes."""
        if self.settings.get("writable"):
            clock = self.writer.clock() if self.writer else datetime.now(timezone.utc)
            now = date_range(clock.astimezone(timezone.utc).isoformat(timespec="microseconds"), self.tz)[0]
        else:
            now = self.last_updated[0]
        gap = abs((low + high) // 2 - now) // 10
        return low - gap, high + gap

    def _date_sql(self, prefix, low_col, high_col, low, high):
        return _date_sql(prefix, low_col, high_col, low, high, self._approx(low, high))

    def fulltext_crit(self, code, modifier, value):
        """_content (all text of the resource) / _text (narrative): word match, case- and
        accent-insensitive; terms in one value must all occur, comma-separated values are OR."""
        if modifier:
            raise SearchError(400, f"Unsupported modifier :{modifier} for {code}")
        column = "content" if code == "_content" else "narrative"
        alternatives = []
        for v in split_or(value):
            try:
                tree = parse_text_query(unescape(v))
            except ValueError as exc:
                raise SearchError(400, f'Invalid {code} value "{v}": {exc}') from exc
            if tree is not None:
                try:
                    alternatives.append(fts_query(tree))
                except ValueError as exc:
                    raise SearchError(400, f'Invalid {code} value "{v}": {exc}') from exc
        if not alternatives:
            raise SearchError(400, f'Invalid {code} value "{value}"')
        match = f"{column} : ({' OR '.join(alternatives)})"
        sql = "SELECT rowid FROM fts WHERE fts MATCH ?"
        return Crit(f"{{a}}.pid IN ({sql})", [match], sql, [match], RANK["token"])

    def near_crit(self, p, modifier, value):
        """near=latitude|longitude|distance|units (units km, m, mi or [mi_i]; default distance from settings)."""
        if modifier or p["code"] != "near":
            raise SearchError(400, f"Search parameter {p['code']} is not supported")
        clauses, args = [], []
        for v in split_or(value):
            parts = split_escaped(v, "|")
            try:
                lat, lon = float(parts[0]), float(parts[1])
                distance = float(parts[2]) if len(parts) > 2 and parts[2] else self.settings.get("near_default_km", 1.0)
            except (ValueError, IndexError) as exc:
                raise SearchError(400, f'Invalid near value "{v}"') from exc
            unit = parts[3] if len(parts) > 3 and parts[3] else "km"
            factor = {"km": 1.0, "m": 0.001, "mi": 1.609344, "[mi_i]": 1.609344}.get(unit)
            if factor is None:
                raise SearchError(400, f'Unsupported near distance unit "{unit}"')
            clauses.append("distance_km(t.lat, t.lon, ?, ?) <= ?")
            args += [lat, lon, distance * factor]
        return self._exists("sp_position", p, " OR ".join(clauses), args, RANK["quantity"])

    def token_crit(self, p, modifier, value):
        if modifier == "of-type":
            clauses, args = [], []
            for v in split_or(value):
                parts = split_escaped(v, "|")
                if len(parts) != 3 or not parts[1] or not parts[2]:
                    raise SearchError(400, f'Invalid :of-type value "{v}": expected system|code|value')
                system, code, ident = parts
                clauses.append(("(t.tsystem = ?" if system else "(t.tsystem IS NULL") + " AND t.tcode = ? AND t.value = ?)")
                args += ([self.sid(system)] if system else []) + [code, ident]
            return self._exists("sp_ident_type", p, " OR ".join(clauses), args, RANK["token"])
        if modifier not in (None, "not", "text", "in", "not-in"):
            raise SearchError(400, f"Unsupported modifier :{modifier} for token parameter {p['code']}")
        values = split_or(value)
        if modifier in ("in", "not-in"):
            urls = [unescape(v).split("|")[0] for v in values]
            for url in urls:
                row = self.db.execute("SELECT complete FROM valuesets WHERE url = ?", (url,)).fetchone()
                if not row or not row[0]:
                    raise SearchError(400, f'ValueSet "{url}" is not known to this server or cannot be expanded '
                                           f"(filters or external code systems)")
            marks = ",".join("?" * len(urls))
            where = (f"EXISTS (SELECT 1 FROM valueset_codes v WHERE v.url IN ({marks}) AND v.code = t.code "
                     f"AND v.system IS t.system)")
            return self._exists("sp_token", p, where, urls, RANK["token"], negate=modifier == "not-in")
        if modifier == "text":
            norms = [normalize(unescape(v)) for v in values]
            where = " OR ".join("substr(t.norm, 1, ?) = ?" for _ in norms)
            args = [x for n in norms for x in (len(n), n)]
            return self._exists("sp_token_text", p, where, args, RANK["token"])
        where, args = self._token_where(values, "t.system", "t.code")
        return self._exists("sp_token", p, where, args, RANK["token"], negate=modifier == "not")

    def _token_where(self, values, system_col, code_col):
        clauses, args = [], []
        for v in values:
            parts = split_escaped(v, "|")
            if len(parts) == 1:
                clauses.append(f"{code_col} = ?")
                args.append(parts[0])
            elif len(parts) == 2:
                system, code = parts
                if system == "":
                    clauses.append(f"({system_col} IS NULL AND {code_col} = ?)")
                    args.append(code)
                elif code == "":
                    clauses.append(f"{system_col} = ?")
                    args.append(self.sid(system))
                else:
                    clauses.append(f"({system_col} = ? AND {code_col} = ?)")
                    args += [self.sid(system), code]
            else:
                raise SearchError(400, f'Invalid token value "{v}"')
        return " OR ".join(clauses), args

    def hierarchy_crit(self, p, modifier, value):
        """reference :below / :above: the target, or anything below / above it along the target type's
        own circular reference (e.g. Location.partof)."""
        clauses, args = [], []
        for v in split_or(value):
            v = unescape(v).split("/_history/")[0]
            local = next((b for b in self.local_bases if v.startswith(b + "/")), None)
            if local:
                v = v[len(local) + 1:]
            segs = v.split("/")
            rtype, rid = (segs[-2], segs[-1]) if len(segs) >= 2 else (p["targets"][0] if len(p["targets"]) == 1 else None, segs[0])
            if rtype is None or ":" in rtype:
                raise SearchError(400, f'Hierarchical search needs a local Type/id value, not "{v}"')
            circular = [q["id"] for (t, c), q in self.params.items()
                        if t == rtype and q["ptype"] == "reference" and rtype in q["targets"] and c in self.known.get(rtype, ())]
            if not circular:
                raise SearchError(400, f":{modifier} needs a hierarchy, but {rtype} has no reference parameter to {rtype}",
                                  "not-supported")
            tid, ids = self.sid(rtype), ",".join(map(str, circular))
            if modifier == "below":
                step = (f"SELECT hr.id FROM sp_ref hs JOIN resources hr ON hr.pid = hs.pid JOIN h ON hs.target_id = h.id "
                        f"WHERE hs.param IN ({ids}) AND hs.target_type = {tid} AND hr.type_id = {tid} AND hr.container IS NULL")
            else:
                step = (f"SELECT hs.target_id FROM sp_ref hs JOIN resources hr ON hr.pid = hs.pid JOIN h ON hr.id = h.id "
                        f"WHERE hs.param IN ({ids}) AND hs.target_type = {tid} AND hr.type_id = {tid} AND hr.container IS NULL")
            clauses.append(f"(t.target_type = {tid} AND t.target_id IN (WITH RECURSIVE h(id) AS (SELECT ? UNION {step}) SELECT id FROM h))")
            args.append(rid)
        return self._exists("sp_ref", p, " OR ".join(clauses), args, RANK["reference"])

    def list_crit(self, rtype, modifier, value):
        """_list=id: the resources a List's entries reference (functional lists are not defined)."""
        if modifier:
            raise SearchError(400, f"Unsupported modifier :{modifier} for _list")
        item = self.params.get(("List", "item"))
        lists = []
        for v in split_or(value):
            v = unescape(v)
            if v.startswith("$"):
                raise SearchError(400, f"Functional list {v} is not defined on this server", "not-supported")
            row = self.db.execute("SELECT pid FROM resources WHERE type_id = ? AND id = ? AND container IS NULL",
                                  (self.sid("List"), v)).fetchone() if item else None
            if row:
                lists.append(str(row[0]))
        if not lists:
            return Crit("0")                  # R4: a logical condition (unknown list) gives an empty set
        t, marks = self.alias(), ",".join(lists)
        cond = (f"EXISTS (SELECT 1 FROM sp_ref {t} WHERE {t}.pid IN ({marks}) AND {t}.param = {item['id']} "
                f"AND {t}.target_type = {{a}}.type_id AND {t}.target_id = {{a}}.id)")
        driver = (f"SELECT lr.pid FROM sp_ref {t} JOIN resources lr ON lr.type_id = {t}.target_type AND lr.id = {t}.target_id "
                  f"WHERE {t}.pid IN ({marks}) AND {t}.param = {item['id']}")
        return Crit(cond, [], driver, [], RANK["_id"])

    def reference_crit(self, p, modifier, value):
        if modifier in ("below", "above"):
            return self.hierarchy_crit(p, modifier, value)
        if modifier == "identifier":
            where, args = self._token_where(split_or(value), "t.system", "t.value")
            return self._exists("sp_ref_ident", p, where, args, RANK["reference"])
        target_mod = modifier if modifier and modifier[:1].isupper() else None
        if modifier and not target_mod:
            raise SearchError(400, f"Unsupported modifier :{modifier} for reference parameter {p['code']}")
        clauses, args, url_clauses, url_args = [], [], [], []
        for v in split_or(value):
            v = unescape(v)
            local = next((b for b in self.local_bases if v.startswith(b + "/")), None)
            if ":" in v.split("/")[0]:
                # Absolute or canonical reference: match the recorded URL (canonical |version optional);
                # on this server's own base it is also the relative reference.
                url, _, version = v.split("/_history/")[0].partition("|")
                segs = url.rstrip("/").split("/")
                url_type = segs[-2] if len(segs) >= 2 and segs[-2] in self.fhir_types else None
                if target_mod and url_type != target_mod:
                    # :[type] restricts the target type; a URL naming another type (or none) cannot match.
                    continue
                url_clauses.append("(t.url = ?" + (" AND t.version = ?)" if version else ")"))
                url_args += [url] + ([version] if version else [])
                if local is None:
                    continue
                v = v[len(local) + 1:]
            v = v.split("/_history/")[0]
            segs = v.split("/")
            rtype, rid = (segs[-2], segs[-1]) if len(segs) >= 2 else (target_mod, segs[0])
            if target_mod and rtype != target_mod:
                clauses.append("0")
                continue
            if rtype is None and len(p["targets"]) == 1:
                rtype = p["targets"][0]
            if rtype is None:
                kinds = [self.sid(t) for t in p["targets"] if self.sid(t) > 0]
                found = self.db.execute(
                    f"SELECT COUNT(DISTINCT type_id) FROM resources WHERE id = ? AND container IS NULL "
                    f"AND type_id IN ({','.join(map(str, kinds)) or '0'})", (rid,)).fetchone()[0]
                if found > 1:
                    raise SearchError(400, f'"{rid}" names resources of more than one type that {p["code"]} can '
                                           f"reference; use Type/{rid}")
                clauses.append("t.target_id = ?")
                args.append(rid)
            else:
                clauses.append("(t.target_id = ? AND t.target_type = ?)")
                args += [rid, self.sid(rtype)]
        crits = []
        if clauses:
            crits.append(self._exists("sp_ref", p, " OR ".join(clauses), args, RANK["reference"]))
        if url_clauses:
            crits.append(self._exists("sp_ref_url", p, " OR ".join(url_clauses), url_args, RANK["reference"]))
        if not crits:
            return Crit("0")            # every value contradicts the :[type] restriction
        return crits[0] if len(crits) == 1 else _any(crits)

    def date_crit(self, p, modifier, value):
        if modifier:
            raise SearchError(400, f"Unsupported modifier :{modifier} for date parameter {p['code']}")
        clauses, args = [], []
        for v in split_or(value):
            prefix, low, high = self._date_bounds(v)
            sql, a = self._date_sql(prefix, "t.low", "t.high", low, high)
            clauses.append(sql)
            args += a
        return self._exists("sp_date", p, " OR ".join(clauses), args, RANK["date"])

    def string_crit(self, p, modifier, value):
        if p.get("usage") == "phonetic":
            if modifier:
                raise SearchError(400, f"Unsupported modifier :{modifier} for phonetic parameter {p['code']}")
            # every part of a value (letters by Soundex, digits exactly) must match; values are OR
            alternatives = [phonetic_codes(unescape(v)) for v in split_or(value)]
            if not all(alternatives):
                raise SearchError(400, f'Phonetic search value "{value}" has no letters or digits')
            conds, drivers, args = [], [], []
            for codes in alternatives:
                parts = []
                for code in codes:
                    t = self.alias()
                    parts.append(f"EXISTS (SELECT 1 FROM sp_string {t} WHERE {t}.pid = {{a}}.pid AND "
                                 f"{t}.param = {p['id']} AND {t}.norm = ?)")
                    args.append(code)
                conds.append("(" + " AND ".join(parts) + ")")
                drivers.append(" INTERSECT ".join(f"SELECT pid FROM sp_string WHERE param = {p['id']} AND norm = ?"
                                                  for _ in codes))
            driver_args = [c for codes in alternatives for c in codes]
            return Crit("(" + " OR ".join(conds) + ")", args,
                        " UNION ".join(f"SELECT * FROM ({d})" for d in drivers), driver_args, RANK["string"])
        if modifier not in (None, "exact", "contains"):
            raise SearchError(400, f"Unsupported modifier :{modifier} for string parameter {p['code']}")
        clauses, args = [], []
        for v in split_or(value):
            v = unescape(v)
            if modifier == "exact":
                clauses.append("t.exact = ?")
                args.append(v)
            elif modifier == "contains":
                clauses.append("instr(t.norm, ?) > 0")
                args.append(normalize(v))
            else:
                n = normalize(v)
                clauses.append("substr(t.norm, 1, ?) = ?")
                args += [len(n), n]
        return self._exists("sp_string", p, " OR ".join(clauses), args, RANK["string"])

    def quantity_crit(self, p, modifier, value):
        if modifier:
            raise SearchError(400, f"Unsupported modifier :{modifier} for quantity parameter {p['code']}")
        clauses, args = [], []
        for v in split_or(value):
            sql, a = self._quantity_where(v, "t.low", "t.high", "t.system", "t.code", "t.unit")
            clauses.append(sql)
            args += a
        return self._exists("sp_quantity", p, " OR ".join(clauses), args, RANK["quantity"])

    def _quantity_where(self, v, low_col, high_col, system_col, code_col, unit_col):
        prefix, text = split_prefix(v)
        parts = split_escaped(text, "|")
        sql, args = self._number_sql(prefix, parts[0], low_col, high_col)
        if len(parts) >= 3:
            system, code = parts[1], parts[2]
            if system and code:
                sql += f" AND {system_col} = ? AND {code_col} = ?"
                args += [self.sid(system), code]
            elif code:
                # number||code: any system, matching the code or the recorded unit
                sql += f" AND ({code_col} = ? OR {unit_col} = ?)"
                args += [code, code]
            elif system:
                sql += f" AND {system_col} = ?"
                args.append(self.sid(system))
        elif len(parts) == 2:
            raise SearchError(400, f'Invalid quantity value "{v}"')
        return f"({sql})", args

    def _number_sql(self, prefix, text, low_col, high_col=None):
        """Prefixes on a target interval [low_col, high_col] (a plain value has low = high):
        eq it lies within the search value's precision range [low, high); ne it does not;
        gt/lt it extends above/below the value; ge/le likewise including the value; sa/eb it
        lies wholly above/below; ap it overlaps the value +-10%."""
        high_col = high_col or low_col
        try:
            number, low, high = number_bounds(text)
        except ValueError as exc:
            raise SearchError(400, f'Invalid number: "{text}"') from exc
        inside = f"({low_col} >= ? AND {high_col} < ?)"
        if prefix == "eq":
            return inside, [low, high]
        if prefix == "ne":
            return f"NOT {inside}", [low, high]
        if prefix == "ap":
            a, b = sorted((number * 0.9, number * 1.1))
            return f"({high_col} >= ? AND {low_col} <= ?)", [a, b]
        col, op = {"gt": (high_col, ">"), "ge": (high_col, ">="), "lt": (low_col, "<"), "le": (low_col, "<="),
                   "sa": (low_col, ">"), "eb": (high_col, "<")}[prefix]
        return f"{col} {op} ?", [number]

    def number_crit(self, p, modifier, value):
        if modifier:
            raise SearchError(400, f"Unsupported modifier :{modifier} for number parameter {p['code']}")
        clauses, args = [], []
        for v in split_or(value):
            prefix, text = split_prefix(v)
            sql, a = self._number_sql(prefix, text, "t.value")
            clauses.append(f"({sql})")
            args += a
        return self._exists("sp_number", p, " OR ".join(clauses), args, RANK["number"])

    def uri_crit(self, p, modifier, value):
        if modifier not in (None, "below", "above"):
            raise SearchError(400, f"Unsupported modifier :{modifier} for uri parameter {p['code']}")
        clauses, args, versioned = [], [], []
        version_param = self.params.get((p["type"], "version"))
        for v in split_or(value):
            v = unescape(v)
            if modifier and not re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", v):
                raise SearchError(400, f":{modifier} applies only to URLs, not to URNs such as OIDs (\"{v}\")")
            if not modifier and "|" in v and version_param and "version" in self.known.get(p["type"], ()):
                versioned.append(v.split("|", 1))           # canonical url|version: url and version
                continue
            if modifier == "below":
                clauses.append("substr(t.uri, 1, ?) = ?")
                args += [len(v), v]
            elif modifier == "above":
                clauses.append("substr(?, 1, length(t.uri)) = t.uri")
                args.append(v)
            else:
                clauses.append("t.uri = ?")
                args.append(v)
        crits = [self._exists("sp_uri", p, " OR ".join(clauses), args, RANK["uri"])] if clauses else []
        for url, version in versioned:
            a = self._exists("sp_uri", p, "t.uri = ?", [url], RANK["uri"])
            b = self.token_crit(version_param, None, version.replace(",", "\\,"))
            # the driver must select exactly the matches: _where does not re-check a driving criterion
            crits.append(Crit(f"({a.cond} AND {b.cond})", a.args + b.args, f"{a.driver} INTERSECT {b.driver}",
                              a.driver_args + b.driver_args, RANK["uri"]))
        return crits[0] if len(crits) == 1 else _any(crits)

    def composite_crit(self, p, modifier, value):
        if modifier or len(p["components"]) != 2:
            raise SearchError(400, f"Unsupported composite search on {p['code']}")
        kind = p["components"][1][0]
        clauses, args = [], []
        for v in split_or(value):
            parts = split_or(v, "$")                    # escapes (\| \$ \,) survive for each component
            if len(parts) != 2:
                raise SearchError(400, f'Invalid composite value "{v}"')
            first, second = parts
            sql1, a1 = self._token_where([first], "t.system", "t.code")
            if kind == "token":
                sql2, a2 = self._token_where([second], "t.c2_system", "t.c2_code")
            elif kind == "quantity":
                sql2, a2 = self._quantity_where(second, "t.c2_low", "t.c2_high", "t.c2_system", "t.c2_code", "t.c2_text")
            elif kind == "date":
                prefix, low, high = self._date_bounds(second)
                sql2, a2 = self._date_sql(prefix, "t.c2_low", "t.c2_high", low, high)
            else:
                n = normalize(unescape(second))
                sql2, a2 = "substr(t.c2_text, 1, ?) = ?", [len(n), n]
            clauses.append(f"(({sql1}) AND ({sql2}))")
            args += a1 + a2
        return self._exists("sp_composite", p, " OR ".join(clauses), args, RANK["composite"])

    def chain(self, rtype, code, target_mod, rest, value):
        p = self.param(rtype, code)
        if p["ptype"] != "reference":
            raise SearchError(400, f"Invalid parameter chain: {code}.{rest}")
        targets = [target_mod] if target_mod else p["targets"]
        branches = []          # (target type id, SQL of matching target ids, args)
        for target in targets:
            if target not in self.loaded and target not in self.known:
                continue
            try:
                nested = self.criterion(target, rest, value)
            except SearchError:
                if len(targets) == 1:
                    raise
                continue
            c, tid = self.alias("c"), self.sid(target)
            if nested.driver:
                ids = f"SELECT {c}.id FROM resources {c} WHERE {c}.type_id = {tid} AND {c}.pid IN ({nested.driver})"
                branches.append((tid, ids, nested.driver_args))
            else:
                sql, a = nested.on(c)
                branches.append((tid, f"SELECT {c}.id FROM resources {c} WHERE {c}.type_id = {tid} AND {sql}", a))
        if not branches:
            raise SearchError(400, f"Invalid parameter chain: {code}.{rest}", "not-supported")
        # One indexed (param, target_id, target_type) probe per target type.
        t = self.alias()
        match = " OR ".join(f"({t}.target_type = {tid} AND {t}.target_id IN ({ids}))" for tid, ids, _ in branches)
        args = [x for _, _, a in branches for x in a]
        cond = f"EXISTS (SELECT 1 FROM sp_ref {t} WHERE {t}.pid = {{a}}.pid AND {t}.param = {p['id']} AND ({match}))"
        parts = []
        for tid, ids, _ in branches:
            u = self.alias()
            parts.append(f"SELECT {u}.pid FROM sp_ref {u} WHERE {u}.param = {p['id']} AND {u}.target_id IN ({ids}) "
                         f"AND {u}.target_type = {tid}")
        return Crit(cond, args, " UNION ALL ".join(parts), args, RANK["chain"])

    def has(self, rtype, name, value):
        parts = name.split(":", 3)
        if len(parts) != 4:
            raise SearchError(400, f"Invalid _has parameter: {name}")
        _, source, ref_code, rest = parts
        if source not in self.loaded and source not in self.known:
            raise SearchError(400, f"Invalid resource type in _has: {source}")
        ref = self.param(source, ref_code)
        if ref["ptype"] != "reference":
            raise SearchError(400, f"Invalid _has reference parameter: {ref_code}")
        nested = self.criterion(source, rest, value)
        h, x = self.alias("h"), self.alias("x")
        if nested.driver:
            inner, args = f"{h}.pid IN ({nested.driver})", list(nested.driver_args)
        else:
            sql, args = nested.on(x)
            inner = f"EXISTS (SELECT 1 FROM resources {x} WHERE {x}.pid = {h}.pid AND {sql})"
        type_id = self.sid(rtype)
        cond = (f"EXISTS (SELECT 1 FROM sp_ref {h} WHERE {h}.param = {ref['id']} AND {h}.target_type = {type_id} "
                f"AND {h}.target_id = {{a}}.id AND {inner})")
        driver = (f"SELECT y.pid FROM resources y WHERE y.type_id = {type_id} AND y.id IN "
                  f"(SELECT {h}.target_id FROM sp_ref {h} WHERE {h}.param = {ref['id']} AND {h}.target_type = {type_id} AND {inner})")
        return Crit(cond, args, driver, args, RANK["has"])


def _date_sql(prefix, low_col, high_col, low, high, approx):
    """FHIR R4 date prefixes on the target range [low_col, high_col] against search range [low, high]:
    eq contained; ne not contained; gt/lt the target extends above/below the search range;
    ge/le that or contained; sa/eb wholly after/before; ap overlaps the widened range."""
    inside = f"({low_col} >= ? AND {high_col} <= ?)"
    if prefix == "eq":
        return inside, [low, high]
    if prefix == "ne":
        return f"NOT {inside}", [low, high]
    if prefix == "gt":
        return f"{high_col} > ?", [high]
    if prefix == "lt":
        return f"{low_col} < ?", [low]
    if prefix == "ge":
        return f"({high_col} > ? OR {inside})", [high, low, high]
    if prefix == "le":
        return f"({low_col} < ? OR {inside})", [low, low, high]
    if prefix == "sa":
        return f"{low_col} > ?", [high]
    if prefix == "eb":
        return f"{high_col} < ?", [low]
    return f"({high_col} >= ? AND {low_col} <= ?)", list(approx)


def date_eval(prefix, t_low, t_high, low, high, approx):
    inside = t_low >= low and t_high <= high
    return {"eq": inside, "ne": not inside, "gt": t_high > high, "lt": t_low < low,
            "ge": t_high > high or inside, "le": t_low < low or inside, "sa": t_low > high,
            "eb": t_high < low}.get(prefix, t_high >= approx[0] and t_low <= approx[1])
