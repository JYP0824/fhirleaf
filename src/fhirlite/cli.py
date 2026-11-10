"""Command line: load | get | serve | verify | support | corpus | record | compare."""
from __future__ import annotations

import argparse
import json
import sys

from .conformance import corpus
from .search.engine import Engine
from .server import serve
from .store.load import load



def main(argv=None):
    p = argparse.ArgumentParser(prog="fhirlite", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    a = sub.add_parser("load", help="FHIR resources -> fresh store")
    a.add_argument("--input", "--fhir-dir", dest="inputs", action="append", default=[],
                   help="File or directory of *.ndjson[.gz] / *.json[.gz] (Bundles or resources); repeatable")
    a.add_argument("--package", action="append", default=[], help="FHIR package .tgz (e.g. an implementation guide); repeatable")
    a.add_argument("--fulltext", action="store_true", help="Build the full-text index for _content/_text")
    a.add_argument("--definitions", required=True, help="HL7 FHIR R4 definitions.json.zip")
    a.add_argument("--output", required=True)
    a.add_argument("--timezone", default="UTC",
                   help="Server zone for date/time values without an offset: UTC, +HH:MM or a POSIX TZ rule "
                        "(MIMIC-IV-on-FHIR records use EST5EDT,M3.2.0,M11.1.0)")
    a.add_argument("--base-url", action="append", dest="local_bases",
                   help="This server's base URL; absolute references on it count as local (default http://localhost/fhir)")
    a.add_argument("--default-count", type=int, default=100)
    a.add_argument("--max-count", type=int, default=1000)
    a.add_argument("--last-updated", help="meta.lastUpdated stamped on every resource (default: load time)")
    a.add_argument("--no-hash", action="store_true", help="Skip source file SHA-256 (large inputs)")
    a.add_argument("--workers", type=int, default=1,
                   help="Processes that parse and extract in parallel (the store is the same for any number)")
    g = sub.add_parser("get", help="One request against a store, JSON to stdout")
    g.add_argument("--db", required=True)
    g.add_argument("--base-url", default="http://localhost/fhir")
    g.add_argument("--lenient", action="store_true", help="Prefer: handling=lenient (ignore unknown parameters)")
    g.add_argument("request")
    s = sub.add_parser("serve", help="Serve a store over HTTP at /fhir")
    s.add_argument("--db", required=True)
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8080)
    s.add_argument("--public-base", help="Base URL clients see (e.g. behind a proxy); default http://host:port/fhir")
    s.add_argument("--default-count", type=int, help="Page size when _count is absent (overrides the store)")
    s.add_argument("--max-count", type=int, help="Largest page size served (overrides the store)")
    s.add_argument("--cors-origin", help="Send CORS headers for this origin (e.g. http://localhost:3000 or *); off by default")
    s.add_argument("--writable", action="store_true", help="Also create, update and delete (needs --definitions)")
    s.add_argument("--definitions", help="The definitions.json.zip the store was built with (for --writable)")
    s.add_argument("--package", action="append", default=[], help="Packages the store was built with (for --writable)")
    s.add_argument("--handling", choices=("strict", "lenient"), default="strict",
                   help="Unknown-parameter handling for requests without a Prefer header")
    c = sub.add_parser("corpus", help="Derive conformance requests from a store")
    c.add_argument("--db", required=True)
    c.add_argument("--output", required=True)
    c.add_argument("--seed", type=int, default=42)
    c.add_argument("--patients", type=int, default=5)
    c.add_argument("--values-per-param", type=int, default=3)
    r = sub.add_parser("record", help="Record another FHIR server's responses for a request file")
    r.add_argument("--base-url", required=True)
    r.add_argument("--requests", required=True)
    r.add_argument("--output", required=True)
    r.add_argument("--max-pages", type=int, default=20)
    m = sub.add_parser("compare", help="Replay recorded requests in the engine and classify differences")
    m.add_argument("--db", required=True)
    m.add_argument("--recorded", required=True)
    m.add_argument("--report", required=True)
    v = sub.add_parser("verify", help="--cases: the specification cases for the declared contract; otherwise "
                                      "brute-force FHIR matching on a patient subset of --input")
    v.add_argument("--cases", action="store_true", help="Run the built-in specification cases (no data needed)")
    v.add_argument("--input", "--fhir-dir", dest="inputs", action="append")
    v.add_argument("--package", action="append", default=[])
    v.add_argument("--fulltext", action="store_true")
    v.add_argument("--definitions", required=True)
    v.add_argument("--output")
    v.add_argument("--patients", type=int, default=5)
    v.add_argument("--seed", type=int, default=42)
    v.add_argument("--timezone", default="UTC")
    u = sub.add_parser("support", help="Support status of every SearchParameter in the definitions (and packages)")
    u.add_argument("--definitions", required=True)
    u.add_argument("--package", action="append", default=[])
    u.add_argument("--output", help="Full per-parameter table (JSON); the summary goes to stdout")
    args = p.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):        # JSON output is UTF-8 on every console
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    if args.command == "support":
        from .conformance.support import write_support
        print(json.dumps(write_support(args.definitions, args.package, args.output), ensure_ascii=False, indent=1))
        return
    if args.command == "verify" and args.cases:
        from .conformance.casecheck import run_cases
        report = run_cases(args.definitions, args.output)
        ok = not (report["failed"] or report["uncovered_or_failing_combinations"] or report["write_failed"]
                  or any(report["equivalence"].values()))
        raise SystemExit(0 if ok else 1)
    if args.command == "verify":
        if not args.inputs or not args.output:
            p.error("verify needs --input and --output (or --cases)")
        from .conformance.oracle import verify
        verify(args.inputs, args.definitions, args.output, packages=args.package, fulltext=args.fulltext,
               patients=args.patients, seed=args.seed,
               settings={"timezone": args.timezone, "last_updated": "2026-01-01T00:00:00.000+00:00"})
        return
    if args.command == "load":
        settings = {"timezone": args.timezone, "default_count": args.default_count, "max_count": args.max_count}
        if args.local_bases:
            settings["local_bases"] = args.local_bases
        if args.last_updated:
            settings["last_updated"] = args.last_updated
        load(args.inputs, args.definitions, args.output, packages=args.package, settings=settings,
             fulltext=args.fulltext, hash_sources=not args.no_hash, workers=args.workers)
    elif args.command == "get":
        status, body = Engine(args.db, base_url=args.base_url).get(args.request, "lenient" if args.lenient else "strict")
        json.dump({"status": status, "body": body}, sys.stdout, ensure_ascii=False, indent=1)
        print()
    elif args.command == "serve":
        server, url = serve(args.db, args.host, args.port, args.public_base, args.handling,
                            args.default_count, args.max_count, cors_origin=args.cors_origin, writable=args.writable,
                            definitions=args.definitions, packages=args.package)
        print(f"FHIR_BASE_URL={url}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            if server.engine:
                server.engine.close()
    elif args.command == "corpus":
        corpus.make_corpus(args.db, args.output, seed=args.seed, patients=args.patients,
                                values_per_param=args.values_per_param)
    elif args.command == "record":
        corpus.record(args.base_url, args.requests, args.output, max_pages=args.max_pages)
    elif args.command == "compare":
        corpus.compare(args.db, args.recorded, args.report)


if __name__ == "__main__":
    main()
