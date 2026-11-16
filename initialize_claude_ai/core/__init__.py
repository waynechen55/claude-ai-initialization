"""Shared infrastructure: configuration, secrets, redaction, process, HTTP, reporting.

Nothing in here knows about a specific phase; every phase is built out of these
pieces so that behaviour like "never log a credential" or "read-only probes still
run in dry-run mode" is enforced in exactly one place.
"""
