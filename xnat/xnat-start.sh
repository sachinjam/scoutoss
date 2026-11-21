#!/bin/sh
# Write XNAT's datasource settings from the environment, then hand over to the image's
# entrypoint. Adapted from NrgXnat/xnat-docker-compose (xnat-conf.properties template).
set -eu
conf="${XNAT_HOME}/config"
mkdir -p "$conf" "${XNAT_HOME}/plugins" "${XNAT_HOME}/logs" "${XNAT_HOME}/work"
umask 077
cat > "$conf/xnat-conf.properties" <<CONF
datasource.driver=org.postgresql.Driver
datasource.url=jdbc:postgresql://xnat-db:5432/xnat
datasource.username=xnat
datasource.password=${XNAT_DB_PASSWORD}
hibernate.dialect=org.hibernate.dialect.PostgreSQL9Dialect
hibernate.hbm2ddl.auto=update
hibernate.show_sql=false
hibernate.cache.use_second_level_cache=true
hibernate.cache.use_query_cache=true
spring.http.multipart.max-file-size=1073741824
spring.http.multipart.max-request-size=1073741824
CONF
# First-boot site preferences (siteUrl etc. are finalised by scripts/bootstrap/xnat.py).
cat > "$conf/prefs-init.ini" <<INI
[siteConfig]
siteId=ScoutOSS
siteUrl=${XNAT_PUBLIC_URL}
adminEmail=admin@scoutoss.local
archivePath=/data/xnat/archive
prearchivePath=/data/xnat/prearchive
cachePath=/data/xnat/cache
buildPath=/data/xnat/build
ftpPath=/data/xnat/ftp
pipelinePath=/data/xnat/pipeline
requireLogin=true
userRegistration=false
enableCsrfToken=true
initialized=true
INI
# Keycloak single sign-on (NrgXnat openid-auth-plugin). The browser is sent to the public issuer;
# XNAT itself talks to Keycloak over the internal network. autoLogin signs a visitor in silently
# (OIDC prompt=none) when they already have a Keycloak session, e.g. coming from the ScoutOSS UI.
# Accounts are created on first sign-in; project access is granted by the xnat-access-sync service.
if [ -n "${KC_XNAT_CLIENT_SECRET:-}" ]; then
  mkdir -p "$conf/auth"
  cat > "$conf/auth/openid-provider.properties" <<OIDC
auth.method=openid
type=openid
provider.id=keycloak
name=ScoutOSS single sign-on
auto.enabled=true
auto.verified=true
visible=true
siteUrl=${XNAT_PUBLIC_URL}
preEstablishedRedirUri=/openid-login
openid.keycloak.clientId=scoutoss-xnat
openid.keycloak.clientSecret=${KC_XNAT_CLIENT_SECRET}
openid.keycloak.userAuthUri=${KEYCLOAK_PUBLIC_ISSUER}/protocol/openid-connect/auth
openid.keycloak.accessTokenUri=${KEYCLOAK_INTERNAL_ISSUER}/protocol/openid-connect/token
openid.keycloak.userInfoUri=${KEYCLOAK_INTERNAL_ISSUER}/protocol/openid-connect/userinfo
openid.keycloak.logoutUri=${KEYCLOAK_PUBLIC_ISSUER}/protocol/openid-connect/logout
openid.keycloak.scopes=openid,profile,email
openid.keycloak.link=<p><a href="/openid-login?providerId=keycloak">Sign in with ScoutOSS (Keycloak)</a></p>
openid.keycloak.autoLogin=true
openid.keycloak.pkceEnabled=true
openid.keycloak.forceUserCreate=true
openid.keycloak.shouldFilterEmailDomains=false
openid.keycloak.emailProperty=email
openid.keycloak.givenNameProperty=given_name
openid.keycloak.familyNameProperty=family_name
openid.keycloak.usernamePattern=[preferred_username]
OIDC
fi
rm -f /data/xnat/home/plugins/*.jar 2>/dev/null || true
cp /plugins/*.jar "${XNAT_HOME}/plugins/" 2>/dev/null || echo "warning: no XNAT plugins mounted at /plugins"
exec /usr/local/bin/entrypoint.sh /usr/local/tomcat/bin/catalina.sh run
