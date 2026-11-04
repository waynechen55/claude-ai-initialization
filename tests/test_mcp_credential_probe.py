"""The confluence.example.com fixes: the credential test, and what surrounds it.

confluence.example.com finished with nothing in ``.mcp.json``. Five separate
things went wrong, and each has a generic fix tested here:

1. A trial without the credential that listed **nothing**, or crashed **without
   naming** a variable, said nothing either way and was filed as a failure.
   ``mcp-atlassian`` lists no tools for a service that is not fully configured;
   ``@atlassian-dc-mcp/confluence`` exits without its token and does not say
   so. One more trial, with a stand-in value for the credential, now decides.
2. The agent proposed the same launch for an hour, and every proposal was
   downloaded and trialled again. A target now remembers its verdicts.
3. ``verify_all`` stopped at the first candidate that handshaked -- one the
   registration gate then refused -- and never launched the sixteen behind it.
4. The official registry matches server *names*, and every query sent to it
   was a phrase no server is called.
5. A registry entry that declares ``CONFLUENCE_HOST`` was launched with it
   empty, so even a server that started could not be registered.

The fake servers below are real processes speaking real JSON-RPC over stdio;
nothing about the handshake is mocked.
"""

from __future__ import annotations

import json
import logging
import sys

import pytest

from initialize_claude_ai.core.http import Response
from initialize_claude_ai.mcp_auto_discovery.agentic import _correction
from initialize_claude_ai.mcp_auto_discovery.registry_client import PackageHit, RegistryClient
from initialize_claude_ai.mcp_auto_discovery.traditional import TraditionalPipeline
from initialize_claude_ai.mcp_auto_discovery.types import (
    Candidate,
    Handshake,
    Stage,
    StageOutcome,
    TargetOutcome,
    Transport,
)
from initialize_claude_ai.mcp_auto_discovery.verifier import (
    REUSED_VERDICT,
    CandidateVerifier,
)
from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter

TARGET = "https://confluence.example.com"

#: MODE decides how the server treats TOKEN:
#:   gated  -- starts either way, lists its tools only when TOKEN is set
#:   crash  -- exits without TOKEN, saying nothing about it
#:   never  -- lists nothing whatever TOKEN holds
#:   echo   -- like gated, and repeats TOKEN's value back in a tool description
SERVER = """
import json, os, sys
mode, token = os.environ.get("MODE", ""), os.environ.get("TOKEN", "")
if mode == "crash" and not token:
    sys.stderr.write("Error: cannot initialise the client\\n"); raise SystemExit(1)
listed = bool(token) and mode in ("gated", "crash", "echo")
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    rid, method = msg.get("id"), msg.get("method")
    if rid is None:
        continue
    if method == "initialize":
        result = {"protocolVersion": msg["params"]["protocolVersion"],
                  "capabilities": {"tools": {}},
                  "serverInfo": {"name": "fake-confluence", "version": "1.0"}}
    elif method == "tools/list":
        description = "reads pages" + (" as " + token if mode == "echo" else "")
        tools = [{"name": "confluence_search", "description": description,
                  "inputSchema": {"type": "object", "properties": {}}}]
        result = {"tools": tools if listed else []}
    else:
        result = {}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}) + "\\n")
    sys.stdout.flush()
"""


@pytest.fixture
def live(ctx):
    """A context that may launch processes, with no credential available."""
    ctx.dry_run = False
    ctx.runner.which = lambda name, **_: name  # type: ignore[assignment]
    ctx.secrets.resolve = lambda *a, **k: None  # type: ignore[assignment]
    ctx.settings.set("mcp.packages.trial_timeout_seconds", 20)
    ctx.settings.set("mcp.packages.credential_probe.timeout_seconds", 20)
    return ctx


def fake(tmp_path, mode: str) -> Candidate:
    script = tmp_path / "server.py"
    script.write_text(SERVER)
    candidate = Candidate(
        kind="stdio",
        name="confluence-example-com",
        transport=Transport.STDIO,
        command=[sys.executable, str(script)],
        env={"MODE": mode, "CONFLUENCE_URL": TARGET},
    )
    candidate.secret_env = {"TOKEN": "env:TOKEN"}
    return candidate


def placeholder(ctx) -> str:
    return ctx.settings.str_("mcp.packages.credential_probe.placeholder")


# --------------------------------------------------------------------------- #
# 1. the credential test
# --------------------------------------------------------------------------- #


def test_a_server_that_lists_its_tools_only_once_the_credential_is_set_waits_for_it(live, tmp_path) -> None:
    """The `mcp-atlassian` shape: an empty list that the credential explains."""
    candidate = CandidateVerifier(live).verify(fake(tmp_path, "gated"))

    assert candidate.needs_credential, "registered provisionally, as an endpoint waiting"
    assert not candidate.verified, "never counted as working"
    assert candidate.handshake.credential_pending == ["TOKEN"]
    assert "stand-in" in candidate.handshake.credential_test
    assert candidate.handshake.tools == ["confluence_search"], "what it listed is kept"


def test_its_variable_slot_is_kept_in_the_entry(live, tmp_path) -> None:
    """The launch that worked *had* the variable, so it is not optional."""
    candidate = CandidateVerifier(live).verify(fake(tmp_path, "gated"))

    assert candidate.proven_without == set()
    env = McpWriter(live).entry_for(candidate, "confluence-example-com")["env"]
    assert env["TOKEN"].startswith("${") and ":-" not in env["TOKEN"]


def test_a_server_that_crashes_without_naming_its_credential_waits_for_it(live, tmp_path) -> None:
    """The `@atlassian-dc-mcp/confluence` shape: silence about the reason."""
    candidate = CandidateVerifier(live).verify(fake(tmp_path, "crash"))

    assert candidate.needs_credential
    assert not candidate.verified
    assert "did not start" in candidate.handshake.credential_test


def test_a_variable_that_changes_nothing_is_proved_to_change_nothing(live, tmp_path) -> None:
    """The `PERSONAL_TOKEN` mistake, ruled out rather than merely avoided."""
    candidate = CandidateVerifier(live).verify(fake(tmp_path, "never"))

    assert not candidate.needs_credential
    assert not candidate.verified
    hints = " ".join(candidate.handshake.protocol_hints)
    assert "changed nothing" in hints
    assert "not what it is missing" in candidate.handshake.credential_test


def test_the_stand_in_never_reaches_the_entry_the_report_or_the_log(live, tmp_path, caplog) -> None:
    """Even from a server that repeats it back."""
    value = placeholder(live)
    caplog.set_level(logging.DEBUG)
    candidate = CandidateVerifier(live).verify(fake(tmp_path, "echo"))
    assert candidate.needs_credential

    entry = McpWriter(live).entry_for(candidate, "confluence-example-com")
    report = json.dumps(candidate.to_dict(), default=str)
    audit = json.dumps([c.to_dict() for c in CandidateVerifier.audit(candidate)])
    carried = json.dumps({k: v for k, v in vars(candidate.handshake).items()}, default=str)
    for where, text in {
        "entry": json.dumps(entry),
        "report": report,
        "audit": audit,
        "handshake": carried,
        "notes": " ".join(candidate.notes),
        "log": caplog.text,
    }.items():
        assert value not in text, f"the stand-in leaked into the {where}"
    assert value not in candidate.env.values()


def test_the_report_shows_what_it_listed_and_does_not_claim_it_is_usable(live, tmp_path) -> None:
    candidate = CandidateVerifier(live).verify(fake(tmp_path, "gated"))
    rows = {check.name: check for check in CandidateVerifier.audit(candidate)}

    assert rows["connected"].ok is True and "stand-in" in rows["connected"].detail
    assert rows["tools"].ok is True
    assert rows["usable"].ok is None


def test_the_credential_test_can_be_switched_off(live, tmp_path) -> None:
    live.settings.set("mcp.packages.credential_probe.enabled", False)
    candidate = CandidateVerifier(live).verify(fake(tmp_path, "gated"))

    assert not candidate.needs_credential, "the older verdict: an empty list is a failure"
    assert not candidate.handshake.credential_test


def test_a_trial_that_timed_out_is_not_asked_again(live) -> None:
    """A slow download says nothing about the credential; don't pay twice."""
    verifier = CandidateVerifier(live)
    calls = []

    def probe(*_a, **_k):
        calls.append(1)
        return Handshake(transport=Transport.STDIO, error="no response to initialize")

    verifier.prober.probe_stdio = probe  # type: ignore[assignment]
    candidate = Candidate(kind="stdio", name="slow", command=["slow-server"])
    candidate.secret_env = {"TOKEN": "env:TOKEN"}
    verifier.verify(candidate)

    assert len(calls) == 1
    assert not candidate.needs_credential


# --------------------------------------------------------------------------- #
# 2. a target remembers its verdicts
# --------------------------------------------------------------------------- #


def counting(verifier: CandidateVerifier, shake: Handshake) -> list[str]:
    launched: list[str] = []

    def verify(candidate: Candidate) -> Candidate:
        launched.append(candidate.label)
        candidate.handshake = Handshake(**{**vars(shake)})
        return candidate

    verifier.verify = verify  # type: ignore[assignment]
    return launched


def stdio(name: str, **env: str) -> Candidate:
    return Candidate(kind="stdio", name=name, command=["uvx", "mcp-atlassian"], env=dict(env))


def test_an_identical_launch_is_answered_from_the_targets_record(ctx) -> None:
    verifier = CandidateVerifier(ctx)
    launched = counting(verifier, Handshake(transport=Transport.STDIO, error="process exited with rc=1: boom"))
    memo: dict[str, Handshake] = {}

    verifier.verify_all([stdio("first", URL=TARGET)], memo=memo)
    again = stdio("second", URL=TARGET)
    verifier.verify_all([again], memo=memo)

    assert launched == ["first"], "the same launch is not started twice"
    assert any(note.startswith(REUSED_VERDICT) for note in again.notes)
    assert again.handshake and "boom" in again.handshake.error


def test_a_launch_that_differs_in_anything_is_trialled(ctx) -> None:
    verifier = CandidateVerifier(ctx)
    launched = counting(verifier, Handshake(transport=Transport.STDIO, error="rc=1: boom"))
    memo: dict[str, Handshake] = {}

    verifier.verify_all([stdio("first", URL=TARGET)], memo=memo)
    verifier.verify_all([stdio("second", URL=TARGET, EXTRA="1")], memo=memo)

    assert launched == ["first", "second"]


def test_a_timeout_is_never_reused(ctx) -> None:
    verifier = CandidateVerifier(ctx)
    launched = counting(verifier, Handshake(transport=Transport.STDIO, error="no response to initialize"))
    memo: dict[str, Handshake] = {}

    verifier.verify_all([stdio("first")], memo=memo)
    verifier.verify_all([stdio("second")], memo=memo)

    assert launched == ["first", "second"]


def test_without_a_record_everything_is_trialled(ctx) -> None:
    """What recheck() and any caller that passes no memo get."""
    verifier = CandidateVerifier(ctx)
    launched = counting(verifier, Handshake(transport=Transport.STDIO, error="rc=1: boom"))

    verifier.verify_all([stdio("first")])
    verifier.verify_all([stdio("second")])

    assert launched == ["first", "second"]


def test_reuse_can_be_switched_off(ctx) -> None:
    ctx.settings.set("mcp.verification.reuse_verdicts.enabled", False)
    verifier = CandidateVerifier(ctx)
    launched = counting(verifier, Handshake(transport=Transport.STDIO, error="rc=1: boom"))
    memo: dict[str, Handshake] = {}

    verifier.verify_all([stdio("first")], memo=memo)
    verifier.verify_all([stdio("second")], memo=memo)

    assert launched == ["first", "second"]


def test_the_agent_is_told_what_the_credential_test_showed_and_what_was_reused() -> None:
    shake = Handshake(
        transport=Transport.STDIO,
        error="server exposes no tools, resources or prompts",
        credential_test="a second trial with TOKEN set to a stand-in value changed nothing",
    )
    candidate = Candidate(kind="stdio", name="mcp-atlassian", handshake=shake)
    candidate.add_note(f"{REUSED_VERDICT}, so it was not started again: x")
    stage = StageOutcome(stage=Stage.PACKAGE, candidates=[candidate], detail="none verified")

    brief = _correction(stage, 1)

    assert "changed nothing" in brief
    assert "not started again" in brief


# --------------------------------------------------------------------------- #
# 3. "the first" means the first that can be written
# --------------------------------------------------------------------------- #


def test_the_search_goes_past_a_server_the_registration_gate_would_refuse(ctx) -> None:
    verifier = CandidateVerifier(ctx)
    ok = Handshake(ok=True, transport=Transport.STDIO, tools=["t"], round_trip=True)
    launched = counting(verifier, ok)
    unaddressed, addressed = stdio("confluence-mcp-server"), stdio("good", URL=TARGET)

    verified = verifier.verify_all([unaddressed, addressed], keep=lambda c: bool(c.env.get("URL")))

    assert launched == ["confluence-mcp-server", "good"]
    assert verified == [unaddressed, addressed], "both reported; the gate refuses one later"


def test_it_still_stops_at_the_first_that_can_be_written(ctx) -> None:
    verifier = CandidateVerifier(ctx)
    launched = counting(verifier, Handshake(ok=True, transport=Transport.STDIO, tools=["t"], round_trip=True))

    verifier.verify_all([stdio("a", URL=TARGET), stdio("b", URL=TARGET)], keep=lambda c: True)

    assert launched == ["a"]


def test_a_credential_wall_the_writer_would_refuse_does_not_settle_the_stage(ctx) -> None:
    """It would close the lower tiers and then be refused, leaving nothing."""
    pipeline = TraditionalPipeline(ctx)
    walled = Candidate(kind="stdio", name="nowhere", command=["npx", "-y", "some-mcp"])
    walled.handshake = Handshake(transport=Transport.STDIO, credential_pending=["TOKEN"])
    stage = StageOutcome(stage=Stage.PACKAGE, candidates=[walled])
    outcome = TargetOutcome(target=TARGET)

    pipeline.discard_unregistrable(stage, outcome)

    assert walled not in stage.candidates
    assert stage.findings["not_registrable"]


# --------------------------------------------------------------------------- #
# 4. the official registry is asked what it can answer
# --------------------------------------------------------------------------- #


class FakeIndex:
    def __init__(self, answers: dict[str, Response] | None = None, default: Response | None = None):
        self.answers = answers or {}
        self.default = default
        self.asked: list[str] = []

    def __enter__(self) -> "FakeIndex":
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False

    def get(self, url: str, **_kwargs: object) -> Response:
        self.asked.append(url)
        if url in self.answers:
            return self.answers[url]
        return self.default or Response(url=url, status=404)


def registry(ctx, monkeypatch, index: FakeIndex) -> RegistryClient:
    monkeypatch.setattr(type(ctx), "http", lambda _self, **_k: index)
    return RegistryClient(ctx)


def test_the_registry_is_asked_for_the_product_name(ctx, monkeypatch) -> None:
    index = FakeIndex()
    client = registry(ctx, monkeypatch, index)

    client.search_all(TARGET)

    base = ctx.settings.str_("mcp.packages.registry_url")
    asked = [u for u in index.asked if u.startswith(base)]
    assert asked and "search=confluence&" in asked[0], "the bare product name, first"
    assert not any("mcp%20server" in u for u in asked), "never a phrase no server is called"
    npm = [u for u in index.asked if "npmjs" in u]
    assert any("confluence%20mcp%20server" in u for u in npm), "full-text sources keep theirs"


def test_a_source_that_did_not_answer_is_reported(ctx, monkeypatch) -> None:
    index = FakeIndex(default=Response(url="", status=0, error="timed out"))
    problems: list[str] = []

    registry(ctx, monkeypatch, index).search_all(TARGET, problems)

    assert problems and "timed out" in problems[0]


def test_a_missing_package_is_an_answer_not_a_problem(ctx, monkeypatch) -> None:
    problems: list[str] = []
    registry(ctx, monkeypatch, FakeIndex()).search_all(TARGET, problems)
    assert problems == []


# --------------------------------------------------------------------------- #
# 5. the target's address goes where the entry declares it
# --------------------------------------------------------------------------- #


def entry(*variables: dict) -> PackageHit:
    return PackageHit(
        name="io.github/atlassian-dc-mcp-confluence",
        source="mcp-registry",
        raw={
            "name": "io.github/atlassian-dc-mcp-confluence",
            "packages": [
                {
                    "registryType": "npm",
                    "identifier": "@atlassian-dc-mcp/confluence",
                    "environmentVariables": list(variables),
                }
            ],
        },
    )


HOST = {"name": "CONFLUENCE_HOST", "description": "host domain (e.g. your-instance.atlassian.net)"}
BASE_PATH = {"name": "CONFLUENCE_API_BASE_PATH", "isRequired": True}
SECRET = {"name": "CONFLUENCE_API_TOKEN", "isSecret": True, "isRequired": True}


def test_a_product_named_host_variable_gets_the_targets_host(ctx, monkeypatch) -> None:
    [candidate] = registry(ctx, monkeypatch, FakeIndex()).to_candidates(entry(HOST, BASE_PATH, SECRET), TARGET)

    assert candidate.env["CONFLUENCE_HOST"] == "confluence.example.com"
    assert candidate.env.get("CONFLUENCE_API_BASE_PATH", "") == "", "not an address"
    assert "CONFLUENCE_API_TOKEN" not in candidate.env, "a secret is never filled"
    assert candidate.secret_env["CONFLUENCE_API_TOKEN"] == "env:CONFLUENCE_API_TOKEN"


def test_a_url_variable_gets_the_base_url(ctx, monkeypatch) -> None:
    [candidate] = registry(ctx, monkeypatch, FakeIndex()).to_candidates(
        entry({"name": "CONFLUENCE_URL"}, SECRET), TARGET
    )
    assert candidate.env["CONFLUENCE_URL"].startswith(TARGET)


def test_another_kind_of_address_is_left_alone(ctx, monkeypatch) -> None:
    [candidate] = registry(ctx, monkeypatch, FakeIndex()).to_candidates(
        entry({"name": "HTTPS_PROXY_URL"}, {"name": "OAUTH_CALLBACK_URL"}), TARGET
    )
    assert "HTTPS_PROXY_URL" not in candidate.env
    assert "OAUTH_CALLBACK_URL" not in candidate.env


def test_an_address_about_something_else_is_left_for_the_operator(ctx, monkeypatch) -> None:
    """Two address-shaped variables and only one names the product."""
    [candidate] = registry(ctx, monkeypatch, FakeIndex()).to_candidates(entry(HOST, {"name": "MIRROR_URL"}), TARGET)
    assert candidate.env["CONFLUENCE_HOST"] == "confluence.example.com"
    assert "MIRROR_URL" not in candidate.env


def test_the_only_address_variable_is_filled_whatever_it_is_called(ctx, monkeypatch) -> None:
    [candidate] = registry(ctx, monkeypatch, FakeIndex()).to_candidates(entry({"name": "SERVICE_BASE_URL"}), TARGET)
    assert candidate.env["SERVICE_BASE_URL"].startswith(TARGET)


def test_filling_can_be_switched_off(ctx, monkeypatch) -> None:
    ctx.settings.set("mcp.packages.address_fill.enabled", False)
    [candidate] = registry(ctx, monkeypatch, FakeIndex()).to_candidates(entry(HOST), TARGET)
    assert "CONFLUENCE_HOST" not in candidate.env


def test_without_a_target_nothing_is_filled(ctx, monkeypatch) -> None:
    [candidate] = registry(ctx, monkeypatch, FakeIndex()).to_candidates(entry(HOST))
    assert "CONFLUENCE_HOST" not in candidate.env


# --------------------------------------------------------------------------- #
# 6. a fill is not evidence
# --------------------------------------------------------------------------- #


def test_a_generic_sole_fill_does_not_ground_the_entry(ctx, monkeypatch) -> None:
    """The program put the address there, so it cannot be why the package is the target's."""
    from initialize_claude_ai.mcp_auto_discovery.grounding import assess

    [candidate] = registry(ctx, monkeypatch, FakeIndex()).to_candidates(entry({"name": "SERVICE_BASE_URL"}), TARGET)

    assert candidate.env["SERVICE_BASE_URL"].startswith(TARGET), "still set, so it can start"
    assert candidate.unvouched_env == {"SERVICE_BASE_URL"}
    verdict = assess(candidate, TARGET)
    assert not verdict.registrable
    assert "SERVICE_BASE_URL" in verdict.reason


def test_a_product_named_fill_still_grounds_the_entry(ctx, monkeypatch) -> None:
    from initialize_claude_ai.mcp_auto_discovery.grounding import assess

    [candidate] = registry(ctx, monkeypatch, FakeIndex()).to_candidates(entry(HOST), TARGET)

    assert candidate.unvouched_env == set()
    assert assess(candidate, TARGET).registrable


def test_the_server_naming_the_target_still_grounds_an_unvouched_fill(ctx, monkeypatch) -> None:
    from initialize_claude_ai.mcp_auto_discovery.grounding import assess

    [candidate] = registry(ctx, monkeypatch, FakeIndex()).to_candidates(entry({"name": "SERVICE_BASE_URL"}), TARGET)
    candidate.handshake = Handshake(
        ok=True,
        tools=["page"],
        round_trip=True,
        advertised=["resource https://confluence.example.com/display/SPACE"],
    )
    assert assess(candidate, TARGET).registrable


def test_a_sole_fill_can_be_made_to_ground_again(ctx, monkeypatch) -> None:
    from initialize_claude_ai.mcp_auto_discovery.grounding import assess

    ctx.settings.set("mcp.packages.address_fill.sole_fill_grounds", True)
    [candidate] = registry(ctx, monkeypatch, FakeIndex()).to_candidates(entry({"name": "SERVICE_BASE_URL"}), TARGET)
    assert candidate.unvouched_env == set()
    assert assess(candidate, TARGET).registrable


def test_the_agent_is_told_an_endpoint_was_ruled_out() -> None:
    stage = StageOutcome(stage=Stage.ENDPOINT, detail="nothing answered")
    text = _correction(stage, 1, {"https://mcp.0dteconfluence.com/mcp": "this entry names no address of it"})
    assert "https://mcp.0dteconfluence.com/mcp was ruled out" in text
    assert "Do not propose it again" in text
    assert "ruled out" not in _correction(stage, 1)
