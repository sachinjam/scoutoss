#!/usr/bin/env bash
# Self-signed dev CA + server certificate for *.localhost (nginx TLS termination).
# Production uses cert-manager (see helm/scoutoss/templates/certificate.yaml).
set -euo pipefail
cd "$(dirname "$0")/.."
DIR=infra/nginx/certs
mkdir -p "$DIR"
if [[ -f "$DIR/server.crt" && -f "$DIR/server.key" ]]; then
  echo "dev certificates exist"
  exit 0
fi
umask 077
openssl req -x509 -newkey rsa:3072 -sha256 -days 825 -nodes \
  -keyout "$DIR/ca.key" -out "$DIR/ca.crt" -subj "/CN=ScoutOSS Dev CA" >/dev/null 2>&1
cat > "$DIR/san.cnf" <<CNF
[req]
distinguished_name=dn
[dn]
[ext]
subjectAltName=DNS:localhost,DNS:app.localhost,DNS:xnat.localhost,DNS:auth.localhost,IP:127.0.0.1
extendedKeyUsage=serverAuth
keyUsage=digitalSignature,keyEncipherment
CNF
openssl req -newkey rsa:3072 -sha256 -nodes -keyout "$DIR/server.key" -out "$DIR/server.csr" \
  -subj "/CN=localhost" >/dev/null 2>&1
openssl x509 -req -in "$DIR/server.csr" -CA "$DIR/ca.crt" -CAkey "$DIR/ca.key" -CAcreateserial \
  -out "$DIR/server.crt" -days 825 -sha256 -extfile "$DIR/san.cnf" -extensions ext >/dev/null 2>&1
cat "$DIR/server.crt" "$DIR/ca.crt" > "$DIR/fullchain.crt"
chmod 644 "$DIR/server.crt" "$DIR/fullchain.crt" "$DIR/ca.crt"
chmod 640 "$DIR/server.key"
rm -f "$DIR/server.csr" "$DIR/san.cnf"
echo "dev certificates written to $DIR (trust $DIR/ca.crt to avoid browser warnings)"
