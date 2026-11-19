"""Shared building blocks for ScoutOSS services.

Every service uses the same structured logging, Prometheus metrics, OpenTelemetry
tracing, Keycloak JWT validation, audit trail and PHI redaction from this package so
the cross-cutting rules (no PHI in logs, every action audited) live in one tested place.
"""

__version__ = "0.1.0"
