"""A trust bundle that travels keeps the per-machine name, a partial chain gets a runtime that takes it,
and a server that logged a failure is not "still starting".

gitlab.example.net's graphql server failed on the second machine:

* its bundle -- the chain gitlab presents, without the self-signed root above
  it, which the run machine did not have -- was copied beside the entry and
  named ``${ICA_MCP_SERVERS}/.../ca-bundle.pem``, undoing the writer's
  ``${ICA_MCP_CA_BUNDLE:-...}``;
* that bundle works on Python 3.13 and later, which accept a partial chain,
  and failed with SSLError where ``uvx`` picked Python 3.12;
* the server then logged "Failed to load schema" and stayed alive without
  answering -- which a "still running means still starting" rule would have
  written as an unconfirmed start.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from initialize_claude_ai.mcp_auto_discovery import trust
from initialize_claude_ai.mcp_auto_discovery.types import Candidate, Transport
from initialize_claude_ai.mcp_auto_discovery.verifier import CandidateVerifier
from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter, Registration

sys.path.insert(0, str(Path(__file__).parent))
import test_mcp_trust_chain_complete as _tls  # noqa: E402  (its real TLS server and PKI)

pki, server, real_tls = _tls.pki, _tls.server, _tls.real_tls
HOST = _tls.HOST


# -- 1. the bundle travels and keeps the per-machine name, in the file written -----------------


def test_the_written_file_reads_the_bundle_through_the_per_machine_name(ctx, sandbox) -> None:
    ctx.dry_run = False
    project = sandbox / "project"
    project.mkdir()
    ctx.settings.set("mcp.mcp_json_path", str(project / ".mcp.json"))
    store = Path(ctx.state_dir) / "trust"
    store.mkdir(parents=True, exist_ok=True)
    bundle = store / "ca-bundle.pem"
    bundle.write_text("-----BEGIN CERTIFICATE-----\nAA==\n-----END CERTIFICATE-----\n")
    ctx.settings.set("mcp.trust.store_dir", str(store))
    candidate = Candidate(
        kind="stdio",
        name="gitlab-example-net",
        transport=Transport.STDIO,
        command=[sys.executable, "-V"],
        env={"SSL_CERT_FILE": str(bundle), "REQUESTS_CA_BUNDLE": str(bundle)},
    )
    writer = McpWriter(ctx)
    registration = Registration(
        name="gitlab-example-net", candidate=candidate, entry=writer.entry_for(candidate, "gitlab-example-net")
    )
    writer._write_json([registration])
    env = json.loads((project / ".mcp.json").read_text())["mcpServers"]["gitlab-example-net"]["env"]
    for key in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"):
        assert env[key].startswith("${ICA_MCP_CA_BUNDLE:-"), env[key]
        copy = env[key][len("${ICA_MCP_CA_BUNDLE:-") : -1]
        assert Path(copy).is_file() and str(ctx.state_dir) not in copy, "the file travels beside .mcp.json"


def test_a_value_already_read_through_the_per_machine_name_is_recognised(ctx, tmp_path) -> None:
    writer = McpWriter(ctx)
    assert writer.keeps_machine_bundle("SSL_CERT_FILE", "${ICA_MCP_CA_BUNDLE:-/etc/ssl/ca-bundle.pem}")
    assert not writer.keeps_machine_bundle("SOME_SETTING", "${ICA_MCP_CA_BUNDLE:-/etc/ssl/ca-bundle.pem}")
    assert not writer.keeps_machine_bundle("SSL_CERT_FILE", str(tmp_path / "elsewhere.pem"))


# -- 2. a partial chain gets a runtime that accepts one -----------------------------------------


def test_a_bundle_without_its_root_is_recorded_as_partial(real_tls, pki, server) -> None:
    real_tls.set("mcp.trust.bundle", pki["intermediate_only"])
    check = trust.check_named_bundles(
        {"SSL_CERT_FILE": pki["intermediate_only"]}, [(HOST, server)], real_tls, _tls.store(real_tls)
    )
    assert check.partial == ["SSL_CERT_FILE"]
    assert any("Python 3.13 and later" in n and "older Python" in n for n in check.notes)


def test_a_complete_bundle_is_not_partial(real_tls, pki, server) -> None:
    check = trust.check_named_bundles(
        {"SSL_CERT_FILE": pki["complete"]}, [(HOST, server)], real_tls, _tls.store(real_tls)
    )
    assert check.partial == []


def test_a_uv_launch_is_given_a_runtime_that_accepts_a_partial_chain(ctx) -> None:
    entry = {"command": "${ICA_MCP_UVX:-/root/.local/bin/uvx}", "args": ["graphql-mcp-server==1.0.2"]}
    note = McpWriter(ctx).pin_partial_chain_runtime(entry, ["SSL_CERT_FILE"])
    assert entry["args"] == ["--python", ">=3.13", "graphql-mcp-server==1.0.2"]
    assert "--mcp-trust-ca" in note


def test_the_pin_is_not_repeated_and_never_touches_other_launchers(ctx) -> None:
    writer = McpWriter(ctx)
    pinned = {"command": "uvx", "args": ["--python", "3.14", "pkg"]}
    assert writer.pin_partial_chain_runtime(pinned, ["SSL_CERT_FILE"]) == ""
    assert pinned["args"] == ["--python", "3.14", "pkg"], "an interpreter already chosen is kept"
    node = {"command": "npx", "args": ["-y", "pkg"]}
    assert writer.pin_partial_chain_runtime(node, ["NODE_EXTRA_CA_CERTS"]) == "" and node["args"] == ["-y", "pkg"]
    complete = {"command": "uvx", "args": ["pkg"]}
    assert writer.pin_partial_chain_runtime(complete, []) == "" and complete["args"] == ["pkg"]


def test_the_switch_turns_the_pin_off(ctx) -> None:
    ctx.settings.set("mcp.trust.partial_chain_runtime.enabled", False)
    entry = {"command": "uvx", "args": ["pkg"]}
    assert McpWriter(ctx).pin_partial_chain_runtime(entry, ["SSL_CERT_FILE"]) == "" and entry["args"] == ["pkg"]


# -- 3. a server that logged a failure is not "still starting" -----------------------------------

#: Writes STDERR to stderr and then never answers -- as graphql-mcp-server did
#: after "certificate verify failed ... Failed to load schema".
HANGS = """
import os, sys, time
sys.stderr.write(os.environ.get("STDERR", "") + "\\n")
sys.stderr.flush()
time.sleep(3600)
"""


@pytest.fixture
def live(ctx):
    ctx.dry_run = False
    ctx.settings.set("mcp.packages.trial_timeout_seconds", 2)
    ctx.settings.set("mcp.packages.slow_start_timeout_factor", 1.5)
    ctx.settings.set("mcp.packages.slow_start_retries", 1)
    return ctx


def hanging(tmp_path, stderr: str) -> dict:
    script = tmp_path / "server.py"
    script.write_text(HANGS)
    return {"type": "stdio", "command": sys.executable, "args": [str(script)], "env": {"STDERR": stderr}}


def test_a_server_that_logged_a_trust_failure_and_hung_is_a_failure(live, tmp_path) -> None:
    said = "requests.exceptions.SSLError: [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed"
    shake = CandidateVerifier(live).verify_as_written(hanging(tmp_path, said), "gitlab")
    assert not shake.ok and not shake.start_unconfirmed
    assert shake.stderr_cause.get("class") == "trust", "the cause the repairs act on"


def test_a_server_that_said_nothing_wrong_is_still_unconfirmed(live, tmp_path) -> None:
    shake = CandidateVerifier(live).verify_as_written(hanging(tmp_path, "Loading GraphQL schema..."), "gitlab")
    assert not shake.ok and "slow start" in shake.start_unconfirmed
