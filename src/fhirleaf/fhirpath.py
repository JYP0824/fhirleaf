"""Minimal FHIRPath for the R4 SearchParameter expressions of the indexed resource types.

Covers what those definitions use: path navigation (with choice types), union,
parentheses, as/is/ofType, where(), exists(), empty(), not(), resolve(),
equality, and/or, string and boolean literals. Nodes are (value, fhir_type,
implicit_system) tuples: the type drives as()/is and index extraction, the
implicit system is the code system of a required-binding `code` element.
"""
from __future__ import annotations

import re

QUANTITY_PROFILES = frozenset({"Age", "Count", "Distance", "Duration", "MoneyQuantity", "SimpleQuantity"})
SYSTEM_TYPES = {
    "http://hl7.org/fhirpath/System.String": "string",
    "http://hl7.org/fhirpath/System.Boolean": "boolean",
    "http://hl7.org/fhirpath/System.Integer": "integer",
    "http://hl7.org/fhirpath/System.Decimal": "decimal",
    "http://hl7.org/fhirpath/System.Date": "date",
    "http://hl7.org/fhirpath/System.DateTime": "dateTime",
    "http://hl7.org/fhirpath/System.Time": "time",
}
TOKEN = re.compile(r"\s*(?:(?P<str>'(?:[^'\\]|\\.)*')|(?P<op>!=|[.()|=,\[\]])|(?P<env>%[A-Za-z]+)"
                   r"|(?P<id>[A-Za-z_][A-Za-z0-9_]*)|(?P<num>\d+(?:\.\d+)?))")
RESOLVED = object()          # third slot of nodes produced by resolve(): only the target type is known


class UnsupportedPath(ValueError):
    """The expression needs data the store does not have (the content behind resolve())."""


# %resource for the expression being evaluated; set per resource by the extractor (single-threaded load).
ENV = {"resource": None}


class Model:
    """Element types from the R4 StructureDefinition snapshots (specializations only)."""

    def __init__(self, structures, binding_system):
        self.elements = {}
        self.summary, self.mandatory, self.resource_types = {}, {}, set()
        for sd in structures:
            if sd.get("derivation") == "constraint":
                continue
            if sd.get("kind") == "resource":
                self.resource_types.add(sd["type"])
            for el in sd.get("snapshot", {}).get("element", []):
                path = el["path"]
                types = tuple(SYSTEM_TYPES.get(t["code"], t["code"]) for t in el.get("type", []))
                cref = el.get("contentReference")
                self.elements[path] = (types, binding_system(el), cref[1:] if cref else None)
                if sd.get("kind") == "resource" and path.count(".") == 1:
                    name = path.split(".")[1].replace("[x]", "")
                    if el.get("isSummary"):
                        self.summary.setdefault(sd["type"], set()).add(name)
                    if el.get("min", 0) > 0:
                        self.mandatory.setdefault(sd["type"], set()).add(name)

    def children(self, node, name):
        if node[2] is RESOLVED:
            raise UnsupportedPath("Navigation into resolve() results is not supported")
        value, typ = node[0], node[1]
        if not isinstance(value, dict):
            return []
        base = "Quantity" if typ in QUANTITY_PROFILES else typ
        path = base + "." + name
        out = []
        el = self.elements.get(path)
        if el is None:
            el = self.elements.get(path + "[x]")
            if el is None:
                return out
            for t in el[0]:
                raw = value.get(name + t[0].upper() + t[1:])
                if raw is not None:
                    _extend(out, raw, t, el[1])
            return out
        types, system, cref = el
        if cref:
            child = cref
        elif not types or types[0] in ("BackboneElement", "Element"):
            child = path
        else:
            child = types[0]
        raw = value.get(name)
        if raw is not None:
            _extend(out, raw, child, system)
        return out


def _extend(out, raw, typ, system):
    for item in raw if isinstance(raw, list) else (raw,):
        if item is not None:
            out.append((item, typ, system))


# FHIRPath system types and the FHIR primitive types whose values they are
# (e.g. R4 Observation-code-value-date uses `value.as(DateTime)` for a FHIR dateTime).
SYSTEM_TYPE_PRIMITIVES = {
    "Boolean": {"boolean"}, "Integer": {"integer", "positiveInt", "unsignedInt"}, "Decimal": {"decimal"},
    "String": {"string", "code", "id", "uri", "url", "canonical", "markdown", "oid", "uuid", "base64Binary"},
    "Date": {"date"}, "DateTime": {"dateTime", "instant"}, "Time": {"time"},
}


def type_matches(node_type, name):
    name = name.split(".", 1)[1] if name.startswith(("System.", "FHIR.")) else name
    return (node_type == name or (name == "Quantity" and node_type in QUANTITY_PROFILES)
            or node_type in SYSTEM_TYPE_PRIMITIVES.get(name, ()))


def reference_type(value):
    """Target type of a literal Reference ('Type/id', absolute or versioned URL)."""
    ref = value.get("reference") if isinstance(value, dict) else None
    if not isinstance(ref, str) or ref.startswith("#"):
        return None
    parts = ref.split("/_history/")[0].rstrip("/").split("/")
    return parts[-2] if len(parts) >= 2 else None


class _Parser:
    def __init__(self, text, model, resource_type):
        self.tokens, pos = [], 0
        while pos < len(text):
            m = TOKEN.match(text, pos)
            if not m or m.end() == pos:
                if text[pos:].strip():
                    raise ValueError(f"Unsupported FHIRPath near {text[pos:pos + 30]!r}")
                break
            kind = m.lastgroup
            self.tokens.append((kind, m.group(kind)))
            pos = m.end()
        self.i, self.model, self.rtype = 0, model, resource_type

    def peek(self, k=0):
        j = self.i + k
        return self.tokens[j] if j < len(self.tokens) else (None, None)

    def take(self, value=None):
        tok = self.peek()
        if value is not None and tok[1] != value:
            raise ValueError(f"Expected {value!r}, got {tok[1]!r}")
        self.i += 1
        return tok

    def parse(self):
        fn = self.boolean()
        if self.i != len(self.tokens):
            raise ValueError(f"Unexpected FHIRPath token {self.peek()[1]!r}")
        return fn

    def boolean(self):
        left = self.equality()
        while self.peek() in (("id", "and"), ("id", "or")):
            op = self.take()[1]
            right = self.equality()
            left = _and(left, right) if op == "and" else _or(left, right)
        return left

    def equality(self):
        left = self.union()
        if self.peek() in (("op", "="), ("op", "!=")):
            negate = self.take()[1] == "!="
            right = self.union()
            return _equals(left, right, negate)
        return left

    def union(self):
        parts = [self.typed()]
        while self.peek() == ("op", "|"):
            self.take()
            parts.append(self.typed())
        parts = [p for p in parts if p is not None]
        if len(parts) == 1:
            return parts[0]
        return lambda focus: [n for p in parts for n in p(focus)]

    def typed(self):
        fn = self.postfix()
        while self.peek() in (("id", "as"), ("id", "is")):
            op = self.take()[1]
            name = self.take()[1]
            fn = _as(fn, name) if op == "as" else _is(fn, name)
        return fn

    def postfix(self):
        fn = self.primary()
        while self.peek() in (("op", "."), ("op", "[")):
            if getattr(fn, "resolved", False):
                # The store indexes each resource on its own; it cannot read the referenced
                # resource, so such parameters are reported unsupported instead of matching nothing.
                raise ValueError("Navigation after resolve() is not supported")
            if self.take()[1] == "[":
                kind, index = self.take()
                if kind != "num":
                    raise ValueError("Indexer needs an integer")
                self.take("]")
                fn = _index(fn, int(index))
            else:
                fn = self.invocation(fn)
        return fn

    def primary(self):
        kind, value = self.peek()
        if value == "(" and kind == "op":
            self.take()
            fn = self.boolean()
            self.take(")")
            return fn
        if kind == "str":
            self.take()
            text = value[1:-1].replace("\\'", "'")
            return lambda focus: [(text, "string", None)]
        if kind == "num":
            self.take()
            number = float(value) if "." in value else int(value)
            return lambda focus: [(number, "decimal" if "." in value else "integer", None)]
        if kind == "id" and value in ("true", "false"):
            self.take()
            flag = value == "true"
            return lambda focus: [(flag, "boolean", None)]
        if kind == "env":
            self.take()
            if value not in ("%resource", "%rootResource", "%context"):
                raise ValueError(f"Unsupported FHIRPath variable {value}")
            return lambda focus: [ENV["resource"]] if ENV["resource"] is not None else []
        if kind == "id" and self.peek(1) == ("op", "("):
            return self.invocation(lambda focus: focus)
        if kind == "id":
            self.take()
            model, rtype = self.model, self.rtype
            if value in ("Resource", "DomainResource") or value == rtype:
                return lambda focus: [n for n in focus if n[1] == rtype or value in ("Resource", "DomainResource")]
            if value in model.resource_types:
                return lambda focus: []          # another resource type's branch
            return lambda focus: [c for n in focus for c in model.children(n, value)]
        raise ValueError(f"Unsupported FHIRPath primary {value!r}")

    def invocation(self, fn):
        name = self.take()[1]
        model = self.model
        if self.peek() != ("op", "("):
            return lambda focus: [c for n in fn(focus) for c in model.children(n, name)]
        self.take("(")
        if name in ("as", "ofType", "is"):
            typ = self.take()[1]
            self.take(")")
            return _as(fn, typ) if name != "is" else _is(fn, typ)
        if name in ("where", "exists") and self.peek() != ("op", ")"):
            cond = self.boolean()
            self.take(")")
            where = lambda focus: [n for n in fn(focus) if cond([n]) == [(True, "boolean", None)]]
            return where if name == "where" else (lambda focus: [(bool(where(focus)), "boolean", None)])
        if name == "extension":
            kind, url = self.take()
            if kind != "str":
                raise ValueError("extension() needs a URL string")
            url = url[1:-1]
            self.take(")")
            return lambda focus: [c for n in fn(focus) for c in model.children(n, "extension")
                                  if isinstance(c[0], dict) and c[0].get("url") == url]
        self.take(")")
        if name == "exists":
            return lambda focus: [(bool(fn(focus)), "boolean", None)]
        if name == "count":
            return lambda focus: [(len(fn(focus)), "integer", None)]
        if name == "hasValue":
            return lambda focus: [(len(items := fn(focus)) == 1 and not isinstance(items[0][0], dict), "boolean", None)]
        if name == "empty":
            return lambda focus: [(not fn(focus), "boolean", None)]
        if name == "not":
            return lambda focus: [(not n[0], "boolean", None) for n in fn(focus)[:1] if isinstance(n[0], bool)]
        if name == "first":
            return lambda focus: fn(focus)[:1]
        if name == "resolve":
            # Only the target type is known (enough for `resolve() is Type`), not the target's content.
            def resolved(focus):
                return [(n[0], t, RESOLVED) for n in fn(focus) if (t := reference_type(n[0]))]
            resolved.resolved = True
            return resolved
        raise ValueError(f"Unsupported FHIRPath function {name}()")


def _index(fn, i):
    return lambda focus: (lambda items: items[i:i + 1])(fn(focus))


def _as(fn, name):
    return lambda focus: [n for n in fn(focus) if type_matches(n[1], name)]


def _is(fn, name):
    def run(focus):
        items = fn(focus)
        return [(type_matches(items[0][1], name), "boolean", None)] if len(items) == 1 else []
    return run


def _equals(left, right, negate):
    def run(focus):
        a, b = left(focus), right(focus)
        if not a or not b:
            return []
        same = len(a) == len(b) and all(type(x[0]) is type(y[0]) and x[0] == y[0] for x, y in zip(a, b))
        return [(same != negate, "boolean", None)]
    return run


def _truth(items):
    return items[0][0] if len(items) == 1 and isinstance(items[0][0], bool) else None


def _and(left, right):
    def run(focus):
        a, b = _truth(left(focus)), _truth(right(focus))
        if a is False or b is False:
            return [(False, "boolean", None)]
        return [(True, "boolean", None)] if a and b else []
    return run


def _or(left, right):
    def run(focus):
        a, b = _truth(left(focus)), _truth(right(focus))
        if a or b:
            return [(True, "boolean", None)]
        return [(False, "boolean", None)] if a is False and b is False else []
    return run


def compile_expression(text, model, resource_type):
    """Compile `text` for one resource type; returns fn(focus_nodes) -> nodes."""
    return _Parser(text, model, resource_type).parse()
