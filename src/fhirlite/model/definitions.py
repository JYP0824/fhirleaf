"""FHIR definitions: core resources and SearchParameters, implementation-guide packages, ValueSets."""
from __future__ import annotations

import hashlib
import json
import os
import re
import zipfile

from .fhirpath import Model
from ..inputs import package_resources


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
