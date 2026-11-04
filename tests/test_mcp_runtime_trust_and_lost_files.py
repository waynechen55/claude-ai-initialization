"""gitlab-example-net vanished from .mcp.json: Node never got the host's trust store, and a lost bridge file was final.

* mcp-graphql (Node) was trialled with no NODE_EXTRA_CA_CERTS because the host
  verified gitlab.example.net with its own store -- but Node keeps its own store
  and ignores the host's: "fetch failed". The entry it would have become named
  the host bundle; the trial now does too.
* A failure that never reached the service is repaired and proved again in
  every stage, and an agent is told it is the machine's, not the server's.
* With the generic GraphQL server failing, this program's own bridge for the
  kind (its operations, as data) backs it up.
* The working bridge was deleted from the scratch area: a copy kept when it
  last worked restores it, into the server's own folder.
"""

from __future__ import annotations

import http.server
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

from initialize_claude_ai.core.paths_home import artifacts_home
from initialize_claude_ai.mcp_auto_discovery import api_kinds, known_good
from initialize_claude_ai.mcp_auto_discovery.agentic import _correction
from initialize_claude_ai.mcp_auto_discovery.types import Candidate, Handshake, StageOutcome, TargetOutcome
from initialize_claude_ai.mcp_auto_discovery.verifier import CandidateVerifier

# -- the trial names the bundle its entry will ------------------------------------------------------


def test_a_node_trial_names_the_host_bundle_when_no_host_needed_the_run_s(ctx, monkeypatch, tmp_path) -> None:
    bundle = tmp_path / "host-bundle.pem"
    bundle.write_text("pem\n")
    ctx.dry_run = False
    ctx.settings.set("mcp.trust.bundle", str(bundle))
    ctx.runner.which = lambda name, **_: f"/usr/bin/{name}"  # type: ignore[assignment]
    seen: list[dict] = []
    verifier = CandidateVerifier(ctx)

    def probe(**launch):
        seen.append(dict(launch.get("env") or {}))
        return Handshake(ok=True, tools=["introspect-schema"])

    monkeypatch.setattr(verifier.prober, "probe_stdio", probe)
    candidate = Candidate(
        kind="stdio",
        name="gitlab-example-test-mcp-graphql",
        command=["npx", "-y", "mcp-graphql"],
        env={"ENDPOINT": "https://gitlab.example.test/api/graphql"},
    )
    verifier.verify(candidate)
    assert seen and seen[0].get("NODE_EXTRA_CA_CERTS") == str(bundle)

    seen.clear()
    ctx.settings.set("mcp.trust.trial_names_host_bundle", False)
    verifier.verify(
        Candidate(
            kind="stdio",
            name="g2",
            command=["npx", "-y", "mcp-graphql"],
            env={"ENDPOINT": "https://gitlab.example.test/api/graphql"},
        )
    )
    assert seen and "NODE_EXTRA_CA_CERTS" not in seen[0]


# -- never reached the service: repaired everywhere, and said so ---------------------------------


def test_every_stage_s_candidates_are_repaired_when_they_never_reached_the_service(ctx, monkeypatch) -> None:
    from initialize_claude_ai.mcp_auto_discovery.traditional import TraditionalPipeline

    pipeline = TraditionalPipeline(ctx)
    healed: list[str] = []
    monkeypatch.setattr(pipeline, "heal_transport", lambda c, o: healed.append(c.name) or False)
    new = Candidate(kind="stdio", name="new", command=["npx", "x"])
    old = Candidate(kind="stdio", name="old", command=["npx", "y"])
    old.existing_name = "old"
    pipeline.heal_or_discard(
        StageOutcome(stage="package", candidates=[new, old]), TargetOutcome(target="https://x.test")
    )
    assert healed == ["new"], "an existing one is healed where reuse judges it"


def test_an_agent_is_told_a_failure_is_the_machine_s_not_the_server_s(settings) -> None:
    candidate = Candidate(kind="stdio", name="gitlab-graphql", command=["npx", "mcp-graphql"])
    candidate.handshake = Handshake(
        ok=False, error="usage task 'describe the API' failed: Failed to introspect schema: TypeError: fetch failed"
    )
    text = _correction(StageOutcome(stage="strategy", candidates=[candidate]), 2, None, settings)
    assert "never reached the service" in text and "not the server" in text


# -- the program's own bridge for a kind ------------------------------------------------------------


class _GraphQL(http.server.BaseHTTPRequestHandler):
    received: list[dict] = []

    def do_POST(self):  # noqa: N802 - the stdlib's name
        body = json.loads(self.rfile.read(int(self.headers["content-length"])) or b"{}")
        _GraphQL.received.append(body)
        answer = (
            {"data": {"__schema": {"queryType": {"name": "Query"}}}}
            if "__schema" in body.get("query", "")
            else {"data": {"echo": body.get("variables")}}
        )
        data = json.dumps(answer).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_a):
        pass


def test_the_graphql_kind_bridge_reads_and_refuses_mutations(ctx) -> None:
    from initialize_claude_ai.mcp_auto_discovery.bridge import BridgeBuilder

    server = http.server.HTTPServer(("127.0.0.1", 0), _GraphQL)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        ctx.dry_run = False
        ctx.settings.set("mcp.trust.bundle_candidates", "")
        url = f"http://127.0.0.1:{server.server_address[1]}/api/graphql"
        kind = next(k for k in api_kinds.kinds(ctx.settings) if k.name == "graphql")
        spec = api_kinds.spec_for(kind, url)
        candidate, artifacts, detail = BridgeBuilder(ctx).generate_from_openapi(
            "gitlab.example.test", spec, base_url=url, spec_is_evidence=False
        )
        assert candidate is not None, detail
        tools = {t["name"]: t for t in json.loads(Path(artifacts.manifest).read_text())["operations"]}
        assert set(tools) == {"introspect_schema", "graphql_query"} and all(t["read_only"] for t in tools.values())
        assert tools["graphql_query"]["path"] == "/api/graphql"
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "introspect_schema", "arguments": {}},
            },
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "graphql_query", "arguments": {"query": "{ echo }", "variables": '{"a": 1}'}},
            },
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "graphql_query", "arguments": {"query": "mutation { drop }"}},
            },
        ]
        done = subprocess.run(
            [sys.executable, str(artifacts.script)],
            capture_output=True,
            text=True,
            timeout=30,
            input="".join(json.dumps(r) + "\n" for r in requests),
            env={
                **os.environ,
                **{k: str(v) for k, v in candidate.env.items()},
                "MCP_BRIDGE_BASE_URL": f"http://127.0.0.1:{server.server_address[1]}",
            },
        )
        replies = {r["id"]: r for r in (json.loads(x) for x in done.stdout.splitlines() if x.strip())}
        assert "Query" in replies[2]["result"]["content"][0]["text"]
        assert '"a": 1' in replies[3]["result"]["content"][0]["text"]
        assert replies[4]["result"]["isError"] and "only reads" in replies[4]["result"]["content"][0]["text"]
        assert all("mutation" not in r.get("query", "") for r in _GraphQL.received), "never sent"
    finally:
        server.shutdown()


def test_the_kind_bridge_backs_up_generic_servers_only_once_they_failed(ctx) -> None:
    from initialize_claude_ai.mcp_auto_discovery.traditional import TraditionalPipeline

    pipeline = TraditionalPipeline(ctx)
    kind = next(k for k in api_kinds.kinds(ctx.settings) if k.name == "graphql")
    outcome = TargetOutcome(target="https://gitlab.example.test")
    assert not pipeline._generic_servers_failed(outcome, kind), "not tried: theirs first"
    failed = Candidate(kind="stdio", name="g", command=["npx", "-y", "mcp-graphql@2.0.4"])
    failed.handshake = Handshake(ok=False, error="fetch failed")
    outcome.stages.append(StageOutcome(stage="package", candidates=[failed]))
    assert pipeline._generic_servers_failed(outcome, kind)
    failed.handshake = Handshake(ok=True, tools=["introspect-schema"])
    assert not pipeline._generic_servers_failed(outcome, kind), "one that passed: nothing to back up"


# -- lost files ------------------------------------------------------------------------------------


def test_a_lost_bridge_is_restored_from_the_copy_kept_when_it_worked(ctx, sandbox) -> None:
    ctx.dry_run = False
    own = Path(os.environ["HOME"]) / "work" / "artifacts" / "gitlab-example-test" / "bridge-gitlab-example-test"
    own.mkdir(parents=True)
    (own / "bridge.py").write_text("print('bridge')\n")
    (own / "manifest.json").write_text("{}")
    entry = {"command": "/usr/bin/python3", "args": ["-I", str(own / "bridge.py")]}
    files = known_good.snapshot(ctx, "gitlab-example-test", entry)
    assert str(own) in files, "the bridge's own folder is kept, manifest included"
    known_good.path(ctx).write_text(
        json.dumps(
            {
                "https://gitlab.example.test": [
                    {"name": "gitlab-example-test", "entry": entry, "tasks": ["t"], "files": files}
                ]
            }
        )
    )
    for item in own.iterdir():
        item.unlink()
    own.rmdir()

    candidate = Candidate(
        kind="stdio", name="gitlab-example-test", command=["/usr/bin/python3", "-I", str(own / "bridge.py")]
    )
    restored = known_good.restore_missing(ctx, "https://gitlab.example.test", candidate)
    home = artifacts_home(ctx.settings) / "gitlab-example-test"
    assert restored and candidate.command[2] == str(home / own.name / "bridge.py")
    assert (home / own.name / "manifest.json").is_file() and Path(candidate.command[2]).is_file()


def test_nothing_is_snapshotted_outside_the_configured_roots(ctx) -> None:
    ctx.dry_run = False
    assert known_good.snapshot(ctx, "x", {"command": "/usr/bin/python3", "args": ["/etc/hostname"]}) == {}


def test_packaged_settings_equal_the_code(settings) -> None:
    assert settings.bool_("mcp.trust.trial_names_host_bundle") is True
    assert settings.bool_("mcp.verification.heal_transport_everywhere") is True
    assert settings.bool_("mcp.discovery.api_kind_bridge_when_generic_fails") is True
    assert settings.str_("mcp.known_good.files_dir") == known_good.DEFAULT_FILES_DIR
    assert settings.bool_("mcp.known_good.keep_files") and settings.bool_("mcp.known_good.restore_missing_files")
    assert settings.bool_("mcp.registration.adopt_existing_files")
    assert settings.bool_("mcp.registration.replace_lost_files_cli_copies")
