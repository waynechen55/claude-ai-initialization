"""The entry that ships has to be the entry that was tested.

Three servers reached an operator's machine, were listed by ``claude mcp list``
as configured, and failed:

.. code-block:: text

    bugzilla-opensuse-org  . failed   (CONNECTION_CLOSED)
    build-example-net          . failed   (CONNECTION_CLOSED)
    confluence-example-com    . failed   (HTTP 401, invalid_token)

Every one had passed every check this program had. Two of them share a cause
and the third is its own.

**The launch that was proven was not the launch that was written.** The
verifier trials a stdio candidate with the unresolvable secrets left out, and
logs that it is doing so::

    bugzilla-opensuse-org-mcp-bugzilla: trialling without BUGZILLA_API_KEY,
      BUGZILLA_AUTH_MODE -- a handshake proves what it is
    build-example-net-openapi-mcp: trialling without API_HEADERS

Both handshaked. Both were then written with those variables present as
``${NAME}`` placeholders -- a command nobody had run. An unset variable is not
a no-op: ``mcp-bugzilla`` exited during start-up, and so did
``@ivotoby/openapi-mcp-server``. ``CONNECTION_CLOSED`` is a process that died,
not a credential that was refused.

Two aggravating defects fed it:

* ``BUGZILLA_AUTH_MODE`` is a *selector* (``apikey``/``basic``/``anonymous``),
  not a secret. ``auth`` is a whole word in it, so the word-boundary rule that
  rescued ``NODE_TLS_REJECT_UNAUTHORIZED`` did not help, and no value was ever
  seen for it -- only a name.
* ``API_HEADERS`` takes ``Name:value`` pairs. Every credential was described
  with one sentence -- "may call it an API token" -- so the operator exported a
  bare token into it, exactly as instructed.

**And a vendor's cloud is not a customer's deployment.** confluence.example.com is
self-hosted Data Center, which the endpoint stage established itself by
following the SSO redirect. It registered ``https://mcp.atlassian.com/v2/mcp``
anyway -- Atlassian Cloud, which serves ``*.atlassian.net`` tenants -- and that
one URL closed tiers 2, 3 and 4, so ``mcp-atlassian``, which does talk to Data
Center, was never looked for.
"""

from __future__ import annotations

import pytest

from initialize_claude_ai.mcp_auto_discovery.grounding import (
    SERVED_ELSEWHERE,
    UNGROUNDED,
    assess,
    endpoint_hosts,
)
from initialize_claude_ai.mcp_auto_discovery.policy import DueDiligence, TierPolicy
from initialize_claude_ai.mcp_auto_discovery.portability import expand, launch_of, unresolved
from initialize_claude_ai.mcp_auto_discovery.secret_policy import (
    holds_a_credential,
    is_secret_name,
    names_a_selector,
)
from initialize_claude_ai.mcp_auto_discovery.types import (
    Candidate,
    Handshake,
    Stage,
    TargetOutcome,
    Transport,
)
from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter

# --------------------------------------------------------------------------- #
# a selector is not a secret
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "name",
    [
        "BUGZILLA_AUTH_MODE",  # the one that caused this
        "AUTH_METHOD",
        "TOKEN_TYPE",
        "KEY_FORMAT",
        "CREDENTIAL_SCHEME",
        "SESSION_STRATEGY",
    ],
)
def test_a_variable_that_names_a_choice_is_not_a_credential(name: str) -> None:
    assert names_a_selector(name)
    assert not is_secret_name(name), f"{name} selects a mode; it does not hold a value"


@pytest.mark.parametrize(
    "name",
    ["BUGZILLA_API_KEY", "AUTH_HEADER", "JIRA_PERSONAL_TOKEN", "DB_PASSWORD", "BEARER_TOKEN"],
)
def test_the_exemption_does_not_open_a_hole(name: str) -> None:
    """The last word decides, and none of these end in a selector word."""
    assert is_secret_name(name)
    assert holds_a_credential(name, "Nzk1NjMwMjcyODMw")


def test_a_selector_keeps_its_value_instead_of_becoming_a_placeholder() -> None:
    """The whole point: there is a value, and it is not a secret."""
    assert not holds_a_credential("BUGZILLA_AUTH_MODE", "apikey")
    assert holds_a_credential(
        "BUGZILLA_API_KEY",
        "ygS0WoUoRWfnhgm0vIoy81g2pKdFmNBXkyWAF9Lx",  # gitleaks:allow
    )


def test_the_agentic_pipeline_reclassifies_it_too(ctx) -> None:
    """The path the real failure actually took, which is not the obvious one.

    ``BUGZILLA_AUTH_MODE`` did not arrive in ``env`` to be reclassified. The
    agent put it straight into ``secret_env``, where it was filed as a
    credential with no classification at all -- so fixing only the ``env`` side
    would have left the reported bug exactly where it was.
    """
    from initialize_claude_ai.mcp_auto_discovery.agentic import AgenticPipeline

    plain, secrets, notes, _shapes, _sources = AgenticPipeline(ctx)._split_env(
        {"BUGZILLA_BASE_URL": "https://bugzilla.opensuse.org"},
        {"BUGZILLA_API_KEY": "env:BUGZILLA_API_KEY", "BUGZILLA_AUTH_MODE": "env:BUGZILLA_AUTH_MODE"},
    )

    assert "BUGZILLA_API_KEY" in secrets, "a real credential still is one"
    assert "BUGZILLA_AUTH_MODE" not in secrets, "a mode selector is not a credential"
    assert "BUGZILLA_AUTH_MODE" not in plain, "and no value was reported, so nothing to write"
    assert any("selects between named alternatives" in n for n in notes), "say why"


def test_the_agentic_pipeline_uses_the_shared_predicate_for_env_too(ctx) -> None:
    """It matched the raw regex by hand, so the selector guard walked past it."""
    from initialize_claude_ai.mcp_auto_discovery.agentic import AgenticPipeline

    plain, secrets, _notes, _shapes, _sources = AgenticPipeline(ctx)._split_env(
        {"BUGZILLA_AUTH_MODE": "apikey", "BUGZILLA_API_KEY": "Nzk1NjMwMjcyODMwOm73KJJW"}, {}
    )

    assert plain["BUGZILLA_AUTH_MODE"] == "apikey", "the value survives, it is not secret"
    assert "BUGZILLA_API_KEY" in secrets


# --------------------------------------------------------------------------- #
# write the launch that was verified
# --------------------------------------------------------------------------- #


def trialled_without(*pending: str) -> Candidate:
    """A candidate that handshaked with those variables absent."""
    candidate = Candidate(
        kind="stdio",
        name="bugzilla",
        transport=Transport.STDIO,
        command=["uvx", "mcp-bugzilla", "--bugzilla-server", "https://bugzilla.opensuse.org"],
        env={"BUGZILLA_BASE_URL": "https://bugzilla.opensuse.org"},
    )
    candidate.secret_env = {name: f"env:{name}" for name in pending}
    candidate.handshake = Handshake(
        ok=True,
        transport=Transport.STDIO,
        server_name="mcp-bugzilla",
        tools=["bug_info"],
        round_trip=True,
        credential_pending=sorted(pending),
    )
    return candidate


def test_a_variable_the_proven_launch_lacked_is_written_empty_while_unset(ctx) -> None:
    """The exact entry that shipped and died -- and the fix that followed it.

    A bare ``${BUGZILLA_AUTH_MODE}`` reached the server as those literal
    characters and it exited. Leaving the variable out fixed that and hid it
    from everyone who copies the file. ``${NAME:-}`` is both: empty while unset,
    so the launch is the one that was proven, and named, so it can be found.
    The start check then launches it as written; a server that minds an empty
    value has the slot withdrawn by the ``withdraw_optional_reads`` repair.
    """
    candidate = trialled_without("BUGZILLA_API_KEY", "BUGZILLA_AUTH_MODE")
    entry = McpWriter(ctx).entry_for(candidate, "bugzilla-opensuse-org")

    # Optional, and read through names only this entry reads (own_reference_names):
    # a generic BUGZILLA_API_KEY a shell exports for something else never wins.
    assert entry["env"]["BUGZILLA_AUTH_MODE"] == "${BUGZILLA_OPENSUSE_ORG_BUGZILLA_AUTH_MODE:-}"
    assert entry["env"]["BUGZILLA_API_KEY"] == "${BUGZILLA_OPENSUSE_ORG_BUGZILLA_API_KEY:-}"
    assert entry["env"]["BUGZILLA_BASE_URL"] == "https://bugzilla.opensuse.org", (
        "the configuration that *was* trialled stays"
    )


def test_the_optional_slot_is_explained_rather_than_silent(ctx) -> None:
    candidate = trialled_without("BUGZILLA_AUTH_MODE")
    McpWriter(ctx).entry_for(candidate, "bugzilla-opensuse-org")

    assert any("proven to start without it" in note for note in candidate.notes)
    assert any("empty string while it is unset" in note for note in candidate.notes)


def test_a_variable_the_proven_launch_carried_is_written(ctx) -> None:
    """Deferred, not abolished -- otherwise no credential would ever be written."""
    candidate = trialled_without()
    candidate.secret_env = {"BUGZILLA_API_KEY": "env:BUGZILLA_API_KEY"}
    entry = McpWriter(ctx).entry_for(candidate, "bugzilla-opensuse-org")

    assert entry["env"]["BUGZILLA_API_KEY"] == "${BUGZILLA_OPENSUSE_ORG_BUGZILLA_API_KEY}"


def test_a_name_with_no_value_is_neither_trialled_nor_written(ctx) -> None:
    """It used to fail the candidate outright, which would have lost the server.

    Dropping is the honest answer: a name with no value says nothing about
    whether the server needs it, and the writer already omits empty values --
    so failing here trialled one launch and wrote another.
    """
    from initialize_claude_ai.mcp_auto_discovery.verifier import CandidateVerifier

    ctx.dry_run = False
    ctx.runner.which = lambda name: f"/usr/bin/{name}"  # type: ignore[assignment]
    candidate = Candidate(
        kind="stdio",
        name="x",
        transport=Transport.STDIO,
        command=["/bin/true"],
        env={"REAL": "https://example.test", "NAMED_BUT_EMPTY": ""},
    )
    CandidateVerifier(ctx).verify(candidate)

    assert any("NAMED_BUT_EMPTY" in note for note in candidate.notes)
    assert "NAMED_BUT_EMPTY" not in McpWriter(ctx).entry_for(candidate).get("env", {})


# --------------------------------------------------------------------------- #
# resolve an entry the way the client will
# --------------------------------------------------------------------------- #


def test_a_default_answers_when_the_variable_is_unset() -> None:
    assert expand("${NOPE:-/usr/bin/python3}", {}) == "/usr/bin/python3"


def test_the_variable_wins_when_it_is_set() -> None:
    assert expand("${PY:-/usr/bin/python3}", {"PY": "/opt/py"}) == "/opt/py"


def test_an_unset_variable_with_no_default_is_left_visible() -> None:
    """Not silently emptied. That case is the bug, so it has to stay legible."""
    assert expand("${GONE}", {}) == "${GONE}"
    assert unresolved("${GONE}", {}) == ["GONE"]
    assert unresolved("${GONE:-x}", {}) == [], "a default is not a hole"


def test_the_launch_of_an_entry_reports_its_holes() -> None:
    entry = {
        "command": "uvx",
        "args": ["mcp-bugzilla"],
        "env": {"BUGZILLA_AUTH_MODE": "${BUGZILLA_AUTH_MODE}", "OK": "yes"},
    }
    argv, env, gaps = launch_of(entry, {})

    assert argv == ["uvx", "mcp-bugzilla"]
    assert env["OK"] == "yes"
    assert gaps == ["BUGZILLA_AUTH_MODE"], "the variable that killed the real server"


# --------------------------------------------------------------------------- #
# and then actually start it
# --------------------------------------------------------------------------- #


def startable(ctx):
    """A context permitted to launch a real process."""
    ctx.dry_run = False
    ctx.runner.which = lambda name, **_: f"/usr/bin/{name}"  # type: ignore[assignment]
    return ctx


def test_an_entry_that_cannot_start_is_reported_rather_than_claimed(ctx) -> None:
    """A server that exits on start-up must not be reported as configured."""
    from initialize_claude_ai.mcp_auto_discovery.verifier import CandidateVerifier

    shake = CandidateVerifier(startable(ctx)).verify_as_written(
        {"command": "/bin/false", "args": []}, "exits-immediately"
    )
    assert shake is not None and not shake.ok


def dies_on_start() -> Candidate:
    """Handshaked when trialled; the written entry will not start.

    ``/bin/false`` stands in for `mcp-bugzilla` launched with an auth mode it
    cannot parse. The handshake is recorded because the *candidate* really did
    pass -- that is the whole shape of the bug.
    """
    candidate = Candidate(
        kind="stdio",
        name="dies",
        transport=Transport.STDIO,
        command=["/bin/false"],
        env={"BASE_URL": "https://bugzilla.opensuse.org"},
    )
    candidate.handshake = Handshake(ok=True, transport=Transport.STDIO, server_name="x", tools=["t"], round_trip=True)
    return candidate


def test_publish_actually_runs_the_gate(ctx) -> None:
    """Wired, not merely defined.

    A check that the publish path never reaches is a check that does not exist,
    and this suite has shipped one of those before -- the fixture context is a
    dry run, so a test that forgets to turn that off proves nothing.
    """
    registrations = McpWriter(startable(ctx)).publish(
        TargetOutcome(target="https://bugzilla.opensuse.org", verified=[dies_on_start()])
    )
    assert registrations[0].start_check is not None, "the entry was never launched"
    assert registrations[0].start_check.ok is False


def test_and_the_operator_is_told_rather_than_left_to_find_out(ctx) -> None:
    registration = McpWriter(startable(ctx)).publish(
        TargetOutcome(target="https://bugzilla.opensuse.org", verified=[dies_on_start()])
    )[0]

    assert "did not start" in registration.detail
    assert any("was NOT written" in r for r in registration.remediation)
    assert registration.to_dict()["starts_as_written"] is False


def test_the_refusal_says_which_repairs_were_tried(ctx) -> None:
    """A refusal has to be actionable, and "we tried these" is most of that.

    The earlier contract kept a non-starting entry on the grounds that
    discarding a real server is how confluence.example.com was lost. That worry is
    answered differently now: the entry is repaired first, and only a candidate
    that cannot be made to start is refused -- which also reopens the lower
    tiers, so the target keeps looking rather than settling on a dead line.
    """
    registration = McpWriter(startable(ctx)).publish(
        TargetOutcome(target="https://bugzilla.opensuse.org", verified=[dies_on_start()])
    )[0]
    assert "repairs tried --" in registration.refused
    # Each one reports its own outcome: "nothing to change" and "changed it and
    # it did not help" are different facts, and the operator needs both.
    assert registration.attempted_repairs, "say what was attempted, not just that it failed"
    assert all(":" in line for line in registration.attempted_repairs)


# --------------------------------------------------------------------------- #
# the entry is tested the way the client will launch it, and repaired if it can be
# --------------------------------------------------------------------------- #


def test_the_entry_is_launched_with_the_clients_environment_not_ours(ctx) -> None:
    """The mechanism behind every `starts: yes` beside `claude: no`.

    Our trial inherited `os.environ`; Claude Code passes the entry's own `env`
    plus a narrow passthrough. So an entry could pass here on a variable from
    the operator's shell and fail there for want of it -- a check more
    permissive than the thing it predicts cannot predict it.
    """
    import os as _os

    from initialize_claude_ai.mcp_auto_discovery.probes import McpProber

    _os.environ["ICA_TEST_ONLY_INHERITED"] = "1"
    try:
        built = McpProber(ctx).client_env({"DECLARED": "yes"})
    finally:
        _os.environ.pop("ICA_TEST_ONLY_INHERITED", None)

    assert built["DECLARED"] == "yes", "what the entry declares is passed"
    assert "ICA_TEST_ONLY_INHERITED" not in built, "what it does not declare is not"
    assert "PATH" in built, "a server still needs to find its interpreter"


def test_the_passthrough_is_configuration(ctx) -> None:
    ctx.settings.set("mcp.verification.client_env.passthrough", "HOME")
    from initialize_claude_ai.mcp_auto_discovery.probes import McpProber

    built = McpProber(ctx).client_env({})
    assert "PATH" not in built and ("HOME" in built or "HOME" not in __import__("os").environ)


def needs_declared_env(tmp_path) -> Candidate:
    """A server that starts only when a variable is present.

    Modelled on the real shape: the value is known to discovery -- it is in the
    candidate -- and simply never made it into the entry.
    """
    script = tmp_path / "needs_env.py"
    script.write_text(
        "import os, sys\n"
        "if not os.environ.get('NEEDED'):\n"
        "    sys.stderr.write('NEEDED is not set\\n'); raise SystemExit(1)\n" + WORKING_SERVER
    )
    candidate = Candidate(
        kind="stdio",
        name="needs-env",
        transport=Transport.STDIO,
        command=[__import__("sys").executable, str(script)],
        env={"NEEDED": "yes", "BASE_URL": "https://bugzilla.opensuse.org"},
    )
    candidate.handshake = Handshake(ok=True, transport=Transport.STDIO, server_name="x", tools=["t"], round_trip=True)
    return candidate


def test_a_variable_the_entry_forgot_is_declared_and_re_proved(ctx, tmp_path) -> None:
    """Self-healing, and the repair is verified rather than assumed.

    The entry is stripped of a variable the server needs -- the shape that made
    two openQA entries start for us and fail for Claude Code -- and the repair
    puts it back from what discovery already knew, then launches it again.
    """
    candidate = needs_declared_env(tmp_path)
    writer = McpWriter(startable(ctx))

    # Simulate the gap: the entry omits it, the candidate still knows it.
    original = writer.entry_for
    writer.entry_for = lambda c, n="": {  # type: ignore[assignment]
        **original(c, n),
        "env": {"BASE_URL": "https://bugzilla.opensuse.org"},
    }

    registration = writer.publish(TargetOutcome(target="https://bugzilla.opensuse.org", verified=[candidate]))[0]

    assert not registration.refused, "it was repairable, so it must not be refused"
    assert registration.repairs == ["declare_inherited_env"]
    assert registration.entry["env"]["NEEDED"] == "yes"
    assert registration.start_check.ok is True, "the repair was re-proved, not assumed"


def test_a_repair_that_does_not_help_is_not_kept(ctx) -> None:
    """Each repair is re-verified, so a change that fixed nothing is discarded."""
    registration = McpWriter(startable(ctx)).publish(
        TargetOutcome(target="https://bugzilla.opensuse.org", verified=[dies_on_start()])
    )[0]
    assert registration.repairs == [], "nothing worked, so nothing is claimed"
    assert registration.refused


def test_repairs_can_be_turned_off(ctx, tmp_path) -> None:
    live = startable(ctx)
    live.settings.set("mcp.healing.repairs", "")
    candidate = needs_declared_env(tmp_path)
    writer = McpWriter(live)
    original = writer.entry_for
    writer.entry_for = lambda c, n="": {  # type: ignore[assignment]
        **original(c, n),
        "env": {"BASE_URL": "https://bugzilla.opensuse.org"},
    }
    registration = writer.publish(TargetOutcome(target="https://bugzilla.opensuse.org", verified=[candidate]))[0]
    assert registration.refused, "without repair it cannot start, so it is refused"


#: A minimal but honest stdio MCP server: it negotiates, lists a tool, and
#: answers a second request on the same session -- the three things the prober
#: requires. Here so the new gate can be shown *not* to fire on a good entry;
#: a check that fails everything would be worse than no check at all.
WORKING_SERVER = r"""
import json, sys
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
                  "serverInfo": {"name": "honest", "version": "1.0"}}
    elif method == "tools/list":
        result = {"tools": [{"name": "ping", "description": "",
                             "inputSchema": {"type": "object", "properties": {}}}]}
    else:
        result = {}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}) + "\n")
    sys.stdout.flush()
"""


def test_a_good_entry_is_not_reported_as_broken(ctx, tmp_path) -> None:
    """The false-positive half, which matters as much as the true-positive half.

    Without this, a gate that refused to start anything would pass the suite
    and break every working stdio entry in the file.
    """
    import sys

    script = tmp_path / "honest_server.py"
    script.write_text(WORKING_SERVER)

    candidate = dies_on_start()
    candidate.command = [sys.executable, str(script)]

    registration = McpWriter(startable(ctx)).publish(
        TargetOutcome(target="https://bugzilla.opensuse.org", verified=[candidate])
    )[0]

    assert registration.start_check is not None, "it was launched"
    assert registration.start_check.ok is True, registration.start_check.error
    assert "did not start" not in registration.detail
    assert not registration.refused


def test_the_gate_is_configuration(ctx) -> None:
    startable(ctx)
    ctx.settings.set("mcp.registration.verify_as_written", False)

    registration = McpWriter(ctx).publish(
        TargetOutcome(target="https://bugzilla.opensuse.org", verified=[dies_on_start()])
    )[0]
    assert registration.start_check is None


# --------------------------------------------------------------------------- #
# a server that advertises nothing has not asked for a credential
# --------------------------------------------------------------------------- #


def trialled(tools: list[str]) -> Candidate:
    """`mcp-atlassian` for confluence.example.com, launched without its secret."""
    candidate = Candidate(
        kind="stdio",
        name="confluence-example-com-mcp-atlassian",
        transport=Transport.STDIO,
        command=["/bin/true"],
        env={"CONFLUENCE_URL": "https://confluence.example.com"},
    )
    candidate.secret_env = {"PERSONAL_TOKEN": "env:PERSONAL_TOKEN"}
    candidate.handshake = Handshake(
        ok=True,
        transport=Transport.STDIO,
        server_name="mcp-atlassian",
        tools=tools,
        round_trip=True,
    )
    return candidate


def wall(ctx, candidate: Candidate) -> Candidate:
    """Run the verifier's credential-wall reasoning over a finished handshake."""
    from initialize_claude_ai.mcp_auto_discovery.verifier import CandidateVerifier

    ctx.dry_run = False
    ctx.runner.which = lambda name, **_: f"/usr/bin/{name}"  # type: ignore[assignment]
    ctx.secrets.resolve = lambda *a, **k: None  # type: ignore[assignment]
    shake = candidate.handshake
    verifier = CandidateVerifier(ctx)
    verifier.prober.probe_stdio = lambda *a, **k: shake  # type: ignore[assignment]
    verifier.verify(candidate)
    return candidate


def test_a_server_that_lists_nothing_is_not_a_credential_wall(ctx) -> None:
    """The confluence entry, and the inference that cost the target its answer.

    `mcp-atlassian` handshaked, advertised zero tools, and was recorded as "a
    real MCP endpoint that refused an anonymous client". That made it
    provisional, which settled the target, which closed tiers 3 and 4 -- and
    shipped an entry naming `PERSONAL_TOKEN`, a variable the package does not
    read. The operator supplied it and still had no tools, because the
    credential was never the missing piece.
    """
    candidate = wall(ctx, trialled([]))

    assert candidate.needs_credential is False, "nothing showed a credential was what it wanted"
    assert candidate.verified is False, "and it is certainly not working"
    hints = " ".join(candidate.handshake.protocol_hints)
    assert "advertised no tools" in hints
    assert "not the one this package reads" in hints, "name the likeliest cause"


def test_a_server_that_lists_its_tools_still_is_one(ctx) -> None:
    """The justification for trialling without the secret, intact.

    A stdio server reads its credential when a tool is *called*, so one
    launched without it still lists what it can do. That is the case this
    branch exists for and it must keep working.
    """
    candidate = wall(ctx, trialled(["confluence_search", "confluence_get_page"]))

    assert candidate.needs_credential is True
    assert candidate.handshake.credential_pending == ["PERSONAL_TOKEN"]


def test_the_older_behaviour_is_still_reachable(ctx) -> None:
    ctx.settings.set("mcp.packages.credential_wall_needs_tools", False)
    candidate = wall(ctx, trialled([]))
    assert candidate.needs_credential is True


def test_so_the_stage_does_not_settle_and_the_lower_tiers_run(ctx) -> None:
    """The consequence that matters: tiers 3 and 4 get their turn.

    A candidate that is neither verified nor provisional contributes nothing
    for the target to settle on, which is the whole point -- confluence.example.com
    had a bridge stage waiting behind this.
    """
    candidate = wall(ctx, trialled([]))
    outcome = TargetOutcome(target="https://confluence.example.com")
    assert candidate not in outcome.verified
    assert candidate not in outcome.provisional


# --------------------------------------------------------------------------- #
# the client's own verdict is a finding, not a column
# --------------------------------------------------------------------------- #


def test_a_refusal_from_claude_code_reaches_the_report(ctx) -> None:
    """It was rendered into a table and consumed by nothing.

    Two servers were registered in a run whose own table said::

        connected  tools  two-way  usable  starts  claude
        yes        yes    yes      -       yes     no
              - claude: Claude Code reports: failed to connect

    The phase reported PARTIAL, both entries were written, and the operator
    found out in the next session. `claude mcp get` is the one check performed
    by the *real client*, connecting exactly as the next session will, so a
    negative from it is the strongest signal available here.
    """
    from initialize_claude_ai.mcp_auto_discovery.verifier import Check

    writer = McpWriter(ctx)
    registration = writer.publish(TargetOutcome(target="https://openqa.example.net", verified=[dies_on_start()]))[0]
    writer.absorb_claude_check(registration, Check("claude", False, "Claude Code reports: failed to connect"))

    assert "could not connect" in registration.detail
    assert registration.remediation, "a finding with nothing to do about it is a column"


def test_the_disagreement_with_our_own_launch_is_spelled_out(ctx) -> None:
    """`starts: yes` beside `claude: no` is the sharpest diagnostic in the run.

    It says the entry works when this program launches it and not when the
    client does -- which narrows the cause to the environment and the working
    directory. Leaving the operator to infer that from two green ticks and a
    red one wastes the information.
    """
    from initialize_claude_ai.mcp_auto_discovery.verifier import Check

    writer = McpWriter(ctx)
    registration = writer.publish(TargetOutcome(target="https://openqa.example.net", verified=[dies_on_start()]))[0]
    registration.start_check = Check("starts", True, "the entry as written completed a handshake")
    writer.absorb_claude_check(registration, Check("claude", False, "failed to connect"))

    advice = " ".join(registration.remediation)
    assert "environment and the working directory" in advice


def test_a_server_the_client_is_happy_with_says_nothing_extra(ctx) -> None:
    from initialize_claude_ai.mcp_auto_discovery.verifier import Check

    writer = McpWriter(ctx)
    registration = writer.publish(TargetOutcome(target="https://openqa.example.net", verified=[dies_on_start()]))[0]
    before = list(registration.remediation)
    writer.absorb_claude_check(registration, Check("claude", True, "connected"))
    assert registration.remediation == before


def test_a_check_that_could_not_be_performed_is_not_a_refusal(ctx) -> None:
    """`-` means nobody asked. Treating it as "no" would fail every dry run."""
    from initialize_claude_ai.mcp_auto_discovery.verifier import Check

    writer = McpWriter(ctx)
    registration = writer.publish(TargetOutcome(target="https://openqa.example.net", verified=[dies_on_start()]))[0]
    before = list(registration.remediation)
    writer.absorb_claude_check(registration, Check("claude", None, "the CLI is not installed"))
    assert registration.remediation == before


def test_an_entry_that_will_not_start_is_refused_rather_than_written(ctx) -> None:
    """The contract changed, and this is the change.

    An entry that does not start is not a configuration -- it is a line that
    fails in front of the operator as "Connection closed", with no reason
    attached. Two openQA entries shipped exactly that way while the run called
    itself PARTIAL. Refusing also reopens the lower tiers, so the target keeps
    looking instead of settling on something that cannot run.
    """
    registration = McpWriter(startable(ctx)).publish(
        TargetOutcome(target="https://bugzilla.opensuse.org", verified=[dies_on_start()])
    )[0]

    assert registration.refused, "it does not start, so it is not an entry"
    assert "did not start when launched exactly as written" in registration.refused
    assert not registration.written


def test_an_earlier_entry_is_left_alone_and_the_operator_is_told(ctx, tmp_path) -> None:
    """Refusing to write is not the same as clearing the file, and saying
    nothing about the difference is how an operator ends up debugging an entry
    they think this run produced.

    This run has nothing better to put there, so the old entry stays -- but it
    was not verified today, and that is the sentence that matters.
    """
    import json as _json

    live = startable(ctx)
    from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter

    writer = McpWriter(live)
    writer.json_path.parent.mkdir(parents=True, exist_ok=True)
    writer.json_path.write_text(
        _json.dumps({"mcpServers": {"bugzilla-opensuse-org": {"type": "stdio", "command": "old"}}}),
        encoding="utf-8",
    )

    registration = writer.publish(TargetOutcome(target="https://bugzilla.opensuse.org", verified=[dies_on_start()]))[0]

    assert registration.refused
    assert registration.superseded_nothing == {"type": "stdio", "command": "old"}
    advice = " ".join(registration.remediation)
    assert "left untouched" in advice
    assert "NOT re-verified today" in advice

    # And the old entry really is still there, unchanged.
    servers = _json.loads(writer.json_path.read_text())["mcpServers"]
    assert servers["bugzilla-opensuse-org"] == {"type": "stdio", "command": "old"}


def test_with_no_earlier_entry_it_simply_says_nothing_was_written(ctx) -> None:
    registration = McpWriter(startable(ctx)).publish(
        TargetOutcome(target="https://bugzilla.opensuse.org", verified=[dies_on_start()])
    )[0]
    assert registration.superseded_nothing is None
    assert not any("left untouched" in r for r in registration.remediation)


def test_writing_it_anyway_is_still_reachable(ctx) -> None:
    """The older, report-only behaviour remains one setting away."""
    live = startable(ctx)
    live.settings.set("mcp.registration.require_entry_starts", False)
    registration = McpWriter(live).publish(
        TargetOutcome(target="https://bugzilla.opensuse.org", verified=[dies_on_start()])
    )[0]

    assert not registration.refused
    assert any("did not start when launched" in r for r in registration.remediation)


def test_every_mark_lands_under_its_own_heading(ctx) -> None:
    """The verification table is built by name, not by position.

    ``audit`` returns a different number of rows depending on what happened: a
    server that handshaked without its credential gets an extra ``usable`` row.
    Laid out in order, that pushed every mark after it one column right -- so
    for exactly the servers this work is about, the column saying whether the
    thing worked was the one that slid under the wrong heading. Nothing failed;
    the table was just quietly wrong.
    """
    from initialize_claude_ai.mcp_auto_discovery.verifier import CHECKS, CandidateVerifier
    from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter

    walled = trialled_without("BUGZILLA_API_KEY")
    writer = McpWriter(ctx)
    registration = writer.publish(TargetOutcome(target="https://bugzilla.opensuse.org", provisional=[walled]))[0]

    checks = CandidateVerifier.audit(registration.candidate)
    checks.append(writer.start_check_row(registration))
    checks.append(writer.claude_check(registration))
    marks = {check.name: check.mark for check in checks}

    assert "usable" in marks, "the row that used to shift everything after it"
    assert set(marks) <= set(CHECKS), f"a check with no column: {set(marks) - set(CHECKS)}"
    assert marks["connected"] == "yes", "it did handshake"
    assert marks["usable"] == "-", "and it is not usable yet"


def test_a_remote_entry_has_no_process_to_start(ctx) -> None:
    from initialize_claude_ai.mcp_auto_discovery.verifier import CandidateVerifier

    entry = {"type": "http", "url": "https://mcp.example.test/mcp"}
    assert CandidateVerifier(startable(ctx)).verify_as_written(entry, "remote") is None


def test_a_dry_run_launches_nothing(ctx) -> None:
    from initialize_claude_ai.mcp_auto_discovery.verifier import CandidateVerifier

    ctx.dry_run = True
    entry = {"command": "/bin/false", "args": []}
    assert CandidateVerifier(ctx).verify_as_written(entry, "x") is None


# --------------------------------------------------------------------------- #
# one variable, one shape
# --------------------------------------------------------------------------- #


def openapi_bridge() -> Candidate:
    """build.example.net's real entry, minus the token the operator pasted in."""
    candidate = Candidate(
        kind="stdio",
        name="build",
        transport=Transport.STDIO,
        command=["npx", "-y", "@ivotoby/openapi-mcp-server@1.16.1"],
        env={"API_BASE_URL": "https://build.example.net"},
    )
    candidate.secret_env = {"API_HEADERS": "env:API_HEADERS"}
    return candidate


def test_a_header_list_is_not_described_as_a_token(ctx) -> None:
    """The sentence the operator acted on, and the value it produced."""
    writer = McpWriter(ctx)
    placeholder = writer.credential_placeholder(openapi_bridge(), "API_HEADERS")

    assert "Name:value" in placeholder
    assert "your real token" not in placeholder
    assert "not a bare token" in placeholder, "say what the operator actually got wrong"


def test_two_variables_on_one_server_get_two_answers(ctx) -> None:
    """One sentence for all of them is what made the guidance wrong."""
    candidate = openapi_bridge()
    candidate.secret_env["API_USERNAME"] = "env:API_USERNAME"
    writer = McpWriter(ctx)

    headers = writer.credential_placeholder(candidate, "API_HEADERS")
    user = writer.credential_placeholder(candidate, "API_USERNAME")

    assert headers != user
    assert "account name" in user


def test_an_unknown_shape_admits_it(ctx) -> None:
    candidate = openapi_bridge()
    candidate.secret_env = {"WIDGET_CREDENTIAL": "env:WIDGET_CREDENTIAL"}

    placeholder = McpWriter(ctx).credential_placeholder(candidate, "WIDGET_CREDENTIAL")
    assert "shape unknown" in placeholder


def test_a_discovered_shape_beats_the_name(ctx) -> None:
    """Whatever read the product's documentation knows more than a regex."""
    candidate = openapi_bridge()
    candidate.secret_formats["API_HEADERS"] = "X-Api-Key:<key> only, comma separated"

    placeholder = McpWriter(ctx).credential_placeholder(candidate, "API_HEADERS")
    assert "X-Api-Key" in placeholder


# --------------------------------------------------------------------------- #
# a vendor's cloud is not a customer's deployment
# --------------------------------------------------------------------------- #


def atlassian_cloud() -> Candidate:
    """What tier 1 returned for confluence.example.com."""
    return Candidate(
        kind="url",
        name="confluence",
        url="https://mcp.atlassian.com/v2/mcp",
        transport=Transport.STREAMABLE_HTTP,
        source="Atlassian's own MCP documentation and the official MCP registry",
        handshake=Handshake(
            ok=False,
            status=401,
            requires_auth=True,
            auth_evidence=['Bearer resource_metadata="https://mcp.atlassian.com/..."'],
        ),
    )


def test_the_vendors_cloud_endpoint_is_not_grounded_in_a_self_hosted_instance() -> None:
    verdict = assess(atlassian_cloud(), "https://confluence.example.com")
    assert verdict.kind == UNGROUNDED


def test_a_claim_cannot_corroborate_itself() -> None:
    """The circle that let it through.

    ``endpoint_hosts`` collects the tier-1 checklist *and the candidates*, so
    the address being judged was in its own ``served_by`` -- "mcp.atlassian.com
    is served from mcp.atlassian.com, which this run found answering".
    """
    outcome = TargetOutcome(target="https://confluence.example.com")
    outcome.provisional = [atlassian_cloud()]

    assert "mcp.atlassian.com" in endpoint_hosts(outcome), "it is in the evidence"
    assert (
        assess(atlassian_cloud(), "https://confluence.example.com", served_by=endpoint_hosts(outcome)).kind
        == UNGROUNDED
    ), "and it still may not vouch for itself"


def test_the_vendors_own_service_is_still_believed() -> None:
    """github.com is served from api.githubcopilot.com. That must keep working.

    The difference is not the hop, which both have. It is whose claim it is:
    GitHub documented a GitHub-named address for a GitHub target, while
    Atlassian documented an Atlassian-named address for a Example one.
    """
    github = Candidate(
        kind="url",
        name="github",
        url="https://api.githubcopilot.com/mcp/",
        transport=Transport.STREAMABLE_HTTP,
        source="GitHub's own documentation for the remote MCP server",
        handshake=Handshake(ok=False, status=401, requires_auth=True, auth_evidence=["json, not a web page"]),
    )
    assert assess(github, "https://github.com").kind == SERVED_ELSEWHERE


# --------------------------------------------------------------------------- #
# and it must not close the tiers that could have answered
# --------------------------------------------------------------------------- #


def policy(ctx) -> TierPolicy:
    return TierPolicy(ctx)


def test_an_unproven_foreign_wall_leaves_the_lower_tiers_open(ctx) -> None:
    """The damage was not the entry; it was the three stages it cancelled."""
    record = DueDiligence(target="https://confluence.example.com")
    record.auth_walls = ["https://mcp.atlassian.com/v2/mcp"]

    allowed, why = policy(ctx).gate(Stage.PACKAGE, record)
    assert allowed, why
    assert policy(ctx).gate(Stage.BRIDGE, record)[0]


def test_a_wall_on_the_targets_own_organisation_still_closes_them(ctx) -> None:
    """The rule is about whose domain it is, not about being unproven."""
    record = DueDiligence(target="https://gitlab.example.net")
    record.auth_walls = ["https://mcp.example.net/mcp"]

    allowed, why = policy(ctx).gate(Stage.PACKAGE, record)
    assert not allowed
    assert "forbid_lower_tiers_when_native" in why


def test_a_proven_endpoint_closes_them_wherever_it_lives(ctx) -> None:
    """api.githubcopilot.com is the ordinary shape of a hosted offering."""
    record = DueDiligence(target="https://github.com")
    record.native_endpoints = ["https://api.githubcopilot.com/mcp/"]

    assert not policy(ctx).gate(Stage.PACKAGE, record)[0]


def test_an_operator_can_turn_the_reopening_off(ctx) -> None:
    ctx.settings.set("mcp.policy.lower_tiers_when_wall_is_foreign", False)
    record = DueDiligence(target="https://confluence.example.com")
    record.auth_walls = ["https://mcp.atlassian.com/v2/mcp"]

    assert not TierPolicy(ctx).gate(Stage.PACKAGE, record)[0]
