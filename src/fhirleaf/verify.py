"""Specification check of the search path on a small patient subset.

`subset` copies a few patients' resources (and everything they reference) out of
an NDJSON directory. `verify` loads that subset, derives a request corpus and
compares, per request, the engine's complete result (all pages, in order, plus
total) with a brute-force evaluation: every resource of the type is re-indexed
from its JSON and tested against the FHIR R4 matching rules in plain Python,
without the SQL index tables or the SQL translation.
"""
from __future__ import annotations

import gzip
import json
import random
import re
import tempfile
import zlib
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import parse_qsl

from .conformance import make_corpus
from .extract import contained_resources, Extractor
from .search import CONTROL, Engine, _date_eval, _distance_km
from .inputs import conditional_key, input_files, read_file, resolve_conditional
from .store import Definitions, _narrative, _text_leaves, load
from .values import soundex, normalize, number_bounds, resolve_timezone, split_escaped, split_or, split_prefix, unescape

TERM = re.compile(r"[^\W_]+")


def _refs(node, out):
    if isinstance(node, dict):
        ref = node.get("reference")
        if isinstance(ref, str):
            out.add(ref)
        for v in node.values():
            if isinstance(v, (dict, list)):
                _refs(v, out)
    elif isinstance(node, list):
        for v in node:
            _refs(v, out)
    return out


def subset(inputs, output, *, patients=5, seed=42):
    """A few patients' resources plus everything they reference (literal or conditional),
    written as NDJSON with conditional references resolved."""
    files = input_files([inputs] if isinstance(inputs, (str, Path)) else inputs)
    ids = sorted({r["id"] for f in files for r in read_file(f) if r["resourceType"] == "Patient"})
    chosen = {f"Patient/{i}" for i in random.Random(seed).sample(ids, min(patients, len(ids)))}
    keep, wanted = set(), set()
    for f in files:
        for r in read_file(f):
            key = f"{r['resourceType']}/{r['id']}"
            refs = _refs(r, set())
            if key in chosen or refs & chosen:
                keep.add(key)
                wanted |= refs
    resolved = {}
    for _ in range(4):              # close over referenced resources (Medication, Location -> Organization, ...)
        missing = wanted - keep - set(resolved)
        if not missing:
            break
        for f in files:
            for r in read_file(f):
                key = f"{r['resourceType']}/{r['id']}"
                hits = ({key} | set(conditional_key(r))) & missing
                if hits:
                    keep.add(key)
                    resolved.update({h: key for h in hits if "?" in h})
                    wanted |= _refs(r, set())
    out = Path(output)
    out.mkdir(parents=True, exist_ok=True)
    written = {}
    for f in files:
        for r in read_file(f):
            key = f"{r['resourceType']}/{r['id']}"
            if key in keep and key not in written:
                resolve_conditional(r, resolved)
                written[key] = r
    by_type = defaultdict(list)
    for r in written.values():
        by_type[r["resourceType"]].append(r)
    for rtype, rows in by_type.items():
        with gzip.open(out / f"{rtype}.ndjson.gz", "wt", encoding="utf-8") as h:
            h.writelines(json.dumps(r) + "\n" for r in rows)
    return sorted(chosen), len(written)


def _phrase(doc, terms):
    n = len(terms)
    return any(doc[i:i + n] == terms for i in range(len(doc) - n + 1)) if n else False


class Oracle:
    """Brute-force FHIR search over every resource of a store, from re-extracted index values."""

    def __init__(self, engine, definitions):
        self.e = engine
        params = [dict(zip(("id", "type", "code", "ptype", "url", "expression", "targets", "components", "usage",
                            "multiple_or", "multiple_and"), row))
                  for row in engine.db.execute("SELECT * FROM params")]
        for p in params:
            p["targets"], p["components"] = json.loads(p["targets"]), json.loads(p["components"])
        self.tz = resolve_timezone(engine.settings["timezone"])
        extractor = Extractor(definitions.model, params, lambda s: s, self.tz, engine.settings.get("local_bases", []))
        self.by_type, self.res, self.index, self.words = defaultdict(list), {}, {}, {}
        self.rowid, locals_ = {}, {}
        for pid, rowid, body, container in engine.db.execute("SELECT pid, id, json, container FROM resources ORDER BY pid"):
            r = json.loads(zlib.decompress(body))
            if container is None:          # contained resources are reachable only through their container
                self.by_type[r["resourceType"]].append(pid)
                local, contained = contained_resources(r)
                locals_.update({row: inner for row, _, inner in contained})
            else:
                local = locals_.get(rowid)
            self.res[pid], self.rowid[pid] = r, rowid
            rows = defaultdict(list)
            for table, items in extractor.rows(r, pid, local).items():
                for row in items:
                    rows[(table, row[0])].append(row[1:-1])
            self.index[pid] = rows
            self.words[pid] = ([normalize(w) for w in TERM.findall(" ".join(_text_leaves(r, [])))],
                               [normalize(w) for w in TERM.findall(_narrative(r))])
        self.key = {(r["resourceType"], self.rowid[pid]): pid for pid, r in self.res.items()}
        self.vs = defaultdict(set)
        strings = dict(engine.db.execute("SELECT id, value FROM strings"))
        for url, system, code in engine.db.execute("SELECT url, system, code FROM valueset_codes"):
            self.vs[url].add((strings.get(system), code))

    def search(self, rtype, query, compartment=None):
        pairs = parse_qsl(query, keep_blank_values=True)
        types = [rtype] if rtype else ([t for n, v in pairs if n == "_type" for t in v.split(",") if t]
                                       or sorted(self.e.known))
        crit = [(n, v) for n, v in pairs if n.split(":", 1)[0] not in CONTROL and n != "_type" and v != ""]
        found = sorted(pid for t in types for pid in self.by_type.get(t, [])
                       if all(self.match(t, pid, n, v) for n, v in crit)
                       and (not compartment or self.in_compartment(t, pid, *compartment)))
        for key in reversed([k for n, v in pairs if n == "_sort" for k in v.split(",") if k]):
            found = self.sort(types[0], found, key)
        return [f"{self.res[pid]['resourceType']}/{self.res[pid]['id']}" for pid in found]

    def includes(self, keys, query):
        """_include / _revinclude targets (with :iterate) of the matched resources, from the index rows."""
        pairs = parse_qsl(query, keep_blank_values=True)
        fwd = [(n.endswith((":iterate", ":recurse")), v) for n, v in pairs if n.split(":")[0] == "_include"]
        rev = [(n.endswith((":iterate", ":recurse")), v) for n, v in pairs if n.split(":")[0] == "_revinclude"]
        if not fwd and not rev:
            return set()
        top = {pid for pids in self.by_type.values() for pid in pids}      # contained rows are never included
        refs = lambda t, code=None: [p for (pt, c), p in self.e.params.items()   # noqa: E731
                                     if pt == t and p["ptype"] == "reference" and (code in (None, "*") or c == code)]
        seen = {self.key[tuple(k.split("/", 1))] for k in keys}
        frontier, out, depth = list(seen), set(), 0
        while frontier:
            found = set()
            for iterate, value in fwd:
                if depth and not iterate:
                    continue
                parts = value.split(":")
                for pid in frontier:
                    r = self.res[pid]
                    if value != "*" and parts[0] != r["resourceType"]:
                        continue
                    params = refs(r["resourceType"], None if value == "*" else parts[1])
                    target = parts[2] if len(parts) == 3 else None
                    for p in params:
                        for t, i in self.rows(pid, "ref", p):
                            q = self.key.get((t, i))
                            if q in top and (target is None or t == target):
                                found.add(q)
            for iterate, value in rev:
                if depth and not iterate:
                    continue
                parts = value.split(":")
                target = parts[2] if len(parts) == 3 else None
                marks = {(self.res[pid]["resourceType"], self.res[pid]["id"]) for pid in frontier
                         if pid in top and (target is None or self.res[pid]["resourceType"] == target)}
                sources = sorted(self.by_type) if value == "*" else [parts[0]]
                for source in sources:
                    for p in refs(source, None if value == "*" else parts[1]):
                        for q in self.by_type.get(source, []):
                            if any(ref in marks for ref in self.rows(q, "ref", p)):
                                found.add(q)
            fresh = [pid for pid in found if pid not in seen]
            seen.update(fresh)
            out.update(f"{self.res[pid]['resourceType']}/{self.res[pid]['id']}" for pid in fresh)
            frontier, depth = fresh, depth + 1
        return out

    def subset_ok(self, returned, query):
        """_summary / _elements: the returned elements are exactly those the rule keeps, tagged SUBSETTED."""
        pairs = dict(parse_qsl(query, keep_blank_values=True))
        summary, elements = pairs.get("_summary"), [e for e in pairs.get("_elements", "").split(",") if e]
        original = self.res[self.key[(returned["resourceType"], returned["id"])]]
        if summary not in ("true", "text", "data") and not elements:
            return returned == original
        rtype, meta = returned["resourceType"], self.e.meta
        if summary == "data":
            keep = {k for k in original if k != "text"}
        else:
            names = set(meta["summary_elements"].get(rtype, [])) if summary == "true" else \
                ({"text"} | set(meta["mandatory_elements"].get(rtype, []))) if summary == "text" else \
                (set(elements) | set(meta["mandatory_elements"].get(rtype, [])))
            keep = {k for k in original if k in ("resourceType", "id", "meta") or k in names
                    or any(k.startswith(n) and k[len(n):][:1].isupper() for n in names)}
        tagged = any(t.get("code") == "SUBSETTED" for t in returned.get("meta", {}).get("tag", []))
        return set(returned) == keep | {"meta"} and tagged and all(returned[k] == original[k] for k in keep if k != "meta")

    def in_compartment(self, rtype, pid, comp, cid):
        for code in self.e.compartments.get(comp, {}).get(rtype, []):
            if code == "{def}":
                if rtype == comp and self.res[pid]["id"] == cid:
                    return True
            elif (rtype, code) in self.e.params and \
                    (comp, cid) in self.rows(pid, "ref", self.e.params[(rtype, code)]):
                return True
        return False

    def sort(self, rtype, pids, key):
        desc, name = key.startswith("-"), key.lstrip("-")
        if name == "_id":
            return sorted(pids, key=lambda p: self.res[p]["id"], reverse=desc)
        p = self.e.params[(rtype, name)]
        table = {"date": "date", "token": "token", "string": "string", "quantity": "quantity", "number": "number",
                 "reference": "ref", "uri": "uri"}[p["ptype"]]
        pos = {"date": 0, "token": 1, "string": 0, "quantity": 0, "number": 0, "ref": 1, "uri": 0}[table]
        def value(pid):
            vals = [row[pos] for row in self.index[pid].get((table, p["id"]), []) if row[pos] is not None]
            return (max(vals) if desc else min(vals)) if vals else None
        have = [x for x in pids if value(x) is not None]
        none = [x for x in pids if value(x) is None]
        return sorted(have, key=value, reverse=desc) + none          # stable: ties keep store order

    def rows(self, pid, table, p):
        return self.index[pid].get((table, p["id"]), [])

    def match(self, rtype, pid, name, value):
        if name.startswith("_has:"):
            _, source, ref_code, rest = name.split(":", 3)
            ref = self.e.params[(source, ref_code)]
            me = (rtype, self.res[pid]["id"])
            return any(any((t, i) == me for t, i in self.rows(q, "ref", ref)) and self.match(source, q, rest, value)
                       for q in self.by_type.get(source, []))
        head, dot, rest = name.partition(".")
        code, _, modifier = head.partition(":")
        r = self.res[pid]
        if dot:
            p = self.e.params[(rtype, code)]
            targets = [modifier] if modifier else p["targets"]
            for t, i in self.rows(pid, "ref", p):
                if t in targets and (t, i) in self.key:
                    try:
                        if self.match(t, self.key[(t, i)], rest, value):
                            return True
                    except KeyError:        # chained parameter not defined for this target type
                        continue
            return False
        if code == "_id":
            return r["id"] in [unescape(v) for v in split_or(value)]
        if code == "_lastUpdated":
            low, high = self.e.last_updated
            for v in split_or(value):
                prefix, s_low, s_high = self.e._date_bounds(v)
                if _date_eval(prefix, low, high, s_low, s_high, self.e._approx(s_low, s_high)):
                    return True
            return False
        if code in ("_content", "_text"):
            doc = self.words[pid][0 if code == "_content" else 1]
            return any(all(_phrase(doc, [normalize(w) for w in TERM.findall(term)]) for term in unescape(v).split())
                       for v in split_or(value))
        p = self.e.params[(rtype, code)]
        table = {"reference": "ref", "special": "position"}.get(p["ptype"], p["ptype"])
        if modifier == "missing":
            present = self.rows(pid, table, p) or (p["ptype"] == "reference" and
                                                  (self.rows(pid, "ref_url", p) or self.rows(pid, "ref_ident", p)))
            return (not present) == (value == "true")
        values = split_or(value)
        if p["ptype"] == "special":
            out = False
            for v in values:
                parts = split_escaped(v, "|")
                km = (float(parts[2]) if len(parts) > 2 and parts[2] else self.e.settings.get("near_default_km", 1.0)) * \
                    {"km": 1.0, "m": 0.001, "mi": 1.609344, "[mi_i]": 1.609344}[parts[3] if len(parts) > 3 and parts[3] else "km"]
                out = out or any(_distance_km(lat, lon, float(parts[0]), float(parts[1])) <= km
                                 for lat, lon in self.rows(pid, "position", p))
            return out
        if p["ptype"] == "reference" and modifier == "identifier":
            return any(_token(row, v) for row in self.rows(pid, "ref_ident", p) for v in values)
        if p["ptype"] == "token" and modifier in ("in", "not-in"):
            codes = set().union(*(self.vs[unescape(v).split("|")[0]] for v in values))
            hit = any((system, code_) in codes for system, code_ in self.rows(pid, "token", p))
            return not hit if modifier == "not-in" else hit
        if p["ptype"] == "token":
            if modifier == "text":
                texts = [row[0] for row in self.rows(pid, "token_text", p)]
                return any(t.startswith(normalize(unescape(v))) for v in values for t in texts)
            hit = any(_token(row, v) for row in self.rows(pid, "token", p) for v in values)
            return not hit if modifier == "not" else hit
        if p["ptype"] == "reference":
            out = False
            for v in values:
                v = unescape(v).split("/_history/")[0]
                local = next((b for b in self.e.local_bases if v.startswith(b + "/")), None)
                if ":" in v.split("/")[0]:
                    url, _, version = v.partition("|")
                    out = out or any(u == url and (not version or ver == version) for u, ver in self.rows(pid, "ref_url", p))
                    if local is None:
                        continue
                    v = v[len(local) + 1:]
                segs = v.split("/")
                t, i = (segs[-2], segs[-1]) if len(segs) >= 2 else (modifier or None, segs[0])
                if t is None and len(p["targets"]) == 1:
                    t = p["targets"][0]
                if modifier and t != modifier:
                    continue
                out = out or any(ri == i and (t is None or rt == t) for rt, ri in self.rows(pid, "ref", p))
            return out
        if p["ptype"] == "date":
            return any(_date_eval(b[0], lo, hi, b[1], b[2], self.e._approx(b[1], b[2]))
                       for v in values for b in [self.e._date_bounds(v)] for lo, hi in self.rows(pid, "date", p))
        if p["ptype"] == "string" and p.get("usage") == "phonetic":
            return any(soundex(unescape(v)) == norm for v in values for norm, _ in self.rows(pid, "string", p))
        if p["ptype"] == "string":
            out = False
            for v in values:
                v = unescape(v)
                for norm, exact in self.rows(pid, "string", p):
                    out = out or (exact == v if modifier == "exact" else normalize(v) in norm if modifier == "contains"
                                  else norm.startswith(normalize(v)))
            return out
        if p["ptype"] == "quantity":
            return any(_quantity(row, v) for v in values for row in self.rows(pid, "quantity", p))
        if p["ptype"] == "number":
            return any(_number(split_prefix(v)[0], split_prefix(v)[1], row[0]) for v in values for row in self.rows(pid, "number", p))
        if p["ptype"] == "uri":
            out = False
            for v in values:
                v = unescape(v)
                for (u,) in self.rows(pid, "uri", p):
                    out = out or (u.startswith(v) if modifier == "below" else v.startswith(u) if modifier == "above" else u == v)
            return out
        if p["ptype"] == "composite":
            kind = p["components"][1][0]
            for v in values:
                first, second = split_or(v, "$")
                for system, code_, s2, c2, val, lo, hi, text in self.rows(pid, "composite", p):
                    if not _token((system, code_), first):
                        continue
                    if kind == "token" and _token((s2, c2), second):
                        return True
                    if kind == "quantity" and lo is not None and _quantity((lo, hi, s2, c2, text), second):
                        return True
                    if kind == "date":
                        b = self.e._date_bounds(second)
                        if _date_eval(b[0], lo, hi, b[1], b[2], self.e._approx(b[1], b[2])):
                            return True
                    if kind == "string" and (text or "").startswith(normalize(unescape(second))):
                        return True
            return False
        return False


def _token(row, v):
    system, code = row
    parts = split_escaped(v, "|")
    if len(parts) == 1:
        return code == parts[0]
    s, c = parts
    if s == "":
        return system is None and code == c
    if c == "":
        return system == s
    return system == s and code == c


def _number(prefix, text, t_low, t_high=None):
    """Prefix semantics on a target interval [t_low, t_high] (a plain value: t_low = t_high)."""
    t_high = t_low if t_high is None else t_high
    number, low, high = number_bounds(text)
    inside = low <= t_low and t_high < high
    if prefix == "eq":
        return inside
    if prefix == "ne":
        return not inside
    if prefix == "ap":
        a, b = sorted((number * 0.9, number * 1.1))
        return t_high >= a and t_low <= b
    return {"gt": t_high > number, "ge": t_high >= number, "lt": t_low < number, "le": t_low <= number,
            "sa": t_low > number, "eb": t_high < number}[prefix]


def _quantity(row, v):
    t_low, t_high, system, code, unit = row
    prefix, text = split_prefix(v)
    parts = split_escaped(text, "|")
    if not _number(prefix, parts[0], t_low, t_high):
        return False
    if len(parts) >= 3:
        s, c = parts[1], parts[2]
        if s and c:
            return system == s and code == c
        if c:
            return code == c or unit == c
        if s:
            return system == s
    return True


def verify(inputs, definitions_path, output, *, packages=(), fulltext=False, patients=5, seed=42, settings=None):
    out = Path(output)
    out.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(dir=out))
    chosen, kept = subset(inputs, work / "ndjson", patients=patients, seed=seed)
    db = work / "subset.sqlite"
    load(work / "ndjson", definitions_path, db, packages=packages, settings=settings, fulltext=fulltext,
         hash_sources=False)
    corpus = out / "verify_corpus.jsonl"
    make_corpus(db, corpus, seed=seed, patients=patients)
    engine, definitions = Engine(db), Definitions(definitions_path, packages)
    oracle = Oracle(engine, definitions)
    outcomes, kinds, examples = Counter(), defaultdict(Counter), defaultdict(list)
    skipped = Counter()
    for line in corpus.read_text(encoding="utf-8").splitlines():
        item = json.loads(line)
        path = item["request"]
        head, _, query = path.partition("?")
        segs = [s for s in head.split("/") if s]
        if len(segs) == 3:
            rtype, compartment = segs[2], segs[:2]
        elif len(segs) == 1:
            rtype, compartment = segs[0], None
        elif not segs:
            rtype, compartment = None, None
        else:
            skipped["read"] += 1
            continue
        status, body = engine.get("/" + path)
        if status != 200:
            outcomes["error_response"] += 1
            kinds[item["kind"]]["error_response"] += 1
            continue
        got, total, included, subset_bad = [], body.get("total"), set(), 0
        while True:
            for e in body.get("entry", []):
                key = f"{e['resource']['resourceType']}/{e['resource']['id']}" if e["resource"].get("id") else None
                mode = e.get("search", {}).get("mode")
                if mode == "match":
                    got.append(key)
                    subset_bad += not oracle.subset_ok(e["resource"], query)
                elif mode == "include":
                    included.add(key)
            nxt = [l["url"] for l in body.get("link", []) if l["relation"] == "next"]
            if not nxt:
                break
            status, body = engine.get(nxt[0][len(engine.base):])
        expected = oracle.search(rtype, query, compartment)
        sorted_query = "_sort=" in query
        expected_inc = oracle.includes(expected, query) - set(expected)
        verdict = "match"
        counts_only = dict(parse_qsl(query)).get("_summary") == "count" or dict(parse_qsl(query)).get("_count") == "0"
        if total != len(expected):
            verdict = "total"
        elif counts_only:
            verdict = "match" if not got else "membership"      # total only, no entries
        elif (got != expected) if sorted_query else (sorted(got) != sorted(expected)):
            verdict = "order" if sorted(got) == sorted(expected) else "membership"
        elif included - set(got) != expected_inc:
            verdict = "include"
        elif subset_bad:
            verdict = "subsetting"
        outcomes[verdict] += 1
        kinds[item["kind"]][verdict] += 1
        if verdict != "match" and len(examples[verdict]) < 15:
            examples[verdict].append({"request": path, "engine": len(got), "expected": len(expected),
                                      "missing": sorted(set(expected) - set(got))[:3], "extra": sorted(set(got) - set(expected))[:3]})
    report = {"patients": chosen, "resources": kept, "outcomes": dict(outcomes), "skipped": dict(skipped),
              "by_kind": {k: dict(v) for k, v in sorted(kinds.items())}, "examples": dict(examples)}
    (out / "verify_report.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    print(json.dumps({"resources": kept, "outcomes": dict(outcomes)}))
    return report
