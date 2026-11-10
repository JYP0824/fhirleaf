"""Specification cases for the declared search contract (README "Search contract").

A small hand-written fixture and, for every declared combination (parameter type x value form x
modifier x prefix, result parameters, interactions, HTTP framing, rejections), requests whose
expected results were worked out by hand from the FHIR R4 rules - not produced by running the
engine. `fhirlite verify --cases` builds the fixture store, runs every case in process and over
HTTP, and checks that each declared combination has at least one passing case.

Fixture settings: zone UTC, store instant 2026-01-01T00:00:00Z, full text on, page 4 / max 6.
"""
from __future__ import annotations

L, U, S = "http://loinc.org", "http://unitsofmeasure.org", "http://snomed.info/sct"
P = "http://example.org/StructureDefinition/"
CAT = "http://terminology.hl7.org/CodeSystem/observation-category"


def _obs(oid, code, *, subject="Patient/p1", status="final", **fields):
    r = {"resourceType": "Observation", "id": oid, "status": status, "code": {"coding": [code]}}
    if subject:
        r["subject"] = {"reference": subject} if isinstance(subject, str) else subject
    r.update(fields)
    return r


def _cat(code):
    return [{"coding": [{"system": CAT, "code": code}]}]


HR = {"system": L, "code": "8867-4", "display": "Heart rate"}
GLU = {"system": L, "code": "2339-0", "display": "Glucose"}
FIXTURE = [
    {"resourceType": "Patient", "id": "p1", "meta": {"profile": [P + "patient-a"]},
     "identifier": [{"type": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/v2-0203", "code": "MR"}]},
                     "system": "urn:mrn", "value": "111"}], "name": [{"family": "Müller", "given": ["Anna"]}],
     "gender": "female", "birthDate": "1980-05-15", "address": [{"city": "Boston"}],
     "maritalStatus": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/v3-MaritalStatus", "code": "M"}]}},
    {"resourceType": "Patient", "id": "p2", "meta": {"profile": [P + "patient-b"]},
     "identifier": [{"system": "urn:mrn", "value": "222"}, {"system": "urn:alt", "value": "a,b"}],
     "name": [{"family": "Mueller", "given": ["Bob"]}], "gender": "male", "birthDate": "1990",
     "managingOrganization": {"reference": "Organization/org1"}},
    {"resourceType": "Patient", "id": "p3", "identifier": [{"system": "urn:esc", "value": "x|y"}],
     "name": [{"family": "Smith", "given": ["Carl"]}], "gender": "other", "birthDate": "2000-01-01",
     "deceasedDateTime": "2023-01-01"},
    {"resourceType": "Organization", "id": "org1", "name": "General Hospital"},
    {"resourceType": "Location", "id": "loc1", "name": "Ward A", "position": {"latitude": 42.0, "longitude": -71.0}},
    {"resourceType": "Location", "id": "loc2", "name": "Ward B", "position": {"latitude": 42.0, "longitude": -71.05}},
    {"resourceType": "Location", "id": "loc3", "name": "Ward C"},
    {"resourceType": "Location", "id": "loc4", "name": "Room A1", "partOf": {"reference": "Location/loc1"}},
    {"resourceType": "Location", "id": "loc5", "name": "Bed A1-1", "partOf": {"reference": "Location/loc4"}},
    {"resourceType": "Encounter", "id": "e3", "status": "finished",
     "class": {"system": "http://terminology.hl7.org/CodeSystem/v3-ActCode", "code": "AMB"},
     "subject": {"reference": "Patient/p3"}, "period": {"start": "2019-01-01T00:00:00Z", "end": "2019-01-02T00:00:00Z"},
     "location": [{"location": {"reference": "Location/loc5"}}]},
    {"resourceType": "Invoice", "id": "inv1", "status": "issued", "subject": {"reference": "Patient/p3"},
     "totalGross": {"value": 40, "currency": "USD"}},
    {"resourceType": "ServiceRequest", "id": "sr1", "status": "active", "intent": "order", "subject": {"reference": "Patient/p3"},
     "occurrenceTiming": {"repeat": {"boundsPeriod": {"start": "2013-01-31", "end": "2013-03-24"}, "frequency": 1,
                                     "period": 2, "periodUnit": "d"}}},
    {"resourceType": "ServiceRequest", "id": "sr2", "status": "active", "intent": "order", "subject": {"reference": "Patient/p3"},
     "occurrenceTiming": {"event": ["2013-01-31", "2013-03-24"]}},
    {"resourceType": "Group", "id": "loc2", "type": "person", "actual": True},
    {"resourceType": "ValueSet", "id": "vs1", "url": "http://example.org/vs/1", "version": "2.0", "status": "active"},
    {"resourceType": "Practitioner", "id": "pr2", "name": [{"family": "Patient_123"}]},
    {"resourceType": "Practitioner", "id": "pr3", "name": [{"family": "Patient_456"}]},
    {"resourceType": "Practitioner", "id": "pr1", "name": [{"family": "Carreno Quinones", "given": ["Jose-Maria"]},
                                                       {"family": "O'Brien"}]},
    {"resourceType": "List", "id": "list1", "status": "current", "mode": "working",
     "entry": [{"item": {"reference": "Observation/o1"}}, {"item": {"reference": "Observation/o3"}},
               {"item": {"reference": "Condition/c1"}}]},
    {"resourceType": "MedicationRequest", "id": "mr2", "status": "active", "intent": "order",
     "contained": [{"resourceType": "Medication", "id": "med1",
                    "code": {"coding": [{"system": "http://www.nlm.nih.gov/research/umls/rxnorm", "code": "1191"}]}}],
     "medicationReference": {"reference": "#med1"}, "subject": {"reference": "Patient/p2"}},
    {"resourceType": "Encounter", "id": "e1", "status": "finished",
     "class": {"system": "http://terminology.hl7.org/CodeSystem/v3-ActCode", "code": "AMB"},
     "subject": {"reference": "Patient/p1"}, "period": {"start": "2020-01-01T08:00:00Z", "end": "2020-01-03T12:00:00Z"},
     "location": [{"location": {"reference": "Location/loc1"}}], "serviceProvider": {"reference": "Organization/org1"}},
    {"resourceType": "Encounter", "id": "e2", "status": "in-progress",
     "class": {"system": "http://terminology.hl7.org/CodeSystem/v3-ActCode", "code": "IMP"},
     "subject": {"reference": "Patient/p2"}, "period": {"start": "2021-06-01T00:00:00Z"}},
    _obs("o1", HR, category=_cat("vital-signs"), encounter={"reference": "Encounter/e1"},
         effectiveDateTime="2020-01-01T10:00:00Z", valueQuantity={"value": 72, "system": U, "code": "/min", "unit": "beats/minute"}),
    _obs("o2", HR, category=_cat("vital-signs"), effectiveDateTime="2020-01-01T10:00:00.150Z",
         valueQuantity={"value": 80.5, "system": U, "code": "/min", "unit": "/min"}),
    _obs("o3", GLU, category=_cat("laboratory"),
         effectivePeriod={"start": "2020-01-02T00:00:00Z", "end": "2020-01-03T06:00:00Z"},
         valueQuantity={"value": 5, "comparator": "<", "system": U, "code": "mmol/L", "unit": "millimole per liter"}),
    _obs("o4", GLU, subject="http://localhost/fhir/Patient/p2", status="preliminary", category=_cat("laboratory"),
         effectiveDateTime="2021-06-01T09:00:00+02:00", valueQuantity={"value": 7.25, "system": U, "code": "mmol/L", "unit": "mmol/L"}),
    _obs("o5", {"system": "urn:esc", "code": "a|b"}, subject="Patient/p2", effectiveDateTime="2021-06-02",
         valueCodeableConcept={"coding": [{"system": S, "code": "373066001", "display": "Yes"}]}),
    _obs("o6", {"system": L, "code": "8867-4"}, subject="http://other.org/fhir/Patient/p1",
         effectiveDateTime="2019-12-31T23:00:00Z", valueQuantity={"value": 90, "system": U, "code": "/min"}),
    _obs("o7", {"system": "urn:esc", "code": "x,y"}, subject="Patient/p3", status="amended",
         effectiveDateTime="2022-03-04T05:06:07Z", valueString="Hello World"),
    _obs("o8", {"system": L, "code": "85354-9", "display": "Blood pressure panel"}, effectiveDateTime="2020-01-01T11:00:00Z",
         component=[{"code": {"coding": [{"system": L, "code": "8480-6"}]}, "valueQuantity": {"value": 120, "system": U, "code": "mm[Hg]"}},
                    {"code": {"coding": [{"system": L, "code": "8462-4"}]}, "valueQuantity": {"value": 80, "system": U, "code": "mm[Hg]"}}]),
    _obs("o9", {"system": "urn:local", "code": "dt"}, effectiveDateTime="2020-02-02", valueDateTime="2020-02-02T02:02:02Z"),
    _obs("o11", {"system": L, "code": "8867-4"}, subject={"identifier": {"system": "urn:mrn", "value": "333"}},
         valueQuantity={"value": 60, "system": U, "code": "/min"}),
    _obs("o12", {"code": "nosys"}, subject=None),
    _obs("o13", {"system": "urn:local", "code": "x"}, subject="Patient/p3", effectiveDateTime="2020-01-02T12:00:00Z"),
    {"resourceType": "Condition", "id": "c1", "subject": {"reference": "Patient/p1"},
     "text": {"status": "generated", "div": "<div xmlns=\"http://www.w3.org/1999/xhtml\">Patient has <b>diabetes</b> mellitus</div>"},
     "clinicalStatus": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/condition-clinical", "code": "active"}]},
     "code": {"coding": [{"system": S, "code": "44054006", "display": "Diabetes mellitus type 2"}]},
     "onsetAge": {"value": 45, "system": U, "code": "a"}, "recordedDate": "2020-01-01"},
    {"resourceType": "Condition", "id": "c2", "subject": {"reference": "Patient/p2"},
     "code": {"coding": [{"system": S, "code": "38341003", "display": "Hypertension"}]},
     "onsetRange": {"low": {"value": 30, "system": U, "code": "a"}, "high": {"value": 40, "system": U, "code": "a"}}},
    {"resourceType": "Medication", "id": "m1", "code": {"coding": [{"system": "http://www.nlm.nih.gov/research/umls/rxnorm", "code": "860975"}]},
     "ingredient": [{"itemReference": {"reference": "Medication/m2"}}]},
    {"resourceType": "Medication", "id": "m2", "code": {"coding": [{"system": "http://www.nlm.nih.gov/research/umls/rxnorm", "code": "6809"}]}},
    {"resourceType": "MedicationRequest", "id": "mr1", "status": "active", "intent": "order",
     "medicationReference": {"reference": "Medication/m1"}, "subject": {"reference": "Patient/p1"},
     "encounter": {"reference": "Encounter/e1"}, "authoredOn": "2020-01-01"},
    {"resourceType": "QuestionnaireResponse", "id": "qr1", "status": "completed",
     "questionnaire": "http://example.org/Questionnaire/q1|2.0", "subject": {"reference": "Patient/p1"}},
    {"resourceType": "RiskAssessment", "id": "ra1", "status": "final", "subject": {"reference": "Patient/p1"},
     "prediction": [{"probabilityDecimal": 0.25}]},
    {"resourceType": "RiskAssessment", "id": "ra2", "status": "final", "subject": {"reference": "Patient/p2"},
     "prediction": [{"probabilityDecimal": 0.7}]},
]
PACKAGE = [
    {"resourceType": "ValueSet", "id": "hr", "url": "http://example.org/vs/hr", "status": "active",
     "compose": {"include": [{"system": L, "concept": [{"code": "8867-4"}]}]}},
    {"resourceType": "ValueSet", "id": "partial", "url": "http://example.org/vs/partial", "status": "active",
     "expansion": {"timestamp": "2020-01-01", "total": 2, "contains": [{"system": L, "code": "8867-4"}]}},
]
SETTINGS = {"timezone": "UTC", "last_updated": "2026-01-01T00:00:00.000+00:00", "default_count": 4, "max_count": 6}

OBS = ["o1", "o2", "o3", "o4", "o5", "o6", "o7", "o8", "o9", "o11", "o12", "o13"]
P1_OBS = ["o1", "o2", "o3", "o8", "o9"]
NOT_HR = ["o3", "o4", "o5", "o7", "o8", "o9", "o12", "o13"]


def _ids(rtype, ids):
    return [f"{rtype}/{i}" for i in ids]


def O(*ids):
    return _ids("Observation", ids)


def Pt(*ids):
    return _ids("Patient", ids)


# Each case: id, combination keys it covers, request (path; or http dict), expectation.
# expectation keys: status, match (all pages, as a set unless order=True), include, total, no_total,
# first_page (entries on page 1 only), next, keys (first match's element names), subsetted, outcome.
C = []


def case(cid, combos, path, **expect):
    C.append({"id": cid, "keys": combos.split(), "path": path, "expect": {"status": 200, **expect}})


def error(cid, combos, path, status, **kw):
    C.append({"id": cid, "keys": combos.split(), "path": path, "expect": {"status": status}, **kw})


def http(cid, combos, method, path, status, headers=None, body=None, raw=None, **expect):
    C.append({"id": cid, "keys": combos.split(), "http": {"method": method, "path": path, "headers": headers or {},
                                                         "body": body, "raw": raw}, "expect": {"status": status, **expect}})


# ---- token -------------------------------------------------------------------------------
case("token-code", "token:code", "Observation?code=8867-4", match=O("o1", "o2", "o6", "o11"))
case("token-system-code", "token:system|code", "Observation?code=http://loinc.org|8867-4", match=O("o1", "o2", "o6", "o11"))
case("token-nosystem", "token:|code", "Observation?code=|nosys", match=O("o12"))
case("token-system-only", "token:system|", "Observation?code=http://loinc.org|", match=O("o1", "o2", "o3", "o4", "o6", "o8", "o11"))
case("token-not", "token:not", "Observation?code:not=8867-4", match=O(*NOT_HR))
case("token-text", "token:text", "Observation?code:text=heart", match=O("o1", "o2"))
case("token-missing-true", "token:missing", "Observation?category:missing=true", match=O("o5", "o6", "o7", "o8", "o9", "o11", "o12", "o13"))
case("token-missing-false", "token:missing", "Observation?category:missing=false", match=O("o1", "o2", "o3", "o4"))
case("token-in", "token:in", "Observation?code:in=http://example.org/vs/hr", match=O("o1", "o2", "o6", "o11"))
case("token-not-in", "token:not-in", "Observation?code:not-in=http://example.org/vs/hr", match=O(*NOT_HR))
case("token-required-binding-system", "token:system|code", "Observation?status=http://hl7.org/fhir/observation-status|final",
     match=O("o1", "o2", "o3", "o5", "o6", "o8", "o9", "o11", "o12", "o13"))
case("token-identifier", "token:system|code", "Patient?identifier=urn:mrn|111", match=Pt("p1"))
case("phonetic", "string:phonetic", "Patient?phonetic=Mueller", match=Pt("p1", "p2"))
case("phonetic-accent", "string:phonetic", "Patient?phonetic=M%C3%BCler", match=Pt("p1", "p2"))
case("phonetic-no-match", "string:phonetic", "Patient?phonetic=Anderson", match=[])
error("err-phonetic-modifier", "reject:bad-modifier", "Patient?phonetic:exact=Mueller", 400)
error("err-phonetic-no-letters", "reject:bad-value", "Patient?phonetic=%20-%20", 400)
case("phonetic-digits", "string:phonetic", "Practitioner?phonetic=Patient_123", match=["Practitioner/pr2"])
case("phonetic-digits-sound", "string:phonetic", "Practitioner?phonetic=Patyent_123", match=["Practitioner/pr2"])
case("phonetic-letters-only", "string:phonetic", "Practitioner?phonetic=Patient", match=["Practitioner/pr2", "Practitioner/pr3"])
case("phonetic-or", "string:phonetic", "Practitioner?phonetic=Patient_123,Patient_456",
     match=["Practitioner/pr2", "Practitioner/pr3"])
case("composite-and-repeat", "composite:token$quantity common:and",
     "Observation?code-value-quantity=http://loinc.org|8867-4$gt75&code-value-quantity=http://loinc.org|8867-4$lt85",
     match=O("o2"))
error("err-composite-multiple-or", "reject:multiple-or",
      "Observation?code-value-quantity=http://loinc.org|8867-4$gt50,http://loinc.org|2339-0$gt1", 400)
case("token-of-type", "token:of-type", "Patient?identifier:of-type=http://terminology.hl7.org/CodeSystem/v2-0203|MR|111",
     match=Pt("p1"))
case("token-of-type-untyped", "token:of-type", "Patient?identifier:of-type=http://terminology.hl7.org/CodeSystem/v2-0203|MR|222",
     match=[])
case("token-boolean-expression", "token:code", "Patient?deceased=true", match=Pt("p3"))
case("token-boolean-false", "token:code", "Patient?deceased=false", match=Pt("p1", "p2"))
# ---- common value syntax -------------------------------------------------------------------
case("escape-pipe", "common:escape", "Observation?code=urn:esc|a\\|b", match=O("o5"))
case("escape-comma", "common:escape", "Observation?code=urn:esc|x\\,y", match=O("o7"))
case("escape-comma-identifier", "common:escape", "Patient?identifier=urn:alt|a\\,b", match=Pt("p2"))
case("escape-pipe-identifier", "common:escape", "Patient?identifier=urn:esc|x\\|y", match=Pt("p3"))
case("or-comma", "common:or", "Observation?code=2339-0,85354-9", match=O("o3", "o4", "o8"))
case("and-repeat", "common:and", "Observation?category=vital-signs&category=laboratory", match=[])
case("and-repeat-date", "common:and date:ge date:le", "Observation?date=ge2020-01-01&date=le2020-01-01", match=O("o1", "o2", "o8"))
case("empty-value-ignored", "common:empty", "Patient?name=&gender=female", match=Pt("p1"))
case("id", "common:_id", "Observation?_id=o1,o3", match=O("o1", "o3"))
case("last-updated-ge", "common:_lastUpdated", "Patient?_lastUpdated=ge2025", match=Pt("p1", "p2", "p3"))
case("last-updated-lt", "common:_lastUpdated", "Patient?_lastUpdated=lt2025", match=[])
# ---- reference ---------------------------------------------------------------------------
case("ref-type-id", "reference:Type/id", "Observation?subject=Patient/p1", match=O(*P1_OBS))
case("ref-id-only", "reference:id", "Observation?subject=p1", match=O(*P1_OBS))
error("err-ref-id-ambiguous", "reject:ambiguous-id", "Observation?subject=loc2", 400)
case("ref-id-typed-not-ambiguous", "reference:Type/id", "Observation?subject=Location/loc2", match=[])
case("ref-absolute-local", "reference:absolute-local", "Observation?subject=http://localhost/fhir/Patient/p2", match=O("o4", "o5"))
case("ref-relative-matches-absolute-local", "reference:absolute-local", "Observation?subject=Patient/p2", match=O("o4", "o5"))
case("ref-absolute-external", "reference:absolute-external", "Observation?subject=http://other.org/fhir/Patient/p1", match=O("o6"))
case("ref-type-modifier", "reference:type-modifier", "Observation?subject:Patient=p1", match=O(*P1_OBS))
case("ref-type-modifier-conflict", "reference:type-modifier", "Observation?subject:Group=p1", match=[])
case("ref-type-modifier-url-conflict", "reference:type-modifier", "Observation?subject:Group=http://localhost/fhir/Patient/p2", match=[])
case("ref-identifier", "reference:identifier", "Observation?subject:identifier=urn:mrn|333", match=O("o11"))
case("ref-missing", "reference:missing", "Observation?subject:missing=true", match=O("o12"))
case("ref-patient-param", "reference:Type/id", "Observation?patient=p1", match=O(*P1_OBS))
case("ref-canonical", "reference:canonical", "QuestionnaireResponse?questionnaire=http://example.org/Questionnaire/q1",
     match=["QuestionnaireResponse/qr1"])
case("ref-canonical-version", "reference:canonical-version", "QuestionnaireResponse?questionnaire=http://example.org/Questionnaire/q1|2.0",
     match=["QuestionnaireResponse/qr1"])
case("ref-canonical-other-version", "reference:canonical-version", "QuestionnaireResponse?questionnaire=http://example.org/Questionnaire/q1|9.9", match=[])
case("chain-typed", "reference:chain-typed", "Observation?subject:Patient.identifier=urn:mrn|111", match=O(*P1_OBS))
case("chain", "reference:chain", "Observation?patient.name=muller", match=O(*P1_OBS))
case("chain-multilevel", "reference:chain-multilevel", "Observation?encounter.service-provider.name=General", match=O("o1"))
case("ref-below", "reference:below", "Encounter?location:below=Location/loc1", match=["Encounter/e1", "Encounter/e3"])
case("ref-plain-not-hierarchical", "reference:Type/id", "Encounter?location=Location/loc1", match=["Encounter/e1"])
case("ref-above", "reference:above", "Encounter?location:above=Location/loc5", match=["Encounter/e1", "Encounter/e3"])
case("ref-above-middle", "reference:above", "Encounter?location:above=Location/loc4", match=["Encounter/e1"])
case("ref-below-circular-param", "reference:below", "Location?partof:below=Location/loc1", match=["Location/loc4", "Location/loc5"])
case("list", "common:_list", "Observation?_list=list1", match=O("o1", "o3"))
case("list-other-type", "common:_list", "Condition?_list=list1", match=["Condition/c1"])
case("list-and", "common:_list common:and", "Observation?_list=list1&code=2339-0", match=O("o3"))
case("has", "reference:has", "Patient?_has:Observation:patient:code=2339-0", match=Pt("p1", "p2"))
case("has-condition", "reference:has", "Patient?_has:Condition:subject:code=38341003", match=Pt("p2"))
# ---- date --------------------------------------------------------------------------------
case("date-year", "date:year date:eq", "Observation?date=2020", match=O("o1", "o2", "o3", "o8", "o9", "o13"))
case("date-month", "date:month", "Observation?date=2020-01", match=O("o1", "o2", "o3", "o8", "o13"))
case("date-day", "date:day", "Observation?date=2020-01-01", match=O("o1", "o2", "o8"))
case("date-minute", "date:minute date:Z", "Observation?date=2020-01-01T10:00Z", match=O("o1", "o2"))
case("date-second", "date:second", "Observation?date=2020-01-01T10:00:00Z", match=O("o1", "o2"))
case("date-fraction", "date:fraction", "Observation?date=2020-01-01T10:00:00.1Z", match=O("o2"))
case("date-offset", "date:offset", "Observation?date=2021-06-01T09:00:00%2B02:00", match=O("o4"))
case("date-offset-utc", "date:Z", "Observation?date=2021-06-01T07:00:00Z", match=O("o4"))
case("date-no-offset", "date:no-offset", "Observation?date=2021-06-01T07:00:00", match=O("o4"))
case("date-eq", "date:eq", "Observation?date=eq2020-01-02", match=O("o13"))
case("date-ne", "date:ne", "Observation?date=ne2020-01-02", match=O("o1", "o2", "o3", "o4", "o5", "o6", "o7", "o8", "o9"))
case("date-gt", "date:gt", "Observation?date=gt2020-01-02", match=O("o3", "o4", "o5", "o7", "o9"))
case("date-lt", "date:lt", "Observation?date=lt2020-01-02", match=O("o1", "o2", "o6", "o8"))
case("date-ge", "date:ge", "Observation?date=ge2020-01-02", match=O("o3", "o4", "o5", "o7", "o9", "o13"))
case("date-le", "date:le", "Observation?date=le2020-01-02", match=O("o1", "o2", "o6", "o8", "o13"))
case("date-sa", "date:sa", "Observation?date=sa2020-01-02", match=O("o4", "o5", "o7", "o9"))
case("date-eb", "date:eb", "Observation?date=eb2020-01-02", match=O("o1", "o2", "o6", "o8"))
case("date-ap", "date:ap", "Observation?date=ap2020-01-02", match=O("o1", "o2", "o3", "o6", "o8", "o9", "o13"))
case("date-missing", "date:missing", "Observation?date:missing=true", match=O("o11", "o12"))
case("date-open-period", "date:ge", "Encounter?date=ge2022", match=["Encounter/e2"])
case("date-period-lt", "date:lt", "Encounter?date=lt2021", match=["Encounter/e1", "Encounter/e3"])
case("date-date-element", "date:lt", "Patient?birthdate=lt1985", match=Pt("p1"))
case("date-partial-target", "date:year", "Patient?birthdate=1990", match=Pt("p2"))
# ---- string ------------------------------------------------------------------------------
case("string-default-accent", "string:default", "Patient?name=muller", match=Pt("p1"))
case("string-default-prefix", "string:default", "Patient?name=Mu", match=Pt("p1", "p2"))
case("string-exact", "string:exact", "Patient?name:exact=M%C3%BCller", match=Pt("p1"))
case("string-exact-case", "string:exact", "Patient?name:exact=muller", match=[])
case("string-contains", "string:contains", "Patient?name:contains=ell", match=Pt("p2"))
case("string-or", "string:default common:or", "Patient?name=Smith,Bob", match=Pt("p2", "p3"))
case("string-address", "string:default", "Patient?address-city=bos", match=Pt("p1"))
case("string-missing", "string:missing", "Patient?address-city:missing=true", match=Pt("p2", "p3"))
# ---- quantity ----------------------------------------------------------------------------
case("quantity-eq", "quantity:n quantity:eq quantity:point", "Observation?value-quantity=72", match=O("o1"))
case("quantity-eq-precision", "quantity:eq", "Observation?value-quantity=80.5", match=O("o2"))
case("quantity-lt-comparator", "quantity:lt quantity:comparator", "Observation?value-quantity=lt5", match=O("o3"))
case("quantity-gt", "quantity:gt", "Observation?value-quantity=gt75", match=O("o2", "o6"))
case("quantity-ge", "quantity:ge", "Observation?value-quantity=ge72", match=O("o1", "o2", "o6"))
case("quantity-le", "quantity:le quantity:comparator", "Observation?value-quantity=le60", match=O("o3", "o4", "o11"))
case("quantity-ne", "quantity:ne", "Observation?value-quantity=ne72", match=O("o2", "o3", "o4", "o6", "o11"))
case("quantity-sa", "quantity:sa", "Observation?value-quantity=sa80", match=O("o2", "o6"))
case("quantity-eb", "quantity:eb quantity:comparator", "Observation?value-quantity=eb7", match=O("o3"))
case("quantity-ap", "quantity:ap", "Observation?value-quantity=ap80", match=O("o1", "o2"))
case("quantity-system-code", "quantity:n|system|code", "Observation?value-quantity=7.25|http://unitsofmeasure.org|mmol/L", match=O("o4"))
case("quantity-code", "quantity:n||code", "Observation?value-quantity=lt10||mmol/L", match=O("o3", "o4"))
case("quantity-unit", "quantity:n||unit", "Observation?value-quantity=lt10||millimole%20per%20liter", match=O("o3"))
case("quantity-missing", "quantity:missing", "Observation?value-quantity:missing=true", match=O("o5", "o7", "o8", "o9", "o12", "o13"))
case("quantity-range-gt", "quantity:gt quantity:range", "Condition?onset-age=gt35", match=["Condition/c1", "Condition/c2"])
case("quantity-range-lt", "quantity:lt quantity:range", "Condition?onset-age=lt35", match=["Condition/c2"])
case("quantity-range-eq", "quantity:eq quantity:range", "Condition?onset-age=eq35", match=[])
case("quantity-age-code", "quantity:n||code", "Condition?onset-age=45||a", match=["Condition/c1"])
# ---- number ------------------------------------------------------------------------------
RA = lambda *ids: _ids("RiskAssessment", ids)  # noqa: E731
case("number-eq", "number:eq", "RiskAssessment?probability=0.25", match=RA("ra1"))
case("number-eq-precision", "number:eq", "RiskAssessment?probability=0.3", match=RA("ra1"))
case("number-ne", "number:ne", "RiskAssessment?probability=ne0.25", match=RA("ra2"))
case("number-gt", "number:gt", "RiskAssessment?probability=gt0.5", match=RA("ra2"))
case("number-lt", "number:lt", "RiskAssessment?probability=lt0.5", match=RA("ra1"))
case("number-ge", "number:ge", "RiskAssessment?probability=ge0.25", match=RA("ra1", "ra2"))
case("number-le", "number:le", "RiskAssessment?probability=le0.7", match=RA("ra1", "ra2"))
case("number-sa", "number:sa", "RiskAssessment?probability=sa0.25", match=RA("ra2"))
case("number-eb", "number:eb", "RiskAssessment?probability=eb0.7", match=RA("ra1"))
case("number-ap", "number:ap", "RiskAssessment?probability=ap0.25", match=RA("ra1"))
case("number-missing", "number:missing", "RiskAssessment?probability:missing=false", match=RA("ra1", "ra2"))
# ---- uri ---------------------------------------------------------------------------------
case("uri-exact", "uri:exact", "Patient?_profile=http://example.org/StructureDefinition/patient-a", match=Pt("p1"))
case("uri-below", "uri:below", "Patient?_profile:below=http://example.org/StructureDefinition/", match=Pt("p1", "p2"))
case("uri-above", "uri:above", "Patient?_profile:above=http://example.org/StructureDefinition/patient-a/v2", match=Pt("p1"))
case("uri-missing", "uri:missing", "Patient?_profile:missing=true", match=Pt("p3"))
# ---- composite ---------------------------------------------------------------------------
case("composite-quantity", "composite:token$quantity", "Observation?code-value-quantity=http://loinc.org|8867-4$gt75", match=O("o2", "o6"))
case("composite-concept", "composite:token$token common:escape", "Observation?code-value-concept=urn:esc|a\\|b$http://snomed.info/sct|373066001",
     match=O("o5"))
case("composite-date", "composite:token$date", "Observation?code-value-date=urn:local|dt$2020-02-02", match=O("o9"))
case("composite-string", "composite:token$string common:escape", "Observation?code-value-string=urn:esc|x\\,y$Hello", match=O("o7"))
case("composite-component-pairing", "composite:token$quantity", "Observation?component-code-value-quantity=http://loinc.org|8480-6$gt100",
     match=O("o8"))
case("composite-component-pairing-none", "composite:token$quantity", "Observation?component-code-value-quantity=http://loinc.org|8462-4$gt100",
     match=[])
# ---- special (near) ----------------------------------------------------------------------
LO = lambda *ids: _ids("Location", ids)  # noqa: E731
case("near-km", "special:near-km", "Location?near=42.0|-71.0|1|km", match=LO("loc1"))
case("near-km-wide", "special:near-km", "Location?near=42.0|-71.0|5|km", match=LO("loc1", "loc2"))
case("near-m", "special:near-m", "Location?near=42.0|-71.0|5000|m", match=LO("loc1", "loc2"))
case("near-mi", "special:near-mi", "Location?near=42.0|-71.0|2|mi", match=LO("loc1"))
case("near-default", "special:near-default", "Location?near=42.0|-71.0", match=LO("loc1"))
case("near-missing", "special:missing", "Location?near:missing=true", match=LO("loc3", "loc4", "loc5"))
# ---- full text ---------------------------------------------------------------------------
case("text-narrative", "fulltext:_text", "Condition?_text=diabetes", match=["Condition/c1"])
case("content-boolean-and", "fulltext:boolean", "Observation?_content=hello%20AND%20world", match=O("o7"))
case("content-boolean-or", "fulltext:boolean", "Observation?_content=(nosuchword%20OR%20hello)%20world", match=O("o7"))
case("content-boolean-not", "fulltext:boolean", "Observation?_content=hello%20NOT%20world", match=[])
case("content-phrase", "fulltext:boolean", "Observation?_content=%22hello%20world%22", match=O("o7"))
case("content-phrase-order", "fulltext:boolean", "Observation?_content=%22world%20hello%22", match=[])
error("err-content-syntax", "reject:bad-value", "Observation?_content=(hello", 400)
case("content", "fulltext:_content", "Condition?_content=hypertension", match=["Condition/c2"])
case("content-or", "fulltext:_content common:or", "Condition?_content=diabetes,hypertension", match=["Condition/c1", "Condition/c2"])
case("content-and", "fulltext:_content", "Condition?_content=diabetes%20hypertension", match=[])
# ---- result parameters -------------------------------------------------------------------
case("count-default", "result:_count interaction:paging", "Observation", first_page=4, next=True, total=12)
case("count-n", "result:_count", "Observation?_count=3", first_page=3, next=True, total=12)
case("count-cap", "result:_count", "Observation?_count=50", first_page=6, total=12)
case("count-zero", "result:_count", "Observation?_count=0", first_page=0, next=False, total=12)
case("count-zero-total-none", "result:_count", "Observation?_count=0&_total=none", first_page=0, total=12)
case("paging-all", "interaction:paging", "Observation?_count=5", match=O(*OBS), total=12)
case("sort-asc", "result:_sort", "Observation?_sort=date", order=True,
     match=O("o6", "o1", "o2", "o8", "o3", "o13", "o9", "o4", "o5", "o7", "o11", "o12"))
case("sort-desc", "result:_sort", "Observation?_sort=-date", order=True,
     match=O("o7", "o5", "o4", "o9", "o13", "o3", "o8", "o2", "o1", "o6", "o11", "o12"))
case("sort-multi", "result:_sort", "Observation?_sort=status,-date", order=True,
     match=O("o7", "o5", "o9", "o13", "o3", "o8", "o2", "o1", "o6", "o11", "o12", "o4"))
case("sort-id", "result:_sort", "Observation?_sort=_id", order=True,
     match=O("o1", "o11", "o12", "o13", "o2", "o3", "o4", "o5", "o6", "o7", "o8", "o9"))
case("total-none", "result:_total", "Observation?_total=none", no_total=True)
case("total-accurate", "result:_total", "Observation?_total=accurate", total=12)
case("total-estimate", "result:_total", "Observation?_total=estimate", total=12)
case("summary-count", "result:_summary", "Observation?_summary=count", first_page=0, total=12)
case("summary-true", "result:_summary", "Patient?_id=p1&_summary=true", subsetted=True,
     keys=["resourceType", "id", "meta", "identifier", "name", "gender", "birthDate", "address"])
case("summary-data", "result:_summary", "Condition?_id=c1&_summary=data", subsetted=True,
     keys=["resourceType", "id", "meta", "clinicalStatus", "code", "subject", "onsetAge", "recordedDate"])
case("summary-text", "result:_summary", "Condition?_id=c1&_summary=text", subsetted=True,
     keys=["resourceType", "id", "meta", "text", "subject"])
case("summary-false", "result:_summary", "Condition?_id=c1&_summary=false", subsetted=False,
     keys=["resourceType", "id", "meta", "text", "clinicalStatus", "code", "subject", "onsetAge", "recordedDate"])
case("elements", "result:_elements", "Observation?_id=o1&_elements=code", subsetted=True,
     keys=["resourceType", "id", "meta", "status", "code"])
case("elements-choice", "result:_elements", "Observation?_id=o1&_elements=value", subsetted=True,
     keys=["resourceType", "id", "meta", "status", "code", "valueQuantity"])
case("elements-choice-typed", "result:_elements", "Observation?_id=o1&_elements=valueQuantity", subsetted=True,
     keys=["resourceType", "id", "meta", "status", "code", "valueQuantity"])
case("include", "result:_include", "MedicationRequest?_id=mr1&_include=MedicationRequest:medication",
     match=["MedicationRequest/mr1"], include=["Medication/m1"])
case("include-iterate", "result:_include-iterate", "MedicationRequest?_id=mr1&_include=MedicationRequest:medication&_include:iterate=Medication:ingredient",
     match=["MedicationRequest/mr1"], include=["Medication/m1", "Medication/m2"])
case("include-target", "result:_include", "Observation?_id=o1&_include=Observation:subject:Patient", match=O("o1"), include=Pt("p1"))
case("include-wildcard", "result:_include", "Observation?_id=o1&_include=*", match=O("o1"), include=["Patient/p1", "Encounter/e1"])
case("include-type-wildcard", "result:_include include:wildcard", "Observation?_id=o1&_include=Observation:*", match=O("o1"),
     include=["Patient/p1", "Encounter/e1"])
case("revinclude-wildcard", "result:_revinclude include:wildcard", "Location?_id=loc5&_revinclude=*", match=["Location/loc5"],
     include=["Encounter/e3"])
case("revinclude-type-wildcard", "result:_revinclude include:wildcard", "Location?_id=loc4&_revinclude=Location:*",
     match=["Location/loc4"], include=["Location/loc5"])
case("include-iterate-closure", "result:_include-iterate", "Location?_id=loc5&_include:iterate=Location:partof",
     match=["Location/loc5"], include=["Location/loc4", "Location/loc1"])
case("include-overlap-once", "result:_include", "MedicationRequest?_id=mr1&_include=MedicationRequest:medication&_include=MedicationRequest:*",
     match=["MedicationRequest/mr1"], include_unique=True)
error("err-include-type-wildcard-other", "reject:result-param", "Observation?_include=Patient:*", 400)
case("include-absolute-local", "result:_include reference:absolute-local", "Observation?_id=o4&_include=Observation:subject",
     match=O("o4"), include=Pt("p2"))
case("revinclude", "result:_revinclude", "Patient?_id=p1&_revinclude=Observation:subject", match=Pt("p1"), include=O(*P1_OBS))
case("contained-false", "result:_contained", "Patient?_id=p1&_contained=false", match=Pt("p1"))
case("contained-default-excluded", "result:_contained", "Medication?code=1191", match=[])
case("contained-true-container", "result:_contained result:_containedType", "Medication?code=1191&_contained=true",
     match=["MedicationRequest/mr2"], total=1)
case("contained-true-contained", "result:_containedType", "Medication?code=1191&_contained=true&_containedType=contained",
     match=["Medication/med1"], total=1)
case("contained-true-only", "result:_contained", "Medication?code=860975&_contained=true", match=[])
case("contained-both", "result:_contained", "Medication?code=1191,860975&_contained=both",
     match=["Medication/m1", "MedicationRequest/mr2"])
case("contained-chain", "result:_contained reference:chain", "MedicationRequest?medication.code=1191",
     match=["MedicationRequest/mr2"])
case("contained-local-ref-not-missing", "reference:missing", "MedicationRequest?medication:missing=false&subject=Patient/p2",
     match=["MedicationRequest/mr2"])
case("contained-not-addressable", "reference:Type/id", "MedicationRequest?medication=Medication/med1", match=[])
case("contained-not-included", "result:_include", "MedicationRequest?_id=mr2&_include=MedicationRequest:medication",
     match=["MedicationRequest/mr2"], include=[])
error("err-contained-read", "reject:unknown-id", "Medication/med1", 404)
error("err-contained-type-alone", "reject:result-param", "Medication?_containedType=contained", 400)
error("err-contained-sort", "reject:result-param", "Medication?_contained=true&_sort=code", 400)
case("format-json", "result:_format", "Patient?_id=p1&_format=json", match=Pt("p1"))
case("pretty", "result:_pretty", "Patient?_id=p1&_pretty=true", match=Pt("p1"))
# ---- interactions ------------------------------------------------------------------------
case("read", "interaction:read", "Patient/p1", read="Patient/p1")
case("metadata", "interaction:metadata", "metadata", resource_type="CapabilityStatement")
case("format-fhir-plus", "result:_format", "metadata?_format=application/fhir+json", resource_type="CapabilityStatement")
case("read-percent-path", "interaction:read", "Patient/p%31", read="Patient/p1")
case("read-summary", "interaction:read-params", "Patient/p1?_summary=true", read="Patient/p1", read_subsetted=True)
case("read-elements", "interaction:read-params", "Patient/p1?_elements=gender", read="Patient/p1",
     read_keys=["resourceType", "id", "meta", "gender"])
case("read-format", "interaction:read-params", "Patient/p1?_format=json", read="Patient/p1", read_subsetted=False)
error("err-read-unknown-param", "interaction:read-params reject:unknown-param", "Patient/p1?bogus=1", 400)
case("read-unknown-param-lenient", "interaction:read-params handling:lenient", "Patient/p1?bogus=1", read="Patient/p1",
     handling="lenient")
case("compartment-all-types", "interaction:compartment-all", "Patient/p3/*?_type=Encounter,Invoice",
     match=["Encounter/e3", "Invoice/inv1"])
case("money", "quantity:money", "Invoice?totalgross=40", match=["Invoice/inv1"])
case("money-currency", "quantity:money", "Invoice?totalgross=40|urn:iso:std:iso:4217|USD", match=["Invoice/inv1"])
case("money-gt", "quantity:money", "Invoice?totalgross=gt40", match=[])
case("timing-bounds-ge", "date:timing", "ServiceRequest?occurrence=ge2013-02-01",
     match=["ServiceRequest/sr1", "ServiceRequest/sr2"])
case("timing-outer-sa", "date:timing", "ServiceRequest?occurrence=sa2013-02-01", match=[])
case("timing-outer-eq-month", "date:timing", "ServiceRequest?occurrence=eq2013-03", match=[])
case("timing-outer-eq-year", "date:timing", "ServiceRequest?occurrence=eq2013",
     match=["ServiceRequest/sr1", "ServiceRequest/sr2"])
case("token-case-insensitive", "token:case-insensitive", "Patient?gender=MALE", match=Pt("p2"))
case("uri-canonical-version", "uri:version", "ValueSet?url=http://example.org/vs/1|2.0", match=["ValueSet/vs1"])
case("uri-canonical-other-version", "uri:version", "ValueSet?url=http://example.org/vs/1|3.0", match=[])
error("err-uri-below-urn", "reject:urn-modifier", "ValueSet?url:below=urn:oid:1.2", 400)
case("string-family-part", "string:family-part", "Practitioner?family=Quinones", match=["Practitioner/pr1"])
case("string-family-part-exact", "string:family-part", "Practitioner?family:exact=Quinones", match=[])
case("string-punctuation", "string:punctuation", "Practitioner?family=obrien", match=["Practitioner/pr1"])
case("string-hyphen", "string:punctuation", "Practitioner?given=josemaria", match=["Practitioner/pr1"])
case("string-whitespace", "string:punctuation", "Practitioner?family=carreno%20%20%09quinones", match=["Practitioner/pr1"])
case("empty-result-params", "common:empty", "Patient?gender=male&_count=&_summary=&_sort=&_include=", match=Pt("p2"),
     self="/Patient?gender=male")
case("self-effective", "result:self", "Observation?code=8867-4&_count=100&_count=2&code=", self="/Observation?code=8867-4&_count=2")
case("self-count-capped", "result:self", "Observation?code=8867-4&_count=100", self="/Observation?code=8867-4&_count=6")
case("page-self", "result:self interaction:paging", "Observation?code=8867-4&_count=1", page_self=True)
case("type-and", "interaction:system", "?_type=Observation,Patient&_type=Patient&_id=p1", match=Pt("p1"))
case("type-and-empty", "interaction:system", "?_type=Observation&_type=Patient", match=[], total=0)
error("err-summary-text-include", "reject:result-param", "Observation?_include=Observation:subject&_summary=text", 400)
error("err-composite-missing", "reject:bad-modifier", "Observation?code-value-quantity:missing=true", 400)
case("lenient-sort-unknown", "handling:lenient", "Observation?code=8867-4&_sort=nosuch", handling="lenient", outcome=True)
case("lenient-chain-unknown", "handling:lenient", "Observation?code=8867-4&patient.nosuch=1", handling="lenient", outcome=True)
error("err-sort-unknown", "reject:unknown-param", "Observation?_sort=nosuch", 400)
case("vread", "interaction:vread", "Patient/p1/_history/1", read="Patient/p1")
error("err-vread-version", "interaction:vread reject:unknown-id", "Patient/p1/_history/2", 404)
error("err-history", "reject:history", "Patient/p1/_history", 400)
error("err-history-type", "reject:history", "Patient/_history", 400)
error("err-history-system", "reject:history", "_history", 400)
error("err-operation", "reject:operation", "Patient/p1/$everything", 400)
error("err-operation-type", "reject:operation", "Patient/$match", 400)
error("err-of-type-parts", "reject:bad-value", "Patient?identifier:of-type=MR|111", 400)
case("compartment", "interaction:compartment", "Patient/p1/Observation", match=O(*P1_OBS))
case("compartment-condition", "interaction:compartment", "Patient/p1/Condition", match=["Condition/c1"])
case("compartment-params", "interaction:compartment", "Patient/p2/Observation?code=2339-0", match=O("o4"))
case("system-type", "interaction:system", "?_type=Patient,Location&_id=p1,loc1", match=["Patient/p1", "Location/loc1"])
case("system-all-types", "interaction:system", "?_id=o1", match=O("o1"))
case("lenient-unknown", "handling:lenient", "Observation?foo=bar&code=2339-0", handling="lenient", match=O("o3", "o4"), outcome=True)
# ---- rejections (contract boundary) ------------------------------------------------------
error("err-unknown-param", "reject:unknown-param handling:strict", "Observation?foo=bar", 400)
error("err-unknown-type", "reject:unknown-type", "Foo?x=1", 404)
error("err-unknown-id", "reject:unknown-id", "Patient/nope", 404)
error("err-date", "reject:bad-value", "Observation?date=notadate", 400)
error("err-date-unicode", "reject:bad-value", "Observation?date=%D9%A2%D9%A0%D9%A2%D9%A0", 400)
error("err-number", "reject:bad-value", "RiskAssessment?probability=abc", 400)
error("err-number-unicode", "reject:bad-value", "RiskAssessment?probability=%D9%A5", 400)
error("err-token-parts", "reject:bad-value", "Observation?code=a|b|c", 400)
error("err-quantity-parts", "reject:bad-value", "Observation?value-quantity=5|mg", 400)
error("err-modifier-token", "reject:bad-modifier", "Observation?code:below=x", 400)
error("err-modifier-string", "reject:bad-modifier", "Patient?name:text=x", 400)
error("err-in-unknown", "reject:valueset", "Observation?code:in=http://example.org/vs/none", 400)
error("err-in-partial", "reject:valueset", "Observation?code:in=http://example.org/vs/partial", 400)
error("err-chain-not-reference", "reject:chain", "Observation?code.name=x", 400)
error("err-hierarchy-none", "reject:hierarchy", "Encounter?subject:below=Patient/p1", 200, note="Patient.link makes Patient hierarchical")
error("err-hierarchy-absent", "reject:hierarchy", "MedicationRequest?requester:below=Practitioner/x", 400)
case("list-unknown-empty", "common:_list", "Observation?_list=nope", match=[])
error("err-list-functional", "reject:list", "Observation?_list=$current-problems", 400)
error("err-has-not-reference", "reject:chain", "Patient?_has:Observation:code:code=1", 400)
error("err-summary-value", "reject:result-param", "Observation?_summary=garbage", 400)
error("err-count-negative", "reject:result-param", "Observation?_count=-1", 400)
error("err-control-modifier", "reject:result-param", "Observation?_count:x=1", 400)
error("err-summary-elements", "reject:result-param", "Observation?_summary=true&_elements=code", 400)
error("err-elements-name", "reject:result-param", "Observation?_elements=nosuch", 400)
error("err-include-source", "reject:result-param", "Observation?_include=Encounter:subject", 400)
error("err-sort-composite", "reject:result-param", "Observation?_sort=code-value-quantity", 400)
error("err-format-xml", "reject:format", "Observation?_format=xml", 406)
error("err-resolve-param", "reject:unknown-param", "Observation?_query=x", 400)
# ---- HTTP interface ----------------------------------------------------------------------
http("http-head", "interaction:head", "HEAD", "/Patient/p1", 200, empty_body=True, header="ETag")
http("http-read-headers", "interaction:read", "GET", "/Patient/p1", 200, header="Last-Modified")
http("http-post-type", "interaction:post-type", "POST", "/Observation/_search", 200,
     headers={"Content-Type": "application/x-www-form-urlencoded"}, body="code=8867-4", match=O("o1", "o2", "o6", "o11"))
http("http-post-url-and-body", "interaction:post-type common:and", "POST", "/Observation/_search?category=vital-signs", 200,
     headers={"Content-Type": "application/x-www-form-urlencoded"}, body="value-quantity=gt75", match=O("o2"))
http("http-post-system", "interaction:post-system", "POST", "/_search", 200,
     headers={"Content-Type": "application/x-www-form-urlencoded"}, body="_type=Patient&gender=male", match=Pt("p2"))
http("http-post-compartment", "interaction:post-compartment", "POST", "/Patient/p1/Observation/_search", 200,
     headers={"Content-Type": "application/x-www-form-urlencoded"}, body="", match=O(*P1_OBS))
http("http-put", "reject:method", "PUT", "/Patient/p1", 405, headers={"Content-Type": "application/fhir+json"}, body="{}")
http("http-delete", "reject:method", "DELETE", "/Patient/p1", 405)
http("http-post-not-search", "reject:method http:allow", "POST", "/Patient", 405,
     headers={"Content-Type": "application/x-www-form-urlencoded"}, body="x=1", header_equals={"Allow": "GET, HEAD"})
http("http-put-allow", "reject:method http:allow", "PUT", "/Patient/p1", 405, headers={"Content-Type": "application/fhir+json"},
     body="{}", header_equals={"Allow": "GET, HEAD"})
http("http-post-read-search", "reject:method", "POST", "/Patient/p1/_search", 400,
     headers={"Content-Type": "application/x-www-form-urlencoded"}, body="x=1")
http("http-accept-q0", "reject:format http:accept", "GET", "/metadata", 406, headers={"Accept": "application/fhir+json;q=0"})
http("http-accept-version", "reject:format http:accept", "GET", "/metadata", 406,
     headers={"Accept": "application/fhir+json; fhirVersion=3.0"})
http("http-accept-q-order", "http:accept", "GET", "/Patient/p1", 200,
     headers={"Accept": "application/json;q=0.5, application/fhir+json"},
     header_equals={"Content-Type": "application/fhir+json;charset=utf-8"})
http("http-generic-json", "http:generic-type", "GET", "/Patient/p1", 200, headers={"Accept": "application/json"},
     header_equals={"Content-Type": "application/json;charset=utf-8"})
http("http-text-json", "http:generic-type", "GET", "/Patient/p1", 200, headers={"Accept": "text/json"},
     header_equals={"Content-Type": "text/json;charset=utf-8"})
http("http-older-type", "http:generic-type", "GET", "/Patient/p1", 200, headers={"Accept": "application/json+fhir"})
http("http-format-generic", "http:generic-type", "GET", "/Patient/p1?_format=application/json", 200,
     header_equals={"Content-Type": "application/json;charset=utf-8"})
http("http-metadata-etag", "http:metadata-etag", "GET", "/metadata", 200, header="ETag")
http("http-prefer-quoted", "http:prefer", "GET", "/Patient?bogus=1", 200, headers={"Prefer": 'Handling="lenient"'}, outcome=True)
http("http-options", "reject:method", "OPTIONS", "/Patient", 405, header_equals={"Allow": "GET, HEAD"})
http("http-accept-xml", "reject:format", "GET", "/Patient/p1", 406, headers={"Accept": "application/fhir+xml"})
http("http-post-json-body", "reject:post-body", "POST", "/Observation/_search", 415, headers={"Content-Type": "application/json"}, body="{}")
http("http-length-invalid", "reject:post-framing", "POST", "/Observation/_search", 400, raw="Content-Length: ²\r\n")
http("http-length-chunked", "reject:post-framing", "POST", "/Observation/_search", 411, raw="Transfer-Encoding: chunked\r\n")
http("http-length-large", "reject:post-framing", "POST", "/Observation/_search", 413, raw="Content-Length: 99999999\r\n")

CASES = C

# Declared contract (README): every combination must have at least one passing case.
DECLARED = sorted(set("""
token:code token:system|code token:|code token:system| token:missing token:not token:text token:in token:not-in token:of-type
reference:Type/id reference:id reference:absolute-local reference:absolute-external reference:canonical reference:canonical-version
reference:chain reference:chain-typed reference:chain-multilevel reference:has reference:missing reference:type-modifier reference:identifier
reference:below reference:above common:_list result:_containedType reject:hierarchy reject:list
string:phonetic reject:multiple-or include:wildcard
quantity:money date:timing token:case-insensitive uri:version string:family-part string:punctuation fulltext:boolean
result:self interaction:read-params interaction:compartment-all reject:operation reject:ambiguous-id reject:urn-modifier
http:allow http:accept http:generic-type http:metadata-etag http:prefer
date:year date:month date:day date:minute date:second date:fraction date:Z date:offset date:no-offset date:missing
date:eq date:ne date:gt date:lt date:ge date:le date:sa date:eb date:ap
string:default string:exact string:contains string:missing
quantity:n quantity:n|system|code quantity:n||code quantity:n||unit quantity:missing quantity:point quantity:comparator quantity:range
quantity:eq quantity:ne quantity:gt quantity:lt quantity:ge quantity:le quantity:sa quantity:eb quantity:ap
number:eq number:ne number:gt number:lt number:ge number:le number:sa number:eb number:ap number:missing
uri:exact uri:below uri:above uri:missing
composite:token$token composite:token$quantity composite:token$date composite:token$string
special:near-km special:near-m special:near-mi special:near-default special:missing
common:or common:and common:escape common:empty common:_id common:_lastUpdated fulltext:_content fulltext:_text
result:_count result:_sort result:_total result:_summary result:_elements result:_include result:_include-iterate result:_revinclude
result:_contained result:_format result:_pretty
interaction:read interaction:vread interaction:metadata interaction:compartment interaction:system interaction:paging interaction:head
interaction:post-type interaction:post-system interaction:post-compartment handling:strict handling:lenient
reject:unknown-param reject:unknown-type reject:unknown-id reject:bad-value reject:bad-modifier reject:valueset reject:chain
reject:result-param reject:history reject:format reject:method reject:post-body reject:post-framing
""".split()))
