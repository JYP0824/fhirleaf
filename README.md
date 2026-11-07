# FHIRLeaf: A local FHIR R4 RESTful search engine

FHIRLeaf is a search engine for running FHIR R4 RESTful Search locally.
It loads FHIR data into one SQLite file and searches it on your machine, with no FHIR server or database service to deploy.

## Requirements

- Python 3.10 or later
- No third-party packages (standard library only)
- SQLite FTS5 for `--fulltext` (included in the python.org, uv and conda builds)

## Quick start

```bash
uv add "fhirleaf @ git+https://github.com/JYP0824/fhirleaf@v0.1.0"
curl -fL https://hl7.org/fhir/R4/definitions.json.zip -o definitions-r4.json.zip
fhirleaf verify --cases --definitions definitions-r4.json.zip
fhirleaf load --input data/ --definitions definitions-r4.json.zip --output store.db
fhirleaf get --db store.db "Patient?birthdate=ge1990"
fhirleaf serve --db store.db --port 8080
```

## Install

| Source | Command |
|---|---|
| Git tag | `uv add "fhirleaf @ git+https://github.com/JYP0824/fhirleaf@v0.1.0"` |
| PyPI (after release) | `uv add fhirleaf` |
| Wheel | `uv add ./fhirleaf-0.1.0-py3-none-any.whl` |
| Local checkout | `uv add --editable ./fhirleaf` |
| CLI only | `uv tool install "git+https://github.com/JYP0824/fhirleaf@v0.1.0"` |

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

During load, Bundles are unpacked and `urn:uuid:` and conditional references are resolved.
Identical duplicates are stored once, and conflicting duplicates stop the load.
A finished store is read-only and can be shared by many processes.

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

### Unknown parameters

| Mode | Result | How to select |
|---|---|---|
| `strict` (default) | `400` with the list of supported parameters | – |
| `lenient` | Ignored and reported in an `outcome` entry | `Prefer: handling=lenient`, `serve --handling lenient`, `get --lenient`, `handling="lenient"` |

FHIR leaves this default to the server. State the mode when comparing results.

## Check

| Command | What it checks | Result |
|---|---|---|
| `fhirleaf verify --cases --definitions D` | Hand-computed specification cases, in process and over HTTP | 243/243 cases, 139/139 combinations |
| `fhirleaf verify --input DATA --definitions D --output DIR` | A request corpus on a patient subset against brute-force matching | MIMIC-IV-on-FHIR Demo 1,240/1,240, Synthea + US Core 2,431/2,431 |
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
| `GET [base]/[compartment]/[id]/[type]?params` | Compartment search |
| `GET [base]?_type=A,B&params` | Whole-system search |
| `GET [base]/[type]/[id]` | Read |
| `GET [base]/[type]/[id]/_history/1` | Vread (one stored version) |
| `POST [base]/[type]/_search` | Search with a form-encoded body |
| `HEAD` | Headers only |
| Other methods | `405` |

### Parameters

| Type | Modifiers | Prefixes |
|---|---|---|
| token | `:missing` `:not` `:text` `:in` `:not-in` `:of-type` | – |
| reference | `:missing` `:[type]` `:identifier` `:below` `:above`, chains, `_has` | – |
| date | `:missing` | `eq ne gt lt ge le sa eb ap` |
| string | `:missing` `:exact` `:contains`, phonetic (Soundex) | – |
| quantity | `:missing` | `eq ne gt lt ge le sa eb ap` |
| number | `:missing` | `eq ne gt lt ge le sa eb ap` |
| uri | `:missing` `:below` `:above` | – |
| composite | `:missing` (token + one component) | of the second component |
| special | `near` | – |

Common parameters: `_id`, `_lastUpdated`, `_list`, `_content`, `_text` (with `--fulltext`).

### Result parameters

`_count`, `_sort`, `_total`, `_summary`, `_elements`, `_include`, `_revinclude`
(`Type:*`, `*`, `:iterate`), `_contained`, `_containedType`, `_format`, `_pretty`.

### Server choices

| Choice | Default |
|---|---|
| Zone for times without an offset | `UTC` (`--timezone`) |
| Page size, maximum | 100, 1000 |
| Order without `_sort` | Load order |
| Missing sort values | Last |
| `near` without a distance | 1 km |
| Phonetic matching | American Soundex |

These choices are stored in the file and reported in `metadata`.

## Not supported

These requests return an error:

- Create, update, patch, delete, history, batch, transaction and operations
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
It binds to `127.0.0.1` by default. Expose it only behind a proxy that adds these.

## License

FHIRLeaf is licensed under Apache-2.0 (`LICENSE`, `NOTICE`).
The HL7 FHIR definitions are CC0 and are not redistributed.
"FHIR" and "HL7" are HL7 trademarks, and FHIRLeaf is not an HL7 product.
Package terminology and loaded data keep their own terms.
