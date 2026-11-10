"""Support status of every SearchParameter in a definitions package (plus IG packages): what the
engine indexes, what it withdraws and why, and which declared flags it enforces."""
from __future__ import annotations

import json
import re
from collections import Counter

from ..model.definitions import Definitions
from ..store.extract import Extractor


# Modifiers the engine accepts per parameter type (a reference also takes a resource type, :Type).
MODIFIERS = {
    "token": ["missing", "not", "text", "in", "not-in", "of-type"],
    "string": ["missing", "exact", "contains"],
    "reference": ["missing", "Type", "identifier", "below", "above"],
    "date": ["missing"], "number": ["missing"], "quantity": ["missing"],
    "uri": ["missing", "below", "above"], "composite": [], "special": ["missing"],
}
PREFIXES = ["eq", "ne", "gt", "lt", "ge", "le", "sa", "eb", "ap"]
RESOLVE_NAVIGATION = re.compile(r"resolve\(\)\s*(\.|\)\s*\.|as\b)")
ENGINE_PARAMS = {"_id": "engine (resource id)", "_lastUpdated": "engine (store load instant)",
                 "_content": "full-text index (load --fulltext)", "_text": "full-text index (load --fulltext)",
                 "_has": "engine (reverse chaining)", "_list": "engine (List.item)", "_type": "engine (whole-system search)",
                 "_query": "excluded: named queries are server-defined and this server defines none"}


def support_table(definitions_path, packages=()):
    definitions = Definitions(definitions_path, packages)
    params = definitions.params_for(sorted(definitions.model.resource_types))
    for i, p in enumerate(params, 1):
        p["id"] = i
    extractor = Extractor(definitions.model, params, lambda s: s, None, [])
    withdrawn = {(t, c): why for t, c, why in extractor.unsupported}
    rows = []
    for p in params:
        key = (p["type"], p["code"])
        expression = p.get("expression") or ""
        if p["code"] in ENGINE_PARAMS:
            status, note = ("excluded" if p["code"] == "_query" else "supported"), ENGINE_PARAMS[p["code"]]
        elif key in withdrawn:
            status, note = "unsupported", withdrawn[key]
        elif p["ptype"] == "special" and p["code"] != "near":
            status, note = "unsupported", "special parameter without a defined algorithm"
        elif RESOLVE_NAVIGATION.search(expression):
            status, note = "conditional", "navigates through resolve(): withdrawn at load when a resource needs it"
        else:
            status, note = "supported", ("American Soundex" if p["usage"] == "phonetic" else "")
        rows.append({
            "type": p["type"], "code": p["code"], "param_type": p["ptype"], "url": p["url"], "status": status,
            "note": note, "expression": expression, "usage": p["usage"],
            "resolve_type_test_only": "resolve()" in expression and not RESOLVE_NAVIGATION.search(expression),
            "modifiers": MODIFIERS.get(p["ptype"], []),
            "declared_modifiers": p.get("modifiers") or [],
            "prefixes": PREFIXES if p["ptype"] in ("date", "number", "quantity") else [],
            "declared_comparators": p.get("comparators") or [],
            "multiple_or": p.get("multiple_or"), "multiple_and": p.get("multiple_and"),
            "components": [k for k, _ in p.get("components", [])],
        })
    gaps = [r for r in rows if r["declared_comparators"] and set(r["declared_comparators"]) - set(r["prefixes"])]
    gaps += [r for r in rows if r["declared_modifiers"] and set(r["declared_modifiers"]) - set(r["modifiers"])]
    summary = {
        "fhir_version": definitions.fhir_version, "packages": definitions.packages,
        "definitions": len({p["url"] for p in params}), "type_parameter_pairs": len(rows),
        "status": dict(Counter(r["status"] for r in rows)),
        "by_param_type": {t: dict(Counter(r["status"] for r in rows if r["param_type"] == t))
                          for t in sorted({r["param_type"] for r in rows})},
        "declared_flag_gaps": [f"{r['type']}.{r['code']}" for r in gaps],
        # one line per (code, reason), with the resource types it applies to
        "not_supported": [{"code": code, "status": status, "note": note, "types": sorted(types)}
                          for (code, status, note), types in sorted(_group(rows).items())],
    }
    return {"summary": summary, "parameters": rows}


def _group(rows):
    out = {}
    for r in rows:
        if r["status"] != "supported":
            out.setdefault((r["code"], r["status"], r["note"]), set()).add(r["type"])
    return out


def write_support(definitions_path, packages, output):
    table = support_table(definitions_path, packages)
    if output:
        with open(output, "w", encoding="utf-8") as f:
            json.dump(table, f, ensure_ascii=False, indent=1)
    return table["summary"]
