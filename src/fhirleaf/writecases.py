"""Write cases for writable stores, run by `fhirleaf verify --cases`.

Steps: an ordered list of requests on a writable copy of the case fixture, with expectations
worked out by hand from FHIR R4 (http.html: create, update, delete, read, vread, history-instance).
Each step sets the store clock first, so the in-process and HTTP runs see the same instants.

Equivalence: a store filled by PUT from an empty store must equal the loaded fixture (resources and
every index row), and a fixture store after updates and deletes must index like a fresh load of the
resulting resources.
"""
from __future__ import annotations

import copy
import json
import re
import shutil
import sqlite3
import threading
import zlib
from collections import Counter
from datetime import datetime

from .cases import FIXTURE, SETTINGS
from .extract import TABLES
from .search import Engine
from .store import INTERNED, load


class Clock:
    def __init__(self, at):
        self.at = datetime.fromisoformat(at)

    def set(self, at):
        self.at = datetime.fromisoformat(at)

    def __call__(self):
        return self.at


def _t(minutes):
    return f"2026-02-01T00:{minutes:02d}:00+00:00"


NEW_PATIENT = {"resourceType": "Patient", "id": "ignored", "meta": {"versionId": "9", "source": "#client"},
               "name": [{"family": "Neu"}]}
P1 = next(r for r in FIXTURE if r["resourceType"] == "Patient" and r["id"] == "p1")
P1_V2 = {**copy.deepcopy(P1), "name": [{"family": "Schmidt", "given": ["Anna"]}]}
P1_V3 = {**copy.deepcopy(P1_V2), "gender": "unknown"}
O1 = next(r for r in FIXTURE if r["resourceType"] == "Observation" and r["id"] == "o1")
O4_P3 = {**copy.deepcopy(next(r for r in FIXTURE if r["resourceType"] == "Observation" and r["id"] == "o4")),
         "subject": {"reference": "Patient/p3"}}
CONTAINED = {"resourceType": "MedicationRequest", "id": "mrw", "status": "active", "intent": "order",
             "subject": {"reference": "Patient/p2"}, "medicationReference": {"reference": "#mw"},
             "contained": [{"resourceType": "Medication", "id": "mw", "code": {"coding": [{"system": "urn:w", "code": "w1"}]}}]}

# Each step: id, method, path, body (dict or text), headers, at (clock), expect: status plus checks.
#   version: body meta.versionId; family: body name[0].family; match: search matches (all pages, as a set);
#   total; header: {name: regex} (HTTP only); entries: [(method, has resource)] of a history page;
#   empty: no body; save: remember body id under this name ($name in later paths).
STEPS = [
    ("capability", "GET", "metadata", None, {}, _t(0), {"status": 200, "patient_interactions": [
        "read", "vread", "search-type", "create", "update", "delete", "history-instance"]}),
    ("create", "POST", "Patient", NEW_PATIENT, {}, _t(1), {
        "status": 201, "version": "1", "last_updated": _t(1).replace("+00:00", ".000+00:00"), "source": "#client",
        "not_id": "ignored", "save": "new", "header": {"Location": r"/fhir/Patient/[0-9a-f-]{36}/_history/1$",
                                                       "ETag": r'^W/"1"$'}}),
    ("create-searchable", "GET", "Patient?family=Neu", None, {}, _t(1), {"status": 200, "match": ["Patient/$new"]}),
    ("read-created", "GET", "Patient/$new", None, {}, _t(1), {"status": 200, "version": "1"}),
    ("update", "PUT", "Patient/p1", P1_V2, {}, _t(2), {"status": 200, "version": "2", "family": "Schmidt",
                                                       "header": {"ETag": r'^W/"2"$'}}),
    ("update-old-value-gone", "GET", "Patient?family=M%C3%BCller", None, {}, _t(2), {"status": 200, "match": []}),
    ("update-new-value", "GET", "Patient?family=Schmidt", None, {}, _t(2), {"status": 200, "match": ["Patient/p1"]}),
    ("update-same-content", "PUT", "Patient/p1", P1_V2, {}, _t(3), {"status": 200, "version": "2"}),
    ("update-if-match-stale", "PUT", "Patient/p1", P1_V3, {"If-Match": 'W/"1"'}, _t(4), {"status": 412}),
    ("update-if-match-list", "PUT", "Patient/p1", P1_V3, {"If-Match": 'W/"1", W/"2"'}, _t(5),
     {"status": 200, "version": "3"}),
    ("update-id-mismatch", "PUT", "Patient/p1", {**P1_V3, "id": "p2"}, {}, _t(6), {"status": 400}),
    ("update-id-missing", "PUT", "Patient/p1", {k: v for k, v in P1_V3.items() if k != "id"}, {}, _t(6), {"status": 400}),
    ("update-as-create", "PUT", "Patient/pw", {"resourceType": "Patient", "id": "pw", "gender": "female"}, {}, _t(7),
     {"status": 201, "version": "1", "header": {"Location": r"/fhir/Patient/pw/_history/1$"}}),
    ("vread-past", "GET", "Patient/p1/_history/1", None, {}, _t(7), {"status": 200, "version": "1", "family": "Müller"}),
    ("vread-middle", "GET", "Patient/p1/_history/2", None, {}, _t(7), {"status": 200, "family": "Schmidt"}),
    ("vread-unknown", "GET", "Patient/p1/_history/9", None, {}, _t(7), {"status": 404}),
    ("delete", "DELETE", "Observation/o1", None, {}, _t(8), {"status": 204, "empty": True,
                                                             "header": {"ETag": r'^W/"2"$', "Last-Modified": "GMT"}}),
    ("read-deleted", "GET", "Observation/o1", None, {}, _t(8), {"status": 410}),
    ("vread-deletion", "GET", "Observation/o1/_history/2", None, {}, _t(8), {"status": 410}),
    ("vread-before-deletion", "GET", "Observation/o1/_history/1", None, {}, _t(8), {"status": 200, "version": "1"}),
    ("delete-unindexed", "GET", "Observation?code=8867-4", None, {}, _t(8),
     {"status": 200, "match": ["Observation/o2", "Observation/o6", "Observation/o11"]}),
    ("delete-again", "DELETE", "Observation/o1", None, {}, _t(9), {"status": 204, "empty": True}),
    ("delete-unknown", "DELETE", "Observation/never", None, {}, _t(9), {"status": 204, "empty": True}),
    ("history", "GET", "Patient/p1/_history", None, {}, _t(9), {"status": 200, "total": 3, "versions": ["3", "2", "1"],
                                                               "entries": [("PUT", True)] * 3}),
    ("history-deleted", "GET", "Observation/o1/_history", None, {}, _t(9), {"status": 200, "total": 2,
                                                                           "entries": [("DELETE", False), ("PUT", True)]}),
    ("history-since", "GET", "Patient/p1/_history?_since=2026-02-01T00:05:00Z", None, {}, _t(9),
     {"status": 200, "total": 1, "versions": ["3"]}),
    ("history-count", "GET", "Patient/p1/_history?_count=1", None, {}, _t(9),
     {"status": 200, "total": 3, "versions": ["3"], "all_pages": 3}),
    ("history-repeat", "GET", "Patient/p1/_history?_count=1&_count=2", None, {}, _t(9), {"status": 400}),
    ("history-type", "GET", "Patient/_history", None, {}, _t(9), {"status": 400}),
    ("history-created", "GET", "Patient/$new/_history", None, {}, _t(9), {"status": 200, "entries": [("POST", True)]}),
    ("last-updated-written", "GET", "Patient?_lastUpdated=gt2026-01-15", None, {}, _t(9),
     {"status": 200, "match": ["Patient/$new", "Patient/p1", "Patient/pw"]}),
    ("last-updated-loaded", "GET", "Patient?_lastUpdated=lt2026-01-15", None, {}, _t(9),
     {"status": 200, "match": ["Patient/p2", "Patient/p3"]}),
    ("sort-last-updated", "GET", "Patient?_sort=-_lastUpdated", None, {}, _t(9),
     {"status": 200, "match": ["Patient/pw", "Patient/p1", "Patient/$new", "Patient/p2", "Patient/p3"], "order": True}),
    ("recreate", "PUT", "Observation/o1", O1, {}, _t(10), {"status": 201, "version": "3"}),
    ("recreate-searchable", "GET", "Observation?code=8867-4", None, {}, _t(10),
     {"status": 200, "match": ["Observation/o1", "Observation/o2", "Observation/o6", "Observation/o11"]}),
    ("history-recreated", "GET", "Observation/o1/_history", None, {}, _t(10),
     {"status": 200, "total": 3, "versions": ["3", "2", "1"], "entries": [("PUT", True), ("DELETE", False), ("PUT", True)]}),
    ("chain-before", "GET", "Patient?organization.name=General", None, {}, _t(10), {"status": 200, "match": ["Patient/p2"]}),
    ("chain-target-update", "PUT", "Organization/org1", {"resourceType": "Organization", "id": "org1", "name": "City Clinic"},
     {}, _t(10), {"status": 200, "version": "2"}),
    ("chain-old-name", "GET", "Patient?organization.name=General", None, {}, _t(10), {"status": 200, "match": []}),
    ("chain-new-name", "GET", "Patient?organization.name=City", None, {}, _t(10), {"status": 200, "match": ["Patient/p2"]}),
    ("has-before", "GET", "Patient?_has:Observation:patient:code=2339-0", None, {}, _t(10),
     {"status": 200, "match": ["Patient/p1", "Patient/p2"]}),
    ("has-source-delete", "DELETE", "Observation/o3", None, {}, _t(10), {"status": 204}),
    ("has-after-delete", "GET", "Patient?_has:Observation:patient:code=2339-0", None, {}, _t(10),
     {"status": 200, "match": ["Patient/p2"]}),
    ("has-source-update", "PUT", "Observation/o4", O4_P3, {}, _t(10), {"status": 200, "version": "2"}),
    ("has-after-update", "GET", "Patient?_has:Observation:patient:code=2339-0", None, {}, _t(10),
     {"status": 200, "match": ["Patient/p3"]}),
    ("contained", "PUT", "MedicationRequest/mrw", CONTAINED, {}, _t(11), {"status": 201}),
    ("contained-searchable", "GET", "Medication?_contained=true&code=urn:w|w1", None, {}, _t(11),
     {"status": 200, "match": ["MedicationRequest/mrw"]}),     # _containedType defaults to container
    ("contained-itself", "GET", "Medication?_contained=true&_containedType=contained&code=urn:w|w1", None, {}, _t(11),
     {"status": 200, "total": 1}),
    ("new-type", "POST", "Device", {"resourceType": "Device", "status": "active"}, {}, _t(12), {"status": 201}),
    ("new-type-search", "GET", "Device?status=active", None, {}, _t(12), {"status": 200, "total": 1}),
    ("type-without-data", "GET", "Basic", None, {}, _t(12), {"status": 200, "total": 0}),
    ("create-wrong-type", "POST", "Patient", {"resourceType": "Observation", "status": "final"}, {}, _t(13), {"status": 400}),
    ("create-not-json", "POST", "Patient", "{", {}, _t(13), {"status": 400}),
    ("create-form", "POST", "Patient", "a=b", {"Content-Type": "application/x-www-form-urlencoded"}, _t(13), {"status": 415}),
    ("create-bad-date", "PUT", "Patient/pbad", {"resourceType": "Patient", "id": "pbad", "birthDate": "1990-13-45"}, {},
     _t(13), {"status": 400}),
    ("create-bad-date-not-stored", "GET", "Patient/pbad", None, {}, _t(13), {"status": 404}),
    ("prefer-minimal", "POST", "Patient", {"resourceType": "Patient"}, {"Prefer": "return=minimal"}, _t(14),
     {"status": 201, "empty_http": True, "header": {"Location": "/_history/1$"}}),
    ("prefer-outcome", "POST", "Patient", {"resourceType": "Patient"}, {"Prefer": "return=OperationOutcome"}, _t(14),
     {"status": 201, "outcome_http": True}),
    ("patch", "PATCH", "Patient/p1", "[]", {"Content-Type": "application/json-patch+json"}, _t(15), {"status": 405}),
    ("put-type", "PUT", "Patient", {"resourceType": "Patient"}, {}, _t(15), {"status": 405}),
    ("conditional-update", "PUT", "Patient?identifier=urn:mrn|111", P1_V3, {}, _t(15), {"status": 400}),
]


def _matches(fetch, first):
    status, body = first
    keys, page = [], body
    while True:
        keys += [f"{e['resource']['resourceType']}/{e['resource']['id']}" for e in page.get("entry", [])
                 if e.get("search", {}).get("mode") == "match"]
        nxt = [link["url"] for link in page.get("link", []) if link.get("relation") == "next"]
        if not nxt:
            return keys
        status, page = fetch(nxt[0])


def _history_all(fetch, first):
    status, body = first
    entries, page = [], body
    while True:
        entries += page.get("entry", [])
        nxt = [link["url"] for link in page.get("link", []) if link.get("relation") == "next"]
        if not nxt:
            return entries
        status, page = fetch(nxt[0])


def judge(expect, status, body, headers, fetch, saved, http):
    problems = []
    if status != expect["status"]:
        return [f"status {status}, expected {expect['status']}: {json.dumps(body)[:200] if body else ''}"]
    body = body or {}
    meta = body.get("meta") or {}
    for key, want in (("version", "versionId"), ("last_updated", "lastUpdated"), ("source", "source")):
        if key in expect and meta.get(want) != expect[key]:
            problems.append(f"meta.{want} {meta.get(want)!r} expected {expect[key]!r}")
    if "not_id" in expect and body.get("id") in (None, expect["not_id"]):
        problems.append(f"id {body.get('id')!r}: the server must assign it")
    if "family" in expect and ((body.get("name") or [{}])[0].get("family")) != expect["family"]:
        problems.append(f"family {body.get('name')}")
    if "match" in expect:
        want = [m.replace("$new", saved.get("new", "")) for m in expect["match"]]
        got = _matches(fetch, (status, body))
        if (got if expect.get("order") else sorted(got)) != (want if expect.get("order") else sorted(want)):
            problems.append(f"matches {got} expected {want}")
    if "total" in expect and body.get("total") != expect["total"]:
        problems.append(f"total {body.get('total')} expected {expect['total']}")
    entries = body.get("entry", [])
    if "versions" in expect:
        got = [e["response"]["etag"][3:-1] for e in entries]
        if got != expect["versions"]:
            problems.append(f"versions {got} expected {expect['versions']}")
    if "entries" in expect:
        got = [(e["request"]["method"], "resource" in e) for e in entries]
        if got != [tuple(x) for x in expect["entries"]]:
            problems.append(f"entries {got} expected {expect['entries']}")
        if any(not re.match(r"^\d{3}( |$)", e["response"]["status"]) or not e["response"].get("lastModified")
               for e in entries):
            problems.append("history response.status/lastModified")
    if "all_pages" in expect and len(_history_all(fetch, (status, body))) != expect["all_pages"]:
        problems.append("history paging did not return every version")
    if "patient_interactions" in expect:
        res = next(r for r in body["rest"][0]["resource"] if r["type"] == "Patient")
        if [i["code"] for i in res["interaction"]] != expect["patient_interactions"]:
            problems.append(f"interactions {res['interaction']}")
    if expect.get("empty") and body:
        problems.append("body present")
    if http:
        lowered = {k.lower(): v for k, v in headers.items()}
        for name, pattern in expect.get("header", {}).items():
            if not re.search(pattern, lowered.get(name.lower(), "")):
                problems.append(f"header {name}: {lowered.get(name.lower())!r} !~ {pattern}")
        if expect.get("empty_http") and body:
            problems.append("Prefer return=minimal returned a body")
        if expect.get("outcome_http") and body.get("resourceType") != "OperationOutcome":
            problems.append("Prefer return=OperationOutcome returned no OperationOutcome")
    if "save" in expect:
        saved[expect["save"]] = body.get("id", "")
    return problems


def run_steps(db, definitions_path, packages, request, http):
    """request(method, path, body, headers) -> (status, body, headers); fetch for later pages."""
    saved, results = {}, []
    for sid, method, path, body, headers, at, expect in STEPS:
        path = path.replace("$new", saved.get("new", ""))
        status, answer, answer_headers, fetch = request(method, path, body, headers, at)
        problems = judge(expect, status, answer, answer_headers, fetch, saved, http)
        results.append({"id": sid, "passed": not problems, "problems": problems})
    return results


# ---- equivalence -----------------------------------------------------------------------------
def snapshot(path, drop_version=False):
    """Resources and index rows with ids resolved: comparable across stores whatever their pids."""
    db = sqlite3.connect(path)
    strings = dict(db.execute("SELECT id, value FROM strings"))
    params = {pid: (t, c) for pid, t, c in db.execute("SELECT id, type, code FROM params")}
    keys = {pid: (strings[t], rid) for pid, t, rid in db.execute("SELECT pid, type_id, id FROM resources")}
    resources = {}
    for pid, blob, container in db.execute("SELECT pid, json, container FROM resources"):
        resource = json.loads(zlib.decompress(blob))
        if drop_version and "meta" in resource:
            resource["meta"] = {k: v for k, v in resource["meta"].items() if k != "versionId"}
        resources[keys[pid]] = (resource, keys.get(container))
    rows = Counter()
    for table in TABLES:
        cols = INTERNED.get(table, ())
        for row in db.execute(f"SELECT * FROM sp_{table}"):
            row = list(row)
            row[0] = params[row[0]]
            for c in cols:
                row[c] = strings.get(row[c]) if row[c] is not None else None
            row[-1] = keys[row[-1]]
            rows[(table, *row)] += 1
    fts = Counter()
    if db.execute("SELECT 1 FROM sqlite_master WHERE name = 'fts'").fetchone():
        fts = Counter((keys[r], c, n) for r, c, n in db.execute("SELECT rowid, content, narrative FROM fts"))
    db.close()
    return resources, rows, fts


def compare(a, b):
    (ra, ia, fa), (rb, ib, fb) = a, b
    problems = []
    for key in sorted(set(ra) | set(rb)):
        if ra.get(key) != rb.get(key):
            problems.append(f"resource {key} differs")
    extra, missing = ib - ia, ia - ib
    if extra or missing:
        problems.append(f"index rows: {sum(missing.values())} only in the first, {sum(extra.values())} only in the second; "
                        f"e.g. {list(missing)[:2]} / {list(extra)[:2]}")
    if fa != fb:
        problems.append("full-text rows differ")
    return problems[:20]


def equivalence(fixture_db, definitions_path, packages, work):
    """(1) PUT every fixture resource into an empty store -> equals the loaded fixture.
    (2) update, delete and create on a fixture copy -> indexes like a fresh load of the result."""
    load_instant = SETTINGS["last_updated"]
    report = {}
    empty = work / "empty.sqlite"
    load([], definitions_path, empty, packages=packages, fulltext=True, hash_sources=False, settings=dict(SETTINGS))
    with Engine(empty, writable=True, definitions=definitions_path, packages=packages,
                clock=Clock(load_instant)) as engine:
        failed = [(r["resourceType"], r["id"], s) for r in FIXTURE
                  for s in [engine.put(f"{r['resourceType']}/{r['id']}", copy.deepcopy(r))[0]] if s != 201]
    report["put_equals_load"] = ([f"PUT failed: {failed[:5]}"] if failed else []) + compare(snapshot(fixture_db), snapshot(empty))

    changed = work / "changed.sqlite"
    shutil.copy(fixture_db, changed)
    device = {"resourceType": "Device", "id": "dw", "status": "active"}
    with Engine(changed, writable=True, definitions=definitions_path, packages=packages,
                clock=Clock(load_instant)) as engine:
        statuses = [engine.put("Patient/p1", copy.deepcopy(P1_V2))[0], engine.delete("Observation/o1")[0],
                    engine.put("Device/dw", copy.deepcopy(device))[0]]
    resulting = [P1_V2 if (r["resourceType"], r["id"]) == ("Patient", "p1") else r for r in FIXTURE
                 if (r["resourceType"], r["id"]) != ("Observation", "o1")] + [device]
    (work / "after").mkdir()
    (work / "after" / "after.ndjson").write_text("".join(json.dumps(r) + "\n" for r in resulting), encoding="utf-8")
    fresh = work / "fresh.sqlite"
    load(work / "after", definitions_path, fresh, packages=packages, fulltext=True, hash_sources=False,
         settings=dict(SETTINGS))
    report["writes_equal_fresh_load"] = ([f"write statuses {statuses}"] if statuses != [200, 204, 201] else []) + \
        compare(snapshot(fresh, drop_version=True), snapshot(changed, drop_version=True))
    report["failed_write_rolls_back"] = rollback(changed, definitions_path, packages)
    report["if_match_race_one_winner"] = if_match_race(fixture_db, definitions_path, packages, work)
    return report


def _counts(path):
    db = sqlite3.connect(path)
    out = [db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("strings", "versions", "written", "resources")]
    db.close()
    return out


def rollback(path, definitions_path, packages):
    """An update that fails after the old version was moved and its index rows deleted (a value
    that cannot be indexed) leaves the store as it was."""
    before, counts = snapshot(path), _counts(path)
    p2 = next(r for r in FIXTURE if r["resourceType"] == "Patient" and r["id"] == "p2")
    bad = {**copy.deepcopy(p2), "birthDate": "1990-13-45", "name": [{"family": "Rollback"}]}
    with Engine(path, writable=True, definitions=definitions_path, packages=packages) as engine:
        status = engine.put("Patient/p2", bad)[0]
        read = engine.get("Patient/p2")[1]
    problems = [] if status == 400 else [f"status {status}, expected 400"]
    if read.get("name", [{}])[0].get("family") != "Mueller" or read.get("meta", {}).get("versionId") != "1":
        problems.append(f"Patient/p2 after the failed update: {read.get('name')} version {read.get('meta')}")
    if _counts(path) != counts:
        problems.append(f"rows (strings, versions, written, resources) {counts} -> {_counts(path)}")
    return problems + compare(before, snapshot(path))


def if_match_race(fixture_db, definitions_path, packages, work, rounds=20):
    """Two connections read the same version and update it at once with If-Match: one wins (200),
    the other gets 412, in every round."""
    path = work / "race.sqlite"
    shutil.copy(fixture_db, path)
    p3 = next(r for r in FIXTURE if r["resourceType"] == "Patient" and r["id"] == "p3")
    engines = [Engine(path, writable=True, definitions=definitions_path, packages=packages) for _ in range(2)]
    problems = []
    try:
        for n in range(rounds):
            version = engines[0].get("Patient/p3")[1]["meta"]["versionId"]
            barrier, statuses = threading.Barrier(2), [None, None]

            def write(k):
                body = {**copy.deepcopy(p3), "name": [{"family": f"Race{n}", "given": [str(k)]}]}
                barrier.wait()
                statuses[k] = engines[k].put("Patient/p3", body, if_match=f'W/"{version}"')[0]

            threads = [threading.Thread(target=write, args=(k,)) for k in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            if sorted(statuses) != [200, 412]:
                problems.append(f"round {n}: statuses {statuses}")
        total = engines[0].get("Patient/p3/_history?_count=1")[1].get("total")
        if total != rounds + 1:
            problems.append(f"history total {total}, expected {rounds + 1}")
    finally:
        for e in engines:
            e.close()
    return problems
