"""Phase 4 in agentic mode -- four subagents, one per stage.

Each stage hands a Claude subagent a brief, a skill that spells out the method,
everything the earlier stages learned, and a workspace to write into.  The agent
searches, reasons and experiments; it reports back a JSON payload of candidate
MCP servers.

What it does *not* get to do is decide whether it succeeded.  Every candidate an
agent returns goes through :class:`~initialize_claude_ai.mcp_auto_discovery.verifier.CandidateVerifier`
exactly like a candidate the deterministic pipeline produced -- a real
``initialize`` handshake or it did not happen.  An agent that reports a working
server which does not answer produces a *failed* stage, not a passing one.

Three more boundaries are enforced here rather than trusted to the model:

* **The tier order is a gate, not advice.** The package and bridge stages are
  refused outright while the target has a native MCP endpoint, or while the
  tier-1 checklist has a step nobody performed -- see :mod:`.policy`. The agent
  is told the rule as well, but it is not the thing enforcing it, and neither is
  the fallback path.

* **No credential may cross this line.** Anything an agent returns is scrubbed:
  values under secret-looking names become references, references are checked
  against the known schemes, and a candidate with a credential baked into its
  command line is dropped outright.
* **No agent runs in a dry run.** Agents write files and execute packages; a
  verify-only run degrades to the traditional pipeline instead, per stage.

An agent that does not settle a stage on its first answer is not finished with
it.  Each stage is worked up to ``mcp.agent.max_attempts`` times
(``--agent-max-attempts``), and every
retry is *different* from the attempt before it: the agent is handed exactly why
the last answer failed -- which candidate did not complete a handshake and with
what error, which one was discarded before it was tried -- and told not to
repeat it.  When the failure was in the machinery rather than the answer, the
machinery changes too: a broken runtime is swapped for the other one, a request
refused before the model ever saw it is restated plainly and, if the operator
configured alternatives, sent to a different model.

**A retry has to be able to find something the last attempt did not.**  An
endpoint that answered 401 or 403 settles its stage even though no handshake
completed: the server was reached, it speaks for the target, it is recorded in
the tier-1 checklist and registered with its credential slot left empty, and
the only thing another attempt can discover is the same URL refusing the same
anonymous client.  Retrying that is not persistence, it is the same five to
eight minutes spent three times over and a deterministic re-sweep afterwards,
all ending in the entry the first attempt already had.  Set
``mcp.agent.credential_wall_settles_stage = false`` to spend the attempts
anyway, looking for an endpoint that needs no credential.

**Retries shrink the prompt; they do not grow it.**  The brief is built at one
of three registers -- ``full``, ``plain``, ``minimal`` (:data:`REGISTERS`) --
and a failure the model never got to read moves the next attempt one register
*down*.  This is the opposite of the obvious design, and it is deliberate.  A
request rejected before a model saw it, or one that ran out of clock, is not
improved by appending a paragraph explaining itself: whatever in the brief
caused the ending is still in there, now with more text around it, and a
three-attempt loop then produces the same ending three times.  Descending the
ladder removes material instead -- the pasted findings become a path to a file
the agent can read for itself, the mandatory-path enumeration becomes a
sentence, the tier lecture and the skill reference fall away -- until what is
left is the target, the question, and the shape of the answer.  A stage that
still cannot be worked at ``minimal`` is a stage the deterministic pipeline
should finish, and the report says which register it got to before it gave up.

Only once every attempt is spent does the stage fall through to
:class:`~initialize_claude_ai.mcp_auto_discovery.traditional.TraditionalPipeline`.  That path still
exists -- an operator who asks for traditional mode gets it, and a host with no
runtime at all still produces a deterministic result -- but it is a last resort,
not the agent's first excuse.  Set ``mcp.agent.fallback_to_traditional = false``
to remove even that, and an exhausted agent stage reports its own failure.
"""

from __future__ import annotations

import json
import re
import shlex
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from ..core import redact, trust_store
from ..core.config import fill, truthy
from ..core.context import RunContext
from ..core.fileio import write_generated
from ..core.logging_setup import get_logger
from ..core.report import Status
from ..core.trust_store import ANCHORED, UNTRUSTED, TrustStore
from . import agent_credentials, api_kinds, login_check
from .address import parse as parse_address
from .grounding import assess as ground
from .grounding import endpoint_hosts, hosting_namespaces_of, identity_rule_of
from .policy import CHECKLIST, DueDiligence, TierPolicy
from .runtime import (
    FAILURE_PATTERNS,
    OTHER,
    REFUSAL,
    TIMEOUT,
    TRANSIENT,
    UNSUPPORTED_MODEL,
    UNUSABLE,
    AgentDefinitionSpec,
    AgentResult,
    AgentRuntime,
    available_runtimes,
    failure_kind,
    json_message,
    workspace_for,
)
from .secret_policy import (
    SAFE_REFERENCE_SCHEMES,
    carries_inline_credential,
    holds_a_credential,
    is_secret_name,
    names_a_selector,
)
from .secret_policy import looks_like_a_value as _looks_like_a_value
from .traditional import TraditionalPipeline
from .types import (
    TRANSPORT_ALIASES,
    Candidate,
    Mode,
    Stage,
    StageOutcome,
    TargetOutcome,
    clip,
    normalize_transport,
    slugify,
    waiting_remedy,
    waits_for_credential,
)
from .verifier import REUSED_VERDICT, CandidateVerifier

log = get_logger("mcp.agentic")

# What counts as a credential is shared with the deterministic pipeline --
# see secret_policy, imported above and re-exported here. It used to live in
# this module, which meant an agent's claims were checked while a package
# registry's were taken on trust; one rule cannot have two strengths.

#: How much brief the agent is given, from most to least. Each step down drops
#: material rather than rewording it -- see the module docstring for why a
#: retry after an unread failure has to subtract.
FULL, PLAIN, MINIMAL = "full", "plain", "minimal"

#: The ladder, in the order it is descended. The last rung is the floor: a
#: stage already at ``minimal`` stays there rather than wrapping round.
REGISTERS: tuple[str, ...] = (FULL, PLAIN, MINIMAL)

#: What each rung is for, in the words the console and the report use.
REGISTER_DETAIL: dict[str, str] = {
    FULL: "the full brief",
    PLAIN: "a shorter brief, with the earlier findings left on disk to read",
    MINIMAL: "the bare question: target, what to establish, and the answer shape",
}


def _next_register(current: str) -> str:
    """One rung down the ladder, or the floor if there is none left."""
    try:
        index = REGISTERS.index(current)
    except ValueError:
        return REGISTERS[0]
    return REGISTERS[min(index + 1, len(REGISTERS) - 1)]


@dataclass
class _RetryPlan:
    """What the next attempt at a stage will do differently from the last one.

    One object threaded through the attempt loop, rather than a growing
    parameter list: every remedy a failure class can call for -- reword, shrink,
    swap the model, cut the turn budget -- is a field here, and
    :meth:`AgenticPipeline._change_approach` is the only thing that sets them.
    """

    #: Which rung of :data:`REGISTERS` the next brief is written at.
    register: str = FULL
    #: What went wrong with the last *answer*, in the agent's own terms.
    correction: str = ""
    #: The last attempt never reached a model. Changes how the brief explains
    #: itself, and is why the register descends.
    refused: bool = False
    #: Multiplier on the turn budget: a stage that ran out of wall clock gets a
    #: smaller one, so the next attempt aims at an answer it can finish.
    turn_scale: float = 1.0
    #: One line per change already made, for the report.
    changes: list[str] = field(default_factory=list)
    #: Nothing is left to change: refused again after the plain wording, with
    #: no shorter brief and no other model. Another attempt would be the same
    #: request, refused the same way (mcp.agent.stop_when_nothing_changes).
    exhausted: bool = False

    def descend(self) -> bool:
        """Drop a register. False when already at the floor."""
        nxt = _next_register(self.register)
        if nxt == self.register:
            return False
        self.register = nxt
        return True


class AgenticPipeline:
    """Four-stage discovery driven by Claude, verified deterministically."""

    mode = Mode.AGENTIC

    def __init__(self, ctx: RunContext, runtime: AgentRuntime | None = None) -> None:
        self.ctx = ctx
        self.settings = ctx.settings
        self.console = ctx.console
        self.config = ctx.settings.section("mcp.agent")
        if runtime is not None:
            self._runtimes: list[AgentRuntime] = [runtime]
            self.runtime_reason = f"{runtime.name} runtime"
        else:
            self._runtimes, self.runtime_reason = available_runtimes(ctx)
        #: Guards the state that outlives one target -- the spend, which
        #: runtimes have already thrown, what a rotation last moved to, the
        #: workspace. Uncontended and therefore free when nothing is concurrent.
        self._lock = threading.Lock()
        #: Which runtime and model *this* address is on, and which models it
        #: has already been moved off. Per thread, because under --concurrency
        #: one pipeline works several addresses at once, and a retry that
        #: changes approach for one of them must not change it for the others
        #: mid-request. One address at a time there is one thread, and this
        #: behaves exactly like the plain attributes it replaced.
        self._local = threading.local()
        #: A runtime that has thrown is a fact about the installation, so it is
        #: shared: no address moves back onto it.
        self._runtimes_tried = {r.name for r in self._runtimes[:1]}
        #: What the last rotation moved to. Each new address *starts* there --
        #: the behaviour a run of one address at a time always had -- while its
        #: own rotation stays its own.
        self._learned_runtime: AgentRuntime | None = None
        self._learned_model = ""
        #: Retry models this deployment said it cannot serve at all.
        self._models_unusable: set[str] = set()
        self.verifier = CandidateVerifier(ctx)
        self.fallback = TraditionalPipeline(ctx)
        self.policy = TierPolicy(ctx)
        self.spent_usd = 0.0
        self._workspace: Path | None = None
        #: Set by the phase when targets run concurrently. See the budget check
        #: in :meth:`_agent_attempts`.
        self.budget_checked_at_admission = False
        #: Why the agent runtime cannot serve *any* request, once something has
        #: proved it -- an unavailable model, a rejected credential. Latched for
        #: the run because it is a fact about the installation, not about a
        #: request, and re-asking costs minutes per stage per target to be told
        #: the same thing. See :meth:`available`.
        self._runtime_unusable = ""
        #: What the operator runs to fix it, when a credential-failure profile
        #: (``mcp.agent.credential_failures``) recognised the failure.
        self.runtime_remedy = ""
        #: Whether any agent turn came back with an answer before the latch --
        #: "the agent never ran" and "the agent stopped part-way" are different
        #: runs, and the verdict must not call the second one the first.
        self.agent_answered = False
        #: Address -> the stages that would have been worked by an agent but
        #: were not, because the runtime had been found unusable. The final
        #: verdict turns this into a re-run command for exactly those addresses.
        self.agent_unavailable: dict[str, list[str]] = {}

    # -- per-address state -----------------------------------------------------

    def _state(self) -> threading.local:
        """This thread's runtime choice and tried models, made on first use."""
        state = self._local
        if not hasattr(state, "runtime"):
            with self._lock:
                start = self._learned_runtime
                model = self._learned_model
            if start is None or start not in self._runtimes:
                start = self._runtimes[0] if self._runtimes else None
            state.runtime = start
            state.models_tried = set()
            for runtime in self._runtimes:
                runtime.model_override = model
        return state

    @property
    def runtime(self) -> AgentRuntime | None:
        """The runtime the calling thread's address is being worked on."""
        return self._state().runtime

    @runtime.setter
    def runtime(self, value: AgentRuntime | None) -> None:
        self._state().runtime = value

    @property
    def _models_tried(self) -> set[str]:
        """Models the calling thread's address has already been moved off."""
        return self._state().models_tried

    @_models_tried.setter
    def _models_tried(self, value: set[str]) -> None:
        self._state().models_tried = set(value)

    def _begin_target(self) -> None:
        """A fresh start for the address about to be worked on this thread.

        Pool threads are reused, so without this the second address a worker
        picked up would inherit the first one's tried models and runtime.
        """
        for name in ("runtime", "models_tried"):
            self._local.__dict__.pop(name, None)
        self._state()

    # -- availability ----------------------------------------------------------

    def _failure_kind(self, detail: str) -> str:
        """Classify an agent failure, with the operator's extra wordings.

        A credential the model cannot load is a fact about the installation,
        so every configured credential-failure wording counts as "unusable"
        too -- that is what stops each stage retrying it three times.
        """
        return failure_kind(
            detail,
            [
                *self.settings.str_list("mcp.agent.unusable_markers", []),
                *agent_credentials.markers(self.settings),
            ],
            self.settings.dict_("mcp.agent.failure_patterns", {k: list(v) for k, v in FAILURE_PATTERNS.items()}),
        )

    def _workspace_problem(self) -> str:
        """Why the agent's workspace cannot be prepared, once for the run; "" when it can.

        A full disk failed the skills copy for every address, each time with a
        traceback, and each stage retried it. Preparing the workspace is not a
        stage's problem: it is checked here, and when it fails the run's agents
        are latched off with that reason -- once -- and the deterministic
        pipeline does the work.
        """
        import shutil as _shutil

        from ..core import diskspace

        if self._runtime_unusable:
            return self._runtime_unusable
        try:
            self.prepare_workspace()
        except (OSError, _shutil.Error) as exc:
            reason = (
                "no space left on device to prepare the agent workspace"
                if diskspace.is_out_of_space(exc, self.ctx.settings)
                else f"the agent workspace could not be prepared: {_one_line(str(exc))}"
            )
            with self._lock:
                if not self._runtime_unusable:
                    self._runtime_unusable = reason
                    log.warning("%s; the rest of this run uses the deterministic pipeline", reason)
            if diskspace.is_out_of_space(exc, self.ctx.settings):
                self.ctx.state.setdefault("out_of_space", reason)
            return self._runtime_unusable
        return ""

    def _unusable_reason(self) -> str:
        return f"the agent runtime cannot serve any request: {self._runtime_unusable}"

    def _note_unavailable(self, target: TargetOutcome, name: str, stage: StageOutcome) -> None:
        """Remember that ``name`` for ``target`` should have had an agent and did not."""
        stage.findings["agent_unavailable"] = self._runtime_unusable
        with self._lock:
            stages = self.agent_unavailable.setdefault(target.target, [])
            if name not in stages:
                stages.append(name)

    def _credential_retry(self, name: str, detail: str) -> tuple[bool, agent_credentials.CredentialProfile | None]:
        """``(retry, profile)`` for a failure a credential-failure profile recognises.

        The refresh is tried once per profile per run, by whichever stage met
        the failure first. Only that stage retries -- without spending an
        attempt, because the attempt proved nothing about the stage -- and
        only if the refresh worked. Everybody else latches: a refresh that
        failed, or one that worked and was refused again, cannot be helped by
        asking a third time.
        """
        profile = agent_credentials.profile_for(self.settings, detail)
        if profile is None:
            return False, None
        first, ok, why = agent_credentials.Refresher.for_run(self.ctx).refresh(profile)
        if first and ok:
            self.console.bullet(
                f"{name}: the agent's {profile.name} credential was refused; refreshed it "
                "non-interactively, retrying without spending an attempt",
                indent=4,
            )
            return True, profile
        if first:
            log.warning("could not refresh the agent's %s credential: %s", profile.name, why)
        return False, profile

    def available(self) -> tuple[bool, str]:
        """Can agents actually be run right now?"""
        if not self.settings.bool_("mcp.agentic", True):
            # The operator's own decision, so it is checked before the
            # circumstantial reasons: --no-agent is an answer, not an obstacle.
            return False, "--no-agent: agents are turned off for this run"
        if self.ctx.dry_run:
            return False, "dry run: agents write files and execute packages, so they are not invoked"
        if self.runtime is None:
            return False, self.runtime_reason
        if self._runtime_unusable:
            # Learned once, honoured for the rest of the run. The first stage
            # that asked got a failure about the *installation*, and asking
            # again cannot change the answer -- it can only spend the same
            # minutes per stage, for every remaining target, and report the
            # same sentence again. Thirteen targets did exactly that.
            return False, self._unusable_reason()
        return True, ""

    # -- entry point -----------------------------------------------------------

    def run(self, target: str, stages: Sequence[str]) -> TargetOutcome:
        """Work one target through the stages, agent first, gate always.

        Each stage is gated by tier policy before an agent is launched, worked
        until it is settled, and then absorbed into the target's outcome --
        verified servers and credential-walled ones kept apart. The loop stops
        as soon as the target has an answer -- a working server, or one that
        proved it exists by demanding a credential -- unless ``--exhaustive-mcp``
        asked for everything. See
        :meth:`~.traditional.TraditionalDiscovery.descent_settled`.
        """
        outcome = TargetOutcome(target=target, base_url=parse_address(target).url)
        exhaustive = self.settings.bool_("mcp.exhaustive", False)
        self._begin_target()
        usable, why = self.available()
        if not usable:
            log.info("agentic mode unavailable (%s); running the deterministic pipeline", why)

        for name in stages:
            self._work(name, outcome, exhaustive, usable, why)

        # An agent can leave the same contradiction behind as the deterministic
        # pipeline -- reporting an endpoint in its findings, which closes the
        # lower tiers, without ever offering it as a candidate to verify. The
        # check is shared for exactly that reason.
        self.fallback.reconcile(outcome, exhaustive)
        # ...and so is the last safety net: a stage refused because of an
        # endpoint that has since been ruled out gets its turn, agent first.
        reopened = self.fallback.reopenable(outcome)
        for name in reopened:
            self.fallback.withdraw_refusal(outcome, name)
            self._work(name, outcome, exhaustive, usable, why, reopened=True)
        if reopened:
            self.fallback.reconcile(outcome, exhaustive)
        return outcome

    def reopen_withdrawn(self, outcome: TargetOutcome) -> list[str]:
        """The stages owed their turn after a withdrawal, worked agent first (:meth:`.TraditionalDiscovery.reopen_withdrawn`)."""
        exhaustive = self.settings.bool_("mcp.exhaustive", False)
        names = self.fallback.reopenable_after_withdrawal(outcome, exhaustive)
        if not names:
            return []
        self._begin_target()
        usable, why = self.available()
        for name in names:
            self.fallback.withdraw_settled(outcome, name)
            self._work(name, outcome, exhaustive, usable, why, reopened=self.fallback.REOPENED_BY_WITHDRAWAL)
        self.fallback.reconcile(outcome, exhaustive)
        return names

    def _work(
        self,
        name: str,
        outcome: TargetOutcome,
        exhaustive: bool,
        usable: bool,
        why: str,
        *,
        reopened: bool | str = False,
    ) -> None:
        """One stage of :meth:`run`: gate, work, check, absorb, vet."""
        target = outcome.target
        # What every stage does first, in either mode (the job learned before
        # anything is judged -- the fallback stage below had skipped it).
        self.fallback.begin_stage(name, outcome)
        # One descent rule for both pipelines, living on the deterministic
        # one: an agent that stops at a different point from the fallback is
        # a second policy nobody wrote down.
        blocked = self.gate(name, outcome) or self.fallback.settled(name, outcome, exhaustive, mode=self.mode)
        if blocked is not None:
            # What this machine already registered is judged even so.
            outcome.stages.append(self.fallback.recheck_blocked(name, outcome, blocked, exhaustive))
            return
        # A subagent turn is the longest quiet stretch in the whole run, and
        # the one place where a reader is most likely to assume a hang.
        self.console.progress(
            f"{target} :: {name:<14}",
            f"tier {self.policy.tier(name)}, {'agent' if usable else 'fallback'}",
        )
        started = time.monotonic()
        try:
            # Registrations this machine already has are tried first, whichever
            # pipeline works the stage: a proven one settles tiers 2 and 3
            # without an agent, and anything else is handed to the agent's brief.
            stage = self.fallback.reuse(name, outcome, exhaustive) or (
                self.fallback.recheck_stage(name)
                if self.fallback.rechecking
                else self._run_stage(name, outcome, agentic=usable, unavailable=why)
            )
            self.fallback.merge_reused(stage, outcome)
        except Exception as exc:  # noqa: BLE001 - a stage must not kill the phase
            log.exception("agentic stage %s failed", name)
            stage = self._after_crash(name, outcome, exc, agentic=usable)
        stage.duration = time.monotonic() - started
        # The same guard the deterministic loop applies, and for the same
        # reason -- but it has to be applied *here* too, which it was not.
        # `_drop_ungrounded` filters what an *agent* proposes; a stage that
        # falls back calls `fallback.stage_*` directly, so its candidates
        # met neither check. That is how `@ateam-ai/mcp` -- an npm search
        # hit unrelated to Open Build Service, naming no address -- settled
        # the package stage for build.opensuse.org, closed the bridge and
        # strategy stages beneath it, and was then refused at registration,
        # leaving the address with nothing at all.
        # ...and the entry has to start. Asked here, while the agent can
        # still fix it and while the tiers below are still open -- which is
        # the whole reason this moved out of publish. An agent gets first
        # refusal -- the strategist, briefed with the entry, the failure and
        # what discovery knew; if it has nothing, the deterministic repairs
        # run, and if those fail too the candidate is dropped and the next
        # candidate, or the next tier, is tried. The same steps as the
        # deterministic loop, from the one place both modes call.
        self.fallback.finish_stage(
            stage,
            outcome,
            exhaustive,
            reopened=reopened,
            agent_repair=self._agent_repair if usable else None,
        )

    def _after_crash(self, name: str, target: TargetOutcome, exc: Exception, *, agentic: bool) -> StageOutcome:
        """What a stage that raised becomes: the deterministic stage, if it can run.

        An exception in the agent's machinery -- a workspace that could not be
        prepared, a runtime that threw -- says nothing about the target, so it
        is handled like an agent loop that ran out of attempts: the
        deterministic pipeline does the stage. Recording FAILED instead left
        the endpoint stage without its record, and the tier policy then
        refused every tier beneath it with "the endpoint stage has not run" --
        seven addresses of one run ended with nothing at all that way. Only if
        the deterministic stage raises too, or was what raised, is it FAILED.
        """
        crashed = StageOutcome(
            stage=name,
            mode=self.mode,
            status=Status.FAILED,
            detail=f"unexpected error: {exc}",
            error=type(exc).__name__,
        )
        if not (agentic and self._fallback_enabled()):
            return crashed
        try:
            fallback = self._delegate(name, target)
        except Exception:  # noqa: BLE001 - the report says what happened
            log.exception("the deterministic %s stage failed as well", name)
            return crashed
        why = f"the agent stage raised {type(exc).__name__}: {_one_line(str(exc), 200)}"
        crashed.error = ""
        merged = _merge(crashed, fallback, why)
        merged.findings["agent_crashed"] = why
        return merged

    def gate(self, name: str, target: TargetOutcome) -> StageOutcome | None:
        """The tier policy, enforced before an agent is ever launched.

        An agent that is told not to install packages when the target has its
        own endpoint will usually comply, and "usually" is not a guarantee worth
        paying for: the cheapest way to stop a forbidden tier is not to spend a
        turn on it. The same check runs in the deterministic pipeline, so a
        fallback cannot route around it either.
        """
        allowed, why = self.policy.gate(name, target.due_diligence)
        if allowed:
            return None
        log.info("tier policy blocked the %s stage: %s", name, why)
        stage = StageOutcome(stage=name, mode=self.mode, status=Status.SKIPPED, detail=why)
        stage.findings["blocked_by_policy"] = why
        stage.findings["tier"] = self.policy.tier(name)
        stage.add_remediation(*self.policy.remediation(name, target.due_diligence))
        return stage

    def _run_stage(self, name: str, target: TargetOutcome, *, agentic: bool, unavailable: str) -> StageOutcome:
        """One stage: the agent works at it until it is out of ways to try.

        The deterministic pipeline runs only when agents cannot run at all, when
        no agent owns this stage, or when every attempt has been spent -- never
        as the answer to a single disappointing reply.
        """
        reason = unavailable
        if self._runtime_unusable and (agentic or unavailable == self._unusable_reason()):
            # Learned after this address started -- by an earlier stage of it,
            # or by another address running beside it. Asked per stage, not
            # once per address, so nobody spends another attempt learning it.
            agentic = False
            reason = unavailable = self._unusable_reason()
            stage = self._delegate(name, target)
            stage.findings.setdefault("agentic_skipped", reason)
            self._note_unavailable(target, name, stage)
            return stage
        if agentic and self._workspace_problem():
            agentic = False
            reason = unavailable = self._unusable_reason()
            stage = self._delegate(name, target)
            stage.findings.setdefault("agentic_skipped", reason)
            self._note_unavailable(target, name, stage)
            return stage
        if agentic:
            over_budget = self._over_budget()
            if over_budget:
                reason = over_budget
            else:
                stage, reason = self._agent_attempts(name, target)
                if stage is not None:
                    if self._stage_is_conclusive(stage, target):
                        if self._fallback_enabled() and self.fallback.owes_generated_bridge(name, target, stage):
                            # The agent's answer proved no access, and the run
                            # can build the documented bridge itself: tried
                            # beside it, ranked by proof (see owes_generated_bridge).
                            kinds = ", ".join(self.fallback.untried_bridges(target, [stage]))
                            return _merge(
                                stage,
                                self._delegate(name, target),
                                (f"the bridge generated from the documented operations of {kinds} was tried as well"),
                                settled=True,
                            )
                        return stage
                    if not self._fallback_enabled():
                        latched = stage
                    else:
                        # Keep what the agent did learn; the fallback appends to it.
                        latched = _merge(stage, self._delegate(name, target), reason)
                    if stage.findings.get("agent_failure_kind") == UNUSABLE and self._runtime_unusable:
                        self._note_unavailable(target, name, latched)
                    return latched

        stage = self._delegate(name, target)
        if reason:
            stage.findings.setdefault("agentic_skipped", reason)
            # Only a stage-specific reason earns a line under "Next steps". When
            # agents cannot run at all -- a dry run, no runtime -- the reason is
            # the same sentence for every stage, the phase has already said it
            # once in its "discovery mode" step, and the [traditional] tag on
            # this very line says it again; a fourth copy per stage would bury
            # the remediations that are actually about this stage.
            if reason != unavailable:
                stage.add_remediation(f"Ran the {name} stage deterministically: {reason}")
        return stage

    def _stage_is_conclusive(self, stage: StageOutcome, target: TargetOutcome | None = None) -> bool:
        """A stage stands on its own only when the agent proved something.

        Proving something is not the same as producing a working server. An
        endpoint that answered 401 or 403 was *reached*, it speaks for the
        target, and it is registered with its credential slot empty -- so the
        stage has its answer, and another agent attempt can only find the same
        URL and be refused by it again. The default three attempts at five to eight minutes
        each, then a full deterministic re-sweep, all ending in the entry the
        first attempt already had, is what this avoids.

        ``mcp.agent.credential_wall_settles_stage = false`` restores the older
        behaviour for an operator who would rather spend the attempts looking
        for an endpoint that needs no credential at all.
        """
        unproved = (
            (lambda c: self.fallback.unproved_with_alternative(c, target)) if target is not None else (lambda c: False)
        )
        if any(not unproved(c) for c in stage.verified):
            return True
        if target is not None:
            # Said, so the retry can fix it rather than start over: smelt's
            # verified bridge was retried twice with no word of why.
            for candidate in stage.verified:
                candidate.unsettled = self.fallback.why_unsettled(candidate, target)
        # A server whose access is unproved while the target serves another API
        # (see TraditionalPipeline.unproved_with_alternative) settles nothing either.
        # A server whose account has no API access answered nothing usable:
        # the credential is valid, the API is not one this account may use.
        # See login_check.keeps_searching.
        waiting = [
            c for c in stage.provisional if not login_check.keeps_searching(c, self.settings) and not unproved(c)
        ]
        if not waiting:
            return False
        return self.config.bool_("credential_wall_settles_stage", True)

    def _fallback_enabled(self) -> bool:
        return self.config.bool_("fallback_to_traditional", True)

    def _delegate(self, name: str, target: TargetOutcome) -> StageOutcome:
        handlers: dict[str, Callable[[TargetOutcome], StageOutcome]] = {
            Stage.ENDPOINT: self.fallback.stage_endpoint,
            Stage.PACKAGE: self.fallback.stage_package,
            Stage.BRIDGE: self.fallback.stage_bridge,
            Stage.STRATEGY: self.fallback.stage_strategy,
        }
        handler = handlers.get(name)
        if handler is None:
            return StageOutcome(stage=name, mode=self.mode, detail=f"unknown stage {name!r}")
        return handler(target)

    def _over_budget(self) -> str:
        cap = self.config.float_("max_budget_usd", 0.0)
        with self._lock:
            spent = self.spent_usd
        if cap > 0 and spent >= cap:
            return f"agent budget of ${cap:.2f} reached (spent ${spent:.2f})"
        return ""

    # -- working a stage until it is settled -----------------------------------

    def _agent_attempts(self, name: str, target: TargetOutcome) -> tuple[StageOutcome | None, str]:
        """Work one stage with the agent until it succeeds or runs out of ways.

        Returns ``(stage, reason)``: the furthest-progressed attempt, and why it
        is still not conclusive. ``(None, reason)`` means no agent owns this
        stage at all, which is the one case where there was nothing to attempt.

        The point of the loop is that the attempts differ. A first answer that
        did not verify is usually not a dead end but a correctable mistake -- a
        package name that does not exist, a transport the server does not speak,
        a launch command that needed a shell -- and the agent is the thing best
        placed to correct it, provided somebody tells it what happened.
        :func:`_correction` is that telling. Failures in the machinery rather
        than the answer are handled by :meth:`_change_approach` instead, because
        no amount of rewording fixes a runtime that will not start.
        """
        attempts = max(1, self.config.int_("max_attempts", 3))
        best: StageOutcome | None = None
        history: list[str] = []
        reason = ""
        kind = ""
        plan = _RetryPlan(register=self._starting_register())

        free_retries = max(0, self.config.int_("transient_free_retries", 3))
        attempt = 0
        while attempt < attempts:
            attempt += 1
            stage = self._agent_stage(name, target, attempt=attempt, attempts=attempts, plan=plan)
            if stage is None:
                return None, f"no agent is configured for the {name} stage"
            if stage.error != "agent-failed":
                self.agent_answered = True

            # A service that was unavailable said nothing about this stage, so
            # charging an attempt for it spends the agent's budget on our own
            # bad luck -- and under --concurrency the bad luck is partly
            # self-inflicted, because N sessions at once is what provokes the
            # rate limit. The agent must exhaust its own loop, not a loop that
            # rate limiting exhausted for it.
            if stage.error == "agent-failed" and self._failure_kind(stage.detail) == TRANSIENT and free_retries > 0:
                free_retries -= 1
                attempt -= 1
                self.console.bullet(
                    f"{name}: the service was unavailable; retrying without spending an "
                    f"attempt ({free_retries} free retr{'y' if free_retries == 1 else 'ies'} left)",
                    indent=4,
                )
                log.info("transient failure on the %s stage; not counting the attempt", name)
                continue

            # One line per attempt in the log: which model, which brief, what came of it.
            # gitlab.example.net's three refusals all named "Opus 5", and nothing showed
            # whether another model was ever tried.
            log.info(
                "%s: %s agent attempt %d of %d -- model %s, %s brief: %s",
                target.target,
                name,
                attempt,
                attempts,
                (self.runtime.model() if self.runtime else "") or "the runtime's default",
                plan.register,
                _one_line(f"{stage.status}: {stage.detail or 'no detail'}")[:200],
            )
            best = _best_of(best, stage)
            best.findings["attempts"] = attempt
            if history:
                best.findings["attempt_history"] = list(history)
            if self._stage_is_conclusive(stage, target):
                self._record_plan(best, plan)
                return best, ""

            reason = stage.detail or "the agent produced nothing usable"
            kind = self._failure_kind(stage.detail) if stage.error == "agent-failed" else ""
            on_retry_model = bool(self.runtime and self.runtime.model_override)
            retry, credential = self._credential_retry(name, stage.detail) if kind == UNUSABLE else (False, None)
            if retry:
                attempt -= 1
                continue
            if kind == UNUSABLE and (credential is not None or not on_retry_model):
                # Not a failure of this request, so a different request cannot
                # help: the model is not enabled, or the credential is refused,
                # for the whole installation. Retrying spends minutes to be told
                # the same thing, and every later target would spend them again.
                # (On a *retry* model it proves only that that model is not
                # served; _change_approach leaves it and the loop goes on.)
                # A credential is not model-specific, so a retry model does
                # not exempt it: the next model would load the same one.
                with self._lock:
                    self._runtime_unusable = _one_line(reason)
                    if credential is not None and not self.runtime_remedy:
                        self.runtime_remedy = credential.remedy
                history.append(f"attempt {attempt} ({plan.register}): {_one_line(reason)}")
                log.warning(
                    "the agent runtime cannot serve any request (%s); the rest of this run "
                    "uses the deterministic pipeline",
                    self._runtime_unusable,
                )
                break
            history.append(f"attempt {attempt} ({plan.register}): {_one_line(reason)}")
            if attempt >= attempts:
                break
            # Deliberately not checked when targets run concurrently: with N in
            # flight, another target's spend could cut this stage's agent loop
            # short, and "the agent owns the stage until it settles it" would
            # become "until somebody else spent the money". Under concurrency
            # the cap is applied when a target is admitted instead -- a target
            # either gets the whole agentic treatment or is not started.
            spent = "" if self.budget_checked_at_admission else self._over_budget()
            if spent:
                reason = spent
                break

            record = target.due_diligence
            changing = self._change_approach(
                stage,
                plan,
                attempt,
                ruled_out=record.ruled_out if isinstance(record, DueDiligence) else None,
            )
            self.console.bullet(
                f"{name}: attempt {attempt} of {attempts} did not settle it ({_one_line(reason)}); {changing}",
                indent=4,
            )
            if plan.exhausted:
                history.append("stopped: refused again with nothing left to change (no shorter brief, no other model)")
                log.info(
                    "%s: the %s agent was refused again and nothing is left to change -- no shorter brief and "
                    "no other model (mcp.agent.retry_models); the deterministic pipeline finishes the stage",
                    target.target,
                    name,
                )
                break
            # debug, not info: the console bullet above already said this, and an
            # INFO line would print the same sentence twice, one under the other.
            log.debug("retrying the %s agent: attempt %d of %d", name, attempt + 1, attempts)

        if best is not None:
            best.findings["attempts"] = len(history)
            if len(history) > 1:
                best.findings["attempt_history"] = history
            self._record_plan(best, plan)
            if kind:
                best.findings["agent_failure_kind"] = kind
                reason = f"{_failure_summary(kind)}: {reason}"
            reason = f"{reason}, over {len(history)} agent attempt{'s' if len(history) != 1 else ''}"
        return best, reason

    def _starting_register(self) -> str:
        """The rung the first attempt is written at.

        Configurable because the right starting point is a property of the
        deployment, not of this program: an operator whose endpoint keeps
        rejecting the full brief can start everything at ``plain`` and stop
        paying for the attempt that teaches the loop that.
        """
        configured = self.config.str_("prompt_register", FULL).strip().lower()
        return configured if configured in REGISTERS else FULL

    @staticmethod
    def _record_plan(stage: StageOutcome, plan: _RetryPlan) -> None:
        """Leave the retry ladder in the findings, so the report can show it."""
        stage.findings["prompt_register"] = plan.register
        if plan.changes:
            stage.findings["retry_changes"] = list(plan.changes)

    def _change_approach(
        self,
        stage: StageOutcome,
        plan: _RetryPlan,
        attempt: int = 1,
        ruled_out: Mapping[str, str] | None = None,
    ) -> str:
        """Make the next attempt differ from the one that just failed.

        Returns what a reader should expect to be different, for the console
        line that announces the retry, and records the same thing on ``plan``.

        The four failure classes get four different remedies, because they fail
        for four different reasons. An answer that did not verify is corrected.
        A request no model ever read is *shortened* -- and, if the operator
        configured or the run can derive one, sent to a different model. A stage
        that ran out of clock is narrowed and given a smaller turn budget, since
        repeating a brief that did not fit in fifteen minutes buys another
        fifteen minutes of the same. Anything else is machinery, and the other
        runtime is the cheapest thing to change about machinery.
        """
        if stage.error != "agent-failed":
            # The agent ran and answered; the answer is what was wrong. The
            # change is the correction it is about to be handed, not the wiring.
            plan.correction = _correction(stage, attempt, ruled_out, self.settings)
            plan.refused = False
            change = "retrying with what went wrong"
            plan.changes.append(change)
            return change

        plan.correction = ""
        kind = self._failure_kind(stage.detail)
        if kind == UNSUPPORTED_MODEL:
            # The runtime's own Claude Code is too old for the model: another
            # runtime may serve it as configured; else this one's default does.
            plan.refused = False
            model = self.runtime.model() if self.runtime else ""
            before = self.runtime.name if self.runtime else "agent"
            runtime = self._rotate_runtime()
            if runtime:
                change = f"switching to the {runtime} runtime ({before} cannot serve {model or 'the configured model'})"
            else:
                if self.runtime is not None:
                    self.runtime.configured_model_unsupported = True
                change = f"retrying on the {before} runtime's default model ({model or 'the configured model'} is not supported by it)"
            stage.add_remediation(
                f"The {before} agent runtime cannot serve {model or 'the configured model'} "
                f"({_one_line(stage.detail)[:160]}). Update it (its bundled Claude Code), or set mcp.agent.model "
                "to a model it serves."
            )
        elif kind == UNUSABLE:
            # Reached only on a retry model (see _agent_attempts): the model the
            # run was configured with is not in question, this one is.
            plan.refused = False
            dropped = self._drop_unusable_model()
            model = self.runtime.model() if self.runtime else ""
            change = (
                f"leaving {dropped}, which this deployment does not serve, for {model or 'the configured model'}"
                if dropped
                else "retrying"
            )
        elif kind == REFUSAL:
            already_plain = plan.refused
            plan.refused = True
            shorter = plan.descend()
            model = self._rotate_model()
            if already_plain and not shorter and not model and self.config.bool_("stop_when_nothing_changes", True):
                plan.exhausted = True
            parts = ["describing the work plainly"]
            if shorter:
                parts.append(f"cutting the brief down to {REGISTER_DETAIL[plan.register]}")
            if model:
                parts.append(f"trying {model}")
            change = ", ".join(parts)
        elif kind == TIMEOUT:
            plan.refused = False
            scale = self.config.float_("timeout_turn_scale", 0.5)
            plan.turn_scale = max(0.1, plan.turn_scale * (scale if 0 < scale < 1 else 0.5))
            plan.descend()
            change = (
                f"narrowing the brief to {REGISTER_DETAIL[plan.register]} and cutting the turn "
                "budget so the next attempt finishes inside the clock"
            )
        elif kind == TRANSIENT:
            plan.refused = False
            change = "retrying the same request"
        else:
            plan.refused = False
            runtime = self._rotate_runtime()
            change = f"switching to the {runtime} runtime" if runtime else "retrying"
        plan.changes.append(change)
        return change

    def _rotate_runtime(self) -> str:
        """Move to a runtime that has not failed yet. Returns its name, or ''.

        A runtime that has already thrown is the least likely of the two to work
        the second time, and the other one drives the same models over the same
        protocol -- so this is a cheaper change of approach than giving up.
        """
        current = self.runtime
        with self._lock:
            candidates = [r for r in self._runtimes if r.name not in self._runtimes_tried]
            if candidates:
                runtime = candidates[0]
                self._runtimes_tried.add(runtime.name)
                self._learned_runtime = runtime
            elif self._learned_runtime is not None and self._learned_runtime is not current:
                # Another address already moved off the runtime this one is on.
                # That move is shared knowledge; the runtime it moved to is
                # simply the next thing for this address to try.
                runtime = self._learned_runtime
            else:
                return ""
            self.runtime_reason = f"{runtime.name} runtime"
        runtime.model_override = current.model_override if current else ""
        self.runtime = runtime
        log.debug("switching to the %s runtime after a failure", runtime.name)
        return runtime.name

    def _rotate_model(self) -> str:
        """Move to the next model a refused request may be sent to. '' if none.

        A request rejected before the model saw it names two remedies: different
        words, or a different model. This is the second; the first is the
        shorter prompt the next attempt gets anyway.

        ``mcp.agent.retry_models`` is the operator's own list and wins. It is
        empty by default, because writing a model name into this program would
        pin it to whatever was current the day it was written -- so when it is
        empty the list is *derived* from the models this run already resolved
        for the tiers in phase 3 (:meth:`_derived_retry_models`). That is the
        difference between a remedy that works on a host nobody configured and
        one that only works on a host that anticipated this.
        """
        current = self.runtime.model() if self.runtime else ""
        tried = self._models_tried
        if current:
            # The model this address was just refused on is not one to come
            # back to. It used to be possible to: the list was shared, so a
            # third address reading "current" after two others had rotated
            # could land on the very model that refused the first.
            tried.add(current)
        with self._lock:
            unusable = set(self._models_unusable)
        configured = [m.strip() for m in self.config.str_list("retry_models") if str(m).strip()]
        for model in configured or self._derived_retry_models():
            if model == current or model in tried or model in unusable:
                continue
            tried.add(model)
            # Per thread (see AgentRuntime.model_override): this address's
            # next request, and nobody else's.
            for runtime in self._runtimes:
                runtime.model_override = model
            with self._lock:
                self._learned_model = model
            log.debug("retrying the agent on %s", model)
            return model
        return ""

    def _drop_unusable_model(self) -> str:
        """Leave a retry model the deployment cannot serve. Returns its name.

        A retry model that turns out not to be enabled says nothing about the
        model the run was configured with, so it must not latch the whole
        runtime as unusable. It is struck off for every address instead, and
        this one goes back to what it would otherwise be on.
        """
        model = self.runtime.model_override if self.runtime else ""
        if not model:
            return ""
        with self._lock:
            self._models_unusable.add(model)
            if self._learned_model == model:
                self._learned_model = ""
            fallback = self._learned_model
        for runtime in self._runtimes:
            runtime.model_override = fallback
        log.debug("retry model %s is not served by this deployment; leaving it", model)
        return model

    def _derived_retry_models(self) -> list[str]:
        """Models this run already knows about, from the tier settings.

        Phase 3 resolves one model id per tier against the live backend and
        exports each under the variable named by that backend's
        ``env_names.model_tier`` template. Those ids are real names for *this*
        deployment -- exactly what a rotation needs, and exactly what a constant
        in the source could never be. Ordered by ``mcp.agent.retry_model_tiers``
        so the operator decides which way the rotation goes; an empty list
        means no derivation, and the shorter brief stands alone as the remedy.
        """
        templates = self._model_tier_templates()
        if not templates:
            return []
        out: list[str] = []
        for tier in self.config.str_list("retry_model_tiers", ["sonnet", "haiku", "opus"]):
            tier = str(tier).strip()
            if not tier:
                continue
            for template in templates:
                name = template.replace("{TIER}", tier.upper()).replace("{tier}", tier.lower())
                value = str(self.settings.environ.get(name) or self.ctx.env.values.get(name) or "").strip()
                if value and value not in out:
                    out.append(value)
        return out

    def _model_tier_templates(self) -> list[str]:
        """Every ``env_names.model_tier`` pattern configuration knows about.

        Read from ``[provision.*.env_names]`` rather than named here, because
        which variable Claude Code reads a tier's model from is a fact about the
        backend and already written down once. Every backend is consulted, not
        just the selected one: phase 4 can be run on its own, long after the
        phase that chose a backend, and the variable is either set or it is not.
        """
        out: list[str] = []
        for entry in self.settings.section("provision").data.values():
            if not isinstance(entry, Mapping):
                continue
            names = entry.get("env_names")
            template = str(names.get("model_tier", "") or "") if isinstance(names, Mapping) else ""
            if template and template not in out:
                out.append(template)
        return out

    # -- repairing an entry that will not start --------------------------------

    def _agent_repair(self, candidate: Candidate, name: str, failure: str) -> Candidate | None:
        """Ask the agent to fix an entry that does not start, and hand it back.

        The agent gets first refusal because it can reason about what no rule
        here can. It is the strategist, given the entry, the failure, how the
        proven launch differs from it and what discovery knew about the server
        (including any script a bridge wrote). It can change things no rule
        here can reason about -- an argument that needs a different spelling, a
        variable the package documents under another name, a file it generated
        with the wrong shape.

        What it may not do is decide whether the fix worked. It returns a
        candidate; :class:`~.healing.EntryDoctor` launches it like any other and
        keeps it only if it starts. A repair that is not re-proved is a guess
        with extra steps.

        Returning ``None`` -- no agent, no answer, an unusable runtime -- is not
        a failure. It is the fallback: the deterministic repairs run next.
        """
        if not self.settings.bool_("mcp.healing.agent_enabled", True):
            return None
        usable, why = self.available()
        if not usable:
            log.debug("no agent repair for %s: %s", name, why)
            return None
        spec = self._agent_for(Stage.STRATEGY, self._starting_register())
        if spec is None or self.runtime is None:
            return None

        entry = self._writer().entry_for(candidate, name)
        brief = self._repair_brief(name, entry, failure, candidate)
        workspace = self.prepare_workspace()
        self.console.bullet(f"{name}: asking the agent to repair the entry", indent=6)
        result = self.runtime.run(
            brief,
            agent=spec,
            workspace=workspace,
            system_prompt=self._repair_system_prompt(name),
        )
        # The repair is not a stage, so its turn usage has nowhere to be kept;
        # a scratch outcome takes the findings and the console still hears
        # about an exhausted budget. Calling this with the result alone raised
        # TypeError after the turn was paid for, and every agent repair was
        # recorded as "agent: raised TypeError".
        self._note_turn_usage(StageOutcome(stage=Stage.STRATEGY, mode=Mode.AGENTIC), spec, result)
        if not result.ok or not result.payload:
            log.info("%s: the agent offered no repair (%s)", name, result.error or "no payload")
            return None

        for raw in _as_list(result.payload.get("candidates")):
            if not isinstance(raw, Mapping):
                continue
            revised, problem = self._candidate_from(raw, origin=candidate.origin)
            if revised is None:
                log.debug("%s: the agent's repair was unusable: %s", name, problem)
                continue
            # Carry the proof forward: the repair changes how it is launched,
            # not what it was shown to be, and the doctor re-proves the launch.
            revised.handshake = candidate.handshake
            return revised
        return None

    def _writer(self):
        from .writer import McpWriter

        return McpWriter(self.ctx)

    def _repair_system_prompt(self, name: str) -> str:
        return (
            f"You are repairing one entry in a Claude Code .mcp.json for the server {name}. "
            "The server itself answered during discovery, so the service works; what does not "
            "work is the entry. Change the entry, not your opinion of the service."
        )

    def _repair_brief(self, name: str, entry: Mapping[str, Any], failure: str, candidate: Candidate) -> str:
        """What the agent is told about a broken entry.

        Assembled from configuration like every other brief, so the wording is
        editable without a code change, with the specifics appended.
        """
        head = self.config.str_("repair_brief").strip()
        return "\n\n".join(
            [
                *([head] if head else []),
                f"Server name: {name}",
                f"Entry as written:\n{json.dumps(dict(entry), indent=2)}",
                f"What happened when it was launched:\n{failure}",
                f"What discovery knew about it:\n{_one_line(candidate.source, 400)}",
            ]
        )

    # -- running one agent -----------------------------------------------------

    def _agent_stage(
        self,
        name: str,
        target: TargetOutcome,
        *,
        attempt: int = 1,
        attempts: int = 1,
        plan: _RetryPlan | None = None,
    ) -> StageOutcome | None:
        plan = plan or _RetryPlan(register=self._starting_register())
        spec = self._agent_for(name, plan.register)
        if spec is None:
            return None
        assert self.runtime is not None  # guarded by available()
        if plan.turn_scale < 1.0:
            # A stage that ran out of wall clock is given a budget it can finish
            # inside, not the same one again. `spec` is rebuilt from
            # configuration on every attempt, so this narrows one attempt rather
            # than ratcheting the cap down for the rest of the run.
            base = spec.turn_budget(
                self.config.int_("max_turns", 24),
                override=self.config.int_("max_turns_override", 0),
            )
            if base:
                spec.max_turns = max(1, int(base * plan.turn_scale))

        stage = StageOutcome(stage=name, mode=self.mode)
        workspace = self.prepare_workspace()
        context_file = self._write_context(workspace, target, name)

        where = f"attempt {attempt} of {attempts}, " if attempts > 1 else ""
        register = f", {plan.register} brief" if plan.register != FULL else ""
        self.console.bullet(f"{name}: delegating to {spec.name} ({where}{self.runtime.name}{register})", indent=4)
        result = self.runtime.run(
            self._task_prompt(name, spec, target, workspace, context_file, plan),
            agent=spec,
            workspace=workspace,
            system_prompt=self._system_prompt(name, target, plan.register),
            on_progress=self._progress_hook(name),
        )
        with self._lock:
            self.spent_usd += result.cost_usd
        stage.findings["agent"] = result.to_dict()
        stage.findings["agent_name"] = spec.name
        stage.findings["prompt_register"] = plan.register
        self._note_turn_usage(stage, spec, result)

        if not result.ok:
            stage.status = Status.PARTIAL
            stage.detail = result.error or "the agent returned nothing"
            stage.error = "agent-failed"
            return stage
        if result.payload is None:
            stage.status = Status.PARTIAL
            stage.detail = "the agent did not emit the required JSON block"
            return stage

        self._ingest(stage, result, target)
        return stage

    def _note_turn_usage(self, stage: StageOutcome, spec: Any, result: AgentResult) -> None:
        """Say how much of its turn budget the agent actually used.

        Without this the budget is a dial with no gauge. Nothing in the run said
        whether a stage stopped because it had nothing left to try or because it
        ran out of room, so ``--agent-max-turns`` could only be tuned by
        instinct -- and the two cases want opposite adjustments. An agent that
        finished at 4 of 10 will not be helped by 30; one that stopped at 10 of
        10 may well be.

        Reported as a finding rather than a console line: it is per attempt and
        there are up to twelve of them per address, which is a paragraph of
        noise for a number most runs never need. It reaches the screen only
        when the budget was actually spent, which is the case worth knowing.
        """
        budget = spec.turn_budget(
            self.config.int_("max_turns", 24),
            override=self.config.int_("max_turns_override", 0),
        )
        used = int(result.turns or 0)
        stage.findings["turns_used"] = used
        stage.findings["turn_budget"] = budget
        if not budget or not used:
            return
        stage.findings["turn_budget_exhausted"] = used >= budget
        if used >= budget:
            # The one case the operator can act on, so it is said out loud.
            self.console.bullet(
                f"{stage.stage}: used all {budget} turn(s) -- the budget, not the target, "
                f"may be what ended this attempt (--agent-max-turns)",
                indent=6,
            )
            stage.add_remediation(
                f"{spec.name} spent its whole {budget}-turn budget. If the answer looks "
                "truncated, --agent-max-turns raises it for every agent -- with --timeout-scale, "
                "since the stage timeout binds first."
            )

    def _progress_hook(self, stage: str) -> Callable[[str], None] | None:
        """Let the runtime break the silence of a stage that takes minutes.

        Without this the terminal shows "delegating to ..." and then nothing at
        all until the stage ends, which for a slow target is indistinguishable
        from a hung process -- the operator's only options are to wait out an
        unknown interval or kill a run that was working. It is routed through
        the console's progress switch, so anyone who wants verdicts only still
        gets verdicts only.
        """
        if not self.console.show_progress:
            return None

        def report(message: str) -> None:
            self.console.bullet(f"{stage}: {message}", indent=6)

        return report

    def _ingest(self, stage: StageOutcome, result: AgentResult, target: TargetOutcome) -> None:
        """Turn an agent's payload into candidates, then prove or disprove them."""
        payload = result.payload or {}

        findings = payload.get("findings")
        if isinstance(findings, Mapping):
            for key, value in findings.items():
                stage.findings[str(key)] = clip(value, self.config.int_("finding_value_chars", 1500))

        self._ingest_due_diligence(stage, payload, target)
        self._note_websockets(stage, target, findings)

        proposals = [str(p) for p in _as_list(payload.get("proposals")) if str(p).strip()]
        if proposals:
            stage.findings["proposals"] = proposals
            limit = self.settings.int_("mcp.strategy.max_proposals", 5)
            for proposal in proposals[:limit]:
                if proposal not in target.playbook:
                    target.playbook.append(proposal)

        artifacts = [str(a) for a in _as_list(findings.get("artifacts") if isinstance(findings, Mapping) else [])]
        stage.artifacts.extend(artifacts)

        rejected: list[str] = []
        candidates: list[Candidate] = []
        for raw in _as_list(payload.get("candidates")):
            if not isinstance(raw, Mapping):
                continue
            candidate, problem = self._candidate_from(raw, origin=stage.stage)
            if candidate is None:
                rejected.append(problem)
                continue
            if problem:
                candidate.add_note(problem)
            candidates.append(candidate)
        if rejected:
            # Names and reasons only -- never the offending value.
            stage.findings["rejected_unsafe"] = rejected
            stage.add_remediation(*[f"Discarded an agent candidate: {r}" for r in rejected[:3]])

        # Relevance, then registrability -- both rules the deterministic
        # pipeline already applied, and neither of which an agent's candidate
        # used to meet. `@ateam-ai/mcp`, an "AI Team Solution Platform", became
        # the package stage's answer for build.opensuse.org that way: traditional
        # mode rejects it as unrelated, the agentic path never asked.
        if stage.stage == Stage.PACKAGE:
            candidates = self.fallback._relevant_only(candidates, target, stage)
        candidates = self._drop_ungrounded(stage, candidates, target)
        stage.candidates = candidates
        summary = str(payload.get("summary") or "").strip()

        if not candidates:
            stage.status = Status.PARTIAL if summary or proposals else Status.SKIPPED
            # Whose sentence this is matters. An agent that writes "endpoint
            # confirmed, a real initialize handshake completed" and returns no
            # candidate to test has found nothing this program can register or
            # re-prove, and quoting it bare reads as though it had.
            stage.detail = (
                f"the agent proposed nothing to verify; it reported: {_one_line(summary, 200)}"
                if summary
                else "the agent found no candidate MCP server"
            )
            return

        # The agent's own claim is a hypothesis. This is the test -- with a
        # product's own task asked only where the address is that product.
        self.fallback._learn_job(target)
        stage.verified = self.verifier.verify_all(
            candidates,
            stop_at_first=not self.settings.bool_("mcp.exhaustive", False),
            budget_seconds=self.settings.float_("mcp.discovery.stage_timeout_seconds", 180),
            # The same two rules the deterministic stages pass: do not stop at a
            # server the writer will refuse, and do not launch again what this
            # target has already judged. The second is most of what an agent
            # retry used to cost -- confluence.example.com re-trialled the same
            # `uvx mcp-atlassian` launch in attempt after attempt.
            keep=lambda c: self.fallback.registrable(c, target),
            memo=target.trials,
            why=lambda c: self.fallback.why_not_registrable(c, target),
        )
        # A candidate that never reached the service is repaired and proved
        # again now, before another agent attempt is spent on it.
        if not stage.verified and self.settings.bool_("mcp.verification.heal_transport_everywhere", True):
            for candidate in candidates:
                if (
                    self.fallback.heal_transport(candidate, target)
                    and candidate.verified
                    and self.fallback.registrable(candidate, target)
                ):
                    stage.verified.append(candidate)
                    break
        if summary:
            stage.findings["agent_summary"] = summary
        # Before anything is concluded from what verified: `verify_all` returns
        # every server that handshaked, including one the writer would refuse,
        # and a refused endpoint that reached _note_native closed every lower
        # tier on the strength of an entry that was never written.
        self.fallback.discard_unregistrable(stage, target)
        if stage.verified:
            best = stage.verified[0]
            stage.status = Status.OK
            stage.detail = f"{best.label} -- {best.handshake.summary() if best.handshake else 'verified'}"
            self._note_native(stage, target)
            return

        # No handshake completed -- but "nothing answered" and "something
        # answered by asking who you are" are different findings, and only the
        # first is worth spending another agent turn on. A 401 from the
        # target's own host is the endpoint proving it exists; searching for it
        # twice more returns the same URL and the same 401.
        # discard_unregistrable() ran above, before a credential wall may
        # settle the stage, not after: one the writer will refuse would end the
        # agent's attempts with nothing to show, and the refusal -- "its entry
        # names no address" -- is exactly what the next attempt can fix once
        # _correction tells it.
        self.fallback.mark_evidence(stage, target)
        walled = stage.note_provisional(
            self._provisional_limit(),
            prefer_detected_api=self.settings.bool_("mcp.discovery.auth.prefer_detected_api", True),
        )
        if walled:
            self._note_auth_wall(stage, target, walled)
            first = walled[0]
            stage.status = Status.PARTIAL
            said = _one_line(first.handshake.error if first.handshake else "", 90) or _NOTHING_SAID
            stage.detail = f"{first.label} -- {said}; {waits_for_credential(first).removeprefix('is ')}"
            stage.add_remediation(waiting_remedy(first))
            if first.kind != "stdio":
                # A bearer token for probing an HTTP endpoint; it does nothing
                # for a local package, which reads its own variable.
                stage.add_remediation(
                    "To probe with a token instead, so the handshake completes during this run: "
                    "set INITIALIZE_CLAUDE_AI_MCP_TOKEN_REF=env:YOUR_VAR (the value is read at "
                    "run time)."
                )
            return

        reasons = [
            f"{c.label}: {_one_line(c.handshake.error, 90)}"
            for c in candidates[:3]
            if c.handshake and c.handshake.error
        ]
        stage.status = Status.PARTIAL
        stage.detail = (
            f"the agent proposed {len(candidates)} candidate(s); none completed a handshake"
            # The verifier's reason, not the agent's: "none completed a
            # handshake" on its own does not say whether the URL was wrong, the
            # host was down or the transport was not the one being spoken, and
            # that is the difference between a retry worth making and three
            # identical ones.
            + (f" -- {reasons[0]}" if reasons else "")
        )
        stage.add_remediation(*reasons)

    def _note_native(self, stage: StageOutcome, target: TargetOutcome) -> None:
        """Record a verified tier-1 endpoint, which shuts the lower tiers.

        Only the endpoint stage can produce one: a URL that a *package* stage
        verified is a server somebody else runs, and registering it is tier 2
        however the handshake went.
        """
        if stage.stage != Stage.ENDPOINT:
            return
        urls = [c.url for c in stage.verified if c.kind == "url" and c.url]
        if not urls:
            return
        if not isinstance(target.due_diligence, DueDiligence):
            target.due_diligence = DueDiligence(target=target.target, source="agent")
        target.due_diligence.note("native_endpoints", urls)
        stage.findings["due_diligence"] = target.due_diligence.to_dict()

    def _provisional_limit(self) -> int:
        """How many credential-walled endpoints this stage may carry forward.

        The same number the deterministic pipeline uses, read from the same
        setting, so which mode found the endpoint cannot change how many of them
        are registered.
        """
        return self.fallback.provisional_limit(self.settings.bool_("mcp.exhaustive", False))

    def _note_auth_wall(self, stage: StageOutcome, target: TargetOutcome, walled: Sequence[Candidate]) -> None:
        """Record a tier-1 endpoint that answered by demanding a credential.

        The deterministic pipeline has always written these into the checklist;
        the agentic path did not, and the omission was expensive. Tier 1 had
        been found and the record did not say so, so the gate let the package
        and bridge stages open and go looking for a replacement for a server
        that was already sitting there -- each of them with its own three agent
        attempts. An authenticated endpoint is still the target's own endpoint.
        """
        if stage.stage != Stage.ENDPOINT:
            return
        urls = [c.url for c in walled if c.kind == "url" and c.url]
        if not urls:
            return
        if not isinstance(target.due_diligence, DueDiligence):
            target.due_diligence = DueDiligence(target=target.target, source="agent")
        target.due_diligence.note("auth_walls", urls)
        stage.findings["auth_required_urls"] = list(target.due_diligence.auth_walls)
        stage.findings["due_diligence"] = target.due_diligence.to_dict()

    def _drop_ungrounded(
        self, stage: StageOutcome, candidates: list[Candidate], target: TargetOutcome
    ) -> list[Candidate]:
        """Apply the registration rule *now*, so the agent can still act on it.

        This check also runs at publish time, where it is the last word. Running
        it only there made it a silent veto: the verdict arrived after every
        stage had finished, so an agent could never be told, and a one-line
        omission cost the whole address.

        It really did. An agent built a bridge for
        ``developers.google.com/knowledge/api``, the bridge verified with three
        tools -- and it was refused at the very end because its entry contained
        no address at all, which is a single environment variable's worth of
        repair. The agent had two attempts left and was never told.

        So the stage does not settle on a candidate that cannot be registered,
        and the reason goes where the retry will read it. The agent's next
        attempt is handed "your entry names no address; put the endpoint URL in
        env", which is actionable in a way that a refusal printed after the run
        is not.

        Both of the writer's refusals are asked: grounding, and portability --
        an entry naming a path in a trial venv or the sandbox will be refused as
        surely as one naming no address, and the agent can fix that too.
        """
        grounded = self.settings.bool_("mcp.registration.require_address_in_entry", True)
        known = endpoint_hosts(target)
        kept: list[Candidate] = []
        refused: list[str] = []
        for candidate in candidates:
            # In the writer's order: grounding, then portability.
            verdict = (
                ground(
                    candidate,
                    target.target,
                    allow_front_door=self.settings.bool_("mcp.registration.allow_bare_origin", False),
                    served_by=known,
                    # Nothing has been dialled yet -- this runs before verify_all --
                    # so any test resting on "did it answer" is unanswerable here and
                    # must not be read as "no". It was, and it cost Google's own
                    # endpoint three refusals in one run before it was ever probed.
                    probed=candidate.handshake is not None,
                    hosting_namespaces=hosting_namespaces_of(self.settings),
                    identity=identity_rule_of(self.settings),
                )
                if grounded
                else None
            )
            if verdict is None or verdict.registrable:
                verdict = self.fallback.portability_of(candidate)
            if verdict.registrable:
                kept.append(candidate)
                continue
            refused.append(f"{candidate.label}: {verdict.reason}")
            log.info("%s: %s cannot be registered -- %s", target.target, candidate.label, verdict.reason)
        if refused:
            stage.findings["not_registrable"] = refused
            stage.add_remediation(
                *[
                    f"{reason} -- an entry that cannot be registered is not a result, so this "
                    "candidate was not verified"
                    for reason in refused[:3]
                ]
            )
        return kept

    def _note_websockets(self, stage: StageOutcome, target: TargetOutcome, findings: Any) -> None:
        """Read ``findings.websocket_urls`` the way the deterministic pipeline does.

        Both modes report a WebSocket-only route under that name, but only the
        deterministic one turned it into "a native endpoint that needs a bridge"
        -- so an agent that reported one exactly as its skill described left the
        bridge stage shut in the one case it exists for. The same vocabulary
        now means the same thing whichever mode wrote it.
        """
        if stage.stage != Stage.ENDPOINT or not isinstance(findings, Mapping):
            return
        urls = [str(u).strip() for u in _as_list(findings.get("websocket_urls")) if str(u).strip()]
        if not urls:
            return
        if not isinstance(target.due_diligence, DueDiligence):
            target.due_diligence = DueDiligence(target=target.target, source="agent")
        record = target.due_diligence
        record.note(
            "needs_bridge",
            (
                url
                for url in urls
                if url not in record.native_endpoints and url not in record.claimed.get("native_endpoints", [])
            ),
        )
        stage.findings["due_diligence"] = record.to_dict()

    def _ingest_due_diligence(self, stage: StageOutcome, payload: Mapping[str, Any], target: TargetOutcome) -> None:
        """Fold the agent's checklist into the target's record.

        The agent reports which of the four steps it carried out and what it has
        to show for each. It does not get to mark the checklist done by saying
        so -- :meth:`DueDiligence.from_payload` downgrades any step that arrived
        without evidence -- and it cannot *un*-do a step the deterministic
        pipeline already performed, because the merge keeps the stronger of the
        two. Together that makes the record additive: whichever mode did the
        work, the gate sees it.
        """
        reported = DueDiligence.from_payload(target.target, payload.get("due_diligence"), source="agent")
        if reported is None:
            if stage.stage == Stage.ENDPOINT:
                stage.add_remediation(
                    "The endpoint agent returned no due_diligence block, so the tier-1 checklist "
                    "counts as unperformed and the lower tiers stay shut. Re-run, or set "
                    "mcp.policy.require_checklist = false to accept an unfinished search."
                )
            return
        if isinstance(target.due_diligence, DueDiligence):
            target.due_diligence.merge(reported)
        else:
            target.due_diligence = reported
        record = target.due_diligence
        stage.findings["due_diligence"] = record.to_dict()
        gaps = record.gaps()
        if gaps:
            stage.add_remediation(
                "Tier-1 checklist still incomplete: " + ", ".join(f"{s.letter} ({s.title})" for s in gaps)
            )

    # -- candidate hygiene -----------------------------------------------------

    def _placeholder_in(self, texts: Sequence[Any]) -> str:
        """The first configured placeholder (``mcp.agent.placeholder_patterns``) any of ``texts`` carries."""
        for pattern in self.config.str_list(
            "placeholder_patterns",
            [
                "/abs/path",
                "(^|/)path/to/",
                "<[A-Z][A-Z0-9 _./-]*>",
                "YOUR[_-][A-Z0-9_]+",
                "/your/",
            ],
        ):
            for text in texts:
                try:
                    match = re.search(pattern, str(text or ""))
                except re.error:
                    break
                if match:
                    return match.group(0)
        return ""

    def _candidate_from(self, raw: Mapping[str, Any], *, origin: str) -> tuple[Candidate | None, str]:
        """Build a candidate from agent output. Returns ``(candidate, note)``.

        ``candidate`` is None when the entry had to be discarded; ``note`` then
        explains why, in terms safe to print.
        """
        kind = str(raw.get("kind") or ("stdio" if raw.get("command") else "url")).strip().lower()
        # An agent's word for a kind, normalised rather than refused: two
        # build.example.net candidates were discarded as "of unknown kind 'package'"
        # (mcp.agent.kind_aliases).
        kind = str(
            self.config.dict_(
                "kind_aliases",
                {
                    "package": "stdio",
                    "local": "stdio",
                    "process": "stdio",
                    "command": "stdio",
                    "remote": "url",
                    "http": "url",
                    "https": "url",
                    "endpoint": "url",
                    "sse": "url",
                },
            ).get(kind, kind)
        )
        url = str(raw.get("url") or "").strip()
        command = _as_argv(raw.get("command"))
        args = _as_argv(raw.get("args"))
        name = str(raw.get("name") or "").strip()
        if kind not in ("url", "stdio") and self.config.bool_("kind_from_shape", True) and bool(command) != bool(url):
            # A word nobody listed ("bridge") never decides: what the candidate
            # carries does -- a command to launch is stdio, a url is url.
            # smelt.example.net's second bridge was discarded as "of unknown kind 'bridge'".
            kind = "stdio" if command else "url"
        longest = self.config.int_("max_name_length", 64)
        if longest > 0 and len(slugify(name)) > longest:
            # A description in the name field -- "url https://… transport
            # streamable-http how found …" -- is not a name: the candidate is
            # named from what it launches or reaches instead.
            name = ""

        if kind == "url" and not url:
            return None, "a url candidate with no url"
        if kind == "stdio" and not command:
            return None, f"{name or 'a stdio candidate'} with no launch command"
        if kind not in ("url", "stdio"):
            return None, f"{name or 'a candidate'} of unknown kind {kind!r}"

        left = self._placeholder_in(
            [url, *command, *args, *_strings_in(raw.get("env")), *_strings_in(raw.get("headers"))]
        )
        if left:
            # Copied from an example rather than filled in. Said precisely, so
            # the next attempt fixes the value instead of the whole proposal.
            return None, (
                f"{name or 'a candidate'} still carries the placeholder {left!r} -- you copied an "
                "example's placeholder; give the real value you found or produced"
            )
        for token in [*command, *args]:
            if carries_inline_credential(token):
                return None, (
                    f"{name or 'a candidate'} carried a credential on its command line; "
                    "credentials must be passed by environment reference"
                )
        if url and carries_inline_credential(url):
            return None, f"{name or 'a candidate'} carried a credential in its URL"

        candidate = Candidate(
            kind=kind,
            name=name or slugify(url or (command[0] if command else "candidate")),
            url=url,
            command=command,
            args=args,
            # Normalised to one the kind can carry, never copied: a word the
            # table did not know became "stdio" beside a url, and Claude Code
            # dropped the entry (mcp.registration.transport_aliases).
            transport=normalize_transport(
                str(raw.get("transport") or ""),
                kind,
                self.settings.dict_("mcp.registration.transport_aliases", dict(TRANSPORT_ALIASES)),
            ),
            origin=origin,
            source=str(raw.get("source") or "proposed by the discovery agent").strip(),
            confidence=_as_float(raw.get("confidence"), 0.5),
            install=[],
            notes=[str(n) for n in _as_list(raw.get("notes"))],
        )

        notes: list[str] = []
        said = str(raw.get("transport") or "").strip()
        if said and said.lower().replace("_", "-") != candidate.transport:
            notes.append(f"the proposal said transport {said!r}; a {kind} entry is registered as {candidate.transport}")
        candidate.install, install_notes = _install_steps(raw.get("install"))
        notes.extend(install_notes)
        (
            candidate.env,
            candidate.secret_env,
            env_notes,
            candidate.secret_formats,
            candidate.secret_sources,
        ) = self._split_env(_as_mapping(raw.get("env")), _as_mapping(raw.get("secret_env")))
        notes.extend(env_notes)
        candidate.headers, header_notes = self._safe_headers(_as_mapping(raw.get("headers")))
        notes.extend(header_notes)
        # A bridge the agent wrote for a configured API kind is proved like the
        # generated one: its tool that needs no login must answer with content
        # (CandidateVerifier.public_calls). An unknown name is ignored.
        claimed_kind = str(raw.get("api_kind") or "").strip()
        if claimed_kind and claimed_kind in self.settings.dict_("mcp.discovery.api_kinds", {}):
            candidate.api_kind = claimed_kind
        if kind == "url" and truthy(raw.get("requires_auth")):
            # The agent read something that says this endpoint wants a
            # credential. That is a reason, not a verdict: the probe still
            # decides, and only an inconclusive reply is read in its light --
            # see CandidateVerifier.credit_auth_claim.
            said = str(
                clip(
                    redact.redact(str(raw.get("auth_evidence") or "").strip()),
                    self.config.int_("auth_claim_chars", 300),
                )
            )
            candidate.auth_claim = "the discovery agent says it needs a credential" + (f" ({said})" if said else "")

        return candidate, "; ".join(notes)

    def _split_env(
        self, env: Mapping[str, Any], secret_env: Mapping[str, Any]
    ) -> tuple[dict[str, str], dict[str, str], list[str], dict[str, str], dict[str, str]]:
        """Separate plain configuration from credential references.

        A value the agent put under a secret-looking name is moved out and
        replaced by a reference: whatever it thought it was passing, this process
        will read the real value from the operator's environment at launch time.
        """
        notes: list[str] = []
        plain: dict[str, str] = {}
        secrets: dict[str, str] = {}
        shapes: dict[str, str] = {}
        sources: dict[str, str] = {}

        for key, value in env.items():
            name = str(key).strip()
            if not name:
                continue
            text = "" if value is None else str(value)
            # The shared predicate, not the raw pattern. Calling
            # SENSITIVE_NAME.search here is how this pipeline kept its own
            # private answer to a question secret_policy exists to settle once:
            # the selector guard that spares BUGZILLA_AUTH_MODE lives in
            # is_secret_name, and a direct match on the regex walks straight
            # past it. It also picks up an inline credential hiding under an
            # innocuous name, which the old condition missed.
            if holds_a_credential(name, text):
                secrets[name] = f"env:{name}"
                notes.append(f"{name} looked like a credential; it will be read from the environment")
                continue
            plain[name] = text

        for key, value in secret_env.items():
            name = str(key).strip()
            if not name:
                continue
            if names_a_selector(name):
                # An agent put it here, and an agent can be wrong about this.
                # `BUGZILLA_AUTH_MODE` arrived in `secret_env` with no value --
                # it selects apikey/basic/anonymous and holds no secret -- and
                # taking that at face value put "${BUGZILLA_AUTH_MODE}" into the
                # entry for a variable nobody could fill. The server exited on
                # start-up. This is the path the real failure took, so
                # reclassifying only what arrives in `env` would have fixed
                # nothing.
                notes.append(
                    f"{name} selects between named alternatives rather than holding a secret, "
                    "so it is not treated as a credential. No value was reported for it, so "
                    "it is left out of the entry entirely rather than written as an empty "
                    "placeholder"
                )
                continue
            # Two accepted shapes, because the richer one had to be added
            # without breaking every agent that emits the plain one:
            #   "VAR": "env:VAR"
            #   "VAR": {"reference": "env:VAR", "shape": "username:key:secret",
            #           "obtain": "https://host/api_keys"}
            # Only `reference` can carry a credential, and it is scrubbed below
            # exactly as before; `shape` and `obtain` are descriptions of a
            # value, never a value, and are rejected if they look like one.
            if isinstance(value, Mapping):
                shape = str(value.get("shape") or value.get("format") or "").strip()
                where = str(value.get("obtain") or value.get("source") or "").strip()
                reference = str(value.get("reference") or value.get("ref") or "").strip()
                if shape and not _looks_like_a_value(shape):
                    shapes[name] = shape[:120]
                elif shape:
                    notes.append(f"discarded the reported shape of {name}: it looked like a value")
                if where and not _looks_like_a_value(where):
                    sources[name] = where[:200]
            else:
                reference = str(value or "").strip()
            scheme = reference.partition(":")[0].strip().lower()
            if scheme not in SAFE_REFERENCE_SCHEMES:
                # Includes 'literal:' and bare values: replace with the safe reading.
                if reference and reference != name:
                    notes.append(f"{name} was not given as a secret reference; using env:{name}")
                reference = f"env:{name}"
            secrets[name] = reference
            plain.pop(name, None)

        return plain, secrets, notes, shapes, sources

    def _safe_headers(self, headers: Mapping[str, Any]) -> tuple[dict[str, str], list[str]]:
        """Keep non-sensitive headers; refuse to carry an inline credential."""
        notes: list[str] = []
        out: dict[str, str] = {}
        for key, value in headers.items():
            name = str(key).strip()
            text = str(value or "").strip()
            if not name:
                continue
            if is_secret_name(name) or carries_inline_credential(text):
                notes.append(
                    f"dropped the {name} header: set mcp.discovery.auth.bearer_token_ref "
                    "so the value is resolved at run time instead"
                )
                continue
            out[name] = text
        return out, notes

    # -- workspace -------------------------------------------------------------

    def prepare_workspace(self) -> Path:
        """Materialise skills and scratch directories the agents work in.

        Rebuilt on every run so an agent can never read a stale copy of its own
        instructions, and so ``setting_sources = ["project"]`` picks up the
        skills that ship with the version of initialize-claude-ai doing the running.
        """
        # Once per run, under the lock. Every address's first stage asks for
        # the workspace; under --concurrency they ask at the same moment, and
        # an unguarded "not prepared yet" let each of them delete and re-copy
        # the same skills directory underneath the others -- FileExistsError,
        # or a file vanishing mid-copy, and the stage lost.
        with self._lock:
            if self._workspace is not None:
                return self._workspace

            workspace = workspace_for(self.ctx)
            skills_target = workspace / ".claude" / "skills"
            source = self._skills_source()
            if source is not None and source.is_dir() and not self.ctx.dry_run:
                _replace_tree(source, skills_target)
                log.debug(
                    "copied %d skill(s) into %s",
                    len(list(skills_target.glob("*/SKILL.md"))),
                    skills_target,
                )
            elif source is None:
                log.warning("no skills directory found; agents will run without their method guides")

            for sub in ("context", "artifacts"):
                self.ctx.ensure_dir(workspace / sub)
            self._workspace = workspace
            return workspace

    def _artifacts_dir(self, workspace: Path, target: TargetOutcome) -> Path:
        """Where one address's agents put what they generate.

        One directory per address, named from the address like its context
        file, so two addresses worked at once cannot write the same venv or
        the same bridge file over each other.
        """
        path = workspace / "artifacts" / slugify(target.target)
        self.ctx.ensure_dir(path)
        return path

    def _skills_source(self) -> Path | None:
        configured = self.config.path("skills_dir")
        if configured:
            return Path(configured).expanduser()
        packaged = Path(__file__).resolve().parent.parent / "skills"
        return packaged if packaged.is_dir() else None

    def _write_context(self, workspace: Path, target: TargetOutcome, stage: str) -> Path:
        """Persist what earlier stages learned, for the agent to read."""
        path = workspace / "context" / f"{slugify(target.target)}.{stage}.json"
        payload = {
            "target": target.target,
            "base_url": target.base_url,
            "stage": stage,
            "previous_stages": [
                {
                    "stage": s.stage,
                    "mode": s.mode,
                    "status": s.status,
                    "detail": s.detail,
                    "findings": clip(s.findings, self.config.int_("previous_stage_findings_chars", 6000)),
                }
                for s in target.stages
            ],
            "already_verified": [c.label for c in target.verified],
            "due_diligence": (
                target.due_diligence.to_dict() if isinstance(target.due_diligence, DueDiligence) else None
            ),
        }
        if not self.ctx.dry_run:
            path.parent.mkdir(parents=True, exist_ok=True)
            write_generated(path, json.dumps(payload, indent=2, default=str))
        return path

    # -- prompts ---------------------------------------------------------------

    def _agent_for(self, stage: str, register: str = FULL) -> AgentDefinitionSpec | None:
        """Resolve the subagent that owns a stage, entirely from configuration."""
        definitions = self.config.dict_("definitions")
        if not definitions:
            return None
        mapped = self.config.str_(f"stage_agents.{stage}")
        name = mapped if mapped in definitions else ""
        if not name:
            # No explicit mapping: the definition whose name mentions the stage.
            name = next((k for k in definitions if stage in str(k).lower()), "")
        if not name:
            return None

        entry = definitions[name] if isinstance(definitions.get(name), Mapping) else {}
        skill = str(entry.get("skill") or "").strip()
        objective = str(entry.get("objective") or entry.get("description") or "").strip()
        if register == MINIMAL:
            # The one-line description, not the paragraph: at the floor of the
            # ladder the brief is the question and nothing else.
            objective = str(entry.get("description") or objective).strip()
        tools = [str(t) for t in _as_list(entry.get("tools"))]
        return AgentDefinitionSpec(
            name=str(name),
            description=str(entry.get("description") or objective),
            prompt=self._agent_instructions(stage, objective, skill, register),
            tools=tools,
            model=str(entry.get("model") or self.config.str_("model")),
            max_turns=_as_int(entry.get("max_turns"), 0),
        )

    def _agent_instructions(self, stage: str, objective: str, skill: str, register: str = FULL) -> str:
        """The subagent's own system prompt: brief, method, hard rules.

        The hard rules survive every register. They are the rules that keep a
        credential out of a report and an unverified guess out of a candidate
        list, and a shorter prompt is worth nothing if it is also a laxer one.
        What shortens is the method: the skill reference and the tier lecture.
        """
        lines = [objective]
        if skill and register != MINIMAL:
            lines.append(
                f"Follow the `{skill}` skill; it is available in this workspace and it defines both "
                "the method and the exact output format you must produce."
            )
        lines.append(self._tier_rules(stage, register))
        if register == MINIMAL:
            lines.append(
                "Hard rules:\n"
                "- Report a server as working only if you completed an MCP `initialize` handshake "
                "with it; everything else is a lead, and an honest lead costs nothing.\n"
                "- Never put a password, key or token in a URL, a command line, a file or your "
                "output. Name the environment variable it should come from instead.\n"
                "- Read only towards the target: ask it for information, change nothing on it.\n"
                "- Finish with a single fenced JSON block, and nothing after it."
            )
        else:
            lines.append(
                "Hard rules, which override anything else you decide:\n"
                "- Never report a server as working unless you completed an MCP `initialize` "
                "handshake with it. Everything you return is independently re-verified, so an "
                "honest 'unverified' costs nothing and a false positive wastes the whole run.\n"
                "- Never put a password, key, token or other credential in a URL, a command line, "
                "a file you write, or your output. Name the environment variable it should come "
                "from instead.\n"
                "- Read only. Send ordinary client requests and read what comes back; do not invoke "
                "tools a discovered server advertises, and do not change anything on the target.\n"
                "- Name every server after the service it talks to, derived from its fully "
                "qualified domain name -- `bugzilla-example-com`, never `bugzilla`. Two services with "
                "the same short name are the commonest way a working configuration silently starts "
                "pointing at the wrong host.\n"
                "- Report every environment variable, API key or token a candidate needs, by name, "
                "in `env` or `secret_env`. A server that is registered without its credentials "
                "fails at first use, in front of the user, with no explanation. A credential in "
                "parts -- a user and a password -- is every part in `secret_env`, each under the "
                "exact name the server reads (from its --help or docs, never invented).\n"
                "- Report a variable only if you know what it holds, and say what that is. A name "
                "with no value is dropped before the trial -- it says nothing about what the "
                "server needs. `BUGZILLA_AUTH_MODE` once reached an entry as an empty `${NAME}`; "
                "it is a mode selector, not a token, and the server exited on start-up. Put a "
                "selector in `env` with its value; give each secret its own `shape`; leave out "
                "what you cannot describe, and say so in your notes.\n"
                "- An entry is only accepted if it starts when launched the way the client "
                "launches it: with the entry's own `env` and nothing else, from the client's "
                "own directory -- and that includes a server that is only waiting for a "
                "credential, launched with a stand-in in the slot. Your shell's variables are "
                "not passed on, and relative paths do not resolve. So every variable the server "
                "needs must be *in "
                "the entry* -- a server that works for you because something was already "
                "exported will be refused, and the target will fall to the next tier. List "
                "each one in `env` (a value) or `secret_env` (a reference), and never rely on "
                "the ambient environment.\n"
                "- That means every variable the server *reads*, not only the ones it needs to "
                "start: settings with defaults (with the default as the value), and every file it "
                "opens -- a manifest, a CA bundle, a schema -- by the variable it is read from, "
                "with its absolute path. A file found only by a fallback in the source is a "
                "setting nobody who copies the entry can find: an openQA bridge opened a stale "
                "manifest that way on another machine. The caller makes every path relocatable "
                "and adds any read it can see in a script you wrote, but it cannot see inside a "
                "package, so for a package list what its documentation says it reads.\n"
                "- If a command you run fails, read the failure before retrying it. A message "
                "saying the model is unavailable, the credential is refused or the quota is "
                "gone is about this machine, not about your request, and repeating the request "
                "spends minutes to be told the same thing. Report it plainly and stop -- it is "
                "the operator's to fix, and the error usually names the fix.\n"
                "- A server that starts and lists no tools has not asked for a credential. One "
                "kept waiting for a token still advertises what it can do, so an empty listing "
                "means something else: most often the credential variable is not the name that "
                "package actually reads. Report the variable exactly as the package documents "
                "it and say where you read it -- a generic `PERSONAL_TOKEN` or `API_KEY` is "
                "usually a paraphrase. `mcp-atlassian` was registered for confluence.example.com "
                "with `PERSONAL_TOKEN`, which it does not read; the operator set it and still "
                "had no tools.\n"
                "- When you handshake with a candidate, read what it says about itself and quote "
                "the part that names the target: its `instructions`, its resource URIs, its tool "
                "descriptions, the defaults in its input schemas. That is the strongest evidence "
                "that a server serves your target -- stronger than the package name or the "
                "command line, because it is the running server saying what it does. If nothing "
                "in the handshake mentions the target and nothing in the entry names it, you "
                "have found a package and not an answer: say so, so the next tier can run. "
                "`@ateam-ai/mcp` handshaked perfectly for build.opensuse.org and had nothing to "
                "do with it.\n"
                "- A certificate failure is a finding with a fix, and it does not look like one. "
                "A runtime that ships its own trust store -- Node, anything using certifi -- "
                "cannot reach a target behind an internal CA even where the host trusts it: "
                "build.example.net answered curl with HTTP 200 and gave Node "
                "UNABLE_TO_GET_ISSUER_CERT_LOCALLY, which reached the operator as "
                "`Connection closed` with no mention of a certificate. Report the issuer. "
                "When the brief names a trust bundle for the target, use that file -- do not "
                "extract a chain yourself. "
                "Never set a variable that switches verification off -- it is stripped from the "
                "entry, and the trust store is the answer.\n"
                "- An endpoint on a host that is not the target's belongs to whoever owns that "
                "host. A vendor serving its own service from a sibling domain is ordinary "
                "(`github.com` from `api.githubcopilot.com`); a vendor's multi-tenant cloud is "
                "not the target when the target is a private installation of that vendor's "
                "product. `mcp.atlassian.com` serves Atlassian Cloud tenants, not a self-hosted "
                "Confluence. If a multi-tenant endpoint cannot be told which instance you mean, "
                "it is not an answer for this one.\n"
                "- Finish with a single fenced JSON block in the documented shape, and nothing "
                "after it."
            )
        return "\n\n".join(line for line in lines if line)

    def _tier_rules(self, stage: str, register: str = FULL) -> str:
        """The tier policy, in the second person, for the agent that must obey it.

        Shortens with the register. The *rule* never changes -- the gate in
        :meth:`gate` enforces it either way, and telling an agent about a rule
        is a courtesy, not the enforcement -- so at ``minimal`` this is one
        sentence, and the checklist paragraph the caller still needs is asked
        for in the task prompt instead.
        """
        if not self.policy.enabled:
            return ""
        tier = self.policy.tier(stage)
        if register == MINIMAL:
            if tier <= 1:
                return (
                    "This is tier 1: settle whether the target has its own MCP endpoint, and "
                    "report the four checklist steps you were asked for. Nothing below tier 1 runs "
                    "until you have."
                )
            return (
                f"This is tier {tier} of 3, and it only runs because the target has no MCP endpoint "
                "of its own. If you find one after all, report that URL: it beats anything you "
                "could install or write."
            )

        head = (
            f"Discovery priority. This stage is tier {tier} of 3. Tier 1 is the target's own native "
            "MCP endpoint, tier 2 is a package or repository someone has already published, tier 3 "
            "is code written here from scratch. Descending a tier while a higher one was available "
            "is an architectural failure: it installs or writes software to do a job the target was "
            "already doing, and somebody then has to maintain it."
        )
        if tier > 1:
            return (
                f"{head}\n\n"
                "You are below tier 1, which means the endpoint stage has already been run and its "
                "checklist is on record; this stage only runs at all because no native endpoint "
                "answered. If anything you find points back at a native endpoint on the target "
                "itself, say so and report that URL instead -- it outranks anything you could "
                "install or write."
            )

        checklist = "\n".join(f"  {letter}. {title}" for _key, letter, title in CHECKLIST)
        method = (
            "Step A means a range of subpaths rather than only `/mcp`: the conventional ones, and "
            "the ones the target's own documentation and URL shape point to -- each confirmed with "
            "one ordinary client request. Step B means asking over both http:// and https://; when "
            "a certificate is not trusted, verify it against the trust bundle your brief names "
            "(curl --cacert) or report its issuer -- an untrusted certificate is a fact about the "
            "network, not an answer about MCP, and the caller has already retried once without "
            "verification, so never switch it off yourself. Step C means sending "
            "`Accept: application/json, text/event-stream` and "
            "`Content-Type: application/json`, and actually reading the response body and headers "
            "for JSON-RPC errors, SSE frames or WebSocket upgrades. Step D means reading what the "
            "service says about itself: the vendor's own documentation, their `/llms.txt`, the "
            "official MCP registry and public repositories."
        )
        if register == FULL:
            paths = ", ".join(self.policy.mandatory_paths())
            if paths:
                method = f"{method} The conventional locations to include in step A: {paths}."
        return (
            f"{head}\n\n"
            "You are tier 1, so the entire question of whether the lower tiers may run at all "
            "rests on how thoroughly you look. Carry out all four steps and report them in the "
            "`due_diligence` block:\n"
            f"{checklist}\n"
            f"{method}\n"
            "A step you genuinely cannot perform here is reported with `unavailable` and the "
            "reason; a step you did not attempt is a gap, and a gap keeps the lower tiers shut. "
            "Do not claim a step you did not run."
        )

    def _system_prompt(self, stage: str, target: TargetOutcome, register: str = FULL) -> str:
        stages = self.settings.str_list("mcp.stages", [Stage.ENDPOINT, Stage.PACKAGE, Stage.BRIDGE, Stage.STRATEGY])
        position = stages.index(stage) + 1 if stage in stages else 0
        setting = (
            f"You are stage {position} of {len(stages)} in initialize-claude-ai's MCP "
            f"auto-discovery, working on the target {target.target}. This is the operator's own "
            "machine being set up to talk to services they already use and are authorised to "
            "reach, and the work is reading published documentation and sending ordinary, "
            "read-only client requests to find out whether a service offers an MCP endpoint."
        )
        if register == MINIMAL:
            return setting
        return (
            f"{setting} Earlier stages have already run and their findings are given to you; "
            "later stages will see yours. Be concrete, be honest about what you verified versus "
            "what you are proposing, and stop as soon as you have a working answer."
        )

    def _certificate_brief(self, target: TargetOutcome) -> str:
        """What the run knows about this target's certificate; "" when nothing needs saying.

        Empty for every host whose certificate verifies here, so their prompts
        are exactly what they were.
        """
        store = TrustStore.for_run(self.ctx)
        found = store.finding(target.base_url or target.target)
        if found is None or found.status not in (ANCHORED, UNTRUSTED):
            return ""
        runtimes = self.settings.dict_("mcp.trust.runtimes", {})
        variables = [v for names in runtimes.values() for v in (names if isinstance(names, list) else [names])]
        return trust_store.brief(found, store.bundle(), [str(v) for v in variables])

    def _auth_wall_brief(self, stage: str, target: TargetOutcome) -> str:
        """How to report an endpoint that refuses without a credential; "" outside tier 1.

        Without this an endpoint agent that met an auth wall, and had no
        credential to get past it, proposed nothing -- openqa.opensuse.org's
        agents did exactly that -- and the endpoint that exists was lost. The
        brief names the variable the entry will read (the same name the writer
        would choose, auth profile included) so the agent can propose the
        endpoint with ``requires_auth`` and that slot, and never a value.
        """
        if self.policy.tier(stage) > 1:
            return ""
        template = self.config.str_("auth_wall_brief").strip()
        if not template:
            return ""
        from .writer import McpWriter  # the writer imports the verifier; import late

        writer = McpWriter(self.ctx)
        probe = Candidate(kind="url", url=target.base_url or target.target)
        variable = writer.credential_env_name(probe, writer.base_name(probe.url))
        shape, obtain = writer.credential_shape(probe)
        host = parse_address(probe.url).host
        return fill(
            template,
            {
                "variable": variable,
                "shape": shape or "whatever the product issues -- say so if you found out",
                "obtain": (obtain or "the product's own documentation").replace("{host}", host),
            },
        )

    def _existing_brief(self, stage: str, target: TargetOutcome) -> str:
        """This machine's registrations for the address, and what became of them; "" if none.

        They were tried before the agent was started (see
        :meth:`~.traditional.TraditionalPipeline.reuse`), so the agent is told
        what they are and how they answered -- a lead to fix or improve on,
        never something to re-register blindly.
        """
        template = self.config.str_("existing_brief").strip()
        tried = self.fallback.reused_for(target.target, stage)
        if not template or not tried:
            return ""
        # Proved not to be this address's (see TraditionalPipeline.note_stale):
        # said so, so the agent does not propose the same entry back.
        wrong = {s.get("name"): s.get("reason", "") for s in target.stale}
        lines = []
        for candidate in tried:
            shake = candidate.handshake
            verdict = shake.summary() if shake and shake.ok else (shake.error if shake else "not tried")
            if candidate.name in wrong:
                verdict = f"NOT THIS ADDRESS'S -- {wrong[candidate.name]}"
            lines.append(
                f"  - {candidate.label}: {redact.redact(_one_line(str(verdict), self.config.int_('existing_brief_chars', 200)))}"
            )
        return fill(template, {"registrations": "\n".join(lines)})

    def _api_kind_brief(self, stage: str, target: TargetOutcome) -> str:
        """The kinds of API the target serves, and the generic servers for them; "" if none or tier 1.

        smelt.example.net serves GraphQL and nothing else, and the agents searched
        the registries for its product name, which no server is named after.
        The server that works is generic -- ``mcp-graphql`` -- and is found by
        what the target *is*. See :mod:`.api_kinds`.
        """
        template = self.config.str_("api_kind_brief").strip()
        if not template or self.policy.tier(stage) < 2:
            return ""
        found = self.fallback.api_kinds_for(target)
        if not found:
            return ""
        generic = api_kinds.adapter_candidates(self.ctx, target.target, found, base_url=target.base_url)
        servers = [
            "  - "
            + " ".join(c.full_command())
            + (" with " + ", ".join(f"{k}={v}" for k, v in c.env.items()) if c.env else "")
            for c in generic
        ]
        return fill(
            template,
            {
                "kinds": ", ".join(f"a {v['label']} API at {v['url']}" for v in found.values()),
                "search_terms": ", ".join(api_kinds.search_terms(self.settings, found)) or "the kind's name",
                "servers": "\n".join(servers) or "  (none configured)",
                "operations": "\n".join(api_kinds.operations_in_words(self.settings, found)) or "  (none configured)",
            },
        )

    def _task_prompt(
        self,
        stage: str,
        spec: AgentDefinitionSpec,
        target: TargetOutcome,
        workspace: Path,
        context_file: Path,
        plan: _RetryPlan | None = None,
    ) -> str:
        """The brief for one attempt, written at ``plan.register``.

        Every rung asks for the same work and the same answer. What changes is
        how much of the surrounding material travels in the prompt: at ``full``
        the earlier findings and the checklist so far are pasted inline, at
        ``plain`` they stay in ``context_file`` for the agent to read if it
        wants them, and at ``minimal`` only the question survives.
        """
        plan = plan or _RetryPlan(register=self._starting_register())
        register = plan.register
        protocol = self.settings.str_list(
            "mcp.protocol.versions",
            [
                "2026-07-28",
                "2025-11-25",
                "2025-06-18",
                "2025-03-26",
                "2024-11-05",
            ],
        )
        parts = [
            f"Target: {target.target}",
            f"Base URL: {target.base_url or '(not a URL)'}",
            "",
            f"Delegate this to the `{spec.name}` subagent if the Task tool is available to you; "
            "otherwise do the work yourself, following the same instructions.",
            "",
        ]
        if register == FULL:
            parts += [
                f"What earlier stages found (also on disk at {context_file}):",
                "```json",
                json.dumps(self._prompt_findings(target), indent=2, default=str),
                "```",
                "",
            ]
        elif target.stages:
            # Not pasted, but not withheld: the agent can Read the file, which
            # costs it one turn and costs the prompt nothing.
            parts += [
                f"What earlier stages found is on disk at {context_file}; read it if you need it.",
                "",
            ]
        parts += [
            f"Workspace you may write into: {workspace}",
            f"Put any generated artefacts under {self._artifacts_dir(workspace, target)} "
            "(this address's own directory; other addresses may be worked beside it).",
            f"MCP protocol versions to offer, newest first: {', '.join(protocol)}.",
        ]
        token_ref = self.settings.str_("mcp.discovery.auth.bearer_token_ref")
        if token_ref:
            parts.append(
                f"A bearer token for this target is configured by reference ({token_ref}); "
                "initialize-claude-ai resolves it during verification. Do not try to read it yourself."
            )
        certificate = self._certificate_brief(target)
        if certificate:
            parts.append(certificate)
        if register != MINIMAL:
            auth_wall = self._auth_wall_brief(stage, target)
            if auth_wall:
                parts.append(auth_wall)
            existing = self._existing_brief(stage, target)
            if existing:
                parts.append(existing)
            kind = self._api_kind_brief(stage, target)
            if kind:
                parts.append(kind)
        if register == FULL and isinstance(target.due_diligence, DueDiligence):
            parts.append(
                "Tier-1 checklist so far (from the stages before you -- extend it, do not repeat "
                "work it already covers):\n```json\n"
                + json.dumps(target.due_diligence.to_dict(), indent=2, default=str)
                + "\n```"
            )
        if target.verified:
            parts.append(
                "A working server has already been found for this target: "
                + ", ".join(c.label for c in target.verified)
                + ". Only report something better."
            )

        budget = spec.turn_budget(
            self.config.int_("max_turns", 24),
            override=self.config.int_("max_turns_override", 0),
        )
        if budget:
            parts.append(
                f"Turn budget: {budget}. Group your requests -- one command that asks about several "
                "paths at once, rather than a turn each -- and spend no turn on exploration that "
                "cannot change what you report. Running out of turns halfway through produces a "
                "worse answer than a narrower search that finished and said so."
            )
        if self.policy.enabled and self.policy.tier(stage) <= 1:
            keys = ", ".join(f"`{key}`" for key, _letter, _title in CHECKLIST)
            if register == MINIMAL:
                parts.append(
                    f"Include a `due_diligence` object keyed {keys}, each entry either "
                    '`{"done": true, "detail": "...", "evidence": ["..."]}` or '
                    '`{"unavailable": "why"}`.'
                )
            else:
                parts.append(
                    "Your JSON block must include a `due_diligence` object with one entry per "
                    f"checklist step -- keys {keys} -- each "
                    '`{"done": true|false, "detail": "...", "evidence": ["..."]}`, or '
                    '`{"unavailable": "why it could not be run here"}`. A step without evidence is '
                    "recorded as partial, and an absent step blocks the package and bridge stages."
                )
        if plan.correction:
            parts.append(
                "\nThis stage has been attempted before and is being retried, not started over. "
                "Read this before you plan anything:\n" + plan.correction
            )
        if plan.refused:
            parts.append(
                "\nAn earlier attempt at this stage did not reach a model at all, so this brief is "
                "deliberately short. Keep your own working short too: plain words, no long lists "
                "of URLs in your reply, and only the findings that bear on the answer."
            )
        parts.append(
            "\nEnd your reply with the single fenced JSON block "
            + ("your skill specifies" if register != MINIMAL else "described above")
            + ". Every candidate you list will be re-verified by an independent handshake before "
            "it is accepted, so report what you actually observed."
        )
        return "\n".join(parts)

    def _prompt_findings(self, target: TargetOutcome) -> dict[str, Any]:
        """Earlier findings, small enough to belong in a prompt.

        The deterministic pipeline records everything it did, which is right for
        a report and wrong for a brief: one endpoint stage produces a list of a
        hundred and sixty URLs it asked about, and pasting that into the next
        stage's prompt spends the context window on data no reader -- human or
        model -- will act on, in the shape most likely to be mistaken for a
        scan. A count and a few examples say the same thing in one line, and
        the full list is still in the context file on disk.
        """
        limit = max(0, self.config.int_("max_list_items", 8))
        summarised = {key: _summarise_list(value, limit) for key, value in target.all_findings().items()}
        return clip(summarised, self.config.int_("max_prompt_findings_chars", 4000))


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------


def _human_part(text: str) -> str:
    """The sentence a person needs out of a runtime's JSON answer.

    A failing ``claude -p --output-format json`` returns a whole result object,
    and the reason lives in one field of it. Squeezing the object onto a console
    line truncates it mid-key, so what reached the report was::

        ackground":0,"foreground":0,"unset":0},"started_in_background":0,...

    -- 29 times, while the field that mattered said "The model claude-opus-5-5
    is not available on your vertex deployment. Try --model to switch to
    claude-opus-5." The remedy was in the run's own output and never once
    legible.

    Falls back to the original text, so a plain error message is untouched.
    """
    flat = str(text or "").strip()
    found = json_message(flat)
    return " ".join(found).strip() if found else flat


def _one_line(text: str, limit: int = 120) -> str:
    """A reason squeezed onto a console line, without wrapping the terminal.

    The text is reduced to its human part first: truncating a JSON object at
    120 characters produces a fragment of somebody else's schema, which is how
    a fully explained failure became unreadable 29 times in one run.
    """
    flat = " ".join(_human_part(text).split())
    return flat if len(flat) <= limit else flat[: limit - 3].rstrip() + "..."


#: Why a stage ended, in the words the console line and the report use. Keyed
#: by the failure class, so "[fallback]" on a stage line is never the whole
#: story: the operator is told which of these four happened, and each one has a
#: different thing to do about it.
_FAILURE_SUMMARY: dict[str, str] = {
    UNSUPPORTED_MODEL: "the agent runtime cannot serve the configured model",
    UNUSABLE: "the agent runtime cannot serve any request",
    REFUSAL: "the request was rejected before a model read it",
    TIMEOUT: "the agent ran out of wall clock",
    TRANSIENT: "the service was unavailable",
    OTHER: "the agent could not settle it",
}

#: What an operator can actually do about each class. A fallback that does not
#: say this leaves them with "[fallback]" and no next move.
_FAILURE_REMEDY: dict[str, str] = {
    UNSUPPORTED_MODEL: (
        "The agent runtime's own Claude Code is too old for the configured model; the other runtime, "
        "or this one's default model, was tried instead. Update the runtime, or set mcp.agent.model "
        "(INITIALIZE_CLAUDE_AI_AGENT_MODEL) to a model it serves"
    ),
    UNUSABLE: (
        "The failure is about the installation -- a model that is not enabled, or a credential "
        "the agent runtime cannot load -- not about this address, so more attempts cannot help "
        "and agents were stopped for the rest of the run. Fix it (the final verdict names the "
        "command when it is known), then re-run phase 4 for the addresses the verdict lists"
    ),
    REFUSAL: (
        "Every attempt was rejected before a model saw the brief, so the agent never got to work. "
        "Set mcp.agent.retry_models (or INITIALIZE_CLAUDE_AI_AGENT_RETRY_MODELS) to a model that "
        'will take it, or mcp.agent.prompt_register = "plain" to start every stage from the '
        "shorter brief"
    ),
    TIMEOUT: (
        "Every attempt ran past mcp.agent.timeout_seconds. Raise it "
        "(INITIALIZE_CLAUDE_AI_AGENT_TIMEOUT_SECONDS) for a slow target, or lower "
        "mcp.agent.max_turns so the agent commits to an answer sooner"
    ),
    TRANSIENT: (
        "Every attempt hit a service that was unavailable or rate-limited. This one is worth simply running again"
    ),
    OTHER: (
        "Raise mcp.agent.max_attempts (--agent-max-attempts N) to give the agent more room, or set "
        "mcp.agent.fallback_to_traditional = false to see its own verdict instead of this one"
    ),
}


def _failure_summary(kind: str) -> str:
    return _FAILURE_SUMMARY.get(kind, _FAILURE_SUMMARY[OTHER])


def _summarise_list(value: Any, limit: int) -> Any:
    """Collapse a long list into a count and a few examples.

    Applied to findings on their way into a prompt, never to findings on their
    way into the report: the report is where the full list belongs.
    """
    if limit <= 0 or isinstance(value, (str, bytes, Mapping)) or not isinstance(value, Sequence):
        return value
    items = list(value)
    if len(items) <= limit:
        return items
    return {
        "count": len(items),
        "examples": items[:limit],
        "note": f"{len(items) - limit} more, in the context file on disk",
    }


def _rank(stage: StageOutcome) -> tuple[int, int, int, int]:
    """How far an attempt got, for comparing one against another.

    An endpoint that answered 401 outranks any number of candidates that did
    not answer at all: it is a real server, and it is the one that will be
    written out.
    """
    return (
        len(stage.verified),
        sum(1 for c in stage.candidates if c.needs_credential),
        len(stage.candidates),
        0 if stage.error else 1,
    )


def _best_of(best: StageOutcome | None, stage: StageOutcome) -> StageOutcome:
    """Keep the attempt that got furthest, so retrying can never lose ground.

    A second attempt that comes back empty must not erase a first one that at
    least produced candidates: retrying is meant to be free, and it is only free
    if the worst case is the result you already had.
    """
    if best is None or _rank(stage) > _rank(best):
        return stage
    return best


def _transport_words(candidate: Candidate, settings: Any) -> str:
    """The failure text that says ``candidate`` never reached the service; "" when none (shared markers)."""
    from .traditional import TRANSPORT_FAILURE_MARKERS

    shake = candidate.handshake
    if shake is None or settings is None:
        return ""
    markers = settings.str_list("mcp.verification.transport_failure_markers", list(TRANSPORT_FAILURE_MARKERS))
    texts = [shake.error or "", *[str(r.get("detail") or "") for r in shake.contract or [] if not r.get("passed")]]
    return next((t for t in texts if any(m and m.lower() in t.lower() for m in markers)), "")


def _correction(
    stage: StageOutcome, attempt: int, ruled_out: Mapping[str, str] | None = None, settings: Any = None
) -> str:
    """What to tell the next attempt so it is not a re-run of this one.

    Specific failures only. "Try again" produces the same answer; "the package
    you named does not exist on the registry" produces a different one.
    ``ruled_out`` is the target's record of endpoints already judged not to be
    its own, so the agent does not spend the retry proposing one of them again.
    """
    lines = [f"Attempt {attempt} did not settle this stage. What happened:"]
    if stage.error == "agent-failed":
        lines.append(f"- the run itself failed: {_one_line(stage.detail, 300)}")
    else:
        if stage.detail:
            lines.append(f"- {_one_line(stage.detail, 300)}")
        for candidate in stage.candidates[:5]:
            shake = candidate.handshake
            error = shake.error if shake else ""
            if error:
                lines.append(f"- {candidate.label} did not answer: {_one_line(error, 300)}")
            if candidate.verified and candidate.unsettled:
                lines.append(
                    f"- {candidate.label} answered and was verified, but did not settle the stage: "
                    f"{_one_line(candidate.unsettled, 300)}. Keep it and fix exactly that -- give the tool a name or a "
                    "description that says what it does -- rather than starting over"
                )
            transport = _transport_words(candidate, settings)
            if transport:
                # The machine's trust or network, not the server: gitlab.example.net's
                # agents proposed mcp-graphql again after "fetch failed".
                lines.append(
                    f"- {candidate.label} never reached the service ({_one_line(transport, 160)}): that is this "
                    "machine's trust store or network, not the server. The program names the runtime's trust "
                    "variable itself (mcp.trust.runtimes) and retries; do not send the same launch back "
                    "unchanged, and do not blame the server for it"
                )
            # What the verifier concluded, not only what the server said. "It
            # started without X and listed nothing" and "with X set to a
            # stand-in value it still listed nothing" are the findings that
            # tell the agent the variable is not the missing piece -- without
            # them it proposed the same launch for an hour.
            if shake and shake.credential_test and shake.credential_test != error:
                lines.append(f"- {candidate.label}: {_one_line(shake.credential_test, 300)}")
            if any(str(note).startswith(REUSED_VERDICT) for note in candidate.notes):
                lines.append(
                    f"- {candidate.label} was proposed with exactly the launch already judged "
                    "above, so it was not started again. An identical proposal will get the "
                    "identical verdict; change what was wrong with it or propose something else."
                )
        for note in [str(n) for n in _as_list(stage.findings.get("rejected_unsafe"))][:3]:
            lines.append(f"- one candidate was discarded before it was tried: {_one_line(note, 300)}")
        # The correctable one. A candidate that works but cannot be *registered*
        # is usually a single missing environment variable, and the agent is the
        # only thing that can put it there -- so it has to be told, in the one
        # place a retry actually reads. Left out of this list, the verdict
        # arrives after the run and costs the whole address.
        for note in [str(n) for n in _as_list(stage.findings.get("not_registrable"))][:3]:
            lines.append(
                f"- one candidate could not be registered and so was not verified: "
                f"{_one_line(note, 300)}. Put the target's address in the entry -- the endpoint "
                "URL in an `env` variable the server reads, or an argument -- and propose it again."
            )
        # An endpoint that answered but is not this target's: a search hit or a
        # registry entry whose name matched. It cannot be fixed by an env var,
        # and proposing it again gets the same verdict -- so say it is closed.
        for url, why in list((ruled_out or {}).items())[:3]:
            lines.append(
                f"- {redact.redact(url)} was ruled out as this target's MCP endpoint: "
                f"{_one_line(redact.redact(why), 300)}. "
                "Do not propose it again. A name match in a registry or a search result is not "
                "evidence; propose it only with something that ties it to the target (its host "
                "or organisation, the vendor publishing it, the server naming the target), "
                "otherwise look elsewhere."
            )
    lines.append(
        "Do not send those same candidates back unchanged. Either fix what was wrong with them -- "
        "a package name that does not exist, a transport the server does not speak, a missing "
        "environment variable, a launch command that needed a shell -- or look somewhere you have "
        "not looked yet: another registry, the vendor's own documentation, a different path on the "
        "target. Report an empty result only once you have genuinely run out of leads, and say "
        "which ones you exhausted."
    )
    return "\n".join(lines)


def _replace_tree(source: Path, target: Path) -> None:
    """Make ``target`` a fresh copy of ``source``, never a half-written one.

    Copied beside the target under a unique name and renamed into place, so a
    reader -- an agent of another initialize-claude-ai run sharing this state
    directory -- sees the old tree or the new one and nothing in between.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex[:12]
    staging = target.parent / f".{target.name}.new-{token}"
    retired = target.parent / f".{target.name}.old-{token}"
    shutil.copytree(source, staging)
    try:
        if target.exists():
            target.rename(retired)
        try:
            staging.rename(target)
        except OSError:
            if retired.exists() and not target.exists():
                retired.rename(target)  # the old guides beat none at all
            raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        shutil.rmtree(retired, ignore_errors=True)


def _merge(agent_stage: StageOutcome, fallback: StageOutcome, reason: str, *, settled: bool = False) -> StageOutcome:
    """Combine an inconclusive agent stage with the deterministic retry.

    ``settled``: the agent did settle the stage, and the deterministic stage
    ran only to try what the run can build itself (the generated bridge) --
    so its verified servers are kept, and the operator is not told to give
    the agent more attempts.
    """
    merged = StageOutcome(
        stage=fallback.stage,
        mode=Mode.FALLBACK,
        status=fallback.status,
        detail=fallback.detail,
        candidates=[*agent_stage.candidates, *fallback.candidates],
        verified=[*(agent_stage.verified if settled else []), *fallback.verified],
        # Recomputed from the merged candidate list by
        # :meth:`StageOutcome.note_provisional` when the target absorbs this
        # stage; carried anyway so that a caller reading the stage before then
        # does not see an empty list where the agent found an endpoint.
        provisional=[*agent_stage.provisional, *fallback.provisional],
        findings={**agent_stage.findings, **fallback.findings},
        remediation=[*agent_stage.remediation, *fallback.remediation],
        artifacts=[*agent_stage.artifacts, *fallback.artifacts],
        error=fallback.error or agent_stage.error,
    )
    if settled:
        merged.findings["also_tried"] = reason
        return merged
    merged.findings["agentic_inconclusive"] = reason
    kind = str(agent_stage.findings.get("agent_failure_kind") or "")
    merged.add_remediation(
        f"The {fallback.stage} agent did not settle this stage ({_one_line(reason, 200)}); "
        "the deterministic pipeline finished it instead. " + _FAILURE_REMEDY.get(kind, _FAILURE_REMEDY[OTHER]) + "."
    )
    return merged


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (str, bytes, Mapping)):
        return [value]
    if isinstance(value, Sequence):
        return list(value)
    return [value]


def _as_mapping(value: Any) -> dict[str, Any]:
    return {str(k): v for k, v in value.items()} if isinstance(value, Mapping) else {}


def _as_argv(value: Any) -> list[str]:
    """Accept a list of words or a shell-ish string, as models produce both."""
    if value is None:
        return []
    if isinstance(value, str):
        try:
            return shlex.split(value)
        except ValueError:
            return value.split()
    if isinstance(value, Sequence):
        return [str(item) for item in value if str(item) != ""]
    return []


#: Characters that only mean anything to a shell. Install steps are executed as
#: argv with no shell, so a step carrying one of these was written as a shell
#: line and cannot do what it says: the metacharacter would be passed to the
#: command as a literal argument.
_SHELL_SYNTAX = re.compile(r"[|&;<>`$()\n]")

#: Punctuation that ends a *word* in a sentence and never in an argument
#: vector: a trailing comma, or a full stop followed by a space or the end of
#: the step. ``pip install .`` keeps its dot (no preceding letter), and
#: ``python3.11`` and ``example.com`` keep theirs (no following break).
_PROSE = re.compile(r"[a-zA-Z],|[a-zA-Z]\.(?:\s|$)")


def _install_steps(raw: Any) -> tuple[list[list[str]], list[str]]:
    """Turn the agent's ``install`` list into argv, dropping what cannot run.

    Each step is executed on its own, as argv, with no shell -- so a pipeline, a
    redirection or a half-sentence is not a step that happens to fail, it is a
    step that was never runnable. Dropping it here is the difference between
    rejecting a candidate on its merits and rejecting it because we ran the
    agent's prose. What this cannot catch is a well-formed command that is
    simply wrong; that is what the verifier's own error reporting is for.
    """
    steps: list[list[str]] = []
    notes: list[str] = []
    for entry in _as_list(raw):
        argv = _as_argv(entry)
        if not argv:
            continue
        problem = _unrunnable(argv)
        if problem:
            # The command only -- an argument may name a host or a path the
            # operator would rather not see echoed into a report.
            notes.append(f"dropped a prerequisite step ({argv[0]}): {problem}")
            continue
        steps.append(argv)
    return steps, notes


def _unrunnable(argv: list[str]) -> str:
    """Why this argv cannot be executed as given, or "" if it can be tried."""
    command = argv[0]
    if _SHELL_SYNTAX.search(" ".join(argv)):
        return "it uses shell syntax, and steps run without a shell"
    if "=" in command and not command.startswith("/"):
        return "it sets an environment variable instead of naming a command"
    if command.startswith("-"):
        return "it starts with an option rather than a command"
    if _PROSE.search(" ".join(argv)):
        # A sentence survives shlex.split as a perfectly well-formed argv whose
        # word zero is an article, so build.example.net reported "prerequisite needs
        # the, which is not installed" and lost the candidate to it. Sentence
        # punctuation is the evidence, rather than a list of English words: a
        # command does not end a word with a comma or a full stop, and a rule
        # made of vocabulary would only ever be right about one language.
        return "it reads as a sentence rather than a command"
    return ""


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return default


#: The credential patterns are not re-exported: this module asks
#: :mod:`.secret_policy`'s predicates, never a pattern, because the patterns are
#: configured (``[mcp.secret_policy]``) and a copy imported here would keep the
#: packaged one -- one rule, two strengths, which is what the module prevents.
__all__ = [
    "AgenticPipeline",
    "SAFE_REFERENCE_SCHEMES",
]


#: Said where a server's own words would go when it printed none, so a line never
#: ends in a dangling "--".
_NOTHING_SAID = "(the server printed nothing)"


def _strings_in(value: Any) -> list[str]:
    """The string values of a mapping an agent wrote (env, headers); [] for anything else."""
    return [str(v) for v in value.values()] if isinstance(value, Mapping) else []
