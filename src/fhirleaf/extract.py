"""Search index rows of one resource, from the compiled SearchParameter expressions."""
from __future__ import annotations

import math
import re
from collections import defaultdict

from .fhirpath import ENV, QUANTITY_PROFILES, UnsupportedPath, compile_expression
from .values import HIGH_OPEN, LOW_OPEN, date_range, normalize, phonetic_codes

INF = float("inf")

# Not stored as index rows: _id lives on the resource row, _lastUpdated is the store's
# load instant, _content/_text use the optional full-text table and _query (named
# queries) is not implemented. Of the special parameters only `near` (Location.position)
# is indexed; the others are not listed in the CapabilityStatement.
UNINDEXED = frozenset({"_id", "_lastUpdated", "_content", "_text", "_query"})
TABLES = ("token", "token_text", "ident_type", "ref", "ref_url", "ref_ident", "date", "string", "quantity", "number", "uri", "position",
          "composite")
DATE_TYPES = frozenset({"date", "dateTime", "instant"})
NAME_PARTS = ("family", "given", "prefix", "suffix", "text")
ADDRESS_PARTS = ("line", "city", "district", "state", "postalCode", "country", "text")


def _strings(value):
    if isinstance(value, list):
        return [v for v in value if isinstance(v, str)]
    return [value] if isinstance(value, str) else []


def token_values(node):
    """(system, code) pairs and display texts of one node."""
    value, typ, implicit = node
    pairs, texts = [], []
    if typ == "Coding" and isinstance(value, dict):
        if value.get("code") is not None:
            pairs.append((value.get("system"), str(value["code"])))
        texts += _strings(value.get("display"))
    elif typ == "CodeableConcept" and isinstance(value, dict):
        for coding in value.get("coding", []):
            if isinstance(coding, dict) and coding.get("code") is not None:
                pairs.append((coding.get("system"), str(coding["code"])))
                texts += _strings(coding.get("display"))
        texts += _strings(value.get("text"))
    elif typ == "Identifier" and isinstance(value, dict):
        if value.get("value") is not None:
            pairs.append((value.get("system"), str(value["value"])))
        texts += _strings((value.get("type") or {}).get("text"))
    elif typ == "ContactPoint" and isinstance(value, dict):
        if value.get("value") is not None:
            pairs.append((value.get("system"), str(value["value"])))
    elif isinstance(value, bool):
        pairs.append((None, "true" if value else "false"))
    elif isinstance(value, (str, int, float)):
        pairs.append((implicit, str(value)))
    return pairs, texts


def date_values(node, tz):
    """(low_us, high_us) instant ranges; an open Period end is unbounded."""
    value, typ, _ = node
    if typ in DATE_TYPES and isinstance(value, str):
        return [date_range(value, tz)[:2]]
    if typ == "Period" and isinstance(value, dict):
        start, end = value.get("start"), value.get("end")
        if start is None and end is None:
            return []
        low = LOW_OPEN if start is None else date_range(start, tz)[0]
        high = HIGH_OPEN if end is None else date_range(end, tz)[1]
        return [(low, high)]
    if typ == "Timing" and isinstance(value, dict):
        # R4: scheduling details are ignored and only the outer limits matter (events and
        # repeat.boundsPeriod together form one range)
        ranges = [date_range(e, tz)[:2] for e in _strings(value.get("event"))]
        bounds = (value.get("repeat") or {}).get("boundsPeriod")
        if isinstance(bounds, dict) and (bounds.get("start") or bounds.get("end")):
            ranges.append((LOW_OPEN if not bounds.get("start") else date_range(bounds["start"], tz)[0],
                           HIGH_OPEN if not bounds.get("end") else date_range(bounds["end"], tz)[1]))
        return [(min(r[0] for r in ranges), max(r[1] for r in ranges))] if ranges else []
    return []


def string_values(node):
    value, typ, _ = node
    if typ == "HumanName" and isinstance(value, dict):
        return [s for part in NAME_PARTS for s in _strings(value.get(part))]
    if typ == "Address" and isinstance(value, dict):
        return [s for part in ADDRESS_PARTS for s in _strings(value.get(part))]
    if isinstance(value, str):
        return [value]
    return []


def _number(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def quantity_values(node):
    """(low, high, system, code, unit): the value interval a Quantity or Range denotes.
    A comparator makes it one-sided ('<5' is below 5); a Range spans low..high (open ends
    unbounded). SampledData is not indexed."""
    value, typ, _ = node
    if not isinstance(value, dict):
        return []
    if typ == "Money":                  # R4 lists Money under quantity: currency as an ISO 4217 code
        number = _number(value.get("value"))
        if number is None:
            return []
        currency = value.get("currency")
        return [(number, number, "urn:iso:std:iso:4217" if currency else None, currency, currency)]
    if typ == "Quantity" or typ in QUANTITY_PROFILES:
        number = _number(value.get("value"))
        if number is None:
            return []
        low, high = {"<": (-INF, math.nextafter(number, -INF)), "<=": (-INF, number),
                     ">": (math.nextafter(number, INF), INF), ">=": (number, INF)}.get(value.get("comparator"), (number, number))
        return [(low, high, value.get("system"), value.get("code"), value.get("unit"))]
    if typ == "Range":
        lo, hi = value.get("low") or {}, value.get("high") or {}
        low, high = _number(lo.get("value")), _number(hi.get("value"))
        if low is None and high is None:
            return []
        units = lo if low is not None else hi
        return [(-INF if low is None else low, INF if high is None else high,
                 units.get("system"), units.get("code"), units.get("unit"))]
    return []


def contained_resources(resource):
    """(row id, contained resource, local references) for each contained resource of a container.
    Row ids are 'Type/id#local'; inside the container '#local' names a contained resource and
    inside a contained resource '#' names the container."""
    rtype, rid = resource.get("resourceType"), resource.get("id")
    items = [c for c in resource.get("contained") or [] if isinstance(c, dict) and c.get("resourceType") and c.get("id")]
    local = {f"#{c['id']}": (c["resourceType"], f"{rtype}/{rid}#{c['id']}") for c in items}
    inner = {**local, "#": (rtype, rid)}
    return local, [(local[f"#{c['id']}"][1], c, inner) for c in items]


def reference_values(node, local_bases=(), local=None):
    """Relative literal references as (type, id); absolute references on this server's own
    base URLs (`local_bases`) count as relative ones; '#local' references resolve through `local`."""
    value, typ, _ = node
    if typ != "Reference" or not isinstance(value, dict):
        return []
    ref = value.get("reference")
    if isinstance(ref, str) and ref.startswith("#"):
        return [local[ref]] if local and ref in local else []
    if not isinstance(ref, str):
        return []
    if ":" in ref.split("/")[0]:
        base = next((b for b in local_bases if ref.startswith(b + "/")), None)
        if base is None:
            return []
        ref = ref[len(base) + 1:]
    parts = ref.split("/_history/")[0].rstrip("/").split("/")
    return [(parts[-2], parts[-1])] if len(parts) >= 2 else []


def reference_urls(node):
    """Absolute references (http(s):, urn:) and canonical references as (url, version)."""
    value, typ, _ = node
    if typ == "Reference" and isinstance(value, dict):
        ref = value.get("reference")
        if isinstance(ref, str) and ":" in ref.split("/")[0]:
            return [(ref.split("/_history/")[0], None)]
        return []
    if typ in ("canonical", "uri", "url") and isinstance(value, str) and value:
        url, _, version = value.partition("|")
        return [(url, version or None)]
    return []


def reference_identifiers(node):
    value, typ, _ = node
    ident = value.get("identifier") if typ == "Reference" and isinstance(value, dict) else None
    if isinstance(ident, dict) and ident.get("value") is not None:
        return [(ident.get("system"), str(ident["value"]))]
    return []


class Extractor:
    def __init__(self, model, params, intern, tz, local_bases=()):
        """params: dicts with id, type, code, ptype, expression, components [(ptype, expression)]."""
        self.model, self.intern, self.tz = model, intern, tz
        self.local_bases = tuple(b.rstrip('/') for b in local_bases)
        self.by_type = defaultdict(list)
        self.unsupported = []
        self.add(params)

    def add(self, params):
        model = self.model
        for p in params:
            special_ok = p["ptype"] == "special" and p["code"] == "near"
            if p["code"] in UNINDEXED or (p["ptype"] == "special" and not special_ok) or not p.get("expression"):
                continue
            shape = composite_shape_problem(p)
            if shape:
                self.unsupported.append((p["type"], p["code"], shape))
                continue
            try:
                fn = compile_expression(p["expression"], model, p["type"])
                comps = [(ptype, compile_expression(expr, model, p["type"])) for ptype, expr in p.get("components", [])]
            except ValueError as exc:
                self.unsupported.append((p["type"], p["code"], str(exc)))
                continue
            self.by_type[p["type"]].append((p, fn, comps))

    def rows(self, resource, pid, local=None):
        self.local = local
        out = {t: set() for t in TABLES}
        rtype = resource["resourceType"]
        root = [(resource, rtype, None)]
        ENV["resource"] = root[0]
        intern, tz = self.intern, self.tz
        for entry in list(self.by_type.get(rtype, ())):
            try:
                self._param_rows(entry, root, out, pid)
            except UnsupportedPath as exc:
                # Needs the referenced resource's content: withdraw the parameter rather than
                # let it answer "nothing found".
                self.by_type[rtype].remove(entry)
                self.unsupported.append((rtype, entry[0]["code"], str(exc)))
        return out

    def _param_rows(self, entry, root, out, pid):
        p, fn, comps = entry
        intern, tz = self.intern, self.tz
        nodes = fn(root)
        if not nodes:
            return
        pid_ = p["id"]
        ptype = p["ptype"]
        if ptype == "token":
            for node in nodes:
                pairs, texts = token_values(node)
                for system, code in pairs:
                    out["token"].add((pid_, intern(system) if system else None, code, pid))
                for text in texts:
                    out["token_text"].add((pid_, normalize(text), pid))
                value = node[0]
                if node[1] == "Identifier" and isinstance(value, dict) and value.get("value") is not None:
                    for coding in (value.get("type") or {}).get("coding", []):
                        if isinstance(coding, dict) and coding.get("code") is not None:
                            system = coding.get("system")
                            out["ident_type"].add((pid_, intern(system) if system else None, str(coding["code"]),
                                                   str(value["value"]), pid))
        elif ptype == "reference":
            for node in nodes:
                for t, i in reference_values(node, self.local_bases, self.local):
                    out["ref"].add((pid_, intern(t), i, pid))
                for url, version in reference_urls(node):
                    out["ref_url"].add((pid_, url, version, pid))
                for system, value in reference_identifiers(node):
                    out["ref_ident"].add((pid_, intern(system) if system else None, value, pid))
        elif ptype == "special":
            for node in nodes:
                lat, lon = (node[0].get("latitude"), node[0].get("longitude")) if isinstance(node[0], dict) else (None, None)
                if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
                    out["position"].add((pid_, float(lat), float(lon), pid))
        elif ptype == "date":
            for node in nodes:
                for low, high in date_values(node, tz):
                    out["date"].add((pid_, low, high, pid))
        elif ptype == "string" and p.get("usage") == "phonetic":
            # phonetic: one row per name part, norm = its phonetic code (letters Soundex, digits exact)
            for node in nodes:
                for s in string_values(node):
                    for code in phonetic_codes(s):
                        out["string"].add((pid_, code, s, pid))
        elif ptype == "string":
            family_param = (p.get("expression") or "").rstrip().endswith(".family")
            for node in nodes:
                for s in string_values(node):
                    out["string"].add((pid_, normalize(s), s, pid))
                # R4: the parts of a family name are searched independently ("Quinones" finds
                # "Carreno Quinones"); :exact still compares the whole value
                if node[1] == "HumanName" and isinstance(node[0], dict):
                    families = _strings(node[0].get("family"))
                elif isinstance(node[0], str) and family_param:
                    families = [node[0]]
                else:
                    families = []
                for family in families:
                    for part in re.split(r"[\s\-]+", family)[1:]:
                        if part:
                            out["string"].add((pid_, normalize(part), family, pid))
        elif ptype == "quantity":
            for node in nodes:
                for low, high, system, code, unit in quantity_values(node):
                    out["quantity"].add((pid_, low, high, intern(system) if system else None, code, unit, pid))
        elif ptype == "number":
            for node in nodes:
                if isinstance(node[0], (int, float)) and not isinstance(node[0], bool):
                    out["number"].add((pid_, float(node[0]), pid))
        elif ptype == "uri":
            for node in nodes:
                if isinstance(node[0], str):
                    out["uri"].add((pid_, node[0], pid))
        elif ptype == "composite" and len(comps) == 2:
            (_, first), (kind, second) = comps
            for node in nodes:
                left = [pair for n in first([node]) for pair in token_values(n)[0]]
                right = _component(kind, second([node]), intern, tz)
                for system, code in left:
                    for extra in right:
                        out["composite"].add((pid_, intern(system) if system else None, code, *extra, pid))


COMPOSITE_SECOND = ("token", "quantity", "date", "string")


def composite_shape_problem(p):
    """Composites this index stores: a token first component and one token/quantity/date/string second."""
    if p["ptype"] != "composite":
        return None
    comps = p.get("components", [])
    if p.get("declared_components", len(comps)) != len(comps):
        return "composite component definition not found"
    if len(comps) != 2 or comps[0][0] != "token" or comps[1][0] not in COMPOSITE_SECOND:
        return "composite shape not implemented: " + "$".join(k for k, _ in comps)
    return None


def _component(kind, nodes, intern, tz):
    """(c2_system, c2_code, c2_value, c2_low, c2_high, c2_text) tuples of the second component
    (quantity: interval in c2_low/c2_high, unit in c2_text)."""
    rows = []
    for node in nodes:
        if kind == "token":
            rows += [(intern(s) if s else None, c, None, None, None, None) for s, c in token_values(node)[0]]
        elif kind == "quantity":
            rows += [(intern(s) if s else None, c, None, lo, hi, u) for lo, hi, s, c, u in quantity_values(node)]
        elif kind == "date":
            rows += [(None, None, None, lo, hi, None) for lo, hi in date_values(node, tz)]
        elif kind == "string":
            rows += [(None, None, None, None, None, normalize(s)) for s in string_values(node)]
    return rows
