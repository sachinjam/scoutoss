# ScoutOSS API

Both services publish OpenAPI at `/docs` and `/openapi.json` (inside the network). All
endpoints except `/health`, `/ready` and `/metrics` require `Authorization: Bearer <JWT>`
from the `scoutoss` Keycloak realm with the service's audience. Send a W3C `traceparent`
header to join a trace. Errors are JSON: `{"detail": ...}`.

## Copilot — `https://app.<host>/api/copilot`

Audience `scoutoss-copilot`. Roles: **R** = viewer/researcher/admin, **W** = researcher/admin.
Results are always restricted to the caller's projects (Keycloak groups `/projects/<ID>`;
admins are unrestricted). Every row marked *audited* writes `{user, action, resource,
params_hash, ts, ip}` to the `audit` index; if the audit write fails the call fails (503).

| Method | Path | Role | Description |
|---|---|---|---|
| POST | `/query` | R | Answer a question. *Audited* (`query`). |
| POST | `/query/stream` | R | Same, as Server-Sent Events. |
| GET | `/sessions/{session_id}/history` | R | Conversation memory (last N turns). |
| DELETE | `/sessions/{session_id}` | R | Clear memory. |
| GET | `/cohorts` | R | Caller's cohorts. |
| GET | `/cohorts/{id}` | R | Cohort incl. member pseudonyms. *Audited* (`cohort_read`). |
| POST | `/cohorts/{id}/save` | W | Name and keep a cohort. *Audited* (`cohort_save`). |
| GET | `/cohorts/{id}/export` | W | CSV of the cohort's studies. *Audited* (`export`). |
| GET | `/pipelines?project=` | W | Session-level pipelines enabled in a project. |
| POST | `/cohorts/{id}/pipeline` | W | Launch a pipeline on every session of the cohort. *Audited* (`pipeline_launch`). |
| GET | `/health` `/ready` `/metrics` | — | Liveness, readiness (OpenSearch, Postgres), Prometheus. |

### `POST /query`

```json
{"question": "patients under 85 with lung cancer AND CT chest", "session_id": "a1b2c3"}
```

`session_id` (optional, `[A-Za-z0-9_.-]{1,64}`) enables follow-ups ("of those, how many are
female?"). Response:

```json
{
  "answer_text": "Found 1 patient (2 imaging studies). Modalities: CT 2. Sex: male 1. Mean age at study 71.5 years. Diagnoses: LUNG_CANCER 1.",
  "cohort_count": 1,
  "study_count": 2,
  "cohort_id": "5f0c…",
  "charts": [{"id": "age", "type": "bar", "title": "Age at study …", "spec": {"$schema": "https://vega.github.io/schema/vega-lite/v5.json", "...": "..."}}],
  "generated_query": {
    "steps": [
      {"index": "dicom_studies", "size": 10000, "description": "imaging studies matching the imaging criteria",
       "query": {"bool": {"filter": [{"terms": {"modalities": ["CT"]}}, {"terms": {"body_parts": ["CHEST"]}},
                                     {"range": {"patient_age": {"lt": 85.0}}}]}}},
      {"index": "clinical_events", "size": 10000, "description": "patients with a matching diagnosis in HL7 feeds",
       "query": {"bool": {"filter": [{"terms": {"diagnosis_category": ["LUNG_CANCER"]}}]}}}
    ],
    "combine": "intersect", "join_field": "patient_pseudonym", "subject": "patients", "charts": ["age", "sex"]
  },
  "executed_queries": [{"index": "dicom_studies", "query": {"...": "with project filter"}, "size": 10000, "_source": ["patient_pseudonym", "study_uid"]}],
  "studies": [{"study_uid": "2.25.…", "patient_pseudonym": "SP…", "study_date": "2021-10-02", "modalities": ["CT"], "viewer_url": "https://xnat…/VIEWER/?…"}],
  "stats": {"patients": 1, "studies": 2, "modality": {"CT": 2}, "sex": {"M": 1}, "age": {"70": 2}, "diagnosis": {"LUNG_CANCER": 1}},
  "warnings": [],
  "provider": "heuristic",
  "session_id": "a1b2c3",
  "question": "patients under 85 with lung cancer AND CT chest"
}
```

| Status | Meaning |
|---|---|
| 401 | missing/invalid/expired token, wrong audience or issuer |
| 403 | role not allowed, or no project access |
| 422 | `{"error":"query_rejected","reason":"unknown_index|unknown_field|mutation|size_cap|forbidden_clause|…"}` or `{"error":"phi_detected"}` |
| 502 | LLM provider unavailable |
| 503 | audit trail unavailable (fail closed) |
| 504 | OpenSearch search failed or exceeded 5 s |

### `POST /query/stream`

`text/event-stream` with events `status` (`{"stage":"planning"|"answering"}`), `query`
(`{"generated_query": …}`), `token` (`{"text": "…"}`) and finally `result` (the `/query`
body) — or `error` (`{"status": 422, "detail": …}`).

### `POST /cohorts/{id}/pipeline`

```json
{"pipeline": "scoutoss-qc-session"}
```
→ `{"cohort_id": "…", "pipeline": "…", "launched": [{"experiment_id": "ScoutOSS_E00001", "status": "success", "workflow_id": "35"}], "errors": []}`.
413 if the cohort has more sessions than `MAX_PIPELINE_SESSIONS` (default 200).

## Indexer — internal (`http://indexer:8000`)

Audience `scoutoss-indexer`.

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/schema` | any role | Field catalog + de-identified example values (used by the copilot). |
| GET | `/stats` | any role | Index counts and worker state. |
| POST | `/reindex` | admin | Reset the cursor (`{"project": "P"}` / `{"experiment_ids": [...]}` / `{}` = all) and wake the worker. *Audited* (`reindex`). |
| POST | `/webhook/xnat` | `X-Scout-Webhook-Token` | Event hook: `{"experiment_id": "XNAT_E1"}` triggers an immediate poll. |
| POST | `/clinical/events` | service/admin | HL7 ingest (Mirth). Body `{"events": [ClinicalEvent]}` (1–500). *Audited* (`clinical_ingest`). 502 if documents failed to index (they are dead-lettered). |
| GET | `/health` `/ready` `/metrics` | — | Liveness (worker thread alive), readiness (OpenSearch, Postgres), Prometheus. |

`ClinicalEvent`:

```json
{"message_control_id": "MSG1", "message_type": "ADT^A01", "patient_id": "<MRN>",
 "patient_sex": "M", "birth_date": "19500314", "event_datetime": "20220105101500", "project": "SCOUT_DEMO",
 "diagnoses": [{"code": "C34.90", "system": "I10", "text": "Malignant neoplasm of lung"}],
 "orders": [{"code": "CTCHEST", "text": "CT chest"}],
 "observations": [{"code": "2160-0", "name": "Creatinine", "value": "1.3", "units": "mg/dL", "flag": "H"}]}
```

## Index schemas

Defined once in `services/common/scoutoss_common/catalog.py` (strict mappings; new fields are
added in place on startup). Key fields:

* `dicom_studies`: `study_uid, patient_pseudonym, project, study_date, study_year, modalities[],
  body_parts[], patient_sex, patient_age, manufacturers[], institution (site code),
  xnat_experiment_id, viewer_url`
* `dicom_series`: per-series header fields (`modality, body_part, series_description,
  manufacturer_model, slice_thickness, magnetic_field_strength, instance_count, …`)
* `dicom_patients`: rollup (`study_count, modalities[], body_parts[], projects[], diagnoses[],
  diagnosis_codes[], age_latest, first/last_study_date`)
* `clinical_events`: `patient_pseudonym, project, message_type, event_type, event_date,
  diagnosis_code/category/text, order_*, lab_*`
* `indexer_dlq`: `ref (scrubbed), source, stage (read|deid|extract|index|fetch), error_type, attempts`
* `audit`: `user, action, resource, params_hash, ts, ip, service, trace_id, outcome`

## Metrics

`http_requests_total{service,method,endpoint,status}`, `http_request_duration_seconds`,
`indexer_documents_indexed_total{index}`, `indexer_dlq_total{stage}`, `indexer_poll_cycles_total`,
`indexer_last_success_timestamp_seconds`, `copilot_queries_total{outcome}`,
`copilot_llm_latency_seconds{provider}`, `copilot_query_validation_failures_total{reason}`,
`copilot_phi_redactions_total`, `gateway_instances_{received,forwarded,rejected}_total`,
`gateway_sessions_expedited_total`.
