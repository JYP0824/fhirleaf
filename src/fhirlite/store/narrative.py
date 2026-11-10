"""Text of a resource for _content and _text: narrative and string leaves."""
from __future__ import annotations

import html
import re


def text_leaves(node, out):
    if isinstance(node, dict):
        for key, value in node.items():
            if key not in ("resourceType", "id", "meta", "text"):
                text_leaves(value, out)
    elif isinstance(node, list):
        for value in node:
            text_leaves(value, out)
    elif isinstance(node, str):
        out.append(node)
    return out


def narrative(resource):
    div = (resource.get("text") or {}).get("div") or ""
    return html.unescape(re.sub(r"<[^>]+>", " ", div))
