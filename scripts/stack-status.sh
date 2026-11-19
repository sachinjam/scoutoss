#!/usr/bin/env bash
# Quick data-path status: XNAT archive/prearchive counts and OpenSearch index sizes.
set -uo pipefail
cd "$(dirname "$0")/.."
pw="$(grep -E '^XNAT_ADMIN_PASSWORD=' .env | cut -d= -f2)"
x() { docker compose exec -T xnat-web curl -s -u "admin:$pw" "http://localhost:8080$1"; }
x "/data/experiments?format=json" | python3 -c 'import sys,json; r=json.load(sys.stdin)["ResultSet"]["Result"]; print("xnat archived sessions:", len(r))'
x "/data/prearchive/projects?format=json" | python3 -c 'import sys,json; r=json.load(sys.stdin)["ResultSet"]["Result"]; print("xnat prearchive:", len(r), sorted({x["status"] for x in r}))'
docker compose exec -T opensearch curl -s 'localhost:9200/_cat/indices/dicom*,indexer*,clinical*,audit?h=index,docs.count&s=index'
