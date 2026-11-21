# ScoutOSS runbook

## Dev stack

```bash
make up        # .env secrets, TLS certs, XNAT plugins, build, boot, bootstrap jobs, health check
make seed      # 10 synthetic fake-PHI studies via the de-id gateway + 13 HL7 messages via Mirth
make e2e       # end-to-end smoke test (API checks, SSO, then the UI in headless Chromium)
make ui-smoke  # only the browser pass; screenshots in artifacts/ui-smoke/
make logs SERVICE=indexer
make down      # stop (keeps data)    |    make clean   # stop and DELETE all volumes
```

Requirements: Docker with ~12 GB RAM for the stack, `uv`, Node 22 (UI dev only), dcmtk
`storescu` (e2e), and Chromium for the UI smoke test (`uv run --group e2e playwright install
chromium`, or an installed Google Chrome). XNAT's official image is amd64-only and runs under emulation on Apple
silicon (first boot ≈ 2–5 min).

URLs (dev CA in `infra/nginx/certs/ca.crt`; trust it, or accept the warning):

| URL | What |
|---|---|
| https://app.localhost:24443 | UI (copilot, embedded dashboards) |
| https://app.localhost:24443/dashboards | OpenSearch Dashboards (Keycloak SSO) |
| https://app.localhost:24443/grafana | Grafana (admin / `GRAFANA_ADMIN_PASSWORD`) |
| https://app.localhost:24443/jaeger | Jaeger (Keycloak SSO) |
| https://app.localhost:24443/prometheus | Prometheus (Keycloak SSO) |
| https://app.localhost:24443/notebooks | JupyterHub (Keycloak) |
| https://xnat.localhost:24443 | XNAT + OHIF (Keycloak sign-on for users; `admin` / `XNAT_ADMIN_PASSWORD` is a local account) |
| https://auth.localhost:24443 | Keycloak (admin / `KEYCLOAK_ADMIN_PASSWORD`) |
| `127.0.0.1:24112` AE `SCOUTGW` | DICOM C-STORE into the de-id gateway (called AE = project) |
| `127.0.0.1:26661` | HL7 MLLP |

Demo users: `demo` / `demo` (researcher role; a fixed, publicly known password, imported as a hash
because it does not meet the realm's password policy), and `researcher`, `viewer`, `scoutadmin` with
the generated `DEMO_USER_PASSWORD` in `.env`.
Chrome and Firefox resolve `*.localhost` themselves. Safari and other clients use the system
resolver, which does not on macOS ("Safari Can't Find the Server"): add
`127.0.0.1 app.localhost xnat.localhost auth.localhost` to `/etc/hosts`.

## First install (production, Helm)

1. Provision PostgreSQL 15 (HA), create roles/schemas with `infra/postgres/init.sh`
   (`scout_app`, `deid_svc`, `copilot_app`, `mirth`, `keycloak`), TLS required.
2. Provision OpenSearch 2.x **with the security plugin**: users `scoutoss_app` (write
   `dicom_*`, `clinical_events`, `indexer_dlq`; `create_doc` only on `audit`), `scoutoss_reader`
   (read-only, used by copilot and notebooks); document-level security on `project` mapped from
   the Keycloak `groups` claim for Dashboards tenants.
3. Import `infra/keycloak/realm.json` (replace hostnames; keep secrets in the IdP; **delete the demo
   users `demo`, `researcher`, `viewer` and `scoutadmin` from the file first** — `demo` has a publicly
   known password), create
   `/projects/<XNAT project>` groups and add users matching their XNAT project membership.
4. Deploy XNAT 1.8+ (or point at an existing one) with the Container Service (Kubernetes
   backend) and OHIF plugins; run `scripts/bootstrap/xnat.py` against it.
   For Keycloak sign-on into XNAT also install `openid-auth-plugin`, write the provider properties
   as `xnat/xnat-start.sh` does, and run the access sync (`scoutoss-indexer xnat-access-sync`, see
   the `xnat-access-sync` service in `docker-compose.yml`). The Helm chart does not deploy the
   sync yet; run it as a separate Deployment from the indexer image.
5. Create the `scoutoss-secrets` Secret (keys listed in `helm/scoutoss/values.yaml`), e.g. via
   External Secrets from Vault. Generate `DEID_HMAC_SALT` (≥ 32 random bytes) and
   `DEID_VAULT_KEY` (`openssl rand -base64 32`) **once** and escrow them (see Backup).
6. `helm upgrade --install scoutoss helm/scoutoss -f values-prod.yaml -n scoutoss`
7. Run `scripts/bootstrap/opensearch.py` (ISM policies, dashboards) and `scripts/bootstrap/mirth.py`.
8. Branch protection on `main`: require all `ci` jobs (lint, unit, integration, ui, helm,
   images, trivy-config, e2e).

## Operations

### Health
`scripts/healthcheck.sh` (dev) — every container healthcheck plus the three edge hosts.
Each service: `/health` (liveness), `/ready` (dependencies), `/metrics`.

### Re-indexing
* Everything: `POST /reindex {}` (admin token) → cursor cleared, worker re-polls. Safe: IDs
  are deterministic and rollups are recomputed.
* One project / sessions: `{"project": "P"}` or `{"experiment_ids": ["XNAT_E1"]}`.
* Mapping changes: additive fields are applied on indexer start. For type changes create
  `dicom_studies_v2`, reindex (`POST _reindex`), swap an alias, then delete the old index.

### Dead-letter queue
`indexer_dlq` holds items that could not be processed (`stage`: read, deid, extract, index,
fetch). Typical causes: corrupt files (extract), SR/burned-in objects (deid, by design), XNAT
outages (fetch, after 5 attempts). Fix the cause and re-run `/reindex` for the experiment;
repeats increment `attempts` on the same DLQ document.

### XNAT archive delays
The gateway expedites archiving; if sessions sit in the prearchive, check
`gateway_sessions_expedited_total{outcome}` and the prearchive page. XNAT's session rebuilder
(1 min) archives them regardless.

### XNAT single sign-on and project access
XNAT signs users in through Keycloak (`openid-auth-plugin`, config written by `xnat/xnat-start.sh`
to `$XNAT_HOME/config/auth/openid-provider.properties`). A visitor with a Keycloak session is
signed in silently; their XNAT account (same username) is created on first visit. `admin` and the
`scout_*` service accounts stay local XNAT accounts.

* **Project access** comes from Keycloak groups `/projects/<XNAT project ID>`, mirrored by the
  `xnat-access-sync` service into XNAT's read-only `collaborator` role every few seconds. It only
  adds or removes collaborators that are Keycloak users; owners, members and local accounts are
  never touched. To give someone access, add them to the group; do not add them in XNAT.
* **"Open in OHIF" links** go to `https://<xnat>/scoutoss/open#<path>` (`infra/nginx/html/xnat-open.html`),
  which signs in if needed and continues to the viewer, because XNAT itself goes to its home page
  after a sign-in. Behind another proxy, serve that file at `/scoutoss/open` on XNAT's host and add
  the `Index.vm` cookie redirect from `infra/nginx/templates/scoutoss.conf.template`, or set
  `viewerLanding: false` in the UI's `config.js` to link straight to XNAT.
* **Troubleshooting:** `xnat-access-sync` unhealthy means it cannot reach Keycloak or XNAT (its log
  says which). A 401 from Keycloak's admin API means the `scoutoss-xnat-sync` client lost the
  `kc-admin-api` client scope or its `realm-management` roles (`view-users`, `query-users`,
  `query-groups`). "You do not have access to this project" on the landing page means the user is
  not in the project's Keycloak group.

### HL7
Mirth Administrator: `https://<mirth>:8443` (admin / `MIRTH_ADMIN_PASSWORD`). ACK `AE`
means the indexer rejected or failed the batch (it is in the DLQ when indexing failed);
Mirth retries 3× every 10 s. Message content is not stored (metadata only).

### LLM provider
`LLM_PROVIDER=heuristic|azure|openai_compatible`. Switching is a config change + copilot restart.
Watch `copilot_llm_latency_seconds` and `copilot_query_validation_failures_total`.

### Secret rotation
* Keycloak client secrets, DB passwords, XNAT service accounts: rotate in the source, update
  the Secret, `kubectl rollout restart`.
* `DEID_VAULT_KEY`: decrypt-and-re-encrypt `deid.*` with a one-off job holding both keys.
* `DEID_HMAC_SALT`: **never rotate in place** — pseudonyms would change and imaging/clinical
  joins would break. A new salt means a new pseudonym space (full re-ingest).

## Alerting

Rules (`infra/prometheus/alerts.yml`, Helm `PrometheusRule`):

| Alert | Condition | Action |
|---|---|---|
| `IndexerDeadLetters` | DLQ grew in 15 min | inspect `indexer_dlq`, fix, reindex |
| `CopilotValidationFailureRateHigh` | > 5 % generated queries rejected for 5 min | check provider/prompt; review `audit` (`outcome=rejected`) for abuse |
| `ServiceDown` | any `/health`/metrics target down > 2 min | `docker compose ps` / `kubectl describe`, logs in `logs-*` |
| `IndexerStalled` | no successful poll for 10 min | XNAT reachability, credentials, Postgres |

In dev the rules are evaluated (Prometheus → Alerts) but no Alertmanager is wired
("dev-off"). In production route them through Alertmanager to the on-call channel.

## Logs

All services log one JSON object per line (`ts, level, service, env, trace_id, msg` plus
`user_id, endpoint, latency_ms, status` on requests). Filebeat → Logstash OSS → OpenSearch
`logs-YYYY.MM.dd`, deleted after 30 days (ISM `logs-retention`). Search by `trace_id` to follow
one request across UI → copilot → indexer, and open the same ID in Jaeger.

## Backup and restore

| Data | Backup | Notes |
|---|---|---|
| `scout-db` (cursor, clinical, cohorts, **deid vault**) | `pg_dump -Fc` nightly + WAL archiving (PITR), encrypted, 35-day retention | The vault is useless without `DEID_VAULT_KEY`; back the key up separately (HSM / Vault), never next to the dump |
| `xnat-db` + `/data/xnat/archive` | XNAT DB dump + filesystem/object snapshots | XNAT is the archive of record |
| OpenSearch | snapshot repository (S3/NFS), daily | Indices are **rebuildable**: `/reindex` from XNAT + replay HL7 from Mirth/EHR; audit is not rebuildable — snapshot it and keep 6 years |
| Keycloak | realm export + its DB | |
| Secrets | `DEID_HMAC_SALT`, `DEID_VAULT_KEY` escrowed (two-person access) | Loss of the salt = loss of the ability to link new data to existing pseudonyms |

Dev commands:

```bash
docker compose exec -T scout-db pg_dump -U postgres -Fc scoutoss > backup/scoutoss.dump
docker compose exec -T xnat-db  pg_dump -U xnat -Fc xnat > backup/xnat.dump
docker run --rm -v scoutoss_xnat-archive:/a -v "$PWD/backup":/b alpine tar czf /b/xnat-archive.tgz -C /a .
```

Restore: stop services, restore the databases (`pg_restore -c -d`), restore the archive volume,
start XNAT, then `POST /reindex {}` to rebuild OpenSearch from XNAT and re-send HL7 if needed.

### Disaster recovery

* **RPO** ≤ 15 min (Postgres WAL), 24 h for OpenSearch snapshots (rebuildable from XNAT).
* **RTO** ≈ 2 h: restore Postgres + XNAT storage, deploy the chart, restore the latest
  OpenSearch snapshot (or reindex), verify with `scripts/e2e.sh` against the recovered stack.
* Test the restore quarterly into an isolated namespace.
