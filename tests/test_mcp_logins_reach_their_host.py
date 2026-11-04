"""Three wrong choices from one run on a machine holding no credentials.

* build-example-net's SSH-signing bridge named ``/root/.ssh/id_ed25519`` in its key
  slot; the run machine had no key, so the only bridge that could log in was
  refused as naming "a file that is not there", and a password bridge the API
  host does not accept was written instead. A key file is a credential.
* build-opensuse-org's agent bridge sent Basic to build.opensuse.org (refused);
  the osc client bridge's manifest sends it to api.opensuse.org (accepted). The
  plan compared the login's type only, so both read "asked at the API host".
  The type and the host it is sent to now count together, and a waiting server
  aimed at the wrong host is tried on the API host.
* smelt-example-net's NODE_OPTIONS was dropped after one passing launch; Claude
  Code's check 20 s later failed every call with "fetch failed". A drop now
  needs several passing launches, and a network-type failure after it brings
  the setting back for that machine only.
"""

from __future__ import annotations

import contextlib
import json
import sys
from pathlib import Path

from initialize_claude_ai.mcp_auto_discovery import claude_check, evidence
from initialize_claude_ai.mcp_auto_discovery.types import Candidate, Handshake
from initialize_claude_ai.mcp_auto_discovery.verifier import NETWORK_FAILURE_PATTERNS, CandidateVerifier
from initialize_claude_ai.mcp_auto_discovery.writer import CREDENTIAL_FILE_SLOTS, McpWriter, Registration

# -- a key file is a credential -------------------------------------------------------------------


def _stdio(tmp_path: Path, env: dict) -> Candidate:
    script = tmp_path / "bridge.py"
    script.write_text("print('x')\n")
    return Candidate(kind="stdio", name="build-example-test", command=[sys.executable, str(script)], env=dict(env))


def test_an_absent_key_file_is_a_credential_to_supply_not_a_lost_file(ctx, tmp_path) -> None:
    keyed = _stdio(tmp_path, {"OBS_SSH_KEY": str(tmp_path / "absent" / "id_ed25519")})
    assert McpWriter(ctx).portability_of(keyed).registrable
    data = _stdio(tmp_path, {"BRIDGE_MANIFEST": str(tmp_path / "absent" / "operations.json")})
    assert not McpWriter(ctx).portability_of(data).registrable, "a data file the entry lost is still refused"


def test_switching_it_off_refuses_the_absent_key_again(ctx, tmp_path) -> None:
    ctx.settings.set("mcp.registration.credential_files_may_be_absent", False)
    keyed = _stdio(tmp_path, {"OBS_SSH_KEY": str(tmp_path / "absent" / "id_ed25519")})
    assert not McpWriter(ctx).portability_of(keyed).registrable


# -- the login's type and its host together -------------------------------------------------------


PLAN = {
    "offered": [{"scheme": "basic", "host": "build.example.test"}, {"scheme": "Basic", "host": "api.example.test"}],
    "chosen": {"method": "basic"},
}


def _waiting(name: str, env: dict) -> Candidate:
    candidate = Candidate(
        kind="stdio",
        name=name,
        command=["python3", f"{name}.py"],
        env=dict(env),
        secret_env={"BUILD_EXAMPLE_PASSWORD": "env:BUILD_EXAMPLE_PASSWORD"},
    )
    candidate.handshake = Handshake(ok=True, tools=["t"], credential_pending=["BUILD_EXAMPLE_PASSWORD"])
    return candidate


def test_a_login_ranks_first_only_when_sent_to_the_host_that_asked(ctx, tmp_path) -> None:
    ctx.settings.set("mcp.discovery.auth.api_host_patterns", ["api.{domain}"])
    manifest = tmp_path / "client.json"
    manifest.write_text(json.dumps({"config": "[general]\napiurl = https://api.example.test\n"}))
    client = _waiting(
        "client-bridge",
        {"MCP_CLIENT_MANIFEST": str(manifest), "BUILD_EXAMPLE_USERNAME": "", "BUILD_EXAMPLE_PASSWORD": ""},
    )
    agent = _waiting(
        "agent-bridge",
        {
            "MCP_BRIDGE_BASE_URL": "https://build.example.test",
            "BUILD_EXAMPLE_USERNAME": "",
            "BUILD_EXAMPLE_PASSWORD": "",
        },
    )
    assert evidence.login_hosts(client, ctx.settings) == {"api.example.test"}, "read from the file it names"
    assert evidence.login_hosts(agent, ctx.settings) == {"build.example.test"}
    assert evidence.key(ctx, client, "build.example.test", PLAN)[1] == 0
    assert evidence.key(ctx, agent, "build.example.test", PLAN)[1] == 1
    ctx.settings.set("mcp.discovery.auth.evidence.host_matters", False)
    assert evidence.key(ctx, agent, "build.example.test", PLAN)[1] == 0, "the switch restores type-only"


def test_a_waiting_server_aimed_at_the_wrong_host_moves_to_the_api_host(ctx, monkeypatch) -> None:
    verifier = CandidateVerifier(ctx)
    ctx.state["mcp_api_hosts"] = {"build.example.test": ["api.example.test"]}
    agent = _waiting("agent-bridge", {"MCP_BRIDGE_BASE_URL": "https://build.example.test/", "OTHER": "x"})
    shake = agent.handshake
    seen: list[str] = []

    def trial(candidate, inner):
        seen.append(candidate.env["MCP_BRIDGE_BASE_URL"])
        inner["launch"] = object()
        return Handshake(ok=True, tools=["t"], credential_pending=["BUILD_EXAMPLE_PASSWORD"])

    monkeypatch.setattr(verifier, "_trial_stdio", trial)
    monkeypatch.setattr(verifier, "check_in_use", lambda *a, **k: None)
    assert verifier._sends_login_off_api_host(agent)
    again = verifier._prove_address_forms(agent, shake, {}, waiting=True)
    assert seen[0] == "https://api.example.test/", "the value as written, on the API host"
    assert again.address_forms == {"MCP_BRIDGE_BASE_URL": "https://api.example.test/"}
    assert agent.env["OTHER"] == "x"


def test_it_stays_when_the_api_host_does_less_of_the_job(ctx, monkeypatch) -> None:
    verifier = CandidateVerifier(ctx)
    ctx.state["mcp_api_hosts"] = {"build.example.test": ["api.example.test"]}
    agent = _waiting("agent-bridge", {"MCP_BRIDGE_BASE_URL": "https://build.example.test"})
    agent.handshake.functional = [{"tool": "list_public", "verdict": "accepted"}]

    def trial(candidate, inner):
        inner["launch"] = object()
        return Handshake(ok=True, tools=["t"], credential_pending=["BUILD_EXAMPLE_PASSWORD"])

    monkeypatch.setattr(verifier, "_trial_stdio", trial)
    monkeypatch.setattr(verifier, "check_in_use", lambda *a, **k: None)
    again = verifier._prove_address_forms(agent, agent.handshake, {}, waiting=True)
    assert not again.address_forms and agent.env["MCP_BRIDGE_BASE_URL"] == "https://build.example.test"


def test_a_server_already_sending_to_the_api_host_is_not_moved(ctx, tmp_path) -> None:
    ctx.state["mcp_api_hosts"] = {"build.example.test": ["api.example.test"]}
    client = _waiting("client", {"MCP_BRIDGE_BASE_URL": "https://api.example.test"})
    assert not CandidateVerifier(ctx)._sends_login_off_api_host(client)


def test_it_moves_only_to_an_api_host_asking_for_its_login(ctx) -> None:
    """build-example-net's password bridge was moved to api.example.net, which asks for Signature only."""
    ctx.state["mcp_api_hosts"] = {"build.example.test": ["api.example.test"]}
    ctx.state["mcp_api_logins"] = {"build.example.test": {"api.example.test": ["signature"]}}
    basic = _waiting(
        "password-bridge",
        {
            "MCP_BRIDGE_BASE_URL": "https://build.example.test",
            "BUILD_EXAMPLE_USERNAME": "",
            "BUILD_EXAMPLE_PASSWORD": "",
        },
    )
    assert evidence.scheme_of(basic, ctx.settings) == "basic"
    assert not CandidateVerifier(ctx)._sends_login_off_api_host(basic)
    ctx.state["mcp_api_logins"] = {"build.example.test": {"api.example.test": ["basic"]}}
    assert CandidateVerifier(ctx)._sends_login_off_api_host(basic)


def test_a_client_bridge_is_built_for_the_api_like_a_kind_bridge(ctx, tmp_path) -> None:
    """The OBS-kind password bridge outranked the osc bridge signing where the API asks, before logins were compared."""
    from initialize_claude_ai.mcp_auto_discovery import types

    plan = {
        "offered": [
            {"scheme": "basic", "host": "build.example.test"},
            {"scheme": "Signature", "host": "api.example.test"},
        ],
        "chosen": {"method": "client"},
    }
    ctx.settings.set("mcp.discovery.auth.api_host_patterns", ["api.{domain}"])
    manifest = tmp_path / "signature.client.json"
    manifest.write_text(json.dumps({"config": "[general]\napiurl = https://api.example.test\n"}))
    kind = _waiting(
        "password-bridge",
        {
            "MCP_BRIDGE_BASE_URL": "https://build.example.test",
            "BUILD_EXAMPLE_USERNAME": "",
            "BUILD_EXAMPLE_PASSWORD": "",
        },
    )
    kind.api_kind = "open_build_service"
    client = Candidate(
        kind="stdio",
        name="client-bridge-signature",
        command=["python3", "c.py"],
        env={"MCP_CLIENT_MANIFEST": str(manifest), "BUILD_EXAMPLE_SSH_KEY_FILE": "", "BUILD_EXAMPLE_USERNAME": ""},
        secret_env={"BUILD_EXAMPLE_USERNAME": "env:BUILD_EXAMPLE_USERNAME"},
    )
    client.handshake = Handshake(ok=True, tools=["t"], credential_pending=["BUILD_EXAMPLE_USERNAME"])
    for c in (kind, client):
        c.evidence = evidence.key(ctx, c, "build.example.test", plan)
    assert types.built_for_api(client) and types.evidence_beats(client, kind)


# -- a runtime setting: dropped on repeated proof, restored on a network failure ----------------------


def test_one_failing_sample_keeps_the_setting(ctx, monkeypatch) -> None:
    verifier = CandidateVerifier(ctx)
    candidate = Candidate(
        kind="stdio",
        name="smelt-example-test",
        command=["npx", "-y", "mcp-graphql@2.0.4"],
        env={"NODE_OPTIONS": "--network-family-autoselection-attempt-timeout=30000"},
    )
    good = Handshake(
        ok=True, tools=["q"], start_seconds=1.0, contract=[{"task": "describe the API", "tool": "q", "passed": True}]
    )
    bad = Handshake(
        ok=True, tools=["q"], start_seconds=1.0, contract=[{"task": "describe the API", "tool": "q", "passed": False}]
    )
    candidate.handshake = good
    answers = [good, bad, good]
    monkeypatch.setattr(verifier, "_verify_stdio", lambda c: answers.pop(0))
    assert verifier._without_runtime_tuning(candidate) == [] and "NODE_OPTIONS" in candidate.env
    answers[:] = [good, good, good]
    assert verifier._without_runtime_tuning(candidate) == ["NODE_OPTIONS"]
    assert candidate.dropped_tuning == {"NODE_OPTIONS": "--network-family-autoselection-attempt-timeout=30000"}


def test_a_network_failure_after_the_drop_restores_it_for_this_machine(ctx, monkeypatch) -> None:
    ctx.dry_run = False
    writer = McpWriter(ctx)
    candidate = Candidate(
        kind="stdio",
        name="smelt-example-test",
        command=["npx", "-y", "mcp-graphql@2.0.4"],
        env={"ENDPOINT": "https://smelt.example.test/graphql"},
    )
    candidate.dropped_tuning = {"NODE_OPTIONS": "--network-family-autoselection-attempt-timeout=30000"}
    candidate.handshake = Handshake(ok=True, tools=["q"])
    entry = {
        "command": "npx",
        "args": ["-y", "mcp-graphql@2.0.4"],
        "env": {"ENDPOINT": "https://smelt.example.test/graphql"},
    }
    registration = Registration(name="smelt-example-test", candidate=candidate, entry=dict(entry))
    monkeypatch.setattr(claude_check, "enabled", lambda s: True)
    monkeypatch.setattr(
        claude_check, "connect_check", lambda *a, **k: claude_check.Outcome(claude_check.PASSED, "connected")
    )
    asked: list[dict] = []

    def check(ctx_, name, entry_, candidate_):
        asked.append(dict(entry_.get("env") or {}))
        if "NODE_OPTIONS" in entry_.get("env", {}):
            return claude_check.Outcome(claude_check.PASSED, "answered", "q")
        return claude_check.Outcome(claude_check.FAILED, "Failed to introspect schema: TypeError: fetch failed", "q")

    monkeypatch.setattr(claude_check, "check", check)
    monkeypatch.setattr(writer, "_write_json", lambda regs: None)
    monkeypatch.setattr(writer, "_keep_held_credentials", lambda reg: None)
    writer._check_through_claude(registration)
    assert registration.claude_usage["outcome"] == claude_check.PASSED
    assert registration.entry["env"]["NODE_OPTIONS"] == "${ICA_MCP_SMELT_EXAMPLE_TEST_NODE_OPTIONS:-}"
    assert "network-family" not in json.dumps(registration.entry), "never a literal in the file that travels"
    assert candidate.machine_values["ICA_MCP_SMELT_EXAMPLE_TEST_NODE_OPTIONS"].startswith("--network-family")


def test_another_failure_does_not_bring_it_back(ctx, monkeypatch) -> None:
    ctx.dry_run = False
    writer = McpWriter(ctx)
    candidate = Candidate(kind="stdio", name="x", command=["npx", "x"], env={})
    candidate.dropped_tuning = {"NODE_OPTIONS": "--x"}
    registration = Registration(name="x", candidate=candidate, entry={"command": "npx", "env": {}})
    assert not writer._restore_runtime_tuning(registration, "401 Unauthorized")
    assert "NODE_OPTIONS" not in registration.entry["env"]


def test_packaged_settings_equal_the_code(settings) -> None:
    ev = settings.section("mcp.discovery.auth.evidence")
    assert ev.bool_("host_matters") is True
    assert list(ev.str_list("url_setting_keys", [])) == list(evidence.URL_SETTING_KEYS)
    assert ev.str_("url_assignment_pattern") == evidence.URL_ASSIGNMENT_PATTERN
    assert settings.bool_("mcp.verification.address_forms.try_api_hosts_when_waiting") is True
    reg = settings.section("mcp.registration")
    assert reg.bool_("credential_files_may_be_absent") is True
    assert list(reg.str_list("credential_file_slots", [])) == list(CREDENTIAL_FILE_SLOTS)
    tuning = settings.section("mcp.verification.runtime_tuning")
    from initialize_claude_ai.mcp_auto_discovery import types

    assert ev.bool_("client_bridge_is_built_for_api") is types.CLIENT_BRIDGE_COUNTS
    assert tuple(ev.str_list("client_bridge_markers", [])) == types.CLIENT_BRIDGE_MARKERS
    assert tuning.int_("drop_samples") == 3 and tuning.bool_("restore") is True
    assert list(tuning.str_list("restore_on", [])) == list(NETWORK_FAILURE_PATTERNS)


def test_evidence_is_marked_even_when_another_server_in_the_stage_answered(ctx, tmp_path) -> None:
    """build-example-net: an unrelated search hit "verified" in the stage, so the waiting bridges went unmarked
    and confidence plus start time chose between the osc signing bridge and the Basic one."""
    from initialize_claude_ai.mcp_auto_discovery.traditional import TraditionalPipeline
    from initialize_claude_ai.mcp_auto_discovery.types import Stage, StageOutcome, TargetOutcome

    ctx.settings.set("mcp.discovery.auth.api_host_patterns", ["api.{domain}"])
    signed, basic = tmp_path / "signature.client.json", tmp_path / "basic.client.json"
    signed.write_text(json.dumps({"config": "[general]\napiurl = https://api.example.test\n"}))
    basic.write_text(json.dumps({"config": "[general]\napiurl = https://build.example.test\n"}))

    def bridge(name, manifest, slots, confidence, start):
        c = Candidate(
            kind="stdio",
            name=name,
            command=["python3", f"{name}.py"],
            env={"MCP_CLIENT_MANIFEST": str(manifest), **{s: "" for s in slots}},
            secret_env={s: f"env:{s}" for s in slots if "KEY_FILE" not in s},
            confidence=confidence,
        )
        c.handshake = Handshake(
            ok=True, tools=["t"], start_seconds=start, credential_pending=[s for s in slots if "KEY_FILE" not in s]
        )
        return c

    signing = bridge(
        "client-bridge-signature", signed, ["BUILD_EXAMPLE_SSH_KEY_FILE", "BUILD_EXAMPLE_USERNAME"], 0.5, 3.0
    )
    password = bridge("client-bridge-basic", basic, ["BUILD_EXAMPLE_USERNAME", "BUILD_EXAMPLE_PASSWORD"], 0.9, 0.1)
    elsewhere = Candidate(kind="url", name="search-hit", url="https://elsewhere.example.org/mcp")
    elsewhere.handshake = Handshake(ok=True, tools=["t"])
    stage = StageOutcome(stage=Stage.BRIDGE, candidates=[password, signing, elsewhere], verified=[elsewhere])
    outcome = TargetOutcome(
        target="https://build.example.test",
        auth_plan={
            "offered": [
                {"scheme": "basic", "host": "build.example.test"},
                {"scheme": "Signature", "host": "api.example.test"},
            ],
            "chosen": {"method": "client"},
        },
    )
    TraditionalPipeline(ctx).absorb(outcome, stage, exhaustive=False)
    assert signing.evidence and signing.evidence[1] == 0 and password.evidence[1] == 1
    assert outcome.provisional[:1] == [signing]


def test_a_login_slot_its_manifest_declares_is_a_credential_when_read_back(ctx, tmp_path) -> None:
    """osc signing needs only the user name; read back, it launched with nothing and was judged "not accepted"."""
    from initialize_claude_ai.mcp_auto_discovery.existing import ExistingRegistrations
    from initialize_claude_ai.mcp_auto_discovery.existing import Registration as Existing

    manifest = tmp_path / "build-example.signature.client.json"
    manifest.write_text(
        json.dumps(
            {
                "config": "[general]\napiurl = https://api.example.test\n",
                "parts": {"BUILD_EXAMPLE_USERNAME": "user = {value}", "BUILD_EXAMPLE_SSH_KEY_FILE": "sshkey = {value}"},
                "optional_parts": ["BUILD_EXAMPLE_SSH_KEY_FILE"],
                "key_part": "",
            }
        )
    )
    script = tmp_path / "bridge.py"
    script.write_text("print('x')\n")
    entry = {
        "command": sys.executable,
        "args": [str(script)],
        "env": {
            "MCP_CLIENT_MANIFEST": str(manifest),
            "BUILD_EXAMPLE_USERNAME": "${BUILD_EXAMPLE_USERNAME:-}",
            "BUILD_EXAMPLE_SSH_KEY_FILE": "${BUILD_EXAMPLE_SSH_KEY_FILE:-}",
        },
    }
    existing = ExistingRegistrations(ctx)
    candidate = existing.candidate(Existing("build-example-test", entry, "known_good"), "bridge", record_origin=False)
    assert candidate.secret_env.get("BUILD_EXAMPLE_USERNAME") == "env:BUILD_EXAMPLE_USERNAME"
    assert "BUILD_EXAMPLE_SSH_KEY_FILE" not in candidate.secret_env, "an optional part stays optional"
    ctx.settings.set("mcp.discovery.reuse.manifest_login_slots", False)
    again = ExistingRegistrations(ctx).candidate(
        Existing("build-example-test", entry, "known_good"), "bridge", record_origin=False
    )
    assert "BUILD_EXAMPLE_USERNAME" not in again.secret_env


def test_an_origin_that_timed_out_still_gets_a_plan_from_what_it_asked_before(ctx, monkeypatch) -> None:
    """build.example.net timed out once under --concurrency, and no plan was made at all."""
    from initialize_claude_ai.mcp_auto_discovery import login_methods
    from initialize_claude_ai.mcp_auto_discovery.traditional import TraditionalPipeline
    from initialize_claude_ai.mcp_auto_discovery.types import Stage, StageOutcome, TargetOutcome

    ctx.dry_run = False
    ctx.settings.set("mcp.discovery.auth.read_challenges", True)
    seen = [
        {"host": "build.example.test", "scheme": "basic", "method": "basic", "evidence": "Basic"},
        {"host": "api.example.test", "scheme": "Signature", "method": "client", "evidence": "Signature realm=x"},
    ]
    login_methods.with_remembered(ctx, "build.example.test", list(seen))
    outcome = TargetOutcome(target="https://build.example.test")
    outcome.stages.append(StageOutcome(stage=Stage.ENDPOINT, findings={"origin_status": 0}))
    pipeline = TraditionalPipeline(ctx)
    monkeypatch.setattr(pipeline, "_answers_now", lambda *a: False)
    monkeypatch.setattr(ctx, "http", lambda **k: contextlib.nullcontext(None))  # nothing live is read
    plan = pipeline.auth_plan_for(outcome)
    assert {(o["scheme"].lower(), o["host"]) for o in plan["offered"]} >= {("signature", "api.example.test")}
    assert ctx.state["mcp_api_logins"]["build.example.test"] == {"api.example.test": ["signature"]}
