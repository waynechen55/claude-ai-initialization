"""An entry has to work tomorrow, and on the next machine.

``.mcp.json`` exists to be copied -- that is why the briefing was taken out of
it. Discovery can undo that in one move by writing an absolute path into the
entry, and it did: a bridge was registered as

    …/work/agent-workspace/artifacts/openqa-mcp-bridge/venv/bin/python3

which handshaked perfectly when it was verified and was ``ENOENT`` on the next
host. Verification proved the command worked *at that instant*; nothing asked
whether it would still work afterwards, or anywhere else.
"""

from __future__ import annotations

from initialize_claude_ai.mcp_auto_discovery.portability import (
    DISPOSABLE,
    HOST_BOUND,
    PORTABLE,
    assess,
)
from initialize_claude_ai.mcp_auto_discovery.types import Candidate, Transport
from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter


def stdio(command: list[str], env: dict[str, str] | None = None) -> Candidate:
    return Candidate(kind="stdio", name="x", command=command, env=env or {}, transport=Transport.STDIO)


# -- the three grades -----------------------------------------------------------


def test_a_launcher_is_portable() -> None:
    """Nothing to leave behind: it fetches what it needs when it starts."""
    for command in (
        ["npx", "-y", "mcp-graphql@2.0.4"],
        ["uvx", "openqa-mcp", "--readonly"],
        ["uv", "tool", "run", "openqa-mcp"],
    ):
        assert assess(stdio(command)).kind == PORTABLE, command


def test_a_url_is_portable() -> None:
    assert assess(Candidate(kind="url", url="https://x.test/mcp")).kind == PORTABLE


def test_a_generated_bridge_is_host_bound_but_allowed(tmp_path) -> None:
    """It works here, it is registered, and the briefing says it will not travel."""
    script = tmp_path / "mcp_bridge_x.py"
    script.write_text("")
    verdict = assess(stdio(["/usr/bin/python3", str(script)]))
    assert verdict.kind == HOST_BOUND
    assert verdict.registrable is True
    assert str(script) in verdict.paths


def test_a_path_inside_a_virtualenv_is_refused(tmp_path) -> None:
    """The exact shape that broke openqa-opensuse-org and openqa-example-net."""
    venv = tmp_path / "openqa-mcp-bridge" / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("home = /usr/bin\n")
    python = venv / "bin" / "python3"
    python.write_text("")

    verdict = assess(stdio([str(python), "-m", "openqa_mcp", "--readonly"]))
    assert verdict.kind == DISPOSABLE
    assert verdict.registrable is False
    assert "virtualenv" in verdict.reason


def test_the_trial_sandbox_is_refused(tmp_path) -> None:
    sandbox = tmp_path / "mcp-trials"
    (sandbox / "pkg").mkdir(parents=True)
    binary = sandbox / "pkg" / "server"
    binary.write_text("")
    verdict = assess(stdio([str(binary)]), sandbox_dirs=[str(sandbox)])
    assert verdict.kind == DISPOSABLE
    assert "scratch" in verdict.reason


def test_a_path_that_is_not_there_is_refused() -> None:
    """Verification proved it worked then. This asks whether it works now."""
    verdict = assess(stdio(["/usr/bin/python3", "/nowhere/at/all/bridge.py"]))
    assert verdict.kind == DISPOSABLE
    assert "/nowhere/at/all/bridge.py" in verdict.paths


def test_the_existence_check_can_be_skipped_for_ranking() -> None:
    """Ranking compares shapes; it must not care whether a file is there yet."""
    assert assess(stdio(["/usr/bin/python3", "/nowhere/x.py"]), must_exist=False).kind == HOST_BOUND


def test_an_environment_value_counts_too(tmp_path) -> None:
    """A manifest named in env is as load-bearing as the interpreter."""
    verdict = assess(stdio(["npx", "-y", "thing"], {"MANIFEST": "/nowhere/manifest.json"}))
    assert verdict.kind == DISPOSABLE


# -- what registration does with it ---------------------------------------------


def test_a_disposable_entry_is_not_registered(ctx, tmp_path) -> None:
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("")
    (venv / "bin" / "python3").write_text("")

    writer = McpWriter(ctx)
    verdict = writer.portability_of(stdio([str(venv / "bin" / "python3"), "-m", "srv"]))
    assert verdict.registrable is False


def test_the_briefing_names_what_will_not_travel(ctx, tmp_path) -> None:
    from initialize_claude_ai.mcp_auto_discovery.types import Handshake, TargetOutcome

    script = tmp_path / "bridge.py"
    script.write_text("")
    # A real generated bridge carries the address it relays to in MCP_BRIDGE_BASE_URL
    # (see bridge.BridgeBuilder), which is also what keeps it grounded -- an entry
    # has to name the service it talks to. See mcp_auto_discovery/grounding.py.
    candidate = stdio(
        ["/usr/bin/python3", str(script)],
        {"MCP_BRIDGE_BASE_URL": "https://x.example.test"},
    )
    candidate.handshake = Handshake(ok=True, tools=["a"], round_trip=True)

    writer = McpWriter(ctx)
    registrations = writer.publish(TargetOutcome(target="https://x.example.test", verified=[candidate]))
    briefing = writer.briefing(registrations)

    assert "these_entries_only_work_on_this_machine" in briefing
    assert str(script) in str(briefing["these_entries_only_work_on_this_machine"])
    assert "copied" in briefing["why_that_matters"]


def test_a_portable_entry_raises_no_warning(ctx) -> None:
    from initialize_claude_ai.mcp_auto_discovery.types import Handshake, TargetOutcome

    candidate = stdio(["npx", "-y", "mcp-graphql@2.0.4"], {"ENDPOINT": "https://x.test/graphql/"})
    candidate.handshake = Handshake(ok=True, tools=["a"], round_trip=True)
    writer = McpWriter(ctx)
    briefing = writer.briefing(writer.publish(TargetOutcome(target="https://x.example.test", verified=[candidate])))
    assert "these_entries_only_work_on_this_machine" not in briefing


# -- a venv kept on purpose is not a trial venv ---------------------------------
#
# openqa.example.net went missing from .mcp.json this way. Where the system Python
# is externally managed, phase 1 installs into its own venv (install.venv_path)
# and the program runs from it, so the bridge it generated named that venv's
# interpreter -- and every venv was taken to be a trial one, so the entry was
# refused. The gate that refused it also ran only after the bridge stage had
# settled and closed the strategy stage, so nothing else was tried.


def make_venv(root) -> str:
    (root / "bin").mkdir(parents=True)
    (root / "pyvenv.cfg").write_text("home = /usr/bin\n")
    python = root / "bin" / "python3"
    python.write_text("")
    return str(python)


def bridge_in(tmp_path, python: str) -> Candidate:
    script = tmp_path / "artifacts" / "mcp_bridge_openqa_example_net.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("")
    return stdio([python, str(script)], {"MCP_BRIDGE_BASE_URL": "https://openqa.example.net"})


def test_a_venv_under_a_durable_root_is_host_bound(tmp_path) -> None:
    python = make_venv(tmp_path / "state" / "venv")
    verdict = assess(stdio([python, "-m", "srv"]), durable_dirs=[str(tmp_path / "state" / "venv")])
    assert verdict.kind == HOST_BOUND
    assert verdict.registrable is True


def test_a_venv_elsewhere_is_still_refused(tmp_path) -> None:
    python = make_venv(tmp_path / "artifacts" / "bridge" / "venv")
    verdict = assess(stdio([python]), durable_dirs=[str(tmp_path / "state" / "venv"), ""])
    assert verdict.kind == DISPOSABLE
    assert "durable_venv_roots" in verdict.reason


def test_the_sandbox_wins_over_a_durable_root(tmp_path) -> None:
    sandbox = tmp_path / "mcp-trials"
    python = make_venv(sandbox / "pkg" / "venv")
    verdict = assess(stdio([python]), sandbox_dirs=[str(sandbox)], durable_dirs=[str(tmp_path)])
    assert verdict.kind == DISPOSABLE
    assert "scratch" in verdict.reason


def test_phase_one_venv_is_durable_by_default(ctx, tmp_path) -> None:
    from initialize_claude_ai.core.config import load_settings

    venv = tmp_path / "state" / "venv"
    ctx.settings = load_settings(overrides=[f"install.venv_path={venv}"], environ={})
    candidate = bridge_in(tmp_path, make_venv(venv))
    assert McpWriter(ctx).portability_of(candidate).kind == HOST_BOUND


def test_the_venv_this_program_runs_from_is_durable(ctx, tmp_path, monkeypatch) -> None:
    import sys

    from initialize_claude_ai.core.config import load_settings

    venv = tmp_path / "running"
    python = make_venv(venv)
    monkeypatch.setattr(sys, "prefix", str(venv))
    candidate = bridge_in(tmp_path, python)
    assert McpWriter(ctx).portability_of(candidate).registrable is True
    ctx.settings = load_settings(overrides=["mcp.registration.trust_running_venv=false"], environ={})
    assert McpWriter(ctx).portability_of(candidate).registrable is False


def test_the_durable_roots_are_settings(settings) -> None:
    roots = settings.str_list("mcp.registration.durable_venv_roots")
    assert str(settings.path("install.venv_path")) in roots


def test_an_unportable_candidate_does_not_settle_a_stage(ctx, tmp_path) -> None:
    """Asked while the tiers below are open, in both modes, not only at publish."""
    from initialize_claude_ai.mcp_auto_discovery.agentic import AgenticPipeline
    from initialize_claude_ai.mcp_auto_discovery.traditional import TraditionalPipeline
    from initialize_claude_ai.mcp_auto_discovery.types import Stage, StageOutcome, TargetOutcome

    candidate = bridge_in(tmp_path, make_venv(tmp_path / "artifacts" / "venv"))
    outcome = TargetOutcome(target="https://openqa.example.net")

    stage = StageOutcome(stage=Stage.BRIDGE, verified=[candidate], candidates=[candidate])
    TraditionalPipeline(ctx).discard_unregistrable(stage, outcome)
    assert stage.verified == []
    assert "virtualenv" in stage.findings["not_registrable"][0]

    stage = StageOutcome(stage=Stage.BRIDGE)
    assert AgenticPipeline(ctx)._drop_ungrounded(stage, [candidate], outcome) == []
    assert "virtualenv" in stage.findings["not_registrable"][0]
