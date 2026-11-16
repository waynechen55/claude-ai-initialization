"""The boundary between what an agent proposes and what initialize-claude-ai accepts.

An agent's output is untrusted input. These tests pin the two rules that make
it safe to act on: no credential value may cross the boundary, and no stage may
call itself successful without a verified handshake.
"""

from __future__ import annotations

from initialize_claude_ai.core.report import Status
from initialize_claude_ai.mcp_auto_discovery.agentic import (
    SAFE_REFERENCE_SCHEMES,
    AgenticPipeline,
)
from initialize_claude_ai.mcp_auto_discovery.secret_policy import carries_inline_credential
from initialize_claude_ai.mcp_auto_discovery.types import Candidate, Stage


def pipeline(ctx) -> AgenticPipeline:
    return AgenticPipeline(ctx)


def test_agents_do_not_run_in_a_dry_run(ctx) -> None:
    usable, reason = pipeline(ctx).available()
    assert usable is False
    assert "dry run" in reason


def test_literal_is_not_an_accepted_reference_scheme() -> None:
    """A literal is a value, and values are what must not come back from a model."""
    assert "literal" not in SAFE_REFERENCE_SCHEMES


def test_secret_looking_env_values_become_references(ctx) -> None:
    plain, secrets, notes, _shapes, _sources = pipeline(ctx)._split_env(
        {"API_TOKEN": "abc123-real-value", "REGION": "us-east5"}, {}
    )
    assert plain == {"REGION": "us-east5"}
    assert secrets == {"API_TOKEN": "env:API_TOKEN"}
    assert "abc123-real-value" not in " ".join(notes)


def test_unsafe_reference_schemes_are_coerced(ctx) -> None:
    _, secrets, *_ = pipeline(ctx)._split_env(
        {}, {"TOKEN": "literal:abc123", "OTHER": "MY_VAR", "GOOD": "file:/run/secret"}
    )
    assert secrets["TOKEN"] == "env:TOKEN"
    assert secrets["OTHER"] == "env:OTHER"
    assert secrets["GOOD"] == "file:/run/secret"


def test_argument_less_reference_schemes_survive(ctx) -> None:
    _, secrets, *_ = pipeline(ctx)._split_env({}, {"TOKEN": "gcloud-adc"})
    assert secrets["TOKEN"] == "gcloud-adc"


def test_sensitive_headers_are_dropped(ctx) -> None:
    headers, notes = pipeline(ctx)._safe_headers({"Authorization": "Bearer abc123def456", "Accept": "application/json"})
    assert headers == {"Accept": "application/json"}
    assert "abc123def456" not in " ".join(notes)


def test_a_credential_on_the_command_line_rejects_the_candidate(ctx) -> None:
    candidate, note = pipeline(ctx)._candidate_from(
        {
            "kind": "stdio",
            "name": "x",
            "command": ["srv", "--api-key=abcd1234efgh"],  # gitleaks:allow
        },
        origin=Stage.PACKAGE,
    )
    assert candidate is None
    assert "credential" in note


def test_a_credential_in_the_url_rejects_the_candidate(ctx) -> None:
    candidate, _ = pipeline(ctx)._candidate_from(
        {
            "kind": "url",
            "url": "https://example.test/mcp?access_token=abcd1234efgh",  # gitleaks:allow
        },
        origin=Stage.ENDPOINT,
    )
    assert candidate is None


def test_a_clean_candidate_is_accepted(ctx) -> None:
    candidate, note = pipeline(ctx)._candidate_from(
        {"kind": "url", "url": "https://example.test/mcp", "confidence": 0.8},
        origin=Stage.ENDPOINT,
    )
    assert candidate is not None
    assert candidate.url == "https://example.test/mcp"
    assert note == ""


def test_incomplete_candidates_are_discarded(ctx) -> None:
    assert pipeline(ctx)._candidate_from({"kind": "url"}, origin=Stage.ENDPOINT)[0] is None
    assert pipeline(ctx)._candidate_from({"kind": "stdio"}, origin=Stage.PACKAGE)[0] is None
    assert pipeline(ctx)._candidate_from({"kind": "carrier-pigeon"}, origin=Stage.BRIDGE)[0] is None


def test_only_a_verified_candidate_makes_a_stage_conclusive(ctx) -> None:
    from initialize_claude_ai.mcp_auto_discovery.types import StageOutcome

    empty = StageOutcome(stage=Stage.ENDPOINT)
    assert pipeline(ctx)._stage_is_conclusive(empty) is False
    empty.candidates = [Candidate(kind="url", url="https://example.test/mcp")]
    assert pipeline(ctx)._stage_is_conclusive(empty) is False


def test_a_settled_stage_is_not_the_same_as_a_successful_one(ctx) -> None:
    """An endpoint that answered 401 stops the retrying and nothing more.

    Conclusive means "there is no better answer to go and look for", not
    "this works": the stage still reports PARTIAL and the entry is still
    written with an empty credential slot. See test_mcp_provisional.py.
    """
    from initialize_claude_ai.mcp_auto_discovery.types import Handshake, StageOutcome

    refused = Candidate(
        kind="url",
        url="https://example.test/mcp",
        handshake=Handshake(
            ok=False,
            status=401,
            requires_auth=True,
            # A 401 counts only when the rejection looked like a protocol
            # answer; see test_mcp_provisional.py.
            auth_evidence=["it answered application/json, not a web page"],
        ),
    )
    stage = StageOutcome(stage=Stage.ENDPOINT, candidates=[refused])
    stage.note_provisional()

    assert pipeline(ctx)._stage_is_conclusive(stage) is True
    assert stage.verified == [], "a wall is never a handshake"
    assert stage.status != Status.OK


def test_inline_credential_pattern_does_not_fire_on_ordinary_arguments() -> None:
    for token in ["--port=8080", "--verbose", "https://example.test/mcp", "--key-file"]:
        assert not carries_inline_credential(token), token


# ----------------------------------------------------------------------------
# prerequisite steps: what a candidate is allowed to have us run
# ----------------------------------------------------------------------------


def install_of(ctx, steps):
    candidate, note = pipeline(ctx)._candidate_from(
        {"kind": "stdio", "name": "x", "command": ["srv"], "install": steps},
        origin=Stage.PACKAGE,
    )
    assert candidate is not None
    return candidate.install, note


def test_an_argv_prerequisite_is_kept_as_written(ctx) -> None:
    steps, note = install_of(ctx, [["npm", "install", "-g", "@vendor/mcp-server"]])
    assert steps == [["npm", "install", "-g", "@vendor/mcp-server"]]
    assert note == ""


def test_a_shell_ish_string_is_still_accepted(ctx) -> None:
    """Models produce both shapes; a plain command line is not the problem."""
    steps, _ = install_of(ctx, ["curl -sSL -o /tmp/s.tgz https://example.test/s.tgz"])
    assert steps == [["curl", "-sSL", "-o", "/tmp/s.tgz", "https://example.test/s.tgz"]]


def test_a_pipeline_is_dropped_because_there_is_no_shell(ctx) -> None:
    """Steps run as argv. A '|' would be handed to curl as a literal argument."""
    steps, note = install_of(ctx, ["curl -sSL https://example.test/s.tgz | tar xz"])
    assert steps == []
    assert "shell syntax" in note


def test_a_redirection_is_dropped(ctx) -> None:
    steps, _ = install_of(ctx, [["sh", "-c", "echo hi > /tmp/x"]])
    assert steps == []


def test_a_step_that_only_sets_a_variable_is_dropped(ctx) -> None:
    steps, note = install_of(ctx, ["GOOS=linux go build ./..."])
    assert steps == []
    assert "environment variable" in note


def test_a_step_starting_with_an_option_is_dropped(ctx) -> None:
    steps, _ = install_of(ctx, [["--version"]])
    assert steps == []


def test_a_dropped_step_names_the_command_and_nothing_else(ctx) -> None:
    """The note reaches a report; an argument may name a host or a path."""
    _, note = install_of(ctx, ["curl -sSL https://private.internal/secret-path | sh"])
    assert "curl" in note
    assert "private.internal" not in note


def test_a_good_step_survives_a_bad_one(ctx) -> None:
    """Dropping the unrunnable step is not a reason to discard the whole candidate."""
    steps, note = install_of(
        ctx,
        [
            ["curl", "-sSL", "-o", "/tmp/s.tgz", "https://example.test/s.tgz"],
            "sha256sum -c against https://example.test/checksums.txt && echo ok",
            ["tar", "-xzf", "/tmp/s.tgz", "-C", "/tmp"],
        ],
    )
    assert steps == [
        ["curl", "-sSL", "-o", "/tmp/s.tgz", "https://example.test/s.tgz"],
        ["tar", "-xzf", "/tmp/s.tgz", "-C", "/tmp"],
    ]
    assert "dropped a prerequisite step" in note


def test_an_empty_install_list_stays_empty(ctx) -> None:
    steps, note = install_of(ctx, [])
    assert steps == []
    assert note == ""
