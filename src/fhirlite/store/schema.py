"""Store file format: tables, indexes, insert statements and default search settings."""
from __future__ import annotations


FORMAT = "fhirlite-store-v1"


SCHEMA = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE strings(id INTEGER PRIMARY KEY, value TEXT NOT NULL UNIQUE);
-- usage: the definition's processing mode (normal | phonetic | nearby | distance | other);
-- multiple_or / multiple_and: the definition's multipleOr / multipleAnd (NULL = not stated, allowed)
CREATE TABLE params(id INTEGER PRIMARY KEY, type TEXT NOT NULL, code TEXT NOT NULL, ptype TEXT NOT NULL,
                    url TEXT, expression TEXT, targets TEXT NOT NULL, components TEXT NOT NULL,
                    usage TEXT, multiple_or INTEGER, multiple_and INTEGER, UNIQUE(type, code));
-- contained resources are rows too: id 'Container/cid#local', container = the container's pid
CREATE TABLE resources(pid INTEGER PRIMARY KEY, type_id INTEGER NOT NULL, id TEXT NOT NULL, json BLOB NOT NULL, container INTEGER);
-- token codes and identifier values compare case-insensitively (ASCII; R4: 'when in doubt' case-insensitive)
CREATE TABLE sp_token(param INTEGER NOT NULL, system INTEGER, code TEXT NOT NULL COLLATE NOCASE, pid INTEGER NOT NULL);
-- Identifier.type coding + value, for token :of-type
CREATE TABLE sp_ident_type(param INTEGER NOT NULL, tsystem INTEGER, tcode TEXT NOT NULL COLLATE NOCASE, value TEXT NOT NULL COLLATE NOCASE, pid INTEGER NOT NULL);
CREATE TABLE sp_token_text(param INTEGER NOT NULL, norm TEXT NOT NULL, pid INTEGER NOT NULL);
CREATE TABLE sp_ref(param INTEGER NOT NULL, target_type INTEGER NOT NULL, target_id TEXT NOT NULL, pid INTEGER NOT NULL);
CREATE TABLE sp_ref_url(param INTEGER NOT NULL, url TEXT NOT NULL, version TEXT, pid INTEGER NOT NULL);
CREATE TABLE sp_ref_ident(param INTEGER NOT NULL, system INTEGER, value TEXT NOT NULL COLLATE NOCASE, pid INTEGER NOT NULL);
-- date ranges in microseconds since 1970-01-01T00:00:00Z
CREATE TABLE sp_date(param INTEGER NOT NULL, low INTEGER NOT NULL, high INTEGER NOT NULL, pid INTEGER NOT NULL);
CREATE TABLE sp_string(param INTEGER NOT NULL, norm TEXT NOT NULL, exact TEXT NOT NULL, pid INTEGER NOT NULL);
-- quantity intervals: a plain value is low = high; comparators and Ranges are one- or two-sided
CREATE TABLE sp_quantity(param INTEGER NOT NULL, low REAL NOT NULL, high REAL NOT NULL, system INTEGER, code TEXT, unit TEXT,
                         pid INTEGER NOT NULL);
CREATE TABLE sp_number(param INTEGER NOT NULL, value REAL NOT NULL, pid INTEGER NOT NULL);
CREATE TABLE sp_uri(param INTEGER NOT NULL, uri TEXT NOT NULL, pid INTEGER NOT NULL);
CREATE TABLE sp_position(param INTEGER NOT NULL, lat REAL NOT NULL, lon REAL NOT NULL, pid INTEGER NOT NULL);
-- second component: token (c2_system, c2_code), quantity (interval c2_low/c2_high, c2_system, c2_code, unit in c2_text),
-- date (c2_low, c2_high) or string (normalized c2_text)
CREATE TABLE sp_composite(param INTEGER NOT NULL, system INTEGER, code TEXT NOT NULL COLLATE NOCASE, c2_system INTEGER, c2_code TEXT COLLATE NOCASE,
                          c2_value REAL, c2_low INTEGER, c2_high INTEGER, c2_text TEXT, pid INTEGER NOT NULL);
-- versions that are no longer current, and deletions (json NULL), written through a writable connection
CREATE TABLE versions(type_id INTEGER NOT NULL, id TEXT NOT NULL, version INTEGER NOT NULL, updated TEXT NOT NULL,
                      method TEXT NOT NULL, status INTEGER NOT NULL, json BLOB, PRIMARY KEY(type_id, id, version));
-- resources (and their contained rows) written after load: lastUpdated range in microseconds and the
-- method and status of that write. Loaded resources have no row: their lastUpdated is the load instant.
CREATE TABLE written(pid INTEGER PRIMARY KEY, low INTEGER NOT NULL, high INTEGER NOT NULL, method TEXT NOT NULL,
                     status INTEGER NOT NULL);
CREATE TABLE valuesets(url TEXT PRIMARY KEY, complete INTEGER NOT NULL);
CREATE TABLE valueset_codes(url TEXT NOT NULL, system INTEGER, code TEXT NOT NULL COLLATE NOCASE);
"""


INDEXES = """
CREATE INDEX resources_type_id ON resources(type_id, id);
CREATE INDEX resources_type ON resources(type_id);
CREATE INDEX sp_token_value ON sp_token(param, code, system);
CREATE INDEX sp_token_pid ON sp_token(pid, param);
CREATE INDEX sp_ident_type_value ON sp_ident_type(param, value, tcode);
CREATE INDEX sp_ident_type_pid ON sp_ident_type(pid, param);
CREATE INDEX sp_token_text_value ON sp_token_text(param, norm);
CREATE INDEX sp_token_text_pid ON sp_token_text(pid, param);
CREATE INDEX sp_ref_target ON sp_ref(param, target_id, target_type);
CREATE INDEX sp_ref_pid ON sp_ref(pid, param);
CREATE INDEX sp_ref_url_value ON sp_ref_url(param, url);
CREATE INDEX sp_ref_url_pid ON sp_ref_url(pid, param);
CREATE INDEX sp_ref_ident_value ON sp_ref_ident(param, value, system);
CREATE INDEX sp_ref_ident_pid ON sp_ref_ident(pid, param);
CREATE INDEX sp_date_low ON sp_date(param, low);
CREATE INDEX sp_date_high ON sp_date(param, high);
CREATE INDEX sp_date_pid ON sp_date(pid, param);
CREATE INDEX sp_string_norm ON sp_string(param, norm);
CREATE INDEX sp_string_exact ON sp_string(param, exact);
CREATE INDEX sp_string_pid ON sp_string(pid, param);
CREATE INDEX sp_quantity_low ON sp_quantity(param, low);
CREATE INDEX sp_quantity_high ON sp_quantity(param, high);
CREATE INDEX sp_quantity_pid ON sp_quantity(pid, param);
CREATE INDEX sp_number_value ON sp_number(param, value);
CREATE INDEX sp_number_pid ON sp_number(pid, param);
CREATE INDEX sp_uri_value ON sp_uri(param, uri);
CREATE INDEX sp_uri_pid ON sp_uri(pid, param);
CREATE INDEX sp_position_pid ON sp_position(pid, param);
CREATE INDEX sp_composite_value ON sp_composite(param, code, system);
CREATE INDEX sp_composite_pid ON sp_composite(pid, param);
CREATE INDEX valueset_codes_value ON valueset_codes(url, code, system);
CREATE INDEX written_low ON written(low);
"""


INSERT = {
    "token": "INSERT INTO sp_token VALUES (?,?,?,?)",
    "token_text": "INSERT INTO sp_token_text VALUES (?,?,?)",
    "ident_type": "INSERT INTO sp_ident_type VALUES (?,?,?,?,?)",
    "ref": "INSERT INTO sp_ref VALUES (?,?,?,?)",
    "ref_url": "INSERT INTO sp_ref_url VALUES (?,?,?,?)",
    "ref_ident": "INSERT INTO sp_ref_ident VALUES (?,?,?,?)",
    "date": "INSERT INTO sp_date VALUES (?,?,?,?)",
    "string": "INSERT INTO sp_string VALUES (?,?,?,?)",
    "quantity": "INSERT INTO sp_quantity VALUES (?,?,?,?,?,?,?)",
    "number": "INSERT INTO sp_number VALUES (?,?,?)",
    "uri": "INSERT INTO sp_uri VALUES (?,?,?)",
    "position": "INSERT INTO sp_position VALUES (?,?,?,?)",
    "composite": "INSERT INTO sp_composite VALUES (?,?,?,?,?,?,?,?,?,?)",
}


# ---- extraction workers ------------------------------------------------------------------
# Index row columns that hold interned strings (systems, target types). Workers return the
# strings and their own batch-local pids; the writing process numbers and interns them.
INTERNED = {"token": (1,), "ident_type": (1,), "ref": (1,), "ref_ident": (1,), "quantity": (3,), "composite": (1, 3)}


DEFAULT_SETTINGS = {
    # Server choices the specification leaves open; stored in the file and reported by metadata.
    "default_count": 100, "max_count": 1000, "timezone": "UTC", "near_default_km": 1.0,
    # Absolute references on these bases are references to this store (same server).
    "local_bases": ["http://localhost/fhir"],
}


def flag(value):
    return None if value is None else int(bool(value))


def preassigned_params(definitions):
    """{resource type: parameters} with ids fixed by the definitions alone."""
    out = {}
    for i, p in enumerate(definitions.params_for(sorted(definitions.model.resource_types)), 1):
        p["id"] = i
        out.setdefault(p["type"], []).append(p)
    return out
