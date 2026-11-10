"""_content/_text query syntax, its FTS5 translation and plain matching."""
from __future__ import annotations

import re

from ..model.values import normalize


TEXT_TOKEN = re.compile(r'\s*(?:(\()|(\))|"([^"]*)"|([^\s()"]+))')


def parse_text_query(text):
    """_text/_content syntax: words (implicit AND), "quoted phrases", AND, OR, NOT (any case) and
    parentheses. Returns a tree: ("word", [terms]) | ("and"|"or", [a, b]) | ("not", [a]), or None."""
    tokens, pos = [], 0
    while pos < len(text):
        m = TEXT_TOKEN.match(text, pos)
        if not m or m.end() == pos:
            if text[pos:].strip():
                raise ValueError("unbalanced quote")
            break
        pos = m.end()
        if m.group(1):
            tokens.append(("(", None))
        elif m.group(2):
            tokens.append((")", None))
        elif m.group(3) is not None:
            words = re.findall(r"[^\W_]+", m.group(3))
            if words:
                tokens.append(("word", words))
        else:
            word = m.group(4)
            if word.upper() in ("AND", "OR", "NOT"):
                tokens.append((word.upper(), None))
            else:
                words = re.findall(r"[^\W_]+", word)
                if words:
                    tokens.append(("word", words))
    i = 0

    def expr():
        nonlocal i
        left = term()
        while i < len(tokens) and tokens[i][0] == "OR":
            i += 1
            left = ("or", [left, term()])
        return left

    def term():
        nonlocal i
        left = factor()
        while i < len(tokens) and tokens[i][0] not in ("OR", ")"):
            if tokens[i][0] == "AND":
                i += 1
            left = ("and", [left, factor()])
        return left

    def factor():
        nonlocal i
        if i >= len(tokens):
            raise ValueError("incomplete expression")
        kind, words = tokens[i]
        i += 1
        if kind == "NOT":
            return ("not", [factor()])
        if kind == "(":
            inner = expr()
            if i >= len(tokens) or tokens[i][0] != ")":
                raise ValueError("missing )")
            i += 1
            return inner
        if kind == "word":
            return ("word", words)
        raise ValueError(f"unexpected {kind}")

    if not tokens:
        return None
    tree = expr()
    if i != len(tokens):
        raise ValueError("unexpected )")
    return tree


def fts_query(tree):
    kind, items = tree
    if kind == "word":
        return '"' + " ".join(w.replace('"', "") for w in items) + '"'
    if kind == "not":
        raise ValueError("NOT needs a left side")
    if kind == "and":
        left, right = items
        if right[0] == "not":
            return f"({fts_query(left)} NOT {fts_query(right[1][0])})"
        if left[0] == "not":
            return f"({fts_query(right)} NOT {fts_query(left[1][0])})"
        return f"({fts_query(left)} AND {fts_query(right)})"
    return f"({fts_query(items[0])} OR {fts_query(items[1])})"


def text_match(tree, words):
    """Evaluate a parsed _text/_content query against a document's normalized words (oracle)."""
    kind, items = tree
    if kind == "word":
        n = len(items)
        terms = [normalize(w) for w in items]
        return any(words[k:k + n] == terms for k in range(len(words) - n + 1))
    if kind == "not":
        return not text_match(items[0], words)
    if kind == "and":
        return text_match(items[0], words) and text_match(items[1], words)
    return text_match(items[0], words) or text_match(items[1], words)
