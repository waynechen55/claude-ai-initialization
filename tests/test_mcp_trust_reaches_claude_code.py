"""Trust follows the runtime that opens the connection: Claude Code itself, for a URL entry.

openqa-example-net was verified here with a store holding the internal Example root,
written as a URL entry, and showed "Failed to connect -- UNABLE_TO_GET_ISSUER_CERT_LOCALLY"
in Claude Code on a machine without it: a URL entry has no env, and nothing put
the trust where Claude Code reads it. The log said only "could not connect".
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from initialize_claude_ai.mcp_auto_discovery import claude_check, consumer_trust
from initialize_claude_ai.mcp_auto_discovery.claude_check import Outcome

ROOT = "-----BEGIN CERTIFICATE-----\nroot\n-----END CERTIFICATE-----\n"
LEAF = "-----BEGIN CERTIFICATE-----\nleaf\n-----END CERTIFICATE-----\n"
ENTRIES = {
    "openqa-example-test": {
        "type": "http",
        "url": "https://openqa.example.test/mcp",
        "headers": {"Authorization": "Bearer ${OPENQA_EXAMPLE_TEST_TOKEN}"},
    },
    "local": {"command": "python3", "args": ["s.py"]},
}


@pytest.fixture
def trusting(ctx, tmp_path, monkeypatch):
    """A machine whose Claude Code cannot verify openqa.example.test until a file holds the root."""
    from initialize_claude_ai.core import trust_store

    ctx.dry_run = False
    ctx.settings.set("mcp.trust.consumer.enabled", True)
    ctx.settings.set("machine_env.enabled", True)
    ctx.settings.set("machine_env.places", ["settings", str(tmp_path / "bashrc")])
    ctx.settings.set("machine_env.places_if_present", [])
    ctx.settings.set("machine_env.scan", [])
    ctx.settings.set("machine_env.check_effective", False)
    ctx.settings.set("mcp.registration.artifacts_home", str(tmp_path / "mcp-servers"))
    monkeypatch.setattr(consumer_trust, "consumer_store", lambda c: [])
    store = trust_store.TrustStore.for_run(ctx)

    def complete(host, port, cafile, *, extra=()):
        return os.path.isfile(cafile) and "root" in Path(cafile).read_text()

    monkeypatch.setattr(store, "chain_complete", complete)
    monkeypatch.setattr(store, "chain_to_root", lambda host, port: ([LEAF, ROOT], [ROOT]))
    monkeypatch.setattr(
        trust_store, "describe", lambda pem: {"sha256": pem, "subject": "CN=Example Root", "self_signed": "root" in pem}
    )
    monkeypatch.setattr(consumer_trust, "consumer_trusts", lambda c, h, p, extra="": False)
    return store


def test_only_entries_claude_code_opens_itself_count() -> None:
    assert consumer_trust.url_hosts(ENTRIES) == [("openqa-example-test", "openqa.example.test", 443)]


def test_the_bundle_holds_the_chain_above_the_leaf_and_reaches_every_place(trusting, ctx, tmp_path) -> None:
    record = consumer_trust.ensure(ctx, ENTRIES)
    bundle = tmp_path / "mcp-servers" / "trust" / "claude-code-ca.pem"
    assert record.value == str(bundle) and "root" in bundle.read_text() and "leaf" not in bundle.read_text()
    assert f"NODE_EXTRA_CA_CERTS={bundle}" in (tmp_path / "bashrc").read_text()
    assert "openqa-example-test (openqa.example.test)" in record.why
    assert ".mcp.json" not in str(bundle.parent.name), "trust is never written into the entry"


def test_a_bundle_it_is_given_that_already_works_is_kept(trusting, ctx, tmp_path) -> None:
    theirs = tmp_path / "theirs.pem"
    theirs.write_text(ROOT)
    ctx.settings.set("mcp.trust.consumer.repair_broken", False)
    (ctx.claude_home).mkdir(parents=True, exist_ok=True)
    (ctx.claude_home / "settings.json").write_text('{"env": {"NODE_EXTRA_CA_CERTS": "%s"}}' % theirs)
    record = consumer_trust.ensure(ctx, ENTRIES)
    assert record.value == str(theirs) and "already holds it" in record.why


def test_a_missing_file_it_is_given_is_corrected(trusting, ctx, tmp_path) -> None:
    (tmp_path / "bashrc").write_text('export NODE_EXTRA_CA_CERTS="/tmp/tmp3ynfawgm/ca-bundle.pem"\n')
    ctx.settings.set("mcp.trust.consumer.repair_broken", False)
    record = consumer_trust.ensure(ctx, ENTRIES)
    corrected = next(p for p in record.places if p.place == str(tmp_path / "bashrc"))
    assert corrected.outcome == "corrected" and "does not exist" in corrected.why_wrong


def test_a_bundle_lacking_the_root_is_merged_never_replaced(trusting, ctx, tmp_path) -> None:
    theirs = tmp_path / "theirs.pem"
    theirs.write_text("-----BEGIN CERTIFICATE-----\ntheirs\n-----END CERTIFICATE-----\n")
    ctx.settings.set("mcp.trust.consumer.repair_broken", False)
    (ctx.claude_home).mkdir(parents=True, exist_ok=True)
    (ctx.claude_home / "settings.json").write_text('{"env": {"NODE_EXTRA_CA_CERTS": "%s"}}' % theirs)
    record = consumer_trust.ensure(ctx, ENTRIES)
    merged = Path(record.value).read_text()
    assert "theirs" in merged and "root" in merged and f"plus what {theirs} held" in record.why


def test_nothing_is_written_when_claude_code_trusts_the_host_already(trusting, ctx, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(consumer_trust, "consumer_trusts", lambda c, h, p, extra="": True)
    ctx.settings.set("mcp.trust.consumer.repair_broken", False)
    assert consumer_trust.ensure(ctx, ENTRIES) is None and not (tmp_path / "bashrc").exists()


def test_a_store_replacing_variable_naming_a_missing_file_goes_back_to_the_system_bundle(
    trusting, ctx, tmp_path, monkeypatch
) -> None:
    from initialize_claude_ai.core import trust_store

    system = tmp_path / "system.pem"
    system.write_text(ROOT)
    monkeypatch.setattr(trust_store, "system_bundle", lambda s: str(system))
    # Only a login shell exports the deleted bundle (~/.profile); a terminal
    # window's ~/.bashrc has the good one. Broken in one kind is broken.
    monkeypatch.setattr(
        claude_check,
        "shell_values_each",
        lambda c, names: {
            "a new login shell": {"SSL_CERT_FILE": "/tmp/tmp3ynfawgm/ca-bundle.pem"},
            "a new interactive shell": {"SSL_CERT_FILE": str(system)},
        },
    )
    fixed = consumer_trust.repair_broken(ctx)
    assert [r.variable for r in fixed] == ["SSL_CERT_FILE"] and fixed[0].value == str(system)
    assert "trusted nothing" in fixed[0].why


def test_no_variable_is_added_that_nobody_set(trusting, ctx, monkeypatch) -> None:
    monkeypatch.setattr(claude_check, "shell_values", lambda c, names: {})
    monkeypatch.setattr(claude_check, "shell_values_each", lambda c, names: {"a new login shell": {}})
    assert consumer_trust.repair_broken(ctx) == []


def test_without_a_root_on_the_machine_the_step_is_said(trusting, ctx, monkeypatch) -> None:
    monkeypatch.setattr(trusting, "chain_to_root", lambda host, port: ([LEAF], []))
    ctx.settings.set("mcp.trust.consumer.repair_broken", False)
    assert consumer_trust.ensure(ctx, ENTRIES) is None


# -- why Claude Code could not connect -----------------------------------------------------------


@pytest.mark.parametrize(
    "said, kind",
    [
        ("UNABLE_TO_GET_ISSUER_CERT_LOCALLY: unable to get local issuer certificate", "trust"),
        ("SELF_SIGNED_CERT_IN_CHAIN", "trust"),
        ("Server rejected the configured Authorization header (HTTP 401)", "credential"),
        ("Request timed out", "temporary"),
        ("CLIENT_HTTP_UNEXPECTED_CONTENT: Unexpected content type: text/html;charset=utf-8", "not_mcp"),
        ("Internal error", "server"),
        ("", ""),
    ],
)
def test_a_connection_failure_is_classified_by_its_words(settings, said, kind) -> None:
    assert claude_check.classify(said, settings) == kind


def test_a_trust_failure_gives_claude_code_the_trust_and_asks_again(ctx, monkeypatch) -> None:
    from initialize_claude_ai.mcp_auto_discovery.types import Candidate, Handshake
    from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter, Registration

    ctx.dry_run = False
    writer = McpWriter(ctx)
    candidate = Candidate(kind="url", name="openqa-example-test", url="https://openqa.example.test/mcp")
    candidate.handshake = Handshake(ok=True, tools=["t"])
    registration = Registration(
        name="openqa-example-test", candidate=candidate, entry=dict(ENTRIES["openqa-example-test"])
    )
    answers = [
        Outcome(
            claude_check.FAILED, "could not connect", reason="UNABLE_TO_GET_ISSUER_CERT_LOCALLY", reason_class="trust"
        ),
        Outcome(claude_check.PASSED, "Claude Code connects to it"),
    ]
    monkeypatch.setattr(claude_check, "enabled", lambda s: True)
    monkeypatch.setattr(claude_check, "connect_check", lambda *a, **k: answers.pop(0))
    monkeypatch.setattr(claude_check, "check", lambda *a, **k: Outcome(claude_check.PASSED, "answered", "t"))
    asked = []
    monkeypatch.setattr(
        consumer_trust, "ensure", lambda c, entries, force=False: asked.append((list(entries), force)) or object()
    )
    writer._check_through_claude(registration)
    assert asked == [(["openqa-example-test"], True)] and registration.claude_connect["outcome"] == claude_check.PASSED


def test_packaged_settings_equal_the_code(settings) -> None:
    c = settings.section("mcp.trust.consumer")
    assert c.str_("variable") == consumer_trust.VARIABLE and c.bool_("proactive") is True
    assert list(c.list_("store_command", [])) == list(consumer_trust.STORE_COMMAND)
    assert list(c.str_list("trust_variables", [])) == list(consumer_trust.TRUST_VARIABLES)
    assert list(c.str_list("replacing_variables", [])) == list(consumer_trust.REPLACING_VARIABLES)
    assert (
        c.str_("bundle_dir") == consumer_trust.BUNDLE_DIR
        and c.str_("bundle_filename") == consumer_trust.BUNDLE_FILENAME
    )
    assert settings.dict_("mcp.registration.claude_check.failure_classes", {}) == claude_check.FAILURE_CLASSES
    assert list(settings.str_list("mcp.registration.fresh_env.shell_variables", [])) == list(
        claude_check.SHELL_VARIABLES
    )
    assert list(settings.list_("mcp.registration.claude_check.list_command", [])) == list(claude_check.LIST_COMMAND)


def test_a_credential_refusal_while_waiting_is_waiting_not_failed(ctx, monkeypatch) -> None:
    """With the certificate trusted, openqa.example.net answered 403 to the empty token: the server is reached."""
    import subprocess

    ctx.settings.set("mcp.registration.claude_check.connect", True)
    monkeypatch.setattr(ctx, "tool", lambda *a, **k: "claude")
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, "  Status: ✘ Failed to connect\n", "")
    )
    monkeypatch.setattr(
        claude_check,
        "_failure_reason",
        lambda c, n, e: "Server rejected the configured Authorization header (HTTP 403)",
    )
    waiting = claude_check.connect_check(ctx, "openqa-example-test", ENTRIES["openqa-example-test"], waiting=True)
    assert waiting.outcome == claude_check.MISSING_VARIABLE and waiting.reason_class == "credential"
    proven = claude_check.connect_check(ctx, "openqa-example-test", ENTRIES["openqa-example-test"], waiting=False)
    assert proven.outcome == claude_check.FAILED and "credential failure" in proven.detail
