"""Input resources: NDJSON (bulk data), Bundle JSON, single-resource JSON, FHIR packages.

Bundles of type transaction, batch, collection, searchset and history are unpacked
the way a server would store their entries: `urn:uuid:`/`urn:oid:` fullUrls become
`Type/id` and references to them are rewritten; conditional references of the
form `Type?identifier=system|value` are resolved after all inputs are read.
Document and message Bundles are stored as Bundle resources.
"""
from __future__ import annotations

import gzip
import json
import re
import tarfile
from pathlib import Path

SUFFIXES = (".ndjson", ".ndjson.gz", ".json", ".json.gz")
UNPACKED = {"transaction", "batch", "collection", "searchset", "history", "transaction-response", "batch-response"}
CONDITIONAL = re.compile(r"^([A-Za-z]+)\?identifier=([^|&]*)\|([^&]+)$")


def input_files(paths):
    files = []
    for path in map(Path, paths):
        if path.is_dir():
            files += sorted(p for p in path.rglob("*") if p.is_file() and p.name.endswith(SUFFIXES))
        elif path.is_file():
            files.append(path)
        else:
            raise ValueError(f"Input not found: {path}")
    if not files:
        raise ValueError("No FHIR input files (*.ndjson[.gz], *.json[.gz])")
    return files


def _open(path):
    return gzip.open(path, "rt", encoding="utf-8") if path.name.endswith(".gz") else open(path, encoding="utf-8")


def read_lines(path):
    """Non-empty lines of an NDJSON file, unparsed."""
    with _open(path) as handle:
        for line in handle:
            if line.strip():
                yield line


def read_file(path):
    """Resources of one input file, Bundles unpacked."""
    if path.name.endswith((".ndjson", ".ndjson.gz")):
        with _open(path) as handle:
            for line in handle:
                if line.strip():
                    yield from unpack(json.loads(line))
    else:
        with _open(path) as handle:
            yield from unpack(json.load(handle))


def unpack(resource):
    if not isinstance(resource, dict) or "resourceType" not in resource:
        raise ValueError("Input is not a FHIR resource")
    if resource["resourceType"] != "Bundle" or resource.get("type") not in UNPACKED:
        yield resource
        return
    entries = [e for e in resource.get("entry", []) if isinstance(e.get("resource"), dict)]
    local = {}
    for e in entries:
        r, full = e["resource"], e.get("fullUrl") or ""
        if "id" not in r and full.startswith(("urn:uuid:", "urn:oid:")):
            r["id"] = full.rsplit(":", 1)[1]
        if full and "id" in r:
            local[full] = f"{r['resourceType']}/{r['id']}"
    for e in entries:
        r = e["resource"]
        if local:
            _rewrite(r, local)
        yield from unpack(r)


def _rewrite(node, mapping):
    if isinstance(node, dict):
        ref = node.get("reference")
        if isinstance(ref, str) and ref in mapping:
            node["reference"] = mapping[ref]
        for value in node.values():
            if isinstance(value, (dict, list)):
                _rewrite(value, mapping)
    elif isinstance(node, list):
        for value in node:
            _rewrite(value, mapping)


def conditional_references(node, found):
    """Collect `Type?identifier=system|value` reference strings."""
    if isinstance(node, dict):
        ref = node.get("reference")
        if isinstance(ref, str) and "?" in ref:
            found.add(ref)
        for value in node.values():
            if isinstance(value, (dict, list)):
                conditional_references(value, found)
    elif isinstance(node, list):
        for value in node:
            conditional_references(value, found)
    return found


def resolve_conditional(node, mapping):
    """Rewrite resolvable conditional references in place; returns the unresolved ones."""
    unresolved = set()
    if isinstance(node, dict):
        ref = node.get("reference")
        if isinstance(ref, str) and "?" in ref:
            if ref in mapping:
                node["reference"] = mapping[ref]
            else:
                unresolved.add(ref)
        for value in node.values():
            if isinstance(value, (dict, list)):
                unresolved |= resolve_conditional(value, mapping)
    elif isinstance(node, list):
        for value in node:
            unresolved |= resolve_conditional(value, mapping)
    return unresolved


def conditional_key(resource):
    """`Type?identifier=system|value` keys a resource answers."""
    return [f"{resource['resourceType']}?identifier={i.get('system', '')}|{i['value']}"
            for i in resource.get("identifier", []) if isinstance(i, dict) and i.get("value") is not None]


def package_resources(path):
    """Conformance resources of an NPM FHIR package (.tgz), e.g. an implementation guide."""
    out = []
    with tarfile.open(path, "r:gz") as archive:
        for member in archive.getmembers():
            if member.isfile() and member.name.startswith("package/") and member.name.endswith(".json") \
                    and member.name.count("/") == 1 and not member.name.endswith(("package.json", ".index.json")):
                try:
                    resource = json.load(archive.extractfile(member))
                except ValueError:
                    continue
                if isinstance(resource, dict) and resource.get("resourceType"):
                    out.append(resource)
    return out
