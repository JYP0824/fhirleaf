"""Cross-implementation comparison with another FHIR server (for example HAPI).

`corpus` derives search requests from the store's own index (types, every
indexed parameter, modifiers, prefixes, chains, _has, controls, error cases).
`record` replays requests against the other server and keeps every page.
`compare` replays them in the embedded engine and classifies each difference
(status, total, membership, order, content, links). The specification, not the
other implementation, decides which behavior is right; error texts and choices
the specification leaves open (default order, page size) are expected to differ.
"""
from __future__ import annotations

import datetime as _dt
import json
import random
import sqlite3
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import quote, urljoin

from .search import Engine

VOLATILE_META = ("versionId", "lastUpdated", "source")


def _q(value):
    return quote(value, safe="|:/$,")


def make_corpus(db_path, output, *, seed=42, patients=5, values_per_param=3):
    rng = random.Random(seed)
    db = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True)
    strings = dict(db.execute("SELECT id, value FROM strings"))
    params = [dict(zip(("id", "type", "code", "ptype", "usage", "multiple_or"), r))
              for r in db.execute("SELECT id, type, code, ptype, usage, multiple_or FROM params")]
    by_key = {(p["type"], p["code"]): p for p in params}
    pt = [r[0] for r in db.execute("SELECT id FROM resources WHERE type_id = (SELECT id FROM strings WHERE value='Patient') ORDER BY pid")]
    chosen = rng.sample(pt, min(patients, len(pt)))
    requests = []

    def add(path, kind):
        requests.append({"request": path, "kind": kind})

    def sample(sql, args=()):
        rows = db.execute(sql, args).fetchall()
        return rng.sample(rows, min(values_per_param, len(rows)))

    types = sorted({p["type"] for p in params})
    patient_types = [t for t in types if (t, "patient") in by_key]
    ident = db.execute("SELECT s.value, t.code FROM sp_token t LEFT JOIN strings s ON s.id = t.system JOIN params p ON p.id = t.param "
                       "JOIN resources r ON r.pid = t.pid WHERE p.type='Patient' AND p.code='identifier' AND r.id IN (%s)"
                       % ",".join("?" * len(chosen)), chosen).fetchall()
    for pid in chosen:
        add(f"Patient?_id={pid}", "id")
        add(f"Patient/{pid}", "read")
        for t in patient_types:
            add(f"{t}?patient=Patient/{pid}&_count=200", "patient_scope")
        add(f"Encounter?subject=Patient/{pid}&_sort=date", "sort")
        add(f"Observation?patient={pid}&_sort=-date&_count=50", "sort")
        add(f"Observation?patient=Patient/{pid}&_summary=count", "summary")
        add(f"MedicationRequest?patient=Patient/{pid}&_include=MedicationRequest:medication&_count=100", "include")
        add(f"Patient?_id={pid}&_revinclude=Encounter:patient", "revinclude")
        add(f"Observation?patient=Patient/{pid}&_elements=code,subject&_count=10", "elements")
        add(f"Observation?patient=Patient/{pid}&_elements=value&_count=10", "elements")
        add(f"Patient?_id={pid}&_summary=true", "summary")
        add(f"Condition?patient=Patient/{pid}&_summary=text", "summary")
        add(f"Condition?patient=Patient/{pid}&_summary=data", "summary")
        add(f"MedicationRequest?patient=Patient/{pid}&_include=MedicationRequest:medication"
            f"&_include:iterate=Medication:ingredient&_count=50", "include")
        add(f"Observation?patient=Patient/{pid}&_include=Observation:encounter&_include=Observation:subject:Patient&_count=30", "include")
        add(f"Encounter?patient=Patient/{pid}&_include=*", "include")
        add(f"Encounter?patient=Patient/{pid}&_include=Encounter:*&_count=20", "include")
        add(f"Patient?_id={pid}&_revinclude=Encounter:*", "revinclude")
        add(f"Encounter?patient=Patient/{pid}&_revinclude=*&_count=10", "revinclude")
        add(f"Patient?_id={pid}&_revinclude=Observation:subject&_revinclude=Condition:subject", "revinclude")
        add(f"Observation?patient=Patient/{pid}", "default_paging")
    for system, value in ident:
        add(f"Patient?identifier={_q(system)}|{_q(value)}", "identifier")
        add(f"Encounter?patient.identifier={_q(system)}|{_q(value)}", "chain")
        add(f"Observation?subject:Patient.identifier={_q(system)}|{_q(value)}&_count=100", "chain")
    for p in params:
        if p["code"].startswith("_") and p["code"] not in ("_profile", "_tag", "_security", "_source"):
            continue
        t, code, pid_ = p["type"], p["code"], p["id"]
        scope = ""
        if t in patient_types and chosen:
            scope = f"&patient=Patient/{rng.choice(chosen)}"
        if p["ptype"] == "token":
            for system, c in sample("SELECT DISTINCT system, code FROM sp_token WHERE param=? LIMIT 2000", (pid_,)):
                sys_ = strings.get(system)
                add(f"{t}?{code}={_q(c)}{scope}", "token")
                if sys_:
                    add(f"{t}?{code}={_q(sys_)}|{_q(c)}{scope}", "token")
                    add(f"{t}?{code}={_q(sys_)}|{scope}", "token_system")
                else:
                    add(f"{t}?{code}=|{_q(c)}{scope}", "token_nosystem")
                add(f"{t}?{code}:not={_q(c)}{scope}", "token_not")
            for (norm,) in sample("SELECT DISTINCT norm FROM sp_token_text WHERE param=? LIMIT 500", (pid_,)):
                add(f"{t}?{code}:text={_q(norm[:6].lower())}{scope}", "token_text")
        elif p["ptype"] == "reference":
            for tt, tid in sample("SELECT DISTINCT target_type, target_id FROM sp_ref WHERE param=? LIMIT 2000", (pid_,)):
                add(f"{t}?{code}={strings[tt]}/{tid}", "reference")
                add(f"{t}?{code}={tid}", "reference_id")
            add(f"{t}?{code}:missing=true{scope}", "missing")
            for url, version in sample("SELECT DISTINCT url, version FROM sp_ref_url WHERE param=? LIMIT 500", (pid_,)):
                add(f"{t}?{code}={_q(url)}", "reference_url")
                if version:
                    add(f"{t}?{code}={_q(url)}|{_q(version)}", "reference_url")
        elif p["ptype"] == "date":
            for low, high in sample("SELECT DISTINCT low, high FROM sp_date WHERE param=? AND low > -4611686018427387904 LIMIT 2000", (pid_,)):
                day = _dt.datetime(1970, 1, 1, tzinfo=_dt.timezone.utc) + _dt.timedelta(microseconds=low)
                d = day.strftime("%Y-%m-%d")
                add(f"{t}?{code}={d}{scope}", "date_day")
                add(f"{t}?{code}=ge{d}&{code}=le{d}{scope}", "date_range")
                add(f"{t}?{code}=gt{day.strftime('%Y-%m-%dT%H:%M:%S')}{scope}", "date_time_no_tz")
                add(f"{t}?{code}=lt{_q(day.strftime('%Y-%m-%dT%H:%M:%S+00:00'))}{scope}", "date_time_tz")
                add(f"{t}?{code}={day.strftime('%Y-%m')}{scope}", "date_month")
        elif p["ptype"] == "string" and p["usage"] == "phonetic":
            for norm, exact in sample("SELECT DISTINCT norm, exact FROM sp_string WHERE param=? LIMIT 2000", (pid_,)):
                add(f"{t}?{code}={_q(exact)}{scope}", "string_phonetic")
        elif p["ptype"] == "string":
            for norm, exact in sample("SELECT DISTINCT norm, exact FROM sp_string WHERE param=? LIMIT 2000", (pid_,)):
                add(f"{t}?{code}={_q(exact[:4])}{scope}", "string")
                add(f"{t}?{code}:exact={_q(exact)}{scope}", "string_exact")
                add(f"{t}?{code}:contains={_q(exact[1:5].lower())}{scope}", "string_contains")
        elif p["ptype"] == "quantity":
            for value, system, c in sample("SELECT DISTINCT low, system, code FROM sp_quantity WHERE param=? AND low = high LIMIT 2000", (pid_,)):
                v = repr(value).rstrip("0").rstrip(".") if "." in repr(value) else repr(value)
                add(f"{t}?{code}={v}{scope}", "quantity_eq")
                add(f"{t}?{code}=gt{v}{scope}", "quantity_gt")
                if c:
                    add(f"{t}?{code}=le{v}|{_q(strings.get(system) or '')}|{_q(c)}{scope}", "quantity_units")
        elif p["ptype"] == "uri":
            for (u,) in sample("SELECT DISTINCT uri FROM sp_uri WHERE param=? LIMIT 200", (pid_,)):
                add(f"{t}?{code}={_q(u)}{scope}", "uri")
                add(f"{t}?{code}:below={_q(u[:len(u) // 2])}{scope}", "uri_below")
        elif p["ptype"] == "composite":
            for system, c, value in sample("SELECT DISTINCT system, code, c2_low FROM sp_composite WHERE param=? AND c2_low IS NOT NULL AND c2_low = c2_high AND c2_text IS NOT NULL LIMIT 2000", (pid_,)):
                add(f"{t}?{code}={_q(strings.get(system, ''))}|{_q(c)}$gt{value - 1:g}{scope}", "composite")
    for t in ("Observation", "Condition", "Procedure"):
        for (c,) in sample(f"SELECT DISTINCT t.code FROM sp_token t JOIN params p ON p.id=t.param WHERE p.type='{t}' AND p.code='code' LIMIT 500"):
            add(f"Patient?_has:{t}:patient:code={_q(c)}", "has")
    meta = {k: json.loads(v) for k, v in db.execute("SELECT key, value FROM meta")}
    compartments = meta.get("compartments", {})
    for pid in chosen:
        for t in sorted(compartments.get("Patient", {}))[:12]:
            add(f"Patient/{pid}/{t}?_count=200", "compartment")
        add(f"Patient/{pid}/Observation?_sort=-date&_count=50", "compartment")
    for c, members in compartments.items():
        if c == "Patient":
            continue
        rows = db.execute("SELECT id FROM resources WHERE type_id = (SELECT id FROM strings WHERE value = ?) LIMIT 2", (c,)).fetchall()
        for (cid,) in rows:
            for t in sorted(members)[:4]:
                add(f"{c}/{cid}/{t}", "compartment")
    loaded = sorted(meta.get("resource_counts", {}))
    if chosen:
        add(f"?_id={chosen[0]}", "system")
        add(f"?_type=Patient,{loaded[0]}&_lastUpdated=ge2000-01-01&_count=50", "system")
        add(f"?_type={','.join(loaded[:3])}&_count=100&_sort=_id", "system")
    vs_params = db.execute("SELECT DISTINCT p.type, p.code, v.url FROM params p JOIN sp_token t ON t.param = p.id "
                           "JOIN valueset_codes v ON v.code = t.code AND v.system IS t.system "
                           "JOIN valuesets s ON s.url = v.url AND s.complete = 1 LIMIT 3000").fetchall()
    for t, code, url in rng.sample(vs_params, min(12, len(vs_params))):
        scope = f"&patient=Patient/{rng.choice(chosen)}" if (t, "patient") in by_key and chosen else ""
        add(f"{t}?{code}:in={_q(url)}{scope}", "token_in")
        add(f"{t}?{code}:not-in={_q(url)}{scope}", "token_not_in")
    for t, code, system, value in sample("SELECT DISTINCT p.type, p.code, s.value, i.value FROM sp_ref_ident i "
                                         "JOIN params p ON p.id = i.param LEFT JOIN strings s ON s.id = i.system LIMIT 500"):
        add(f"{t}?{code}:identifier={_q(system or '')}|{_q(value)}", "reference_identifier")
    if meta["settings"].get("fulltext"):
        for t, text, narrative in sample("SELECT s.value, f.content, f.narrative FROM fts f JOIN resources r ON r.pid = f.rowid "
                                         "JOIN strings s ON s.id = r.type_id LIMIT 3000"):
            words = [w for w in text.split() if w.isalpha() and len(w) > 3]
            if words:
                add(f"{t}?_content={_q(words[0])}", "fulltext")
                add(f"{t}?_content={_q(' '.join(words[:2]))}", "fulltext")
            words = [w for w in narrative.split() if w.isalpha() and len(w) > 3]
            if words:
                add(f"{t}?_text={_q(words[0])}", "fulltext")
    for (lat, lon) in sample("SELECT lat, lon FROM sp_position LIMIT 200"):
        add(f"Location?near={lat}|{lon}|5|km", "near")
        add(f"Location?near={lat}|{lon}", "near")
    add("Observation?foo=bar", "error_unknown_param")
    add("Observation?date=notadate", "error_bad_date")
    add("Observation?code:below=abc", "error_bad_modifier")
    add("Foo?x=1", "error_unknown_type")
    add("Practitioner?_count=5", "empty_type")
    add("Observation?patient.foo=bar", "error_bad_chain")
    add("Observation?_count=abc", "error_bad_count")
    out = Path(output)
    out.write_text("".join(json.dumps(r) + "\n" for r in requests), encoding="utf-8")
    print(json.dumps({"requests": len(requests), "kinds": Counter(r["kind"] for r in requests)}))
    return requests


def _fetch(url, timeout):
    request = urllib.request.Request(url, headers={"Accept": "application/fhir+json", "Prefer": "handling=strict"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            body = json.load(exc)
        except ValueError:
            body = {"raw": exc.read().decode("utf-8", "replace")[:2000]}
        return exc.code, body


def record(base_url, requests_path, output, *, max_pages=20, timeout=120):
    base = base_url.rstrip("/")
    out = Path(output)
    out.mkdir(parents=True, exist_ok=True)
    status, metadata = _fetch(base + "/metadata", timeout)
    (out / "metadata.json").write_text(json.dumps(metadata, indent=1), encoding="utf-8")
    done = set()
    responses = out / "responses.jsonl"
    if responses.exists():
        done = {json.loads(line)["request"] for line in responses.read_text(encoding="utf-8").splitlines() if line}
    requests = [json.loads(line) for line in Path(requests_path).read_text(encoding="utf-8").splitlines() if line.strip()]
    with responses.open("a", encoding="utf-8") as handle:
        for item in requests:
            if item["request"] in done:
                continue
            pages, url = [], f"{base}/{item['request'].lstrip('/')}"
            while url and len(pages) < max_pages:
                status, body = _fetch(url, timeout)
                pages.append({"status": status, "body": body})
                nxt = [l["url"] for l in body.get("link", []) if l.get("relation") == "next"] if status == 200 else []
                url = urljoin(url, nxt[0]) if nxt else None
            handle.write(json.dumps({**item, "base": base, "pages": pages, "truncated": url is not None}) + "\n")
            handle.flush()
    software = metadata.get("software", {}) if isinstance(metadata, dict) else {}
    print(json.dumps({"recorded": len(requests), "server": software}))


def _strip(resource):
    if not isinstance(resource, dict):
        return resource
    out = dict(resource)
    meta = {k: v for k, v in out.get("meta", {}).items() if k not in VOLATILE_META}
    if meta:
        out["meta"] = meta
    else:
        out.pop("meta", None)
    return out


def _entries(bundle):
    return [(e.get("resource", {}).get("resourceType", "") + "/" + e.get("resource", {}).get("id", ""),
             e.get("search", {}).get("mode")) for e in bundle.get("entry", [])]


def compare(db_path, recorded_dir, report_path, *, examples=15):
    lines = (Path(recorded_dir) / "responses.jsonl").read_text(encoding="utf-8").splitlines()
    engines = {}
    counts, kinds, samples = Counter(), defaultdict(Counter), defaultdict(list)
    for line in lines:
        item = json.loads(line)
        base = item["base"]
        engine = engines.get(base) or engines.setdefault(base, Engine(db_path, base_url=base))
        path = "/" + item["request"].lstrip("/")
        problems = []
        for i, expected in enumerate(item["pages"]):
            status, body = engine.get(path)
            problems += _diff(i, expected["status"], expected["body"], status, body)
            nxt = [l["url"] for l in body.get("link", []) if l.get("relation") == "next"] if status == 200 else []
            if not nxt:
                if i + 1 < len(item["pages"]):
                    problems.append(("paging", f"page {i}: engine has no next link, reference has {len(item['pages'])} pages"))
                break
            path = nxt[0][len(base):] if nxt[0].startswith(base) else nxt[0]
        categories = sorted({p[0] for p in problems}) or ["match"]
        for c in categories:
            counts[c] += 1
            kinds[item.get("kind", "external")][c] += 1
            if c != "match" and len(samples[c]) < examples:
                samples[c].append({"request": item["request"], "kind": item.get("kind"),
                                   "details": [p[1] for p in problems if p[0] == c][:3]})
    report = {"requests": len(lines), "outcomes": dict(counts), "by_kind": {k: dict(v) for k, v in sorted(kinds.items())},
              "examples": dict(samples)}
    Path(report_path).write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"requests": len(lines), "outcomes": dict(counts)}))
    return report


def _diff(i, ref_status, ref, status, got):
    if ref_status != status:
        return [("status", f"page {i}: reference {ref_status}, engine {status}: "
                           f"{json.dumps(got.get('issue', ''))[:200]} | ref {json.dumps(ref.get('issue', ''))[:200]}")]
    if ref.get("resourceType") == "OperationOutcome":
        return []           # same status; error texts are implementation-specific
    if ref.get("resourceType") != "Bundle":
        return [] if _strip(ref) == _strip(got) else [("content", f"page {i}: non-Bundle response differs")]
    out = []
    if ("total" in ref) != ("total" in got) or ref.get("total") != got.get("total"):
        out.append(("total", f"page {i}: reference total {ref.get('total')}, engine {got.get('total')}"))
    a, b = _entries(ref), _entries(got)
    if a != b:
        if sorted(a) == sorted(b):
            out.append(("order", f"page {i}: same entries, different order"))
        else:
            missing, extra = sorted(set(a) - set(b))[:3], sorted(set(b) - set(a))[:3]
            out.append(("membership", f"page {i}: {len(a)} vs {len(b)}; missing {missing} extra {extra}"))
    else:
        for x, y in zip(ref.get("entry", []), got.get("entry", [])):
            if _strip(x.get("resource")) != _strip(y.get("resource")):
                out.append(("content", f"page {i}: {x.get('fullUrl')} resource body differs"))
                break
            if x.get("fullUrl") != y.get("fullUrl"):
                out.append(("full_url", f"page {i}: {x.get('fullUrl')} vs {y.get('fullUrl')}"))
                break
    ra = sorted(l.get("relation") for l in ref.get("link", []))
    rb = sorted(l.get("relation") for l in got.get("link", []))
    if ra != rb:
        out.append(("links", f"page {i}: reference {ra}, engine {rb}"))
    return out
