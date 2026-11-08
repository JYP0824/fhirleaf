"""Writes through a writable connection: create, update and delete, with versions and index upkeep.

A written resource is stored and indexed exactly as `load` would store and index it, with
meta.versionId and meta.lastUpdated set by this store. The superseded version and deletions go
to the `versions` table, so read, vread and instance history answer every version.
"""
from __future__ import annotations

import json
import re
import uuid
import zlib
from datetime import datetime, timezone

from .extract import TABLES, Extractor, contained_resources
from .search import SearchError
from .store import INSERT, INTERNED, Definitions, _flag, _narrative, _text_leaves, derived_meta, preassigned_params
from .values import date_range, resolve_timezone

ID = re.compile(r"[A-Za-z0-9\-.]{1,64}")


class Writer:
    def __init__(self, engine, definitions_path, packages=(), clock=None):
        self.engine, self.db = engine, engine.db
        meta = engine.meta
        self.definitions = d = Definitions(definitions_path, packages)
        if d.sha256 != meta["definitions"]["sha256"]:
            raise ValueError("The definitions differ from the ones this store was built with "
                             f"({meta['definitions']['path']})")
        if sorted(p["sha256"] for p in d.packages) != sorted(p["sha256"] for p in meta.get("packages", [])):
            raise ValueError("The packages differ from the ones this store was built with")
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.config = dict(meta["settings"])
        self.tz = resolve_timezone(self.config["timezone"])
        self.fulltext = bool(self.config.get("fulltext"))
        self.preassigned = preassigned_params(d)
        self.counts = dict(meta["resource_counts"])
        self.extractor = Extractor(d.model, [], lambda value: value, self.tz, self.config["local_bases"])
        for rtype in sorted(self.counts):
            self.extractor.add(self.preassigned[rtype])
        self._keep_withdrawn(meta.get("unsupported_expressions", []))
        self.changed = False
        if not self.config.get("writable"):
            self._open_for_writing()

    def _keep_withdrawn(self, unsupported):
        """Parameters withdrawn at load time (or by an earlier write) stay withdrawn."""
        gone = {(t, c) for t, c, _ in unsupported}
        for rtype in list(self.extractor.by_type):
            self.extractor.by_type[rtype] = [e for e in self.extractor.by_type[rtype] if (rtype, e[0]["code"]) not in gone]
        self.extractor.unsupported = [list(u) for u in unsupported]

    def _open_for_writing(self):
        """First writable open: every resource type of the definitions can be written and searched,
        and date 'ap' becomes relative to the current time."""
        db = self.db
        db.execute("BEGIN IMMEDIATE")
        try:
            for rtype in sorted(self.preassigned):
                if rtype in self.counts:
                    continue
                for p in self.preassigned[rtype]:
                    db.execute("INSERT INTO params VALUES (?,?,?,?,?,?,?,?,?,?,?)", (
                        p["id"], p["type"], p["code"], p["ptype"], p["url"], p.get("expression"),
                        json.dumps(p["targets"]), json.dumps(p["components"]), p["usage"], _flag(p["multiple_or"]),
                        _flag(p["multiple_and"])))
                self.extractor.add(self.preassigned[rtype])
                self.counts[rtype] = 0
            self.config["writable"] = True
            self._save_meta(full=True)
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise

    def _save_meta(self, full=False):
        rows = {"resource_counts": self.counts}
        if full:
            params = [p for t in sorted(self.counts) for p in self.preassigned[t]]
            rows = {**derived_meta(self.definitions, params, self.extractor, self.counts, self.config),
                    "settings": self.config}
        self.db.executemany("INSERT OR REPLACE INTO meta VALUES (?,?)",
                            ((k, json.dumps(v, ensure_ascii=False)) for k, v in rows.items()))

    # ---- transaction frame (called by Engine) --------------------------------------------
    def begin(self):
        db = self.db
        self.strings = {}
        self.next_string = db.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM strings").fetchone()[0]
        self.next_pid = db.execute("SELECT COALESCE(MAX(pid), 0) + 1 FROM resources").fetchone()[0]
        self.counts = json.loads(db.execute("SELECT value FROM meta WHERE key = 'resource_counts'").fetchone()[0])
        self.withdrawn_now = False

    def finish(self):
        self._save_meta(full=self.withdrawn_now)
        self.changed = self.withdrawn_now

    # ---- helpers ---------------------------------------------------------------------------
    def now(self):
        return self.clock().astimezone(timezone.utc).isoformat(timespec="milliseconds")

    def intern(self, value):
        sid = self.strings.get(value)
        if sid is None:
            row = self.db.execute("SELECT id FROM strings WHERE value = ?", (value,)).fetchone()
            if row:
                sid = row[0]
            else:
                sid, self.next_string = self.next_string, self.next_string + 1
                self.db.execute("INSERT INTO strings VALUES (?,?)", (sid, value))
            self.strings[value] = sid
        return sid

    def type_id(self, rtype):
        row = self.db.execute("SELECT id FROM strings WHERE value = ?", (rtype,)).fetchone()
        return row[0] if row else None

    def current(self, rtype, rid):
        """(pid, resource, method, status) of the current version, or None."""
        tid = self.type_id(rtype)
        row = tid and self.db.execute("SELECT pid, json FROM resources WHERE type_id = ? AND id = ? AND container IS NULL",
                                      (tid, rid)).fetchone()
        if not row:
            return None
        written = self.db.execute("SELECT method, status FROM written WHERE pid = ?", (row[0],)).fetchone()
        method, status = written or ("PUT", 201)          # loaded: stored like PUT with its id
        return row[0], json.loads(zlib.decompress(row[1])), method, status

    def last_version(self, rtype, rid):
        tid = self.type_id(rtype)
        row = tid and self.db.execute("SELECT MAX(version) FROM versions WHERE type_id = ? AND id = ?", (tid, rid)).fetchone()
        return (row[0] or 0) if row else 0

    def _check(self, rtype, resource):
        if not isinstance(resource, dict):
            raise SearchError(400, "The body is not a JSON object", "structure")
        if resource.get("resourceType") != rtype:
            raise SearchError(400, f'The body resourceType "{resource.get("resourceType")}" is not {rtype}', "invalid")
        for inner in resource.get("contained") or []:
            if not (isinstance(inner, dict) and inner.get("resourceType") in self.preassigned and inner.get("id")):
                raise SearchError(400, "A contained resource needs a known resourceType and an id", "invalid")

    # ---- interactions ------------------------------------------------------------------
    def create(self, rtype, resource):
        """POST [type]: a new resource with a server-assigned id (an id in the body is ignored)."""
        self._check(rtype, resource)
        rid = str(uuid.uuid4())
        body = {"resourceType": rtype, "id": rid, **{k: v for k, v in resource.items() if k not in ("resourceType", "id")}}
        return 201, self._store(rtype, rid, body, 1, "POST", 201, None)

    def update(self, rtype, rid, resource, if_match=None):
        """PUT [type]/[id]: a new version, or the resource created with this id."""
        self._check(rtype, resource)
        if not ID.fullmatch(rid):
            raise SearchError(400, f'"{rid}" is not a valid resource id', "invalid")
        if resource.get("id") != rid:
            raise SearchError(400, f'The body id "{resource.get("id")}" does not match the URL id "{rid}"', "invalid")
        current = self.current(rtype, rid)
        version = int(current[1]["meta"]["versionId"]) if current else self.last_version(rtype, rid)
        if if_match is not None and (current is None or str(version) not in if_match):
            raise SearchError(412, f"If-Match does not match the current version of {rtype}/{rid}", "conflict")
        if current and _content(current[1]) == _content(resource):
            return 200, current[1]                    # same content: no new version (as Blaze and HAPI)
        if current:
            self._supersede(rtype, rid, current)
        status = 200 if current else 201
        return status, self._store(rtype, rid, dict(resource), version + 1, "PUT", status, current and current[0])

    def delete(self, rtype, rid, if_match=None):
        """DELETE [type]/[id]: 204 without a body, also when there is nothing to delete. The
        deletion is a version (self.deleted = its versionId and lastUpdated, for ETag and Last-Modified)."""
        self.deleted = None
        current = self.current(rtype, rid)
        if current is None:
            if if_match is not None:
                raise SearchError(412, f"If-Match: {rtype}/{rid} has no current version", "conflict")
            return 204, None
        version = int(current[1]["meta"]["versionId"])
        if if_match is not None and str(version) not in if_match:
            raise SearchError(412, f"If-Match does not match the current version of {rtype}/{rid}", "conflict")
        self._supersede(rtype, rid, current)
        updated = self.now()
        self.db.execute("INSERT INTO versions VALUES (?,?,?,?,?,?,?)",
                        (self.intern(rtype), rid, version + 1, updated, "DELETE", 204, None))
        self._unindex(current[0])
        self.db.execute("DELETE FROM resources WHERE pid = ?", (current[0],))
        self.counts[rtype] = self.counts.get(rtype, 1) - 1
        self.deleted = {"versionId": str(version + 1), "lastUpdated": updated}
        return 204, None

    # ---- storage --------------------------------------------------------------------------
    def _supersede(self, rtype, rid, current):
        pid, resource, method, status = current
        self.db.execute("INSERT INTO versions VALUES (?,?,?,?,?,?,?)", (
            self.intern(rtype), rid, int(resource["meta"]["versionId"]), resource["meta"]["lastUpdated"], method, status,
            zlib.compress(json.dumps(resource, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), 6)))

    def _unindex(self, pid):
        db = self.db
        pids = [pid] + [r[0] for r in db.execute("SELECT pid FROM resources WHERE container = ?", (pid,))]
        marks = ",".join("?" * len(pids))
        for table in TABLES:
            db.execute(f"DELETE FROM sp_{table} WHERE pid IN ({marks})", pids)
        db.execute(f"DELETE FROM written WHERE pid IN ({marks})", pids)
        db.execute("DELETE FROM resources WHERE container = ?", (pid,))
        if self.fulltext:
            db.execute("DELETE FROM fts WHERE rowid = ?", (pid,))

    def _store(self, rtype, rid, resource, version, method, status, pid):
        """Store the resource as version `version` (in place of pid if given) and index it like load."""
        db, updated = self.db, self.now()
        resource["meta"] = {**(resource.get("meta") or {}), "versionId": str(version), "lastUpdated": updated}
        text = json.dumps(resource, ensure_ascii=False, separators=(",", ":"))
        low, high, _ = date_range(updated, self.tz)
        if pid is None:
            pid, self.next_pid = self.next_pid, self.next_pid + 1
            self.counts[rtype] = self.counts.get(rtype, 0) + 1
        else:
            self._unindex(pid)
        rows = {t: [] for t in TABLES}
        db.execute("INSERT OR REPLACE INTO resources VALUES (?,?,?,?,?)",
                   (pid, self.intern(rtype), rid, zlib.compress(text.encode("utf-8"), 6), None))
        db.execute("INSERT INTO written VALUES (?,?,?,?,?)", (pid, low, high, method, status))
        scope, contained = contained_resources(resource)
        self._extract(resource, pid, scope, rows)
        if self.fulltext:
            db.execute("INSERT INTO fts(rowid, content, narrative) VALUES (?,?,?)",
                       (pid, " ".join(_text_leaves(resource, [])), _narrative(resource)))
        for row_id, inner_resource, inner in contained:
            ctype = inner_resource["resourceType"]
            self.counts.setdefault(ctype, 0)
            cpid, self.next_pid = self.next_pid, self.next_pid + 1
            ctext = json.dumps(inner_resource, ensure_ascii=False, separators=(",", ":"))
            db.execute("INSERT INTO resources VALUES (?,?,?,?,?)",
                       (cpid, self.intern(ctype), row_id, zlib.compress(ctext.encode("utf-8"), 6), pid))
            db.execute("INSERT INTO written VALUES (?,?,?,?,?)", (cpid, low, high, method, status))
            self._extract(inner_resource, cpid, inner, rows)
        for table, found in rows.items():
            if found:
                cols = INTERNED.get(table, ())
                db.executemany(INSERT[table], (self._interned(r, cols) for r in found))
        return resource

    def _interned(self, row, cols):
        row = list(row)
        for c in cols:
            if row[c] is not None:
                row[c] = self.intern(row[c])
        return row

    def _extract(self, resource, pid, scope, rows):
        extractor = self.extractor
        before = len(extractor.unsupported)
        try:
            found = extractor.rows(resource, pid, scope)
        except (ValueError, TypeError, KeyError) as exc:
            raise SearchError(400, f"{resource['resourceType']}/{resource.get('id')}: a value cannot be indexed ({exc})",
                              "invalid") from None
        for table, items in found.items():
            rows[table].extend(items)
        for rtype, code, _ in extractor.unsupported[before:]:
            # The expression needs resolve(): withdraw the parameter everywhere, as load does.
            pid_param = next(p["id"] for p in self.preassigned[rtype] if p["code"] == code)
            for table in TABLES:
                self.db.execute(f"DELETE FROM sp_{table} WHERE param = ?", (pid_param,))
                rows[table] = [r for r in rows[table] if r[0] != pid_param]
            self.withdrawn_now = True


def _content(resource):
    """A resource without the meta.versionId and meta.lastUpdated a server sets."""
    meta = {k: v for k, v in (resource.get("meta") or {}).items() if k not in ("versionId", "lastUpdated")}
    rest = {k: v for k, v in resource.items() if k != "meta"}
    return json.dumps({**rest, **({"meta": meta} if meta else {})}, sort_keys=True)
