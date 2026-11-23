# ScoutOSS

Open-source, AI-assisted clinical research data exploration built on **XNAT**. DICOM and
HL7 data are de-identified on the way in and land in a unified, searchable data lake. You
explore it through OpenSearch dashboards, a natural-language copilot with a *Review Code*
panel, notebooks (pyxnat), the OHIF zero-footprint viewer, and containerized AI pipelines
run by the XNAT Container Service.

```
modality ─C-STORE─▶ de-id gateway ─▶ XNAT ─▶ indexer ─▶ OpenSearch ◀─ copilot ◀─ UI
EHR ─HL7─▶ Mirth ───────────────────────────▶ indexer ─▶ clinical_events ┘
XNAT ─▶ auto-run ─▶ AI container ─▶ result (DICOM) ─▶ dicom-export ─C-STORE─▶ research PACS / DICOM server
```

Pipelines can start by themselves: the `auto-run` service launches the pipelines named in
`AUTO_RUN_PIPELINES` (in the dev stack the QC pipeline) on every study when it arrives, once per
study. Studies already in XNAT when it is switched on are left alone; see the
[runbook](docs/runbook.md#auto-run-pipelines-when-a-study-arrives).

AI results can be returned over DICOM: a pipeline writes DICOM objects into the session resource
`AI_DICOM` and the `dicom-export` service sends them to a configured DICOM destination (in the dev
stack an Orthanc at http://127.0.0.1:28042, user `scout`, password `ORTHANC_PASSWORD` in `.env`).
Results stay de-identified; see the [runbook](docs/runbook.md#dicom-export-of-ai-results).

## Quick start

```bash
make up      # generates .env + dev TLS certs, fetches XNAT plugins, boots and bootstraps everything
make seed    # 10 synthetic fake-PHI studies (7 patients) + 13 HL7 messages
             # make seed CASES=100 -> 100 generated patients (136 studies) with matching HL7
make e2e     # C-STORE → XNAT → searchable < 60 s → auto-run → copilot → pipeline → assessor → DICOM export → audit → SSO → UI in a browser
```

Open https://app.localhost:24443 in Chrome or Firefox (Safari needs an `/etc/hosts` entry, see the
[runbook](docs/runbook.md)) and sign in as `demo` / `demo`, a dev-only login with the researcher
role. Then ask:

* *how many CT studies*
* *patients under 85 with lung cancer AND CT chest*
* *of those, how many are female?*

Every answer shows the generated query plan, Vega-Lite charts and OHIF links. "Open in OHIF"
opens the study in XNAT's viewer without a second login (Keycloak single sign-on into XNAT). From there you
can **Save Cohort**, **Export CSV** or **Run Pipeline on Cohort**.

The OHIF plugin jar is not publicly downloadable from every network. Put it in
`$XNAT_PLUGIN_CACHE`, or set `OHIF_PLUGIN_URL` in `.env`.

## Development

```bash
uv sync                      # Python 3.12 workspace (services/common, indexer, copilot)
make lint                    # ruff + mypy --strict
make test                    # unit + integration (testcontainers), coverage gate 80%
make ui-test                 # UI typecheck, eslint, vitest
make helm-lint
make qc                      # run the QC pipeline on every study and verify each result
make loadtest                # 10,000 synthetic studies: copilot answers checked against ground truth
```

| Service | Path | Stack |
|---|---|---|
| shared library | `services/common` | structlog, Prometheus, OpenTelemetry, PyJWT, audit, PHI redaction |
| indexer + de-id gateway + HL7 ingest | `services/indexer` | FastAPI, pydicom, pynetdicom, psycopg, opensearch-py |
| copilot | `services/copilot` | FastAPI, LLM providers (heuristic / Azure OpenAI / vLLM) |
| UI | `services/ui` | React 18, Vite, TypeScript, keycloak-js, vega-embed |
| HL7 gateway | `services/hl7-gateway` | Mirth Connect 4.5.2 channel |
| notebooks | `services/notebooks` | JupyterHub + pyxnat |

## Documentation

* [Architecture](docs/architecture.md): diagrams, data flows, security model
* [Runbook](docs/runbook.md): operations, re-indexing, alerting, backup and restore, DR
* [API](docs/api.md): copilot and indexer endpoints, schemas, metrics
* [Scaling](docs/scaling.md): measured at 10,000-40,000 studies (`make loadtest`); backlog for 100,000
* [Compliance](docs/compliance.md): PHI data flow, de-identification spec, audit model, HIPAA/GDPR mapping

## License

Apache-2.0. Third-party components keep their own licenses: XNAT (BSD-2), Mirth Connect 4.5.2
(MPL-2.0), OpenSearch (Apache-2.0), Keycloak (Apache-2.0), OHIF (MIT). All bundled demo data is
synthetic.
