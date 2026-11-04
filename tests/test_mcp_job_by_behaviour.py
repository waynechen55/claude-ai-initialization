"""smelt-example-net vanished from .mcp.json: a verified bridge was dropped because its tool had another name.

* The GraphQL job ("describe the API") was found by tool name only; the agent's
  bridge named its introspection tool otherwise, could "not be asked the job",
  settled nothing, and the address ended with nothing. A task's tool is now
  found by what a safe read tool says it does, or by an answer of the API's own
  form -- and a verified server is written rather than leaving the address empty.
* The agent was retried with no word of why its verified bridge did not count.
* A candidate of "unknown kind 'bridge'" was discarded although it carried a command.
* The root smelt's chain needed was only in a CA file lost with the scratch
  folder; the program's own copies are searched for issuers too.
"""

from __future__ import annotations

import json
import sys

import pytest

from initialize_claude_ai.mcp_auto_discovery import contract
from initialize_claude_ai.mcp_auto_discovery.agentic import AgenticPipeline, _correction
from initialize_claude_ai.mcp_auto_discovery.types import Candidate, Handshake, StageOutcome, TargetOutcome
from initialize_claude_ai.mcp_auto_discovery.verifier import CandidateVerifier

SCHEMA = json.dumps({"data": {"__schema": {"queryType": {"name": "Query"}}}})

SERVER = (
    """
import json, sys
tools = [
    {"name": "schema_dump", "description": "Returns everything the service has", "annotations": {"readOnlyHint": True},
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "drop_everything", "description": "Introspect and delete", "annotations": {"destructiveHint": True},
     "inputSchema": {"type": "object", "properties": {}}},
]
for line in sys.stdin:
    msg = json.loads(line)
    rid, method = msg.get("id"), msg.get("method")
    if rid is None:
        continue
    if method == "initialize":
        result = {"protocolVersion": msg["params"]["protocolVersion"], "capabilities": {"tools": {}},
                  "serverInfo": {"name": "smelt-graphql", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": tools}
    elif method == "tools/call":
        if msg["params"]["name"] == "drop_everything":
            raise SystemExit("never call me")
        result = {"content": [{"type": "text", "text": %r}]}
    else:
        result = {}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}) + "\\n")
    sys.stdout.flush()
"""
    % SCHEMA
)


def _task(settings):
    return next(
        t for t in contract.tasks_for(settings, "smelt.example.test", ["graphql"]) if t.name == "describe the API"
    )


# -- what the job is matched by --------------------------------------------------------------------


def test_a_task_s_tool_is_found_by_what_a_safe_read_tool_says_it_does(settings) -> None:
    specs = [
        {"name": "schema_view", "description": "Show the GraphQL schema", "read_only": True, "required": []},
        {"name": "nuke", "description": "introspect then drop", "destructive": True, "required": []},
    ]
    calls, _ = contract.resolve([_task(settings)], specs, settings=settings)
    assert [c["tool"] for c in calls] == ["schema_view"]
    assert contract.resolve([_task(settings)], specs[1:], settings=settings)[0] == [], "never a destructive tool"
    settings.set("mcp.contract.match_descriptions", False)
    assert contract.resolve([_task(settings)], specs, settings=settings)[0] == []


def test_a_task_is_done_by_a_read_tool_whose_answer_carries_the_kind_s_words(ctx, tmp_path) -> None:
    ctx.dry_run = False
    ctx.runner.which = lambda name, **_: name  # type: ignore[assignment]
    ctx.state["mcp_address_job"] = {"smelt.example.test": ["graphql"]}
    script = tmp_path / "server.py"
    script.write_text(SERVER)
    candidate = CandidateVerifier(ctx).verify(
        Candidate(
            kind="stdio",
            name="smelt-example-test-graphql",
            command=[sys.executable, str(script)],
            env={"ENDPOINT": "https://smelt.example.test/graphql/"},
        )
    )
    rows = {r["task"]: r for r in candidate.handshake.contract}
    assert rows["describe the API"]["tool"] == "schema_dump" and rows["describe the API"]["passed"]
    assert rows["describe the API"]["found_by"] == "answer" and candidate.verified


def test_the_job_is_not_missed_when_a_tool_did_it_by_its_answer(ctx, monkeypatch) -> None:
    from initialize_claude_ai.mcp_auto_discovery.traditional import TraditionalPipeline

    pipeline = TraditionalPipeline(ctx)
    monkeypatch.setattr(pipeline, "know_job", lambda outcome: ["graphql"])
    candidate = Candidate(kind="stdio", name="s", command=["x"])
    candidate.handshake = Handshake(
        ok=True, tools=["schema_dump"], tool_specs=[{"name": "schema_dump", "required": []}]
    )
    outcome = TargetOutcome(target="https://smelt.example.test")
    assert pipeline.misses_the_job(candidate, outcome) == ["describe the API"]
    candidate.handshake.contract = [{"task": "describe the API", "tool": "schema_dump", "passed": True}]
    assert pipeline.misses_the_job(candidate, outcome) == []


# -- a verified server that settled nothing ---------------------------------------------------------


def test_the_retry_brief_says_why_a_verified_candidate_did_not_count(settings) -> None:
    candidate = Candidate(kind="stdio", name="smelt-graphql", command=["python3", "b.py"])
    candidate.handshake = Handshake(ok=True, tools=["a", "b", "c"])
    candidate.unsettled = "no tool of it can be asked what the address is for (describe the API)"
    stage = StageOutcome(stage="strategy", candidates=[candidate], verified=[candidate])
    text = _correction(stage, 1, None, settings)
    assert "verified, but did not settle" in text and "describe the API" in text and "rather than starting over" in text


def test_an_address_with_only_an_unsettled_verified_server_still_gets_it(ctx, sandbox) -> None:
    from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter

    ctx.dry_run = False
    ctx.settings.set("mcp.mcp_json_path", str(sandbox / ".mcp.json"))
    script = sandbox / "smelt_graphql_mcp.py"
    script.write_text("print()\n")
    candidate = Candidate(
        kind="stdio",
        name="smelt-example-test-graphql",
        command=[sys.executable, str(script)],
        env={"SMELT_GRAPHQL_URL": "https://smelt.example.test/graphql/"},
    )
    candidate.handshake = Handshake(ok=True, tools=["a"])
    candidate.unsettled = "no tool of it can be asked what the address is for"
    assert candidate.verified
    outcome = TargetOutcome(target="https://smelt.example.test")
    outcome.stages.append(StageOutcome(stage="strategy", candidates=[candidate]))
    ctx.settings.set("mcp.registration.verify_as_written", False)
    McpWriter(ctx).publish(outcome)
    servers = json.loads((sandbox / ".mcp.json").read_text())["mcpServers"]
    assert servers, "never empty while a verified server exists"
    assert any("settled nothing" in note for note in candidate.notes)


# -- an agent's unknown word, and lost roots -----------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "kind"),
    [
        ({"kind": "bridge", "name": "smelt-graphql", "command": ["python3", "/x/b.py"]}, "stdio"),
        ({"kind": "server", "name": "x", "url": "https://x.example.test/mcp"}, "url"),
    ],
)
def test_an_unknown_kind_word_is_decided_by_the_candidate_s_shape(ctx, raw, kind) -> None:
    made = AgenticPipeline(ctx)
    candidate, note = made._candidate_from(raw, origin="strategy")
    assert candidate is not None, note
    assert candidate.kind == kind


def test_issuers_are_searched_in_this_program_s_own_copies(ctx, tmp_path) -> None:
    from initialize_claude_ai.core.trust_store import TrustStore

    home = tmp_path / "servers"
    (home / "smelt-example-test").mkdir(parents=True)
    cryptography = pytest.importorskip("cryptography.x509")
    from datetime import datetime, timedelta, timezone

    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = cryptography.Name([cryptography.NameAttribute(NameOID.COMMON_NAME, "Example Lost Root")])
    now = datetime.now(timezone.utc)
    cert = (
        cryptography.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=30))
        .sign(key, hashes.SHA256())
    )
    (home / "smelt-example-test" / "bundle.pem").write_text(cert.public_bytes(serialization.Encoding.PEM).decode())
    ctx.settings.set("mcp.registration.artifacts_home", str(home))
    ctx.settings.set("mcp.trust.issuer_sources", [])
    ctx.settings.set("mcp.trust.issuers_from_entries", False)
    pool = TrustStore(ctx.settings).issuer_pool()
    assert any("BEGIN CERTIFICATE" in p for p in pool)
    from initialize_claude_ai.core.trust_store import describe

    assert any(describe(p)["subject"].endswith("Example Lost Root") for p in pool)


def test_packaged_settings_equal_the_code(settings) -> None:
    assert settings.bool_("mcp.contract.match_descriptions") and settings.bool_("mcp.contract.match_answers")
    assert settings.int_("mcp.contract.answer_probe_max_tools") == contract.DEFAULT_ANSWER_PROBES
    assert settings.bool_("mcp.registration.never_empty_when_verified") is True
    assert settings.bool_("mcp.agent.kind_from_shape") is True
    assert settings.bool_("mcp.trust.issuers_from_program_dirs") is True
