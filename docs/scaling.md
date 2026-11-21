# Scaling ScoutOSS to 100,000 cases (backlog)

Status: **planned, not implemented.** The largest dataset ScoutOSS has been run with is about a
dozen synthetic studies. Everything below is derived from the design and the code, not measured;
step 1 exists to replace these estimates with numbers. Work through the steps in order.

## What already holds at 100,000 studies

| Area | Why |
|---|---|
| Search and dashboards | About a million series documents is small for OpenSearch; statistics are aggregations. |
| XNAT archive | XNAT runs archives of this size; it needs tuning (heap, PostgreSQL), not redesign. |
| Viewing (OHIF) | One study is loaded at a time, so speed depends on disk and network, not archive size. |
| De-identification | Pseudonyms and UIDs are deterministic (HMAC), so work can be spread over replicas. |
| Idempotent indexing | Deterministic document IDs and recomputed rollups make re-runs and parallelism safe. |

## What has to change

### 1. Load test first

* Add an index-only mode to `services/indexer/scoutoss_indexer/synthetic.py` and `scripts/seed.py`:
  generate headers without pixel data and index them directly, 10k then 100k studies, with a
  realistic spread of modalities, body parts, ages and HL7 events.
* Add `make loadtest` that reports search latency (p50/p95), copilot answer time for the three
  example questions, and indexer backfill rate (sessions per minute).
* Record the numbers here. Later steps are judged against them.

### 2. Copilot row cap (correctness, do before anything else)

`services/copilot/scoutoss_copilot/executor.py` returns at most `MAX_ROWS` = 10,000 rows per step
and joins steps in memory on `patient_pseudonym`. Above the cap the result is flagged
`truncated` and counts are wrong.

* Count with aggregations (`cardinality`, or a `composite` aggregation on `patient_pseudonym`)
  instead of fetching rows.
* Page member sets with `search_after` (point-in-time) for joins and exports.
* Store a large cohort as its query plan plus count; materialise members on export or pipeline
  launch (`services/copilot/scoutoss_copilot/cohorts.py` currently stores full member arrays).

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
* XNAT project access sync (`xnat_access.py`): it lists all Keycloak users and each project's
  members every few seconds; lengthen the interval or switch to Keycloak admin events.

### 6. Viewing

No change beyond storage and network speed.

## At 10,000 cases

Only step 2 is required for correct answers, and step 3 makes the first load take hours instead
of most of a day. Steps 4 and 5 can wait.
