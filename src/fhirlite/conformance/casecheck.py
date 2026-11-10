"""Run the specification cases (cases.py) in process and over HTTP and report contract coverage."""
from __future__ import annotations

import io
import json
import shutil
import socket
import tarfile
import tempfile
import threading
import urllib.error
import urllib.request
from collections import defaultdict
from pathlib import Path

from .cases import CASES, DECLARED, FIXTURE, PACKAGE, SETTINGS
from ..search.engine import Engine
from ..server import serve
from ..store.load import load



def build_fixture(definitions_path, workdir):
    work = Path(workdir)
    (work / "data").mkdir(parents=True, exist_ok=True)
    (work / "data" / "fixture.ndjson").write_text("".join(json.dumps(r) + "\n" for r in FIXTURE), encoding="utf-8")
    package = work / "fixture-package.tgz"
    with tarfile.open(package, "w:gz") as archive:
        for name, obj in [("package/package.json", {"name": "fhirlite.cases", "version": "1.0.0"})] + \
                         [(f"package/ValueSet-{v['id']}.json", v) for v in PACKAGE]:
            data = json.dumps(obj).encode("utf-8")
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    db = work / "fixture.sqlite"
    load(work / "data", definitions_path, db, packages=[package], fulltext=True, hash_sources=False,
         settings={k: v for k, v in SETTINGS.items()})
    return db


def _bundle_pages(fetch, first):
    """All pages of a search: (status, [match keys], [include keys], first page body)."""
    status, body = first
    if status != 200 or body.get("resourceType") != "Bundle":
        return status, None, None, body
    matches, includes, page = [], [], body
    while True:
        for e in page.get("entry", []):
            key = f"{e['resource']['resourceType']}/{e['resource'].get('id')}"
            mode = e.get("search", {}).get("mode")
            if mode == "match":
                matches.append(key)
            elif mode == "include":
                includes.append(key)
        nxt = [link["url"] for link in page.get("link", []) if link.get("relation") == "next"]
        if not nxt:
            break
        status, page = fetch(nxt[0])
        if status != 200:
            return status, None, None, body
        # a later page's self link is that page (R4 paging)
        own = [link["url"] for link in page.get("link", []) if link.get("relation") == "self"]
        body.setdefault("__page_self", []).append(own == [nxt[0]])
    return 200, matches, includes, body


def _judge(expect, status, matches, includes, body, headers=None, raw_body=b""):
    problems = []
    if status != expect["status"]:
        return [f"status {status}, expected {expect['status']}: "
                f"{json.dumps(body.get('issue', body.get('resourceType')))[:160] if isinstance(body, dict) else ''}"]
    if status != 200:
        if isinstance(body, dict) and body and body.get("resourceType") != "OperationOutcome":
            problems.append("error answer is not an OperationOutcome")
        return problems
    if "read" in expect and f"{body.get('resourceType')}/{body.get('id')}" != expect["read"]:
        problems.append(f"read returned {body.get('resourceType')}/{body.get('id')}")
    if "resource_type" in expect and body.get("resourceType") != expect["resource_type"]:
        problems.append(f"resourceType {body.get('resourceType')}")
    if "match" in expect:
        got, want = (matches, expect["match"]) if expect.get("order") else (sorted(matches or []), sorted(expect["match"]))
        if got != want:
            problems.append(f"matches {got} expected {want}")
    if expect.get("include_unique") and len(includes or []) != len(set(includes or [])):
        problems.append(f"included resources repeated: {includes}")
    if "include" in expect and sorted(set(includes or [])) != sorted(expect["include"]):
        problems.append(f"includes {sorted(set(includes or []))} expected {sorted(expect['include'])}")
    if "total" in expect and body.get("total") != expect["total"]:
        problems.append(f"total {body.get('total')} expected {expect['total']}")
    if expect.get("no_total") and "total" in body:
        problems.append("total present")
    if "first_page" in expect:
        n = sum(1 for e in body.get("entry", []) if e.get("search", {}).get("mode") == "match")
        if n != expect["first_page"]:
            problems.append(f"first page {n} entries expected {expect['first_page']}")
    if "next" in expect and any(l.get("relation") == "next" for l in body.get("link", [])) != expect["next"]:
        problems.append(f"next link presence != {expect['next']}")
    first = next((e["resource"] for e in body.get("entry", []) if e.get("search", {}).get("mode") == "match"), None)
    if "keys" in expect and (first is None or sorted(first) != sorted(expect["keys"])):
        problems.append(f"elements {sorted(first or [])} expected {sorted(expect['keys'])}")
    if "subsetted" in expect:
        tagged = any(t.get("code") == "SUBSETTED" for t in ((first or {}).get("meta") or {}).get("tag", []))
        if tagged != expect["subsetted"]:
            problems.append(f"SUBSETTED tag {tagged} expected {expect['subsetted']}")
    if expect.get("outcome") and not any(e.get("search", {}).get("mode") == "outcome" for e in body.get("entry", [])):
        problems.append("no outcome entry")
    if expect.get("empty_body") and raw_body:
        problems.append("HEAD returned a body")
    if "header" in expect and not (headers or {}).get(expect["header"]):
        problems.append(f"missing header {expect['header']}")
    lowered = {k.lower(): v for k, v in (headers or {}).items()}
    for name, value in expect.get("header_equals", {}).items():
        if lowered.get(name.lower()) != value:
            problems.append(f"header {name}: {lowered.get(name.lower())!r} expected {value!r}")
    if "self" in expect:
        own = [l["url"] for l in body.get("link", []) if l.get("relation") == "self"]
        got = own[0].split("/fhir", 1)[1] if own and "/fhir" in own[0] else own
        if got != expect["self"]:
            problems.append(f"self link {got!r} expected {expect['self']!r}")
    if expect.get("page_self") and (not body.get("__page_self") or not all(body["__page_self"])):
        problems.append(f"later page self links: {body.get('__page_self')}")
    if "read_subsetted" in expect:
        tagged = any(t.get("code") == "SUBSETTED" for t in (body.get("meta") or {}).get("tag", []))
        if tagged != expect["read_subsetted"]:
            problems.append(f"read SUBSETTED {tagged} expected {expect['read_subsetted']}")
    if "read_keys" in expect and sorted(body) != sorted(expect["read_keys"]):
        problems.append(f"read elements {sorted(body)} expected {sorted(expect['read_keys'])}")
    return problems


def run_cases(definitions_path, output=None):
    work = Path(tempfile.mkdtemp(prefix="fhirlite-cases-"))
    db = build_fixture(definitions_path, work)
    engine = Engine(db)
    server, base = serve(db, port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]

    def http_get(url, method="GET", headers=None, body=None):
        request = urllib.request.Request(url, method=method, headers=headers or {},
                                         data=body.encode("utf-8") if body is not None else None)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                raw = response.read()
                return response.status, dict(response.headers), raw
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read()

    def http_fetch(url):
        status, _, raw = http_get(url)
        return status, json.loads(raw) if raw else {}

    results, coverage = [], defaultdict(lambda: {"cases": 0, "passed": 0})
    for c in CASES:
        expect, outcome = c["expect"], {}
        if "path" in c:
            handling = expect.get("handling", "strict")
            # in process
            first = engine.get("/" + c["path"], handling)
            st, m, inc, body = _bundle_pages(lambda u: engine.get(u[len(engine.base):], handling), first)
            outcome["in_process"] = _judge(expect, st, m, inc, body)
            # over HTTP (Prefer carries the handling)
            headers = {"Prefer": f"handling={handling}"}
            st0, _, raw = http_get(f"{base}/{c['path']}", headers=headers)
            st, m, inc, body = _bundle_pages(http_fetch, (st0, json.loads(raw) if raw else {}))
            outcome["http"] = _judge(expect, st, m, inc, body)
        else:
            h = c["http"]
            if h["raw"]:
                request = (f"{h['method']} /fhir{h['path']} HTTP/1.1\r\nHost: x\r\nContent-Type: "
                           f"application/x-www-form-urlencoded\r\n{h['raw']}Connection: close\r\n\r\nab").encode("utf-8")
                with socket.create_connection(("127.0.0.1", port), timeout=30) as sock:
                    sock.sendall(request)
                    data = b""
                    try:
                        while chunk := sock.recv(65536):
                            data += chunk
                    except socket.timeout:
                        pass
                status = int(data.split(b" ", 2)[1]) if data.startswith(b"HTTP/") else 0
                body = json.loads(data.split(b"\r\n\r\n", 1)[1] or b"{}") if b"\r\n\r\n" in data else {}
                outcome["http"] = _judge(expect, status, None, None, body)
            else:
                st0, headers, raw = http_get(base + h["path"], h["method"], h["headers"], h["body"])
                body = json.loads(raw) if raw else {}
                st, m, inc, body2 = _bundle_pages(http_fetch, (st0, body)) if h["method"] != "HEAD" else (st0, None, None, body)
                outcome["http"] = _judge(expect, st, m, inc, body2 if body2 is not None else body, headers, raw)
        passed = all(not v for v in outcome.values())
        results.append({"id": c["id"], "keys": c["keys"], "passed": passed,
                        "problems": {k: v for k, v in outcome.items() if v}})
        for key in c["keys"]:
            coverage[key]["cases"] += 1
            coverage[key]["passed"] += passed
    server.shutdown()
    writes = run_write_cases(db, definitions_path, work, http_get)
    uncovered = [k for k in DECLARED if coverage[k]["passed"] == 0]
    undeclared = sorted(k for k in coverage if k not in DECLARED)
    failed = [r for r in results if not r["passed"]]
    report = {"cases": len(results), "passed": len(results) - len(failed), "failed": len(failed),
              "declared_combinations": len(DECLARED), "combinations_with_passing_case": len(DECLARED) - len(uncovered),
              "uncovered_or_failing_combinations": uncovered, "undeclared_keys": undeclared,
              "failures": failed, "coverage": {k: coverage[k] for k in DECLARED}, **writes}
    if output:
        Path(output).write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("cases", "passed", "failed", "declared_combinations",
                                             "combinations_with_passing_case", "uncovered_or_failing_combinations",
                                             "undeclared_keys", "write_steps", "write_failed")}, ensure_ascii=False))
    print(json.dumps({"equivalence": {k: "ok" if not v else v for k, v in report["equivalence"].items()}},
                     ensure_ascii=False))
    for r in report["write_failures"]:
        print(f"FAIL write {r['id']} ({r['mode']}): {json.dumps(r['problems'], ensure_ascii=False)[:400]}")
    for r in failed:
        print(f"FAIL {r['id']}: {json.dumps(r['problems'], ensure_ascii=False)[:400]}")
    return report


def run_write_cases(fixture_db, definitions_path, work, http_get):
    """The write steps on two writable copies of the fixture (in process and over HTTP), and the
    load-versus-write equivalence checks."""
    from .writecases import STEPS, Clock, equivalence, run_steps
    package = [work / "fixture-package.tgz"]
    failures = []

    local_db = work / "write-local.sqlite"
    shutil.copy(fixture_db, local_db)
    clock = Clock("2026-02-01T00:00:00+00:00")
    with Engine(local_db, writable=True, definitions=definitions_path, packages=package, clock=clock) as engine:
        def local(method, path, body, headers, at):
            clock.set(at)
            content = headers.get("Content-Type", "application/fhir+json")
            data = body if body is None or isinstance(body, dict) else body.encode("utf-8")
            if method == "GET":
                status, answer = engine.get(path)
            elif method == "POST":
                status, answer = engine.post(path, data, content)
            elif method == "PUT":
                status, answer = engine.put(path, data, content, headers.get("If-Match"))
            elif method == "DELETE":
                status, answer = engine.delete(path, headers.get("If-Match"))
            else:
                status, answer = 405, None
            return status, answer, {}, lambda url: engine.get(url[len(engine.base):])
        failures += [{**r, "mode": "in_process"} for r in run_steps(local_db, definitions_path, package, local, False)
                     if not r["passed"]]

    http_db = work / "write-http.sqlite"
    shutil.copy(fixture_db, http_db)
    http_clock = Clock("2026-02-01T00:00:00+00:00")
    server, base = serve(http_db, port=0, writable=True, definitions=definitions_path, packages=package, clock=http_clock)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def fetch(url):
        status, _, raw = http_get(url)
        return status, json.loads(raw) if raw else {}

    def remote(method, path, body, headers, at):
        http_clock.set(at)
        data = json.dumps(body) if isinstance(body, dict) else body
        sent = {"Content-Type": "application/fhir+json", **headers} if data is not None else dict(headers)
        status, answer_headers, raw = http_get(f"{base}/{path}", method, sent, data)
        return status, json.loads(raw) if raw else {}, answer_headers, fetch
    failures += [{**r, "mode": "http"} for r in run_steps(http_db, definitions_path, package, remote, True)
                 if not r["passed"]]
    server.shutdown()
    server.engine.close()

    checks = equivalence(fixture_db, definitions_path, package, work)
    return {"write_steps": len(STEPS), "write_failed": len(failures), "write_failures": failures,
            "equivalence": checks}
