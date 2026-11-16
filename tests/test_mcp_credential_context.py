"""A credential is recognised by its context and its use; a value is held up against its slot.

build.example.net's entry was written as ``"API_HEADERS": "${BUILD_EXAMPLE_NET_API_HEADERS:-<token>}"``:
the login token, copied from the old entry, sat in the file as a setting's
default, because ``API_HEADERS`` names no credential word and the token did not
look random enough. It was also the wrong shape -- the server reads
``API_HEADERS`` as ``Name:value`` pairs, so build.example.net never got a login --
and the crashing CLI copy of the entry stayed loaded in place of the corrected
one, because "already known to the CLI" skipped it. Two agent candidates were
lost as "of unknown kind 'package'".
"""

from __future__ import annotations

import http.server
import json
import threading

import pytest

from initialize_claude_ai.mcp_auto_discovery import secret_policy, value_shapes
from initialize_claude_ai.mcp_auto_discovery.secret_policy import holds_a_credential, is_secret_name, looks_opaque
from initialize_claude_ai.mcp_auto_discovery.types import Candidate, Handshake, StageOutcome, TargetOutcome
from initialize_claude_ai.mcp_auto_discovery.verifier import CandidateVerifier
from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter

#: 32 hex digits, fixed so the test is not at the mercy of a random draw; at
#: most 4 bits of entropy each, below the absolute threshold of 4.0.
HEX = "".join("0123456789abcdef"[(i * 7 + 3) % 16] for i in range(16)) + "9f3c1e7a52b04d68"


# -- recognising a credential ----------------------------------------------------------------


def test_a_header_list_carries_a_credential_by_its_shape() -> None:
    assert is_secret_name("API_HEADERS") and is_secret_name("SMELT_HEADERS") and is_secret_name("BASIC_AUTH")
    assert not is_secret_name("API_BASE_URL")


def test_a_hex_token_is_random_for_its_alphabet() -> None:
    assert looks_opaque(HEX), "the absolute threshold alone called this not random"
    assert not looks_opaque("a" * 32 + "1"), "a repeated pattern is still not"


def test_an_identifier_is_not_a_secret_however_random() -> None:
    assert not is_secret_name("GIT_COMMIT") and not is_secret_name("IMAGE_DIGEST")


def test_a_credential_in_use_is_a_credential_under_any_name() -> None:
    assert not holds_a_credential("HARMLESS_SETTING", HEX)
    secret_policy.remember_in_use(HEX)
    assert holds_a_credential("HARMLESS_SETTING", HEX)
    assert HEX not in repr(secret_policy._IN_USE), "only fingerprints are kept"


def test_existing_entries_teach_the_credentials_they_use(ctx, sandbox) -> None:
    from initialize_claude_ai.mcp_auto_discovery.existing import ExistingRegistrations

    ctx.settings.set("mcp.discovery.reuse.sources", ["project_file"])
    (sandbox / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "build-example-de": {"command": "npx", "args": ["-y", "x"], "env": {"API_HEADERS": HEX}},
                }
            }
        )
    )
    ExistingRegistrations(ctx).registrations()
    assert secret_policy.is_in_use(HEX)


# -- writing ----------------------------------------------------------------------------------


def candidate_with(env: dict) -> Candidate:
    return Candidate(
        kind="stdio",
        name="build-example-de",
        command=["npx", "-y", "x"],
        env={"API_BASE_URL": "https://build.example.de", **env},
        handshake=Handshake(ok=True, tools=["t"], round_trip=True),
    )


def test_a_credential_is_never_written_as_a_default(ctx) -> None:
    entry = McpWriter(ctx).entry_for(
        candidate_with({"API_HEADERS": "${BUILD_EXAMPLE_DE_API_HEADERS:-" + HEX + "}"}), "build-example-de"
    )
    assert HEX not in json.dumps(entry)
    assert entry["env"]["API_HEADERS"] == "${BUILD_EXAMPLE_DE_API_HEADERS}"


def test_a_literal_credential_becomes_a_reference(ctx) -> None:
    candidate = candidate_with({"API_HEADERS": HEX})
    entry = McpWriter(ctx).entry_for(candidate, "build-example-de")
    assert HEX not in json.dumps(entry)
    assert entry["env"]["API_HEADERS"].startswith("${") and "API_HEADERS" in candidate.secret_env


def test_a_copied_in_use_value_is_not_written_under_a_harmless_name(ctx) -> None:
    secret_policy.remember_in_use(HEX)
    entry = McpWriter(ctx).entry_for(candidate_with({"SOME_SETTING": HEX}), "build-example-de")
    assert HEX not in json.dumps(entry)


# -- the shape of a value ------------------------------------------------------------------


def test_a_bare_token_in_a_header_list_is_named_with_the_exact_form(settings) -> None:
    problem = value_shapes.mismatch("API_HEADERS", HEX, settings)
    assert problem is not None
    remedy = value_shapes.remedy(problem, {"scheme": "basic"}, "build.example.de", "BUILD_EXAMPLE_DE_API_HEADERS")
    assert "Authorization:Basic <base64 of user:password>" in remedy
    assert "BUILD_EXAMPLE_DE_API_HEADERS" in remedy and HEX not in remedy
    assert value_shapes.mismatch("API_HEADERS", "Authorization:Basic abc, X-Trace:1", settings) is None


class Obs(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'basic realm="API login"')
        self.end_headers()

    def log_message(self, *_args):
        pass


@pytest.fixture(scope="module")
def obs():
    server = http.server.HTTPServer(("127.0.0.1", 0), Obs)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_the_method_comes_from_the_server_s_challenge(ctx, obs) -> None:
    candidate = Candidate(kind="stdio", name="build-example-de", command=["npx", "x"], env={"API_BASE_URL": obs})
    problems = CandidateVerifier(ctx).shape_problems(candidate, {"API_HEADERS": HEX})
    assert problems and "Authorization:Basic" in problems[0]["remedy"]


def test_a_shape_problem_reaches_the_table_and_the_remedies(ctx) -> None:
    candidate = candidate_with({})
    candidate.handshake.shape_problems = [{"name": "API_HEADERS", "remedy": "API_HEADERS takes Name:value pairs"}]
    row = next(c for c in CandidateVerifier.audit(candidate) if c.name == "configured")
    assert row.ok is False and "Name:value" in row.detail


# -- choosing --------------------------------------------------------------------------------


def test_named_parts_outrank_a_hand_encoded_header(ctx) -> None:
    from initialize_claude_ai.mcp_auto_discovery.ranking import CandidateRanker

    encoded = candidate_with({})
    encoded.secret_env = {"API_HEADERS": "env:BUILD_EXAMPLE_DE_API_HEADERS"}
    parts = candidate_with({})
    parts.secret_env = {"BUILD_EXAMPLE_DE_USERNAME": "env:A", "BUILD_EXAMPLE_DE_PASSWORD": "env:B"}
    ranker = CandidateRanker(ctx)
    assert ranker._configuration(parts)[0] > ranker._configuration(encoded)[0]
    ctx.settings.set("mcp.ranking.penalties.encoded_credential", 0)
    assert "encode by hand" not in CandidateRanker(ctx)._configuration(parts)[1]


# -- existing registrations ----------------------------------------------------------------------


def test_a_registration_that_does_not_start_is_replaced(ctx, sandbox) -> None:
    from initialize_claude_ai.mcp_auto_discovery.existing import ExistingRegistrations

    ctx.settings.set("mcp.discovery.reuse.sources", ["project_file"])
    (sandbox / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "build-example-de": {
                        "command": "npx",
                        "args": ["-y", "x"],
                        "env": {"API_BASE_URL": "https://build.example.de"},
                    },
                }
            }
        )
    )
    existing = ExistingRegistrations.for_run(ctx)
    old = existing.candidate(existing.registrations()[0], "package")
    old.handshake = Handshake(ok=False, error="process exited with rc=1: at async ToolsManager.initialize")
    outcome = TargetOutcome(target="https://build.example.de")
    outcome.stages.append(StageOutcome(stage="package", candidates=[old]))
    found = McpWriter(ctx)._unusable_existing(outcome)
    assert found["build-example-de"][0] is old and "does not start" in found["build-example-de"][1]


def test_a_cli_copy_that_differs_is_reported_as_shadowing(ctx, monkeypatch) -> None:
    from initialize_claude_ai.mcp_auto_discovery import existing as existing_module
    from initialize_claude_ai.mcp_auto_discovery.writer import Registration

    monkeypatch.setattr(
        existing_module.ExistingRegistrations, "cli_entry", lambda self, name: {"command": "npx", "args": ["-y", "old"]}
    )
    registration = Registration(
        name="build-example-de",
        candidate=candidate_with({}),
        entry={"command": "npx", "args": ["-y", "new", "--operation", "get"]},
    )
    McpWriter(ctx)._report_shadowing(registration)
    assert any("takes precedence" in line for line in registration.remediation)


# -- agent vocabulary ----------------------------------------------------------------------------


def test_an_agent_s_package_kind_is_a_stdio_candidate(ctx) -> None:
    from initialize_claude_ai.mcp_auto_discovery.agentic import AgenticPipeline

    candidate, why = AgenticPipeline(ctx)._candidate_from(
        {"kind": "package", "name": "build-example-de", "command": ["npx", "-y", "x"]}, origin="package"
    )
    assert candidate is not None, why
    assert candidate.kind == "stdio"


def test_packaged_settings_equal_the_code(settings) -> None:
    policy = settings.section("mcp.secret_policy")
    assert tuple(policy.str_list("carrier_patterns")) == secret_policy.CARRIER_PATTERNS
    assert tuple(policy.str_list("selector_words")) == secret_policy.SELECTOR_WORDS
    assert settings.float_("mcp.discovery.reuse.opaque_value_min_entropy_ratio") == secret_policy.OPAQUE_MIN_RATIO
