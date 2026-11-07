# FHIRLeaf: A local FHIR R4 RESTful search engine

Read-only FHIR R4 RESTful Search over one SQLite file. No server process, JVM or database
service; Python 3.10+ standard library only. Answers with the JSON a FHIR server returns
(searchset Bundles, resources, OperationOutcomes, CapabilityStatement), in process or over HTTP.
Exclusions: [Not supported](#not-supported).

## Quick start

```bash
uv add "fhirleaf @ git+https://github.com/JYP0824/fhirleaf@v0.1.0"                    # 1. install
curl -fL https://hl7.org/fhir/R4/definitions.json.zip -o definitions-r4.json.zip      # 2. definitions
fhirleaf verify --cases --definitions definitions-r4.json.zip                         # 3. check install
fhirleaf load --input data/ --definitions definitions-r4.json.zip --output store.db   # 4. build store
fhirleaf get --db store.db "Patient?birthdate=ge1990"                                 # 5. search
fhirleaf serve --db store.db --port 8080                                              # 6. HTTP (optional)
```

## 1. Install

| Source | uv | pip |
|---|---|---|
| Git tag | `uv add "fhirleaf @ git+https://github.com/JYP0824/fhirleaf@v0.1.0"` | `pip install "git+https://github.com/JYP0824/fhirleaf@v0.1.0"` |
| PyPI (once published) | `uv add fhirleaf` | `pip install fhirleaf` |
| Wheel | `uv add ./fhirleaf-0.1.0-py3-none-any.whl` | `pip install ./fhirleaf-0.1.0-py3-none-any.whl` |
| Local checkout | `uv add --editable ./fhirleaf` | `pip install -e ./fhirleaf` |
| CLI only | `uv tool install "git+https://github.com/JYP0824/fhirleaf@v0.1.0"` | `pipx install "git+https://github.com/JYP0824/fhirleaf@v0.1.0"` |

uv records the Git form as
`[tool.uv.sources] fhirleaf = { git = "https://github.com/JYP0824/fhirleaf", tag = "v0.1.0" }`
and pins the commit in `uv.lock`. `--fulltext` needs SQLite FTS5 (included in python.org, uv and
conda Python builds).

## 2. Get the FHIR definitions

Not bundled (HL7, CC0). Use the version of your data:

```bash
curl -fL https://hl7.org/fhir/R4/definitions.json.zip   -o definitions-r4.json.zip
curl -fL https://hl7.org/fhir/R4B/definitions.json.zip  -o definitions-r4b.json.zip
curl -fL https://hl7.org/fhir/R5/definitions.json.zip   -o definitions-r5.json.zip
```

Search behaviour is R4 (4.0.1) and verified for R4 only; R4B/R5 definitions load but are not
verified.

## 3. Build a store

```bash
fhirleaf load --input data/ --definitions definitions-r4.json.zip --output store.db
fhirleaf load --input a.ndjson.gz --input bundles/ --package us-core.tgz \
              --definitions definitions-r4.json.zip --output store.db --fulltext --timezone +09:00
```

| Option | Effect |
|---|---|
| `--input PATH` (repeatable) | `*.ndjson[.gz]`, Bundle or resource `*.json[.gz]`, files or directories (recursive) |
| `--package FILE.tgz` (repeatable) | FHIR package: SearchParameters (replace core ones with the same code), ValueSets, CodeSystems |
| `--fulltext` | build the index for `_content` / `_text` |
| `--timezone` | zone for date/times without an offset: `UTC` (default), `+HH:MM`, POSIX rule (`EST5EDT,M3.2.0,M11.1.0`) |
| `--base-url URL` (repeatable) | this server's base; absolute references on it count as local (default `http://localhost/fhir`) |
| `--default-count N` / `--max-count N` | page size / maximum stored in the file (100 / 1000) |
| `--last-updated INSTANT` | `meta.lastUpdated` for every resource (default: load time) |
| `--no-hash` | skip SHA-256 of the source files |

Load rules: Bundles (transaction, batch, collection, searchset, history) are unpacked, `urn:uuid:`
references become `Type/id`, conditional references `Type?identifier=system|value` are resolved;
document/message Bundles are stored as is; identical duplicates are stored once, conflicting
duplicates stop the load; contained resources are indexed. The store is read-only afterwards and
can be opened by any number of processes.

## 4. Search

### CLI

```bash
fhirleaf get --db store.db "Observation?patient=Patient/1&code=http://loinc.org|8867-4&_sort=-date"
fhirleaf get --db store.db "Patient/1"
fhirleaf get --db store.db --lenient "Patient?id=1"      # unknown parameter ignored, reported
```

### HTTP

```bash
fhirleaf serve --db store.db --port 8080                              # http://127.0.0.1:8080/fhir
fhirleaf serve --db store.db --host 0.0.0.0 --public-base https://example.org/fhir
fhirleaf serve --db store.db --handling lenient --default-count 20 --max-count 200
curl "http://127.0.0.1:8080/fhir/Observation?patient=Patient/1&_count=10"
curl -X POST "http://127.0.0.1:8080/fhir/Observation/_search" -d "patient=Patient/1"
```

### Python

```python
import fhirleaf

store = fhirleaf.connect("store.db")                    # base_url=, default_count=, max_count=
status, bundle = store.get("Observation?patient=Patient/1&date=ge2020-01-01&_count=50")
status, page2 = store.get(bundle["link"][1]["url"].split("/fhir", 1)[1])     # follow 'next'
status, patient = store.get("Patient/1")
status, labs = store.get("Patient/1/Observation?category=laboratory")       # compartment
status, mixed = store.get("?_type=Condition,Procedure&_lastUpdated=ge2020") # whole system
status, bundle = store.get("Patient?id=1", handling="lenient")
status, bundle = store.post("Observation/_search", b"patient=Patient/1")
```

`get(path, handling="strict")` and `post(path, body, content_type, handling="strict")` return
`(status, json)`; `path` is relative to the base.

### Unknown parameters: strict or lenient

| Mode | Unknown/unsupported parameter | Set by |
|---|---|---|
| `strict` (default) | `400` OperationOutcome listing the supported parameters | — |
| `lenient` | ignored; reported in an `outcome` entry of the Bundle | `Prefer: handling=lenient`, `serve --handling lenient`, `get --lenient`, `handling="lenient"` |

FHIR leaves the default to the server (HAPI rejects, Google Cloud Healthcare API ignores). State
the mode when comparing results.

## 5. Check

```bash
fhirleaf verify --cases --definitions definitions-r4.json.zip [--output report.json]
fhirleaf verify --input data/ --definitions definitions-r4.json.zip --output verify/ \
                [--patients 5] [--seed 42] [--package us-core.tgz] [--fulltext] [--timezone ...]
fhirleaf support --definitions definitions-r4.json.zip [--package us-core.tgz] [--output support.json]
```

| Command | Checks | Current result |
|---|---|---|
| `verify --cases` | hand-computed specification cases on a built-in fixture, in process and over HTTP; fails unless every declared combination has a passing case | 243/243 cases, 139/139 combinations |
| `verify --input` | every request of a derived corpus on a patient subset (matches over all pages, order, total, includes, subsetting) against brute-force matching from re-extracted values | MIMIC-IV-on-FHIR Demo 1,240/1,240; Synthea + US Core 2,431/2,431 |
| `support` | status of every SearchParameter per type, with the reason when not supported | R4: 3,026 pairs, 2,873 supported, `_query` excluded (148), 5 composites unsupported |

`verify --input` shares the expression evaluator with the store; the specification cases are the
independent check. "Supported" in `support` means indexed with the type's matching rules, not
tested per parameter.

Compare with another FHIR server:

```bash
fhirleaf corpus  --db store.db --output requests.jsonl [--patients 5] [--seed 42]
fhirleaf record  --base-url http://other-server/fhir --requests requests.jsonl --output recorded.jsonl
fhirleaf compare --db store.db --recorded recorded.jsonl --report report.json
```

## Supported search

### Interactions

| Request | Answer |
|---|---|
| `GET [base]/metadata` | CapabilityStatement (lists exactly the parameters the store answers) |
| `GET [base]/[type]?params` | searchset Bundle |
| `GET [base]/[compartment]/[id]/[type]?params` | compartment search (CompartmentDefinitions) |
| `GET [base]?params`, `GET [base]?_type=A,B&params` | whole-system search (common parameters; `_sort` only `_id`) |
| `GET [base]/[type]/[id]` | resource, `ETag: W/"1"`, `Last-Modified` |
| `GET [base]/[type]/[id]/_history/1` | vread (one stored version; no history kept) |
| `GET` a `next`/`previous` link | page (opaque links) |
| `HEAD` | headers only |
| `POST [base]/[type]/_search`, `POST [base]/_search`, `POST [base]/[comp]/[id]/[type]/_search` | search; URL + `application/x-www-form-urlencoded` body (else `415`; > 1 MB `413`) |
| `PUT`, `PATCH`, `DELETE`, `OPTIONS`, other `POST` | `405`, `Allow: GET, HEAD, POST` |
| non-JSON `Accept` / `_format` | `406` |
| unknown type or id | `404` |
| invalid or unsupported search | `400` (strict) |

Responses: `application/fhir+json;charset=utf-8`.

### Parameter types

| Type | Values | Modifiers | Prefixes |
|---|---|---|---|
| token | `code`, `system\|code`, `\|code`, `system\|`; `:of-type` `system\|code\|value` | `:missing` `:not` `:text` `:in` `:not-in` `:of-type` | – |
| reference | `Type/id`, `id`, absolute URL, canonical `url[\|version]`; chains `a.b`, `a:Type.b`, multi-level, through contained `#id`; `_has:Type:param:param` | `:missing` `:[type]` `:identifier` `:below` `:above` | – |
| date | `YYYY`, `YYYY-MM`, `YYYY-MM-DD`, `…Thh:mm[:ss[.f]]`, optional `Z`/`±hh:mm` | `:missing` | `eq ne gt lt ge le sa eb ap` |
| string | starts-with, case/accent-insensitive; phonetic parameters: Soundex of a name word | `:missing` `:exact` `:contains` | – |
| quantity | `n`, `n\|system\|code`, `n\|\|code` | `:missing` | `eq ne gt lt ge le sa eb ap` |
| number | `n` | `:missing` | `eq ne gt lt ge le sa eb ap` |
| uri | URI | `:missing` `:below` `:above` | – |
| composite | `a$b`: token + token/quantity/date/string; one value per parameter, repeat for AND | `:missing` | of the 2nd component |
| special | `near=lat\|long[\|distance[\|km\|m\|mi]]` | `:missing` | – |

| Rule | Behaviour |
|---|---|
| OR / AND | `a,b` OR; repeated parameter AND; `400` where the definition says `multipleOr`/`multipleAnd = false` |
| Escapes | `\,` `\|` `\$` `\\` (also inside composites); empty value ignored |
| `_id`, `_lastUpdated` | ids; dates |
| `_list=id` | resources referenced by the List's entries; unknown List `404`; `$functional` lists `400` |
| `_content` / `_text` | with `--fulltext`: all words of a value, commas OR; no phrase/wildcard/boolean syntax |
| References | relative, absolute on `--base-url` (= relative), other absolute URLs, canonical `url\|version` |
| Dates | instant ranges at written precision (to microseconds); offset-less values in the store zone |
| Quantities | intervals: comparator one-sided, Range low..high; units as recorded (no conversion) |
| `:in` / `:not-in` | ValueSets expanded at load; not completely expandable → `400` |
| `:below` / `:above` (reference) | along the target type's own reference (e.g. `Location.partOf`); none → `400` |

### Result parameters

| Parameter | Accepted values |
|---|---|
| `_count` | ≥ 0, capped at the maximum; `0` = total only |
| `_sort` | parameters, `-` descending (not composite/special) |
| `_total` | `none`, `estimate`, `accurate` (always exact) |
| `_summary` | `true`, `false`, `text`, `data`, `count` |
| `_elements` | element names (not with `_summary`) |
| `_include`, `_revinclude` | `Type:param[:target]`, `Type:*`, `*`; `:iterate`/`:recurse` to closure; each resource once |
| `_contained` | `false` (default), `true`, `both` |
| `_containedType` | `container` (default; no `_sort`), `contained` (`fullUrl` = container + `#id`) |
| `_format` | `json`, `application/json`, `application/fhir+json` (else `406`) |
| `_pretty` | `true`, `false` (always compact) |
| other `_` parameters, modifiers on these | `400` |

### Server choices (stored in the file, reported in `metadata`)

| Choice | Default | Change with |
|---|---|---|
| zone for offset-less date/times | `UTC` | `load --timezone` |
| page size / maximum | 100 / 1000 | `load` or `serve --default-count/--max-count`, `connect(...)` |
| order without `_sort` | load order | – |
| missing sort values | last | – |
| `meta.lastUpdated`, `ap` reference instant | load time | `load --last-updated` |
| `near` without distance | 1 km | – |
| phonetic matching | American Soundex | – |
| `ap` | ±10% of the distance from the reference instant | – |

## Not supported

Error (`400`/`404`/`405`):

- create, update, patch, delete, history, batch/transaction, operations (`$everything`, `$expand`, …)
- `_query` (no named queries defined), `_filter` (planned)
- token `:above`/`:below` (planned for loaded CodeSystems; external hierarchies such as SNOMED CT/LOINC stay an error)
- composites `DocumentReference.relationship` and the four `MolecularSequence` coordinates (planned)
- SearchParameters navigating through `resolve()` (packages only; core R4 uses only `resolve() is Type`, supported)
- non-JSON formats; R5-only features (string `:text`, `:code-text`, …)

Not searchable (cannot be detected from the request):

- UCUM unit conversion (`mg` does not match `g`)
- SampledData values
- the repeated hour at a daylight-saving change for offset-less times (first occurrence used)

## Develop and release

```bash
uv build                                     # dist/fhirleaf-X.Y.Z.tar.gz and .whl
uv venv /tmp/check && uv pip install --python /tmp/check dist/*.whl
/tmp/check/bin/fhirleaf verify --cases --definitions definitions-r4.json.zip
git tag vX.Y.Z && git push --tags            # Git installs pin this tag
uv publish                                   # PyPI (token required; versions are immutable)
```

## Security

`serve` has no authentication, authorisation, TLS or rate limiting; it binds to `127.0.0.1` by
default. Expose it only behind a proxy that provides these, to users allowed to see all data.

## License

- Code: Apache-2.0 (`LICENSE`, `NOTICE`).
- HL7 FHIR definitions: CC0, not redistributed. "FHIR" and "HL7" are HL7 trademarks; FHIRLeaf is
  not an HL7 product.
- Package terminology (SNOMED CT, LOINC, RxNorm, …) and loaded data (e.g. MIMIC-IV-on-FHIR, which
  requires credentialed access) keep their own terms; a store contains the loaded data in full.
