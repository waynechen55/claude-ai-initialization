"""A server already registered here is a candidate, verified like any other.

smelt.example.net had a working ``mcp-graphql`` entry in ``.mcp.json`` and a later
run ended with nothing for it: discovery searched from scratch, never looked at
what the machine already had, and had nothing to fall back to when its own
search came up short.
"""

from __future__ import annotations

import json
from pathlib import Path

from initialize_claude_ai.core.report import Status
from initialize_claude_ai.mcp_auto_discovery.agentic import AgenticPipeline
from initialize_claude_ai.mcp_auto_discovery.existing import (
    CLAUDE_CLI,
    ExistingRegistrations,
    parse_get,
)
from initialize_claude_ai.mcp_auto_discovery.traditional import TraditionalPipeline
from initialize_claude_ai.mcp_auto_discovery.types import (
    Handshake,
    Stage,
    StageOutcome,
    TargetOutcome,
)

LITERAL = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"

SERVERS = {
    "smelt-example-test": {
        "type": "stdio",
        "command": "npx",
        "args": ["-y", "mcp-graphql@2.0.0"],
        "env": {"ENDPOINT": "https://smelt.example.test/graphql", "API_TOKEN": LITERAL},
    },
    "docs-example-test": {
        "type": "http",
        "url": "https://docs.example.test/mcp",
        "headers": {"Authorization": f"Bearer {LITERAL}"},
    },
    "wiki-example-test": {
        "type": "http",
        "url": "https://wiki.example.test/mcp",
        "headers": {"Authorization": "Bearer ${WIKI_TOKEN}"},
    },
    "qa-example-test": {
        "type": "stdio",
        "command": "python3",
        "args": ["/opt/bridges/mcp_bridge_qa-example-test.py"],
        "env": {"QA_BASE_URL": "https://qa.example.test"},
    },
    "unrelated": {"type": "stdio", "command": "npx", "args": ["-y", "something-else"]},
}


def project_file(servers=None) -> Path:
    path = Path.cwd() / ".mcp.json"
    path.write_text(json.dumps({"mcpServers": SERVERS if servers is None else servers}))
    return path


def found(ctx, target: str, stage: str):
    return ExistingRegistrations(ctx).for_target(target, stage)


# -- reading and matching ------------------------------------------------------------


def test_a_local_entry_for_the_host_is_a_package_candidate(ctx) -> None:
    project_file()
    [candidate] = found(ctx, "smelt.example.test", Stage.PACKAGE)
    assert candidate.kind == "stdio" and candidate.origin == Stage.PACKAGE
    assert candidate.full_command() == ["npx", "-y", "mcp-graphql@2.0.0"]
    assert candidate.env["ENDPOINT"] == "https://smelt.example.test/graphql"
    assert "already registered" in candidate.source


def test_a_remote_entry_is_an_endpoint_candidate(ctx) -> None:
    project_file()
    [candidate] = found(ctx, "https://wiki.example.test", Stage.ENDPOINT)
    assert candidate.kind == "url" and candidate.url == "https://wiki.example.test/mcp"
    assert candidate.headers["Authorization"] == "Bearer ${WIKI_TOKEN}"
    assert candidate.secret_env == {"WIKI_TOKEN": "env:WIKI_TOKEN"}
    assert found(ctx, "https://wiki.example.test", Stage.PACKAGE) == []


def test_a_generated_bridge_is_a_bridge_candidate(ctx) -> None:
    project_file()
    assert found(ctx, "qa.example.test", Stage.PACKAGE) == []
    [candidate] = found(ctx, "qa.example.test", Stage.BRIDGE)
    assert candidate.origin == Stage.BRIDGE


def test_extra_bridge_patterns_are_honoured(ctx) -> None:
    ctx.settings.set("mcp.discovery.reuse.bridge_patterns", [r"something-else"])
    project_file({"x": {"command": "npx", "args": ["something-else", "https://x.example.test"]}})
    assert found(ctx, "x.example.test", Stage.BRIDGE)


def test_an_entry_for_another_host_is_not_offered(ctx) -> None:
    project_file()
    assert found(ctx, "elsewhere.example.test", Stage.PACKAGE) == []


def test_the_setting_turns_it_off(ctx) -> None:
    project_file()
    ctx.settings.set("mcp.discovery.reuse_existing", False)
    assert found(ctx, "smelt.example.test", Stage.PACKAGE) == []


# -- no credential is carried ------------------------------------------------------


def test_a_literal_env_secret_becomes_a_reference(ctx) -> None:
    project_file()
    [candidate] = found(ctx, "smelt.example.test", Stage.PACKAGE)
    assert "API_TOKEN" not in candidate.env
    assert candidate.secret_env == {"API_TOKEN": "env:SMELT_EXAMPLE_TEST_API_TOKEN"}
    assert LITERAL not in json.dumps(candidate.to_dict(), default=str)
    assert any("export SMELT_EXAMPLE_TEST_API_TOKEN" in note for note in candidate.notes)


def test_a_literal_header_secret_becomes_a_reference(ctx) -> None:
    project_file()
    [candidate] = found(ctx, "docs.example.test", Stage.ENDPOINT)
    assert candidate.headers["Authorization"] == "Bearer ${DOCS_EXAMPLE_TEST_AUTHORIZATION}"
    assert candidate.secret_env == {"DOCS_EXAMPLE_TEST_AUTHORIZATION": "env:DOCS_EXAMPLE_TEST_AUTHORIZATION"}
    assert LITERAL not in json.dumps(candidate.to_dict(), default=str)


def test_the_variable_name_is_configurable(ctx) -> None:
    ctx.settings.set("mcp.discovery.reuse.literal_env_template", "REUSED_{KEY}")
    project_file()
    [candidate] = found(ctx, "smelt.example.test", Stage.PACKAGE)
    assert candidate.secret_env == {"API_TOKEN": "env:REUSED_API_TOKEN"}


def test_a_secret_inside_an_argument_is_not_reused(ctx) -> None:
    project_file({"s": {"command": "srv", "args": ["--url", "https://s.example.test", f"--token={LITERAL}"]}})
    assert found(ctx, "s.example.test", Stage.PACKAGE) == []


# -- the claude CLI --------------------------------------------------------------------

GET = f"""jira-example-test:
  Scope: User config (available in all your projects)
  Status: ✔ Connected
  Type: stdio
  Command: npx
  Args: -y jira-datacenter-mcp@0.6.0
  Environment:
    JIRA_HOST=jira.example.test
    JIRA_API_TOKEN={LITERAL}

To remove this server, run: claude mcp remove jira-example-test -s user
"""


def test_the_get_report_reads_back_into_an_entry(ctx) -> None:
    entry = parse_get(GET, ctx.settings.dict_("mcp.discovery.reuse.get_fields"))
    assert entry["command"] == "npx"
    assert entry["args"] == ["-y", "jira-datacenter-mcp@0.6.0"]
    assert entry["env"]["JIRA_HOST"] == "jira.example.test"


def test_a_cli_registration_is_offered_without_its_secret(ctx, monkeypatch) -> None:
    ctx.settings.set("mcp.discovery.reuse.sources", [CLAUDE_CLI])
    monkeypatch.setattr(ExistingRegistrations, "_cli_names", lambda self: ["jira-example-test"])
    monkeypatch.setattr(ExistingRegistrations, "_cli", lambda self, key, name="": GET)
    [candidate] = found(ctx, "jira.example.test", Stage.PACKAGE)
    assert candidate.full_command() == ["npx", "-y", "jira-datacenter-mcp@0.6.0"]
    assert candidate.secret_env == {"JIRA_API_TOKEN": "env:JIRA_EXAMPLE_TEST_JIRA_API_TOKEN"}
    assert LITERAL not in json.dumps(candidate.to_dict(), default=str)


def test_list_lines_are_read_by_the_configured_pattern(ctx, monkeypatch) -> None:
    listing = (
        "Checking MCP server health…\n\n"
        "a-example-test: npx -y a - ✔ Connected\n"
        "b-example-test: https://b.example.test/mcp (HTTP) - ⏸ Pending approval\n"
        "Location: /somewhere/.mcp.json\n"
    )
    monkeypatch.undo()  # put back the real _cli_names the suite stubs out
    monkeypatch.setattr(ExistingRegistrations, "_cli", lambda self, key, name="": listing)
    assert ExistingRegistrations(ctx)._cli_names() == ["a-example-test", "b-example-test"]


# -- both pipelines ----------------------------------------------------------------------


def proven(candidates, **_kwargs):
    for candidate in candidates:
        candidate.handshake = Handshake(ok=True, round_trip=True, tools=["query"])
    return list(candidates)


def test_a_working_registration_settles_the_package_stage(ctx, monkeypatch) -> None:
    project_file()
    pipeline = TraditionalPipeline(ctx)
    monkeypatch.setattr(pipeline.verifier, "verify_all", proven)
    monkeypatch.setattr(pipeline, "registrable", lambda c, o: True)
    target = TargetOutcome(target="smelt.example.test")

    stage = pipeline.reuse(Stage.PACKAGE, target)

    assert stage is not None and stage.status == Status.OK
    assert stage.verified[0].name == "smelt-example-test"


def test_the_endpoint_checklist_still_runs_and_the_endpoint_is_recorded(ctx, monkeypatch) -> None:
    project_file()
    pipeline = TraditionalPipeline(ctx)
    monkeypatch.setattr(pipeline.verifier, "verify_all", proven)
    monkeypatch.setattr(pipeline, "registrable", lambda c, o: True)
    target = TargetOutcome(target="https://wiki.example.test")

    assert pipeline.reuse(Stage.ENDPOINT, target) is None, "tier 1 is never skipped"
    stage = StageOutcome(stage=Stage.ENDPOINT, mode=pipeline.mode, status=Status.SKIPPED, detail="nothing")
    pipeline.merge_reused(stage, target)

    assert stage.status == Status.OK
    assert stage.verified[0].url == "https://wiki.example.test/mcp"
    assert "https://wiki.example.test/mcp" in target.due_diligence.native_endpoints


def test_an_unregistrable_registration_does_not_settle_anything(ctx, monkeypatch) -> None:
    project_file()
    pipeline = TraditionalPipeline(ctx)
    monkeypatch.setattr(pipeline.verifier, "verify_all", lambda c, **k: [])
    assert pipeline.reuse(Stage.PACKAGE, TargetOutcome(target="smelt.example.test")) is None


def test_the_agent_is_told_what_was_tried(ctx) -> None:
    project_file()
    pipeline = AgenticPipeline(ctx)
    target = TargetOutcome(target="smelt.example.test")
    pipeline.fallback._reused[(target.target, Stage.PACKAGE)] = found(ctx, target.target, Stage.PACKAGE)
    pipeline.fallback._reused[(target.target, Stage.PACKAGE)][0].handshake = Handshake(error="HTTP 404")

    brief = pipeline._existing_brief(Stage.PACKAGE, target)

    assert "smelt-example-test: HTTP 404" in brief
    assert "{registrations}" not in brief
    assert pipeline._existing_brief(Stage.BRIDGE, target) == ""
