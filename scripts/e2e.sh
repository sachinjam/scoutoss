#!/usr/bin/env bash
# ScoutOSS end-to-end smoke test. Run after `make up`:
#   1. health-check every service
#   2. C-STORE a fresh synthetic CT chest study with dcmtk storescu to the de-id gateway
#   3. within 60 s: study in XNAT (pseudonymized), doc in OpenSearch, Dashboards search hit
#   4. copilot answers "how many CT studies" (count >= 1) and the cross-modal question
#   5. QC pipeline launched on the cohort -> assessor appears in XNAT; actions audited
#   6. Keycloak SSO into Dashboards, then a real-browser pass over the UI (scripts/ui_smoke.py)
set -euo pipefail
cd "$(dirname "$0")/.."
PY="${PY:-uv run --frozen python}"
DICOM_PORT="$(grep -E '^DICOM_PORT=' .env | cut -d= -f2)"; DICOM_PORT="${DICOM_PORT:-24112}"
PROJECT="$(grep -E '^DEFAULT_PROJECT=' .env | cut -d= -f2)"; PROJECT="${PROJECT:-SCOUT_DEMO}"
command -v storescu >/dev/null || { echo "storescu (dcmtk) is required: brew install dcmtk / apt-get install dcmtk"; exit 1; }

echo "== health"
[[ "${SKIP_HEALTH:-0}" == 1 ]] && echo "(skipped)" || scripts/healthcheck.sh

echo "== C-STORE"
work="$(mktemp -d)"; trap 'rm -rf "$work"' EXIT
$PY scripts/make_study.py "$work/study" > "$work/uid"
mrn="$($PY -c "import pydicom,glob; print(pydicom.dcmread(sorted(glob.glob('$work/study/**/*.dcm', recursive=True))[0]).PatientID)")"
start="$(date +%s)"
storescu -aet E2E -aec "$PROJECT" +sd +r -xs 127.0.0.1 "$DICOM_PORT" "$work/study"
echo "pushed $(find "$work/study" -name '*.dcm' | wc -l | tr -d ' ') instances"

echo "== checks"
$PY scripts/e2e.py --mrn "$mrn" --start "$start" "$@"
echo "== SSO (Keycloak -> oauth2-proxy -> OpenSearch Dashboards)"
$PY scripts/check_sso.py
echo "== UI (headless browser)"
[[ "${SKIP_UI:-0}" == 1 ]] && echo "(skipped)" || uv run --frozen --group e2e python scripts/ui_smoke.py
echo "E2E PASSED"
