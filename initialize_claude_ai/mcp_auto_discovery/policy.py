"""The discovery policy: the target's own endpoint first, and prove it.

Phase 4 can obtain MCP access three ways, and they are not equal:

===== ======================= ==================================================
tier   what it is              why it ranks where it does
===== ======================= ==================================================
1      the target's own        The vendor runs it, keeps it working and knows
       native MCP endpoint     its own data. Nothing is installed, nothing is
                               executed locally, nothing goes stale.
2      a published package     Someone else's code, someone else's release
       or repository           cadence -- but reviewed, versioned and shared.
3      a bridge written here   Code that did not exist a minute ago, that only
                               this machine has, and that nobody maintains.
===== ======================= ==================================================

Descending a tier while a higher one was available is not a matter of taste: it
installs or writes software to do a job the target was already doing, and the
person who inherits the result has to maintain it. So the order is enforced
rather than suggested, and tier 1 has to be *disproved* before tiers 2 and 3 may
run at all.

Disproving it takes more than a ``GET /mcp``. :class:`DueDiligence` is the
four-step checklist every target must go through first -- the same four steps in
both modes, so an agent's "I looked" and the deterministic pipeline's "I looked"
mean the same thing and are reported in the same table:

* **A -- a range of paths.** Every path in ``mcp.policy.mandatory_paths`` at
  least, not just ``/mcp``.
* **B -- both schemes, and a certificate failure explained.** ``http`` and
  ``https``. A corporate or self-signed certificate is a property of the network
  and not an answer about MCP: the deterministic probe retries once without
  verification (``mcp.discovery.tls_insecure_retry``) and marks what answered
  ``insecure_tls``; an agent verifies against the run's trust bundle and names
  the issuer, and never switches verification off itself -- the run already did
  that once, and a brief that asks a model to sweep paths with verification off
  reads like a scan (one was refused by the model's safeguards).
* **C -- content type and handshake headers.** The spec's own ``Accept``, and a
  look at what came back: SSE frames, WebSocket upgrades, JSON-RPC errors.
* **D -- semantic grounding.** What the target *says* about itself: its own
  documentation, the official MCP registry, public repositories, and (agentic
  mode) a web search.

The wording above is the wording in :data:`CHECKLIST`, which is what the report,
the terminal table and the agent's brief all read from -- one sentence per step,
in one place, so the three can never describe different checks.

There are two deliberate exceptions to the descent rule. Neither is a loophole;
both are the rule applied honestly.

**A tier-1 endpoint on a transport Claude Code cannot dial.** A WebSocket route,
most often. A tier-3 bridge in front of *that endpoint* is how the native
endpoint gets used, so it is allowed. What stays forbidden is a bridge or a
package that reimplements what the endpoint already does.

**A credential wall on somebody else's domain, with nothing proven.** Closing
three tiers is a strong act, and "tier 1 exists" has to be a finding to justify
it, not a hypothesis. Against ``confluence.example.com`` tier 1 returned
``https://mcp.atlassian.com/v2/mcp`` -- Atlassian Cloud, which serves
``*.atlassian.net`` tenants and not a customer's self-hosted Data Center wiki,
as the same stage's own step D had already concluded from the SSO redirect. It
never handshaked. It still shut tiers 2, 3 and 4, so ``mcp-atlassian`` -- which
does talk to Data Center, given ``CONFLUENCE_URL`` and a PAT -- was never looked
for, and the one target with a real tier-2 answer got the only wrong tier-1.

So an auth wall on a host belonging to neither the target nor its organisation
leaves the lower tiers open (:meth:`TierPolicy._unproven_foreign_wall`). A
*proven* endpoint still closes them wherever it lives: ``api.githubcopilot.com``
serving ``github.com`` is the ordinary shape of a hosted offering, and it
answered.

A step that genuinely cannot run here -- no network, no search tool -- is
recorded as excused *with the reason*, which keeps the gate honest without
making the program unusable offline. A step nobody attempted is a gap, and a gap
keeps tiers 2 and 3 shut.

Every knob is in ``[mcp.policy]``; nothing here is hardcoded, including which
stage belongs to which tier.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from ..core import redact
from ..core.config import Settings
from ..core.context import RunContext
from ..core.report import Status
from .grounding import host_of, same_organisation, same_service
from .types import Stage

#: The checklist, in order: key, the letter it is known by, and the one-line
#: statement of what the step has to establish. Kept here rather than in
#: configuration because these four are the policy -- an installation may tune
#: how hard each one looks, not delete one and still claim the search was done.
CHECKLIST: tuple[tuple[str, str, str], ...] = (
    ("paths", "A", "a range of paths tried, not just /mcp"),
    # The key stays `bypasses`: it is the name agents emit in their JSON and the
    # name every stored report already uses, so renaming it would silently
    # discard a step rather than relabel it. Only the sentence read by a person
    # -- and by the model being briefed -- changes.
    ("bypasses", "B", "both schemes tried, and a certificate failure explained (its issuer named)"),
    ("handshake", "C", "spec Accept headers sent, and the responses read for SSE/WS/JSON-RPC"),
    ("grounding", "D", "the target's own documentation and the public registries consulted"),
)

#: Tier of each stage when ``[mcp.policy.tiers]`` says nothing about it.
DEFAULT_TIERS: dict[str, int] = {
    Stage.ENDPOINT: 1,
    Stage.PACKAGE: 2,
    Stage.BRIDGE: 3,
    Stage.STRATEGY: 3,
}


@dataclass
class Step:
    """One checklist step, and what was actually done for it."""

    key: str
    letter: str
    title: str
    status: str = Status.SKIPPED
    detail: str = ""
    #: What was tried, in words: the paths, the schemes, the URLs read. This is
    #: the "document the outcome" half of due diligence -- a step that says it
    #: ran and cannot say what it did has not really reported anything.
    evidence: list[str] = field(default_factory=list)
    #: Non-empty when the step could not be carried out *here*, for a reason
    #: about this machine rather than about the target.
    excused: str = ""

    @property
    def attempted(self) -> bool:
        return self.status in (Status.OK, Status.PARTIAL, Status.FAILED)

    @property
    def satisfied(self) -> bool:
        """Did this step do its job, or say honestly why it could not?"""
        return self.status in (Status.OK, Status.PARTIAL) or bool(self.excused)

    @property
    def mark(self) -> str:
        if self.excused and self.status not in (Status.OK, Status.PARTIAL):
            return "n/a"
        return {Status.OK: "yes", Status.PARTIAL: "part", Status.FAILED: "no"}.get(self.status, "no")

    def to_dict(self) -> dict[str, Any]:
        return {
            "step": self.letter,
            "key": self.key,
            "title": self.title,
            "status": self.status,
            "detail": redact.redact(self.detail),
            "evidence": [redact.redact(e) for e in self.evidence[:40]],
            "excused": redact.redact(self.excused),
        }


#: The endpoint kinds an agent's report may only *claim* (``mcp.policy.agent_claims_close_tiers``):
#: each closes the lower tiers when believed, so only a probe here may record one.
CLAIM_KINDS = ("native_endpoints", "auth_walls")
CLAIMS_CLOSE_TIERS = [False]


def configure_claims(settings: Any) -> None:
    """Read ``mcp.policy.agent_claims_close_tiers`` once per run."""
    CLAIMS_CLOSE_TIERS[0] = bool(settings.bool_("mcp.policy.agent_claims_close_tiers", False))


@dataclass
class DueDiligence:
    """The tier-1 checklist for one target: what was done, and what it proved."""

    target: str
    steps: list[Step] = field(default_factory=list)
    #: URLs of native endpoints that completed a handshake. Non-empty means the
    #: search is over: no lower tier may run.
    native_endpoints: list[str] = field(default_factory=list)
    #: Endpoints that answered but demanded a credential. Also a native endpoint
    #: -- an authenticated one -- and treated as evidence that tier 1 exists.
    auth_walls: list[str] = field(default_factory=list)
    #: Native endpoints Claude Code cannot dial as they stand -- a WebSocket
    #: route, most often. Tier 1 exists, but reaching it needs a shim, so these
    #: unlock the bridge stage instead of closing it: the bridge is then built
    #: *for the target's own endpoint*, which is the policy working, not a
    #: violation of it.
    needs_bridge: list[str] = field(default_factory=list)
    #: URL -> why it may not be counted as this target's endpoint. An address
    #: that answered MCP but could never be registered for the target -- found
    #: by a keyword search, tied to nothing -- or that a claim put here and a
    #: probe then failed to confirm. Nothing listed here closes a tier, and no
    #: merge can put it back: evidence that the registration check refused is
    #: not evidence that tier 1 exists. See :meth:`retract`.
    ruled_out: dict[str, str] = field(default_factory=dict)
    #: Endpoints an agent *said* exist (``native_endpoints`` / ``auth_required_endpoints``
    #: in its report) that no probe here has shown to speak MCP: kind -> URLs.
    #: Reported, never counted -- a claim closes no tier. confluence.example.com's
    #: /mcp, which redirects to the login page like every path that does not
    #: exist, was reported "behind authentication" and closed the package,
    #: bridge and strategy stages. (``mcp.policy.agent_claims_close_tiers``.)
    claimed: dict[str, list[str]] = field(default_factory=dict)
    #: Filled in when the record came from an agent rather than from probing.
    source: str = "probing"

    def __post_init__(self) -> None:
        if not self.steps:
            self.steps = [Step(key=key, letter=letter, title=title) for key, letter, title in CHECKLIST]

    # -- recording -------------------------------------------------------------

    def step(self, key: str) -> Step:
        for step in self.steps:
            if step.key == key:
                return step
        step = Step(key=key, letter="?", title=key)
        self.steps.append(step)
        return step

    def record(
        self,
        key: str,
        status: str,
        detail: str = "",
        *,
        evidence: Iterable[str] = (),
        excused: str = "",
    ) -> Step:
        step = self.step(key)
        step.status = status
        step.detail = detail
        step.excused = excused
        for item in evidence:
            text = str(item).strip()
            if text and text not in step.evidence:
                step.evidence.append(text)
        return step

    #: The lists an endpoint can be recorded in, by the name :meth:`note` takes.
    KINDS = ("native_endpoints", "auth_walls", "needs_bridge")

    def note(self, kind: str, urls: Iterable[str]) -> list[str]:
        """Record endpoints under ``kind``, skipping any already ruled out.

        The one way into the record, so a URL retracted by one route cannot be
        re-added by another -- an agent's checklist merged after the fallback
        refused the same address, say. Returns what was newly recorded.
        """
        sink: list[str] = getattr(self, kind)
        added: list[str] = []
        for url in urls:
            text = str(url or "").strip()
            if text and text not in sink and text not in self.ruled_out:
                sink.append(text)
                added.append(text)
        return added

    def retract(self, url: str, reason: str) -> bool:
        """Take ``url`` out of the record for good, saying why.

        Only an endpoint that could actually be written down for this target
        may close the tiers beneath it. mcp.0dteconfluence.com -- an options
        trading tool the registry returned for "confluence" -- answered MCP,
        was recorded as confluence.example.com's native endpoint, and closed the
        package, bridge and strategy stages; the registration check refused it
        a moment later, and the address ended with nothing. Returns whether it
        had been recorded.
        """
        text = str(url or "").strip()
        if not text:
            return False
        present = False
        for kind in self.KINDS:
            sink: list[str] = getattr(self, kind)
            if text in sink:
                sink.remove(text)
                present = True
        self.ruled_out.setdefault(text, str(reason or "").strip() or "not registrable for this target")
        return present

    def merge(self, other: "DueDiligence | None") -> "DueDiligence":
        """Combine two records for one target, keeping the stronger of each step.

        An agent does step D properly and may skip step A; the deterministic
        fallback does the opposite. Neither ran the whole checklist, but between
        them the checklist is done -- and the gate should see that rather than
        punish the target for the handover.
        """
        if other is None:
            return self
        rank = {Status.OK: 3, Status.PARTIAL: 2, Status.FAILED: 1, Status.SKIPPED: 0}
        for theirs in other.steps:
            mine = self.step(theirs.key)
            if rank.get(theirs.status, 0) > rank.get(mine.status, 0):
                mine.status, mine.detail = theirs.status, theirs.detail
            mine.excused = mine.excused or theirs.excused
            for item in theirs.evidence:
                if item not in mine.evidence:
                    mine.evidence.append(item)
        # Ruled-out first, in both directions: neither side may bring back an
        # address the other has already refused.
        for url, reason in other.ruled_out.items():
            self.retract(url, reason)
        for kind in self.KINDS:
            self.note(kind, getattr(other, kind))
        for kind, urls in other.claimed.items():
            for url in urls:
                if url not in self.claimed.setdefault(kind, []) and url not in self.ruled_out:
                    self.claimed[kind].append(url)
        if other.source not in self.source:
            self.source = f"{self.source}+{other.source}"
        return self

    # -- verdicts --------------------------------------------------------------

    @property
    def native_exists(self) -> bool:
        """Is there a tier-1 endpoint, working, merely locked, or undialable?"""
        return bool(self.native_endpoints or self.auth_walls or self.needs_bridge)

    def gaps(self) -> list[Step]:
        """Steps that were neither carried out nor excused."""
        return [s for s in self.steps if not s.satisfied]

    @property
    def complete(self) -> bool:
        return not self.gaps()

    def summary(self) -> str:
        done = sum(1 for s in self.steps if s.satisfied)
        if self.native_endpoints:
            return f"{done}/{len(self.steps)} checks; native endpoint found at {self.native_endpoints[0]}"
        if self.needs_bridge:
            return (
                f"{done}/{len(self.steps)} checks; a native endpoint at {self.needs_bridge[0]} "
                "speaks a transport Claude Code cannot dial directly"
            )
        if self.auth_walls:
            return f"{done}/{len(self.steps)} checks; a native endpoint exists at {self.auth_walls[0]} but wants a credential"
        if self.ruled_out:
            first = next(iter(self.ruled_out))
            return (
                f"{done}/{len(self.steps)} checks; no native endpoint of this target answered "
                f"({len(self.ruled_out)} ruled out, e.g. {first}: not registrable for it)"
            )
        return f"{done}/{len(self.steps)} checks; no native endpoint answered"

    def rows(self) -> list[list[str]]:
        """Table rows for the terminal: one line per step."""
        return [
            [
                step.letter,
                step.title,
                step.mark,
                (step.excused or step.detail or "-"),
            ]
            for step in self.steps
        ]

    def to_dict(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "source": self.source,
            "complete": self.complete,
            "native_exists": self.native_exists,
            "native_endpoints": self.native_endpoints,
            "auth_required_endpoints": self.auth_walls,
            "endpoints_needing_a_bridge": self.needs_bridge,
            "claimed_by_agent_unproven": {k: list(v) for k, v in self.claimed.items() if v},
            "ruled_out": [{"url": url, "reason": redact.redact(reason)} for url, reason in self.ruled_out.items()],
            "steps": [s.to_dict() for s in self.steps],
            "summary": self.summary(),
        }

    @classmethod
    def from_payload(cls, target: str, payload: Any, *, source: str = "agent") -> "DueDiligence | None":
        """Build a record from what an agent reported, trusting nothing blindly.

        The agent says which steps it performed and shows its evidence; it does
        not get to declare the checklist satisfied by fiat. A step arrives here
        as done only if it also carried something to show for it, which is why
        ``evidence`` is what decides between OK and PARTIAL.
        """
        if not isinstance(payload, dict):
            return None
        record = cls(target=target, source=source)
        seen = False
        for key, _letter, _title in CHECKLIST:
            entry = payload.get(key)
            if entry is None:
                continue
            seen = True
            if isinstance(entry, dict):
                detail = str(entry.get("detail") or entry.get("summary") or "").strip()
                evidence = [str(e) for e in _listish(entry.get("evidence"))]
                excused = str(entry.get("unavailable") or entry.get("excused") or "").strip()
                done = entry.get("done")
            else:
                detail, evidence, excused, done = str(entry).strip(), [], "", bool(entry)
            if excused:
                status = Status.SKIPPED
            elif done is False:
                status = Status.FAILED
            elif evidence:
                status = Status.OK
            else:
                status = Status.PARTIAL
            record.record(key, status, detail, evidence=evidence, excused=excused)
        for name, kind in (
            ("native_endpoints", "native_endpoints"),
            ("auth_required_endpoints", "auth_walls"),
            ("endpoints_needing_a_bridge", "needs_bridge"),
        ):
            urls = [str(u) for u in _listish(payload.get(name)) if str(u or "").strip()]
            if kind in CLAIM_KINDS and not CLAIMS_CLOSE_TIERS[0]:
                # What the agent says it found; the pipeline records what a probe
                # here proved. A claim is reported, and closes nothing.
                for url in urls:
                    record.claimed.setdefault(kind, [])
                    if url not in record.claimed[kind] and url not in record.ruled_out:
                        record.claimed[kind].append(url)
                        seen = True
                continue
            if record.note(kind, urls):
                seen = True
        return record if seen else None


class TierPolicy:
    """Decides whether a stage is allowed to run at all.

    The rule it enforces is the one thing in phase 4 that can *stop* work from
    happening, so it is deliberately small, entirely configuration-driven, and
    it always explains itself: every refusal comes back with the sentence the
    operator will read in the report.
    """

    def __init__(self, ctx: RunContext, config: Settings | None = None) -> None:
        self.ctx = ctx
        self.config = config or ctx.settings.section("mcp.policy")

    @property
    def enabled(self) -> bool:
        return self.config.bool_("native_first", True)

    def tier(self, stage: str) -> int:
        configured = self.config.int_(f"tiers.{stage}", 0)
        return configured or DEFAULT_TIERS.get(stage, 1)

    def mandatory_paths(self) -> list[str]:
        return self.config.str_list("mandatory_paths", [])

    def _unproven_foreign_wall(self, record: DueDiligence) -> str:
        """An auth wall on another organisation's domain, and nothing proven.

        Returns the URL, or ``""`` when tier 1's evidence is stronger than that:
        a completed handshake anywhere, a bridge-needing endpoint, or a wall on
        the target's own organisation. See :meth:`gate` for why this reopens the
        lower tiers instead of closing them.
        """
        if record.native_endpoints or record.needs_bridge or not record.auth_walls:
            return ""
        host = host_of(record.target)
        if not host:
            return ""
        for url in record.auth_walls:
            where = host_of(url)
            if where and not same_service(host, where) and not same_organisation(host, where):
                return str(url)
        return ""

    def gate(self, stage: str, record: DueDiligence | None) -> tuple[bool, str]:
        """``(allowed, why_not)`` for one stage, given the tier-1 record so far."""
        if not self.enabled or self.tier(stage) <= 1:
            return True, ""

        if record is None:
            return False, (
                f"the endpoint stage has not run, so tier {self.tier(stage)} cannot be entered: "
                "the target's own MCP endpoint has to be ruled out first"
            )

        # A native endpoint on a transport Claude Code cannot dial is the one
        # case where descending to tier 3 is the *right* answer: the bridge is
        # built for that endpoint, not as a substitute for it.
        if record.needs_bridge and not record.native_endpoints and self.tier(stage) >= 3:
            return True, ""

        unproven = self._unproven_foreign_wall(record)
        if unproven and self.config.bool_("lower_tiers_when_wall_is_foreign", True):
            # The endpoint is on somebody else's domain and has never completed
            # a handshake, so "tier 1 exists" is a hypothesis, not a finding --
            # and it is being used to forbid the tiers that could test it.
            #
            # confluence.example.com is the case. Tier 1 returned
            # https://mcp.atlassian.com/v2/mcp: Atlassian Cloud's multi-tenant
            # endpoint, which serves `*.atlassian.net` tenants and not a
            # customer's self-hosted Data Center wiki -- as the same stage's own
            # step D said, having followed the SSO redirect and concluded "self
            # hosted Data Center, not Atlassian Cloud". That one URL closed
            # tiers 2, 3 and 4, so `mcp-atlassian` -- the package that does talk
            # to Data Center, given CONFLUENCE_URL and a PAT -- was never
            # considered. The one target with a real tier-2 answer got the only
            # wrong tier-1.
            #
            # A vendor's own proven endpoint still closes them: github.com is
            # served from api.githubcopilot.com and that is the ordinary shape
            # of a hosted offering. What does not close them is an unproven
            # address on a domain that is not the target's.
            return True, ""

        if record.native_exists and self.config.bool_("forbid_lower_tiers_when_native", True):
            where = (record.native_endpoints or record.auth_walls or record.needs_bridge)[0]
            if record.native_endpoints:
                which = "a native MCP endpoint"
            elif record.auth_walls:
                which = "a native MCP endpoint (behind authentication)"
            else:
                # Only a bridge-needing endpoint: tier 3 and 4 were let through
                # above, so what is refused here is the package tier.
                which = "a native MCP endpoint on a transport Claude Code cannot dial (it needs a bridge)"
            return False, (
                f"{which} exists at {where}; installing a package or writing a bridge for a "
                "server that already speaks MCP is forbidden by the discovery policy "
                "(mcp.policy.forbid_lower_tiers_when_native)"
            )

        if self.config.bool_("require_checklist", True):
            gaps = record.gaps()
            if gaps:
                missing = ", ".join(f"{s.letter} ({s.title})" for s in gaps)
                return False, (
                    f"tier 1 due diligence is incomplete -- {missing} was never carried out -- "
                    "so no lower tier may run (mcp.policy.require_checklist)"
                )

        return True, ""

    def remediation(self, stage: str, record: DueDiligence | None) -> list[str]:
        """What to do about a refusal, in the operator's terms."""
        if record is not None and record.native_exists:
            where = (record.native_endpoints or record.auth_walls or record.needs_bridge)[0]
            lines = [
                f"Use the native endpoint: {where} is the target's own MCP server.",
            ]
            if record.auth_walls and not record.native_endpoints:
                lines.append(
                    "It needs a credential: set INITIALIZE_CLAUDE_AI_MCP_TOKEN_REF=env:YOUR_VAR "
                    "(a reference, never the value) and re-run phase 4."
                )
            lines.append(
                f"To search for a package or build a bridge anyway, set "
                f"mcp.policy.forbid_lower_tiers_when_native = false and re-run the {stage} stage."
            )
            return lines
        return [
            "Complete the tier-1 checklist first, or set mcp.policy.require_checklist = false "
            f"to let the {stage} stage run on an unfinished search.",
        ]


def _listish(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (str, bytes)):
        return [value]
    if isinstance(value, Sequence):
        return list(value)
    return [value]


__all__ = ["CHECKLIST", "DEFAULT_TIERS", "DueDiligence", "Step", "TierPolicy"]
