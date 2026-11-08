"""Build a store: FHIR resources -> one SQLite file (read-only unless opened writable).

The file holds the unchanged resources (plus the meta.versionId/lastUpdated a
server assigns when storing them), search index rows extracted with the official
SearchParameter expressions (core definitions plus any implementation-guide
packages), expanded ValueSets for :in/:not-in, optional full-text and position
indexes, the CapabilityStatement it answers with and the search settings.
Nothing outside the file is needed at query time.
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import sqlite3
import time
import zipfile
import zlib
from collections import deque
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from .extract import contained_resources, TABLES, Extractor
from .fhirpath import Model
from .inputs import (conditional_key, conditional_references, input_files, package_resources, read_file, read_lines,
                     resolve_conditional, unpack)
from .values import resolve_timezone

VERSION = "fhirleaf-store-v4"
READABLE = ("fhirleaf-store-v3", VERSION)      # v3 has no version tables and opens read-only only
SCHEMA = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE strings(id INTEGER PRIMARY KEY, value TEXT NOT NULL UNIQUE);
-- usage: the definition's processing mode (normal | phonetic | nearby | distance | other);
-- multiple_or / multiple_and: the definition's multipleOr / multipleAnd (NULL = not stated, allowed)
CREATE TABLE params(id INTEGER PRIMARY KEY, type TEXT NOT NULL, code TEXT NOT NULL, ptype TEXT NOT NULL,
                    url TEXT, expression TEXT, targets TEXT NOT NULL, components TEXT NOT NULL,
                    usage TEXT, multiple_or INTEGER, multiple_and INTEGER, UNIQUE(type, code));
-- contained resources are rows too: id 'Container/cid#local', container = the container's pid
CREATE TABLE resources(pid INTEGER PRIMARY KEY, type_id INTEGER NOT NULL, id TEXT NOT NULL, json BLOB NOT NULL, container INTEGER);
-- token codes and identifier values compare case-insensitively (ASCII; R4: 'when in doubt' case-insensitive)
CREATE TABLE sp_token(param INTEGER NOT NULL, system INTEGER, code TEXT NOT NULL COLLATE NOCASE, pid INTEGER NOT NULL);
-- Identifier.type coding + value, for token :of-type
CREATE TABLE sp_ident_type(param INTEGER NOT NULL, tsystem INTEGER, tcode TEXT NOT NULL COLLATE NOCASE, value TEXT NOT NULL COLLATE NOCASE, pid INTEGER NOT NULL);
CREATE TABLE sp_token_text(param INTEGER NOT NULL, norm TEXT NOT NULL, pid INTEGER NOT NULL);
CREATE TABLE sp_ref(param INTEGER NOT NULL, target_type INTEGER NOT NULL, target_id TEXT NOT NULL, pid INTEGER NOT NULL);
CREATE TABLE sp_ref_url(param INTEGER NOT NULL, url TEXT NOT NULL, version TEXT, pid INTEGER NOT NULL);
CREATE TABLE sp_ref_ident(param INTEGER NOT NULL, system INTEGER, value TEXT NOT NULL COLLATE NOCASE, pid INTEGER NOT NULL);
-- date ranges in microseconds since 1970-01-01T00:00:00Z
CREATE TABLE sp_date(param INTEGER NOT NULL, low INTEGER NOT NULL, high INTEGER NOT NULL, pid INTEGER NOT NULL);
CREATE TABLE sp_string(param INTEGER NOT NULL, norm TEXT NOT NULL, exact TEXT NOT NULL, pid INTEGER NOT NULL);
-- quantity intervals: a plain value is low = high; comparators and Ranges are one- or two-sided
CREATE TABLE sp_quantity(param INTEGER NOT NULL, low REAL NOT NULL, high REAL NOT NULL, system INTEGER, code TEXT, unit TEXT,
                         pid INTEGER NOT NULL);
CREATE TABLE sp_number(param INTEGER NOT NULL, value REAL NOT NULL, pid INTEGER NOT NULL);
CREATE TABLE sp_uri(param INTEGER NOT NULL, uri TEXT NOT NULL, pid INTEGER NOT NULL);
CREATE TABLE sp_position(param INTEGER NOT NULL, lat REAL NOT NULL, lon REAL NOT NULL, pid INTEGER NOT NULL);
-- second component: token (c2_system, c2_code), quantity (interval c2_low/c2_high, c2_system, c2_code, unit in c2_text),
-- date (c2_low, c2_high) or string (normalized c2_text)
CREATE TABLE sp_composite(param INTEGER NOT NULL, system INTEGER, code TEXT NOT NULL COLLATE NOCASE, c2_system INTEGER, c2_code TEXT COLLATE NOCASE,
                          c2_value REAL, c2_low INTEGER, c2_high INTEGER, c2_text TEXT, pid INTEGER NOT NULL);
-- versions that are no longer current, and deletions (json NULL), written through a writable connection
CREATE TABLE versions(type_id INTEGER NOT NULL, id TEXT NOT NULL, version INTEGER NOT NULL, updated TEXT NOT NULL,
                      method TEXT NOT NULL, status INTEGER NOT NULL, json BLOB, PRIMARY KEY(type_id, id, version));
-- resources (and their contained rows) written after load: lastUpdated range in microseconds and the
-- method and status of that write. Loaded resources have no row: their lastUpdated is the load instant.
CREATE TABLE written(pid INTEGER PRIMARY KEY, low INTEGER NOT NULL, high INTEGER NOT NULL, method TEXT NOT NULL,
                     status INTEGER NOT NULL);
CREATE TABLE valuesets(url TEXT PRIMARY KEY, complete INTEGER NOT NULL);
CREATE TABLE valueset_codes(url TEXT NOT NULL, system INTEGER, code TEXT NOT NULL COLLATE NOCASE);
"""
INDEXES = """
CREATE INDEX resources_type_id ON resources(type_id, id);
CREATE INDEX resources_type ON resources(type_id);
CREATE INDEX sp_token_value ON sp_token(param, code, system);
CREATE INDEX sp_token_pid ON sp_token(pid, param);
CREATE INDEX sp_ident_type_value ON sp_ident_type(param, value, tcode);
CREATE INDEX sp_ident_type_pid ON sp_ident_type(pid, param);
CREATE INDEX sp_token_text_value ON sp_token_text(param, norm);
CREATE INDEX sp_token_text_pid ON sp_token_text(pid, param);
CREATE INDEX sp_ref_target ON sp_ref(param, target_id, target_type);
CREATE INDEX sp_ref_pid ON sp_ref(pid, param);
CREATE INDEX sp_ref_url_value ON sp_ref_url(param, url);
CREATE INDEX sp_ref_url_pid ON sp_ref_url(pid, param);
CREATE INDEX sp_ref_ident_value ON sp_ref_ident(param, value, system);
CREATE INDEX sp_ref_ident_pid ON sp_ref_ident(pid, param);
CREATE INDEX sp_date_low ON sp_date(param, low);
CREATE INDEX sp_date_high ON sp_date(param, high);
CREATE INDEX sp_date_pid ON sp_date(pid, param);
CREATE INDEX sp_string_norm ON sp_string(param, norm);
CREATE INDEX sp_string_exact ON sp_string(param, exact);
CREATE INDEX sp_string_pid ON sp_string(pid, param);
CREATE INDEX sp_quantity_low ON sp_quantity(param, low);
CREATE INDEX sp_quantity_high ON sp_quantity(param, high);
CREATE INDEX sp_quantity_pid ON sp_quantity(pid, param);
CREATE INDEX sp_number_value ON sp_number(param, value);
CREATE INDEX sp_number_pid ON sp_number(pid, param);
CREATE INDEX sp_uri_value ON sp_uri(param, uri);
CREATE INDEX sp_uri_pid ON sp_uri(pid, param);
CREATE INDEX sp_position_pid ON sp_position(pid, param);
CREATE INDEX sp_composite_value ON sp_composite(param, code, system);
CREATE INDEX sp_composite_pid ON sp_composite(pid, param);
CREATE INDEX valueset_codes_value ON valueset_codes(url, code, system);
CREATE INDEX written_low ON written(low);
"""
INSERT = {
    "token": "INSERT INTO sp_token VALUES (?,?,?,?)",
    "token_text": "INSERT INTO sp_token_text VALUES (?,?,?)",
    "ident_type": "INSERT INTO sp_ident_type VALUES (?,?,?,?,?)",
    "ref": "INSERT INTO sp_ref VALUES (?,?,?,?)",
    "ref_url": "INSERT INTO sp_ref_url VALUES (?,?,?,?)",
    "ref_ident": "INSERT INTO sp_ref_ident VALUES (?,?,?,?)",
    "date": "INSERT INTO sp_date VALUES (?,?,?,?)",
    "string": "INSERT INTO sp_string VALUES (?,?,?,?)",
    "quantity": "INSERT INTO sp_quantity VALUES (?,?,?,?,?,?,?)",
    "number": "INSERT INTO sp_number VALUES (?,?,?)",
    "uri": "INSERT INTO sp_uri VALUES (?,?,?)",
    "position": "INSERT INTO sp_position VALUES (?,?,?,?)",
    "composite": "INSERT INTO sp_composite VALUES (?,?,?,?,?,?,?,?,?,?)",
}
DEFAULT_SETTINGS = {
    # Server choices the specification leaves open; stored in the file and reported by metadata.
    "default_count": 100, "max_count": 1000, "timezone": "UTC", "near_default_km": 1.0,
    # Absolute references on these bases are references to this store (same server).
    "local_bases": ["http://localhost/fhir"],
}
NON_DOMAIN = ("Bundle", "Binary", "Parameters")


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


class Definitions:
    """HL7 FHIR definitions (definitions.json.zip, CC0) plus implementation-guide packages."""

    def __init__(self, path, packages=()):
        self.path, self.sha256 = str(path), file_sha256(path)
        with zipfile.ZipFile(path) as z:
            # Archives differ by version (R4B nests everything under definitions.json/).
            members = {n.rsplit("/", 1)[-1]: n for n in z.namelist() if not n.startswith("__MACOSX") and not n.endswith("/")}

            def entries(name):
                return [e["resource"] for e in json.loads(z.read(members[name])).get("entry", [])] if name in members else []

            info = z.read(members["version.info"]).decode("utf-8", "replace") if "version.info" in members else ""
            found = re.search(r"^version=(\S+)", info, re.M) or re.search(r"FhirVersion=([\d.]+)", info)
            resources = entries("profiles-resources.json")
            self.fhir_version = found.group(1) if found else next(
                (r["fhirVersion"] for r in resources if r.get("resourceType") == "StructureDefinition" and r.get("fhirVersion")), "unknown")
            types = entries("profiles-types.json")
            self.search_parameters = entries("search-parameters.json")
            terminology = [r for name in ("valuesets.json", "v2-tables.json", "v3-codesystems.json") for r in entries(name)]
        self.structures = [r for r in resources + types if r.get("resourceType") == "StructureDefinition"]
        self.compartments = {c["code"]: {r["code"]: r.get("param", []) for r in c.get("resource", [])}
                             for c in resources if c.get("resourceType") == "CompartmentDefinition"}
        self.packages, custom = [], []
        for package in packages:
            items = package_resources(package)
            self.packages.append({"path": os.path.basename(str(package)), "sha256": file_sha256(package),
                                  "resources": len(items)})
            custom += [r for r in items if r.get("resourceType") == "SearchParameter"]
            terminology += [r for r in items if r.get("resourceType") in ("ValueSet", "CodeSystem")]
        self.custom_parameters = custom
        self.valuesets = {r["url"]: r for r in terminology if r.get("resourceType") == "ValueSet" and r.get("url")}
        self.codesystems = {r["url"]: r for r in terminology if r.get("resourceType") == "CodeSystem" and r.get("url")}
        self.valueset_system = {}
        for url, vs in self.valuesets.items():
            includes = vs.get("compose", {}).get("include", [])
            systems = {i.get("system") for i in includes}
            if len(systems) == 1 and None not in systems and not any(i.get("filter") or i.get("valueSet") for i in includes):
                self.valueset_system[url] = systems.pop()
        self.model = Model(self.structures, self.binding_system)

    def binding_system(self, element):
        binding = element.get("binding") or {}
        if binding.get("strength") != "required":
            return None
        return self.valueset_system.get((binding.get("valueSet") or "").split("|")[0])

    def params_for(self, resource_types):
        """Search parameters per type; a package parameter replaces a core one with the same code."""
        everything = self.search_parameters + self.custom_parameters
        by_url = {p.get("url"): p for p in everything}
        out = []
        for rtype in sorted(resource_types):
            chosen = {}
            for p in everything:
                bases = p.get("base", [])
                if rtype in bases or "Resource" in bases or ("DomainResource" in bases and rtype not in NON_DOMAIN):
                    if p.get("code"):
                        chosen[p["code"]] = p
            for code, p in sorted(chosen.items()):
                comps = [(by_url[c["definition"]]["type"], c["expression"]) for c in p.get("component", [])
                         if c.get("definition") in by_url]
                out.append({"type": rtype, "code": code, "ptype": p["type"], "url": p.get("url"),
                            "expression": p.get("expression"), "targets": p.get("target", []), "components": comps,
                            "usage": p.get("xpathUsage") or p.get("processingMode") or "normal",
                            "multiple_or": p.get("multipleOr"), "multiple_and": p.get("multipleAnd"),
                            "declared_components": len(p.get("component", [])),
                            "comparators": p.get("comparator", []), "modifiers": p.get("modifier", [])})
        return out

    def expand(self, url, seen=None):
        """(complete, {(system, code)}) of a ValueSet from its expansion or compose; filters,
        external code systems and missing parts make it incomplete."""
        seen = (seen or set()) | {url}
        vs = self.valuesets.get(url.split("|")[0])
        if vs is None:
            return False, set()
        expansion = vs.get("expansion") or {}
        if expansion.get("contains"):
            codes, count = set(), 0
            stack = list(expansion["contains"])
            while stack:
                c = stack.pop()
                if c.get("code"):
                    codes.add((c.get("system"), c["code"]))
                    count += 1
                stack += c.get("contains", [])
            # Complete only if the expansion says it holds every code (total given and equal, no paging);
            # otherwise use the compose definition, or report the ValueSet as not expandable.
            if expansion.get("total") == count and not expansion.get("offset"):
                return True, codes
            if not vs.get("compose"):
                return False, codes

        def part(item):
            complete, codes = True, set()
            for ref in item.get("valueSet", []):
                if ref in seen:
                    return False, set()
                ok, sub = self.expand(ref, seen)
                complete, codes = complete and ok, codes | sub
            system = item.get("system")
            if system:
                if item.get("filter"):
                    return False, codes
                if item.get("concept"):
                    codes |= {(system, c["code"]) for c in item["concept"]}
                else:
                    cs = self.codesystems.get(system)
                    if cs is None or cs.get("content") not in (None, "complete"):
                        return False, codes
                    stack = list(cs.get("concept", []))
                    while stack:
                        c = stack.pop()
                        codes.add((system, c["code"]))
                        stack += c.get("concept", [])
            return complete, codes

        complete, codes = True, set()
        for item in vs.get("compose", {}).get("include", []):
            ok, sub = part(item)
            complete, codes = complete and ok, codes | sub
        for item in vs.get("compose", {}).get("exclude", []):
            ok, sub = part(item)
            complete, codes = complete and ok, codes - sub
        return complete, codes


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


def _flag(value):
    return None if value is None else int(bool(value))


READ_INTERACTIONS = [{"code": "read"}, {"code": "vread"}, {"code": "search-type"}]
WRITE_INTERACTIONS = READ_INTERACTIONS + [{"code": "create"}, {"code": "update"}, {"code": "delete"},
                                          {"code": "history-instance"}]


def capability(params, supported, settings, definitions, compartments):
    """CapabilityStatement listing exactly the interactions and search parameters this store answers."""
    writable = bool(settings.get("writable"))
    by_type = {}
    for p in params:
        if (p["type"], p["code"]) in supported:
            by_type.setdefault(p["type"], []).append(p)
    resources = []
    for rtype, items in sorted(by_type.items()):
        refs = sorted(p["code"] for p in items if p["ptype"] == "reference")
        rev = sorted(f"{q['type']}:{q['code']}" for q in params if q["ptype"] == "reference"
                     and (q["type"], q["code"]) in supported and rtype in q["targets"])
        entries = [{"name": p["code"], "definition": p["url"], "type": p["ptype"]} for p in sorted(items, key=lambda p: p["code"])]
        if settings.get("fulltext"):
            entries += [{"name": "_content", "type": "string"}, {"name": "_text", "type": "string"}]
        entries.append({"name": "_list", "type": "special"})
        resources.append({
            "type": rtype, "profile": f"http://hl7.org/fhir/StructureDefinition/{rtype}",
            "interaction": WRITE_INTERACTIONS if writable else READ_INTERACTIONS,
            **({"versioning": "versioned-update", "readHistory": True, "updateCreate": True} if writable else {}),
            "searchParam": entries,
            "searchInclude": ["*"] + ([f"{rtype}:*"] if refs else []) + [f"{rtype}:{r}" for r in refs],
            "searchRevInclude": (["*"] if rev else []) + sorted({f"{r.split(':')[0]}:*" for r in rev}) + rev,
        })
    note = (f"fhirleaf embedded FHIR RESTful Search. Date/time search values without an offset are read in zone "
            f"{settings['timezone']}; default page size {settings['default_count']}, maximum {settings['max_count']}; "
            f"results without _sort are in stable store order; near without a distance uses "
            f"{settings['near_default_km']} km; phonetic parameters match name parts, letters by American Soundex and digits exactly; "
            f"token codes and identifier values compare case-insensitively (ASCII); date 'ap' is 10% of the distance "
            + ("from the current time." if writable else f"from the load instant {settings['last_updated']}."))
    return {"resourceType": "CapabilityStatement", "status": "active", "kind": "instance",
            "fhirVersion": definitions.fhir_version, "date": settings["last_updated"],
            "format": ["application/fhir+json"], "software": {"name": "fhirleaf", "version": VERSION},
            "implementation": {"description": note},
            "rest": [{"mode": "server", "resource": resources, "interaction": [{"code": "search-system"}],
                      "compartment": [f"http://hl7.org/fhir/CompartmentDefinition/{c.lower()}" for c in sorted(compartments)]}]}


def derived_meta(definitions, params, registry, counts, config):
    """Meta entries that follow from the registered types (counts) and parameters: capability,
    compartments and element lists. A writable connection recomputes them when it registers types."""
    supported = {(p["type"], p["code"]) for items in registry.by_type.values() for p, _, _ in items}
    supported |= {(t, c) for t in counts for c in ("_id", "_lastUpdated")}
    compartments = {}
    for code, members in definitions.compartments.items():
        if code not in counts:
            continue
        compartments[code] = {t: [c for c in codes if (t, c) in supported or c == "{def}"]
                              for t, codes in members.items() if t in counts and codes}
    elements = definitions.model.elements
    return {"capability": capability(params, supported, config, definitions, compartments),
            "resource_counts": counts, "compartments": compartments,
            "unsupported_expressions": registry.unsupported,
            "elements": {t: sorted({path.split(".")[1].replace("[x]", "") for path in elements
                                    if path.startswith(t + ".") and path.count(".") == 1}) for t in counts},
            "choice_elements": {t: sorted({path.split(".")[1][:-3] for path in elements
                                           if path.startswith(t + ".") and path.count(".") == 1 and path.endswith("[x]")})
                                for t in counts},
            "summary_elements": {k: sorted(v) for k, v in definitions.model.summary.items() if k in counts},
            "mandatory_elements": {k: sorted(v) for k, v in definitions.model.mandatory.items() if k in counts}}


def _text_leaves(node, out):
    if isinstance(node, dict):
        for key, value in node.items():
            if key not in ("resourceType", "id", "meta", "text"):
                _text_leaves(value, out)
    elif isinstance(node, list):
        for value in node:
            _text_leaves(value, out)
    elif isinstance(node, str):
        out.append(node)
    return out


def _narrative(resource):
    div = (resource.get("text") or {}).get("div") or ""
    return html.unescape(re.sub(r"<[^>]+>", " ", div))


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
                json.dumps(p["components"]), p["usage"], _flag(p["multiple_or"]), _flag(p["multiple_and"])))
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
    meta = {"version": VERSION, "fhir_version": definitions.fhir_version, "settings": config,
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


# ---- extraction workers ------------------------------------------------------------------
# Index row columns that hold interned strings (systems, target types). Workers return the
# strings and their own batch-local pids; the writing process numbers and interns them.
INTERNED = {"token": (1,), "ident_type": (1,), "ref": (1,), "ref_ident": (1,), "quantity": (3,), "composite": (1, 3)}
_WORKER = {}


def preassigned_params(definitions):
    """{resource type: parameters} with ids fixed by the definitions alone."""
    out = {}
    for i, p in enumerate(definitions.params_for(sorted(definitions.model.resource_types)), 1):
        p["id"] = i
        out.setdefault(p["type"], []).append(p)
    return out


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
                fts.append((container, " ".join(_text_leaves(resource, [])), _narrative(resource)))
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
