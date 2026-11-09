"""Phase 4 in traditional mode -- the same four stages, no model in the loop.

This is the fallback path, and it is deliberately *not* a stub: it does real
concurrent endpoint probing, real registry search, real bridge generation and
real rule-based strategy synthesis.  Everything it proposes goes through the
same verifier the agentic path uses, so the two modes are directly comparable --
the agentic path is smarter about *where to look*, not about what counts as
success.

Discovery begins agentic unless ``--no-agent`` (``mcp.agentic = false``) selects
this pipeline up front, and otherwise this is what any stage falls back to: when
phases 1-3 have not finished, when there is no credential or runtime, on a dry
run, or when the agent comes back empty or errors. The fallback is per stage, so
one run can use both paths.

Two gates in this module are shared rather than private, and the agentic loop
calls both: :meth:`TraditionalPipeline.discard_unregistrable`, which drops a
candidate the registration rules would refuse, and
:meth:`TraditionalPipeline.heal_or_discard`, which drops one whose ``.mcp.json``
entry will not start -- after trying to repair it, with the agent first where
there is one.  They live beside each other because they answer the same shape of
question: "we found a server" and "we can hand this server to the client" are
different, and a stage that settles on the first while failing the second closes
the tiers beneath it and leaves nothing behind.

The tier policy applies here in full (see :mod:`.policy`). The endpoint stage
carries out all four steps of the tier-1 checklist and records what each of them
did; the package and bridge stages are gated on that record and will refuse to
run while a native endpoint exists or while a step was never performed. Step D
-- semantic grounding -- is the one step that cannot be identical in the two
modes: an agent searches the web, and this pipeline does the deterministic
equivalent, reading the pages a vendor announces an MCP endpoint on and asking
the official registry, which is an index of exactly that question.
"""

from __future__ import annotations

import concurrent.futures
import os
import shlex
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

from ..core import redact, trust_store
from ..core.context import RunContext
from ..core.fileio import write_generated
from ..core.logging_setup import get_logger
from ..core.report import Status
from . import api_kinds, apispec, known_good, login_check, login_methods, pathgen
from .address import parse as parse_address
from .bridge import BridgeBuilder
from .existing import ExistingRegistrations
from .grounding import (
    answered_as_mcp,
    endpoint_hosts,
    gateway_marker,
    hosting_namespaces_of,
    identity_rule_of,
    locations_of,
    points_elsewhere,
    same_service,
    server_names_target,
)
from .grounding import assess as ground
from .policy import DueDiligence, TierPolicy
from .probes import TLS_ERROR_MARKERS, WEBSOCKET_HINT, McpProber
from .registry_client import RegistryClient, dedupe, relevance_of
from .types import (
    Candidate,
    Handshake,
    Mode,
    Stage,
    StageOutcome,
    TargetOutcome,
    Transport,
    clip,
    proves_access,
    slugify,
    waiting_remedy,
    waits_for_credential,
)
from .verifier import CandidateVerifier

log = get_logger("mcp.traditional")

#: Ranks run from 1 (a mandated checklist path on the primary origin) into the
#: low thousands (a template on the last alternate origin); see :mod:`.pathgen`.
_RANK_SCALE = 5000.0


_REIDENTIFY_LOCK = threading.Lock()


def _retry_after(response: Any) -> float:
    """Seconds a ``Retry-After`` header asks for (a number; a date is not read); 0 when none."""
    headers = getattr(response, "headers", None) or {}
    try:
        value = headers.get("Retry-After") or headers.get("retry-after") or ""
    except Exception:  # noqa: BLE001 - an odd headers object asks for nothing
        return 0.0
    try:
        return max(0.0, float(str(value).strip()))
    except ValueError:
        return 0.0


@dataclass
class ProbeRun:
    """What one sweep of the candidate list actually did.

    The checklist has to be written from evidence rather than intent, so the
    sweep reports the URLs it reached as well as the ones that answered: step A
    is a claim about coverage, and coverage is only knowable from the former.
    """

    verified: list[Candidate] = field(default_factory=list)
    attempted: list[Candidate] = field(default_factory=list)
    auth_walls: list[str] = field(default_factory=list)
    websockets: list[str] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)


class TraditionalPipeline:
    """Deterministic four-stage discovery for one target at a time."""

    mode = Mode.TRADITIONAL

    def __init__(self, ctx: RunContext) -> None:
        self.ctx = ctx
        self.settings = ctx.settings
        self.console = ctx.console
        self.discovery = ctx.settings.section("mcp.discovery")
        self.prober = McpProber(ctx)
        self.verifier = CandidateVerifier(ctx, self.prober)
        self._doctor = None
        #: Addresses worked side by side share this pipeline; the lazily built
        #: doctor is the one piece of it built on first use.
        self._doctor_lock = threading.Lock()
        self.registry = RegistryClient(ctx)
        self.bridges = BridgeBuilder(ctx)
        self.policy = TierPolicy(ctx)
        #: Every WebSocket route judged this run: URL -> "" when it showed a
        #: sign of MCP, else why not. Shared by the probe and the vetting pass,
        #: so a route is asked once whichever mode reported it.
        self._sockets: dict[str, str] = {}
        #: (address, stage) -> candidates built from this machine's existing
        #: registrations, tried by :meth:`reuse` and folded into the stage by
        #: :meth:`merge_reused`. See :mod:`.existing`.
        self._reused: dict[tuple[str, str], list[Candidate]] = {}
        #: address -> the API kinds found for it when no endpoint stage of this
        #: pipeline looked (an agent worked tier 1). See :meth:`api_kinds_for`.
        # Shared by every pipeline of the run (the agentic one's fallback, the
        # job learner the verifier asks): an API kind one of them detected is
        # known to all -- sd.example.com's customer API was detected, and the
        # bridge stage was still settled as if it had not been.
        self._api_kinds: dict[str, dict[str, dict[str, str]]] = ctx.state.setdefault("mcp_api_kinds_found", {})
        #: ``{url: why not}`` for API kinds another mode recorded; see _confirmed_api_kinds.
        self._confirmed_kinds: dict[str, str] = {}

    # -- entry point -----------------------------------------------------------

    def run(self, target: str, stages: Sequence[str]) -> TargetOutcome:
        """Work one target through the stages with no agent involved.

        The fallback behind every agentic stage, and the whole of discovery
        under ``--no-agent``. It enforces the same tier gate and produces the
        same :class:`TargetOutcome`, so which pipeline ran changes how hard the
        search looked, never what the result is allowed to claim.
        """
        outcome = TargetOutcome(target=target, base_url=parse_address(target).url)
        handlers: dict[str, Callable[[TargetOutcome], StageOutcome]] = {
            Stage.ENDPOINT: self.stage_endpoint,
            Stage.PACKAGE: self.stage_package,
            Stage.BRIDGE: self.stage_bridge,
            Stage.STRATEGY: self.stage_strategy,
        }
        exhaustive = self.settings.bool_("mcp.exhaustive", False)

        for name in stages:
            self._work(name, outcome, handlers, exhaustive)

        self.reconcile(outcome, exhaustive)
        # A stage refused on the strength of an endpoint that has since been
        # ruled out is owed its turn -- see reopenable().
        reopened = self.reopenable(outcome)
        for name in reopened:
            self.withdraw_refusal(outcome, name)
            self._work(name, outcome, handlers, exhaustive, reopened=True)
        if reopened:
            self.reconcile(outcome, exhaustive)
        return outcome

    def _work(
        self,
        name: str,
        outcome: TargetOutcome,
        handlers: Mapping[str, Callable[[TargetOutcome], StageOutcome]],
        exhaustive: bool,
        *,
        reopened: bool | str = False,
    ) -> None:
        """One stage of :meth:`run`: gate, run, check, absorb, vet."""
        target = outcome.target
        handler = handlers.get(name)
        if handler is None:
            log.warning("unknown stage %r, skipping", name)
            return
        self.begin_stage(name, outcome)
        blocked = self.gate(name, outcome) or self.settled(name, outcome, exhaustive)
        if blocked is not None:
            outcome.stages.append(self.recheck_blocked(name, outcome, blocked, exhaustive))
            return
        # A stage is dozens of HTTP probes against someone else's host, at
        # whatever timeout they impose. Name the one in flight, so a slow
        # target is visibly a slow target rather than a stuck program.
        self.console.progress(f"{target} :: {name:<14}", f"tier {self.policy.tier(name)}")
        started = time.monotonic()
        try:
            stage = self.reuse(name, outcome, exhaustive) or (
                self.recheck_stage(name) if self.rechecking else handler(outcome)
            )
            self.merge_reused(stage, outcome)
        except Exception as exc:  # noqa: BLE001 - a stage must not kill the phase
            log.exception("stage %s failed", name)
            stage = StageOutcome(
                stage=name,
                mode=self.mode,
                status=Status.FAILED,
                detail=f"unexpected error: {exc}",
                error=type(exc).__name__,
            )
        stage.duration = time.monotonic() - started
        self.finish_stage(stage, outcome, exhaustive, reopened=reopened)

    # -- what every stage does, in either mode ----------------------------------

    #: Why a reopened stage ran, by what reopened it (``StageOutcome.findings["reopened"]``).
    REOPENED_BY_POLICY = "run after the endpoint that had closed it was ruled out as not this target's"
    REOPENED_BY_WITHDRAWAL = "run after the server that had settled the address was withdrawn by a later check"

    def begin_stage(self, name: str, outcome: TargetOutcome) -> None:
        """What every stage does first, in either mode -- before its gate, before anything is judged.

        Below the endpoint tier, what the address is (its product and API kinds)
        is learned, and a learner is left for the verifier
        (``ctx.state["mcp_job_learners"]``), so every verdict reached for the
        address knows its job whichever path leads to it. Agent mode's fallback
        called the deterministic stage directly and skipped this; one place for
        it is what keeps the two modes from drifting apart again.
        """
        if name == Stage.ENDPOINT:
            return
        host = (parse_address(outcome.target).host or "").lower()
        if host:
            self.ctx.state.setdefault("mcp_job_learners", {})[host] = lambda: self.know_job(outcome)
        self._learn_job(outcome)

    def finish_stage(
        self,
        stage: StageOutcome,
        outcome: TargetOutcome,
        exhaustive: bool,
        *,
        reopened: bool | str = False,
        agent_repair: Any = None,
    ) -> None:
        """What every stage does last, in either mode: discard, heal, absorb, vet.

        A stage may not settle on something that will be thrown away at
        registration. Left unchecked, one unregistrable candidate closes every
        tier below it and the address ends with nothing: exactly what
        `@ateam-ai/mcp` -- a package unrelated to Open Build Service and naming
        no address at all -- did to build.opensuse.org and build.example.net,
        skipping the bridge stage that was the real answer. A candidate whose
        entry will not start is no more an answer than one that cannot be
        written. ``reopened`` is ``True`` (reopened by the tier policy) or the
        reason the stage was owed its turn.
        """
        self.discard_unregistrable(stage, outcome)
        self.heal_or_discard(
            stage, outcome, agent_repair=agent_repair, provisional_limit=self.provisional_limit(exhaustive)
        )
        outcome.stages.append(stage)
        self.absorb(outcome, stage, exhaustive)
        if reopened:
            stage.findings["reopened"] = reopened if isinstance(reopened, str) else self.REOPENED_BY_POLICY
        self.vet_record(outcome, exhaustive)

    # -- registrations this machine already has --------------------------------

    def recheck_blocked(
        self, name: str, outcome: TargetOutcome, blocked: StageOutcome, exhaustive: bool = False
    ) -> StageOutcome:
        """A stage the tier policy closed still judges what this machine already registered at its tier.

        The policy forbids *new* work below a native endpoint -- a package to
        install, a bridge to write. It never meant to hide what is already
        here: smelt.example.net's connected ``mcp-graphql`` was not looked at once
        another host's endpoint closed the package stage, so nothing compared
        the two and the writer replaced the one that worked. So the existing
        registrations of the closed tier are re-checked exactly as a running
        stage would (:meth:`reuse`, then the same registration and start
        gates), and folded into the outcome; the stage stays a policy skip
        unless one of them proved itself. Only a policy block is re-checked --
        a stage skipped because the target is already settled needs nothing
        more (``mcp.discovery.reuse.recheck_when_blocked``).
        """
        reuse_cfg = self.discovery.section("reuse")
        by_policy = "blocked_by_policy" in blocked.findings and reuse_cfg.bool_("recheck_when_blocked", True)
        # Settled by a server that only *waits* for a credential: nothing
        # proved it, so it cannot hide what is already here. gitlab.example.net's
        # package stage found mcp-graphql waiting for a token; the bridge
        # Claude Code showed connected -- the entry that last worked -- was in
        # the bridge tier, never looked at, and replaced.
        # (mcp.discovery.reuse.recheck_when_settled_unproven)
        by_unproven = bool(blocked.findings.get("settled_unproven")) and reuse_cfg.bool_(
            "recheck_when_settled_unproven", True
        )
        if name not in (Stage.PACKAGE, Stage.BRIDGE) or not (by_policy or by_unproven):
            return blocked
        try:
            found = self.reuse(name, outcome, exhaustive)
        except Exception:  # noqa: BLE001 - a re-check must not kill the phase
            log.exception("re-checking the existing registrations of the %s stage failed", name)
            return blocked
        stage = found or blocked
        if found is not None:
            for key in ("blocked_by_policy", "settled_by", "settled_unproven"):
                if key in blocked.findings:
                    found.findings[key] = blocked.findings[key]
            found.remediation[:0] = [r for r in blocked.remediation if r not in found.remediation]
        self.merge_reused(stage, outcome)
        if not stage.candidates:
            return stage
        self.discard_unregistrable(stage, outcome)
        self.heal_or_discard(stage, outcome, provisional_limit=self.provisional_limit(exhaustive))
        if not stage.verified:
            stage.note_provisional(
                self.provisional_limit(exhaustive),
                prefer_detected_api=self.settings.bool_("mcp.discovery.auth.prefer_detected_api", True),
            )
        self.absorb(outcome, stage, exhaustive)
        if by_unproven and not by_policy and stage.verified and not exhaustive:
            # What settled the descent waited on a credential and proved
            # nothing; the entry already here works. One server, one entry:
            # the unproven one gives its place up rather than being written
            # beside it.
            dropped = [c for c in outcome.provisional if not proves_access(c) and not c.worked_before]
            outcome.provisional = [c for c in outcome.provisional if c not in dropped]
            for candidate in dropped:
                log.info(
                    "%s: %s settled the descent without proving anything; the existing %s works and takes its place",
                    outcome.target,
                    candidate.label,
                    _naming(stage.verified),
                )
        kept = [c.label for c in [*stage.verified, *stage.provisional]]
        if kept:
            stage.detail = f"{blocked.detail}; the existing {', '.join(kept)} was re-checked all the same" + (
                " and still works" if stage.verified else " and waits for a credential"
            )
        log.info(
            "%s: the %s stage is closed (%s); re-checked %d existing registration(s): %s",
            outcome.target,
            name,
            "by policy" if by_policy else "settled by a server nothing proved",
            len(stage.candidates),
            ", ".join(c.label for c in stage.candidates),
        )
        return stage

    def reuse(self, name: str, outcome: TargetOutcome, exhaustive: bool = False) -> StageOutcome | None:
        """Try this machine's existing registrations for ``name``, at their own tier.

        Returns a finished stage only when one of them proved itself and the
        stage may stop there: at tier 2 or 3, outside ``--exhaustive-mcp``. The
        tier-1 checklist always runs in full -- the record it writes is what
        every gate below reads -- so an endpoint found this way is folded into
        the endpoint stage by :meth:`merge_reused` instead of replacing it.
        Shared by both pipelines, which is why it lives here.
        """
        if name not in (Stage.ENDPOINT, Stage.PACKAGE, Stage.BRIDGE):
            return None
        # The entry that last worked here first (known_good), then what is registered now.
        remembered = known_good.candidates(self.ctx, outcome.target, name)
        current = ExistingRegistrations.for_run(self.ctx).for_target(outcome.target, name)
        # ...and the servers an earlier run left this address waiting with
        # beside the one it wrote: a credential supplied since may prove one.
        from . import alternatives

        kept = [
            c
            for c in alternatives.candidates(self.ctx, outcome.target, name)
            if all(c.full_command() != o.full_command() or c.url != o.url for o in [*remembered, *current])
        ]
        candidates = [*remembered, *current, *kept]
        host = (parse_address(outcome.target).host or "").lower()
        # An entry's files are its own: one gone is restored from the copy kept
        # when it last worked, and one still in the run's scratch area is moved
        # out now, while it is there -- gitlab-example-net's working bridge was
        # deleted from work/ and the entry could only be called broken.
        from .writer import McpWriter

        keeper = McpWriter(self.ctx) if candidates else None
        for candidate in candidates:
            if candidate.kind != "stdio" or keeper is None:
                continue
            known_good.restore_missing(self.ctx, outcome.target, candidate)
            # A file it names that is not there -- an older, nested layout --
            # is found again among this program's own folders before it is
            # judged: sd-example-com's working bridge was called broken for a
            # path one folder too deep. (mcp.registration.repoint_before_trial)
            if self.settings.bool_("mcp.registration.repoint_before_trial", True):
                keeper.repoint_missing(candidate)
            if self.settings.bool_("mcp.registration.adopt_existing_files", True):
                keeper.own_files(candidate, candidate.name or candidate.label)
        for candidate in candidates:
            self.refresh_generated_script(candidate, outcome)
            api_kinds.give_held_credential(self.ctx, candidate, host)
        self._reused[(outcome.target, name)] = candidates
        if not candidates:
            return None
        log.info(
            "%s: trying %d existing registration(s) at the %s stage: %s",
            outcome.target,
            len(candidates),
            name,
            ", ".join(c.label for c in candidates),
        )
        # The entry that last worked is tried first, never *instead of* what is
        # registered now: build-opensuse-org's remembered client bridge verified,
        # the trials stopped, and the osc-mcp entry the operator had was never
        # asked. Both are judged; proof decides between them, not order.
        # (mcp.known_good.try_current_too)
        both = bool(remembered and current) and self.settings.bool_("mcp.known_good.try_current_too", True)
        verified = self.verifier.verify_all(
            candidates,
            stop_at_first=not exhaustive and not both,
            keep=lambda c: self.registrable(c, outcome),
            memo=outcome.trials,
            why=lambda c: self.why_not_registrable(c, outcome),
        )
        self.note_stale(candidates, outcome)
        for candidate in candidates:
            self.heal_transport(candidate, outcome)
        existing = ExistingRegistrations.for_run(self.ctx)
        for candidate in candidates:
            registration = existing.registration_for(candidate)
            shake = candidate.handshake
            if registration is None or shake is None:
                continue
            connected = self.discovery.section("reuse").bool_(
                "connected_outranks_trial", True
            ) and existing.cli_connected(registration.name)
            if connected:
                # Claude Code connects to it: that is the launch that matters.
                # build-example-net's working client bridge was replaced because
                # this run could not resolve one of its variables.
                outcome.connected_existing.setdefault(registration.name, registration.source)
                if not shake.ok and not candidate.needs_credential:
                    candidate.add_note(
                        "Claude Code reports it connected; this run could not launch it itself "
                        f"({self.verifier.excerpt(shake.error) or 'no handshake'}), which is not a verdict on it"
                    )
                    outcome.answered_existing.setdefault(registration.name, registration.source)
                    continue
            broken = (
                self.verifier.as_claude_runs_it(registration.name, registration.entry, registration.source, candidate)
                if (shake.ok or candidate.needs_credential) and not connected
                else ""
            ) or self.repair_or_mismatch(registration, candidate, outcome)
            if broken:
                # It worked in the run's own launch, not in Claude Code's: it is
                # broken as written, so it is replaced, never kept in place.
                candidate.start_failure = broken
                log.info("%s: the existing %s %s", outcome.target, registration.name, broken)
                if not any(b.get("name") == registration.name for b in outcome.broken_existing):
                    outcome.broken_existing.append(
                        {
                            "name": registration.name,
                            "source": registration.source,
                            "error": broken,
                            "started": bool(shake.ok),
                        }
                    )
                continue
            if shake.ok or candidate.needs_credential:
                outcome.answered_existing.setdefault(registration.name, registration.source)
            elif shake.not_launched:
                # Not launched (a dry run): no verdict on it either way.
                candidate.add_note(f"not launched this run ({shake.not_launched}); not proved, and not broken")
            elif not any(b.get("name") == registration.name for b in outcome.broken_existing):
                outcome.broken_existing.append(
                    {
                        "name": registration.name,
                        "source": registration.source,
                        "error": self.verifier.excerpt(shake.error) or "it did not complete a handshake",
                    }
                )
        if not verified or name == Stage.ENDPOINT or exhaustive:
            return None
        stage = StageOutcome(stage=name, mode=self.mode, status=Status.OK)
        stage.candidates = list(candidates)
        stage.verified = list(verified)
        stage.detail = f"{verified[0].label} is already registered and still works"
        return stage

    def refresh_generated_script(self, candidate: Candidate, outcome: TargetOutcome) -> str:
        """Bring a bridge script this program generated up to its current template; the path refreshed, or "".

        A kept entry runs whatever copy of the script it names: sd-example-com's
        bridge in the project's bridges/ predated the template that says which
        credential variable never arrived, and kept answering a bare 401. Both
        templates are fixed text that read their manifest at start-up, so an
        outdated copy -- recognised by the template's own first lines, with a
        manifest of the schema the current template reads -- is rewritten in
        place. Never in a dry run. (``mcp.discovery.reuse.refresh_generated_scripts``.)
        """
        import json as _json

        from .bridge import _BRIDGE_SOURCE, MANIFEST_SCHEMA
        from .client_bridge import _SOURCE
        from .portability import expand

        if candidate.kind != "stdio" or self.ctx.dry_run:
            return ""
        if not self.discovery.section("reuse").bool_("refresh_generated_scripts", True):
            return ""
        for token in candidate.full_command()[1:]:
            path = Path(expand(str(token)))
            if path.suffix != ".py" or not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                return ""
            for template, manifest_key in ((_SOURCE, "MCP_CLIENT_MANIFEST"), (_BRIDGE_SOURCE, "MCP_BRIDGE_MANIFEST")):
                if text == template or text.splitlines()[:2] != template.splitlines()[:2]:
                    continue
                if manifest_key == "MCP_BRIDGE_MANIFEST":
                    manifest = expand(str(candidate.env.get(manifest_key) or ""))
                    try:
                        schema = _json.loads(Path(manifest).read_text(encoding="utf-8")).get("schema")
                    except (OSError, ValueError):
                        return ""
                    if schema not in (None, MANIFEST_SCHEMA):
                        return ""
                try:
                    write_generated(path, template, executable=os.access(path, os.X_OK))
                except OSError as exc:
                    log.info(
                        "%s: %s is an outdated generated bridge and cannot be refreshed: %s", outcome.target, path, exc
                    )
                    return ""
                candidate.add_note(
                    f"{path.name} was an older copy of the bridge this program generates; "
                    "refreshed to the current one (it reads the same manifest)"
                )
                log.info("%s: refreshed the generated bridge %s to the current template", outcome.target, path)
                return str(path)
        return ""

    def transport_failure(self, candidate: Candidate) -> str:
        """The connection failure a candidate's calls met (``mcp.verification.transport_failure_markers``); "" when none.

        ``TypeError: fetch failed`` from smelt-example-net's mcp-graphql is Node
        failing to connect -- a trust store it cannot read, a host it cannot
        verify -- not the server failing its job.
        """
        shake = candidate.handshake
        if shake is None:
            return ""
        texts = [shake.error or "", shake.login_detail or ""]
        texts += [str(row.get("detail") or "") for row in (shake.contract or []) if not row.get("passed")]
        markers = self.settings.str_list("mcp.verification.transport_failure_markers", list(TRANSPORT_FAILURE_MARKERS))
        for text in texts:
            low = text.lower()
            hit = next((m for m in markers if m and m.lower() in low), "")
            if hit:
                return text[:200]
        return ""

    def heal_transport(self, candidate: Candidate, outcome: TargetOutcome) -> bool:
        """Repair an existing entry's connection before judging it: point it at a trust store that works, and try again.

        smelt-example-net's working mcp-graphql met ``fetch failed`` on another
        machine and was replaced -- a usage task failing for want of a trust
        store read as the server failing its job. When a candidate's calls fail
        for a transport reason, every trust-store variable its runtime reads
        (``mcp.trust``) that names a file that is missing, empty, or lacks the
        chain its hosts need is pointed at a bundle that has it -- the run's own,
        or this host's -- and the candidate is trialled once more. Kept only if
        it then does better. (``mcp.verification.heal_transport``.)
        """
        import copy

        from . import trust
        from .portability import expand
        from .writer import McpWriter

        if candidate.kind != "stdio" or not self.settings.bool_("mcp.verification.heal_transport", True):
            return False
        why = self.transport_failure(candidate)
        if not why:
            return False
        command = candidate.full_command()
        names = trust.variables_for(command[0] if command else "", self.settings)
        if not names:
            return False
        writer = McpWriter(self.ctx)
        bundle = writer.run_bundle_for(candidate) or trust.system_bundle(self.settings)
        if not bundle:
            return False
        env = dict(candidate.env)
        replaced = writer.bundle_check(candidate, env).replaced
        changed: list[str] = []
        for name in names:
            current = expand(str(env.get(name) or ""))
            good = current and Path(current).is_file() and Path(current).stat().st_size > 0
            target = replaced.get(name) or ("" if good else bundle)
            if target and target != current:
                env[name] = f"${{{name}:-{target}}}"
                changed.append(name)
        if not changed:
            return False
        before = contract_score(candidate)
        variant = copy.deepcopy(candidate)
        variant.env = env
        variant.handshake = None
        self.verifier.verify_all([variant], stop_at_first=True)
        better = (
            (variant.verified and not candidate.verified)
            or contract_score(variant) > before
            or (variant.handshake is not None and not self.transport_failure(variant) and variant.handshake.ok)
        )
        if not better:
            log.info("%s: %s still fails after its trust store was repaired (%s)", outcome.target, candidate.label, why)
            return False
        candidate.env = env
        candidate.handshake = variant.handshake
        candidate.start_failure = ""
        candidate.add_note(
            f"its calls failed to connect ({why}); {', '.join(changed)} now name a trust store that carries its "
            "hosts' chains, and it was proved again -- repaired, not replaced"
        )
        log.info(
            "%s: repaired the trust store of %s (%s) and it passed again",
            outcome.target,
            candidate.label,
            ", ".join(changed),
        )
        return True

    def repair_or_mismatch(self, registration, candidate: Candidate, outcome: TargetOutcome) -> str:
        """The manifest check of an existing entry: repaired when only slots are missing, else why it is broken.

        build-example-net's entry lost ``BUILD_EXAMPLE_NET_USERNAME`` in one run and was
        kept that way by the next: its bridge, its manifest and its password
        slot were all right, and a slot nobody could fill made it useless.
        Replacing such an entry throws away the route that works; the missing
        slots are given back (``${NAME:-}``), named as needed, and the entry
        stays a candidate (``mcp.discovery.reuse.repair_missing_slots``).
        """
        mismatch = manifest_mismatch(registration.entry, self.settings)
        if not mismatch or not self.discovery.section("reuse").bool_("repair_missing_slots", True):
            return mismatch
        _path, missing = manifest_missing(registration.entry, self.settings)
        for variable in missing:
            candidate.secret_env.setdefault(variable, f"env:{variable}")
            if variable not in candidate.completed_slots:
                candidate.completed_slots.append(variable)
        candidate.add_note(
            f"its entry did not pass {', '.join(missing)}, which its bridge's login reads; the slot(s) are "
            "given back, empty until you supply them"
        )
        log.info(
            "%s: the existing %s %s -- repaired: the slot(s) are added", outcome.target, registration.name, mismatch
        )
        return ""

    def note_stale(self, candidates: Sequence[Candidate], outcome: TargetOutcome) -> None:
        """Record the existing registrations this run proved are not the target's.

        Proved, not suspected: the entry answered as an MCP server, it connects
        only to hosts that are neither the target's nor its organisation's, and
        the grounding gate refused it. Six ``api.mcp.ai`` entries were exactly
        that, and nothing took them out -- a later run refused them as
        candidates and left them loaded in every session. An entry that did not
        answer, or that names no address at all, may just be down or configured
        some other way, and is not recorded. Phase 4 acts on the list; see
        :meth:`McpWriter.retire`.
        """
        existing = ExistingRegistrations.for_run(self.ctx)
        rule = identity_rule_of(self.settings)
        for candidate in candidates:
            registration = existing.registration_for(candidate)
            if registration is None or not answered_as_mcp(candidate):
                continue
            if any(s.get("name") == registration.name for s in outcome.stale):
                continue
            grounding = self._grounding(candidate, outcome)
            if grounding is None or grounding.registrable:
                continue
            if not points_elsewhere(candidate, outcome.target, rule):
                continue
            host = parse_address(outcome.target).host or ""
            if server_names_target(candidate, host, rule.words(host)) and not gateway_marker(
                candidate, rule.gateway_markers
            ):
                continue  # its own answer names the target: unproven, not disproved
            # Hosts, not the URL or the command line: those are expanded, and
            # may carry a credential the report has no business repeating.
            outcome.stale.append(
                {
                    "name": registration.name,
                    "source": registration.source,
                    "connects_to": ", ".join(locations_of(candidate)),
                    "reason": grounding.reason,
                }
            )
            outcome.stale_candidates[registration.name] = candidate
            log.info(
                "%s: the existing registration %r is not this address's: %s",
                outcome.target,
                registration.name,
                grounding.reason,
            )

    def reused_for(self, target: str, name: str) -> list[Candidate]:
        """What :meth:`reuse` tried for this stage, for a brief to mention."""
        return list(self._reused.get((target, name), []))

    def merge_reused(self, stage: StageOutcome, outcome: TargetOutcome) -> None:
        """Fold the reused candidates into the stage the search produced."""
        candidates = self._reused.pop((outcome.target, stage.stage), [])
        if not candidates:
            return
        stage.findings["reused_existing"] = [c.label for c in candidates]
        known = {id(c) for c in stage.candidates}
        stage.candidates[:0] = [c for c in candidates if id(c) not in known]
        proven = {id(c) for c in stage.verified}
        verified = [
            c for c in candidates if c.verified and id(c) not in proven and self.registrable_with(c, outcome, stage)
        ]
        # Refused only for want of evidence that it is the address's: kept
        # aside, judged again as evidence arrives (rejudge_identity).
        for c in candidates:
            if c.verified and id(c) not in proven and c not in verified and self.refused_for_identity_only(c, outcome):
                if c not in outcome.identity_pending:
                    outcome.identity_pending.append(c)
        stage.verified[:0] = verified
        if verified and stage.status != Status.OK:
            stage.status = Status.OK
            stage.detail = f"{verified[0].label} is already registered and still works; {stage.detail}"
        if stage.stage == Stage.ENDPOINT:
            record = self._record_for(outcome)
            record.note("native_endpoints", self.admissible(verified, outcome))
            walled = [c for c in candidates if c.needs_credential]
            record.note("auth_walls", self.admissible(walled, outcome))

    def discard_unregistrable(self, stage: StageOutcome, outcome: TargetOutcome) -> None:
        """Drop stage results that the registration gate would refuse.

        The same rule the writer applies, asked while it can still change
        something. "We found a server" and "we can write this server down" are
        different questions, and a stage that answers the first while failing
        the second reports success, closes the tiers beneath it, and leaves
        nothing behind.

        Every stage of both modes passes through here right after verifying,
        so it is also where a bridge is marked with the API kind it serves
        (:meth:`mark_api_kinds`), before anything ranks it.
        """
        self.mark_api_kinds(stage, outcome)
        self.weigh_login_methods(stage, outcome)
        self.repair_failures(stage, outcome)
        self.rejudge_identity(stage, outcome)
        self.fold_copies(stage, outcome)
        keep = [c for c in stage.verified if self.registrable_with(c, outcome, stage)]
        dropped = [c for c in stage.verified if c not in keep]
        for c in dropped:
            if self.refused_for_identity_only(c, outcome) and c not in outcome.identity_pending:
                outcome.identity_pending.append(c)
        # The same question of an endpoint waiting for a credential. It settles
        # the target just as a verified one does (see settled()), so one the
        # writer will refuse closes the tiers below and leaves nothing -- and
        # there are more of them now that a stand-in credential can show a
        # server is waiting for one (mcp.packages.credential_probe).
        walled = [c for c in stage.candidates if c.needs_credential and c not in stage.verified and not c.start_failure]
        refused_walls = [c for c in walled if not self.registrable(c, outcome)]
        if not dropped and not refused_walls:
            return
        stage.verified = keep
        stage.candidates = [c for c in stage.candidates if c not in dropped and c not in refused_walls]
        stage.provisional = [c for c in stage.provisional if c not in refused_walls]
        dropped = [*dropped, *refused_walls]
        reasons = [f"{c.label}: {self.why_not_registrable(c, outcome)}" for c in dropped]
        # Refused as an answer, so it cannot be tier-1 evidence either -- however
        # it reached the record. Left there, it closed the lower tiers anyway.
        self.retract_endpoints(dropped, outcome, lambda c: self.why_not_registrable(c, outcome))
        stage.findings["not_registrable"] = reasons
        stage.add_remediation(*[f"Not registrable, so not an answer: {r}" for r in reasons[:3]])
        for reason in reasons:
            log.info("%s: %s", outcome.target, reason)
        if not stage.verified and not any(c.needs_credential for c in stage.candidates):
            stage.status = Status.PARTIAL
            stage.detail = (
                f"{len(dropped)} candidate(s) "
                + ("verified" if not refused_walls else "answered")
                + " but cannot be registered: "
                + "; ".join(reasons[:2])
            )
        elif not stage.verified and stage.status == Status.OK:
            # Its working server was refused; what is left only waits for a
            # credential. "✔ OK -- confluence-mcp-server verified" stood in the
            # report for a server refused minutes later.
            waiting = [c for c in stage.candidates if c.needs_credential and not c.start_failure]
            stage.status = Status.PARTIAL
            stage.detail = (
                f"{reasons[0] if reasons else 'its verified server cannot be registered'}; "
                f"left waiting for a credential: {', '.join(c.label for c in waiting[:3])}"
            )

    @property
    def doctor(self):
        """Builds, launches and repairs an entry. Shared with the writer.

        Constructed lazily because it needs a writer, and a writer reads the
        configuration when it is made -- so building one in ``__init__`` would
        snapshot settings a test has not set yet.
        """
        with self._doctor_lock:
            if self._doctor is None:
                from .healing import EntryDoctor
                from .writer import McpWriter

                self._doctor = EntryDoctor(self.ctx, McpWriter(self.ctx), self.verifier)
            return self._doctor

    def heal_or_discard(
        self,
        stage: StageOutcome,
        outcome: TargetOutcome,
        agent_repair=None,
        provisional_limit: int | None = None,
    ) -> None:
        """A stage has not settled until its candidate produces a working entry.

        This is the check that used to run at publish, moved to where it can
        still change something. There, an entry that would not start had already
        closed the tiers beneath it and the run was over; here, a candidate that
        cannot be repaired is simply dropped, the stage does not settle, and the
        lower tiers get their turn -- the tier fallback everybody expected,
        arranged by nothing more than asking the question at the right moment.

        It is asked of a local server **waiting for a credential** too
        (``mcp.healing.examine_pending``), because that settles a target just as
        a working one does (see :meth:`settled`). Asking only the verified ones
        is how confluence.example.com lost its server: both of its candidates were
        waiting for a token, neither was launched as written until publish, and
        the one chosen there did not start -- with the tiers already closed and
        the other candidate never tried. They are examined best first, as
        :meth:`StageOutcome.note_provisional` ranks them, until
        ``provisional_limit`` have passed; a failure hands its place to the next.

        ``agent_repair`` is the agentic pipeline's offer to fix the entry
        itself. Its absence *is* the fallback: the deterministic pipeline heals
        with rules alone, and so does an agentic run whose agent has nothing to
        say.

        ``mcp.registration.require_entry_starts = false`` means "write it
        anyway", so then a failure is noted and the candidate kept -- the same
        decision the writer makes at publish, made here too.
        """
        # A candidate whose calls never reached the service -- "fetch failed",
        # a certificate its runtime could not verify -- is repaired before it is
        # judged, whichever stage or agent proposed it: gitlab.example.net's
        # mcp-graphql failed so twice, and the address ended with nothing.
        # (mcp.verification.heal_transport_everywhere)
        if self.settings.bool_("mcp.verification.heal_transport_everywhere", True):
            for candidate in list(stage.candidates):
                if candidate in stage.verified or candidate.existing_name:
                    continue  # an existing one is healed where reuse judges it
                if self.heal_transport(candidate, outcome) and candidate.verified and candidate not in stage.verified:
                    stage.verified.append(candidate)
        pending = self._pending_to_examine(stage)
        if not stage.verified and not pending:
            return
        refuse = self.settings.bool_("mcp.registration.require_entry_starts", True)
        keep, dropped = [], []
        for candidate in stage.verified:
            healing = self._examine(candidate, outcome, agent_repair)
            if healing.ok or not refuse:
                keep.append(candidate)
            else:
                dropped.append((candidate, healing))
        limit = self.provisional_limit(False) if provisional_limit is None else provisional_limit
        passed = 0
        for candidate in pending:
            if limit and passed >= limit:
                break
            healing = self._examine(candidate, outcome, agent_repair)
            if healing.ok or not refuse:
                passed += 1
            else:
                dropped.append((candidate, healing))
        if not dropped:
            return

        stage.verified = keep
        for candidate, healing in dropped:
            candidate.start_failure = redact.redact(healing.error)
            if all(candidate is not c for c in outcome.unstartable):
                outcome.unstartable.append(candidate)
        stage.candidates = [c for c in stage.candidates if all(c is not d for d, _ in dropped)]
        stage.provisional = [c for c in stage.provisional if all(c is not d for d, _ in dropped)]
        failures = {id(c): h for c, h in dropped}
        self.retract_endpoints(
            [c for c, _ in dropped],
            outcome,
            lambda c: f"its entry does not start ({failures[id(c)].error})",
        )
        reasons = [
            f"{c.label}: its entry does not start ({redact.redact(h.error)}); tried: "
            + ("; ".join(h.attempts) or "nothing")
            for c, h in dropped
        ]
        stage.findings["unhealable"] = reasons
        stage.add_remediation(*[f"Discarded because the entry cannot be made to start: {r}" for r in reasons[:3]])
        for candidate, healing in dropped:
            for line in healing.differences:
                stage.add_remediation(f"{candidate.label}: {line}")
        for reason in reasons:
            log.info("%s: %s", outcome.target, reason)
        self.mark_evidence(stage, outcome)
        if stage.verified:
            return
        waiting = stage.note_provisional(
            limit, prefer_detected_api=self.settings.bool_("mcp.discovery.auth.prefer_detected_api", True)
        )
        if waiting:
            stage.status = Status.PARTIAL
            stage.detail = (
                f"{waiting[0].label} {waits_for_credential(waiting[0])}; "
                f"{len(dropped)} other candidate(s) discarded: their entries will not start"
            )
            stage.add_remediation(waiting_remedy(waiting[0]))
            return
        stage.status = Status.PARTIAL
        stage.detail = f"{len(dropped)} candidate(s) answered but their entries will not start: " + "; ".join(
            reasons[:2]
        )

    def _pending_to_examine(self, stage: StageOutcome) -> list[Candidate]:
        """Local candidates waiting for a credential, best first, one per launch.

        One per *launch* (:meth:`Candidate.identity`), not per label: two
        candidates of one name with different arguments are different entries,
        and examining only the first let the other settle the target unchecked.
        """
        if not self.settings.bool_("mcp.healing.examine_pending", True):
            return []
        waiting = sorted(
            (
                c
                for c in stage.candidates
                if c.kind == "stdio" and c.needs_credential and c not in stage.verified and not c.start_failure
            ),
            key=lambda c: -c.confidence,
        )
        seen: set[str] = set()
        out: list[Candidate] = []
        for candidate in waiting:
            if candidate.identity() in seen:
                continue
            seen.add(candidate.identity())
            out.append(candidate)
        return out

    def _examine(self, candidate: Candidate, outcome: TargetOutcome, agent_repair=None):
        """Build, launch and if need be repair one candidate's entry."""
        name = self.writer_name(candidate, outcome)
        healing = self.doctor.examine(candidate, name, agent_repair=agent_repair)
        candidate.healing = healing
        if not healing.ok and healing.tested:
            candidate.add_note(f"its entry does not start as written: {healing.error}")
        if healing.ok and healing.applied:
            candidate.add_note(
                "the entry did not start and was repaired (" + ", ".join(healing.applied) + "), then proved again"
            )
        return healing

    def writer_name(self, candidate: Candidate, outcome: TargetOutcome) -> str:
        """The name this candidate would be registered under, for the doctor.

        The real name is chosen by the writer across all of a target's
        candidates at once; here only one is in hand, so this is the same
        derivation for one. It is used for reporting and to launch the entry --
        never written -- so a name that later gains a suffix changes nothing.
        """
        from .writer import McpWriter

        return McpWriter(self.ctx).server_name(candidate, outcome.target)

    def why_not_registrable(self, candidate: Candidate, outcome: TargetOutcome) -> str:
        grounding = self._grounding(candidate, outcome)
        if grounding is not None and not grounding.registrable:
            return grounding.reason
        return self.trust_refusal(candidate) or self.portability_of(candidate).reason

    def registrable(self, candidate: Candidate, outcome: TargetOutcome) -> bool:
        """Would this candidate survive the registration gate? Same rules, asked early.

        Both of the writer's refusals -- the entry names no address of the
        target, or it names a path that will not outlive the run -- asked while
        the tiers below are still open. Portability used to be asked only by the
        writer, after the stage had settled: a bridge for openqa.example.net
        handshaked, closed the strategy stage beneath it, and was then refused,
        leaving the address with nothing.

        Shared with the writer rather than reimplemented: the point of asking is
        that "we found a server" and "we can write a server down" turned out to
        be different questions, and two implementations of the answer would put
        them back out of step.
        """
        grounding = self._grounding(candidate, outcome)
        if grounding is not None and not grounding.registrable:
            return False
        if self.trust_refusal(candidate):
            return False
        return self.portability_of(candidate).registrable

    def registrable_with(self, candidate: Candidate, outcome: TargetOutcome, stage: StageOutcome | None = None) -> bool:
        """:meth:`registrable`, counting the evidence ``stage`` itself just produced.

        The hosts this run found serving the address (``endpoint_hosts``) were
        read from the outcome alone, so a stage's own findings counted only
        after it was absorbed -- after its reused candidates had been judged.
        developers-google-com was judged before the agent's endpoint stage had
        recorded developers.google.com documenting developerknowledge.googleapis.com.
        """
        if self.registrable(candidate, outcome):
            return True
        if not self.refused_for_identity_only(candidate, outcome):
            return False
        from . import same_server

        pool = [*outcome.verified, *outcome.provisional]
        if stage is not None:
            pool += [*stage.verified, *stage.provisional, *[x for x in stage.candidates if x.needs_credential]]
        admitted = [c for c in pool if c is not candidate and self.registrable(c, outcome)]
        # What ties a server to the address is evidence about the *server*: a
        # copy of it (the same URL and transport, or the same package and
        # hosts) that passed on its own evidence -- the target documenting it --
        # carries that evidence for every copy.
        mine = same_server.key(candidate, self.settings)
        copy_ = next((c for c in admitted if same_server.key(c, self.settings) == mine), None)
        if copy_ is not None:
            # Carried as provenance, so every gate after this one -- the
            # writer's included -- reads the same evidence on this copy.
            candidate.add_note(f"the same server as {copy_.label}, which was found as: {copy_.source}")
            for note in copy_.notes:
                if note not in candidate.notes:
                    candidate.add_note(note)
            return self.registrable(candidate, outcome)
        extra = [c for c in admitted if c.url]
        if not extra or stage is None:
            return False
        view = TargetOutcome(
            target=outcome.target, verified=list(outcome.verified), provisional=[*outcome.provisional, *extra]
        )
        view.due_diligence = outcome.due_diligence
        grounding = self._grounding(candidate, view)
        return grounding is None or grounding.registrable

    def refused_for_identity_only(self, candidate: Candidate, outcome: TargetOutcome) -> bool:
        """Is the grounding gate -- nothing ties it to the address yet -- the only thing refusing ``candidate``?"""
        grounding = self._grounding(candidate, outcome)
        if grounding is None or grounding.registrable:
            return False
        return not self.trust_refusal(candidate) and self.portability_of(candidate).registrable

    def rejudge_identity(self, stage: StageOutcome, outcome: TargetOutcome) -> None:
        """Judge again the proven servers refused only for want of evidence, now that there may be some.

        Both modes pass through here after every stage. One that the evidence
        gathered since -- a documented or linked endpoint, an admitted copy of
        the same server -- now ties to the address rejoins this stage; the
        same-server rule (fold_copies) then keeps the better-proven copy.
        (``mcp.registration.rejudge_identity``.)
        """
        if not outcome.identity_pending or not self.settings.bool_("mcp.registration.rejudge_identity", True):
            return
        for candidate in list(outcome.identity_pending):
            if not self.registrable_with(candidate, outcome, stage):
                continue
            outcome.identity_pending.remove(candidate)
            if candidate not in stage.candidates:
                stage.candidates.append(candidate)
            group = stage.verified if candidate.verified else stage.provisional
            if candidate not in group:
                group.append(candidate)
            stage.findings.setdefault("rejudged", []).append(candidate.label)
            log.info(
                "%s: %s is now tied to the address by what this run found, and rejoins", outcome.target, candidate.label
            )

    def _grounding(self, candidate: Candidate, outcome: TargetOutcome):
        """The writer's grounding verdict, or None when that gate is switched off."""
        if not self.settings.bool_("mcp.registration.require_address_in_entry", True):
            return None
        return ground(
            candidate,
            outcome.target,
            allow_front_door=self.settings.bool_("mcp.registration.allow_bare_origin", False),
            served_by=endpoint_hosts(outcome),
            hosting_namespaces=hosting_namespaces_of(self.settings),
            identity=identity_rule_of(self.settings),
        )

    def trust_refusal(self, candidate: Candidate) -> str:
        """The writer's TLS-chain verdict -- the same call, not a copy of it."""
        from .writer import McpWriter

        return McpWriter(self.ctx).trust_refusal(candidate)

    def portability_of(self, candidate: Candidate):
        """The writer's portability verdict -- the same call, not a copy of it."""
        from .writer import McpWriter

        return McpWriter(self.ctx).portability_of(candidate)

    def reconcile(self, outcome: TargetOutcome, exhaustive: bool = False) -> None:
        """Catch the case where the run believed in an endpoint it never wrote down.

        The tier gate closes tiers 2, 3 and 4 as soon as due diligence records a
        native endpoint. That is right, and it is only right if the endpoint
        then *becomes* something -- and once, for ``developers.google.com``, it
        did not. The registry published the endpoint on another hostname, the
        old host filter dropped it before it could be probed, step D recorded it
        anyway, and the address finished having refused every alternative on the
        strength of an endpoint that was never registered. Three lines of the
        log disagreed with each other and nothing noticed:

            SKIP  endpoint   no MCP endpoint answered ... no native endpoint answered
            SKIP  bridge     a native MCP endpoint exists at https://developerknowledge...
            OK    due diligence  4/4 checks; native endpoint found at https://developerknowledge...

        So the disagreement is now the trigger. Anything the checklist believes
        in is probed here, once, as a last chance -- which also covers a URL
        that reached the record by some route nobody has thought of yet. If it
        answers it becomes a candidate like any other and is judged by the same
        rules; if it does not, the contradiction is reported rather than left
        for somebody to find in the log.

        Runs for both pipelines: the agentic one calls it through ``fallback``,
        because a rule only one pipeline enforces is not a rule.
        """
        record = outcome.due_diligence
        if record is None:
            return
        # "Found something" has to mean "found something that can be written".
        # The guard used to be `if outcome.verified` alone, and that let the
        # worst case through: a bridge for developers.google.com verified with
        # three tools, was refused at publish for carrying no address, and this
        # check -- the one meant to catch an address ending up with nothing --
        # had already decided the target was fine.
        registrable = [
            candidate for candidate in [*outcome.verified, *outcome.provisional] if self.registrable(candidate, outcome)
        ]
        if registrable:
            return
        believed = [
            url for url in [*getattr(record, "native_endpoints", []), *getattr(record, "auth_walls", [])] if url
        ]
        if not believed:
            return

        log.info(
            "%s: the checklist records %d endpoint(s) that never became a candidate; probing them",
            outcome.target,
            len(believed),
        )
        self.console.bullet(
            f"the checklist recorded {len(believed)} endpoint(s) that nothing registered -- "
            "probing them before giving up",
            indent=4,
        )
        stage = self._probe_believed(outcome, believed, exhaustive)
        if stage.verified or stage.provisional:
            stage.status = Status.CHANGED
            stage.detail = (
                f"{', '.join(believed)} was recorded by the checklist but never offered as a "
                "candidate; probing it here recovered it"
            )
            outcome.stages.append(stage)
            return

        # Still nothing that can be written. Say so plainly: the address may
        # have refused every lower tier on the strength of this, so "found
        # nothing" is not the whole story.
        stage.status = Status.PARTIAL
        ruled = [url for url in believed if url in record.ruled_out]
        if ruled and self.reopen_enabled():
            stage.detail = (
                f"the checklist recorded {', '.join(believed)} as this target's MCP endpoint, but "
                f"it cannot be registered for it ({record.ruled_out[ruled[0]]}); it is ruled out, "
                "and any tier it closed is reopened"
            )
            stage.add_remediation(
                f"{ruled[0]} is not counted as this target's endpoint. If it really is, register "
                "it by hand with `claude mcp add-json`."
            )
        else:
            stage.detail = (
                f"the checklist recorded {', '.join(believed)} as this target's MCP endpoint, and "
                "tiers 2-4 were refused on that basis, but it did not answer when probed -- so this "
                "address ends with nothing registered and no alternative attempted"
            )
            stage.add_remediation(
                f"Check {believed[0]} by hand. If it is right, register it with "
                "`claude mcp add-json`; if it is not, re-run with --set "
                "mcp.policy.forbid_lower_tiers_when_native=false so the lower tiers are allowed to "
                "try."
            )
        outcome.stages.append(stage)

    def _probe_believed(
        self, outcome: TargetOutcome, urls: Sequence[str], exhaustive: bool, *, every: bool = False
    ) -> StageOutcome:
        """Probe endpoints the record believes in, and judge them like any candidate.

        Shared by :meth:`vet_record` (before a gate) and :meth:`reconcile`
        (after the last stage). What answers *and* can be registered is
        absorbed into the target; what answers but cannot, or does not answer
        at all, is retracted from the record, so it closes no tier. ``every``
        judges every URL rather than stopping at the first that can be written
        -- the vetting pass needs a verdict on each, since each one gates.
        """
        record = self._record_for(outcome)
        stage = StageOutcome(stage=Stage.ENDPOINT, mode=self.mode, status=Status.SKIPPED)
        stage.findings["vetted"] = list(urls)
        stage.candidates = [
            Candidate(
                kind="url",
                name=slugify(parse_address(url).host or url, fallback="endpoint"),
                url=url,
                transport=Transport.STREAMABLE_HTTP,
                origin=Stage.ENDPOINT,
                confidence=0.5,
                source="recorded by the tier-1 checklist but never probed as a candidate",
                auth_claim=(
                    "the tier-1 checklist recorded it behind an authentication wall" if url in record.auth_walls else ""
                ),
            )
            for url in urls
        ]
        self.verifier.verify_all(
            stage.candidates,
            stop_at_first=not (exhaustive or every),
            keep=lambda c: self.registrable(c, outcome),
            memo=outcome.trials,
            why=lambda c: self.why_not_registrable(c, outcome),
        )
        stage.verified = [c for c in stage.candidates if c.verified]
        # The same two checks every stage meets, *before* anything is absorbed:
        # the old version absorbed first, so an endpoint the writer refuses
        # was reported as "recovered" and then dropped at registration.
        self.discard_unregistrable(stage, outcome)
        self.heal_or_discard(stage, outcome, provisional_limit=self.provisional_limit(exhaustive))
        silent = [c for c in stage.candidates if c.handshake is not None and not c.verified and not c.needs_credential]
        self.retract_endpoints(
            silent,
            outcome,
            lambda c: (
                "it did not answer as an MCP endpoint when probed"
                + (f" ({c.handshake.error})" if c.handshake and c.handshake.error else "")
            ),
        )
        self.absorb(outcome, stage, exhaustive)
        stage.findings["due_diligence"] = record.to_dict()
        return stage

    def vet_record(self, outcome: TargetOutcome, exhaustive: bool = False) -> None:
        """Make sure every endpoint the record believes in can be registered, *before* it gates.

        The tier gate reads only the record, and several routes write to it:
        this pipeline's probe, an agent's endpoint candidates, and an agent's
        checklist -- the last taken on its word. :meth:`reconcile` used to be
        the only place a believed endpoint was tested, and it runs after the
        last stage: by then the tiers it wrongly closed had been skipped.
        This is the same test, run after every stage, so the next gate sees a
        record of endpoints that could actually be written down.

        Only URLs no judged candidate already backs are probed, and the
        target's verdict memory means none is launched twice.
        ``mcp.policy.vet_record_before_gate = false`` leaves it to reconcile.
        """
        if not self.settings.bool_("mcp.policy.vet_record_before_gate", True):
            return
        self.vet_sockets(outcome)
        if not self.settings.bool_("mcp.policy.native_must_be_registrable", True):
            return
        record = outcome.due_diligence
        if not isinstance(record, DueDiligence):
            return
        backed = {
            c.url
            for c in self._judged(outcome)
            if c.url and (c.verified or c.needs_credential) and self.registrable(c, outcome)
        }
        unbacked = [u for u in [*record.native_endpoints, *record.auth_walls] if u not in backed]
        if not unbacked:
            return
        log.info(
            "%s: the checklist believes in %d endpoint(s) nothing has confirmed; probing them "
            "before they gate anything",
            outcome.target,
            len(unbacked),
        )
        stage = self._probe_believed(outcome, unbacked, exhaustive, every=True)
        if stage.verified or stage.provisional:
            stage.status = Status.CHANGED
            stage.detail = (
                f"{', '.join(unbacked)} was recorded by the checklist without being offered as a "
                "candidate; probing it confirmed it"
            )
        else:
            stage.status = Status.PARTIAL
            stage.detail = (
                f"{', '.join(unbacked)} was recorded by the checklist, but cannot be registered for "
                "this target, so it is ruled out and closes no tier: "
                + "; ".join(record.ruled_out.get(u, "") for u in unbacked[:2] if u in record.ruled_out)
            )
        outcome.stages.append(stage)

    def vet_sockets(self, outcome: TargetOutcome) -> None:
        """Take out of the record every bridge-needing WebSocket with no sign of MCP.

        "A native endpoint that needs a bridge" opens the bridge stage and
        closes the package stage, so it has to mean an MCP server. The probe
        only records one with MCP evidence (:meth:`McpProber.probe_websocket`),
        but an agent's ``websocket_urls`` and its checklist's
        ``endpoints_needing_a_bridge`` arrive on the agent's word -- and a
        service's own live-update socket is easy to mistake for one. Each is
        asked here, once per run, exactly as the probe would have asked it.
        """
        record = outcome.due_diligence
        if not isinstance(record, DueDiligence) or not record.needs_bridge:
            return
        if not self.discovery.bool_("websocket_requires_mcp_evidence", True):
            return
        refused: list[str] = []
        for url in list(record.needs_bridge):
            why = self.socket_verdict(url)
            if why:
                self._rule_out(record, Candidate(kind="url", name="", url=url), outcome, why)
                refused.append(url)
        if not refused:
            return
        stage = StageOutcome(stage=Stage.ENDPOINT, mode=self.mode, status=Status.PARTIAL)
        stage.findings["vetted"] = refused
        stage.findings["due_diligence"] = record.to_dict()
        stage.detail = (
            f"{', '.join(refused)} was reported as a native endpoint needing a bridge, but probing "
            f"it found no sign of MCP ({record.ruled_out.get(refused[0], '')}); it closes no tier"
        )
        outcome.stages.append(stage)

    def socket_verdict(self, url: str) -> str:
        """``""`` if the WebSocket route at ``url`` carries MCP; otherwise why not."""
        if url in self._sockets:
            return self._sockets[url]
        parts = urlsplit(url)
        scheme = {"ws": "http", "wss": "https"}.get(parts.scheme.lower(), parts.scheme)
        shake = self.prober.probe_websocket(
            urlunsplit(parts._replace(scheme=scheme)),
            headers=self.verifier.auth_headers(),
            timeout=self.discovery.float_("per_request_timeout_seconds", 8),
        )
        return self.remember_socket(url, shake)

    def remember_socket(self, url: str, shake: Handshake | None) -> str:
        why = (
            ""
            if shake is not None and WEBSOCKET_HINT in shake.protocol_hints
            else (shake.error if shake is not None and shake.error else "it did not answer")
        )
        self._sockets[url] = why
        return why

    @staticmethod
    def _judged(outcome: TargetOutcome) -> list[Candidate]:
        """Every candidate this target has already put through a trial."""
        seen: list[Candidate] = []
        for candidate in [
            *outcome.verified,
            *outcome.provisional,
            *(c for stage in outcome.stages for c in stage.candidates),
        ]:
            if candidate.handshake is not None and all(candidate is not s for s in seen):
                seen.append(candidate)
        return seen

    def reopen_enabled(self) -> bool:
        return self.settings.bool_("mcp.policy.reopen_when_native_retracted", True)

    def reopenable(self, outcome: TargetOutcome) -> list[str]:
        """Stages the tier gate refused on evidence that has since been ruled out.

        The last safety net behind :meth:`vet_record`: if a stage was skipped
        "because a native endpoint exists" and that endpoint turned out not to
        be registrable for the target, the refusal no longer holds, and the
        stage is owed its turn. The gate is asked again, so a stage still
        refused for a real reason stays refused.
        """
        if not self.reopen_enabled():
            return []
        record = outcome.due_diligence
        names: list[str] = []
        for stage in outcome.stages:
            if stage.status != Status.SKIPPED or not stage.findings.get("blocked_by_policy"):
                continue
            if stage.stage in names:
                continue
            allowed, _why = self.policy.gate(stage.stage, record)
            if allowed:
                names.append(stage.stage)
        return names

    def reopenable_after_withdrawal(self, outcome: TargetOutcome, exhaustive: bool) -> list[str]:
        """Stages skipped because the address was settled -- by a server a later check has since withdrawn.

        A tier settles the descent, and the tiers beneath it are skipped. When
        the server that settled it is withdrawn afterwards (the final re-check,
        the start check, a registration refused, Claude Code itself unable to
        use it), those tiers were skipped for a reason that no longer holds:
        gitlab.example.net's package tier settled on mcp-graphql, the re-check
        withdrew it, and the bridge tier -- where its working bridge was
        registered -- was never looked at. The descent rule is asked again
        (:meth:`settled`, :meth:`gate`), so a stage still settled by what
        remains, or refused by the tier policy, stays skipped.
        (``mcp.handover.reopen_settled_tiers``.)
        """
        if not self.settings.bool_("mcp.handover.reopen_settled_tiers", True):
            return []
        names: list[str] = []
        for stage in outcome.stages:
            if stage.status != Status.SKIPPED or not stage.findings.get("settled_by") or stage.stage in names:
                continue
            if self.gate(stage.stage, outcome) is None and self.settled(stage.stage, outcome, exhaustive) is None:
                names.append(stage.stage)
        return names

    @staticmethod
    def withdraw_settled(outcome: TargetOutcome, name: str) -> None:
        """Drop the "already settled" skip a reopened stage is about to replace."""
        outcome.stages = [
            s
            for s in outcome.stages
            if not (s.stage == name and s.status == Status.SKIPPED and s.findings.get("settled_by"))
        ]

    def reopen_withdrawn(self, outcome: TargetOutcome) -> list[str]:
        """Work the stages owed their turn after a withdrawal (:meth:`reopenable_after_withdrawal`); their names."""
        exhaustive = self.settings.bool_("mcp.exhaustive", False)
        handlers: dict[str, Callable[[TargetOutcome], StageOutcome]] = {
            Stage.ENDPOINT: self.stage_endpoint,
            Stage.PACKAGE: self.stage_package,
            Stage.BRIDGE: self.stage_bridge,
            Stage.STRATEGY: self.stage_strategy,
        }
        names = self.reopenable_after_withdrawal(outcome, exhaustive)
        for name in names:
            self.withdraw_settled(outcome, name)
            self._work(name, outcome, handlers, exhaustive, reopened=self.REOPENED_BY_WITHDRAWAL)
        if names:
            self.reconcile(outcome, exhaustive)
        return names

    @staticmethod
    def withdraw_refusal(outcome: TargetOutcome, name: str) -> None:
        """Drop the policy refusal a reopened stage is about to replace."""
        outcome.stages = [
            s
            for s in outcome.stages
            if not (s.stage == name and s.status == Status.SKIPPED and s.findings.get("blocked_by_policy"))
        ]

    def absorb(self, outcome: TargetOutcome, stage: StageOutcome, exhaustive: bool) -> None:
        """:meth:`TargetOutcome.absorb`, configured -- the one call both pipelines make."""
        # What is known about every waiting candidate, before any chooser reads
        # it: build-example-net's stage returned before marking because an
        # unrelated search hit had completed a handshake ("verified", refused by
        # the gate), and the osc signing bridge and the Basic one were then
        # ordered by confidence and start time -- a coin toss between runs.
        self.mark_evidence(stage, outcome)
        outcome.absorb(
            stage,
            self.provisional_limit(exhaustive),
            prefer_detected_api=self.settings.bool_("mcp.discovery.auth.prefer_detected_api", True),
            proven_replaces_unproved=not exhaustive
            and self.settings.bool_("mcp.discovery.proven_replaces_unproved", True),
        )

    def provisional_limit(self, exhaustive: bool) -> int:
        """How many authenticated endpoints are worth registering for a target.

        One, unless asked for all of them: a host that sits entirely behind a
        login answers 401 to every path probed, and the operator wants the
        endpoint, not the inventory of everything that declined to talk.

        The number is a budget for the whole *target*, not for each stage --
        :meth:`TargetOutcome.absorb` is what spends it. Per stage it was no
        budget at all: four stages each kept their own best one and
        ``jira.example.com`` came out of a single default run with two entries.
        """
        if exhaustive:
            return 0
        return self.settings.int_("mcp.discovery.auth.max_provisional", 1)

    def settled(self, name: str, outcome: TargetOutcome, exhaustive: bool, mode: str = "") -> StageOutcome | None:
        """``None`` if the descent should continue; a skip to report if it should not.

        A verified server obviously settles a target. So does a credential-walled
        one, and that is the part worth stating: since a package is now trialled
        *without* its secret, "provisional" is the honest verdict for any target
        whose server wants a token, and while only ``verified`` counted as an
        answer such a target went on through the package and bridge stages
        anyway -- minutes of agent time each, chasing a question already
        answered, and a second entry in ``.mcp.json`` competing with the first.
        ``jira.example.com`` produced exactly that from an ordinary run: a package
        entry that worked and a bridge entry that did not.

        Which one survives is decided by the tier order, since the descent stops
        at the highest tier that answered.

        Shaped like :meth:`gate` rather than as a ``break`` on purpose. Leaving
        the loop early would leave the remaining stages out of the report
        altogether, and "the bridge stage is absent" reads as a bug, where
        "the bridge stage was skipped, because the package stage already found
        an endpoint that only wants a credential" is the reason itself.
        """
        if exhaustive:
            return None
        if (
            outcome.identification_undecided
            and self.discovery.bool_("undecided_keeps_searching", True)
            and not any(proves_access(c) for c in [*outcome.verified, *outcome.provisional])
        ):
            # Nothing proved access, and what the address is -- its product, its
            # APIs -- is not known because it said "try again later" while it
            # was asked: an unproven server is not its answer. sd.example.com
            # answered 502, its customer API went undetected, and a waiting
            # mcp-atlassian closed the bridge stage.
            log.info(
                "descent continues to the %s stage: %s, and nothing proved access",
                name,
                outcome.identification_undecided,
            )
            return None
        if self.owes_generated_bridge(name, outcome):
            log.info(
                "descent continues to the %s stage: nothing proved access yet, and the bridge for %s "
                "can be generated from its documented operations",
                name,
                ", ".join(self.untried_bridges(outcome)),
            )
            return None
        answering = [
            c
            for c in outcome.provisional
            if not login_check.keeps_searching(c, self.settings)
            and not self.unproved_with_alternative(c, outcome)
            and not self.offered_unproved(c)
        ]
        # A server Claude Code itself could not use settles nothing
        # (TargetOutcome.consumer_failed): the tiers it closed are owed a look.
        proven = [
            c
            for c in outcome.verified
            if not self.unproved_with_alternative(c, outcome) and c.identity() not in outcome.consumer_failed
        ]
        if outcome.verified and not proven:
            # It works as far as anything showed -- but every call came back
            # empty or unchecked, and the target serves another API this server
            # does not speak. sd.example.com's customer account got [] from
            # mcp-atlassian; its Service Desk API is one tier down.
            log.info(
                "descent continues to the %s stage: %s answered, but access was not proved "
                "and the target serves another API",
                name,
                _naming(outcome.verified),
            )
            return None
        if proven:
            why = f"{_naming(proven)} already answered; lower tiers are not searched unless it is withdrawn later"
        elif outcome.provisional and not answering:
            # Waiting only on servers whose account has no API access, or that
            # were given the credential this machine holds and still proved
            # nothing: keep looking for a server that can be shown to work.
            log.info(
                "descent continues to the %s stage: %s cannot be shown to work for this account "
                "(no API access, or nothing proved with the credential it was given); a lower tier may serve one that can",
                name,
                _naming(outcome.provisional),
            )
            return None
        elif not outcome.provisional:
            return None
        elif not self.settings.bool_("mcp.discovery.provisional_settles_target", True):
            return None
        else:
            why = (
                f"{_naming(outcome.provisional)} already answered, and asked for a credential; "
                "a server that is already there is the answer, so lower tiers are not searched unless it is withdrawn later"
            )
        log.info("descent settled before the %s stage: %s", name, why)
        stage = StageOutcome(stage=name, mode=mode or self.mode, status=Status.SKIPPED, detail=why)
        stage.findings["settled_by"] = why
        if not proven:
            stage.findings["settled_unproven"] = True
        stage.findings["tier"] = self.policy.tier(name)
        stage.add_remediation(
            "--exhaustive-mcp searches every tier and registers everything that answers",
            "mcp.discovery.provisional_settles_target=false keeps searching after a credential wall",
        )
        return stage

    def offered_unproved(self, candidate: Candidate) -> bool:
        """Given the credential this machine holds for it, did it still prove nothing? Then it waits for nothing.

        gitlab.example.net's mcp-graphql was given the token its entry carried and
        no call could prove it ("no tool is both read-only and callable"); it
        was still "waiting for a credential" and settled the address -- the
        credential it waited for was the one it had been given.
        (``mcp.discovery.offered_unproved_keeps_searching``.)
        """
        if not candidate.in_use or not self.discovery.bool_("offered_unproved_keeps_searching", True):
            return False
        shake = candidate.handshake
        return not proves_access(candidate) and not (shake is not None and shake.login == login_check.ACCEPTED)

    def unproved_with_alternative(self, candidate: Candidate, outcome: TargetOutcome) -> bool:
        """Is access unproved for ``candidate`` while the target serves an API it does not speak?

        Unproved: every call the check made came back empty or unchecked
        (``login_check.UNPROVED``). The alternative: a bridgeable API kind
        (``mcp.discovery.api_kinds.<name>.operations``) detected for the target,
        other than the one the candidate was itself bridged from. Then the
        search goes on (``login_check.unproved_settles = false``) and the
        alternative is checked too; with no alternative, nothing changes.
        """
        if self.settings.bool_("mcp.verification.login_check.unproved_settles", False):
            return False
        shake = candidate.handshake
        if shake is None:
            return False
        if shake.login in login_check.UNPROVED:
            found = self.api_kinds_for(outcome)
            if any(kind.name != candidate.api_kind for kind, _ in api_kinds.bridgeable(self.settings, found)):
                return True
        # A server counted as *working* that cannot be asked what the address
        # is for settles nothing; one still waiting for its credential could
        # not be asked anything yet, and keeps its usual place.
        return (
            candidate.verified and shake.login != login_check.ACCEPTED and bool(self.misses_the_job(candidate, outcome))
        )

    def why_unsettled(self, candidate: Candidate, outcome: TargetOutcome) -> str:
        """In words, why ``candidate`` -- verified -- settles nothing; "" when it does."""
        if not self.unproved_with_alternative(candidate, outcome):
            return ""
        missing = self.misses_the_job(candidate, outcome) if candidate.verified else []
        if missing:
            return (
                f"no tool of it can be asked what the address is for ({'; '.join(missing)}) -- by name, by what a "
                "tool says it does, or by an answer of the API's own form"
            )
        return "its access was never proved, and the address serves an API it is not built for"

    def misses_the_job(self, candidate: Candidate, outcome: TargetOutcome) -> list[str]:
        """The usage tasks of what the address *is* that ``candidate`` lists no tool for; [] if it can be asked one.

        What the address is: the catalogue product it was recognised as
        (``auth_plan``) and every API kind detected for it -- sd.example.com is a
        Jira that serves a Service Management customer API. Their tasks
        (``[mcp.contract.products.<name>]``) are the job. A server that can be
        asked none of them, and whose login no call accepted, does not settle
        the address however well it answered: mcp-openapi-proxy's 283
        path-named tools offered nothing the build service's task matches, and
        its anonymous ``/about`` closed every tier below it.
        (``mcp.contract.address_job``.)
        """
        from . import contract

        if not self.settings.bool_("mcp.contract.address_job", True):
            return []
        tasks = contract.product_tasks(self.settings, self.know_job(outcome))
        if not tasks:
            return []
        shake = candidate.handshake
        tools = [str(s.get("name") or "") for s in (shake.tool_specs if shake else [])] or list(
            shake.tools if shake else []
        )
        if contract.askable(tasks, tools, shake.tool_specs if shake else [], self.settings):
            return []
        # ...or one of its tools did a task already, found by its answer.
        names = {t.name for t in tasks}
        if shake is not None and any(r.get("passed") and r.get("task") in names for r in shake.contract):
            return []
        return [t.name for t in tasks]

    def _generic_servers_failed(self, outcome: TargetOutcome, kind: Any) -> bool:
        """Were ``kind``'s generic servers tried for this address -- by any stage or agent -- and none passed?

        (``mcp.discovery.api_kind_bridge_when_generic_fails``.)
        """
        if not self.discovery.bool_("api_kind_bridge_when_generic_fails", True):
            return False
        packages = [str(getattr(a, "package", "") or "") for a in kind.adapters]
        tried = [
            c
            for st in outcome.stages
            for c in st.candidates
            if any(p and p in " ".join(c.full_command()) for p in packages)
        ]
        return bool(tried) and not any(c.verified or (c.needs_credential and not c.start_failure) for c in tried)

    def mark_evidence(self, stage: StageOutcome, outcome: TargetOutcome) -> None:
        """What the run knows about each waiting candidate, for the choosers that read it (:mod:`.evidence`)."""
        from . import evidence

        host = (parse_address(outcome.target).host or "").lower()
        # Asked, not read: ranked before a plan existed, every login looks alike.
        plan = outcome.auth_plan
        if not plan and any(getattr(c, "needs_credential", False) for c in stage.candidates):
            try:
                plan = self.auth_plan_for(outcome)
            except Exception as exc:  # noqa: BLE001 - an unknown plan only leaves the logins unranked
                log.debug("%s: no login plan for ranking: %s", outcome.target, exc)
        evidence.mark(self.ctx, stage.candidates, host, plan or {})

    def _learn_job(self, outcome: TargetOutcome) -> None:
        try:
            self.know_job(outcome)
        except Exception as exc:  # noqa: BLE001 - not knowing only keeps product tasks unasked
            log.debug("%s: what the address is stayed unknown: %s", outcome.target, exc)

    def know_job(self, outcome: TargetOutcome) -> list[str]:
        """What the address is -- its recognised product and detected API kinds -- recorded for the verifier.

        A product's own task (:func:`.contract.scope_of`) is asked only of a
        server for an address that is that product; the verifier reads this
        from ``ctx.state["mcp_address_job"]``, keyed by the address's host and
        every host serving one of its APIs.
        """
        names = {str(self.auth_plan_for(outcome).get("product") or "")}
        kinds = self.api_kinds_for(outcome)
        names |= set(kinds)
        job = sorted(n for n in names if n)
        hosts = {(parse_address(outcome.target).host or "").lower()}
        for found in kinds.values():
            hosts.add((parse_address(str(found.get("url") or "")).host or "").lower())
        jobs = self.ctx.state.setdefault("mcp_address_job", {})
        for host in sorted(h for h in hosts if h):
            jobs[host] = sorted({*jobs.get(host, []), *job})
        return job

    @property
    def rechecking(self) -> bool:
        """``--mcp-recheck``: prove what is there again -- registrations, the last working entry -- and search nothing."""
        return self.settings.bool_("mcp.recheck", False)

    def recheck_stage(self, name: str) -> StageOutcome:
        """The stage of a recheck: what reuse found is merged into it; nothing new is searched for."""
        stage = StageOutcome(
            stage=name,
            mode=self.mode,
            status=Status.SKIPPED,
            detail="recheck: only the existing and the last working entries are proved again",
        )
        stage.findings["recheck"] = True
        return stage

    def fold_copies(self, stage: StageOutcome, outcome: TargetOutcome) -> None:
        """Keep one of each server the stage found working or waiting (:mod:`.same_server`).

        Both modes pass through here. A copy -- the same package or URL reaching
        the same hosts, under other flags -- is not a second server, whatever
        mode the run is in; one waiting for a credential is not kept beside a
        working copy of itself. The copies stay among the stage's candidates.
        """
        from . import same_server

        if not same_server.enabled(self.settings):
            return
        stage.verified, folded = same_server.fold(stage.verified, self.settings)
        working = {same_server.key(c, self.settings) for c in stage.verified}
        waiting = [c for c in stage.provisional if same_server.key(c, self.settings) not in working]
        folded += [(c, c) for c in stage.provisional if c not in waiting]
        stage.provisional, more = same_server.fold(waiting, self.settings)
        folded += more
        if folded:
            lines = [
                f"{c.label} is a copy of {k.label}"
                if c is not k
                else f"{c.label} waits beside a working copy of itself"
                for c, k in folded
            ]
            stage.findings.setdefault("copies", []).extend(lines)
            for line in lines:
                log.info("%s: %s -- one entry for one server", outcome.target, line)

    def repair_failures(self, stage: StageOutcome, outcome: TargetOutcome) -> None:
        """Repair every candidate that did not do the job, when its failure has a fix (:mod:`.repair`).

        Both modes pass through here after verifying. A repaired variant is
        proved by every check again and joins the stage only if it passes; what
        was tried is kept in the stage's findings. A failure only the operator
        can fix is left to the checklist.
        """
        from . import repair

        if not self.settings.bool_("mcp.repair.enabled", True):
            return
        host = parse_address(outcome.target).host or ""
        context = {
            "settings": self.settings,
            "ctx": self.ctx,
            "target": outcome.target,
            "host": host,
            "plan": outcome.auth_plan,
        }
        for candidate in list(stage.candidates):
            if candidate.verified or candidate in stage.verified or not repair.classify(candidate, outcome.auth_plan):
                continue
            fixed, tried = repair.run(candidate, context, self.verifier.verify)
            if tried:
                stage.findings.setdefault("repairs", []).extend(f"{candidate.label}: {line}" for line in tried)
            if fixed is None:
                continue
            stage.candidates.append(fixed)
            if fixed.verified:
                stage.verified.append(fixed)

    def auth_plan_for(self, outcome: TargetOutcome) -> dict[str, Any]:
        """How the address can be logged in to, and which way to use (:mod:`.login_methods`); asked once."""
        if not self.discovery.section("auth").bool_("read_challenges", True):
            return outcome.auth_plan
        if outcome.auth_plan and not self._reidentify_now(outcome):
            return outcome.auth_plan
        host = parse_address(outcome.target).host or ""
        if not host:
            return outcome.auth_plan
        timeout = self.discovery.float_("per_request_timeout_seconds", 8)
        busy: list[str] = []
        waits: list[float] = []
        with self.ctx.http(timeout=timeout) as client:
            if outcome.all_findings().get("origin_status") == 0 and not self._answers_now(
                client, outcome.target, timeout
            ):
                # Unreachable twice: no live challenge -- but a timeout is not a
                # verdict on how the host logs in. build.example.net timed out once
                # under --concurrency, no plan was made, and its waiting bridges
                # were ranked with no login known. What it asked before stands.
                offered = login_methods.with_remembered(self.ctx, host, [])
                if not offered:
                    return outcome.auth_plan
                product = str(next((o.get("product") for o in offered if o.get("product")), "") or "")
                return self._make_plan(outcome, host, product, offered)

            def get(url: str):
                try:
                    response = client.get(url, timeout=timeout)
                except Exception as exc:  # noqa: BLE001 - no answer is not "nothing here"
                    # A timeout or a dropped connection says nothing about what
                    # the address is: build.example.net came out "product unknown"
                    # in one run and open_build_service in the next minute.
                    busy.append(f"no answer ({type(exc).__name__})")
                    return None
                if self._note_busy(response, url, busy):
                    waits.append(_retry_after(response))
                return response

            product = login_methods.recognise(host, self.settings, get)
            for attempt in range(self._identify_retries() if not product and busy else 0):
                # It said "later", not "no": asked again before deciding anything
                # from it -- after the wait it asked for (Retry-After), else a
                # pause that grows with each attempt.
                self._identify_pause(max(waits, default=0.0), attempt)
                busy.clear()
                waits.clear()
                product = login_methods.recognise(host, self.settings, get)
                if product or not busy:
                    break
            if product and outcome.identification_undecided:
                log.info("%s: identified as %s on asking again", outcome.target, product)
                outcome.identification_undecided = ""
            if not product and busy:
                self._undecided(outcome, f"{host} answered {busy[-1]} while it was being identified")
                with _REIDENTIFY_LOCK:
                    # The interval before asking again counts from this answer.
                    count, _last = self.ctx.state.setdefault("mcp_reidentified", {}).get(outcome.target, (0, 0.0))
                    self.ctx.state["mcp_reidentified"][outcome.target] = (count, time.monotonic())
                # What it was when it last answered stands until it answers
                # again: bugzilla.opensuse.org answered 429, came out "product
                # unknown; logins offered: none seen", and nothing asked for its
                # login. (mcp.discovery.remembered_product_when_busy.)
                if self.discovery.bool_("remembered_product_when_busy", True):
                    remembered = login_methods.with_remembered(self.ctx, host, [])
                    product = str(next((o.get("product") for o in remembered if o.get("product")), "") or "")
                    if product:
                        log.info(
                            "%s: still busy -- %s, as it last answered, until it answers again", outcome.target, product
                        )
            offered = login_methods.challenges(
                host, login_methods.products(self.settings).get(product), self.settings, get, product_name=product
            )
            offered += login_methods.understood_methods(
                host, product, offered, self.settings, get, self._probe_with_headers(client, timeout)
            )
            # A host that gave no challenge this time keeps the one it gave before.
            offered = login_methods.with_remembered(self.ctx, host, offered)
        return self._make_plan(outcome, host, product, offered)

    def _reidentify_now(self, outcome: TargetOutcome) -> bool:
        """Ask a still-undecided address again now? At most ``reidentify_times`` more, ``reidentify_after_seconds`` apart."""
        if not outcome.identification_undecided or not self.discovery.bool_("reidentify_undecided", True):
            return False
        with _REIDENTIFY_LOCK:
            asked = self.ctx.state.setdefault("mcp_reidentified", {})
            count, last = asked.get(outcome.target, (0, 0.0))
            if count >= max(0, self.discovery.int_("reidentify_times", 1)):
                return False
            if last and time.monotonic() - last < max(0.0, self.discovery.float_("reidentify_after_seconds", 60.0)):
                return False
            asked[outcome.target] = (count + 1, time.monotonic())
        return True

    def _answers_now(self, client: Any, url: str, timeout: float) -> bool:
        """Does ``url`` answer at all now (any HTTP status)? The origin probe's one timeout is asked once more."""
        if not self.discovery.section("auth").bool_("reask_unreachable_origin", True):
            return False
        try:
            client.get(url, timeout=timeout)
        except Exception:  # noqa: BLE001 - no answer again
            return False
        return True

    def _make_plan(
        self, outcome: TargetOutcome, host: str, product: str, offered: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """The login plan from what ``host`` offered (live or remembered), recorded for the run's choosers."""
        from . import inventory

        known = inventory.known_environ(self.ctx)
        held = inventory.held(host, self.settings, known)
        in_use: dict[str, dict[str, str]] = {}
        if self.discovery.section("auth").bool_("prefer_in_use_methods", True):
            from .existing import ExistingRegistrations

            in_use = inventory.methods_of_slots(
                ExistingRegistrations.for_run(self.ctx).in_use_slots(host), self.settings
            )
        same_login: dict[str, dict[str, str]] = {}
        if "basic" not in held and not in_use:
            for other in inventory.same_login_hosts(host, inventory.run_hosts(self.ctx), self.settings):
                login = inventory.held(other, self.settings, known).get("basic")
                if login:
                    same_login = {"basic": {**login, "HOST": other}}
                    break
        from . import login_history

        history = login_history.summary(self.ctx, host)
        outcome.auth_plan = login_methods.plan(
            host, product, offered, self.settings, held=held, same_login=same_login, in_use=in_use, history=history
        )
        # The hosts that serve this address's API (same product, they offered a
        # login): an address setting may need one of them rather than the
        # address -- osc-mcp's OSC_MCP_API wanted api.opensuse.org, not
        # build.opensuse.org (see CandidateVerifier._prove_address_forms).
        api = sorted({str(o.get("host")) for o in offered if o.get("host") and str(o.get("host")) != host})
        if api:
            self.ctx.state.setdefault("mcp_api_hosts", {})[host] = api
            # ...and which login each asked for: a server is moved only to one
            # asking for the login it sends (CandidateVerifier._sends_login_off_api_host).
            asked: dict[str, list[str]] = {}
            for o in offered:
                if o.get("host") in api and o.get("scheme"):
                    asked.setdefault(str(o["host"]), []).append(str(o["scheme"]).lower())
            self.ctx.state.setdefault("mcp_api_logins", {})[host] = asked
        log.info(
            "%s: product %s; logins offered: %s; chosen: %s",
            outcome.target,
            product or "unknown",
            ", ".join(f"{o['scheme']}@{o['host']}" for o in offered) or "none seen",
            outcome.auth_plan.get("chosen", {}).get("method") or "none",
        )
        return outcome.auth_plan

    def _note_busy(self, response: Any, url: str, busy: list[str]) -> bool:
        """Remember an identification request answered "try again later" (``login_check.temporary_statuses``)."""
        status = int(getattr(response, "status", 0) or 0)
        statuses = {
            int(c)
            for c in self.settings.list_(
                "mcp.verification.login_check.temporary_statuses", list(login_check.DEFAULT_TEMPORARY_STATUSES)
            )
        }
        if status in statuses:
            busy.append(f"HTTP {status}")
            return True
        return False

    def _identify_retries(self) -> int:
        return max(0, self.discovery.int_("identify_retries", 2))

    def _identify_pause(self, asked: float = 0.0, attempt: int = 0) -> None:
        """Wait before asking a busy host again: what it asked for (Retry-After), else a growing pause; capped."""
        config = self.settings.section("mcp.verification.login_check")
        base = config.float_("temporary_retry_seconds", 5.0) * (2 ** max(0, attempt))
        time.sleep(max(0.0, min(max(base, asked), config.float_("temporary_retry_max_seconds", 30.0))))

    @staticmethod
    def _undecided(outcome: TargetOutcome, why: str) -> None:
        """Record that what the address is stays undecided, because it said "later"; see settled()."""
        if not outcome.identification_undecided:
            outcome.identification_undecided = why
            log.info("%s: %s -- what it is stays undecided, so nothing unproven settles it", outcome.target, why)

    @staticmethod
    def _probe_with_headers(client: Any, timeout: float):
        def ask(url: str, headers: Mapping[str, str]):
            try:
                return client.get(url, headers=dict(headers), timeout=timeout)
            except Exception:  # noqa: BLE001 - no answer proves nothing
                return None

        return ask

    def weigh_login_methods(self, stage: StageOutcome, outcome: TargetOutcome) -> None:
        """Rank a password candidate below the rest when the service issues a token that can read.

        One method per entry, and the best feasible one by ``method_order``: a
        scoped, revocable token needs no password in the environment.
        """
        waiting = [c for c in stage.candidates if c.secret_env]
        if not waiting or self.settings.bool_("mcp.discovery.auth.methods_equal", True):
            # No kind of login outranks another: proof decides between them.
            return
        plan = self.auth_plan_for(outcome)
        if (plan.get("chosen") or {}).get("method") != "token":
            return
        penalty = self.settings.float_("mcp.ranking.penalties.password_when_token", 0.2)
        for candidate in waiting:
            names = " ".join(candidate.secret_env).upper()
            if "PASSWORD" in names or "PASSWD" in names:
                candidate.confidence -= penalty
                candidate.add_note(f"{parse_address(outcome.target).host} issues tokens; a token outranks a password")

    def mark_api_kinds(self, stage: StageOutcome, outcome: TargetOutcome) -> None:
        """Mark a written bridge with the API kind it serves, by evidence -- not by its author's say-so.

        sd.example.com's agent wrote a bridge for the Service Desk customer API --
        tools named after the kind's documented operations, its login accepted
        -- and did not say so; unmarked, it was not "a proven alternative", and
        an unchecked package kept the address. Evidence is either at least
        ``mcp.discovery.api_kind_min_shared_tools`` of its tools named as the
        kind's operations (all of them, for a kind with fewer), or a launch
        value naming the kind's own path on the target.
        """
        pending = [
            c
            for c in stage.candidates
            if not c.api_kind and c.origin in (Stage.BRIDGE, Stage.STRATEGY) and c.handshake and c.handshake.tools
        ]
        if not pending:
            return
        found = self.api_kinds_for(outcome)
        least = max(1, self.discovery.int_("api_kind_min_shared_tools", 2))
        for candidate in pending:
            tools = set(candidate.handshake.tools)
            values = " ".join(str(v) for v in [*candidate.full_command(), *candidate.env.values()])
            for kind, url in api_kinds.bridgeable(self.settings, found):
                shared = tools & {str(op["name"]) for op in kind.operations}
                # The directory the kind's recognising path lives in, on this target.
                prefix = urljoin(url, ".").rstrip("/") if url else ""
                by_path = bool(prefix) and prefix in values
                if len(shared) >= min(least, len(kind.operations)) or by_path:
                    candidate.api_kind = kind.name
                    candidate.add_note(
                        f"serves the {kind.label} API: "
                        + (
                            f"its tools {', '.join(sorted(shared))} are that API's documented operations"
                            if shared
                            else f"it is launched against {prefix}"
                        )
                    )
                    break

    def _client_bridge(self, target: TargetOutcome, stage: StageOutcome) -> list[Candidate]:
        """The client bridges for ``target`` -- one per login route its client can perform; [] when none."""
        from . import client_bridge

        plan = self.auth_plan_for(target)
        if not plan or not client_bridge.client_for(self.settings, plan)[0]:
            return []
        stage.findings["generated_client"] = True
        host = parse_address(target.target).host or ""
        made: list[Candidate] = []
        details: list[str] = []
        for candidate, artifacts, detail in client_bridge.generate_all(self.ctx, target.target, host, plan):
            details.append(detail)
            if artifacts is not None:
                stage.artifacts.extend([str(artifacts.script), str(artifacts.manifest)])
            if candidate is not None:
                made.append(candidate)
        stage.findings["client_bridge_detail"] = "; ".join(details)
        return made

    def untried_bridges(self, outcome: TargetOutcome, extra: Sequence[StageOutcome] = ()) -> list[str]:
        """Bridgeable API kinds of the target whose generated bridge no stage has tried yet."""
        tried = {
            str(name) for stage in [*outcome.stages, *extra] for name in stage.findings.get("generated_api_kinds") or []
        }
        found = self.api_kinds_for(outcome)
        untried = [kind.name for kind, _ in api_kinds.bridgeable(self.settings, found) if kind.name not in tried]
        # The client bridge counts as a bridge the run can build itself, too.
        from . import client_bridge

        # Asked, not merely read: the plan is made on first need (auth_plan_for
        # caches it), and build.example.net settled on a refused entry because no
        # earlier step had needed it -- so its client bridge was never built.
        plan = self.auth_plan_for(outcome)
        if (
            plan
            and client_bridge.client_for(self.settings, plan)[0]
            and not any(s.findings.get("generated_client") for s in [*outcome.stages, *extra])
        ):
            untried.append(f"the {client_bridge.client_for(self.settings, plan)[0]} client bridge")
        return untried

    def owes_generated_bridge(self, name: str, outcome: TargetOutcome, stage: StageOutcome | None = None) -> bool:
        """Must the bridge stage (still) generate the bridge for a documented API kind?

        The run can build that bridge itself -- documented operations, the
        documented way of sending the credential -- so it is tried before the
        address is settled, unless something has already *proved* access: an
        accepted login, or a server that works with nothing left unproved. A
        server only waiting for a credential, or one whose credential was
        refused by an agent's own bridge, proves nothing. sd.example.com's
        agent-written bridge ended the bridge stage that way, and the bridge the
        brief promised "the run generates" was never built.
        (``mcp.discovery.generated_bridge_settles_first``.)
        """
        if name != Stage.BRIDGE or not self.discovery.bool_("generated_bridge_settles_first", True):
            return False
        pool = [*outcome.verified, *outcome.provisional]
        if stage is not None:
            pool += [*stage.verified, *stage.provisional, *stage.candidates]
        # An operator who lets unproved access settle (unproved_settles) means a
        # working server, however empty its answers, is enough.
        unproved_settles = self.settings.bool_("mcp.verification.login_check.unproved_settles", False)
        if any(proves_access(c) or (unproved_settles and c.verified) for c in pool):
            return False
        return bool(self.untried_bridges(outcome, [stage] if stage is not None else []))

    # -- the tier gate ---------------------------------------------------------

    def gate(self, name: str, target: TargetOutcome, mode: str = "") -> StageOutcome | None:
        """``None`` if the stage may run; a refusal to report if it may not.

        This is where the policy stops being a preference. A package download or
        a generated bridge, for a target that already answers MCP on its own
        address, is work nobody asked for and somebody will have to maintain --
        so it does not happen, and the report says which rule prevented it and
        how to override it deliberately.
        """
        allowed, why = self.policy.gate(name, target.due_diligence)
        if allowed:
            return None
        log.info("tier policy blocked the %s stage: %s", name, why)
        # The mode of the pipeline that *decided*, not of the class that happens
        # to own this method. Both pipelines share this rule -- the agentic one
        # calls it through `fallback` -- so stamping `self.mode` labelled an
        # agentic run's skips "traditional", which reads as "the agent gave up
        # here" when the agent was running throughout.
        stage = StageOutcome(stage=name, mode=mode or self.mode, status=Status.SKIPPED, detail=why)
        stage.findings["blocked_by_policy"] = why
        stage.findings["tier"] = self.policy.tier(name)
        stage.add_remediation(*self.policy.remediation(name, target.due_diligence))
        return stage

    # -- stage 1: endpoint -----------------------------------------------------

    def stage_endpoint(self, target: TargetOutcome) -> StageOutcome:
        """Probe the target itself for a live MCP endpoint -- the whole checklist.

        This is tier 1, and everything below it is gated on what happens here, so
        the stage does not merely look: it performs steps A, B, C and D and
        records what each one tried. A search that stops early because it found
        the endpoint is fine -- the gate closes on a native endpoint anyway -- but
        a search that stops early and then reports "no MCP here" is not, and the
        record is what tells those two apart.
        """
        stage = StageOutcome(stage=Stage.ENDPOINT, mode=self.mode)
        record = self._record_for(target)
        origins = pathgen.base_urls(target.target, self.discovery)
        if not origins:
            stage.status = Status.FAILED
            stage.detail = f"{target.target!r} is not a usable server address"
            record.record("paths", Status.FAILED, "the address could not be turned into a URL")
            return stage

        stage.findings["origins"] = origins
        self._gather_hints(target, stage)
        grounded = self._ground_semantically(target, stage, record)

        mandatory = self.policy.mandatory_paths()
        candidates = pathgen.generate(target.target, self.discovery, mandatory=mandatory)
        for url in stage.findings.get("html_links", []):
            candidates.append(
                pathgen.UrlCandidate(
                    url=url, transport=Transport.STREAMABLE_HTTP, reason="linked from the target's landing page", rank=5
                )
            )
        candidates.extend(grounded)
        candidates.sort(key=lambda c: c.rank)
        stage.findings["probed"] = len(candidates)

        probe = self._probe_concurrently(candidates)
        stage.candidates = probe.attempted
        stage.verified = probe.verified
        stage.findings["auth_required_urls"] = probe.auth_walls
        if probe.websockets:
            stage.findings["websocket_urls"] = probe.websockets
        stage.findings["paths_probed"] = probe.urls[:200]

        self._record_search(record, stage, probe, mandatory, origins)
        # Added to, never assigned over: this stage also runs as the fallback
        # behind an agent that already found a tier-1 endpoint, and replacing
        # the list would throw that evidence away -- reopening the lower tiers
        # for a target whose own endpoint had been found minutes earlier.
        #
        # And only what could be written down for this target. An endpoint that
        # answered is not yet the target's endpoint: mcp.0dteconfluence.com, an
        # options-trading server the registry returned for "confluence",
        # handshaked with 37 tools, was recorded as confluence.example.com's native
        # endpoint and closed every lower tier -- then the registration check
        # refused it and the address ended with nothing. The same check, asked
        # before the record is written.
        record.note("native_endpoints", self.admissible(probe.verified, target))
        walled = [c for c in probe.attempted if c.url in probe.auth_walls]
        record.note("auth_walls", self.admissible(walled, target))
        # A WebSocket route is a native endpoint too -- one Claude Code cannot
        # dial directly, which is a reason to build a bridge *for it* and not a
        # reason to go looking for somebody's package instead.
        record.note(
            "needs_bridge",
            (url for url in probe.websockets if url not in record.native_endpoints),
        )
        stage.findings["due_diligence"] = record.to_dict()

        if probe.verified:
            best = probe.verified[0]
            stage.status = Status.OK
            stage.detail = f"{best.url} -- {best.handshake.summary()}"
            stage.findings["endpoint_url"] = best.url
            if best.handshake and best.handshake.insecure_tls:
                stage.add_remediation(
                    f"{best.url} only answered with TLS certificate verification disabled. "
                    "Install the issuing CA certificate on this host and re-run, so the server "
                    "is reached over a connection whose peer is actually authenticated.",
                )
            return stage

        stage.status = Status.PARTIAL if (probe.auth_walls or probe.websockets) else Status.SKIPPED
        if probe.auth_walls:
            stage.detail = f"{len(probe.auth_walls)} endpoint(s) answered but require authentication"
            stage.add_remediation(
                f"{probe.auth_walls[0]} is an MCP endpoint that refused an anonymous client -- "
                "which is how it proved it is there. If it passes the registration checks it is "
                "written out with its credential slot empty, so supplying a token is an export "
                "rather than another discovery run.",
                "To probe with a token instead, so the handshake completes during this run: set "
                "INITIALIZE_CLAUDE_AI_MCP_TOKEN_REF=env:YOUR_VAR (the value is read at run time).",
            )
        elif probe.websockets:
            stage.detail = f"a WebSocket endpoint answered at {probe.websockets[0]}"
            stage.add_remediation(
                f"{probe.websockets[0]} upgrades to WebSocket. Claude Code has no WebSocket MCP "
                "transport, so this needs a stdio bridge in front of it -- which is what the "
                "bridge stage will build, against the target's own endpoint rather than a copy of it.",
            )
        else:
            stage.detail = f"no MCP endpoint answered on {len(probe.urls)} probed path(s); {record.summary()}"
        return stage

    # -- the tier-1 checklist ---------------------------------------------------

    def admissible(self, candidates: Iterable[Candidate], outcome: TargetOutcome) -> list[str]:
        """URLs of ``candidates`` that may be recorded as the target's own endpoint.

        The registration check decides, because "tier 1 exists" is only true of
        an endpoint that could be registered for the target: one that cannot
        is retracted from the record, with the reason, so no route can record
        it later either. Switched off by
        ``mcp.policy.native_must_be_registrable = false``, which records every
        endpoint that answered, as before.
        """
        record = self._record_for(outcome)
        enforce = self.settings.bool_("mcp.policy.native_must_be_registrable", True)
        urls: list[str] = []
        for candidate in candidates:
            if not candidate.url:
                continue
            if enforce and not self.registrable(candidate, outcome):
                self._rule_out(record, candidate, outcome)
                continue
            urls.append(candidate.url)
        return urls

    def _rule_out(self, record: DueDiligence, candidate: Candidate, outcome: TargetOutcome, reason: str = "") -> None:
        why = reason or self.why_not_registrable(candidate, outcome)
        first_time = candidate.url not in record.ruled_out
        if record.retract(candidate.url, why) or first_time:
            log.info(
                "%s: %s is not counted as this target's MCP endpoint, so it closes no tier: %s",
                outcome.target,
                candidate.url,
                why,
            )

    def retract_endpoints(
        self, dropped: Iterable[Candidate], outcome: TargetOutcome, reason: Callable[[Candidate], str]
    ) -> None:
        """Take dropped URL candidates out of the tier-1 record.

        Whatever route recorded them -- the probe, an agent's checklist, a
        stage that ran before the check existed -- a URL that was just refused
        as an answer may not go on refusing the tiers below.
        """
        if not self.settings.bool_("mcp.policy.native_must_be_registrable", True):
            return
        for candidate in dropped:
            if candidate.kind == "url" and candidate.url:
                # Created if need be: an agent that returned no checklist still
                # has to be told, on its retry, which endpoint is closed.
                self._rule_out(self._record_for(outcome), candidate, outcome, reason(candidate))

    @staticmethod
    def _record_for(target: TargetOutcome) -> DueDiligence:
        """The target's checklist, created on first use and shared thereafter."""
        if not isinstance(target.due_diligence, DueDiligence):
            target.due_diligence = DueDiligence(target=target.target)
        return target.due_diligence

    def _record_search(
        self,
        record: DueDiligence,
        stage: StageOutcome,
        probe: "ProbeRun",
        mandatory: Sequence[str],
        origins: Sequence[str],
    ) -> None:
        """Steps A, B and C, written down from what the probing actually did."""
        # -- A: diverse paths
        hit, missed = pathgen.covered(probe.urls, mandatory)
        evidence = [f"{len(probe.urls)} URL(s) probed across {len(origins)} origin(s)"]
        evidence.extend(f"checked {path}" for path in hit)
        if missed:
            record.record(
                "paths",
                Status.PARTIAL,
                f"{len(hit)}/{len(hit) + len(missed)} required paths probed; not reached: {', '.join(missed)}",
                evidence=evidence,
            )
        else:
            record.record("paths", Status.OK, f"all {len(hit)} required paths probed", evidence=evidence)

        # -- B: schemes and TLS
        schemes = sorted({url.split("://", 1)[0] for url in probe.urls if "://" in url})
        insecure = [c.url for c in probe.attempted if c.handshake and c.handshake.insecure_tls]
        retry = self.discovery.bool_("tls_insecure_retry", True)
        bypass_evidence = [f"schemes tried: {', '.join(schemes) or 'none'}"]
        bypass_evidence.append(
            "certificate verification was bypassed on retry where TLS failed"
            if retry
            else "the insecure retry is disabled (mcp.discovery.tls_insecure_retry)"
        )
        bypass_evidence.extend(f"{url} answered only without certificate verification" for url in insecure)
        record.record(
            "bypasses",
            Status.OK if (len(schemes) > 1 and retry) else Status.PARTIAL,
            f"{len(schemes)} scheme(s) tried"
            + (f"; {len(insecure)} endpoint(s) needed the TLS bypass" if insecure else ""),
            evidence=bypass_evidence,
        )

        # -- C: handshake headers, and what came back
        hints: list[str] = []
        for candidate in probe.attempted:
            for hint in candidate.handshake.protocol_hints if candidate.handshake else []:
                text = f"{candidate.url}: {hint}"
                if text not in hints:
                    hints.append(text)
        record.record(
            "handshake",
            Status.OK,
            f"the specification's Accept headers were sent on every probe; {len(hints)} protocol hint(s) came back",
            evidence=[
                f"accept: {self.prober.accept}",
                "content-type: application/json",
                *hints[:20],
            ],
        )
        if hints:
            stage.findings["protocol_hints"] = hints[:40]

    def _ground_semantically(
        self, target: TargetOutcome, stage: StageOutcome, record: DueDiligence
    ) -> list[pathgen.UrlCandidate]:
        """Step D without a model: let the target and the registries speak.

        An agent does this with a web search. Here it is three deterministic
        sources that answer the same question -- does anyone *say* this target
        has an MCP endpoint: the pages a vendor announces one on (``/llms.txt``
        and the documentation roots), the official MCP registry's ``remotes[]``
        entries, and an operator-supplied search endpoint if there is one. Any
        URL that comes back is probed like every other candidate; nothing here
        is believed on the strength of having been written down.
        """
        grounding = self.discovery.section("web_grounding")
        if not grounding.bool_("enabled", True):
            record.record(
                "grounding",
                Status.SKIPPED,
                excused="semantic grounding is disabled (mcp.discovery.web_grounding.enabled)",
            )
            return []

        evidence: list[str] = []
        found: list[pathgen.UrlCandidate] = []
        keywords = [
            k.lower()
            for k in grounding.str_list(
                "keywords",
                [
                    "model context protocol",
                    "mcp server",
                    "mcp endpoint",
                    "/mcp",
                    "mcp.json",
                ],
            )
            if k
        ]
        timeout = grounding.float_("timeout_seconds", 8)
        limit = grounding.int_("max_documents", 8)
        headers = self.verifier.auth_headers()
        patterns = self.discovery.str_list(
            "html_link_patterns",
            [
                "(?i)(?:href|src|action)=[\"']([^\"']*mcp[^\"']*)[\"']",
                "(?i)[\"'](https?://[^\"']*/mcp[^\"']*)[\"']",
            ],
        )
        read = 0

        with self.ctx.http(timeout=timeout) as client:
            for url in pathgen.doc_urls(target.target, self.discovery)[:limit]:
                response = client.get(url, headers=headers, timeout=timeout)
                if not response.ok or not response.text:
                    continue
                read += 1
                body = response.text
                mentions = [word for word in keywords if word in body.lower()]
                if not mentions:
                    continue
                evidence.append(f"{url} mentions {', '.join(mentions[:3])}")
                for link in pathgen.mine_html(body, url, patterns):
                    found.append(
                        pathgen.UrlCandidate(
                            url=link,
                            transport=Transport.STREAMABLE_HTTP,
                            reason=f"named in the target's own documentation ({url})",
                            rank=3,
                        )
                    )

            search_url = self._search_url(grounding, target.target)
            if search_url:
                response = client.get(search_url, timeout=timeout)
                if response.ok and response.text:
                    pattern = grounding.str_("search_url_pattern", "https?://[^\\s\"'<>]+")
                    links = pathgen.mine_html(response.text, search_url, [pattern] if pattern else [])
                    evidence.append(f"the configured search endpoint returned {len(links)} link(s)")
                    stage.findings["search_results"] = links[:20]
                else:
                    evidence.append("the configured search endpoint returned nothing usable")

        if grounding.bool_("use_registry", True):
            remotes, registry_note = self._registry_remotes(target.target)
            evidence.append(registry_note)
            found.extend(remotes)

        if grounding.bool_("use_repositories", True):
            # Not for the packages -- that is stage 2's job, after this gate.
            # A repository is read here only for what it *says* about the
            # target: "we expose MCP at <url>" in a README is step-D evidence.
            queries = self.registry.queries_for(target.target)
            repos = self.registry.search_github(queries[0]) if queries else []
            evidence.append(f"{len(repos)} public repositor(y/ies) mention this target")
            stage.findings["repositories"] = [h.name for h in repos[:10]]

        stage.findings["documentation_read"] = read
        if found:
            evidence.append(f"{len(found)} documented endpoint URL(s) queued for probing")
        if evidence:
            record.record(
                "grounding",
                Status.OK if found or read else Status.PARTIAL,
                f"{read} document(s) read; {len(found)} documented endpoint(s) to probe",
                evidence=evidence,
            )
        else:
            # Nothing was reachable -- which is a fact about the network, not a
            # conclusion about the target, and is recorded as such.
            record.record(
                "grounding",
                Status.PARTIAL,
                "no documentation, registry entry or search result could be read for this target",
                evidence=["all grounding sources were unreachable or empty"],
            )
        return found

    def _search_url(self, grounding, target: str) -> str:
        """The operator's own search endpoint, if they configured one.

        There is no default: sending an internal hostname to a third-party search
        engine is a disclosure, and it is the operator's call rather than this
        program's. Agentic mode uses Claude Code's own WebSearch instead.
        """
        template = grounding.str_("search_url_template")
        if not template:
            return ""
        query = grounding.str_("search_query_template", "{target} model context protocol MCP endpoint").replace(
            "{target}", parse_address(target).host or target
        )
        return template.replace("{query}", quote(query, safe=""))

    def _registry_remotes(self, target: str) -> tuple[list[pathgen.UrlCandidate], str]:
        """Hosted endpoints the official MCP registry publishes for this target.

        A ``remotes[]`` entry is not a package: it is a URL the vendor (or
        somebody) has registered as *the* MCP endpoint for a service. That makes
        it tier-1 evidence, which is why this is read during the endpoint stage
        and not left to the package stage that comes after the gate.

        **An entry on another hostname is probed, not dropped.** It used to be
        discarded unless ``_same_service`` matched the target, and that cost a
        real server: the registry publishes
        ``com.googleapis.developerknowledge/mcp`` with ``remotes[]`` only, whose
        URL is ``https://developerknowledge.googleapis.com/mcp`` -- Google's own
        MCP endpoint for ``developers.google.com``. Different hostname, so it
        never became a candidate, was never probed, and was never registered.
        Step D still recorded it, so the tier gate then refused tiers 2, 3 and 4
        *because a native endpoint exists* -- and the address finished with
        nothing at all, having ruled out every alternative on the strength of an
        endpoint nobody wrote down.

        Serving MCP from a sibling domain is the ordinary shape of a hosted
        offering (``github.com`` from ``api.githubcopilot.com``), and a registry
        entry returned for *this target's* query is the vendor saying where
        theirs lives -- which is the provenance a bare off-host URL lacks. So it
        is probed; whether it may be *registered* is then decided where that
        belongs, by :mod:`.grounding`, which requires it to have answered.

        On-host remains better evidence and is ranked ahead of it, and the
        number of other hosts one registry answer may send us to is capped.
        """
        # The registry's own questions: it matches server names, so the
        # full-text phrases the other sources are asked return nothing here.
        queries = self.registry.registry_queries_for(target)
        if not queries:
            return [], "the official registry was not queried (no query template configured)"
        host = (parse_address(target).host or target).lower()
        limit = self.discovery.int_("registry_remote_other_hosts", 3)
        out: list[pathgen.UrlCandidate] = []
        elsewhere = 0
        hits = self.registry.search_official(queries[0])
        for hit in hits:
            for candidate in self.registry.to_candidates(hit):
                if candidate.kind != "url" or not candidate.url:
                    continue
                other = (parse_address(candidate.url).host or "").lower()
                if not other:
                    continue
                on_host = same_service(host, other)
                if not on_host:
                    if limit and elsewhere >= limit:
                        continue
                    elsewhere += 1
                out.append(
                    pathgen.UrlCandidate(
                        url=candidate.url,
                        transport=candidate.transport or Transport.STREAMABLE_HTTP,
                        reason=(
                            f"published as a remote endpoint in the official MCP registry ({hit.name})"
                            if on_host
                            else f"published in the official MCP registry ({hit.name}) as the "
                            f"remote endpoint for this service, on {other}"
                        ),
                        # Probed after the target's own paths: a URL on the
                        # target is better evidence than one that merely claims
                        # to serve it, so it should win when both answer.
                        rank=2 if on_host else 3,
                    )
                )
        note = (
            f"the official MCP registry returned {len(hits)} entr(y/ies) for {queries[0]!r}; "
            f"{len(out) - elsewhere} name a remote endpoint on this service"
            + (f", {elsewhere} on another host" if elsewhere else "")
        )
        return out, note

    def _gather_hints(self, target: TargetOutcome, stage: StageOutcome) -> None:
        """Cheap GETs that often answer the question outright.

        Read-only evidence, fetched with the run's trust store (see
        :class:`~..core.trust_store.TrustStore`). A certificate nothing here
        vouches for is recorded as exactly that -- ``origin_tls_error`` -- and,
        with ``tls_insecure_retry``, the same GET is repeated once without
        verification and without credentials, so a certificate problem does
        not also cost the evidence. Whatever is registered from it is still
        verified with verification on.
        """
        timeout = self.discovery.float_("per_request_timeout_seconds", 8)
        headers = self.verifier.auth_headers()

        with (
            self.ctx.http(timeout=timeout) as client,
            _Tolerant(self.ctx, self.discovery, timeout) as tolerant,
        ):

            def fetch(url: str, *, send: Mapping[str, str] | None = None):
                response = client.get(url, headers=headers if send is None else send, timeout=timeout)
                if tolerant.is_certificate_failure(response):
                    stage.findings.setdefault("tls_errors", {})[url] = response.error
                    retry = tolerant.get(url)
                    if retry is not None and retry.status:
                        stage.findings.setdefault("read_without_verification", []).append(url)
                        return retry
                return response

            if self.discovery.bool_("preflight", True):
                origin = pathgen.base_urls(target.target, self.discovery)[0]
                response = client.get(origin, headers=headers, timeout=timeout)
                if tolerant.is_certificate_failure(response):
                    # It answered -- with a certificate. That is a fact about this
                    # machine's trust store, not about whether the host is there.
                    stage.findings["origin_tls_error"] = response.error
                    retry = tolerant.get(origin)
                    if retry is not None and retry.status:
                        response = retry
                stage.findings["origin_status"] = response.status
                if response.status == 0 and not stage.findings.get("origin_tls_error"):
                    stage.add_remediation(f"{origin} is unreachable: {response.error}")
                elif self.discovery.bool_("scan_html", True) and "html" in (response.content_type or ""):
                    links = pathgen.mine_html(
                        response.text,
                        origin,
                        self.discovery.str_list(
                            "html_link_patterns",
                            [
                                "(?i)(?:href|src|action)=[\"']([^\"']*mcp[^\"']*)[\"']",
                                "(?i)[\"'](https?://[^\"']*/mcp[^\"']*)[\"']",
                            ],
                        ),
                    )
                    if links:
                        stage.findings["html_links"] = links

            for url in pathgen.well_known_urls(target.target, self.discovery):
                response = fetch(url)
                if not response.ok:
                    continue
                payload = response.json()
                if payload is not None:
                    stage.findings.setdefault("well_known", {})[url] = clip(
                        payload, self.discovery.int_("well_known_chars", 1200)
                    )

            # The OpenAPI spec is stage 3's raw material; fetch it while we are here.
            self._find_api_description(target, stage, fetch, limit=getattr(client, "max_bytes", 0))
            # ...and any other kind of API a generic server can bridge.
            if stage.findings.get("origin_status") != 0 or stage.findings.get("origin_tls_error"):
                found = self._find_api_kinds(target, client, fetch, headers, timeout)
                if found:
                    stage.findings["api_kinds"] = found

    def _find_api_description(self, target: TargetOutcome, stage: StageOutcome, fetch, *, limit: int = 0) -> None:
        """The conventional paths first, then the pages that render one for people.

        A description is accepted as JSON or, when PyYAML is installed, YAML.
        One that is recognisably a description but cannot be parsed here is
        still recorded (``openapi_unparsed_url``) so the agent and the operator
        are told where it is. So is one that filled the response limit
        (``http.max_response_bytes``): the client cuts a body there without
        saying so, and a cut YAML document can still parse -- into a bridge
        missing whatever came after the cut.
        """

        def take(url: str) -> bool:
            response = fetch(url)
            if not response.ok:
                return False
            if limit and len(response.text) >= limit:
                if apispec.looks_like_description(response.text) or response.text.lstrip()[:1] in "{[":
                    stage.findings.setdefault("openapi_unparsed_url", url)
                    stage.findings["openapi_truncated"] = True
                return False
            payload = apispec.parse(response.text)
            if payload is not None:
                stage.findings["openapi_spec"] = payload
                stage.findings["openapi_url"] = url
                return True
            if apispec.looks_like_description(response.text):
                stage.findings.setdefault("openapi_unparsed_url", url)
            return False

        for url in pathgen.api_spec_urls(target.target, self.discovery):
            if take(url):
                return

        same_host = self.discovery.bool_("api_spec_same_host_only", True)
        link_patterns = self.discovery.str_list(
            "api_spec_link_patterns",
            [
                "(?i)\\burl\\s*:\\s*[\"']([^\"']+\\.(?:json|ya?ml)(?:\\?[^\"']*)?)[\"']",
                "(?i)spec-url=[\"']([^\"']+)[\"']",
                "(?i)(?:href|src)=[\"']([^\"']*(?:openapi|swagger)[^\"'/]*\\.(?:json|ya?ml))[\"']",
            ],
        )
        script_patterns = self.discovery.str_list(
            "api_doc_script_patterns",
            [
                "(?i)<script[^>]+src=[\"']([^\"']*(?:initializer|config)[^\"']*\\.js)[\"']",
            ],
        )
        max_scripts = self.discovery.int_("api_doc_max_scripts", 3)
        tried: set[str] = set()
        for page in pathgen.api_doc_urls(target.target, self.discovery):
            response = fetch(page)
            if not response.ok:
                continue
            base = response.final_url or page
            owner = page if same_host else ""
            found = apispec.links(response.text, base, link_patterns, same_host_as=owner)
            for script in apispec.links(response.text, base, script_patterns, same_host_as=owner, limit=max_scripts):
                code = fetch(script)
                if code.ok:
                    found += apispec.links(code.text, code.final_url or script, link_patterns, same_host_as=owner)
            for url in found:
                if url in tried:
                    continue
                tried.add(url)
                if take(url):
                    stage.findings["openapi_found_via"] = page
                    return

    def _find_api_kinds(
        self, target: TargetOutcome, client, fetch, headers: Mapping[str, str], timeout: float
    ) -> dict[str, dict[str, str]]:
        """Every probed kind of :mod:`.api_kinds` the target serves, as ``{name: {"label", "url"}}``."""
        origin = pathgen.base_urls(target.target, self.discovery)[0]
        same_host = self.discovery.bool_("api_kinds_same_host_only", True)
        send = self._api_kind_sender(client, headers, timeout)
        found: dict[str, dict[str, str]] = {}
        for kind in api_kinds.kinds(self.settings):
            if not kind.probed:
                continue
            url = api_kinds.detect(
                kind,
                origin,
                send=send,
                fetch=fetch,
                same_host=same_host,
                control=self.api_kind_control(),
                behind_login=self.discovery.bool_("api_kinds_behind_login", True),
            )
            if url:
                log.info("%s: serves a %s API at %s", target.target, kind.label, url)
                found[kind.name] = {"label": kind.label, "url": url}
        return found

    def api_kind_control(self) -> str:
        """``mcp.discovery.api_kind_control_name``: the control request's last path segment."""
        return self.discovery.str_("api_kind_control_name", api_kinds.DEFAULT_CONTROL_NAME).strip()

    @staticmethod
    def _api_kind_sender(client, headers: Mapping[str, str], timeout: float):
        def send(method: str, url: str, content_type: str, body: str):
            sent = {**headers, "content-type": content_type} if content_type else dict(headers)
            call = getattr(client, method.lower(), None)
            if call is None:
                return None  # a client that cannot make this request has no answer to give
            return call(url, headers=sent, data=body or None, timeout=timeout)

        return send

    def _confirmed_api_kinds(self, target: TargetOutcome, recorded: Mapping[str, Any]) -> dict[str, Any]:
        """The API kinds another mode recorded (an agent's ``findings.api_kinds``) that pass :func:`api_kinds.confirm`.

        An agent's word that a target serves an API is a lead, held to the same
        test as a detection here -- a 2xx of the declared form, not an echo, and
        a control that is not answered the same way -- before a bridge is built
        for it. A kind with nothing to ask (``finding``) is kept as recorded.
        """
        by_name = {kind.name: kind for kind in api_kinds.kinds(self.settings)}
        timeout = self.discovery.float_("per_request_timeout_seconds", 8)
        kept: dict[str, Any] = {}
        for name, where in recorded.items():
            url = str(where.get("url") or "") if isinstance(where, Mapping) else str(where or "")
            kind = by_name.get(str(name))
            if kind is None or not kind.probed or not url:
                kept[name] = where
                continue
            if url not in self._confirmed_kinds:
                with self.ctx.http(timeout=timeout) as client:
                    send = self._api_kind_sender(client, self.verifier.auth_headers(), timeout)
                    self._confirmed_kinds[url] = api_kinds.confirm(
                        kind,
                        url,
                        send=send,
                        control=self.api_kind_control(),
                        behind_login=self.discovery.bool_("api_kinds_behind_login", True),
                    )
            why = self._confirmed_kinds[url]
            if why:
                log.info(
                    "%s: recorded as serving a %s API at %s, but it does not: %s", target.target, kind.label, url, why
                )
                continue
            kept[name] = where
        return kept

    def api_kinds_for(self, target: TargetOutcome) -> dict[str, dict[str, str]]:
        """The API kinds known for ``target``, looking now if no endpoint stage here did.

        In agentic mode tier 1 is an agent's, and the cheap hints of
        :meth:`_gather_hints` are never gathered; the package and bridge stages
        still need to know that the target serves, say, GraphQL. So they are
        looked for once per address, on first need.
        """
        findings = target.all_findings()
        # What another mode recorded is confirmed here; what this pipeline
        # detected already passed the same test.
        ours: dict[str, Any] = {}
        theirs: dict[str, Any] = {}
        for s in target.stages:
            recorded = s.findings.get("api_kinds")
            if isinstance(recorded, Mapping):
                (ours if s.mode == self.mode else theirs).update(recorded)
        if theirs:
            findings = {**findings, "api_kinds": {**self._confirmed_api_kinds(target, theirs), **ours}}
        asked = any(
            s.stage == Stage.ENDPOINT and s.mode == self.mode and "origin_status" in s.findings for s in target.stages
        )
        # Nothing recorded that stood up is the same as nothing recorded: looked for here.
        if not asked and not findings.get("api_kinds") and findings.get("origin_status") != 0:
            if target.target not in self._api_kinds:
                timeout = self.discovery.float_("per_request_timeout_seconds", 8)
                headers = self.verifier.auth_headers()
                busy: list[str] = []
                with self.ctx.http(timeout=timeout) as client:

                    def fetch(url: str):
                        try:
                            response = client.get(url, headers=headers, timeout=timeout)
                        except Exception as exc:  # noqa: BLE001 - no answer is not "nothing here"
                            busy.append(f"no answer ({type(exc).__name__})")
                            raise
                        self._note_busy(response, url, busy)
                        return response

                    found_kinds = self._find_api_kinds(target, client, fetch, headers, timeout)
                    for _ in range(self._identify_retries() if not found_kinds and busy else 0):
                        self._identify_pause()
                        busy.clear()
                        found_kinds = self._find_api_kinds(target, client, fetch, headers, timeout)
                        if found_kinds or not busy:
                            break
                    if not found_kinds and busy:
                        self._undecided(
                            target,
                            f"{parse_address(target.target).host} answered {busy[-1]} while its APIs "
                            "were being looked for",
                        )
                    self._api_kinds[target.target] = found_kinds
            findings = {
                **findings,
                "api_kinds": {**self._api_kinds[target.target], **(findings.get("api_kinds") or {})},
            }
        found = api_kinds.described(self.settings, findings, host=parse_address(target.target).host or "")
        # What any path already found for this address stays found: kept on
        # the outcome, so the writer, the report and every later stage name it
        # without probing again.
        found = {**dict(target.api_kinds or {}), **found}
        target.api_kinds = dict(found)
        return found

    def _probe_concurrently(self, candidates: Sequence[pathgen.UrlCandidate]) -> ProbeRun:
        """Handshake every candidate URL, by default stopping at the first hit.

        ``stop_at_first`` is what --the-best-mcp turns off: one endpoint is all
        that is needed to register a server, but it is not enough to choose
        between servers, and a path that was never probed cannot win.

        Stopping early is still honest about itself: the run reports only the
        URLs whose probes actually completed, so step A of the checklist cannot
        claim coverage the sweep abandoned.
        """
        workers = max(1, self.discovery.int_("max_concurrency", 8))
        timeout = self.discovery.float_("per_request_timeout_seconds", 8)
        budget = self.discovery.float_("stage_timeout_seconds", 180)
        stop_at_first = self.discovery.bool_("stop_at_first", True)
        headers = self.verifier.auth_headers()
        deadline = time.monotonic() + budget if budget > 0 else 0.0
        run = ProbeRun()

        def probe(entry: pathgen.UrlCandidate) -> Candidate:
            candidate = Candidate(
                kind="url",
                url=entry.url,
                transport=entry.transport,
                origin=Stage.ENDPOINT,
                source=entry.reason,
                # The rank scale spans several origins, each with its own block
                # of path templates, so the divisor has to outrun the last of
                # them or every late candidate would arrive at the floor.
                confidence=max(0.1, 1.0 - entry.rank / _RANK_SCALE),
            )
            if entry.transport == Transport.SSE:
                candidate.handshake = self.prober.probe_sse(entry.url, headers=headers, timeout=timeout)
            elif entry.transport == Transport.WEBSOCKET:
                candidate.handshake = self.prober.probe_websocket(entry.url, headers=headers, timeout=timeout)
            else:
                candidate.handshake = self.prober.probe_http(entry.url, headers=headers, timeout=timeout)
                self.verifier.credit_auth_claim(candidate)
            return candidate

        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(probe, entry): entry for entry in candidates}
            try:
                for future in concurrent.futures.as_completed(futures):
                    if deadline and time.monotonic() > deadline:
                        log.debug("endpoint stage budget exhausted")
                        break
                    candidate = future.result()
                    run.urls.append(candidate.url)
                    handshake = candidate.handshake
                    # needs_credential, not requires_auth: a host that answers
                    # 401 on every URL it owns would otherwise fill this list
                    # with its own front door and close the lower tiers on the
                    # strength of a login page.
                    if candidate.needs_credential:
                        run.auth_walls.append(candidate.url)
                    if candidate.transport == Transport.WEBSOCKET:
                        self.remember_socket(candidate.url, handshake)
                    if handshake and WEBSOCKET_HINT in handshake.protocol_hints:
                        run.websockets.append(candidate.url)
                    # Only keep the noise that says something; a 404 on
                    # /gateway/mcp is not worth a line in the report.
                    if handshake and (
                        handshake.ok or handshake.requires_auth or handshake.status >= 500 or handshake.protocol_hints
                    ):
                        run.attempted.append(candidate)
                    if handshake and handshake.ok:
                        accepted, why = self.prober.accepts(handshake)
                        candidate.add_note(why)
                        if accepted:
                            run.verified.append(candidate)
                            if stop_at_first:
                                break
            finally:
                for future in futures:
                    future.cancel()

        # Completion order is whatever the network decided; the path templates
        # are ordered by how likely they are to be the real endpoint, and that
        # is the order a reader (and a tie-break) should see.
        run.verified.sort(key=lambda c: -c.confidence)
        return run

    # -- stage 2: existing package ---------------------------------------------

    def stage_package(self, target: TargetOutcome) -> StageOutcome:
        """Find a server someone has already published for this target."""
        stage = StageOutcome(stage=Stage.PACKAGE, mode=self.mode)
        if not self.settings.bool_("mcp.packages.enabled", True):
            stage.detail = "disabled by configuration"
            return stage

        problems: list[str] = []
        hits = self.registry.search_all(target.target, problems)
        stage.findings["search_hits"] = [h.to_dict() for h in hits[:20]]
        if problems:
            # Said in the report, not only in the debug log: "no published MCP
            # server matched" and "the registry did not answer" call for
            # different next steps, and only the second is fixed by re-running.
            stage.findings["search_problems"] = problems[:10]
            log.info(
                "%s: %d package source request(s) could not be completed: %s",
                target.target,
                len(problems),
                "; ".join(problems[:3]),
            )
            stage.add_remediation(
                f"{len(problems)} package-search request(s) got no usable answer "
                f"({problems[0]}). The search may be incomplete; re-run, or raise "
                "mcp.packages.registry_timeout_seconds / search_timeout_seconds."
            )
        # The searches carry their own queries in the hits they return; the PyPI
        # lookups return nothing at all when no name matched, so record what was
        # asked. Otherwise "no published MCP server matched" is indistinguishable
        # from "PyPI was never asked".
        names = self.registry.pypi_names(target.target)
        if names:
            stage.findings["pypi_names"] = names
        # A generic server for the kind of API the target serves -- mcp-graphql
        # for a GraphQL API -- is a published server for it too, found by what
        # the target *is* rather than by what it is called.
        generic = api_kinds.adapter_candidates(
            self.ctx, target.target, self.api_kinds_for(target), base_url=target.base_url
        )
        if generic:
            stage.findings["api_kind_candidates"] = [c.label for c in generic]
        if not hits and not generic:
            stage.detail = "no published MCP server matched this target"
            return stage

        candidates: list[Candidate] = list(generic)
        for hit in hits:
            candidates.extend(self.registry.to_candidates(hit, target.target))
        candidates = dedupe(candidates)
        candidates = self._relevant_only(candidates, target, stage)
        stage.candidates = candidates
        if not candidates:
            stage.detail = f"{len(hits)} search hit(s), none of which is about this target"
            return stage
        stage.detail = f"{len(hits)} search hit(s), {len(candidates)} launchable candidate(s)"
        if generic:
            stage.detail += f" ({len(generic)} generic server(s) for the API kind it serves)"

        stage.verified = self.verifier.verify_all(
            candidates,
            stop_at_first=not self.settings.bool_("mcp.exhaustive", False),
            budget_seconds=self.discovery.float_("stage_timeout_seconds", 180),
            keep=lambda c: self.registrable(c, target),
            memo=target.trials,
            why=lambda c: self.why_not_registrable(c, target),
        )
        if stage.verified:
            stage.status = Status.OK
            stage.detail = f"{stage.verified[0].label} verified ({stage.verified[0].source})"
            return stage

        stage.status = Status.PARTIAL
        top = candidates[0] if candidates else None
        if top is not None:
            stage.add_remediation(
                f"Best unverified lead: {top.label} - {top.source}",
                *[f"  {note}" for note in top.notes[:3]],
            )
        if self.ctx.dry_run:
            stage.add_remediation(
                "Dry run: candidate packages were not downloaded or executed. Re-run without --dry-run to trial them."
            )
        return stage

    def _relevant_only(
        self, candidates: list[Candidate], target: TargetOutcome, stage: StageOutcome
    ) -> list[Candidate]:
        """Drop packages that merely have "mcp" in the name.

        A registry search is a keyword match, and a keyword match with no
        judgement behind it will happily answer "which MCP server talks to
        Confluence?" with a TypeScript code generator -- which is what it did:
        ``@kubb/mcp`` was registered for bugzilla, confluence *and*
        build.opensuse.org, one package standing in for three unrelated
        services, because all three searches were keyword matches and it
        handshakes.

        The test is :func:`relevance_of`, which accepts either a package that
        names the product or a generic server configured for this host -- the
        second clause being what keeps ``mcp-graphql`` pointed at SMELT, a
        working entry that a name-only filter would have thrown away.

        Rejections are recorded rather than silently dropped: a gate nobody can
        inspect is a gate nobody can correct.
        """
        if not self.settings.bool_("mcp.packages.require_relevance", True):
            return candidates
        tokens = self.registry.product_tokens(target.target)
        host = parse_address(target.target).host
        kept, rejected = [], []
        for candidate in candidates:
            why = relevance_of(candidate, tokens, host, hosting_namespaces_of(self.settings))
            if why:
                candidate.add_note(f"relevant: {why}")
                kept.append(candidate)
            else:
                rejected.append(candidate.label)
        if rejected:
            stage.findings["rejected_as_unrelated"] = rejected[:20]
            log.info(
                "%s: %d package hit(s) mention no product token %s and are aimed at nothing "
                "on this host, so they are not candidates: %s",
                target.target,
                len(rejected),
                tokens,
                ", ".join(rejected[:5]),
            )
            stage.add_remediation(
                f"{len(rejected)} search hit(s) were discarded as unrelated to {host} "
                f"(nothing matching {', '.join(tokens)}). Set "
                "mcp.packages.require_relevance = false to consider them anyway."
            )
        return kept

    # -- stage 3: bridge -------------------------------------------------------

    def stage_bridge(self, target: TargetOutcome) -> StageOutcome:
        """Put a bridge in front of whatever the target *does* expose."""
        stage = StageOutcome(stage=Stage.BRIDGE, mode=self.mode)
        if not self.settings.bool_("mcp.bridge.enabled", True):
            stage.detail = "disabled by configuration"
            return stage

        findings = target.all_findings()
        slug = slugify(target.target)
        candidates = self.bridges.adapter_candidates(slug, findings)

        spec = findings.get("openapi_spec")
        if isinstance(spec, Mapping):
            candidate, artifacts, detail = self.bridges.generate_from_openapi(
                target.target,
                spec,
                base_url=target.base_url,
                plan=self.auth_plan_for(target),
            )
            stage.findings["bridge_detail"] = detail
            if candidate is not None:
                candidates.append(candidate)
            if artifacts is not None:
                stage.artifacts.extend([str(artifacts.script), str(artifacts.manifest)])
        else:
            stage.findings["bridge_detail"] = "the target publishes no machine-readable API description"

        # Another kind of API is something to bridge to as well. Its generic
        # servers are tier 2's to try; they are offered here only when tier 2
        # is switched off, so no launch is trialled twice.
        other = {k: v for k, v in self.api_kinds_for(target).items() if v["url"] != findings.get("openapi_url")}
        if other and not isinstance(spec, Mapping):
            stage.findings["bridge_detail"] = "the target publishes " + ", ".join(
                f"a {v['label']} API at {v['url']}" for v in other.values()
            )
            # A kind with documented operations and no server of its own is
            # bridged from them (mcp.discovery.api_kinds.<name>.operations) --
            # sd.example.com's customer API, the one API its account could use.
            # One bridge per target: the generated files are named after it.
            for kind, url in api_kinds.bridgeable(self.settings, other)[:1]:
                if kind.adapters and not self._generic_servers_failed(target, kind):
                    # A kind with generic servers is theirs first (tier 2): this
                    # program's own bridge for it backs them up only once they
                    # were tried and none passed -- gitlab.example.net's mcp-graphql
                    # could not reach it from Node, and nothing else was tried.
                    continue
                described = api_kinds.spec_for(kind, url)
                if described is None:
                    continue
                # Tried, whatever comes of it: see owes_generated_bridge.
                stage.findings.setdefault("generated_api_kinds", []).append(kind.name)
                candidate, artifacts, detail = self.bridges.generate_from_openapi(
                    target.target,
                    described,
                    base_url=target.base_url,
                    plan=self.auth_plan_for(target),
                    spec_is_evidence=False,
                )
                stage.findings["bridge_detail"] = f"{kind.label} API at {url}: {detail}"
                if candidate is not None:
                    candidate.api_kind = kind.name
                    candidate.add_note(
                        f"bridged from the documented operations of the {kind.label} API "
                        f"(mcp.discovery.api_kinds.{kind.name}.operations)"
                    )
                    candidates.append(candidate)
                if artifacts is not None:
                    stage.artifacts.extend([str(artifacts.script), str(artifacts.manifest)])
        # A service whose login only its own client performs (api.example.net's
        # Signature, Kerberos, NTLM) gets a bridge that drives that client --
        # the one route no header variable can replace (client_bridge).
        candidates += self._client_bridge(target, stage)
        if other and not self.settings.bool_("mcp.packages.enabled", True):
            candidates += api_kinds.adapter_candidates(
                self.ctx, target.target, other, base_url=target.base_url, origin=Stage.BRIDGE
            )

        if not candidates:
            stage.detail = str(stage.findings["bridge_detail"])
            stage.add_remediation(
                "No bridge could be built automatically: nothing to bridge *to* was found.",
                "If the target has an API, point initialize-claude-ai at its OpenAPI URL via "
                "mcp.discovery.api_spec_paths and re-run.",
            )
            stage.add_remediation(*self._api_kind_hints(other))
            return stage

        stage.candidates = candidates
        stage.verified = self.verifier.verify_all(
            candidates,
            stop_at_first=not self.settings.bool_("mcp.exhaustive", False),
            keep=lambda c: self.registrable(c, target),
            memo=target.trials,
            why=lambda c: self.why_not_registrable(c, target),
        )
        if stage.verified:
            stage.status = Status.OK
            stage.detail = f"{stage.verified[0].label} verified"
        else:
            stage.status = Status.PARTIAL
            stage.detail = f"{len(candidates)} bridge candidate(s) built but none verified"
            for candidate in candidates[:3]:
                if candidate.handshake:
                    stage.add_remediation(f"{candidate.label}: {candidate.handshake.error}")
        return stage

    def _api_kind_hints(self, found: Mapping[str, Mapping[str, str]]) -> list[str]:
        """What to do about the API kinds a bridge stage met, or where they are looked for."""
        if found:
            return [
                f"Its {where['label']} API at {where['url']} is the thing to bridge: a generic server for "
                f"it (mcp.discovery.api_kinds.{name}.adapters), or a bridge written against it."
                for name, where in found.items()
            ]
        return [
            f"A {kind.label} API is looked for at mcp.discovery.api_kinds.{kind.name}.paths; "
            "add its path there if it is elsewhere."
            for kind in api_kinds.kinds(self.settings)
            if kind.probed
        ]

    # -- stage 4: strategy -----------------------------------------------------

    def stage_strategy(self, target: TargetOutcome) -> StageOutcome:
        """Reason about what is left, and write it down.

        In traditional mode this is rule-based: it reads what the earlier stages
        found and produces a ranked, concrete playbook rather than a shrug.
        """
        stage = StageOutcome(stage=Stage.STRATEGY, mode=self.mode)
        if not self.settings.bool_("mcp.strategy.enabled", True):
            stage.detail = "disabled by configuration"
            return stage

        findings = target.all_findings()
        proposals = self._proposals(target, findings)
        limit = self.settings.int_("mcp.strategy.max_proposals", 5)
        target.playbook = proposals[:limit]

        stage.findings["proposals"] = target.playbook
        if target.verified:
            stage.status = Status.OK
            stage.detail = "a working server was already found; the playbook lists alternatives"
        elif target.playbook:
            stage.status = Status.PARTIAL
            stage.detail = f"{len(target.playbook)} way(s) forward identified"
            stage.add_remediation(*target.playbook)
        else:
            stage.status = Status.SKIPPED
            stage.detail = "nothing further to propose"
        return stage

    def _proposals(self, target: TargetOutcome, findings: Mapping[str, Any]) -> list[str]:
        """Concrete next actions, ordered by how likely they are to work."""
        out: list[str] = []
        host = target.base_url or target.target

        auth_walls = findings.get("auth_required_urls") or []
        if auth_walls:
            out.append(
                f"{auth_walls[0]} is an MCP endpoint behind authentication. Obtain a token and "
                "expose it by reference: INITIALIZE_CLAUDE_AI_MCP_TOKEN_REF=env:YOUR_VAR, then re-run phase 4."
            )
        websockets = findings.get("websocket_urls") or []
        if websockets:
            out.append(
                f"{websockets[0]} upgrades to WebSocket, so the target does speak MCP -- just not over "
                "a transport `claude mcp add-json` can dial. Write a small stdio bridge that relays "
                "JSON-RPC frames to that socket and register the bridge; do not replace the endpoint "
                "with a reimplementation of it."
            )
        if findings.get("well_known"):
            oauth = [u for u in findings["well_known"] if "oauth" in u]
            adapter = self.settings.str_list(
                "mcp.bridge.adapters.remote_to_stdio",
                [
                    "npx",
                    "-y",
                    "mcp-remote",
                    "{url}",
                ],
            )
            if oauth and adapter:
                command = shlex.join(str(p).replace("{url}", f"{host}/mcp") for p in adapter)
                out.append(
                    f"{host} publishes OAuth metadata at {oauth[0]}; `{command}` "
                    "performs that flow interactively and then serves the session over stdio."
                )
        if findings.get("openapi_spec") and not target.verified:
            out.append(
                f"The generated bridge at {findings.get('openapi_url', 'the API description')} exposes only "
                "read-only operations by default. Set mcp.bridge.generated.include_mutating_methods = true "
                "if write access is wanted, then re-run."
            )
        # Another kind of API (GraphQL...) is an API description too: with one,
        # "no API description was found" would be untrue.
        other = {k: v for k, v in self.api_kinds_for(target).items() if v["url"] != findings.get("openapi_url")}
        if not findings.get("openapi_spec") and findings.get("openapi_unparsed_url"):
            out.append(
                f"{host} publishes an API description at {findings['openapi_unparsed_url']} that could "
                "not be read here"
                + (
                    "; it is larger than http.max_response_bytes -- raise that and re-run."
                    if findings.get("openapi_truncated")
                    else "; it is not valid JSON or YAML."
                    if apispec.yaml_available()
                    else " (it is YAML and PyYAML is not installed). Install PyYAML and re-run, and a "
                    "bridge can be generated from it automatically."
                )
            )
        elif not findings.get("openapi_spec") and not other:
            out.append(
                f"No API description was found on {host}. If one exists at a non-standard path, add it to "
                "mcp.discovery.api_spec_paths (or the page that renders it to mcp.discovery.api_doc_pages); "
                "a bridge can then be generated automatically."
            )
        if other and not target.verified:
            for name, where in other.items():
                out.append(
                    f"{host} serves a {where['label']} API at {where['url']}. A generic MCP server for "
                    f"{where['label']} pointed at it is the usual bridge (mcp.discovery.api_kinds.{name}.adapters "
                    "lists the ones tried); check its README for the variables it needs, then re-run."
                )
        if findings.get("search_hits"):
            names = [h["name"] for h in findings["search_hits"][:3]]
            out.append(
                "Published servers that looked related but did not verify: "
                + ", ".join(names)
                + ". Check their READMEs for required configuration, then re-run with those variables set."
            )
        certificate = trust_store.TrustStore.for_run(self.ctx).finding(target.base_url or target.target)
        if findings.get("origin_tls_error") or (certificate and certificate.status == trust_store.UNTRUSTED):
            issuer = trust_store.common_name(certificate.issuer) if certificate else ""
            out.append(
                f"{host} answered with a certificate "
                + (f"from {issuer} " if issuer else "")
                + "that this machine does not trust, so nothing launched here can verify it"
                + (f" ({certificate.detail})" if certificate and certificate.detail else "")
                + ". Pass that authority as a PEM file with --mcp-trust-ca FILE "
                "(INITIALIZE_CLAUDE_AI_MCP_TRUST_EXTRA_CA), or install it in this host's trust store, "
                "and re-run."
            )
        elif int(findings.get("origin_status") or 0) == 0:
            out.append(
                f"{host} did not respond at all. Confirm the address, and whether it is reachable from "
                "this host (proxy, VPN, firewall) before drawing any conclusion about MCP support."
            )

        out.append(
            "Last resort that always works: run a local MCP server that talks to the target's own client "
            "or CLI, and register it with `claude mcp add`. initialize-claude-ai can generate the skeleton -- "
            "see the bridge under mcp.bridge.output_dir."
        )
        return out


class _Tolerant:
    """A second, unverified GET for evidence, used only after a certificate failure.

    The same bargain the tier-1 probe makes (``tls_insecure_retry``): a page
    that failed on the certificate has told us nothing about the service, so
    it is read once more with verification off -- and never with credentials,
    because a peer nothing vouches for is not one to hand a token to. Nothing
    read this way is trusted as a server: whatever is built from it is still
    launched and verified with verification on.
    """

    def __init__(self, ctx: RunContext, discovery, timeout: float) -> None:
        self.ctx = ctx
        self.timeout = timeout
        self.enabled = discovery.bool_("tls_insecure_retry", True)
        self.markers = [m.lower() for m in discovery.str_list("tls_error_markers", list(TLS_ERROR_MARKERS)) if m]
        self._client = None

    def __enter__(self) -> "_Tolerant":
        return self

    def __exit__(self, *exc) -> None:
        if self._client is not None:
            self._client.__exit__(*exc)
            self._client = None

    def is_certificate_failure(self, response) -> bool:
        if response.status != 0 or not response.error:
            return False
        text = response.error.lower()
        return any(marker in text for marker in self.markers)

    def get(self, url: str):
        if not self.enabled:
            return None
        if self._client is None:
            self._client = self.ctx.http(timeout=self.timeout, verify_tls=False).__enter__()
        return self._client.get(url, timeout=self.timeout)


#: Fallback for ``mcp.verification.transport_failure_markers``: a call that never
#: reached the service -- a trust store, a name, a connection -- not a server failing its job.
TRANSPORT_FAILURE_MARKERS = (
    "fetch failed",
    "certificate",
    "CERT_",
    "SSL",
    "self signed",
    "self-signed",
    "unable to verify",
    "UNABLE_TO_GET_ISSUER",
    "ECONNREFUSED",
    "ECONNRESET",
    "ENOTFOUND",
    "EAI_AGAIN",
)


def contract_score(candidate: Candidate) -> int:
    """How much of the usage contract a candidate passed (:func:`.contract.score`)."""
    from . import contract

    return contract.score(candidate)


def manifest_missing(entry: Mapping[str, Any], settings: Any) -> tuple[str, list[str]]:
    """``(manifest path, [variables its login reads that the entry does not pass])`` -- ``("", [])`` when none.

    The one reading of a generated bridge's manifest that both the reuse check
    (:func:`manifest_mismatch`) and the writer's check of every entry as
    written (``McpWriter.complete_slots``) use, so they cannot disagree.
    """
    env = {str(k): str(v) for k, v in (entry.get("env") or {}).items()}
    path, wanted = manifest_login_slots(entry)
    return path, sorted(v for v in wanted if v not in env)


def manifest_login_slots(entry: Mapping[str, Any]) -> tuple[str, list[str]]:
    """``(manifest path, [variables its login needs])`` from the generated bridge's manifest; ``("", [])`` when none.

    The client bridge's ``parts`` (less ``optional_parts`` and ``key_part``)
    and the generated bridge's ``auth.variables``: what the login reads,
    credential-looking name or not -- osc signing needs only the user name.
    """
    import json as _json
    import os as _os

    from .portability import expand

    env = {str(k): str(v) for k, v in (entry.get("env") or {}).items()}
    # Written portably -- ${MCP_CLIENT_MANIFEST:-/path} -- and read as Claude Code reads it.
    path = next((expand(v) for k, v in env.items() if k.endswith("_MANIFEST") and v), "")
    if not path or not _os.path.isfile(path):
        return "", []
    try:
        manifest = _json.loads(open(path, encoding="utf-8").read())
    except (OSError, ValueError):
        return "", []
    if not isinstance(manifest, Mapping):
        return "", []
    wanted = set((manifest.get("parts") or {})) | set(((manifest.get("auth") or {}).get("variables") or {}).values())
    optional = set(manifest.get("optional_parts") or []) | {manifest.get("key_part") or ""}
    return path, sorted(str(v) for v in wanted - optional if v)


def manifest_mismatch(entry: Mapping[str, Any], settings: Any) -> str:
    """Why a generated bridge's entry and the manifest it names disagree, in words; "" when they agree.

    A manifest is read at start-up and names the variables its login reads.
    build-example-net's entry passed SSH-key slots while its manifest -- rewritten
    by a later run for another route -- read a password, and the server asked
    for a variable the entry never passed: judged "waiting for a credential"
    forever. Only the names are compared. (``mcp.discovery.reuse.check_manifests``.)
    """
    import os as _os

    if not settings.bool_("mcp.discovery.reuse.check_manifests", True):
        return ""
    path, missing = manifest_missing(entry, settings)
    if not missing:
        return ""
    return f"its manifest ({_os.path.basename(path)}) reads {', '.join(missing)}, which the entry does not pass"


def _naming(candidates: Sequence[Candidate]) -> str:
    """Name the stage that already answered, for a skip the operator has to read.

    "an earlier tier already answered" leaves them counting stages; "the package
    stage already answered" tells them where to look in the report.
    """
    stages: list[str] = []
    for candidate in candidates:
        stage = (candidate.origin or "").strip()
        if stage and stage not in stages:
            stages.append(stage)
    if not stages:
        return "an earlier stage"
    return " and ".join(f"the {stage} stage" for stage in stages)


__all__ = ["ProbeRun", "TraditionalPipeline"]
