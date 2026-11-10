"""Build a store: FHIR resources -> one SQLite file (read-only unless opened writable).

The file holds the unchanged resources (plus the meta.versionId/lastUpdated a
server assigns when storing them), search index rows extracted with the official
SearchParameter expressions (core definitions plus any implementation-guide
packages), expanded ValueSets for :in/:not-in, optional full-text and position
indexes, the CapabilityStatement it answers with and the search settings.
Nothing outside the file is needed at query time.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import zlib
from collections import deque
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from ..model.definitions import Definitions, file_sha256
from ..model.values import resolve_timezone
from .capability import derived_meta
from .extract import Extractor, TABLES, contained_resources
from ..inputs import (conditional_key, conditional_references, input_files, read_file, read_lines,
                      resolve_conditional, unpack)
from .narrative import narrative, text_leaves
from .schema import DEFAULT_SETTINGS, FORMAT, INDEXES, INSERT, INTERNED, SCHEMA, flag, preassigned_params








def _drop_duplicates(db, counts, strings):
    """Resources loaded more than once from bulk NDJSON: keep the first copy of identical ones,
    stop on conflicting ones. Returns the number of copies removed."""
    names = {sid: value for value, sid in strings.items()}
    extra = []
    groups = db.execute("SELECT type_id, id, GROUP_CONCAT(pid) FROM resources INDEXED BY resources_type_id "
                        "GROUP BY type_id, id HAVING COUNT(*) > 1").fetchall()
    for type_id, rid, pids in groups:
        pids = sorted(int(p) for p in pids.split(","))
        bodies = [zlib.decompress(db.execute("SELECT json FROM resources WHERE pid = ?", (p,)).fetchone()[0])
                  for p in pids]
        if any(b != bodies[0] for b in bodies[1:]):
            raise ValueError(f"Conflicting duplicate resource {names.get(type_id)}/{rid}")
        extra += pids[1:]
        counts[names[type_id]] -= len(pids) - 1
    if extra:
        marks = ",".join(map(str, extra))
        extra_sql = f"SELECT pid FROM resources WHERE pid IN ({marks}) OR container IN ({marks})"
        tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE 'sp\\_%' ESCAPE '\\'")]
        for table in tables:
            db.execute(f"DELETE FROM {table} WHERE pid IN ({extra_sql})")
        if db.execute("SELECT 1 FROM sqlite_master WHERE name = 'fts'").fetchone():
            db.execute(f"DELETE FROM fts WHERE rowid IN ({marks})")
        db.execute(f"DELETE FROM resources WHERE pid IN ({extra_sql})")
    return len(extra)














def load(inputs, definitions_path, output, *, packages=(), settings=None, fulltext=False, hash_sources=True,
         batch=5000, workers=1):
    """inputs: files or directories of *.ndjson[.gz] / *.json[.gz] (Bundles or single resources).
    No inputs make an empty store, to fill through a writable connection.
    workers > 1 parses and extracts batches in that many processes. The store is the same for any
    number of workers: batches are written in input order by this process."""
    output = Path(output)
    if output.exists():
        raise ValueError(f"Output exists; use a fresh path: {output}")
    files = input_files([inputs] if isinstance(inputs, (str, Path)) else inputs) if inputs else []
    definitions = Definitions(definitions_path, packages)
    config = {**DEFAULT_SETTINGS, **(settings or {}), "fulltext": bool(fulltext)}
    config.setdefault("last_updated", datetime.now(timezone.utc).isoformat(timespec="milliseconds"))
    tz = resolve_timezone(config["timezone"])
    staging = output.with_name(output.name + ".partial")
    if staging.exists():
        staging.unlink()
    db = sqlite3.connect(staging)
    db.executescript("PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF; PRAGMA temp_store=FILE; "
                     "PRAGMA cache_size=-1048576;" + SCHEMA)
    if fulltext:
        db.execute("CREATE VIRTUAL TABLE fts USING fts5(content, narrative, tokenize='unicode61 remove_diacritics 2')")
    strings = {}

    def intern(value):
        sid = strings.get(value)
        if sid is None:
            sid = strings[value] = len(strings) + 1
        return sid

    # Parameter ids are fixed per definitions (every resource type, in order), so every worker
    # writes the same ids. Only the types that occur are registered in the store.
    preassigned = preassigned_params(definitions)
    params, withdrawn = [], set()
    registry = Extractor(definitions.model, [], intern, tz, config["local_bases"])

    def register(rtype):
        if rtype not in preassigned:
            raise ValueError(f"Resource type {rtype} is not defined in the loaded FHIR definitions")
        for p in preassigned[rtype]:
            params.append(p)
            db.execute("INSERT INTO params VALUES (?,?,?,?,?,?,?,?,?,?,?)", (
                p["id"], p["type"], p["code"], p["ptype"], p["url"], p.get("expression"), json.dumps(p["targets"]),
                json.dumps(p["components"]), p["usage"], flag(p["multiple_or"]), flag(p["multiple_and"])))
        registry.add(preassigned[rtype])

    # Conditional references (Type?identifier=system|value) in Bundle inputs resolve against
    # the identifiers of every Bundle/JSON input resource; bulk NDJSON references are literal.
    conditional = {}
    json_inputs = [f for f in files if not f.name.endswith((".ndjson", ".ndjson.gz"))]
    if json_inputs:
        wanted, keys = set(), {}
        for f in json_inputs:
            for r in read_file(f):
                conditional_references(r, wanted)
                for key in conditional_key(r):
                    keys.setdefault(key, set()).add(f"{r['resourceType']}/{r['id']}")
        # Targets that live in NDJSON inputs: look up only the identifiers still missing.
        missing = {ref for ref in wanted if ref not in keys}
        if missing:
            types = {ref.split("?", 1)[0] for ref in missing}
            for f in files:
                if f in json_inputs:
                    continue
                for r in read_file(f):
                    if r.get("resourceType") in types:
                        for key in conditional_key(r):
                            if key in missing:
                                keys.setdefault(key, set()).add(f"{r['resourceType']}/{r['id']}")
        conditional = {ref: next(iter(keys[ref])) for ref in wanted if len(keys.get(ref, ())) == 1}
    meta_patch = {"versionId": "1", "lastUpdated": config["last_updated"]}
    json_set = set(json_inputs)
    counts, per_file, unresolved = {}, {}, set()
    pid, duplicates, contained_count, reported = 0, 0, 0, 0
    started = time.time()

    def batches():
        """(file, items): raw NDJSON lines, or resources of Bundle/JSON inputs after duplicate checks."""
        seen = {}
        for path in files:
            items = []
            if path in json_set:
                for resource in read_file(path):
                    rtype = resource.get("resourceType")
                    if not rtype or not resource.get("id"):
                        raise ValueError(f"Resource without resourceType/id in {path.name}")
                    resolve_conditional(resource, conditional)
                    resource["meta"] = {**resource.get("meta", {}), **meta_patch}
                    text = json.dumps(resource, ensure_ascii=False, separators=(",", ":"))
                    key, crc = (rtype, resource["id"]), zlib.crc32(text.encode("utf-8"))
                    if key in seen:
                        if seen[key] != crc:
                            raise ValueError(f"Conflicting duplicate resource {rtype}/{resource['id']} in {path.name}")
                        nonlocal_duplicates[0] += 1
                        continue
                    seen[key] = crc
                    items.append(resource)
                    if len(items) >= batch:
                        yield path, items
                        items = []
            else:
                for line in read_lines(path):
                    items.append(line)
                    if len(items) >= batch:
                        yield path, items
                        items = []
            if items:
                yield path, items

    nonlocal_duplicates = [0]
    if workers > 1:
        pool = ProcessPoolExecutor(workers, initializer=_init_worker,
                                   initargs=(str(definitions_path), list(packages), config["timezone"],
                                             config["local_bases"], conditional, meta_patch, fulltext))
        results = _ordered(pool, batches(), workers * 3)
    else:
        pool = None
        _setup(definitions, tz, config["local_bases"], conditional, meta_patch, fulltext)
        results = ((path, _process(items, path.name)) for path, items in batches())
    try:
        for path, res in results:
            for rtype, n in res["counts"].items():
                if rtype not in counts:
                    register(rtype)
                    counts[rtype] = 0
                counts[rtype] += n
            for rtype, code, why in res["withdrawn"]:
                if (rtype, code) not in withdrawn:
                    withdrawn.add((rtype, code))
                    registry.by_type[rtype] = [e for e in registry.by_type[rtype] if e[0]["code"] != code]
                    registry.unsupported.append((rtype, code, why))
            base = pid
            db.executemany("INSERT INTO resources VALUES (?,?,?,?,?)",
                           ((base + lp, intern(t), rid, blob, None if c is None else base + c)
                            for lp, t, rid, blob, c in res["resources"]))
            for table, rows in res["rows"].items():
                if rows:
                    cols = INTERNED.get(table)
                    db.executemany(INSERT[table], (_remap(r, cols, base, intern) for r in rows) if cols
                                   else ((*r[:-1], r[-1] + base) for r in rows))
            if res["fts"]:
                db.executemany("INSERT INTO fts(rowid, content, narrative) VALUES (?,?,?)",
                               ((base + c, a, b) for c, a, b in res["fts"]))
            pid += res["n"]
            contained_count += res["contained"]
            unresolved.update(res["unresolved"])
            per_file[path] = per_file.get(path, 0) + res["top"]
            if pid // 200000 > reported:
                reported = pid // 200000
                print(f"loaded {pid} resources ({time.time() - started:.0f}s)", flush=True)
    finally:
        if pool is not None:
            pool.shutdown(cancel_futures=True)
    duplicates += nonlocal_duplicates[0]
    sources = [{"file": str(path.name), "resources": per_file.get(path, 0), "bytes": path.stat().st_size,
                "sha256": file_sha256(path) if hash_sources else None} for path in files]
    # Rows of a parameter withdrawn during extraction (it needs resolve()) are removed, so the
    # store does not depend on which worker met the parameter first.
    if withdrawn:
        ids = ",".join(str(p["id"]) for p in params if (p["type"], p["code"]) in withdrawn)
        for table in INSERT:
            db.execute(f"DELETE FROM sp_{table} WHERE param IN ({ids})")
    complete = 0
    for url in sorted(definitions.valuesets):
        ok, codes = definitions.expand(url)
        complete += ok
        db.execute("INSERT OR IGNORE INTO valuesets VALUES (?,?)", (url, int(ok)))
        db.executemany("INSERT INTO valueset_codes VALUES (?,?,?)",
                       ((url, intern(s) if s else None, c) for s, c in sorted(codes, key=lambda x: (x[0] or "", x[1]))))
    db.executemany("INSERT INTO strings VALUES (?,?)", ((sid, value) for value, sid in strings.items()))
    print(f"indexing {pid} resources", flush=True)
    first, _, rest = INDEXES.strip().partition("\n")
    db.execute(first)                           # resources(type_id, id); then (type, id) must be unique
    duplicates += _drop_duplicates(db, counts, strings)
    db.executescript(rest)
    db.execute("ANALYZE")
    meta = {"version": FORMAT, "fhir_version": definitions.fhir_version, "settings": config,
            **derived_meta(definitions, params, registry, counts, config), "sources": sources,
            "definitions": {"path": os.path.basename(definitions.path), "sha256": definitions.sha256},
            "packages": definitions.packages, "duplicates_skipped": duplicates, "contained_resources": contained_count,
            "unresolved_conditional_references": sorted(unresolved)[:100], "unresolved_conditional_count": len(unresolved),
            "valuesets": {"total": len(definitions.valuesets), "complete": complete},
            "fhir_resource_types": sorted(definitions.model.resource_types)}
    db.executemany("INSERT INTO meta VALUES (?,?)", ((k, json.dumps(v, ensure_ascii=False)) for k, v in meta.items()))
    db.commit()
    db.close()
    os.replace(staging, output)
    print(json.dumps({"output": str(output), "resources": sum(counts.values()), "counts": counts, "duplicates_skipped": duplicates,
                      "unresolved_conditional_references": len(unresolved), "seconds": round(time.time() - started),
                      "unsupported_expressions": registry.unsupported}), flush=True)
    return output


_WORKER = {}




def _setup(definitions, tz, local_bases, conditional, meta_patch, fulltext):
    _WORKER.update(params=preassigned_params(definitions), registered=set(), conditional=conditional,
                   meta_patch=meta_patch, fulltext=fulltext,
                   extractor=Extractor(definitions.model, [], lambda value: value, tz, local_bases))


def _init_worker(definitions_path, packages, timezone_name, local_bases, conditional, meta_patch, fulltext):
    _setup(Definitions(definitions_path, packages), resolve_timezone(timezone_name), local_bases, conditional,
           meta_patch, fulltext)


def _process(items, source):
    """One batch: resources and index rows with batch-local pids (1..n) and uninterned strings."""
    w = _WORKER
    extractor, conditional, meta_patch, fulltext = w["extractor"], w["conditional"], w["meta_patch"], w["fulltext"]
    resources, fts, rows = [], [], {t: [] for t in TABLES}
    counts, unresolved, withdrawn = {}, set(), []
    local = contained_count = top = 0

    def ensure(rtype):
        if rtype not in w["registered"]:
            if rtype not in w["params"]:
                raise ValueError(f"Resource type {rtype} is not defined in the loaded FHIR definitions")
            extractor.add(w["params"][rtype])
            w["registered"].add(rtype)

    def extract(resource, pid, scope):
        before = len(extractor.unsupported)
        for table, found in extractor.rows(resource, pid, scope).items():
            rows[table].extend(found)
        withdrawn.extend(extractor.unsupported[before:])

    for item in items:
        for resource in (unpack(json.loads(item)) if isinstance(item, str) else (item,)):
            rtype = resource.get("resourceType")
            if not rtype or not resource.get("id"):
                raise ValueError(f"Resource without resourceType/id in {source}")
            unresolved |= resolve_conditional(resource, conditional)
            resource["meta"] = {**resource.get("meta", {}), **meta_patch}
            text = json.dumps(resource, ensure_ascii=False, separators=(",", ":"))
            ensure(rtype)
            local += 1
            top += 1
            counts[rtype] = counts.get(rtype, 0) + 1
            container = local
            resources.append((local, rtype, resource["id"], zlib.compress(text.encode("utf-8"), 6), None))
            scope, contained = contained_resources(resource)
            extract(resource, local, scope)
            if fulltext:
                fts.append((container, " ".join(text_leaves(resource, [])), narrative(resource)))
            for row_id, inner_resource, inner in contained:
                ctype = inner_resource["resourceType"]
                ensure(ctype)
                counts.setdefault(ctype, 0)
                local += 1
                contained_count += 1
                ctext = json.dumps(inner_resource, ensure_ascii=False, separators=(",", ":"))
                resources.append((local, ctype, row_id, zlib.compress(ctext.encode("utf-8"), 6), container))
                extract(inner_resource, local, inner)
    return {"n": local, "top": top, "resources": resources, "rows": {t: list(r) for t, r in rows.items()},
            "fts": fts, "counts": counts, "contained": contained_count, "unresolved": sorted(unresolved),
            "withdrawn": withdrawn}


def _ordered(pool, batches, depth):
    """Results of batches in input order, with at most depth batches in flight."""
    pending = deque()
    for path, items in batches:
        pending.append((path, pool.submit(_process, items, path.name)))
        if len(pending) >= depth:
            path, future = pending.popleft()
            yield path, future.result()
    while pending:
        path, future = pending.popleft()
        yield path, future.result()


def _remap(row, cols, base, intern):
    row = list(row)
    for c in cols:
        if row[c] is not None:
            row[c] = intern(row[c])
    row[-1] += base
    return row
