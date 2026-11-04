"""Phase 4 -- MCP auto-discovery.

Each address given to ``--mcp-server`` is first turned into one exact base URL
(see :mod:`..mcp_auto_discovery.address`), because everything below is built by
appending to it. Then four stages run in order until one of them settles the
address:

1. **endpoint** -- is the target already speaking MCP somewhere?
2. **package** -- has someone published a server for it?
3. **bridge** -- can one be put in front of what the target does expose?
4. **strategy** -- what else could work, and can it be done here?

This is the only phase that is agentic. Where phases 1-3 must be deterministic
(the Claude environment does not exist yet), by the time phase 4 runs it does --
so every stage starts as a Claude subagent handed a skill describing the method.
When no agent can run, or when one comes back empty, that stage -- and only that
stage -- finishes deterministically instead, which is why each stage line says
which mode did the work. ``--no-agent`` runs the deterministic pipeline alone,
for a run that must not start an agent at all; the banner and the opening step
say which of the two the run is in before any of it happens.

Nothing an agent claims is taken on trust. Every candidate, from either mode,
has to be *connected* (a real MCP ``initialize``), *callable* (tools, resources
or prompts) and *two-way* (a request sent after the handshake, answered on the
session it opened) before it is reported; all of it is re-checked once more at
the end; and once registered, the claude CLI is asked for its own verdict. That
is the verification report printed per address before anything is written.

An endpoint that answers ``401`` or ``403`` is the one finding that is neither
of those two things. No handshake completed, so it is never counted as working
-- but the server was reached and it speaks for the target, so it settles its
stage rather than sending the agent to look again, closes the lower tiers, and
is registered with its credential slot left **empty**. The operator then exports
one variable instead of running a second discovery; the verdict for such an
address is PARTIAL and the final report names the variable.

Every server that survives is written to ``.mcp.json`` in the current directory
*and* registered with the CLI -- always, with no flag for half of it. That file
holds ``mcpServers`` and nothing else, so it can be copied whole into another
project and work there; the briefing explaining what is in it is printed in its
own block and kept in the log instead, and prose an older version of this
program wrote into the file is swept out at the start of every run.

By default the search stops at the first address that yields a working server.
``--exhaustive-mcp`` records every working endpoint per address, each under its
own name; ``--the-best-mcp`` widens the search the same way and then keeps one
per address, scored by :mod:`..mcp_auto_discovery.ranking` on what the run
observed, with the runners-up printed and kept in the report.
"""

from __future__ import annotations

import concurrent.futures
import re
from typing import Any, Iterator, Mapping, Sequence

from ..core.logging_setup import HEADING_STYLE, Line, get_logger
from ..core.report import PhaseResult, Safety, Status, format_duration, worst
from ..core.trust_store import ANCHORED, UNTRUSTED, TrustStore, common_name
from ..mcp_auto_discovery import login_check, secret_policy
from ..mcp_auto_discovery.address import (
    AddressResolver,
    ServerAddress,
    configure_local_zones,
    parse_all,
)
from ..mcp_auto_discovery.agentic import AgenticPipeline
from ..mcp_auto_discovery.existing import ExistingRegistrations
from ..mcp_auto_discovery.grounding import configure_suffixes_from
from ..mcp_auto_discovery.policy import DueDiligence
from ..mcp_auto_discovery.provisioning import FAILED, INSTALLED, Provisioner, working_on
from ..mcp_auto_discovery.ranking import ASPECTS, CandidateRanker, Score
from ..mcp_auto_discovery.runtime import REFUSAL
from ..mcp_auto_discovery.traditional import TraditionalPipeline
from ..mcp_auto_discovery.types import (
    Candidate,
    Mode,
    Stage,
    TargetOutcome,
    mode_tag,
    waits_for_credential,
)
from ..mcp_auto_discovery.verifier import CHECKS, CandidateVerifier
from ..mcp_auto_discovery.writer import McpWriter, Registration
from .base import Phase, register

#: The value of ``mcp.discovery.target_concurrency`` that means "bare
#: --concurrency": resolved through ``mcp.discovery.target_concurrency_bare``.
BARE_CONCURRENCY = "bare"

log = get_logger("mcp.phase")


def _never_ran(stage: Any) -> bool:
    """Was this stage skipped before it started?

    A skip carries no duration and no candidates because nothing happened; the
    tier gate or the descent rule wrote the outcome and moved on. Distinguished
    from a stage that ran and found nothing, which is a real result with a mode.
    """
    return bool(getattr(stage, "status", "") == Status.SKIPPED and not stage.duration)


#: Stage order used when nothing is configured.
DEFAULT_STAGES = (Stage.ENDPOINT, Stage.PACKAGE, Stage.BRIDGE, Stage.STRATEGY)

#: The frame around the briefing. Bold blue, the heading hue: blue never means
#: an outcome, so the one block the operator has to act on after the run does not
#: compete with the verdicts around it, and the weight is what makes it findable
#: when scrolling back through a long run.
BRIEFING_FRAME_STYLE = HEADING_STYLE

#: Column headings for the --the-best-mcp scorecard. Abbreviated because eight
#: columns of full aspect names do not fit a terminal; the banner and the README
#: carry the long names.
_ASPECT_COLUMNS = {
    "functionality": "funcs",
    "usability": "use",
    "configuration": "config",
    "stability": "stable",
    "security": "secure",
    "protocol": "proto",
    "provenance": "source",
}


@register
class McpPhase(Phase):
    """Phase 4 -- find an MCP server for each address, prove it, register it.

    The module docstring above describes the pipeline; this class is the part
    that reports it. Its organising rule is that the unit of success is the
    *address*, not the probe: a server found on the third stage is one success,
    not two failures and a pass, so every step is filed under the address it
    concerns and the phase result counts addresses.
    """

    slug = "mcp"
    number = 4
    title = "MCP auto-discovery"
    description = (
        "For each address given to --mcp-server, work through the stages until one "
        "settles it: existing endpoint, published package, generated bridge, then "
        "strategy. Nothing is registered as working until it completes a real MCP "
        "handshake; an endpoint that answers 401 is registered with its credential "
        "slot empty and reported as needing a token."
    )
    enable_key = "mcp"
    # Everything below the run-level bookkeeping is about one address, and one
    # address is what this phase is judged on: a server found on the third
    # stage is a success, not two failures and a pass. So every step is filed
    # under the address it concerns and the summary reports per address.
    group_noun = "server"

    def banner_facts(self) -> list[tuple[str, str]]:
        # The canonical form, so the banner shows the URL that will actually be
        # probed rather than the shorthand it was typed as. Parsing is pure text;
        # the confirming request happens in execute(), not here.
        servers = [a.describe() for a in self._addresses()]
        return [
            ("Servers", ", ".join(servers) if servers else "none given (--mcp-server)"),
            ("Stages", " -> ".join(self._stages())),
            ("Selection", self._selection_fact()),
            # Which mode each stage actually ran in is tagged on its own line
            # below; looking for a runtime here would make the banner wait on it.
            # What --no-agent decides is known without asking anything, so the
            # banner can say it -- the first thing on screen, before the wait.
            ("Discovery", self._discovery_fact()),
            (
                "Verification",
                "connected, something callable, and a request answered after the "
                "handshake -- then whatever the claude CLI says about it once registered",
            ),
            ("Authenticated endpoints", self._authenticated_fact()),
            ("Missing toolchains", self._provisioning_fact()),
            (
                "Written to",
                f"{McpWriter(self.ctx).display_path} in this directory, and registered with "
                f"claude mcp add-json --scope "
                f"{self.settings.str_('mcp.register_scope', 'local')}",
            ),
            *super().banner_facts(),
        ]

    def safety(self, result: PhaseResult) -> tuple[str, str]:
        """Phase 4 is additive, so it never blocks -- and says so plainly.

        Nothing here can leave the environment worse than it found it: a server
        is registered only after a real handshake, and a target that yields
        nothing simply is not registered. The worst outcome is fewer MCP servers
        than you hoped for, which is a disappointment and not a hazard.
        """
        if not result.steps or result.fatal:
            # The phase never executed, or stopped for want of a required
            # setting. "Never blocks" is a statement about discovery, which is
            # additive; it is not a licence to call a run safe when the phase
            # was never able to start looking.
            return super().safety(result)
        outcomes = list(result.data.get("servers") or [])
        working = [o for o in outcomes if o.get("succeeded")]
        not_working = [str(o.get("target", "?")) for o in outcomes if not o.get("succeeded")]
        # Addresses that never became a target at all, because they could not be
        # parsed and discovery never ran for them. They are still servers the
        # operator asked for and still did not work, and the scoreboard above
        # counts them -- leaving them out here would have the same box give two
        # different denominators three lines apart.
        named = {str(o.get("target", "")) for o in outcomes}
        unusable = [g.key for g in result.groups() if g.key not in named]
        not_working.extend(unusable)
        total = len(outcomes) + len(unusable)
        # Addresses that produced a real endpoint which would not talk to an
        # anonymous client. Neither a success nor a blank, and the difference
        # decides what the operator does next: find a token, not another host.
        awaiting = [
            o for o in outcomes if o.get("provisional") and not o.get("succeeded") and not o.get("credential_refused")
        ]
        # Given a credential that its service refused: written, and waiting for
        # a *working* one -- a different instruction from "export a token".
        refused_login = [o for o in outcomes if o.get("credential_refused") and not o.get("succeeded")]
        # Waiting for a credential is not "nothing": the entry is written. Only
        # an address with nothing registered is named as missing.
        missing = [str(o.get("target", "?")) for o in outcomes if not o.get("succeeded") and not o.get("registered")]
        missing.extend(unusable)
        needs = sorted(
            {
                name
                for o in outcomes
                for row in (o.get("verification") or [])
                if row.get("provisional")
                for name in (row.get("needs_environment") or [])
            }
        )
        pending = (
            f" {len(awaiting)} address(es) have a server that proved it is there and waits for "
            f"a credential, written out with the credential left empty; exporting "
            f"{', '.join(needs) or 'the named variable(s)'} with a valid token is all that "
            "stands between that and a working server."
            if awaiting
            else ""
        )
        if refused_login:
            pending += (
                f" {len(refused_login)} address(es) have a server whose service refused the credential "
                "supplied (or whose account has no API access); a working credential or account is "
                "what it needs, not another discovery run."
            )

        if not outcomes and not unusable:
            # No server was asked for, so nothing is missing. Flagging this as a
            # caveat would put a warning on every default run, which is the fastest
            # way to teach someone to stop reading the warnings.
            return Safety.SAFE, (
                "No server was given, so there was nothing to discover and nothing was "
                "registered. Claude Code is complete and usable as it stands; pass "
                "--mcp-server to add an MCP server at any later time."
            )
        if not working:
            return Safety.CAUTION, (
                f"No address yielded a server that completed an MCP handshake "
                f"({', '.join(not_working)})"
                + ("." + pending if pending else ", so nothing was registered.")
                + " Claude Code itself is untouched and fully usable -- it simply has no "
                "working MCP server for these addresses. The playbook above is the route to one."
            )
        # Servers that answered, were scored, and lost. Worth a sentence: they
        # are not failures, and the operator may well want one of them.
        set_aside = sum(max(0, len(o.get("ranking") or []) - 1) for o in outcomes)
        aside = (
            f" {set_aside} other server(s) answered and were scored lower; the scorecard above "
            "says why, and a run without --the-best-mcp would register them too."
            if set_aside
            else ""
        )
        if missing:
            return Safety.CAUTION, (
                f"{len(working)} of {total} server(s) are registered and verified. "
                f"Nothing registered for {', '.join(missing)}; Claude Code works without "
                f"them, so this limits only what it can reach, never what it can do.{aside}{pending}"
            )
        if awaiting:
            return Safety.CAUTION, (
                f"{len(working)} of {total} server(s) are registered and verified, and every "
                f"other address has an entry written.{aside}{pending}"
            )
        return Safety.SAFE, (
            f"Every server ({len(working)} of {total}) completed an MCP initialize "
            f"handshake and was registered. Claude Code can reach them in the next session.{aside}{pending}"
        )

    def execute(self, result: PhaseResult) -> None:
        """Run phase 4 unattended: nothing it starts can open a browser (:mod:`..mcp_auto_discovery.noninteractive`)."""
        from ..mcp_auto_discovery import noninteractive

        with noninteractive.session(self.ctx):
            self._execute(result)

    def _execute(self, result: PhaseResult) -> None:
        """Run the four stages against every address, then verify and publish.

        Ordered so that nothing is skipped by an early return: the local zones
        are declared and a legacy ``.mcp.json`` is swept before the addresses
        are even parsed, because a run that discovers nothing still has those
        two obligations.
        """
        # First, before an address is even parsed: the default scheme for a bare
        # name depends on whether that name is on the operator's own network.
        self._declare_local_zones()
        # And which suffixes are public, so "which organisation runs this host"
        # is answered the same way by every stage, in either mode.
        self._declare_public_suffixes()
        # And what counts as a credential, before any entry is read or written.
        secret_policy.configure(self.settings)
        # And the one comparison every chooser shares (types.evidence_key).
        from ..mcp_auto_discovery.types import configure_choice

        configure_choice(self.settings)
        # ...and whether an agent's report may close tiers by itself (policy.CLAIM_KINDS).
        from ..mcp_auto_discovery.policy import configure_claims

        configure_claims(self.settings)
        # Learn the credentials existing entries already use, before any
        # candidate is read: a value equal to one is a credential under any name.
        ExistingRegistrations.for_run(self.ctx).registrations()

        # And before the run can end early: a .mcp.json written by a version
        # that put the briefing inside it is still a file somebody may copy
        # today, and a run that finds nothing would otherwise never clean it.
        writer = McpWriter(self.ctx)
        self._tidy_configuration_file(result, writer)
        # Claude Code expands an entry's ${NAME} from its own settings.json env
        # too, where this program keeps the credentials an entry carried: a
        # trial resolves from there as well, so it tests what the entry gets.
        if self.ctx.settings.bool_("mcp.verification.resolve_from_claude_settings", True) and not self.ctx.dry_run:
            self.ctx.secrets.fallback_env = writer._claude_settings_env

        addresses = self._addresses()
        if not addresses:
            self.report_step(
                result,
                "servers",
                Status.SKIPPED,
                "no server given",
                remediation=[
                    "Point phase 4 at a server: initialize-claude-ai run --phases 4 "
                    "--mcp-server example.com,https://api.example.com",
                    "Or set mcp.servers in the configuration to make it persistent.",
                ],
            )
            return

        usable = self._canonicalise(result, addresses)
        if not usable:
            return

        # Before anything reads the search settings: the pipelines snapshot them
        # when they are built, so widening the search has to happen first.
        if self._pick_best() or self._record_all():
            self._widen_search(result)

        stages = self._stages()
        pipeline, expected_mode, mode_detail = self._pipeline()
        # Kept so the final verdict can ask it what happened across every
        # target -- notably whether the agent runtime turned out to be unusable,
        # which is a fact about the run rather than about any one address.
        self._ran_with = pipeline
        label, styles = mode_tag(expected_mode)
        self.report_step(
            result,
            "discovery mode",
            Status.OK,
            mode_detail,
            tag=label,
            tag_styles=styles,
            data={"stages": list(stages), "mode": expected_mode},
        )

        outcomes: list[TargetOutcome] = []
        groups: dict[str, str] = {}

        # One loop, whatever the concurrency. At 1 the strategy runs each
        # target inline -- no pool, no future, no queue -- so the default path
        # executes no concurrency machinery at all and cannot drift from a
        # separate sequential implementation, because there isn't one.
        for address, outcome in self._work_through(usable, pipeline, stages):
            # Reporting, publishing and the verdict all happen here, on this
            # thread, in the order the addresses were given. That is what keeps
            # a run readable: every line about one server is contiguous, and
            # two servers never interleave however many were in flight.
            self._report_target(result, outcome)
            with working_on(outcome.target):
                # Before publishing: an existing entry proved wrong would
                # otherwise stay loaded, and hold its name against the right one.
                self._retire_stale(result, writer, outcome, [a.url for a in usable])
                self._finalise(result, writer, outcome)
                self._reconcile_with_file(writer, outcome)
                self._report_address_facts(result, writer, outcome)
            # The verdict on this address, settled: whether it ended up with a
            # working MCP server, not how many of the attempts along the way
            # came back empty. The summary counts these, and so does the phase.
            result.declare_group(address.url, self._verdict_on(outcome), self._ensure_detail(result, writer, outcome))
            outcomes.append(outcome)
            groups[outcome.target] = address.url
            # Publishing this address may have withdrawn an entry published for
            # an earlier one -- the same claim turning up for an unrelated
            # address. That address's lines were already printed, so the
            # withdrawal gets its own line and its verdict is settled again.
            self._settle_withdrawals(result, writer, outcomes, groups)

        self._report_toolchains(result)
        self._report_summary(result, outcomes)
        self._report_configuration_file(result, writer, outcomes)
        self._report_checked_as_written(result, writer, outcomes)
        self._report_what_travels(result, writer)
        self._report_moved_literals(result, writer)
        self._report_what_claude_loads(result, writer, outcomes)
        self._report_user_steps(result, writer, outcomes)
        self._report_file_not_written(result, writer)
        self._report_missing_entries(result, writer, outcomes)
        self._report_verdict(result, writer, outcomes)

    # -- inputs ----------------------------------------------------------------

    def _declare_local_zones(self) -> None:
        """Take the operator's word for which DNS zones are their own network.

        ``--mcp-server`` takes any address, and the built-in list of local zones
        (loopback, RFC 1918, ``.local``, ``.internal``, ``.lan``, ``.home.arpa``)
        is a set of conventions, not a fact about this network. An internal
        endpoint on a zone this program has not heard of would otherwise be
        marked down for cleartext, refused its bearer token over http, and
        skipped for the developer ports -- three wrong answers from one wrong
        assumption. Logged rather than reported as a step: it is a setting, and
        a step per setting would bury the findings.
        """
        config = self.settings.section("mcp.address")
        suffixes = config.str_list("local_suffixes")
        hosts = config.str_list("loopback_hosts")
        replace = config.bool_("local_suffixes_replace", False)
        if not (suffixes or hosts or replace):
            return
        zones = configure_local_zones(suffixes, loopback_hosts=hosts, replace_defaults=replace)
        self.log.info("treating these as local zones: %s", ", ".join(zones.suffixes))

    def _declare_public_suffixes(self) -> None:
        """Teach grounding which suffixes are public (``[mcp.grounding]``).

        ``example.co.uk`` is an organisation; ``co.uk`` is not. The packaged
        list always counts and the system's public suffix list is added when it
        has one, so the answer is as complete as this machine allows.
        """
        known = configure_suffixes_from(self.settings)
        self.log.debug("public suffixes known: %d", known)

    def _addresses(self) -> list[ServerAddress]:
        """Server addresses from the CLI first, then configuration.

        Whatever the source, the text goes through the same parser: a comma-
        separated list of URLs, bare domains or ``host:port``, each turned into
        one canonical base URL with the duplicates removed.
        """
        requested = self.ctx.state.get("mcp_servers") or []
        if not requested:
            requested = self.settings.str_list("mcp.servers")
        if not requested:
            # The setting was called mcp.targets before the flag became
            # --mcp-server. Honouring it costs one line; ignoring a config file
            # that says exactly what the operator wants costs them a run.
            legacy = self.settings.str_list("mcp.targets")
            if legacy:
                self.log.warning("mcp.targets is deprecated; rename it to mcp.servers")
            requested = legacy
        return parse_all(self._expand_recheck_all(requested))

    def _expand_recheck_all(self, requested: list[str]) -> list[str]:
        """``--mcp-recheck all``: every address an entry in the project file serves (McpWriter.served_addresses).

        A file copied from another machine is proved on this one -- its paths,
        its start-up time, where its credentials are -- by re-checking it, and
        nobody should have to type every address to ask for that. The word is
        configuration (``mcp.recheck_all_word``); an entry no address can be
        tied to is named, never guessed at.
        """
        word = self.settings.str_("mcp.recheck_all_word", "all").strip().lower()
        items = [part for value in requested for part in re.split(r"[,\s]+", str(value)) if part]
        if not word or not self.settings.bool_("mcp.recheck", False) or word not in {i.lower() for i in items}:
            return requested
        addresses, unplaced = McpWriter(self.ctx).served_addresses()
        if unplaced:
            self.log.warning(
                "--mcp-recheck %s: no address could be tied to %s; name them with --mcp-recheck to include them",
                word,
                ", ".join(unplaced),
            )
            self.console.bullet(
                f"not re-checked (no address could be tied to them): {', '.join(unplaced)} -- "
                "name their addresses with --mcp-recheck to include them",
                indent=4,
            )
        rest = [i for i in items if i.lower() != word]
        expanded = [*rest, *[a for a in addresses if a not in rest]]
        if self.ctx.state.get("mcp_servers"):
            # Every other reader of the addresses (inventory.run_hosts, ...) sees
            # the addresses, never the word.
            self.ctx.state["mcp_servers"] = list(expanded)
        return expanded

    def _canonicalise(self, result: PhaseResult, addresses: list[ServerAddress]) -> list[ServerAddress]:
        """Report what each address resolved to, and drop the ones that cannot.

        Discovery appends dozens of paths to whatever comes out of here, so an
        address that is wrong by one character is dozens of wasted probes and a
        conclusion ("no MCP server") that was never about the right host.
        """
        resolver = AddressResolver(self.ctx)
        trust = TrustStore.for_run(self.ctx)
        usable: list[ServerAddress] = []
        for address in addresses:
            if not address.ok:
                self.report_step(
                    result,
                    f"address {address.raw}",
                    Status.FAILED,
                    address.error,
                    # An address that cannot be parsed is still one of the
                    # servers that were asked for, and it is still a server
                    # that produced nothing. It gets a section of its own so
                    # the denominator matches what was typed on the command line.
                    group=address.raw,
                    data=address.to_dict(),
                    remediation=[
                        "Give the address as a URL, a domain or host:port, "
                        "e.g. https://example.com, example.com or localhost:3000.",
                    ],
                )
                continue
            # Before the resolver's own request: an address whose certificate
            # this host cannot verify would otherwise be "no response", and
            # everything after it would be about a host that never answered.
            trust.examine(address.url, target=True)
            resolved = resolver.resolve(address)
            if resolved.url != address.url:
                trust.examine(resolved.url, target=True)
            self._report_trust(result, trust, resolved)
            detail = resolved.url
            if resolved.notes:
                detail = f"{detail} ({'; '.join(resolved.notes)})"
            self.report_step(
                result,
                f"address {resolved.raw}",
                Status.OK,
                detail,
                group=resolved.url,
                data=resolved.to_dict(),
            )
            # Two addresses can be distinct as written and identical once the
            # redirects have been followed; discovering that server twice would
            # also register it twice.
            if all(resolved.url != seen.url for seen in usable):
                usable.append(resolved)
        return usable

    def _report_trust(self, result: PhaseResult, trust: TrustStore, address: ServerAddress) -> None:
        """A step for an address whose certificate this host could not verify on its own.

        Nothing is reported for the ordinary case, so a run against hosts the
        system trusts reads exactly as it always did.
        """
        found = trust.finding(address.url)
        if found is None or found.status not in (ANCHORED, UNTRUSTED):
            return
        issuer = common_name(found.issuer) or "an unknown authority"
        data = found.to_dict()
        if found.status == ANCHORED:
            bundle = trust.bundle()
            anchors = "; ".join(
                f"{common_name(a.get('subject', '')) or '?'} (sha256 {a.get('sha256', '')})" for a in found.anchors
            )
            remediation = []
            if found.learned:
                remediation.append(
                    f"Trusted on first use: {anchors}. Compare the fingerprints with your "
                    "organisation's published CA before relying on it; delete "
                    f"{trust.anchor_dir() or 'the kept anchors'} to forget them, and "
                    "--mcp-trust-pin never to stop learning."
                )
            if found.needs_root():
                remediation.append(
                    f"No self-signed root came with it, which Node refuses: pass the root that "
                    f"issued {issuer} with --mcp-trust-ca FILE so a Node server can reach "
                    f"{found.host} too."
                )
            self.report_step(
                result,
                f"certificate {found.host}",
                Status.CHANGED if found.learned else Status.OK,
                f"issued by {issuer}, which this host's trust store does not hold; "
                f"{found.detail}" + (f"; entries for it name {bundle}" if bundle else ""),
                group=address.url,
                data=data,
                remediation=remediation,
            )
            return
        self.report_step(
            result,
            f"certificate {found.host}",
            Status.PARTIAL,
            f"issued by {issuer}, which nothing on this machine vouches for ({found.detail})",
            group=address.url,
            data=data,
            remediation=[
                f"Pass the authority that issued {issuer} with --mcp-trust-ca FILE (PEM), or "
                "install it in this host's trust store, then re-run. A server registered for "
                f"{found.host} cannot verify it until then.",
            ],
        )

    def _stages(self) -> list[str]:
        configured = self.settings.str_list("mcp.stages", list(DEFAULT_STAGES))
        known = set(DEFAULT_STAGES)
        stages = [s for s in configured if s in known]
        if not stages:
            self.log.warning("no valid stages configured; using the default order")
            return list(DEFAULT_STAGES)
        return stages

    # -- one server, or all of them --------------------------------------------

    def _pick_best(self) -> bool:
        """Register everything that answers, or only the best one per address?"""
        return self.settings.bool_("mcp.pick_best", False)

    def _record_all(self) -> bool:
        """Keep looking after the first working endpoint, and record them all."""
        return self.settings.bool_("mcp.exhaustive", False)

    def _discovery_fact(self) -> str:
        """How the stages will be carried out, said before the first one starts."""
        if not self._agentic_wanted():
            return (
                "traditional (--no-agent) -- deterministic probing and rule-based reasoning "
                "for every stage; no Claude subagent is started"
            )
        attempts = max(1, self.settings.int_("mcp.agent.max_attempts", 3))
        return (
            f"agentic -- a Claude subagent per stage, retried up to {attempts} time(s) with what "
            "went wrong before deterministic probing finishes a stage it could not settle "
            "(--no-agent runs the deterministic pipeline only)"
        )

    def _selection_fact(self) -> str:
        if self._pick_best():
            return (
                "the best one per address (--the-best-mcp), ranked on "
                + ", ".join(ASPECTS)
                + "; the runners-up are reported, not registered"
            )
        if self._record_all():
            return (
                "every working endpoint per address (--exhaustive-mcp); the search does "
                "not stop at the first one, and each of them is registered"
            )
        return (
            "the first server that completes a handshake, which is where the search stops "
            "(--exhaustive-mcp records them all, --the-best-mcp compares them)"
        )

    def _provisioning_fact(self) -> str:
        """What --mcp-provision-missing will do, in the banner's words."""
        mode = Provisioner.for_run(self.ctx).mode
        return {
            "auto": "installed from the phase 1 catalog when a candidate needs one and no "
            "password prompt is involved (--mcp-provision-missing auto)",
            "always": "installed from the phase 1 catalog when a candidate needs one, "
            "privileged strategies included (--mcp-provision-missing always)",
        }.get(mode, "not installed; a candidate that needs one is rejected (--mcp-provision-missing never)")

    def _authenticated_fact(self) -> str:
        """What happens to an endpoint that answers 401 or 403, said up front.

        It is the commonest way a real endpoint looks like a failure, and what
        this run will do about it -- write it out with an empty credential, or
        only mention it -- is a setting, so the banner says which.
        """
        if not self.settings.bool_("mcp.discovery.auth.register_authenticated", True):
            return (
                "an endpoint that requires authentication is reported only "
                "(mcp.discovery.auth.register_authenticated = false)"
            )
        template = self.settings.str_("mcp.discovery.auth.token_env_template", "{SLUG}_TOKEN")
        return (
            "an endpoint that answers 401/403 is a find: registered with its credential slot "
            f"empty, named {template.replace('{SLUG}', '<server>').replace('{slug}', '<server>')}, "
            "and reported as needing a real token before it works"
        )

    def _widen_search(self, result: PhaseResult) -> None:
        """Keep looking past the first working server, and say so.

        Both of these are on by default for a good reason -- one working server
        is all it takes to finish, and stopping there is quicker and touches
        less. Recording every endpoint and choosing between them are two
        different reasons to want the rest of them, and both need the same
        widening; doing it silently would leave someone wondering why the run
        suddenly took four times as long.
        """
        widened: list[str] = []
        if not self.settings.bool_("mcp.exhaustive", False):
            self.settings.set("mcp.exhaustive", True)
            widened.append("every stage runs, not just up to the first that works")
        if self.settings.bool_("mcp.discovery.stop_at_first", True):
            self.settings.set("mcp.discovery.stop_at_first", False)
            widened.append("every candidate path is probed, not just up to the first endpoint")
        intent = (
            "keeping the best server per address"
            if self._pick_best()
            else "recording every working endpoint per address"
        )
        self.report_step(
            result,
            "selection",
            Status.OK,
            f"{intent}; " + ("; ".join(widened) if widened else "the search was already exhaustive"),
            data={
                "pick_best": self._pick_best(),
                "exhaustive": True,
                "aspects": list(ASPECTS) if self._pick_best() else [],
                "widened": widened,
            },
        )

    def _agentic_wanted(self) -> bool:
        """Whether the operator will allow a subagent at all (``--no-agent``)."""
        return self.settings.bool_("mcp.agentic", True)

    def _pipeline(self) -> tuple[AgenticPipeline | TraditionalPipeline, str, str]:
        """The agentic pipeline by default; it is the one that knows how to degrade.

        Discovery is an open-ended search -- read a site, judge what it exposes,
        decide what would bridge it -- and that is what an agent is for; the
        deterministic pipeline is the safety net beneath it, not an alternative
        route someone should have to pick in advance. So the agentic pipeline
        normally runs, and *within* it each stage is the agent's to settle: it
        retries with what went wrong, changes runtime or model when the failure
        was in the machinery, and only an exhausted stage reaches the
        deterministic pass.

        ``--no-agent`` is the one way to choose, and it is a real choice rather
        than a hint: the deterministic pipeline is run *directly*, so no runtime
        is looked for, no workspace is made and no turn is ever spent. Returns
        the pipeline, the mode it expects to start in, and the sentence the
        operator gets for it.
        """
        if not self._agentic_wanted():
            return (
                TraditionalPipeline(self.ctx),
                Mode.TRADITIONAL,
                (
                    "--no-agent: every stage runs deterministically -- probing and rule-based "
                    "reasoning only, no subagent is started"
                ),
            )
        pipeline = AgenticPipeline(self.ctx)
        usable, why = pipeline.available()
        attempts = max(1, self.settings.int_("mcp.agent.max_attempts", 3))
        if usable:
            return (
                pipeline,
                Mode.AGENTIC,
                (
                    f"every stage is a Claude subagent via the {pipeline.runtime_reason}, retried "
                    f"up to {attempts} time(s) with what went wrong before deterministic probing "
                    "finishes it"
                ),
            )
        # Not a failure: a dry run and a host without a Claude runtime are both
        # ordinary. The phase still does its whole job, deterministically, and
        # every stage line below says so rather than leaving it to be inferred.
        return (
            pipeline,
            Mode.TRADITIONAL,
            (f"agents cannot run here ({why}), so every stage falls back to deterministic probing"),
        )

    # -- per target ------------------------------------------------------------

    def concurrency(self) -> int:
        """How many addresses may be worked at once. 1 means strictly in turn."""
        requested = str(self.settings.get("mcp.discovery.target_concurrency", 1)).strip()
        if requested.lower() == BARE_CONCURRENCY:
            # Bare --concurrency (or the keyword in the environment variable):
            # how many that means is configuration, not a number in the parser.
            return max(1, self.settings.int_("mcp.discovery.target_concurrency_bare", 4))
        return max(1, self.settings.int_("mcp.discovery.target_concurrency", 1))

    def _work_through(
        self, addresses: Sequence[ServerAddress], pipeline, stages: Sequence[str]
    ) -> Iterator[tuple[ServerAddress, TargetOutcome]]:
        """Yield ``(address, outcome)`` in the order the addresses were given.

        The order is the contract, not an accident of scheduling: the caller
        reports and registers as it iterates, so yielding in completion order
        would shuffle the report between runs and interleave one server's lines
        with another's.

        At a concurrency of 1 this does not start a thread. That is deliberate
        -- the sequential path is the one almost every run takes, and it should
        not execute machinery it does not need, nor be a second implementation
        that can drift from this one.
        """
        deadline = self.ctx.deadline
        workers = self.concurrency()
        if workers <= 1:
            for index, address in enumerate(addresses):
                if deadline.expired:
                    self._out_of_time(addresses[index:])
                    return
                self.console.write("")
                self.console.rule(address.url)
                yield address, self._run_target(pipeline, address.url, stages)
            return

        # Concurrent: the spend cap is applied when a target is admitted rather
        # than inside a stage, so no target's agent loop is cut short by what
        # another target spent. See AgenticPipeline._agent_attempts.
        if isinstance(pipeline, AgenticPipeline):
            pipeline.budget_checked_at_admission = True
        log.info("working %d address(es) at a time", workers)
        self.console.bullet(
            f"{len(addresses)} address(es), {workers} at a time -- each server's lines are "
            "held back and printed together, so they never interleave",
            indent=2,
        )
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers, thread_name_prefix="mcp-target") as pool:
            futures = {
                pool.submit(self._run_target, pipeline, address.url, stages, quiet=True): address
                for address in addresses
            }
            pending = {a.url for a in addresses}
            swept = False
            try:
                for address in addresses:
                    future = next(f for f, a in futures.items() if a is address)
                    # Every address is submitted up front, so behind the
                    # `workers` that are running there is a queue the pool will
                    # start the moment a worker frees. That is why the sweep
                    # happens the first time expiry is *noticed*, not when the
                    # loop reaches each address: the loop awaits in order and
                    # the pool is always ahead of it, so checking per address
                    # would find every one of them already started and cancel
                    # nothing. Cancelling a queued future is clean and instant;
                    # a running one is an agent mid-turn, and killing that
                    # throws away tokens already paid for -- so the sweep drops
                    # what has not begun and leaves what has.
                    if deadline.expired and not swept:
                        swept = True
                        dropped = [a for f, a in futures.items() if f.cancel()]
                        pending.difference_update(a.url for a in dropped)
                        self._out_of_time(dropped)
                    if future.cancelled():
                        continue
                    outcome = self._await(future, pending)
                    pending.discard(address.url)
                    self.console.write("")
                    self.console.rule(address.url)
                    yield address, outcome
            except BaseException:
                # Ctrl-C, or a failure while reporting. Stop what has not
                # started; the children already running are ended by the
                # runner's own signal ladder when the process goes down.
                for future in futures:
                    future.cancel()
                raise

    def _out_of_time(self, addresses: Sequence[ServerAddress]) -> None:
        """Record addresses the deadline stopped us from starting.

        Named individually rather than counted, because the useful next step is
        to re-run exactly these -- and because "3 addresses were skipped" is the
        kind of summary that sends somebody back to the log to find out which.
        """
        if not addresses:
            return
        deadline = self.ctx.deadline
        deadline.note_skipped(*(a.url for a in addresses))
        log.info(
            "deadline reached after %s; not starting: %s",
            format_duration(deadline.elapsed),
            ", ".join(a.url for a in addresses),
        )
        self.console.write("")
        self.console.status(
            Status.SKIPPED,
            f"out of time after {format_duration(deadline.elapsed)}",
            f"--timeout was {format_duration(deadline.limit)}; {len(addresses)} address(es) not started",
        )

    def _await(self, future, pending: set[str]) -> TargetOutcome:
        """Wait for one address, saying periodically which are still in flight.

        The workers are silent, so without this a concurrent run would print
        nothing for however long the slowest address takes. One line naming the
        addresses still going is the whole of the live commentary -- enough to
        see it is alive and what it is waiting on, and short enough not to
        become the noise it replaced.
        """
        every = max(0.0, self.settings.float_("mcp.agent.progress_interval_seconds", 30.0))
        if every <= 0 or not self.console.show_progress:
            return future.result()
        waited = 0.0
        while True:
            try:
                return future.result(timeout=every)
            except concurrent.futures.TimeoutError:
                waited += every
                self.console.progress(
                    f"{len(pending)} address(es) still working ({format_duration(waited)})",
                    ", ".join(sorted(pending)),
                )

    def _run_target(self, pipeline, target: str, stages: Sequence[str], *, quiet: bool = False) -> TargetOutcome:
        """Work one address. No reporting: this may be running beside others."""
        if quiet:
            # The live commentary belongs to one piece of work. Four at once,
            # with the agent heartbeat not even naming its target, is noise --
            # the caller prints one consolidated in-flight line instead.
            self.console.mute_transient()
        try:
            with working_on(target):
                return pipeline.run(target, stages)
        finally:
            if quiet:
                self.console.mute_transient(False)

    def _report_target(self, result: PhaseResult, outcome: TargetOutcome) -> None:
        """Everything this address has to say, printed in one contiguous block."""
        target = outcome.target
        for stage in outcome.stages:
            self._report_stage(result, target, stage)

        self._report_due_diligence(result, outcome)

        skipped = [s for s in self._stages() if outcome.stage(s) is None]
        if skipped and outcome.verified:
            self.console.bullet(
                f"stopped after {outcome.stages[-1].stage}: a working server was found "
                f"(--exhaustive-mcp runs {', '.join(skipped)} anyway and records what they find)",
                indent=4,
            )

    def _report_registrations(self, result: PhaseResult, outcome: TargetOutcome, registrations) -> None:
        """One line per registration: refused, skipped, simulated, partial or written."""
        for registration in registrations:
            if registration.refused:
                # A gate rejected the entry and nothing was written. Said as its
                # own line, with the reason, because "the server is missing from
                # .mcp.json" is a question somebody will otherwise have to take
                # to the log to answer.
                self.report_step(
                    result,
                    f"{outcome.target} :: refused {registration.name}",
                    Status.SKIPPED,
                    f"not registered -- {registration.refused}",
                    group=outcome.target,
                    data=registration.to_dict(),
                    remediation=registration.remediation,
                )
                continue
            if registration.skipped and not registration.written:
                status = Status.SKIPPED
            elif registration.simulated:
                status = Status.OK  # everything but the write itself succeeded
            elif (
                registration.cli_failed
                or registration.claude_connect.get("outcome") == "failed"
                or registration.claude_usage.get("outcome") == "failed"
            ):
                # Written, but the claude CLI refused it, Claude Code cannot
                # connect to it, or Claude Code connected and its tool failed:
                # never a plain "registered".
                status = Status.PARTIAL
            elif registration.registered or registration.written:
                status = Status.CHANGED
            else:
                status = Status.PARTIAL
            self.report_step(
                result,
                f"{outcome.target} :: register {registration.name}",
                status,
                registration.detail or registration.skipped,
                group=outcome.target,
                data=registration.to_dict(),
                remediation=registration.remediation,
            )

    def _nothing_written_works(self, outcome: TargetOutcome, registrations) -> bool:
        """Did publishing leave the address with no entry that works or waits -- every one refused, or unusable?"""
        if not registrations or not self.settings.bool_("mcp.handover.after_publish", True):
            return False
        kept = [r for r in registrations if (r.written or r.simulated) and not r.refused]
        return not [r for r in kept if r.candidate is None or r.candidate.identity() not in outcome.consumer_failed]

    def _publish_after_hand_over(
        self, result: PhaseResult, writer: McpWriter, outcome: TargetOutcome, published
    ) -> list[Registration]:
        """Hand over after a refusal at publish; publish only what the hand-over found, and report it."""
        before = {c.identity() for r in published if r.candidate is not None for c in [r.candidate]}
        if not self._hand_over(result, outcome, "nothing registered works"):
            return []
        fresh_verified = [c for c in outcome.verified if c.identity() not in before]
        fresh_waiting = [c for c in outcome.provisional if c.identity() not in before]
        if not fresh_verified and not fresh_waiting:
            return []
        kept_verified, kept_waiting = list(outcome.verified), list(outcome.provisional)
        outcome.verified, outcome.provisional = fresh_verified, ([] if fresh_verified else fresh_waiting)
        try:
            again = writer.publish(outcome)
        finally:
            outcome.verified = [*[c for c in kept_verified if c not in outcome.verified], *outcome.verified]
            outcome.provisional = [*[c for c in kept_waiting if c not in outcome.provisional], *outcome.provisional]
        self._note_toolchains(outcome, again)
        self._report_verification(result, writer, outcome, again)
        self._report_registrations(result, outcome, again)
        return again

    def _report_stage(self, result: PhaseResult, target: str, stage) -> None:
        """One stage's line -- also for a stage a hand-over reopened after the address's block was printed."""
        # A mode answers "how was this stage carried out". A skipped stage
        # was not carried out, by anybody, so none of the three values is
        # true of it and the column is left empty -- the detail already says
        # which rule skipped it, which is the question a reader has.
        label, styles = ("", ()) if _never_ran(stage) else mode_tag(stage.mode)
        self.report_step(
            result,
            f"{target} :: {stage.stage}",
            stage.status,
            stage.detail,
            group=target,
            # The mode is the second thing worth knowing about a stage, so it
            # gets its own coloured column rather than a word inside the
            # detail: "who did this" is scannable down the page, and a stage
            # that quietly fell back cannot pass for one that did not.
            tag=label,
            tag_styles=styles,
            data=stage.to_dict(),
            remediation=stage.remediation,
            duration=stage.duration,
            error=stage.error,
        )
        # "[fallback]" says the agent did not finish the stage and nothing
        # about why, which is a shrug rather than a diagnosis -- and the
        # four reasons it can happen have four different answers. The one
        # that applies goes on the page, next to the tag that raised the
        # question.
        why = str(stage.findings.get("agentic_inconclusive") or "")
        if stage.mode == Mode.FALLBACK and why:
            self.console.bullet(f"why the agent did not settle it: {why}", indent=4)

    def _report_due_diligence(self, result: PhaseResult, outcome: TargetOutcome) -> None:
        """The tier-1 checklist for one address, step by step.

        The stage lines say what was found. This says how hard it was looked
        for, which is the part that has to be on the page before "no MCP server
        here" means anything -- and the part that decides whether a package may
        be installed or a bridge written at all.
        """
        record = outcome.due_diligence
        if not isinstance(record, DueDiligence):
            return

        self.console.write("")
        self.console.write(f"  Native-endpoint due diligence ({record.source}):")
        self.console.table(record.rows(), headers=["step", "what it establishes", "done", "what was done"])

        gaps = record.gaps()
        remediation: list[str] = []
        if gaps:
            remediation.append(
                "Steps not carried out: "
                + "; ".join(f"{s.letter} -- {s.title}" for s in gaps)
                + ". Until they are, the package and bridge stages stay closed "
                "(mcp.policy.require_checklist)."
            )
        if record.native_endpoints:
            remediation.append(
                f"The target runs its own MCP server at {record.native_endpoints[0]}; nothing was "
                "installed or generated, which is the intended outcome."
            )
        self.report_step(
            result,
            f"{outcome.target} :: due diligence",
            Status.OK if record.complete else Status.PARTIAL,
            record.summary(),
            group=outcome.target,
            data=record.to_dict(),
            remediation=remediation,
        )

    def _finalise(self, result: PhaseResult, writer: McpWriter, outcome: TargetOutcome) -> None:
        """Settle the address -- judge again what is stale, re-check, hand over -- then register whatever answers.

        Every step that can take the settling server away hands over before the
        address is given up (:meth:`_hand_over`): gitlab.example.net's re-check
        withdrew mcp-graphql, nothing was tried in its place, and the bridge
        that had worked there -- one tier down, skipped because the tier above
        had "already answered" -- was never looked at.
        """
        writer = writer or McpWriter(self.ctx)
        if outcome.verified or outcome.provisional:
            outcome.verified = self._deduplicate(outcome.verified)
            known = {c.identity() for c in outcome.verified}
            outcome.provisional = [c for c in self._deduplicate(outcome.provisional) if c.identity() not in known]
            self._rejudge_stale(result, outcome)
            if outcome.verified and self.settings.bool_("mcp.verification.final_recheck", True):
                self._final_recheck(result, outcome)
        if not outcome.verified and not outcome.provisional:
            self._hand_over(result, outcome, "nothing was left to register")
        if not outcome.verified and not outcome.provisional:
            outcome.status = worst([s.status for s in outcome.stages]) or Status.PARTIAL
            writer = writer or McpWriter(self.ctx)
            outcome.detail = self._nothing_registered(writer, outcome, "no working MCP server was found")
            self._report_existing_left(result, writer, outcome)
            if outcome.playbook:
                self.console.write("")
                self.console.write(f"  What to try for {outcome.target}:")
                for item in outcome.playbook:
                    self.console.bullet(item, indent=4)
            return

        # Only now, after the last check that could take the working server
        # away: a server that waits for a credential was kept until here so
        # that it could take its place (see _hand_over).
        if outcome.verified and not self.settings.bool_("mcp.exhaustive", False):
            # A server that works beats one that would work given a token the
            # operator may not have. The finding stays in the report; what it
            # does not do is put a second, dormant entry in the file beside a
            # live one. --exhaustive-mcp asks for everything, and gets it.
            for candidate in outcome.provisional:
                candidate.add_note(
                    "not registered: a server that needs no credential was found "
                    "for this address (--exhaustive-mcp registers both)"
                )
            outcome.provisional = []

        # After the re-check, so the winner is chosen among servers that still
        # answer, and before publishing, so only the winner is registered.
        if self._pick_best():
            self._choose_best(result, outcome)
        elif self._record_all() and outcome.verified:
            self._report_inventory(result, outcome)

        registrations = writer.publish(outcome)
        self._note_toolchains(outcome, registrations)
        self._report_verification(result, writer, outcome, registrations)
        self._report_registrations(result, outcome, registrations)
        if self._nothing_written_works(outcome, registrations):
            # A gate after publishing -- every entry refused at the start check,
            # or Claude Code itself unable to use what was written -- leaves the
            # address with nothing that works: it hands over like any other.
            registrations += self._publish_after_hand_over(result, writer, outcome, registrations)

        if outcome.duplicates:
            self.report_step(
                result,
                f"{outcome.target} :: duplicate configuration",
                Status.PARTIAL,
                f"{len(outcome.duplicates)} existing entr(y/ies) in {writer.display_path} already "
                "point at this service",
                group=outcome.target,
                data={"duplicates": outcome.duplicates},
                remediation=[
                    f"{d['name']} -> {d['target']}: {d['why']} Keep one: delete it from {writer.display_path}"
                    + (
                        f", and from the claude CLI: `{writer.remove_by_hand(d['name'])}`"
                        if writer.cli_scopes(d["name"])
                        else ""
                    )
                    + "."
                    for d in outcome.duplicates
                ],
            )

        for kept_entry in outcome.kept_existing:
            # Nothing new could be written, but the address is not empty: the
            # machine still holds a registration that answered today.
            self.report_step(
                result,
                f"{outcome.target} :: keep {kept_entry['name']}",
                Status.SKIPPED,
                "left as it was -- the existing registration answered during this run, and "
                "nothing could be written in its place",
                group=outcome.target,
                data=dict(kept_entry),
                remediation=[kept_entry["remediation"]],
            )

        # Everything below is about what the address *yielded*, so it counts
        # entries that exist. A candidate a gate refused was found and then
        # deliberately not written, and calling the address a success on the
        # strength of it would be the same mistake the gates exist to prevent.
        kept = [r for r in registrations if not r.refused]
        waiting_unwritten = [c for c in outcome.provisional if all(r.candidate is not c for r in registrations)]
        self._settle_found(outcome, registrations)
        if registrations and not kept:
            outcome.status = Status.PARTIAL
            outcome.detail = self._nothing_registered(
                writer,
                outcome,
                "found a server, registered none: " + "; ".join(f"{r.name} -- {r.refused}" for r in registrations),
            )
            if outcome.kept_existing:
                outcome.detail += "; kept the existing " + ", ".join(k["name"] for k in outcome.kept_existing)
            self._report_existing_left(result, writer, outcome)
            return

        if outcome.verified and any(not r.provisional for r in kept):
            outcome.status = Status.OK
            # This sentence heads the address's section in the phase summary,
            # so it says what was found and under which name rather than
            # leaving a bare label to be matched up against the register lines.
            found = [f"{r.name} -- {r.candidate.label}" for r in kept if not r.provisional]
            outcome.detail = "working MCP server: " + ", ".join(found or [c.label for c in outcome.verified])
            return
        # Only servers waiting on a credential: written out, not working, and
        # the phase says so rather than claiming either a success or a blank.
        # Named from what was written, so the labels, the variables and "it" or
        # "them" all describe the same entries.
        waiting = [r for r in kept if r.provisional]
        outcome.status = Status.PARTIAL
        if not waiting:
            # Found, and deliberately not written: registering servers that
            # wait for a credential is switched off.
            outcome.detail = (
                f"{', '.join(c.label for c in waiting_unwritten) or 'a server'} "
                f"{waits_for_credential(waiting_unwritten[0]) if waiting_unwritten else 'waits for a credential'}"
                " -- not registered, because mcp.discovery.auth.register_authenticated is off"
            )
            return
        needed = sorted({name for r in waiting for name in r.needs_env})
        if needed:
            outcome.detail = (
                f"{', '.join(r.candidate.label for r in waiting)} -- registered with an empty "
                f"credential; set {', '.join(needed)} to use {'them' if len(waiting) > 1 else 'it'}"
            )
        else:
            # Nothing is left to set -- the credential is in place where Claude
            # Code reads it -- so the line never asks for nothing: it says what
            # stands between it and working (confluence-example-com read "set  to
            # use it" once its token was kept in settings).
            why = "; ".join(sorted({_login_words(r.candidate) for r in waiting} - {""})) or "its login was not proved"
            outcome.detail = (
                f"{', '.join(r.candidate.label for r in waiting)} -- registered with its credential in place, "
                f"not proved this run: {why}"
            )

    @staticmethod
    def _schema_problems(outcomes: Sequence[TargetOutcome]) -> list[dict[str, Any]]:
        """Every server this run saw whose tools break the client's naming rules, once each."""
        from ..mcp_auto_discovery.probes import name_schema_problems

        seen: dict[str, dict[str, Any]] = {}
        for outcome in outcomes:
            pool = [c for stage in outcome.stages for c in stage.candidates]
            pool += [*outcome.verified, *outcome.provisional, *outcome.refused]
            for candidate in pool:
                shake = candidate.handshake
                if shake is None or not shake.schema_problems or candidate.label in seen:
                    continue
                seen[candidate.label] = {
                    "target": outcome.target,
                    "server": candidate.label,
                    "problems": list(shake.schema_problems),
                    "detail": name_schema_problems(shake.schema_problems),
                }
        return list(seen.values())

    @staticmethod
    def address_outcomes(servers: Sequence[Mapping[str, Any]]) -> dict[str, list[Mapping[str, Any]]]:
        """Each address in exactly one of three kinds, the same everywhere they are counted.

        ``working``: a server completed a handshake and is written. ``waiting``:
        an entry is written, waiting for a credential. ``unregistered``: nothing
        new was written for it -- absent from the file unless an older entry was
        kept. The summary, the verdict and the safety note all read this, so
        build.opensuse.org can no longer sit in one "failed" pile with eight
        addresses that did get entries.
        """
        out: dict[str, list[Mapping[str, Any]]] = {"working": [], "waiting": [], "unregistered": []}
        for server in servers:
            if server.get("succeeded"):
                out["working"].append(server)
            elif server.get("registered"):
                out["waiting"].append(server)
            else:
                out["unregistered"].append(server)
        return out

    def outcome_lines(self, result: PhaseResult, limit: int) -> list[Line]:
        """Working, waiting for a credential, nothing registered -- one row each."""
        servers = list(result.data.get("servers") or [])
        if not servers:
            return []
        kinds = self.address_outcomes(servers)
        labels = {
            "working": ("working", ("green",)),
            "waiting": ("waiting for a credential", ("yellow",)),
            "unregistered": ("nothing registered", ("red", "bold")),
        }
        lines = [Line("")]
        for kind, (label, styles) in labels.items():
            named = []
            for server in kinds[kind]:
                names = ", ".join(server.get("registered") or [])
                # Waiting, but for a *working* credential: the one supplied was
                # refused. Said on the row, because "waiting for a credential"
                # alone tells the operator to do what they already did.
                refused = sorted(
                    {
                        login_check.in_words(str(row.get("verdict") or ""))
                        for row in server.get("credential_refused") or []
                        if isinstance(row, Mapping)
                    }
                    - {""}
                )
                named.append(
                    f"{server.get('target', '?')}"
                    + (f" ({names})" if names else "")
                    + (f" -- {'; '.join(refused)}" if refused else "")
                )
            if len(named) > limit:
                named = [*named[:limit], f"... and {len(named) - limit} more"]
            head = f"  {label}: {len(kinds[kind])}  "
            lines.append(Line(f"{head}{', '.join(named) or '-'}", styles, hang=len(head)))
        return lines

    def _recheck_command(self, target: str) -> str:
        """The command that proves ``target`` again (``mcp.registration.recheck_command``)."""
        from ..mcp_auto_discovery.grounding import host_of

        template = self.settings.str_(
            "mcp.registration.recheck_command", "initialize-claude-ai run --phases 4 --mcp-recheck {host}"
        )
        return template.replace("{host}", host_of(target) or target)

    def _nothing_registered(self, writer: McpWriter, outcome: TargetOutcome, why: str) -> str:
        """The address's line when nothing was written for it: say so, and where to go next.

        build.opensuse.org ended as "no working MCP server was found", grouped
        with eight addresses that *did* get entries, while openSUSE's own server
        had been found four times. The line now says plainly that the file has
        no entry for the address, and names the candidate that came closest.
        """
        outcome.best_lead = self._best_lead(writer, outcome)
        name = writer.base_name(outcome.target)
        if name in writer.existing_entries():
            # Said only after looking: a dry run, or a run that proved nothing
            # better, leaves the file's own entry where it was -- "has no entry"
            # was printed for entries plainly in the file.
            text = (
                f"nothing written this run -- {writer.display_path} keeps its existing {name}, not proved today ({why})"
            )
        else:
            text = f"nothing registered -- {writer.display_path} has no entry for {name} ({why})"
        lead = outcome.best_lead
        if lead:
            text += f"; best lead: {lead['name']}"
            if lead.get("needs"):
                text += f", which needs {', '.join(lead['needs'])}"
            if lead.get("said"):
                text += f" (it said: {lead['said']})"
            if lead.get("why"):
                text += f"; {lead['why']}"
        return text

    def _best_lead(self, writer: McpWriter, outcome: TargetOutcome) -> dict[str, Any]:
        """The candidate that came closest for an address that got nothing; {} if none.

        Only one that could be this address's at all -- grounded for it -- and,
        among those, one waiting for a credential first, then one that answered
        at all, then the most confident. What it is, how it is launched, what it
        needs and what it said, in words an operator can act on.
        """
        from ..core.redact import redact_argv
        from ..mcp_auto_discovery.grounding import endpoint_hosts

        known = endpoint_hosts(outcome)
        pool: list[Candidate] = []
        for stage in outcome.stages:
            pool.extend(c for c in stage.candidates if all(c is not p for p in pool))
        pool.extend(c for c in outcome.refused if all(c is not p for p in pool))
        # Discarded by the start gate, but found: still nearer than a URL that
        # was not there. osc-mcp was dropped three times for build.opensuse.org
        # and the lead printed was https://build.opensuse.org/mcp (HTTP 404).
        pool.extend(c for c in outcome.unstartable if all(c is not p for p in pool))
        grounded = [c for c in pool if writer.grounding_of(c, outcome.target, known).registrable]
        if not grounded:
            return {}
        best = max(grounded, key=_lead_rank)
        launch = best.url or " ".join(redact_argv(best.full_command()))
        said = best.start_failure or (best.handshake.error if best.handshake else "")
        return {
            "name": best.label,
            "launch": launch,
            "source": best.source,
            "needs": sorted(writer.exported_variables(best)) if best.secret_env else [],
            "said": writer.verifier.excerpt(said) if said else "",
            # What the run worked out about it, beyond its own words: that the
            # operator's configuration breaks it, or that a credential is
            # proved not to be what it is missing.
            "why": (best.handshake.config_interference or best.handshake.credential_test) if best.handshake else "",
        }

    def _report_refused_every_way(self, result: PhaseResult, outcome: TargetOutcome) -> None:
        """Every way the address was asked with your credentials, refused alike: said once, plainly.

        build.example.net refused the encoded login (as a password, 401 exactly as
        for nobody) and the SSH signature (api.example.net), while the report went on
        asking for another variable. When a real credential was tried on at least
        one candidate, none proved access, and every one tried was refused, the
        operator is told which credentials (by where they came from, never a
        value) and which servers -- and to check the login itself.
        """
        from ..mcp_auto_discovery import login_check

        pool = [c for st in outcome.stages for c in st.candidates]
        # A login proved by a call is what silences this -- not a server that
        # answered without any login (a public third-party one the gate refused).
        if not pool or any(c.handshake is not None and c.handshake.login == login_check.ACCEPTED for c in pool):
            return
        tried = [c for c in pool if c.handshake is not None and c.handshake.login_credential]
        if not tried or not all(
            c.handshake.login in login_check.REFUSED or c.handshake.login_reason == login_check.SAME_AS_ANONYMOUS
            for c in tried
        ):
            return
        host = outcome.target.split("://", 1)[-1].split("/", 1)[0]
        if all(login_check.credential_missing(c.handshake.login_detail, self.ctx.settings) for c in tried):
            self._report_nothing_to_log_in_with(result, outcome, tried, host)
            return
        sources = sorted({c.handshake.login_source for c in tried if c.handshake.login_source})
        servers = ", ".join(sorted({c.label for c in tried}))
        self.report_step(
            result,
            f"{outcome.target} :: your login",
            Status.PARTIAL,
            f"{host} refused every login this run tried ({len(tried)} server(s))",
            group=outcome.target,
            data={"servers": sorted({c.label for c in tried}), "credentials_from": sources},
            remediation=[
                f"{host} refused your login every way it was tried -- {servers} -- with "
                f"{'; '.join(sources) or 'the credentials you export'}, each answered as for nobody logged in. "
                f"The entry is not what is wrong: check that this login works on {host} itself (sign in on its web "
                "page; for a key, check it is registered in your profile), then re-run phase 4 for it."
            ],
        )

    def _report_login_of_another_service(self, result: PhaseResult, outcomes: list[TargetOutcome]) -> None:
        """A login one service refused that a service of *another* organisation accepted: say whose it is.

        build.example.net was given build.opensuse.org's user name and password and
        answered 401; the report said only "check that the login works". The
        password is the same one build.opensuse.org accepts -- it is the
        openSUSE login, and build.example.net needs the account its own organisation
        issues. Compared by fingerprints of the secret parts
        (:attr:`Handshake.login_parts`), never by value; within one
        organisation one login on many services is ordinary (single sign-on)
        and is not reported. (``mcp.verification.login_check.name_other_service_login``.)
        """
        from ..mcp_auto_discovery.grounding import registrable_domain, same_organisation
        from ..mcp_auto_discovery.types import proves_access

        if not self.ctx.settings.bool_("mcp.verification.login_check.name_other_service_login", True):
            return

        def host(outcome: TargetOutcome) -> str:
            return outcome.target.split("://", 1)[-1].split("/", 1)[0].lower()

        def pool(outcome: TargetOutcome) -> list:
            seen: dict[int, Any] = {}
            for c in [
                *(c for st in outcome.stages for c in st.candidates),
                *outcome.verified,
                *outcome.provisional,
                *outcome.refused,
            ]:
                seen.setdefault(id(c), c)
            return list(seen.values())

        from ..mcp_auto_discovery.secret_policy import secret_part_prints

        def parts(c) -> list[str]:
            shake = c.handshake
            return list(shake.login_parts if shake is not None and shake.login_parts else secret_part_prints(c.in_use))

        # Accepted by a call -- or in use by an entry kept because it answered or
        # Claude Code connects to it: build-opensuse-org was kept that way, its
        # login never checked, and the openSUSE login in build-example-net went
        # unnamed (mcp.verification.login_check.other_service_counts_kept).
        kept_counts = self.ctx.settings.bool_("mcp.verification.login_check.other_service_counts_kept", True)
        accepted: dict[str, tuple[str, str]] = {}
        for outcome in outcomes:
            for c in pool(outcome):
                shake = c.handshake
                proved = shake is not None and (shake.login == login_check.ACCEPTED or proves_access(c))
                kept = (
                    kept_counts
                    and c.existing_name
                    and (c.existing_name in outcome.connected_existing or c.existing_name in outcome.answered_existing)
                )
                if proved or kept:
                    for part in parts(c):
                        if proved or part not in accepted:
                            accepted[part] = (
                                host(outcome),
                                "accepts" if proved else f"uses (its {c.existing_name} entry)",
                            )
        rows: list[dict[str, Any]] = []
        for outcome in outcomes:
            mine = host(outcome)
            for c in pool(outcome):
                shake = c.handshake
                if shake is None or shake.login not in login_check.REFUSED:
                    continue
                if login_check.nothing_sent(shake) or login_check.credential_missing(
                    shake.login_detail, self.ctx.settings
                ):
                    continue  # nothing was sent, so nothing was refused
                others = sorted(
                    {
                        accepted[p]
                        for p in parts(c)
                        if p in accepted and accepted[p][0] != mine and not same_organisation(accepted[p][0], mine)
                    }
                )
                if others and not any(r["host"] == mine for r in rows):
                    rows.append(
                        {
                            "host": mine,
                            "server": c.label,
                            "accepted_by": [h for h, _ in others],
                            "how": others[0][1],
                            "organisation": registrable_domain(mine),
                        }
                    )
        if not rows:
            return
        self.report_step(
            result,
            "a login from another service",
            Status.PARTIAL,
            f"{len(rows)} address(es) refused a login that a service of another organisation accepts",
            data={"logins_of_another_service": rows},
            remediation=[
                f"{r['host']} refused the login {', '.join(r['accepted_by'])} {r.get('how', 'accepts')} ({r['server']}). "
                "One user name and password on several services is fine, each entry with its own address; but "
                f"{r['host']} did not accept this one -- it may hold another password for it, or need the account "
                f"{r['organisation']} issues you. Supply what {r['host']} accepts in its variables, then re-run phase 4 for it."
                for r in rows
            ],
        )

    def _report_nothing_to_log_in_with(
        self, result: PhaseResult, outcome: TargetOutcome, tried: list, host: str
    ) -> None:
        """No login reached the address at all -- every route answered "not set" or "no key": say what to supply.

        build.example.net on a machine with no password exported and no SSH key
        was told it "refused every login" and to check that the login works --
        when nothing was ever sent. Each server is named with what it reads
        (its exported variables) and what it said was missing, so the operator
        supplies one route, not all of them.
        """
        writer = McpWriter(self.ctx)
        routes: list[str] = []
        for candidate in sorted(tried, key=lambda c: c.label):
            variables = writer.exported_variables(candidate)
            said = candidate.handshake.login_detail if candidate.handshake else ""
            routes.append(
                f"{candidate.label}: "
                + (f"export {', '.join(variables)}" if variables else "supply its login")
                + (f" ({said})" if said else "")
            )
        self.report_step(
            result,
            f"{outcome.target} :: your login",
            Status.PARTIAL,
            f"nothing on this machine can log in to {host}: every route this run tried had no credential to send "
            f"({len(tried)} server(s))",
            group=outcome.target,
            data={"servers": sorted({c.label for c in tried}), "routes": routes},
            remediation=[
                f"No login was sent to {host} -- not refused, missing. Supply any one of these routes in the shell "
                "you start Claude Code from, then re-run phase 4 for it: " + "; ".join(routes)
            ],
        )

    def _report_address_facts(self, result: PhaseResult, writer: McpWriter | None, outcome: TargetOutcome) -> None:
        """What every address is told, whatever was written: its file entry, identification, a login refused every way.

        These ran only for an address that ended with nothing written; an
        address with an entry written but no login that works -- build.example.net,
        sd.example.com -- was never told its login was refused every way.
        """
        writer = writer or McpWriter(self.ctx)
        problems = writer.missing_from_file(outcome)
        if problems:
            missing = [p for p in problems if "has no entry" in p or "failed" in p]
            self.report_step(
                result,
                f"{outcome.target} :: in {writer.display_path}",
                Status.FAILED if missing else Status.CHANGED,
                "; ".join(problems),
                group=outcome.target,
                data={"problems": problems},
                remediation=[f"{p}. Re-run phase 4 for this address after fixing what is named." for p in missing],
            )
        if outcome.identification_undecided:
            host = outcome.target.split("://", 1)[-1].split("/", 1)[0]
            self.report_step(
                result,
                f"{outcome.target} :: identification",
                Status.PARTIAL,
                f"{outcome.identification_undecided}; what it is (product, logins, APIs) stays undecided",
                group=outcome.target,
                data={"identification_undecided": outcome.identification_undecided},
                remediation=[
                    f"{outcome.identification_undecided}, so this run could not tell what it is or which of its APIs "
                    f"your account can use, and settled nothing on an unproven server. Re-run phase 4 for {host} "
                    f"once it answers normally: initialize-claude-ai run --phases 4 --mcp-server {host}"
                ],
            )
        self._report_refused_every_way(result, outcome)

    def _report_existing_left(self, result: PhaseResult, writer: McpWriter, outcome: TargetOutcome) -> None:
        """A registration this address already had, that did not start today, left as it was.

        Claude Code still loads it and shows it failing; the run reports it,
        with where it lives and what to do, rather than leaving the operator to
        match a red line in ``claude mcp list`` to nothing in the report.
        """
        from ..mcp_auto_discovery.existing import PROJECT_FILE

        if not outcome.kept_existing and outcome.answered_existing and not outcome.registered:
            # Nothing found or written, yet the machine holds an entry that
            # answered: said, and recorded in the file when it works.
            writer._keep_existing(outcome, [])
        retired = {s.get("name") for s in outcome.stale}
        for broken in outcome.broken_existing:
            name = broken.get("name", "")
            if name in retired or name in outcome.registered:
                continue
            where = writer.display_path if broken.get("source") == PROJECT_FILE else "the claude CLI"
            self.report_step(
                result,
                f"{outcome.target} :: existing {name}",
                Status.PARTIAL,
                (
                    f"left in {where}; it started, but did not do its job this run: {broken.get('error', '')}"
                    if broken.get("started")
                    else f"left in {where}, and it does not start: {broken.get('error', '')}"
                ),
                group=outcome.target,
                data=dict(broken),
                remediation=[
                    (
                        f"{name} in {where} started but did not do its job during this run "
                        f"({broken.get('error', '')}). If that was the service answering for a moment in "
                        f"another form, re-check it: {self._recheck_command(outcome.target)}. Otherwise fix what "
                        "it names, or "
                        if broken.get("started")
                        else f"{name} in {where} did not start during this run ({broken.get('error', '')}). "
                        "Claude Code still loads it and reports it failing. Fix what it names, or "
                    )
                    + (
                        f"delete it from {writer.display_path}."
                        if broken.get("source") == PROJECT_FILE
                        else f"remove it: {writer.remove_by_hand(name)}."
                    )
                ],
            )

    @staticmethod
    def _settle_found(outcome: TargetOutcome, registrations: Sequence[Any]) -> None:
        """Keep, as the address's finds, only what was actually written.

        ``succeeded`` reads ``verified``, and so do the summary, the safety
        verdict and the final verdict. A candidate every gate refused stayed
        there, so an address with nothing written could still be counted as
        working and the run end in SUCCESS. Refused candidates move to
        ``refused``, where the report keeps them.
        """
        written = {id(r.candidate) for r in registrations if not r.refused}
        for registration in registrations:
            if registration.refused and all(c is not registration.candidate for c in outcome.refused):
                outcome.refused.append(registration.candidate)
        outcome.verified = [c for c in outcome.verified if id(c) in written]
        outcome.provisional = [c for c in outcome.provisional if id(c) in written]

    def _retire_stale(
        self,
        result: PhaseResult,
        writer: McpWriter,
        outcome: TargetOutcome,
        others: Sequence[str] = (),
    ) -> None:
        """Report, and retire, existing registrations proved not to be this address's.

        See :meth:`McpWriter.retire`. One line each, so an entry that disappears
        from ``.mcp.json`` or ``claude mcp list`` is never a surprise. Statuses
        follow every other change the phase makes: CHANGED when it was removed,
        OK when a dry run only says it would be, and PARTIAL when it was left in
        place -- something wrong is still loaded, and the operator has to act.
        """
        statuses = {"removed": Status.CHANGED, "would be removed": Status.OK}
        for stale in writer.retire(outcome, others):
            name = stale.get("name", "")
            action = stale.get("action", "")
            self.report_step(
                result,
                f"{outcome.target} :: retire {name}",
                statuses.get(stale.get("result", ""), Status.PARTIAL),
                f"{action} -- not this address's server: {stale.get('reason', '')}",
                group=outcome.target,
                data=dict(stale),
                remediation=[stale["remediation"]] if stale.get("remediation") else [],
            )

    def _settle_withdrawals(
        self,
        result: PhaseResult,
        writer: McpWriter,
        outcomes: Sequence[TargetOutcome],
        groups: Mapping[str, str],
    ) -> None:
        """Report entries withdrawn from earlier addresses, and re-judge those addresses.

        See :meth:`McpWriter._withdraw`. The address keeps whatever else it
        registered; if the withdrawn entry was its only working one, it is no
        longer called a success.
        """
        for registration in writer.take_withdrawn():
            self.report_step(
                result,
                f"{registration.target} :: withdrew {registration.name}",
                Status.SKIPPED,
                f"withdrawn -- {registration.refused}",
                group=registration.target,
                data=registration.to_dict(),
                remediation=registration.remediation,
            )
            outcome = next((o for o in outcomes if o.target == registration.target), None)
            if outcome is None:
                continue
            outcome.registered = [n for n in outcome.registered if n != registration.name]
            mine = [r for r in writer.published if r.target == outcome.target]
            self._settle_found(outcome, mine)
            kept = [r for r in mine if not r.refused]
            if not kept:
                outcome.status = Status.PARTIAL
                outcome.detail = "found a server, registered none: " + "; ".join(
                    f"{r.name} -- {r.refused}" for r in mine
                )
            elif all(r.provisional for r in kept):
                outcome.status = Status.PARTIAL
                outcome.detail = (
                    f"{registration.name} withdrawn; still registered, waiting on a credential: "
                    + ", ".join(r.name for r in kept)
                )
            else:
                continue
            result.declare_group(groups.get(outcome.target, outcome.target), self._verdict_on(outcome), outcome.detail)

    @staticmethod
    def _deduplicate(candidates: Sequence[Candidate]) -> list[Candidate]:
        """One entry per server, however many paths or stages found it.

        The same endpoint can be reached twice -- probed once per transport, or
        found again by a later stage -- and with --the-best-mcp nothing stops at
        the first hit any more, so both copies travel this far. Registering one
        server twice under two generated names is never what was wanted, and the
        same URL on two rows of a scorecard is not a comparison. The extra
        sighting is kept as a note on the survivor rather than thrown away.
        """
        unique: dict[str, Candidate] = {}
        for candidate in candidates:
            first = unique.get(candidate.identity())
            if first is None:
                unique[candidate.identity()] = candidate
                continue
            seen_by = f"the {candidate.origin} stage" if candidate.origin else "another stage"
            source = f" ({candidate.source})" if candidate.source else ""
            first.add_note(f"found again by {seen_by}{source}")
        return list(unique.values())

    # -- proving it, per endpoint -----------------------------------------------

    def _report_verification(
        self,
        result: PhaseResult,
        writer: McpWriter,
        outcome: TargetOutcome,
        registrations: Sequence[Registration],
    ) -> None:
        """Every endpoint of this address, and every check it had to pass.

        The stage lines above say a server was found and the register lines below
        say where it went; neither says *what was proven*. This does, one row per
        endpoint and one column per check, so "verified" can be read rather than
        taken on faith -- and so a server that Claude Code itself cannot connect
        to is visible here rather than at the start of someone's next session.
        """
        # A refused entry was never written, so there is nothing to verify and
        # nothing for `claude mcp get` to find. Including it would put a row of
        # failures in this table for a decision the program made on purpose.
        registrations = [r for r in registrations if not r.refused]
        if not registrations:
            return

        rows: list[list[str]] = []
        passed = 0
        proven = [r for r in registrations if not r.provisional]
        awaiting = [r for r in registrations if r.provisional]
        self.console.write("")
        for registration in registrations:
            checks = CandidateVerifier.audit(registration.candidate)
            # Whether the *entry* starts, which every other row judges the
            # candidate it came from. Three servers passed all of those and
            # died on start-up, so this one goes in the table rather than only
            # into the remediation -- a failure the operator has to go looking
            # for is a failure the report did not tell them about.
            checks.append(writer.start_check_row(registration))
            # Claude Code's own verdict, which is the one check this program
            # does not perform with its own client.
            # Acted on, not just rendered: a "failed to connect" from the real
            # client is a finding about this server, and it used to reach the
            # table and stop there. See McpWriter.absorb_claude_check.
            writer.absorb_claude_check(registration, writer.claude_check(registration))
            checks.append(registration.claude_check)
            # An endpoint waiting on a credential passes nothing and fails
            # nothing: every check is "-", and counting that as a pass would
            # make the tally say a server works when nobody has asked it to.
            if not registration.provisional and all(check.ok is not False for check in checks):
                passed += 1
            # By name, not by position. `audit` returns a different number of
            # rows depending on what happened -- a server that handshaked
            # without its credential gets an extra `usable` row, one that was
            # never probed gets a single `connected` row -- so laying them out
            # in order put marks under whichever heading happened to be next.
            # A check this server has no row for is "-", the same as one that
            # could not be performed.
            marks = {check.name: check.mark for check in checks}
            rows.append(
                [
                    registration.name,
                    registration.candidate.label,
                    *(marks.get(name, "-") for name in CHECKS),
                ]
            )
            outcome.verification.append(
                {
                    "name": registration.name,
                    "endpoint": registration.candidate.label,
                    "checks": [check.to_dict() for check in checks],
                    "provisional": registration.provisional,
                    "needs_environment": registration.needs_env,
                }
            )

        self.console.table(rows, headers=["name", "endpoint", *CHECKS])
        for registration, entry in zip(registrations, outcome.verification):
            checks = entry["checks"]
            self.console.bullet(f"{registration.name} -- {checks[0]['detail']}", indent=4)
            for check in checks[1:]:
                self.console.bullet(f"{check['check']}: {check['detail']}", indent=6)

        detail = (
            f"{passed}/{len(proven)} endpoint(s) are connected, expose something callable "
            "and answered a request after the handshake"
            if proven
            else ""
        )
        refused = [r for r in awaiting if _login_refused(r.candidate)]
        # Only one with something left to set is "written out with the slot
        # empty"; one whose credential is in place but unproved is neither.
        empty = [r for r in awaiting if r not in refused and r.needs_env]
        if empty:
            said = (
                f"{len(empty)} endpoint(s) answered only to say a credential is required, "
                f"and {'are' if len(empty) > 1 else 'is'} written out with the slot empty"
            )
            detail = f"{detail}; {said}" if detail else said
        if refused:
            said = (
                f"{len(refused)} endpoint(s) started, but their service refused the credential supplied "
                "(or its account has no API access)"
            )
            detail = f"{detail}; {said}" if detail else said
        self.report_step(
            result,
            f"{outcome.target} :: verification",
            Status.OK if passed == len(proven) and not awaiting else Status.PARTIAL,
            detail,
            group=outcome.target,
            data={"verification": outcome.verification},
            remediation=[
                # Re-run, not restart. For a local server the variable is named
                # here rather than written into the entry -- the launch that was
                # proven did not carry it -- so the next run is what trials the
                # server with it and writes the slot. A remote entry has the
                # placeholder already and a restart is enough; saying the
                # stronger thing is right for both.
                *(
                    f"{r.name}: export {', '.join(r.needs_env)} with a value from the service "
                    "itself, then re-run phase 4."
                    for r in empty
                ),
                *(f"{r.name}: {r.credential_hint}" for r in refused),
            ],
        )

    def _recheck(self, outcome: TargetOutcome) -> list[Candidate]:
        """A server that has gone away between discovery and now is not a result."""
        verifier = CandidateVerifier(self.ctx)
        return verifier.recheck(outcome.verified)

    def _rejudge_stale(self, result: PhaseResult, outcome: TargetOutcome) -> None:
        """Judge again, under what the address is now known to be, every verdict reached before it was known.

        One standard per server per run: gitlab.example.net's mcp-graphql was
        verified before the address was identified as a GitLab, and the re-check
        then held it to GitLab's own task. The second verdict is reached here,
        before anything is chosen, and sorts the server into working, waiting for
        a credential, or not this address's answer.
        (``mcp.verification.rejudge_stale_verdicts``.)
        """
        if not self.settings.bool_("mcp.verification.rejudge_stale_verdicts", True):
            return
        verifier = CandidateVerifier(self.ctx)
        stale = verifier.stale_verdicts([*outcome.verified, *outcome.provisional])
        if not stale:
            return
        for candidate in stale:
            verifier.verify(candidate)
        lines = []
        for candidate in stale:
            if candidate in outcome.verified and not candidate.verified:
                outcome.verified.remove(candidate)
                if candidate.needs_credential:
                    outcome.provisional.append(candidate)
                    lines.append(f"{candidate.label} waits for a credential under what the address is")
                else:
                    lines.append(f"{candidate.label} no longer passes: {self._why_not(candidate)}")
            elif candidate in outcome.provisional and candidate.verified:
                outcome.provisional.remove(candidate)
                outcome.verified.append(candidate)
                lines.append(f"{candidate.label} works under what the address is")
        if lines:
            self.report_step(
                result,
                f"{outcome.target} :: judged again",
                Status.CHANGED,
                "what the address is became known after these were judged: " + "; ".join(lines),
                group=outcome.target,
            )

    def _final_recheck(self, result: PhaseResult, outcome: TargetOutcome) -> None:
        """Is each verified server still what was proved? Working ones stay, waiting ones wait, gone ones go -- said as such."""
        verifier = CandidateVerifier(self.ctx)
        found = verifier.recheck_all(outcome.verified)
        outcome.verified = found.working
        for candidate in found.waiting:
            if candidate not in outcome.provisional:
                outcome.provisional.append(candidate)
        if not found.waiting and not found.gone:
            return
        parts = [f"{c.label}: {why}" for c, why in found.gone]
        parts += [f"{c.label}: it answers, and waits for a credential" for c in found.waiting]
        self.report_step(
            result,
            f"{outcome.target} :: re-check",
            Status.PARTIAL if (outcome.verified or outcome.provisional) else Status.FAILED,
            "; ".join(parts),
            group=outcome.target,
            remediation=[f"Re-check once the server is back: {self._recheck_command(outcome.target)}"]
            if found.gone
            else [],
        )

    @staticmethod
    def _why_not(candidate: Candidate) -> str:
        shake = candidate.handshake
        if shake is None:
            return "no handshake"
        failed = next((r for r in shake.contract or [] if r.get("passed") is False), None)
        if failed:
            return f"usage task {failed.get('task')!r} failed ({failed.get('detail') or 'no detail'})"
        return shake.error or "it did not verify"

    def _hand_over(self, result: PhaseResult, outcome: TargetOutcome, why: str) -> bool:
        """The settling server was withdrawn: reopen what it had closed, and say what came of it. Whether anything did.

        In order (``[mcp.handover]``): another server proved for the address
        (already kept), one waiting for a credential (kept until the address
        is settled), then the stages skipped only because the withdrawn server
        settled the address -- worked as any stage is, existing registrations
        first, so a bridge that worked here before is tried before anything
        new is built. The final file check repairs from existing entries last
        (:meth:`.McpWriter.missing_from_file`). Never more than
        ``max_rounds`` per address, so a hand-over cannot loop.
        """
        if not self.settings.bool_("mcp.handover.enabled", True):
            return False
        if outcome.handover_rounds >= max(0, self.settings.int_("mcp.handover.max_rounds", 1)):
            return False
        pipeline = getattr(self, "_ran_with", None)
        reopen = getattr(pipeline, "reopen_withdrawn", None)
        if reopen is None:
            return False
        outcome.handover_rounds += 1
        before = len(outcome.stages)
        try:
            names = reopen(outcome)
        except Exception as exc:  # noqa: BLE001 - a hand-over must not kill the phase
            log.exception("%s: reopening after a withdrawal failed", outcome.target)
            self.report_step(
                result,
                f"{outcome.target} :: hand-over",
                Status.FAILED,
                f"{why}; reopening the skipped stages failed: {exc}",
                group=outcome.target,
            )
            return False
        for stage in outcome.stages[before:]:
            self._report_stage(result, outcome.target, stage)
        found = [*outcome.verified, *outcome.provisional]
        if not names:
            return False
        self.report_step(
            result,
            f"{outcome.target} :: hand-over",
            Status.CHANGED if found else Status.FAILED,
            f"{why}; reopened {', '.join(names)}, which had been skipped because the address was settled -- "
            + (f"found {', '.join(c.label for c in found)}" if found else "nothing answered there either"),
            group=outcome.target,
        )
        return bool(found)

    # -- keeping all of them ---------------------------------------------------

    def _report_inventory(self, result: PhaseResult, outcome: TargetOutcome) -> None:
        """What the exhaustive search came back with, before any of it is written.

        The register steps below say one line each, but they say it about a
        generated name; this says it about the address that was asked for, and
        it is the line that makes "the search really did keep going" checkable
        against the one endpoint an ordinary run would have stopped at.
        """
        found = outcome.verified
        self.console.write("")
        for candidate in found:
            where = f" (found by the {candidate.origin} stage)" if candidate.origin else ""
            summary = candidate.handshake.summary() if candidate.handshake else ""
            self.console.bullet(f"{candidate.label}{where} -- {summary}", indent=4)
        self.report_step(
            result,
            f"{outcome.target} :: {len(found)} working endpoint(s)",
            Status.OK,
            "every endpoint that answered is being recorded (--exhaustive-mcp)"
            if len(found) > 1
            else "one endpoint answered; the search ran to the end anyway (--exhaustive-mcp)",
            group=outcome.target,
            data={
                "exhaustive": True,
                "endpoints": [c.label for c in found],
                "origins": [c.origin for c in found],
            },
        )

    # -- picking the best one --------------------------------------------------

    def _choose_best(self, result: PhaseResult, outcome: TargetOutcome) -> None:
        """Score everything that answered and keep one, on the record."""
        if len(outcome.verified) < 2:
            # One server is not a shortlist. Reporting it as "the best" would
            # dress a default up as a decision.
            if outcome.verified:
                self.report_step(
                    result,
                    f"{outcome.target} :: best of 1",
                    Status.OK,
                    f"{outcome.verified[0].label} was the only server that answered, "
                    "so there was nothing to choose between",
                    group=outcome.target,
                    data={"pick_best": True, "candidates": 1},
                )
            return

        ranker = CandidateRanker(self.ctx)
        scores = ranker.rank(outcome.verified, target=outcome.base_url or outcome.target)
        outcome.ranking = [s.to_dict() for s in scores]
        winner, *runners_up = scores
        outcome.verified = [winner.candidate]

        self._report_ranking(scores)
        self.report_step(
            result,
            f"{outcome.target} :: best of {len(scores)}",
            Status.OK,
            f"{winner.label} scored {winner.total:.2f}/1.00 -- {winner.headline()}",
            group=outcome.target,
            data={
                "pick_best": True,
                "winner": winner.to_dict(),
                "runners_up": [s.to_dict() for s in runners_up],
            },
        )

    def _report_ranking(self, scores: Sequence[Score]) -> None:
        """The whole scorecard, so the winner can be disagreed with.

        A single number decides the outcome, so showing only that number would
        be asking to be trusted. The per-aspect columns are what let someone see
        *why* -- and which weight in [mcp.ranking] to change if the ordering is
        wrong for their environment.
        """
        headers = ["server", "score", *(_ASPECT_COLUMNS.get(a, a) for a in ASPECTS)]
        rows = [
            [
                score.label,
                f"{score.total:.2f}",
                *(f"{score.aspects.get(aspect, 0.0):.2f}" for aspect in ASPECTS),
            ]
            for score in scores
        ]
        self.console.write("")
        self.console.table(rows, headers=headers)
        for score in scores[1:]:
            # Why it lost, not what it is: the runners-up share most of their
            # strengths with the winner, so repeating those explains nothing.
            self.console.bullet(
                f"set aside: {score.label} ({score.total:.2f}) -- {score.shortfall(2) or score.headline(2)}",
                indent=4,
            )
        self.console.bullet(
            "run without --the-best-mcp to register every server that answered, or retune the weights in [mcp.ranking]",
            indent=4,
        )

    # -- summary ---------------------------------------------------------------

    @staticmethod
    def _modes_of(outcome: TargetOutcome) -> str:
        """The modes this address's stages ran in, in the order they ran."""
        labels: list[str] = []
        for stage in outcome.stages:
            if _never_ran(stage):
                continue
            label, _styles = mode_tag(stage.mode)
            if label not in labels:
                labels.append(label)
        return ", ".join(labels) or "-"

    @staticmethod
    def _refusals(outcomes: list[TargetOutcome]) -> int:
        """Stages whose agent request was rejected before a model read it.

        A refusal is the only agent failure that costs the stage outright: the
        other three classes mean the agent worked and came up short, which the
        fallback then completes. This one means no agent worked at all, and it
        was only ever visible by grepping the log.
        """
        return sum(
            1
            for outcome in outcomes
            for stage in outcome.stages
            if str(stage.findings.get("agent_failure_kind", "")) == REFUSAL
        )

    def _agent_unavailable_lines(self) -> list[str]:
        """The addresses an agent should have worked and did not, and how to redo them.

        Worked out from what the agentic pipeline recorded, so it names only
        stages that were denied an agent by the runtime -- not ones the tier
        policy closed, and not a run where agents were off by choice.
        """
        ran_with = getattr(self, "_ran_with", None)
        unusable = str(getattr(ran_with, "_runtime_unusable", "") or "")
        affected = getattr(ran_with, "agent_unavailable", {}) or {}
        if not unusable or not affected:
            return []
        from ..cli import PROGRAM  # the CLI imports this phase; import late

        lines = [
            "Agent unavailable ("
            + unusable
            + ") for "
            + "; ".join(f"{address} ({', '.join(stages)})" for address, stages in affected.items())
            + ". Those stages were worked deterministically."
        ]
        remedy = str(getattr(ran_with, "runtime_remedy", "") or "")
        if remedy:
            lines.append(f"Fix the agent's credential first: {remedy}")
        lines.append(
            f"Then re-run those addresses with the agent: {PROGRAM} run --phases 4 --mcp-server " + ",".join(affected)
        )
        return lines

    @staticmethod
    def _mode_tally(outcomes: list[TargetOutcome]) -> dict[str, int]:
        """How many stages ran in each mode, across every address.

        Stages that never ran are counted apart, under ``skipped``. Counting
        them by mode made this number untrue in the direction that matters: a
        run where the agent drove every executed stage reported five
        "traditional" ones, because the tier policy had skipped five and the
        skip carried the mode of whichever pipeline owned the rule. The one
        figure that says how much the agent did should not include work nobody
        did.
        """
        tally: dict[str, int] = {}
        for outcome in outcomes:
            for stage in outcome.stages:
                label = "skipped" if _never_ran(stage) else mode_tag(stage.mode)[0]
                tally[label] = tally.get(label, 0) + 1
        return tally

    @staticmethod
    def _examined(outcome: TargetOutcome) -> int:
        """Distinct candidates this address produced, working or not.

        Not every path probed -- there are hundreds of those and a 404 on
        /gateway/mcp is not a finding -- but everything that answered with
        something worth looking at: a handshake, an authentication wall, a
        server error. The gap between this and the working count is where the
        near-misses are, and "0" says no stage ever got that far, which is a
        different problem needing a different fix.
        """
        return len({c.identity() for stage in outcome.stages for c in stage.candidates})

    def _ensure_detail(self, result: PhaseResult, writer: McpWriter | None, outcome: TargetOutcome) -> str:
        """The address's reason, never empty: gitlab.example.net ended "FAILED -- no detail recorded".

        A path that set none (an early return, a step added later) gets the
        last step of the address that did not pass, else what the file holds
        for it (:meth:`_nothing_registered`).
        """
        if str(outcome.detail or "").strip():
            return outcome.detail
        failing = [
            step
            for step in result.steps
            if step.group == outcome.target and step.status in (Status.FAILED, Status.PARTIAL) and step.detail
        ]
        if failing:
            step = failing[-1]
            outcome.detail = (
                f"{step.name.split(' :: ', 1)[-1]}: {step.detail}" if getattr(step, "name", "") else step.detail
            )
        else:
            outcome.detail = self._nothing_registered(
                writer or McpWriter(self.ctx), outcome, "no step of this address said why"
            )
        return outcome.detail

    @staticmethod
    def _verdict_on(outcome: TargetOutcome) -> str:
        """The answer to the one question asked of an address.

        ``outcome.status`` is the roll-up of the stages, which is the right
        reading of the *search* -- three stages skipped and one partly useful.
        It is the wrong reading of the *address*: the question was "is there a
        working MCP server here", and "no" is not a partial pass, however
        productively the stages failed to find one. An endpoint that answered
        but wants a credential is genuinely partial, so it keeps the roll-up --
        and so does one that found a server every gate then refused, or that
        still has a registration from before the run that answered today.
        """
        if not (outcome.verified or outcome.provisional or outcome.refused or outcome.kept_existing):
            return Status.FAILED
        return outcome.status

    @staticmethod
    def _address_count(result: PhaseResult, outcomes: Sequence[TargetOutcome]) -> int:
        """Every address asked for, including any that never reached discovery.

        An address that could not be parsed has no :class:`TargetOutcome`, but
        it is still one of the servers the operator named and still one that
        produced nothing. Every count in this phase is out of this number, so
        the summary, the verdict and the safety note cannot end up quoting
        three different denominators for the same run.
        """
        named = {o.target for o in outcomes}
        return len(outcomes) + sum(1 for g in result.groups() if g.key not in named)

    def _note_toolchains(self, outcome: TargetOutcome, registrations: Sequence[Registration]) -> None:
        """Remember which catalog toolchain each registered local server runs with.

        Display only: read after the choice was made, from what was chosen.
        """
        provisioner = Provisioner.for_run(self.ctx)
        needs = self.__dict__.setdefault("_toolchain_needs", [])
        for registration in registrations:
            if registration.refused or not (registration.registered or registration.written):
                continue
            command = registration.candidate.full_command() if registration.candidate.kind == "stdio" else []
            component = provisioner.provider(command[0]) if command else None
            if component is not None:
                needs.append(
                    {
                        "address": outcome.target,
                        "server": registration.name,
                        "command": command[0],
                        "component": component.key,
                    }
                )

    def _report_toolchains(self, result: PhaseResult) -> None:
        """What --mcp-provision-missing did, and what the registered servers run with.

        One step for the run, since a toolchain is installed once and shared by
        every address. Never a failure: a toolchain that could not be installed
        cost a candidate, and that candidate's own verdict already says so in
        the words it always used -- this is the why behind it.
        """
        records = Provisioner.for_run(self.ctx).records
        needs = list(self.__dict__.get("_toolchain_needs", []))
        if not records and not needs:
            return
        installed = [r for r in records if r.status == INSTALLED]
        failed = [r for r in records if r.status == FAILED]
        others = [r for r in records if r.status not in (INSTALLED, FAILED)]
        parts: list[str] = []
        if installed:
            parts.append(
                "installed on demand: "
                + ", ".join(f"{r.component} (for {r.command}, {format_duration(r.duration)})" for r in installed)
            )
        if failed:
            parts.append("could not install: " + ", ".join(f"{r.component} ({r.detail})" for r in failed))
        if others:
            parts.append("not installed: " + ", ".join(f"{r.component} ({r.detail})" for r in others))
        if needs:
            parts.append(
                "registered servers run with: "
                + ", ".join(f"{n['server']} -> {n['component']} ({n['command']})" for n in needs)
            )
        remediation: list[str] = []
        for record in (*failed, *others):
            who = " / ".join(x for x in (record.address, record.server) if x)
            remediation.append(
                f"{record.component} was needed as the {record.reason} "
                f"{record.command}{f' by {who}' if who else ''}: {record.detail}"
            )
            remediation.extend(f"  tried {line}" for line in record.attempts)
            remediation.extend(line for line in record.remediation if line not in remediation)
        self.report_step(
            result,
            "toolchains",
            Status.CHANGED if installed else Status.OK,
            "; ".join(parts),
            data={
                "provision_missing": Provisioner.for_run(self.ctx).mode,
                "provisioned": [r.to_dict() for r in records],
                "registered_needs": needs,
            },
            remediation=remediation,
        )

    def _report_summary(self, result: PhaseResult, outcomes: list[TargetOutcome]) -> None:
        """One row per address given to --mcp-server: what came of each of them."""
        working = [o for o in outcomes if o.succeeded]
        total = self._address_count(result, outcomes)
        rows = [
            [
                o.target,
                o.status,
                self._modes_of(o),
                str(self._examined(o)),
                str(len(o.verified)),
                str(len(o.provisional)) if o.provisional else "-",
                ", ".join(o.registered) or "-",
            ]
            for o in outcomes
        ]
        self.console.write("")
        # The mode gets a column of its own: the same result means something
        # different depending on who produced it, and "nothing found" is a much
        # weaker statement when no agent was able to look. "needs token" gets
        # one for the same reason: an endpoint that exists but wants a
        # credential is a different outcome from an address with nothing on it.
        self.console.table(
            rows,
            headers=[
                "address",
                "status",
                "ran as",
                "candidates",
                "working",
                "needs token",
                "registered as",
            ],
        )

        result.data["servers"] = [o.to_dict() for o in outcomes]
        result.data["modes"] = self._mode_tally(outcomes)
        result.data["agent_refusals"] = self._refusals(outcomes)
        self.ctx.state["mcp_outcomes"] = outcomes

        tally = ", ".join(f"{count} {mode}" for mode, count in self._mode_tally(outcomes).items())
        detail = f"{len(working)}/{total} address(es) have a working MCP server"
        refused = self._refusals(outcomes)
        if refused:
            # The one failure that costs agent coverage outright: the request was
            # rejected before a model read it, so the stage finished
            # deterministically without an agent ever having worked on it. It was
            # visible only by grepping the log, which is not visible.
            detail += (
                f". {refused} agent request(s) were rejected before a model read them, so "
                "those stages fell back without the agent having worked on them -- set "
                "mcp.agent.retry_models to give the retry somewhere else to go"
            )
        ran_with = getattr(self, "_ran_with", None)
        unusable = str(getattr(ran_with, "_runtime_unusable", "") or "")
        if unusable and getattr(ran_with, "agent_answered", False):
            # Not the same run as the one below: agents did work some stages
            # before the runtime stopped serving. Saying "never ran" would hide
            # what they found; saying nothing would hide what they did not.
            affected = getattr(ran_with, "agent_unavailable", {}) or {}
            detail += (
                f". THE AGENT STOPPED PART-WAY: {unusable}. From then on "
                f"{len(affected)} address(es) were worked by the deterministic pipeline alone; "
                "the final verdict lists them with the command to re-run just those"
            )
        elif unusable:
            # One conclusion, not one note per stage. The condition is about the
            # installation, so it applied to every target equally and the whole
            # run is degraded -- which is the single most important thing this
            # verdict can say, and it used to be sayable only by reading 29
            # identical per-stage notes in the log.
            detail += (
                f". THE AGENT NEVER RAN: {unusable}. Every address was worked by the "
                "deterministic pipeline alone, so this run found less than an agentic one "
                "would have; fixing that and re-running phase 4 is worth more than anything "
                "else here"
            )
        # "More" than the working ones: an address counted above is not counted
        # again, and only what was written counts at all (see _settle_found).
        pending = [o for o in outcomes if o.provisional and not o.succeeded]
        if pending:
            detail += f", {len(pending)} more have a server registered and waiting on a credential"
        nothing = [o for o in outcomes if not o.registered and not o.succeeded]
        if nothing:
            detail += f", {len(nothing)} have nothing registered (" + ", ".join(o.target for o in nothing) + ")"
        if tally:
            # Carried into the summary box, where the stage lines and their
            # coloured mode tags have already scrolled away.
            detail = f"{detail}  {self.glyph('sep')}  stages ran: {tally}"
        # Lines the stage steps already contributed; repeating them, merely
        # prefixed with the target, would pad the summary without adding anything.
        seen = {line for step in result.steps for line in step.remediation}
        remediation: list[str] = []
        for outcome in outcomes:
            if outcome.succeeded:
                continue
            for item in outcome.playbook[:2]:
                if item in seen:
                    continue
                seen.add(item)
                remediation.append(f"{outcome.target}: {item}" if len(outcomes) > 1 else item)
        self.report_step(
            result,
            "discovery",
            Status.OK if working and len(working) == total else Status.PARTIAL,
            detail,
            remediation=remediation,
        )

    def _report_file_not_written(self, result: PhaseResult, writer: McpWriter | None) -> None:
        """The project file could not be written: said beside the verdict, with why (a full disk, a read-only directory)."""
        if writer is None or not writer.write_failures:
            return
        from ..core import diskspace

        full = any(diskspace.is_out_of_space(r, self.ctx.settings) for r in writer.write_failures)
        self.report_step(
            result,
            f"{writer.display_path} not written",
            Status.FAILED,
            "; ".join(writer.write_failures[:3]),
            remediation=(diskspace.remediation(self.ctx, diskspace.check(self.ctx)) if full else [])
            + [f"Fix that, then re-run phase 4: nothing found in this run reached {writer.display_path}."],
        )

    def _report_checked_as_written(
        self, result: PhaseResult, writer: McpWriter | None, outcomes: list[TargetOutcome]
    ) -> None:
        """Every entry this run wrote or kept, read back from the file and the CLI and repaired (:meth:`McpWriter.verify_written`)."""
        if writer is None:
            return
        names = self._names_of_the_run(writer, outcomes)
        rows = writer.verify_written(names)
        if not rows:
            return
        # What was put right, and what only the operator can -- said apart.
        open_rows = [r for r in rows if r.get("repaired") is False]
        fixed = [r for r in rows if r.get("repaired") is not False]
        words = []
        if fixed:
            words.append(
                f"{len(fixed)} entr(y/ies) repaired after writing: "
                + "; ".join(f"{r['name']} in {r['where']}: {r['change']}" for r in fixed[:4])
            )
        if open_rows:
            words.append(
                f"{len(open_rows)} still to fix: "
                + "; ".join(f"{r['name']} in {r['where']}: {r['change']}" for r in open_rows[:4])
            )
        self.report_step(
            result,
            "checked as written",
            Status.PARTIAL if open_rows else Status.CHANGED,
            "; ".join(words),
            data={"repaired_after_writing": fixed, "still_to_fix": open_rows},
            remediation=[
                f"{r['name']} ({r['where']}): {r['change']}"
                for r in rows
                if r in open_rows or "remove it" in r["change"] or "failed" in r["change"] or "could not" in r["change"]
            ],
        )

    def _report_what_travels(self, result: PhaseResult, writer: McpWriter | None) -> None:
        """What a copied ``.mcp.json`` needs beside it, and what each machine supplies itself (:meth:`McpWriter.travelling_files`).

        The operator copies the file between machines; its entries name
        folders beside it (the servers' home, bridges, trust bundles) and
        files of this machine. Said once, so nothing is left behind.
        (``mcp.registration.report_what_travels``.)
        """
        if writer is None or not self.settings.bool_("mcp.registration.report_what_travels", True):
            return
        beside, machine = writer.travelling_files()
        if not beside and not machine:
            return
        words = []
        if beside:
            words.append(f"copy with {writer.display_path}: " + ", ".join(beside))
        if machine:
            words.append(
                "supplied by each machine (run phase 1 there, or export the variable): "
                + "; ".join(f"{path} ({', '.join(names) or 'no variable'})" for path, names in machine.items())
            )
        self.report_step(
            result,
            "travels with the file",
            Status.OK,
            "; ".join(words),
            data={"beside": beside, "machine": {k: list(v) for k, v in machine.items()}},
        )

    def _reconcile_with_file(self, writer: McpWriter, outcome: TargetOutcome) -> None:
        """What the address has is what ``.mcp.json`` holds for it -- read back, not this run's list.

        build-example-net and build-opensuse-org were kept, working, and in the
        file; the verdict still said "Not in ./.mcp.json" because it counted
        only what this run registered. Every view reads ``outcome.registered``,
        so the file's entries for the address -- the kept and answered ones, or
        the address's own name -- are added there once publishing is done.
        (``mcp.registration.verdict_reads_file``.)
        """
        if not self.settings.bool_("mcp.registration.verdict_reads_file", True) or writer is None:
            return
        in_file = writer._existing_entries()
        if not in_file:
            return
        mine = {
            *(str(k.get("name") or "") for k in outcome.kept_existing),
            *outcome.answered_existing,
            writer.base_name(outcome.target),
        } - {""}
        found = [n for n in sorted(mine) if n in in_file and n not in outcome.registered]
        if not found:
            return
        outcome.registered.extend(found)
        log.info(
            "%s: %s in %s -- counted as this address's entry", outcome.target, ", ".join(found), writer.display_path
        )

    def _report_moved_literals(self, result: PhaseResult, writer: McpWriter | None) -> None:
        """Credentials written into the project file as plain text, moved to where Claude Code reads them (:meth:`McpWriter.move_literal_secrets`)."""
        if writer is None:
            return
        moved, left = writer.move_literal_secrets()
        if not moved and not left:
            return
        self.report_step(
            result,
            "literal credentials",
            Status.CHANGED if moved else Status.PARTIAL,
            (
                f"{len(moved)} credential(s) moved out of {writer.display_path} into Claude Code's settings env: "
                + "; ".join(moved[:6])
                if moved
                else ""
            )
            + (("; " if moved else "") + f"{len(left)} left in place: " + "; ".join(left[:4]) if left else ""),
            data={"moved": moved, "left": left},
            remediation=[
                f"{line} -- set it in Claude Code's settings env yourself and write ${{NAME}} in the entry"
                for line in left
            ],
        )

    def _report_user_steps(self, result: PhaseResult, writer: McpWriter | None, outcomes: list[TargetOutcome]) -> None:
        """Steps only the operator can take, each in the server's own words (:meth:`McpWriter.user_steps`)."""
        if writer is None:
            return
        steps = [(o.target, step) for o in outcomes for step in writer.user_steps(o)]
        if not steps:
            return
        self.report_step(
            result,
            "steps only you can take",
            Status.PARTIAL,
            f"{len(steps)} step(s) no program can take for you: " + "; ".join(s for _, s in steps[:3]),
            data={"user_steps": [{"target": t, "step": s} for t, s in steps]},
            remediation=[f"{s}." for _, s in steps],
        )

    @staticmethod
    def _names_of_the_run(writer: McpWriter, outcomes: list[TargetOutcome]) -> set[str]:
        """Every entry this run is answerable for: written, kept, or the existing ones it judged for its addresses."""
        names = {n for o in outcomes for n in o.registered} | set(writer.address_of)
        for outcome in outcomes:
            names |= set(outcome.connected_existing) | set(outcome.answered_existing)
            names |= {str(k.get("name") or "") for k in outcome.kept_existing if isinstance(k, dict)}
            names |= {c.existing_name for stage in outcome.stages for c in stage.candidates if c.existing_name}
        return {n for n in names if n}

    def _report_what_claude_loads(
        self, result: PhaseResult, writer: McpWriter | None, outcomes: list[TargetOutcome]
    ) -> None:
        """Every written server Claude Code will not load, with the file and key that decide (:meth:`McpWriter.claude_load_check`)."""
        if writer is None:
            return
        names = self._names_of_the_run(writer, outcomes)
        rows = writer.claude_load_check(names)
        if not rows:
            return
        skipped = [r for r in rows if r["why"].startswith("Claude Code skips")]
        self.report_step(
            result,
            "what Claude Code loads",
            Status.FAILED if skipped or any("switched off" in r["why"] for r in rows) else Status.PARTIAL,
            f"{len(rows)} written server(s) Claude Code will not load as things stand: "
            + "; ".join(f"{r['name']} -- {r['why']}" for r in rows[:4]),
            data={"not_loaded": rows},
            remediation=[f"{r['name']}: {r['why']}; {r['fix']}." for r in rows],
        )

    def _report_missing_entries(self, result: PhaseResult, writer: McpWriter, outcomes: list[TargetOutcome]) -> None:
        """The end of the run, once more: every address that has a server has an entry in the file.

        Checked per address as it finishes, and again here, after every address
        has written -- a later write must not have lost an earlier one, and the
        answer belongs beside the final verdict, which is what gets read.
        """
        if writer is None:
            return
        found = [p for o in outcomes for p in writer.missing_from_file(o)]
        repaired = [p for p in found if "is recorded there now" in p]
        if repaired:
            self.report_step(
                result,
                f"entries recorded in {writer.display_path}",
                Status.CHANGED,
                f"{len(repaired)} address(es) would have ended with no entry; repaired: " + "; ".join(repaired),
                data={"repaired": repaired},
                remediation=[f"{r}." for r in repaired],
            )
        missing = [p for p in found if "has no entry" in p or "failed" in p]
        if not missing:
            return
        self.report_step(
            result,
            f"addresses missing from {writer.display_path}",
            Status.FAILED,
            f"{len(missing)} address(es) have a server but no entry in {writer.display_path}",
            data={"missing": missing},
            remediation=[f"{m}." for m in missing],
        )

    def _report_verdict(self, result: PhaseResult, writer: McpWriter, outcomes: list[TargetOutcome]) -> None:
        """The last thing phase 4 says: what happened, and one word for it.

        Everything above is per address, per stage, per check; by the time the
        page has scrolled, "did this work, and is there anything I have to do
        about it" needs answering in one place. The three things that make a
        registered server fail later -- a missing credential, a generated script
        somebody has to keep, a duplicate entry pointing at the same service --
        are repeated here even though each was already said once, because this
        is the paragraph that gets read.

        The verdict word is the last token of the detail line deliberately: it is
        what a wrapping script greps for, so it must not be followed by commentary.
        """
        working = [o for o in outcomes if o.succeeded]
        total = self._address_count(result, outcomes)
        names = [name for o in outcomes for name in o.registered]
        secrets: list[str] = []
        scripts: list[str] = []
        for outcome in outcomes:
            for candidate in outcome.verified:
                # As the entry reads them -- the exported names, which are what
                # the operator sets (see McpWriter._shared_name).
                secrets.extend(n for n in writer.exported_variables(candidate) if n not in secrets)
                scripts.extend(p for p in writer.generated_scripts(candidate) if p not in scripts)
        # Registered, written, and inert until somebody supplies a real token.
        # A refused one was never written, so it is waiting for nothing.
        awaiting = [r for r in writer.published if r.provisional and not r.refused]
        # Waiting too, but not for a missing value: the credential supplied was
        # refused by the service, or the account has no API access. "Export the
        # value" is the wrong instruction for those -- the operator did.
        refused_login = [r for r in awaiting if _login_refused(r.candidate)]
        awaiting = [r for r in awaiting if r not in refused_login]
        pending_env: list[str] = []
        for registration in awaiting:
            pending_env.extend(n for n in registration.needs_env if n not in pending_env)
        # Never asked for what Claude Code already has: a name a new terminal's
        # Claude Code gets -- its settings env, this program's env file -- is not
        # "required". BUILD_EXAMPLE_NET_USERNAME sat in settings.json env and the
        # verdict still said "export BUILD_EXAMPLE_NET_USERNAME".
        unproved_held: list[str] = []
        supplied: list[str] = []
        if self.settings.bool_("mcp.registration.skip_values_claude_has", True):
            from ..mcp_auto_discovery.claude_check import fresh_environ

            held = {k for k, v in fresh_environ(self.ctx).items() if str(v).strip()}
            supplied = [n for n in secrets if n in held]
            secrets = [n for n in secrets if n not in held]
            pending_env = [n for n in pending_env if n not in held]
            unproved_held = [
                r.name
                for r in writer.published
                if not r.refused
                and r.candidate is not None
                and (not r.candidate.verified or r.candidate.unsettled)
                and r not in refused_login
                and all(n in held for n in r.needs_env)
            ]
        duplicates = [d for o in outcomes for d in o.duplicates]
        # Registrations proved not to be their address's: retired, or -- under a
        # name that is not ours, or with retirement off -- still loaded.
        retired = [s for o in outcomes for s in o.stale if s.get("result") != "kept"]
        wrong_kept = [s for o in outcomes for s in o.stale if s.get("result") == "kept"]
        incomplete = [
            o.target for o in outcomes if isinstance(o.due_diligence, DueDiligence) and not o.due_diligence.complete
        ]

        kept_existing = [k for o in outcomes for k in o.kept_existing]
        lines = [
            f"{len(working)}/{total} address(es) yielded a server that completed an MCP "
            "handshake"
            + (
                f"; {len(names)} entr{'y' if len(names) == 1 else 'ies'} registered as {', '.join(names)}"
                if names
                else ""
            ),
        ]
        if awaiting and (pending_env or not self.settings.bool_("mcp.registration.skip_values_claude_has", True)):
            # First among the caveats, and worded as an instruction rather than
            # an observation: this is the one thing the operator must do that
            # this run could not do for them, and it is why an address that
            # looks like a failure above is in fact a lead.
            lines.append(
                "ACTION REQUIRED -- "
                + ", ".join(r.name for r in awaiting)
                + (" was" if len(awaiting) == 1 else " were")
                + " registered with an EMPTY credential. "
                + ", ".join(pending_env)
                + (" is" if len(pending_env) == 1 else " are")
                + " named but unset: each server proved it is there and is waiting for that "
                "value, and none can be used until it is set. Export the value(s) from the "
                f"service itself and re-run phase 4. Each entry in {writer.display_path} "
                "already names its variable -- ${NAME}, or ${NAME:-} where the server was "
                "proven to start without it -- and the next run trials it with the value set. "
                "Nothing else is affected."
            )
        if refused_login:
            lines.append("CREDENTIAL REFUSED -- " + "; ".join(f"{r.name}: {r.credential_hint}" for r in refused_login))
        if secrets:
            lines.append(
                "Required before these servers will work: export "
                + ", ".join(secrets)
                + " in the shell you start Claude Code from. No value for any of them is stored "
                f"in {writer.display_path} or anywhere else this run wrote."
            )
        elif not awaiting and names and not unproved_held and supplied:
            # Read, and already set where Claude Code reads it: "nothing is
            # needed" said that of gitlab-example-net, whose login reads GITLAB_EXAMPLE_NET_TOKEN.
            lines.append(
                "Every credential the registered servers read is already set where Claude Code reads it ("
                + ", ".join(supplied)
                + "); nothing is left to export."
            )
        elif not awaiting and names and not unproved_held:
            lines.append("No API key, token or other credential is needed by any registered server.")
        if unproved_held:
            # Every value they read is set where Claude Code reads it -- so
            # "export it" is the wrong step, and "nothing is needed" a false one:
            # confluence-example-com carried a token its account could not use.
            lines.append(
                "NOT PROVED -- "
                + ", ".join(sorted(unproved_held))
                + (" has" if len(unproved_held) == 1 else " have")
                + " every value it reads set where Claude Code reads it, yet no call proved it works; "
                "each step above says what the service answered"
            )
        if scripts:
            lines.append(
                "Generated locally and not part of any package -- keep or copy with the config: " + ", ".join(scripts)
            )
        if duplicates:
            lines.append(
                f"Duplicate configuration: {', '.join(sorted({d['name'] for d in duplicates}))} "
                f"in {writer.display_path} already point(s) at a service this run just registered. "
                "Keep one and remove the other."
            )
        unregistered = [o for o in outcomes if not o.registered and not o.succeeded]
        if unregistered:
            # Said by name, with what the file lacks and where to go next: the
            # registered list above never mentions an address that got nothing.
            parts = []
            for o in unregistered:
                part = f"{o.target} (no {writer.base_name(o.target)})"
                lead = o.best_lead
                if lead:
                    part += f" -- best lead: {lead['name']}"
                    if lead.get("needs"):
                        part += f", which needs {', '.join(lead['needs'])}"
                parts.append(part)
            lines.append(f"Not in {writer.display_path}: " + "; ".join(parts) + ".")
        schema = self._schema_problems(outcomes)
        if schema:
            lines.append(
                "Tool definitions Claude Code's API rejects: "
                + "; ".join(f"{s['server']} ({s['detail']})" for s in schema)
                + " -- loading one of those tools makes every request of the session fail."
            )
        broken = [
            (o.target, b)
            for o in outcomes
            for b in o.broken_existing
            if b.get("name") not in o.registered and b.get("name") not in {s.get("name") for s in o.stale}
        ]
        if broken:
            lines.append(
                "Left in place and not starting: "
                + ", ".join(sorted({b["name"] for _t, b in broken}))
                + " -- Claude Code still loads them and shows them failing; each step above "
                "says what to fix or how to remove it."
            )
        if kept_existing:
            lines.append(
                "Kept as they were: "
                + ", ".join(sorted({k["name"] for k in kept_existing}))
                + " -- existing registrations that answered during this run, left in place "
                "because nothing could be written for their address. Each step above says "
                "where it is."
            )
        if retired:
            lines.append(
                f"{'Would retire' if self.ctx.dry_run else 'Retired'} "
                f"{', '.join(sorted({s['name'] for s in retired}))}: already registered, but not "
                "the server of the address they were registered for."
            )
        if wrong_kept:
            lines.append(
                f"Not their address's server, left in place: "
                f"{', '.join(sorted({s['name'] for s in wrong_kept}))}. Remove them by hand -- "
                "each step above gives the command."
            )
        if incomplete:
            lines.append(
                "Tier-1 due diligence was not completed for "
                + ", ".join(incomplete)
                + "; anything below tier 1 was refused for those addresses."
            )
        lines.extend(self._agent_unavailable_lines())
        # Named, in order, and with the flag spelled out: the useful next step
        # is to run exactly these again, and that should be a copy-paste rather
        # than a re-reading of the log.
        unattempted = [a for a in self.ctx.deadline.skipped if a.startswith(("http://", "https://"))]
        if unattempted:
            lines.append(
                f"Out of time: the run reached its --timeout of "
                f"{format_duration(self.ctx.deadline.limit)} and never started "
                + ", ".join(unattempted)
                + ". Nothing already registered is affected. Continue with: --mcp-server "
                + ",".join(unattempted)
            )

        self.console.write("")
        self.console.write("  Final verdict:")
        for line in lines:
            self.console.bullet(line, indent=4)

        # Three states, and the word is the verdict rather than a decoration.
        # PARTIAL is the one that earns its place: a run where something works
        # but the operator is left holding a decision -- an address that yielded
        # nothing, or two entries now pointing at one service -- is not the same
        # result as a clean one, and a bare pass/fail hides exactly that case.
        # Needing a credential exported is not a defect: verification already
        # proved the server answers with it. An endpoint registered with an
        # empty credential is PARTIAL rather than FAILED for the same reason in
        # reverse -- the address did yield a real MCP endpoint, and what is
        # left is a token, not another search.
        # An address the deadline never let us start is not a failure of
        # discovery -- nothing was tried -- but it is emphatically not a clean
        # run either: work the operator asked for did not happen.
        # One verdict, from what a fresh Claude Code gets: an entry that works
        # only with this run's shell variables, or that Claude Code cannot
        # connect to, is not a clean result -- build-example-net was "OK" beside
        # "Required before these servers will work: export ...".
        not_fresh = [
            r.name
            for r in getattr(writer, "published", [])
            if not r.refused
            and (
                r.claude_usage.get("outcome") in ("missing_variable", "failed")
                or r.claude_connect.get("outcome") == "failed"
            )
        ]
        if not_fresh:
            line = (
                f"Not working in a fresh Claude Code yet: {', '.join(sorted(set(not_fresh)))} -- each step above "
                "names what it still needs"
            )
            lines.append(line)
            self.console.bullet(line, indent=4)
        if not working and not awaiting:
            verdict, status = "FAILED", Status.FAILED
        elif not working or len(working) < total or duplicates or wrong_kept or unattempted or not_fresh:
            verdict, status = "PARTIAL", Status.PARTIAL
        else:
            verdict, status = "SUCCESS", Status.OK
        detail = " ".join(lines) + f" Verdict: {verdict}"
        result.data["mcp_verdict"] = verdict
        result.data["mcp_never_attempted"] = unattempted
        result.data["mcp_required_environment"] = secrets
        result.data["mcp_pending_environment"] = pending_env
        result.data["mcp_duplicates"] = duplicates
        result.data["mcp_retired"] = [s["name"] for s in retired]
        result.data["mcp_kept_existing"] = [k["name"] for k in kept_existing]
        result.data["mcp_not_registered"] = [o.target for o in unregistered]
        result.data["mcp_schema_problems"] = schema
        result.data["mcp_wrong_registrations_kept"] = [s["name"] for s in wrong_kept]
        self.report_step(result, "final verdict", status, detail, data={"verdict": verdict})

    def _tidy_configuration_file(self, result: PhaseResult, writer: McpWriter) -> None:
        """Take the briefing out of a file an older version wrote it into.

        Reported rather than done quietly: the file is the operator's, this run
        changed it without being asked to, and the one-line reason has to be
        somewhere they can read it afterwards. It says nothing at all when the
        file is already what it should be, which is every run after the first.
        """
        removed = writer.tidy()
        if not removed:
            return
        self.report_step(
            result,
            "configuration file",
            Status.OK if self.ctx.dry_run else Status.CHANGED,
            f"{'would remove' if self.ctx.dry_run else 'removed'} {', '.join(removed)} from "
            f"{writer.display_path}: an older version of this program wrote its instructions "
            "into the file, and the file has to stay copy-and-paste configuration. The same "
            "instructions are printed below and kept in the log.",
            data={"removed_keys": removed, "path": str(writer.json_path)},
        )

    def _report_configuration_file(self, result: PhaseResult, writer: McpWriter, outcomes: list[TargetOutcome]) -> None:
        """Where the servers were written, and what to do with the file.

        A configuration file is only useful to someone who knows Claude Code
        reads it, and where from. That is not obvious -- it is a dotfile in the
        working directory, and the same servers can equally be put in another
        project or in the user's own configuration -- so the phase says it in
        full rather than leaving a file behind and assuming.

        It says it *here*, on screen, rather than in the file. ``.mcp.json``
        gets copied whole; anything in it that is not configuration is something
        the next reader has to notice and delete. So the briefing is a framed
        block in the terminal, captured verbatim in the log, and carried in the
        JSON report -- three places that outlive the run, none of them a file
        anybody pastes into a project.
        """
        names = [name for outcome in outcomes for name in outcome.registered]
        path = writer.display_path
        result.data["mcp_json_path"] = str(writer.json_path)
        if not names:
            # No server, no file: saying where an empty file would have gone
            # would only get in the way of the playbook above.
            return

        briefing = writer.briefing()
        instructions = writer.usage_instructions()
        result.data["mcp_briefing"] = briefing
        self._show_briefing(writer, briefing, names)
        self.report_step(
            result,
            "mcp configuration",
            Status.OK if self.ctx.dry_run else Status.CHANGED,
            f"{'would be written' if self.ctx.dry_run else 'written'} to {path}: " + ", ".join(names),
            data={"path": str(writer.json_path), "servers": names, "briefing": briefing},
            remediation=instructions,
        )
        if writer.operator_steps:
            # Everything the program could do, it did; these are the steps no
            # program can take -- and how to prove each one afterwards.
            result.data["mcp_operator_steps"] = writer.operator_steps
            self.report_step(
                result,
                "what only you can do",
                Status.PARTIAL,
                f"{len(writer.operator_steps)} step(s) left for you; each entry is written for the route that will work",
                data={"operator_steps": writer.operator_steps},
                remediation=[
                    f"{number}. {row['name']}: {row['step']}. Then: {row['confirm']}"
                    for number, row in enumerate(writer.operator_steps, 1)
                ],
            )
        if writer.login_advice:
            # Every way each service can be logged in to, and the one to use: a
            # token where one can read, the service's client where only a client
            # can log in (signed requests, Kerberos), a password last.
            result.data["mcp_login_advice"] = writer.login_advice
            self.report_step(
                result,
                "how to log in",
                Status.OK,
                f"{len(writer.login_advice)} address(es) with advice on how to log in",
                data={"login_advice": writer.login_advice},
                remediation=[f"{row['target']}: {line}" for row in writer.login_advice for line in row["lines"]],
            )
        if writer.lookbacks:
            # The entry that last worked here, and what is different now: what
            # is really missing, rather than a search from scratch.
            result.data["mcp_lookbacks"] = writer.lookbacks
            self.report_step(
                result,
                "what changed since it last worked",
                Status.PARTIAL,
                f"{len(writer.lookbacks)} address(es) had an entry that worked before and none that works now",
                data={"lookbacks": writer.lookbacks},
                remediation=[
                    f"{row['target']}: {row['name']} last worked {row['recorded']} ({'; '.join(row['tasks'])}). "
                    f"Since then -- {'; '.join(row['changes'])}. It is tried first on every run; restore it if "
                    "the change was not intended."
                    for row in writer.lookbacks
                ],
            )
        from initialize_claude_ai.mcp_auto_discovery.existing import ExistingRegistrations

        held = ExistingRegistrations.for_run(self.ctx).default_credentials
        if held:
            # ${NAME:-value} reads like a reference and is a literal: the
            # credential is in the file, and a missing export silently falls
            # back to it -- build-example-net kept sending an old token as its key.
            self.report_step(
                result,
                "credentials written into entries",
                Status.PARTIAL,
                f"{len(held)} credential(s) sit in entries as the default of a ${{...}} reference",
                data={"default_credentials": [{"entry": e, "variable": v} for e, v in held]},
                remediation=[
                    (
                        f"{entry}: the value written as the default of ${{{variable}:-...}} was removed from "
                        f"{path} by this run -- a file meant to be copied never keeps a credential, so a value put "
                        f"there is removed on every run. Export {variable} in the shell you start Claude Code from, "
                        f'or set it in Claude Code\'s settings ("env" in ~/.claude/settings.json); both survive '
                        "every run."
                    )
                    if any(v == variable for _c, v in writer.removed_defaults)
                    else f"{entry}: ${{{variable}:-...}} holds a credential. Remove the default (write ${{{variable}}}), "
                    f"export {variable} in the shell you start Claude Code from, and restart Claude Code."
                    for entry, variable in held
                ],
            )
        self._report_login_of_another_service(result, outcomes)
        shared = writer.shared_secret_findings()
        if shared:
            # One variable read by entries for two different services: one value
            # cannot be both instances' credential, so at most one of them can
            # work -- sd.example.com and jira.example.com both read JIRA_PERSONAL_TOKEN.
            result.data["mcp_shared_secrets"] = shared
            self.report_step(
                result,
                "shared credential variables",
                Status.PARTIAL,
                f"{len(shared)} credential(s) in {path} are used by entries for different services",
                data={"shared_secrets": shared},
                remediation=[
                    (
                        f"{', '.join(row['servers'])} send the same credential to different services "
                        f"({', '.join(row['hosts'])}). A login belongs to one instance: give each entry the "
                        "credential issued by its own service, and re-run phase 4."
                    )
                    if row.get("same_value")
                    else f"{row['variable']} is read by {', '.join(row['servers'])}, which reach "
                    f"{', '.join(row['hosts'])}. A credential belongs to one instance: give each entry its "
                    f"own variable ({', '.join(row['suggested'])}), export each, and re-run phase 4."
                    for row in shared
                ],
            )

    def _show_briefing(self, writer: McpWriter, briefing: dict[str, object], names: list[str]) -> None:
        """Print the briefing where it cannot be mistaken for something optional.

        A framed block with a title, because this is the one part of the run the
        operator has to act on afterwards -- export these variables, install
        that package, keep this generated script -- and a run of bullets in the
        middle of a hundred other lines is not where anybody finds it again. The
        console writes into the log transcript too, so the same block is in the
        log file without being assembled twice.
        """
        if not self.settings.bool_("mcp.instructions.enabled", True):
            return
        lines = _briefing_lines(briefing)
        if not lines:
            return
        self.console.write("")
        self.console.box(
            lines,
            title=f"{len(names)} MCP SERVER(S) -- HOW TO USE {writer.display_path}",
            footer=[Line(f"{self.glyph('running')} {step}", hang=2) for step in writer.usage_instructions()],
            frame_styles=BRIEFING_FRAME_STYLE,
        )


#: Per-server rows of the briefing, in the order they are shown: the key
#: :meth:`McpWriter.briefing` uses, and the label that reads sensibly under a
#: server name. A key the briefing did not produce for a server is simply not a
#: row -- most servers need nothing installed and generate no files.
_SERVER_ROWS: tuple[tuple[str, str], ...] = (
    ("what", "what it is"),
    ("found_by", "found by"),
    ("status", "status"),
    ("before_it_works", "to make it work"),
    ("value_does_not_fit", "fix this value"),
    ("replaces", "replaces"),
    ("required_environment", "needs"),
    ("install_first", "install first"),
    ("to_move_it", "on another machine"),
    ("this_server_is_a_script_written_by_this_run", "generated script"),
    ("duplicates", "already in the file"),
)


def _briefing_lines(briefing: Mapping[str, Any]) -> list[Line]:
    """Lay the briefing out as the block the operator reads.

    Ordered by what stands between them and a working server: the variables to
    export, the packages to install, the generated files not to delete, the
    entries that now collide -- and only then the per-server detail behind those
    four. Whatever the briefing does not have to say is not given a heading,
    because an empty section is a line the reader pays for and learns nothing
    from.
    """
    lines: list[Line] = []
    headline = str(briefing.get("read_this_first") or "")
    if headline:
        lines.append(Line(headline, ("dim",)))

    def section(title: str, styles: tuple[str, ...] = ("bold",)) -> None:
        if lines:
            lines.append(Line(""))
        lines.append(Line(title, styles))

    # First, and in the colour reserved for "you have to do something": these
    # entries are written and registered but inert, and the operator is the only
    # one who can obtain what they are missing.
    refused = briefing.get("these_were_given_a_credential_their_service_refused") or {}
    if isinstance(refused, Mapping) and refused:
        section(
            "ACTION REQUIRED -- the credential supplied was REFUSED, not working yet",
            ("yellow", "bold"),
        )
        for name, remedy in refused.items():
            lines.append(Line(f"  {name}: {remedy}", ("yellow",), hang=4))
    awaiting = briefing.get("these_need_a_real_credential_before_they_work") or {}
    if isinstance(awaiting, Mapping) and awaiting:
        section(
            "ACTION REQUIRED -- registered with an EMPTY credential, not working yet",
            ("yellow", "bold"),
        )
        for name, variables in awaiting.items():
            # Already rendered as `NAME=<shape>` by the writer, which is the
            # only place that knows what each individual variable holds.
            lines.append(Line(f"  {name}: export {' '.join(_strings(variables))}", ("yellow",), hang=4))
        why = str(briefing.get("why_they_were_registered_anyway") or "")
        if why:
            lines.append(Line(why, ("dim",)))

    env = _strings(briefing.get("required_environment_variables"))
    if env:
        section("Export these before you start Claude Code")
        lines.append(Line("export " + " ".join(f"{name}=..." for name in env), (), hang=2))
        note = str(briefing.get("nothing_here_contains_a_secret") or "")
        if note:
            lines.append(Line(note, ("dim",)))

    install = _strings(briefing.get("install_commands"))
    if install:
        section("Install these first")
        lines.extend(Line(f"  {step}", (), hang=2) for step in install)

    scripts = _strings(briefing.get("generated_scripts"))
    if scripts:
        section("Written by this run -- these servers stop working without them")
        lines.extend(Line(f"  {path}", (), hang=2) for path in scripts)

    warning = str(briefing.get("duplicate_warning") or "")
    if warning:
        section(warning, ("yellow", "bold"))
        lines.extend(Line(f"  {name}", ("yellow",), hang=2) for name in _strings(briefing.get("duplicates")))

    servers = briefing.get("servers") or {}
    if isinstance(servers, Mapping) and servers:
        section("Servers")
        rows = [(label, key) for key, label in _SERVER_ROWS]
        pad = max(len(label) for label, _ in rows) + 4
        for name, detail in servers.items():
            lines.append(Line(f"  {name}", ("bold",), hang=4))
            if not isinstance(detail, Mapping):
                continue
            for label, key in rows:
                value = detail.get(key)
                if not value:
                    continue
                text = ", ".join(_strings(value)) if isinstance(value, (list, tuple)) else str(value)
                lines.append(Line(f"    {label.ljust(pad - 4)}{text}", (), hang=pad))
    return lines


def _strings(value: Any) -> list[str]:
    """Whatever the briefing put under a key, as a list of lines to print."""
    if not value:
        return []
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    return [str(value)]


def _lead_rank(candidate: Candidate) -> tuple:
    """How close a candidate came, for the best lead: higher is closer.

    Waiting for a credential first; then one that completed a handshake; then
    one that *ran* and said why it stopped (a local server, or an endpoint that
    answered with something other than "not here"); a 404 last -- it is the
    absence of a server, not a lead to one.
    """
    shake = candidate.handshake
    absent = bool(shake is not None and shake.status == 404)
    ran = bool(candidate.kind == "stdio" and shake is not None) or bool(candidate.start_failure)
    return (candidate.needs_credential, bool(shake and shake.ok), ran, not absent, candidate.confidence)


def _login_words(candidate: Any) -> str:
    """The login check's verdict on ``candidate`` as a phrase; "" without one."""
    shake = getattr(candidate, "handshake", None)
    if shake is None or not shake.login:
        return ""
    words = login_check.in_words(shake.login, sent=getattr(shake, "credential_sent", None))
    return f"{words} ({shake.login_detail})" if words and shake.login_detail else words


def _login_refused(candidate: Candidate) -> bool:
    """Was this candidate's credential refused by its service (the login check)?"""
    return bool(candidate.handshake is not None and candidate.handshake.login_refused)
