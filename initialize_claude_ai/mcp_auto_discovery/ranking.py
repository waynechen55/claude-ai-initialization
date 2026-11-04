"""Choosing one server when several answer -- what ``--the-best-mcp`` decides with.

Discovery normally registers everything that completes a handshake, on the view
that a working server is a working server. ``--the-best-mcp`` takes the opposite
view: it widens the search so that as many servers as possible are found, then
keeps exactly one per address. That only makes sense if "best" means something
an operator can check, so nothing here is a vibe -- every aspect is scored from
something the run actually observed:

``functionality``  how much the server exposes: tools, resources, prompts and
                   the capabilities it advertised in its initialize reply.
``usability``      what using it costs day to day: a URL is simply called, a
                   stdio server is a process to install, launch and keep alive.
``configuration``  what wiring it up costs once: install commands, variables to
                   export, custom headers.
``stability``      measured, not assumed: the handshake is repeated
                   (``stability_probes``) and both the success rate and the
                   latency count.
``security``       TLS -- and whether its certificate actually verified, since
                   discovery is required to retry without checking it -- whether
                   the candidate runs third-party code locally, and whether it is
                   even on the host that was asked about.
``protocol``       transport currency (streamable-http over the deprecated SSE)
                   and how recent the negotiated protocol version is.
``provenance``     which stage produced it: the vendor's own endpoint outranks a
                   package someone published, which outranks a bridge this tool
                   generated a minute ago.

Every weight, penalty and target lives in ``[mcp.ranking]`` (see
``data/40-mcp.toml``), so an operator who disagrees with the trade-off can
retune it without touching code -- and the score of every candidate, winner or
not, is written into the report so the choice can be argued with afterwards.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from ..core.context import RunContext
from ..core.logging_setup import get_logger
from .address import parse as parse_address
from .portability import PORTABLE, assess
from .probes import McpProber, fresh_answers
from .secret_policy import carries_credentials
from .types import Candidate, Stage, Transport
from .verifier import CandidateVerifier

log = get_logger("mcp.rank")

#: Scored aspects, in the order they are shown. Each one is 0.0-1.0 before its
#: weight is applied, so a column can be read on its own.
ASPECTS = (
    "functionality",
    "usability",
    "configuration",
    "stability",
    "security",
    "protocol",
    "provenance",
)

#: Used only when ``[mcp.ranking.weights]`` says nothing about an aspect. The
#: real defaults are in the packaged TOML, where they can be edited.
FALLBACK_WEIGHT = 1.0


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


@dataclass
class Score:
    """One candidate's report card."""

    candidate: Candidate
    total: float = 0.0
    aspects: dict[str, float] = field(default_factory=dict)
    reasons: dict[str, str] = field(default_factory=dict)
    #: The weights this score was computed with, so the summaries below can rank
    #: reasons by how much they actually moved the total rather than by a raw
    #: 0-1 number that a weight of 0.1 makes irrelevant.
    weights: dict[str, float] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return self.candidate.label

    def _weight(self, aspect: str) -> float:
        return self.weights.get(aspect, FALLBACK_WEIGHT)

    def headline(self, limit: int = 3) -> str:
        """The few reasons that most explain this position, strongest first."""
        ordered = sorted(
            (a for a in ASPECTS if self.reasons.get(a)),
            key=lambda a: self.aspects.get(a, 0.0) * self._weight(a),
            reverse=True,
        )
        return ", ".join(self.reasons[a] for a in ordered[:limit])

    def shortfall(self, limit: int = 2) -> str:
        """Where this candidate lost the most ground, worst first.

        A runner-up is explained by where it fell behind, not by what it has in
        common with the winner: "a URL, nothing to run locally" is true of both
        and says nothing about why only one of them is being registered.
        """
        losses = {a: (1.0 - self.aspects.get(a, 0.0)) * self._weight(a) for a in ASPECTS}
        ordered = sorted(
            (a for a in ASPECTS if self.reasons.get(a) and losses[a] > 0),
            key=lambda a: losses[a],
            reverse=True,
        )
        return "; ".join(f"{a} {self.aspects[a]:.2f}: {self.reasons[a]}" for a in ordered[:limit])

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "url": self.candidate.url,
            "origin": self.candidate.origin,
            "transport": self.candidate.transport,
            "total": round(self.total, 3),
            "aspects": {name: round(value, 3) for name, value in self.aspects.items()},
            "reasons": dict(self.reasons),
        }


class CandidateRanker:
    """Scores verified candidates so exactly one of them can be chosen."""

    def __init__(self, ctx: RunContext, prober: McpProber | None = None) -> None:
        self.ctx = ctx
        self.config = ctx.settings.section("mcp.ranking")
        self.weights = self.config.dict_("weights")
        self.penalties = self.config.dict_("penalties")
        self.origin_scores = self.config.dict_("origin_scores")
        self.protocol_versions = ctx.settings.str_list(
            "mcp.protocol.versions",
            [
                "2026-07-28",
                "2025-11-25",
                "2025-06-18",
                "2025-03-26",
                "2024-11-05",
            ],
        )
        self.prober = prober or McpProber(ctx)
        self._headers: dict[str, str] | None = None

    # -- entry points ----------------------------------------------------------

    def rank(self, candidates: Sequence[Candidate], *, target: str = "") -> list[Score]:
        """Every candidate scored, best first.

        Ties are broken by provenance and then by the label, so two runs over
        the same servers choose the same one: an arbitrary winner would make
        "the best" unreproducible, which is worse than a debatable winner.
        """
        scores = [self._score(candidate, target=target) for candidate in candidates]
        scores.sort(
            key=lambda s: (
                -s.total,
                -s.aspects.get("provenance", 0.0),
                s.label,
            )
        )
        return scores

    def best(self, candidates: Sequence[Candidate], *, target: str = "") -> Score | None:
        scores = self.rank(candidates, target=target)
        return scores[0] if scores else None

    # -- scoring ---------------------------------------------------------------

    def _score(self, candidate: Candidate, *, target: str) -> Score:
        score = Score(candidate=candidate, weights={a: self._weight(a) for a in ASPECTS})
        measured = {
            "functionality": self._functionality(candidate),
            "usability": self._usability(candidate),
            "configuration": self._configuration(candidate),
            "stability": self._stability(candidate),
            "security": self._security(candidate, target=target),
            "protocol": self._protocol(candidate),
            "provenance": self._provenance(candidate),
        }
        weighted = 0.0
        total_weight = 0.0
        for name in ASPECTS:
            value, reason = measured[name]
            score.aspects[name] = _clamp(value)
            if reason:
                score.reasons[name] = reason
            weight = self._weight(name)
            weighted += score.aspects[name] * weight
            total_weight += weight
        score.total = weighted / total_weight if total_weight else 0.0
        return score

    def _weight(self, aspect: str) -> float:
        try:
            weight = float(self.weights.get(aspect, FALLBACK_WEIGHT))
        except (TypeError, ValueError):
            log.warning("mcp.ranking.weights.%s is not a number; using %s", aspect, FALLBACK_WEIGHT)
            return FALLBACK_WEIGHT
        # A negative weight would invert the aspect silently, which is never
        # what someone editing a config file meant by "I care less about this".
        return max(0.0, weight)

    def _penalty(self, name: str, default: float) -> float:
        try:
            return max(0.0, float(self.penalties.get(name, default)))
        except (TypeError, ValueError):
            return default

    # -- the aspects -----------------------------------------------------------

    def _functionality(self, candidate: Candidate) -> tuple[float, str]:
        """Abundance: how much the server actually offers."""
        handshake = candidate.handshake
        if handshake is None:
            return 0.0, ""
        tools, resources, prompts = (
            len(handshake.tools),
            len(handshake.resources),
            len(handshake.prompts),
        )
        offered = tools + resources + prompts
        target = max(1.0, self.config.float_("functionality_target", 12))
        breadth = min(1.0, offered / target)
        capability_target = max(1, self.config.int_("capability_target", 4))
        capabilities = min(1.0, len(handshake.capabilities) / capability_target)
        share = _clamp(self.config.float_("functionality_capability_share", 0.25))
        score = breadth * (1.0 - share) + capabilities * share

        counted = []
        for count, noun in ((tools, "tool"), (resources, "resource"), (prompts, "prompt")):
            if count:
                counted.append(f"{count} {noun}(s)")
        return score, ", ".join(counted) or "nothing advertised"

    def _usability(self, candidate: Candidate) -> tuple[float, str]:
        """What it costs to use, every day, once it is configured."""
        score = 1.0
        notes: list[str] = []
        if candidate.kind == "stdio":
            score -= self._penalty("local_process", 0.4)
            notes.append("a local process to keep alive")
        else:
            notes.append("a URL, nothing to run locally")
        if not candidate.handshake or not candidate.handshake.server_name:
            score -= self._penalty("unnamed", 0.1)
        if candidate.handshake and candidate.handshake.instructions:
            notes.append("ships usage instructions")
        else:
            score -= self._penalty("no_instructions", 0.1)
        return score, "; ".join(notes)

    def _configuration(self, candidate: Candidate) -> tuple[float, str]:
        """What it costs to wire up, once."""
        score = 1.0
        chores: list[str] = []
        if candidate.install:
            score -= self._penalty("install_step", 0.2) * len(candidate.install)
            # An entry that names a path on this machine is worth less than one
            # a launcher fetches at start-up, because .mcp.json is meant to be
            # copied and the path will not travel with it.
            if assess(candidate, must_exist=False).kind != PORTABLE:
                score -= self._penalty("host_bound", 0.3)
            chores.append(f"{len(candidate.install)} install step(s)")
        # A login slot proved optional asks nothing of the operator -- it works
        # without one: never a chore, never a reason to rank below a server
        # that cannot send a login at all (the slot verifier.slot_server_credential adds).
        optional = (
            set(getattr(candidate.handshake, "credential_optional", None) or []) if candidate.handshake else set()
        )
        required = [k for k in candidate.secret_env if k not in optional]
        if required:
            score -= self._penalty("secret_to_export", 0.25) * len(required)
            chores.append(f"{len(required)} variable(s) to export")
            # A credential the operator has to assemble by hand -- a header list,
            # a base64'd user:password -- is worth less than one asked for by its
            # parts: build.example.net's API_HEADERS held a bare token, which no
            # header list can use. A server that takes USERNAME and PASSWORD
            # and builds the header itself leaves nothing to get wrong.
            encoded = [k for k in candidate.secret_env if carries_credentials(k)]
            if encoded:
                score -= self._penalty("encoded_credential", 0.3) * len(encoded)
                chores.append(f"{len(encoded)} credential(s) to encode by hand")
        if candidate.headers:
            score -= self._penalty("custom_header", 0.1) * len(candidate.headers)
            chores.append(f"{len(candidate.headers)} custom header(s)")
        return score, ", ".join(chores) or "nothing to install or export"

    def _stability(self, candidate: Candidate) -> tuple[float, str]:
        """Measured, not assumed: handshake it again and time it."""
        handshake = candidate.handshake
        if handshake is None:
            return 0.0, ""
        samples = [handshake.elapsed]
        succeeded = 1 if handshake.ok else 0
        attempts = 1
        for extra in self._repeat(candidate):
            attempts += 1
            samples.append(extra.elapsed)
            succeeded += 1 if extra.ok else 0

        rate = succeeded / attempts
        slowest = max(samples) if samples else 0.0
        target = max(0.0, self.config.float_("latency_target_seconds", 1.0))
        ceiling = max(target + 0.001, self.config.float_("latency_ceiling_seconds", 8.0))
        latency = _clamp((ceiling - slowest) / (ceiling - target))
        share = _clamp(self.config.float_("stability_latency_share", 0.3))
        score = rate * (1.0 - share) + latency * share

        reason = f"{succeeded}/{attempts} handshake(s), slowest {slowest:.2f}s"
        if attempts == 1:
            reason += " (measured once)"
        return score, reason

    def _repeat(self, candidate: Candidate):
        """Extra handshakes for a URL candidate, if the settings allow any.

        A stdio candidate is never re-launched here: starting third-party code
        is a mutating act, and doing it repeatedly to collect a statistic is not
        a trade this tool makes on someone's machine.
        """
        extra = max(0, self.config.int_("stability_probes", 2))
        if not extra or candidate.kind != "url" or not candidate.url:
            return
        if self.ctx.dry_run:
            return
        timeout = self.config.float_("probe_timeout_seconds", 8.0)
        for _ in range(extra):
            # Asked anew each time: a repeat answered from the run's memo would
            # measure nothing (McpProber.patient).
            with fresh_answers():
                if candidate.transport == Transport.SSE:
                    shake = self.prober.probe_sse(candidate.url, headers=self._auth(), timeout=timeout)
                else:
                    shake = self.prober.probe_http(candidate.url, headers=self._auth(), timeout=timeout)
            yield shake

    def _security(self, candidate: Candidate, *, target: str) -> tuple[float, str]:
        """TLS, whose code runs, and whose host it is."""
        score = 1.0
        notes: list[str] = []
        if candidate.kind == "stdio":
            score -= self._penalty("executes_code", 0.35)
            notes.append("runs third-party code locally")
        else:
            address = parse_address(candidate.url)
            if address.scheme == "https" and candidate.handshake and candidate.handshake.insecure_tls:
                # The checklist requires trying with verification off, so that a
                # certificate problem is not mistaken for an absent endpoint.
                # Finding one that way is a result, not a recommendation: the
                # connection is encrypted but the peer was never authenticated.
                score -= self._penalty("insecure_tls", 0.5)
                notes.append("TLS certificate not verified")
            elif address.scheme == "https":
                notes.append("TLS")
            elif not address.is_local:
                # Cleartext to a public host means every request, and any bearer
                # token attached to it, is on the wire in the open.
                score -= self._penalty("cleartext", 0.6)
                notes.append("cleartext HTTP")
            elif address.is_loopback:
                notes.append("loopback, no TLS needed")
            else:
                # A zone the operator declared theirs. Not loopback, so say so
                # rather than claiming a safety this one does not have.
                notes.append("local network, no TLS")
            if target and address.host:
                wanted = parse_address(target)
                if wanted.host and address.host != wanted.host and not address.is_local:
                    score -= self._penalty("third_party_host", 0.25)
                    notes.append(f"hosted by {address.host}, not {wanted.host}")
        if candidate.secret_env:
            # Not a penalty: a reference is exactly how a credential is supposed
            # to travel. Worth saying out loud, because the operator will have to
            # provide it.
            notes.append("credential by reference")
        return score, "; ".join(notes)

    def _protocol(self, candidate: Candidate) -> tuple[float, str]:
        """How current the transport and the negotiated protocol version are."""
        handshake = candidate.handshake
        transport = (handshake.transport if handshake else "") or candidate.transport
        transport_score = {
            Transport.STREAMABLE_HTTP: 1.0,
            Transport.STDIO: self.config.float_("stdio_transport_score", 0.7),
            Transport.SSE: self.config.float_("sse_transport_score", 0.4),
        }.get(transport, 0.5)

        version = handshake.protocol_version if handshake else ""
        known = self.protocol_versions
        if version and version in known and len(known) > 1:
            # Index 0 is the newest offered, so position is recency.
            version_score = 1.0 - known.index(version) / (len(known) - 1)
        elif version:
            version_score = self.config.float_("unknown_protocol_score", 0.5)
        else:
            version_score = 0.0

        share = _clamp(self.config.float_("protocol_version_share", 0.5))
        score = transport_score * (1.0 - share) + version_score * share
        detail = transport or "unknown transport"
        if version:
            detail = f"{detail}, protocol {version}"
        return score, detail

    def _provenance(self, candidate: Candidate) -> tuple[float, str]:
        """Who is behind it: a stage is a claim about that."""
        defaults = {Stage.ENDPOINT: 1.0, Stage.PACKAGE: 0.75, Stage.BRIDGE: 0.45, Stage.STRATEGY: 0.3}
        origin = candidate.origin or ""
        try:
            score = float(self.origin_scores.get(origin, defaults.get(origin, 0.5)))
        except (TypeError, ValueError):
            score = defaults.get(origin, 0.5)
        return score, f"found by the {origin} stage" if origin else ""

    # -- shared ----------------------------------------------------------------

    def _auth(self) -> Mapping[str, str]:
        """Probe headers, resolved once per ranker rather than per handshake."""
        if self._headers is None:
            self._headers = CandidateVerifier(self.ctx, self.prober).auth_headers()
        return self._headers


__all__ = ["ASPECTS", "CandidateRanker", "Score"]
