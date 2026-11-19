#!/bin/sh
# Platform database bootstrap (runs once, on an empty data directory).
# Roles are least-privilege: each service owns only its own schema.
#   scout_app   -> schemas indexer, clinical          (indexer)
#   deid_svc    -> schema deid (encrypted re-id vault) (gateway + indexer de-id)
#   copilot_app -> schema copilot (cohorts)           (copilot)
# Nobody but deid_svc (and the superuser) can read deid.*.
set -eu
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres <<SQL
CREATE ROLE scout_app   LOGIN PASSWORD '${SCOUT_APP_DB_PASSWORD}';
CREATE ROLE deid_svc    LOGIN PASSWORD '${DEID_DB_PASSWORD}';
CREATE ROLE copilot_app LOGIN PASSWORD '${COPILOT_DB_PASSWORD}';
CREATE ROLE keycloak    LOGIN PASSWORD '${KEYCLOAK_DB_PASSWORD}';
CREATE ROLE mirth       LOGIN PASSWORD '${MIRTH_DB_PASSWORD}';
CREATE DATABASE scoutoss OWNER postgres;
CREATE DATABASE keycloak OWNER keycloak;
CREATE DATABASE mirthdb  OWNER mirth;
SQL
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname scoutoss <<'SQL'
REVOKE ALL ON DATABASE scoutoss FROM PUBLIC;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT CONNECT ON DATABASE scoutoss TO scout_app, deid_svc, copilot_app;
CREATE SCHEMA indexer  AUTHORIZATION scout_app;
CREATE SCHEMA clinical AUTHORIZATION scout_app;
CREATE SCHEMA deid     AUTHORIZATION deid_svc;
CREATE SCHEMA copilot  AUTHORIZATION copilot_app;
REVOKE ALL ON SCHEMA deid FROM PUBLIC;
-- the copilot reads clinical facts only through OpenSearch; no SQL access to other schemas
ALTER ROLE scout_app   SET search_path = indexer, clinical;
ALTER ROLE deid_svc    SET search_path = deid;
ALTER ROLE copilot_app SET search_path = copilot;
SQL
