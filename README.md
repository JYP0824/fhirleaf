# FHIRLeaf: An embedded FHIR R4 search engine

FHIRLeaf is an embedded FHIR R4 search engine for Python.
It runs R4 RESTful Search on a portable SQLite file, in process or over HTTP, without deploying a FHIR server.
A store is read-only unless it is opened writable, for create, update and delete.

## Requirements

- Python 3.10 or later
- No third-party packages (standard library only)
- SQLite FTS5 for `--fulltext` (included in the python.org, uv and conda builds)

## Quick start

```bash
uv add "fhirleaf @ git+https://github.com/JYP0824/fhirleaf@v0.2.1"
curl -fL https://hl7.org/fhir/R4/definitions.json.zip -o definitions-r4.json.zip
fhirleaf verify --cases --definitions definitions-r4.json.zip
fhirleaf load --input data/ --definitions definitions-r4.json.zip --output store.db
fhirleaf get --db store.db "Patient?birthdate=ge1990"
fhirleaf serve --db store.db --port 8080
```

## Install

| Source | Command |
|---|---|
| Git tag | `uv add "fhirleaf @ git+https://github.com/JYP0824/fhirleaf@v0.2.1"` |
| PyPI (after release) | `uv add fhirleaf` |
| Wheel | `uv add ./fhirleaf-0.2.1-py3-none-any.whl` |
| Local checkout | `uv add --editable ./fhirleaf` |
| CLI only | `uv tool install "git+https://github.com/JYP0824/fhirleaf@v0.2.1"` |

`pip install` takes the same sources.

## Definitions

HL7 publishes the FHIR definitions under CC0. They are not bundled.

```bash
curl -fL https://hl7.org/fhir/R4/definitions.json.zip -o definitions-r4.json.zip
```

R4B and R5 definitions load, but search behaviour is R4 and only R4 is verified.

## Build a store

```bash
fhirleaf load --input data/ --definitions definitions-r4.json.zip --output store.db
```

| Option | Effect |
|---|---|
| `--input PATH` | NDJSON, Bundle or resource JSON (`.gz` allowed), file or directory. Repeatable. |
| `--package FILE.tgz` | FHIR package with SearchParameters, ValueSets and CodeSystems. Repeatable. |
| `--fulltext` | Builds the index for `_content` and `_text`. |
| `--timezone` | Zone for times without an offset. `UTC` (default), `+HH:MM` or a POSIX rule. |
| `--base-url URL` | This server's base. Absolute references on it count as local. |
| `--default-count`, `--max-count` | Page size and maximum (100, 1000). |
| `--last-updated INSTANT` | `meta.lastUpdated` for every resource (default: load time). |
| `--no-hash` | Skips SHA-256 of the source files. |
| `--workers N` | Reads and extracts index rows in N processes. The store is the same for any N. |

During load, Bundles are unpacked and `urn:uuid:` and conditional references are resolved.
Identical duplicates are stored once, and conflicting duplicates stop the load.
A finished store is read-only and can be shared by many processes.
Without `--input`, `load` makes an empty store to fill through a writable connection.

## Search

```bash
fhirleaf get --db store.db "Observation?patient=Patient/1&code=http://loinc.org|8867-4"
fhirleaf get --db store.db "Patient/1"
fhirleaf serve --db store.db --port 8080
curl "http://127.0.0.1:8080/fhir/Observation?patient=Patient/1&_count=10"
```

```python
import fhirleaf

store = fhirleaf.connect("store.db")
status, bundle = store.get("Observation?patient=Patient/1&date=ge2020-01-01")
status, patient = store.get("Patient/1")
status, bundle = store.post("Observation/_search", b"patient=Patient/1")
```

`get` and `post` return `(status, json)`. Paths are relative to the base.

## Write

Open a store writable to create, update and delete.
It needs the definitions and packages the store was built with.

```python
store = fhirleaf.connect("store.db", writable=True, definitions="definitions-r4.json.zip")
status, patient = store.post("Patient", {"resourceType": "Patient", "name": [{"family": "Lee"}]})
status, patient = store.put(f"Patient/{patient['id']}", {**patient, "gender": "female"}, if_match='W/"1"')
status, _ = store.delete(f"Patient/{patient['id']}")
store.close()
```

```bash
fhirleaf serve --db store.db --writable --definitions definitions-r4.json.zip
```

| Request | Result |
|---|---|
| `POST [type]` | `201` with `Location`, `ETag` and `Last-Modified`. The server assigns the id. |
| `PUT [type]/[id]` | `200` with a new version, or `201` when the id is new or was deleted |
| `PUT` with `If-Match` | `412` when no listed version is the current one |
| `DELETE [type]/[id]` | `204`. Also `204` when there is nothing to delete. |
| `GET [type]/[id]` after delete | `410` |
| `GET [type]/[id]/_history/[vid]` | Any stored version. `410` for the deletion. |
| `GET [type]/[id]/_history` | Every version, newest first, with `_count` and `_since` |
| `Prefer: return=` | `representation` (default), `minimal`, `OperationOutcome` |

A written resource is indexed exactly as `load` indexes it.
The first writable open registers every resource type of the definitions.
From then on every type can be searched, and date `ap` is relative to the current time.
Writes are transactions in SQLite WAL mode. Other connections see them on their next request.
Closing the last writer folds the WAL back into the file.
`serve --writable` answers one request at a time.

### Unknown parameters

| Mode | Result | How to select |
|---|---|---|
| `strict` (default) | `400` with the list of supported parameters | – |
| `lenient` | Ignored and reported in an `outcome` entry | `Prefer: handling=lenient`, `serve --handling lenient`, `get --lenient`, `handling="lenient"` |

FHIR leaves this default to the server. State the mode when comparing results.
Read and vread accept only `_summary`, `_elements`, `_format` and `_pretty`.
Empty parameters are ignored in both modes.

## Check

| Command | What it checks | Result |
|---|---|---|
| `fhirleaf verify --cases --definitions D` | Hand-computed specification cases, in process and over HTTP. CI runs them on Python 3.10, 3.12 and 3.13. | 306/306 cases, 157/157 combinations |
| (same command) | Write steps worked out from R4 http.html, run in process and over HTTP. Stores filled by writes against fresh loads, a failed write rolled back, and concurrent If-Match updates | 61/61 steps, 4/4 checks |
| `fhirleaf verify --input DATA --definitions D --output DIR` | A request corpus on a patient subset against brute-force matching, with per-parameter coverage | MIMIC-IV-on-FHIR Demo 1,240/1,240, Synthea + US Core 2,431/2,431 |
| `fhirleaf support --definitions D --output FILE` | Status of every SearchParameter per resource type | R4: 2,873 of 3,026 supported |

Compare with another FHIR server:

```bash
fhirleaf corpus  --db store.db --output requests.jsonl
fhirleaf record  --base-url http://other/fhir --requests requests.jsonl --output recorded.jsonl
fhirleaf compare --db store.db --recorded recorded.jsonl --report report.json
```

## Supported

### Interactions

| Request | Result |
|---|---|
| `GET [base]/metadata` | CapabilityStatement |
| `GET [base]/[type]?params` | Type search |
| `GET [base]/[compartment]/[id]/[type]?params` | Compartment search (`*` for every type) |
| `GET [base]?_type=A,B&params` | Whole-system search (repeated `_type` is AND) |
| `GET [base]/[type]/[id]` | Read (with `_summary` or `_elements`) |
| `GET [base]/[type]/[id]/_history/1` | Vread (one stored version) |
| `POST [base]/_search`, `[type]/_search`, `[compartment]/[id]/[type]/_search` | Search with a form-encoded body |
| `HEAD` | Headers only |
| Other methods | `405` with `Allow: GET, HEAD` |
| Resource type without loaded data | `404` (not listed in `metadata`) |
| History, operations (`$...`) | `400` (instance history in a writable store, see Write) |
| Create, update, delete | `405`, unless the store is opened writable |

Responses use the requested JSON type: `application/fhir+json` (default), `application/json`,
`text/json` or `application/json+fhir`, chosen by `_format` or by `Accept` with q-values.
An `Accept` that excludes JSON or asks for another `fhirVersion` gets `406`.
Reads and `metadata` carry an `ETag`. Conditional reads are not supported, so reads always
return the full resource.

### Parameters

| Type | Modifiers | Prefixes |
|---|---|---|
| token | `:missing` `:not` `:text` `:in` `:not-in` `:of-type` | – |
| reference | `:missing` `:[type]` `:identifier` `:below` `:above`, chains, `_has` | – |
| date (incl. Period and Timing) | `:missing` | `eq ne gt lt ge le sa eb ap` |
| string | `:missing` `:exact` `:contains`, phonetic (Soundex) | – |
| quantity (incl. Money, Range) | `:missing` | `eq ne gt lt ge le sa eb ap` |
| number | `:missing` | `eq ne gt lt ge le sa eb ap` |
| uri | `:missing`, `:below` `:above` (URLs only), canonical `url\|version` | – |
| composite | none (token + one component) | of the second component |
| special | `near` | – |

Common parameters: `_id`, `_lastUpdated`, `_list`, `_content`, `_text` (with `--fulltext`).

| Rule | Behaviour |
|---|---|
| Token codes and identifier values | Case-insensitive (ASCII). Systems are exact. |
| Strings | Case, accents, punctuation and repeated whitespace are ignored. Family-name parts match on their own. |
| Timing | Matched by its outer limits (events and `repeat.boundsPeriod`). |
| Plain id on a reference with several target types | `400` when the id exists in more than one of them. Use `Type/id`. |
| Unknown `_list` | Empty result |
| `:not-in` | Also matches resources without a value for the parameter, like `:not` |
| `_include`, `_revinclude` | Every included resource is returned. There is no cap. |
| `_text`, `_content` | Words (all required), `"phrases"`, `AND`, `OR`, `NOT`, parentheses. Commas are OR. |

### Result parameters

`_count`, `_sort`, `_total`, `_summary`, `_elements`, `_include`, `_revinclude`
(`Type:*`, `*`, `:iterate`), `_contained`, `_containedType`, `_format`, `_pretty`.

`_summary` with `_elements`, and `_include` or `_revinclude` with `_summary=text`, return `400`.
The `self` link carries the parameters actually used, and on later pages it is that page.

### Server choices

| Choice | Default |
|---|---|
| Zone for times without an offset | `UTC` (`--timezone`) |
| Page size, maximum | 100, 1000 |
| Order without `_sort` | Load order |
| Missing sort values | Last |
| `near` without a distance | 1 km |
| Phonetic matching | Letters by American Soundex, digits exactly. Every part of the value must match. |
| `ap` reference point | Load instant (`--last-updated`). The current time once the store is opened writable. |
| Sort on token, reference | By code, by target id |
| Update with the same content | No new version (`200` with the current one) |
| References on write | Not checked. A reference may point to a missing resource. |
| Resource content on write | Not validated against profiles. Values that cannot be indexed answer `400`. |

These choices are stored in the file and reported in `metadata`.

## Not supported

These requests return an error:

- Patch, conditional create, update and delete, type and system history, batch, transaction and operations
- `_query` and `_filter`
- Token `:above` and `:below`
- Five R4 composites: `DocumentReference.relationship` and four `MolecularSequence` coordinates
- SearchParameters that navigate through `resolve()` (found only in packages)
- Formats other than JSON

These cannot be detected from the request:

- UCUM unit conversion (`mg` does not match `g`)
- SampledData values
- The repeated hour at a daylight-saving change, for times without an offset

## Release

```bash
uv build
uv venv check && uv pip install --python check dist/*.whl
check/bin/fhirleaf verify --cases --definitions definitions-r4.json.zip
git tag vX.Y.Z && git push --tags
uv publish
```

## Security

`serve` has no authentication, authorisation, TLS or rate limiting.
With `--writable` any client that reaches it can change the store.
It binds to `127.0.0.1` by default. Expose it only behind a proxy that adds these.
CORS is off by default. `serve --cors-origin ORIGIN` enables it for one origin.

## License

FHIRLeaf is licensed under Apache-2.0 (`LICENSE`, `NOTICE`).
The HL7 FHIR definitions are CC0 and are not redistributed.
"FHIR" and "HL7" are HL7 trademarks, and FHIRLeaf is not an HL7 product.
Package terminology and loaded data keep their own terms.
