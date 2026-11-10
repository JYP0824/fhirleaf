"""The CapabilityStatement and derived metadata a store answers with."""
from __future__ import annotations

from .schema import FORMAT


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
    note = (f"fhirlite embedded FHIR RESTful Search. Date/time search values without an offset are read in zone "
            f"{settings['timezone']}; default page size {settings['default_count']}, maximum {settings['max_count']}; "
            f"results without _sort are in stable store order; near without a distance uses "
            f"{settings['near_default_km']} km; phonetic parameters match name parts, letters by American Soundex and digits exactly; "
            f"token codes and identifier values compare case-insensitively (ASCII); date 'ap' is 10% of the distance "
            + ("from the current time. Writes do not check that references resolve and do not validate resources "
               "against profiles. An update with unchanged content creates no new version." if writable
               else f"from the load instant {settings['last_updated']}."))
    return {"resourceType": "CapabilityStatement", "status": "active", "kind": "instance",
            "fhirVersion": definitions.fhir_version, "date": settings["last_updated"],
            "format": ["application/fhir+json"], "software": {"name": "fhirlite", "version": FORMAT},
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
