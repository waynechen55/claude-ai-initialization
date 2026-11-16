"""A server that refuses to *start* without its token is not broken.

Two servers exit non-zero when trialled with a credential left out, and they
mean opposite things:

* one crashed for a reason that has nothing to do with the credential, and
  supplying it would change nothing; and
* one read its configuration, found the variable absent, and declined -- which
  is the strongest possible statement that the credential is the missing piece.

``did it answer?`` cannot tell them apart, and both wrong answers have been
shipped. Calling every crash a credential wall wrote entries whose ``${...}``
nobody could fill, because the process died before reading it. Calling every
crash a failure then lost gitlab.example.net, whose bridge had already been built,
pinned at the target and verified end-to-end.

What separates them is evidence from the server itself -- the same standard a
401 has to meet before it counts as an endpoint. It either names a variable we
withheld, or it says something auth-shaped from a configured list.

The consequences of the verdict are checked here too, because they are the
reason it matters: a start-up wall must keep its ``${VAR}`` slot (the server
just said it needs it), must not be called verified, and must still be
registrable.
"""

from __future__ import annotations

from initialize_claude_ai.mcp_auto_discovery.types import Candidate, Handshake, Transport
from initialize_claude_ai.mcp_auto_discovery.verifier import CandidateVerifier

PENDING = ["GITLAB_TOKEN"]


def _verifier(ctx) -> CandidateVerifier:
    return CandidateVerifier(ctx)


def _crash(error: str) -> Handshake:
    return Handshake(transport=Transport.STDIO, url="gitlab-mcp", error=error)


# -- what counts as the server saying so ---------------------------------------


def test_naming_the_withheld_variable_is_decisive(ctx):
    said = _verifier(ctx).refused_to_start_without(
        _crash("process exited with rc=1: RuntimeError: GITLAB_TOKEN is required"),
        PENDING,
    )
    assert "GITLAB_TOKEN" in said


def test_naming_some_other_variable_is_not(ctx):
    """A message about a variable we did supply is about something else."""
    assert not _verifier(ctx).refused_to_start_without(
        _crash("process exited with rc=1: RuntimeError: GITLAB_URL is malformed"),
        PENDING,
    )


def test_an_auth_shaped_message_counts(ctx):
    said = _verifier(ctx).refused_to_start_without(
        _crash("process exited with rc=1: Error: authentication required"), PENDING
    )
    assert "authentication required" in said


def test_an_unrelated_traceback_does_not(ctx):
    """The `@ivotoby/openapi-mcp-server` shape, which must stay a failure."""
    assert not _verifier(ctx).refused_to_start_without(
        _crash(
            "process exited with rc=1: TypeError: Cannot read properties of "
            "undefined (reading 'paths') | at ToolsManager.initialize"
        ),
        PENDING,
    )


def test_silence_is_not_evidence(ctx):
    assert not _verifier(ctx).refused_to_start_without(_crash(""), PENDING)


def test_the_rule_can_be_switched_off(ctx):
    ctx.settings.set("mcp.packages.startup_credential.enabled", False)
    assert not _verifier(ctx).refused_to_start_without(_crash("RuntimeError: GITLAB_TOKEN is required"), PENDING)


def test_emptying_the_markers_leaves_the_naming_rule(ctx):
    """The names come from the candidate, so rule 1 needs no configuration."""
    ctx.settings.set("mcp.packages.startup_credential.markers", [])
    verifier = _verifier(ctx)
    assert verifier.refused_to_start_without(_crash("RuntimeError: GITLAB_TOKEN is required"), PENDING)
    assert not verifier.refused_to_start_without(_crash("Error: authentication required"), PENDING)


# -- and what the verdict then means -------------------------------------------


def _walled() -> Candidate:
    shake = _crash("RuntimeError: GITLAB_TOKEN is required")
    shake.credential_pending = list(PENDING)
    return Candidate(
        name="gitlab-example-net",
        url="https://gitlab.example.net",
        command=["npx", "-y", "gitlab-mcp"],
        handshake=shake,
    )


def test_a_startup_wall_is_not_verified(ctx):
    """It never handshaked. Nothing here may pretend it did."""
    assert not _walled().verified


def test_a_startup_wall_is_registrable(ctx):
    """The whole point: it survives as an endpoint waiting for a token."""
    assert _walled().needs_credential


def test_a_startup_wall_keeps_its_variable_slot(ctx):
    """``proven_without`` is what makes the writer soften a ``${VAR}``.

    A server that proved it starts without a variable gets ``${VAR:-}``, empty
    while unset. A server that refused to start without it needs the plain slot
    -- so the set must stay empty here, or the entry would call optional the one
    thing the server just asked for.
    """
    assert _walled().proven_without == set()


# -- the evidence has to survive to be read ------------------------------------
#
# This verdict is only as good as the text it consults, and the text arrives
# from a subprocess's stderr. Joining the last three lines oldest-first puts a
# code frame in front of the exception, and anything that then shortens the
# string for a console keeps the frame and drops the only line that names a
# cause -- which is exactly how the gitlab bridge's message reached the log as
# "user_lifespan_result = await stack.enter_async_context(self._...".


def test_the_exception_line_leads_the_failure_text():
    from initialize_claude_ai.mcp_auto_discovery.probes import _last_exception_line

    traceback = [
        "Traceback (most recent call last):",
        '  File "server.py", line 91, in lifespan',
        "    user_lifespan_result = await stack.enter_async_context(self._x)",
        "RuntimeError: GITLAB_TOKEN is required",
    ]
    assert _last_exception_line(traceback) == "RuntimeError: GITLAB_TOKEN is required"


def test_plain_log_lines_have_no_exception_to_promote():
    from initialize_claude_ai.mcp_auto_discovery.probes import _last_exception_line

    assert _last_exception_line(["listening on stdio", "ready"]) == ""


def test_a_truncated_failure_still_names_the_variable():
    """The end-to-end property: shorten it hard, the evidence is still there."""
    from initialize_claude_ai.mcp_auto_discovery.probes import _last_exception_line

    lines = [
        '  File "server.py", line 91, in lifespan',
        "    user_lifespan_result = await stack.enter_async_context(self._x)",
        "RuntimeError: GITLAB_TOKEN is required",
    ]
    summary = _last_exception_line(lines)
    detail = " | ".join([summary, *[ln for ln in lines[-3:] if ln != summary]])
    assert "GITLAB_TOKEN" in detail[:80]


# -- and a prerequisite that was never a command -------------------------------


def test_prose_is_not_an_install_step():
    """ "prerequisite needs the, which is not installed" -- build.example.net.

    A sentence survives ``shlex.split`` as a well-formed argv whose word zero is
    an article. Sentence punctuation is the evidence, not a list of English
    words: a rule made of vocabulary would only ever be right about one language.
    """
    from initialize_claude_ai.mcp_auto_discovery.agentic import _install_steps

    steps, notes = _install_steps(["the package must be installed first, then run it."])
    assert steps == []
    assert any("sentence" in note for note in notes)


def test_real_install_commands_survive_the_prose_check():
    from initialize_claude_ai.mcp_auto_discovery.agentic import _install_steps

    given = [
        ["npm", "install", "-g", "@modelcontextprotocol/server-gitlab"],
        ["pip", "install", "."],
        ["uv", "tool", "install", "mcp-server"],
        ["/usr/bin/python3.11", "-m", "pip", "install", "mcp-gitlab"],
        ["zypper", "--non-interactive", "install", "git-core"],
    ]
    steps, notes = _install_steps(given)
    assert steps == given, notes


# -- the whole chain, on the strings gitlab.example.net actually produced ----------


def test_gitlab_example_net_end_to_end(ctx):
    """Every one of the three defects had to be fixed for this to work.

    The real traceback ran past forty lines, so the head-capped stderr buffer
    dropped the ``ValueError`` before anything could read it; ``failure()`` then
    took the tail of the head and reported a frame from the middle; and the
    credential rule, reading that frame, concluded the server was simply broken.

    The message below is the one `mcp_gitlab/config.py` raises -- found in the
    log only because the agent went and read the package's source, which is not
    a thing the deterministic pipeline can do.
    """
    from initialize_claude_ai.mcp_auto_discovery.probes import _last_exception_line

    stderr = [
        "Traceback (most recent call last):",
        *[f'  File "/usr/lib/python3.14/contextlib.py", line {n}, in run' for n in range(60)],
        "    user_lifespan_result = await stack.enter_async_context(self._lifespan(self))",
        "    ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^",
        "ValueError: GitLab token is required. Set one of: GITLAB_TOKEN, "
        "GITLAB_PAT, GITLAB_PERSONAL_ACCESS_TOKEN, or GITLAB_API_TOKEN",
    ]

    # 1. the tail survives the cap, so the exception is still in the buffer
    from collections import deque

    from initialize_claude_ai.mcp_auto_discovery.probes import _STDERR_LINES

    kept = deque(stderr, maxlen=_STDERR_LINES)
    summary = _last_exception_line(list(kept))
    assert summary.startswith("ValueError: GitLab token is required")

    # 2. it leads the failure text, so shortening cannot lose it
    assert "GITLAB_TOKEN" in f"process exited with rc=1: {summary}"[:90]

    # 3. and the rule reads it as the credential wall it is
    said = _verifier(ctx).refused_to_start_without(_crash(f"process exited with rc=1: {summary}"), PENDING)
    assert "GITLAB_TOKEN" in said
