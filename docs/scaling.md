# Scaling ScoutOSS

## Measured: 10,000 to 40,000 studies (done)

`make loadtest` indexes synthetic studies straight into OpenSearch (no XNAT, no images) under a
separate `LOADTEST` project, asks the copilot questions as an admin and compares every answer
with exact ground truth computed from OpenSearch. `make loadtest-clean` removes the data.
Numbers below are from the dev stack on one laptop (a single OpenSearch node).

| | 10,000 studies | 40,000 studies |
|---|---|---|
| Index-only load (series + studies + patients + diagnoses) | 4.7 s | 15.9 s |
| Index size on disk (all four indices) | 15.6 MB | not measured |
| "how many CT studies" | exact, 0.11 s | exact, 0.45 s |
| "how many studies" (every study) | exact, 0.23 s | exact, 1.0 s |
| "patients under 85 with lung cancer AND CT chest" | exact, 0.05 s | exact, 0.12 s |
| "of those, how many are female?" | exact, 0.02 s | exact, 0.03 s |
| CSV export of the largest cohort | — | 40,011 rows, 1.3 s |

What had to change to get there: the copilot read at most 10,000 rows per step, so at 10,011
studies "how many studies" answered 10,000 with a truncation warning, and CSV export stopped at
10,000 studies. Steps and exports are now paged (`search_after`, 5,000 rows per request, still
under the 5 s per-request timeout) up to `MAX_MEMBERS` = 50,000 rows per step.

Real ingest was measured separately: re-indexing the 11 archived demo sessions from XNAT took
5.8 s (about 0.5 s per session, one or two scans each). A one-time backfill of 10,000 sessions is
therefore on the order of 1.5 hours run serially, longer for studies with many series. Not
measured: XNAT's own behaviour with 10,000 archived sessions, the cost of listing them on every
poll, and C-STORE throughput for that many images.

**Limits that remain at this size**

* A step matching more than 50,000 rows is truncated (flagged in the answer). Cohorts go back to
  OpenSearch as `terms` filters, which OpenSearch caps at 65,536 values.
* Images need real storage: 10,000 CT/MR studies is roughly 2-5 TB in XNAT's archive.
* "Run Pipeline on Cohort" is limited to 200 sessions per launch and paced at four launches a
  second (see the runbook); 148 QC runs complete in about 65 s.

# Scaling to 100,000 cases (backlog)

Status: **planned, not implemented.** Everything in this section is derived from the design and
the measurements above, not from a run at that size. Work through the steps in order.

## What already holds at 100,000 studies

| Area | Why |
|---|---|
| Search and dashboards | About a million series documents is small for OpenSearch; statistics are aggregations. |
| XNAT archive | XNAT runs archives of this size; it needs tuning (heap, PostgreSQL), not redesign. |
| Viewing (OHIF) | One study is loaded at a time, so speed depends on disk and network, not archive size. |
| De-identification | Pseudonyms and UIDs are deterministic (HMAC), so work can be spread over replicas. |
| Idempotent indexing | Deterministic document IDs and recomputed rollups make re-runs and parallelism safe. |

## What has to change

### 1. Load test at 100,000

`make loadtest STUDIES=100000` runs today, but "how many studies" will be truncated at 50,000
rows until step 2 is done. Record search latency, copilot answer times and backfill rate here.

### 2. Copilot joins beyond 50,000 rows

`services/copilot/scoutoss_copilot/executor.py` pages each step up to `MAX_MEMBERS` = 50,000 rows
and joins steps in memory on `patient_pseudonym`. Past that:

* Count with exact paged aggregations (a `composite` aggregation on `patient_pseudonym`; the
  `cardinality` aggregation is an estimate and was off by 3 at 13,000 patients).
* Join steps without sending member lists back as `terms` filters (65,536-value limit), for
  example with a terms lookup on a stored cohort document.
* Store a large cohort as its query plan plus count; materialise members on export or pipeline
  launch (`services/copilot/scoutoss_copilot/cohorts.py` stores full member arrays).

### 3. Indexer backfill

`services/indexer/scoutoss_indexer/sources/xnat.py` lists every session on each poll and
`worker.py` processes sessions one at a time. A first load of 100,000 sessions would take days.

* List incrementally (sessions modified since the cursor's high-water mark).
* Process sessions with a bounded worker pool; XNAT load is the limit, so make it configurable.
* Use the existing `/webhook/xnat` hook (XNAT Event Service) as the main trigger and keep polling
  as a safety net at a longer interval.

### 4. Ingest throughput

`services/indexer/scoutoss_indexer/gateway.py` is one process that de-identifies in memory and
forwards per association. Tens of millions of images through it is too slow for an initial load.

* Run several gateway replicas behind a TCP load balancer (safe: deterministic pseudonyms, one
  shared vault).
* For the initial load, add a bulk path: de-identify from a folder or PACS query/retrieve in
  parallel and import to XNAT with its bulk import service, bypassing C-STORE.

### 5. Infrastructure (Helm, not the dev compose stack)

* An x86 server or Kubernetes cluster; the dev stack on Apple silicon runs XNAT under emulation.
* Images: 20–50 TB on NAS or object storage, with backups (see `docs/runbook.md`).
* OpenSearch: three nodes with replicas and ISM policies, for resilience rather than speed.
* PostgreSQL: highly available, sized for XNAT's catalogue; tune XNAT heap.
* Pipelines: the Container Service Kubernetes backend; replace the 200-session
  `MAX_PIPELINE_SESSIONS` limit with a queue.
* DICOM export (`dicom_export.py`): it lists the `AI_DICOM` resource of every session on each
  cycle (one request per session). Trigger it from the Container Service's completion event, or
  list only sessions with a finished container, and send sessions in parallel.
* Auto-run (`auto_run.py`): it lists every session and reads its whole state table on each
  cycle; list incrementally or trigger it from the XNAT session-archived event. Launch pacing
  (four a second) bounds how fast a bulk import is processed.
* XNAT project access sync (`xnat_access.py`): it lists all Keycloak users and each project's
  members every few seconds; lengthen the interval or switch to Keycloak admin events.

### 6. Viewing

No change beyond storage and network speed.
