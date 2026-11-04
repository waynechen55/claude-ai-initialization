"""Command line entry point.

``initialize-claude-ai run`` assembles a :class:`~initialize_claude_ai.core.context.RunContext`
from the configuration layers, then runs the selected phases in order.  Nothing
about the phase list is hardcoded here: phases self-register, and ``--phases``
tokens are resolved by :func:`initialize_claude_ai.phases.select`.

Two flags matter more than the rest:

``--dry-run``   mutating commands are simulated, read-only probes still run, so
                the machine's real state is still reported honestly.
``--json``      stdout carries only the machine-readable report; everything
                human goes to stderr.

Every credential the command line can supply comes in two forms.  ``--x-ref``
names *where* to read the value (``env:NAME``, ``file:/path``,
``keyring:service/user``, ...), which is resolved at the moment of use; ``--x``
carries the value itself, which is convenient and unsafe -- ``ps``, the shell
history and any CI log show it to everyone.  ``--secure`` refuses the second
form everywhere, in this program and in every phase it drives.
"""

from __future__ import annotations

import argparse
import fnmatch
import os
import shlex
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Sequence

from . import __version__
from .core import diskspace, platform_detect, redact
from .core.config import ENV_PREFIX, load_settings
from .core.context import RunContext
from .core.deadline import Deadline, parse_duration
from .core.envfile import EnvStore
from .core.errors import ConfigError, InitializeClaudeAiError
from .core.fileio import write_private
from .core.logging_setup import (
    BADGE_ROWS,
    BADGES,
    BANNER_FRAME_STYLE,
    DEFAULT_DATE_FORMAT,
    DEFAULT_PALETTE,
    FRAME_STYLE,
    LIGHT_PALETTE,
    PALETTE_ALIASES,
    PALETTES,
    Console,
    Line,
    Transcript,
    active_transcript,
    close_transcript,
    get_logger,
    glyph_for,
    record,
    setup_logging,
    styles_for,
)
from .core.proc import Runner
from .core.report import (
    BAD,
    SAFETY_LABEL,
    SAFETY_STYLE,
    PhaseResult,
    RunReport,
    Safety,
    Status,
    format_duration,
    verdict_of,
)
from .core.secrets import SecretResolver
from .core.trust_store import PIN_MODES
from .install.toolchains import PREFETCH_MODES, PROVISION_MODES
from .phases import all_phases, select
from .phases.base import LABEL_WIDTH
from .phases.phase4_mcp_auto_discovery import BARE_CONCURRENCY

log = get_logger("cli")

PROGRAM = "initialize-claude-ai"

#: Subcommand used when the first argument is an option or a phase selector.
DEFAULT_COMMAND = "run"


@dataclass(frozen=True)
class SecretFlag:
    """One credential the command line can supply, in both of its forms.

    ``--auth-anthropic-key-ref env:NAME`` names where to read the value;
    ``--auth-anthropic-key VALUE`` carries it inline.  Both compile to the same
    setting -- the inline form by way of a ``literal:`` reference -- so there is
    a single resolution path, a single place that warns, and a single place that
    registers the value for redaction.

    Declaring the pair here rather than at each call site is what makes
    ``--secure`` total: a credential flag that is not in this table does not
    exist, so none can be added later that quietly escapes the check.
    """

    plain: str
    """The value-taking flag, e.g. ``--auth-anthropic-key``."""
    key: str
    """Dotted setting both forms write to."""
    help: str
    """What the credential is; the two help strings are built from it."""
    group: str
    """Which argument group the pair is declared in."""

    @property
    def ref(self) -> str:
        return f"{self.plain}-ref"

    @property
    def dest(self) -> str:
        return self.plain.lstrip("-").replace("-", "_")

    @property
    def ref_dest(self) -> str:
        return f"{self.dest}_ref"


#: Every credential the CLI accepts. See :class:`SecretFlag`.
SECRET_FLAGS: tuple[SecretFlag, ...] = (
    # Phase 2 signs in with it and phase 3 asks the same key for the model
    # catalogue, so it is not a phase 2 flag -- see the "any phase" group.
    SecretFlag(
        "--auth-anthropic-key",
        "auth.anthropic_api.api_key_refs",
        "Anthropic API key, sent as x-api-key",
        "shared",
    ),
    SecretFlag(
        "--auth-anthropic-token",
        "auth.anthropic_api.auth_token_refs",
        "Anthropic bearer token for gateways and proxies, sent as Authorization",
        "auth",
    ),
    SecretFlag(
        "--auth-claude-token",
        "auth.claude_oauth.token_ref",
        "long-lived Claude Code OAuth token",
        "auth",
    ),
    SecretFlag(
        "--mcp-token",
        "mcp.discovery.auth.bearer_token_ref",
        "bearer token used when probing an MCP target",
        "mcp",
    ),
)


@dataclass(frozen=True)
class SettingFlag:
    """A flag that is exactly ``--set KEY=VALUE``, with the value checked first.

    Each of these used to be written out by hand in three places -- the parser,
    the bounds check and the override -- and each copy was a chance to drift: a
    bare form nobody meant to allow, a check that let 0 through, a key spelt
    differently from the one the phase reads. Declared here, the three are
    derived from one row, so a new numeric knob is one line and cannot be added
    half-wired. ``tests/test_cli.py`` holds every row to the same rules.

    Every one requires a value, and none has a default: an option the operator
    did not type leaves no override behind, so the configuration layers
    underneath decide exactly as they would have without the flag existing.
    """

    flag: str
    """The option, e.g. ``--agent-max-attempts``."""
    key: str
    """Dotted setting the value is written to."""
    kind: type
    """``int`` or ``float``; argparse rejects anything else before this sees it."""
    minimum: float
    """Lowest value accepted -- or, when ``exclusive``, the bound it must exceed."""
    metavar: str
    help: str
    remediation: str
    """Said when the value is out of bounds: what a sensible value looks like."""
    exclusive: bool = False
    agent_only: bool = False
    """Read only by the phase-4 agent, so inert when that agent is turned off."""

    @property
    def dest(self) -> str:
        return self.flag.lstrip("-").replace("-", "_")

    @property
    def section(self) -> str:
        """The top-level section of :attr:`key` -- which is also where it is shown.

        ``mcp.agent.max_attempts`` is read by the phase whose ``enable_key`` is
        ``mcp``, so ``--help`` files it under that phase; a key outside every
        phase's section (``run.timeout_scale``) is about the run as a whole.
        Derived rather than declared, so a row cannot be filed under a phase
        that does not read it.
        """
        return self.key.split(".", 1)[0]

    def bound(self) -> str:
        return f"greater than {self.minimum:g}" if self.exclusive else f"at least {self.minimum:g}"

    def accepts(self, value: float) -> bool:
        return value > self.minimum if self.exclusive else value >= self.minimum


#: Every flag that is shorthand for one bounded numeric setting. See
#: :class:`SettingFlag`; adding a row adds the flag, its check and its override.
SETTING_FLAGS: tuple[SettingFlag, ...] = (
    SettingFlag(
        "--agent-max-turns",
        "mcp.agent.max_turns_override",
        int,
        1,
        "N",
        help=(
            "turn budget for every phase-4 subagent, replacing each stage's own. "
            "Lower makes a run cheaper; raising it usually wants --timeout-scale too. At "
            "least 1; default: each stage's own"
        ),
        remediation="A budget of 0 would leave the agent no move to make at all.",
        agent_only=True,
    ),
    SettingFlag(
        "--agent-max-attempts",
        "mcp.agent.max_attempts",
        int,
        1,
        "N",
        help=(
            "tries the phase-4 agent gets per stage, each told what went wrong with the last "
            "(= --set mcp.agent.max_attempts=N). Transient failures are not counted. At "
            "least 1; default: mcp.agent.max_attempts"
        ),
        remediation="Use 1 for a single try per stage, or a larger number for more corrections.",
        agent_only=True,
    ),
    SettingFlag(
        "--timeout-scale",
        "run.timeout_scale",
        float,
        0,
        "FACTOR",
        help=(
            "multiply every per-operation timeout (5 s connects up to 30 min sign-ins) by "
            "FACTOR, keeping their proportions: 2 doubles them, 0.5 halves them. Default 1.0. "
            "Useful on a slow link or with --concurrency"
        ),
        remediation="Use 2 to double every timeout, 0.5 to halve them, 1 to leave them.",
        exclusive=True,
    ),
)

#: Model tiers that get a ``--default-<tier>-model`` flag of their own. The
#: authoritative list of tiers is ``provision.tiers`` in the configuration; this
#: is the subset the command line puts a flag on, and it has to be a Python
#: constant because the parser is built before any settings are loaded. Adding a
#: name here adds the flag and its override in one move.
MODEL_TIER_FLAGS: tuple[str, ...] = ("opus", "sonnet", "haiku")

#: Backend sections carrying a ``model_aliases`` table. A tier pin is written to
#: every one of them, because ``--default-opus-model`` names a model rather than
#: a provider -- and only the selected backend's table is ever read, so writing
#: the others is inert rather than wrong.
MODEL_ALIAS_SECTIONS: tuple[str, ...] = (
    "provision.vertex",
    "provision.anthropic_api",
    "provision.bedrock",
)


# ----------------------------------------------------------------------------
# argument parsing
# ----------------------------------------------------------------------------


#: Defaults for the flags shared by every subcommand. They are declared with
#: ``SUPPRESS`` so that ``initialize-claude-ai --json phases`` is not undone by the
#: subparser re-applying its own default, and filled in afterwards.
COMMON_DEFAULTS: dict[str, object] = {
    "config": [],
    "env_file": [],
    "overrides": [],
    "json": False,
    "report": None,
    "log_level": None,
    "log_file": None,
    "no_log_file": False,
    "quiet": False,
    "no_color": False,
    "secure": False,
}


class FullHelpAction(argparse.Action):
    """``--help`` on the program itself: its own options, then every command's.

    argparse only ever prints the parser it was asked about, and a
    subcommand's options are invisible from its parent -- so the program's help
    listed the shared options and five command names, and not one phase flag,
    though ``run`` is what ``initialize-claude-ai --anything`` runs. This prints
    each command's own groups after the program's, read off the parsers
    themselves, so a flag added anywhere appears here with no list to update.
    Options every command shares are printed once, at the top.
    """

    def __init__(self, option_strings, dest=argparse.SUPPRESS, default=argparse.SUPPRESS, help=None):
        super().__init__(option_strings=option_strings, dest=dest, default=default, nargs=0, help=help)

    def __call__(self, parser, namespace, values, option_string=None):
        parser._print_message(format_full_help(parser), sys.stdout)
        parser.exit()


def format_full_help(parser: argparse.ArgumentParser) -> str:
    """The program's help followed by every subcommand's own options."""
    epilog, parser.epilog = parser.epilog, None
    try:
        text = parser.format_help()
    finally:
        parser.epilog = epilog
    shared = {option for action in parser._actions for option in action.option_strings}
    commands = next((a for a in parser._actions if isinstance(a, argparse._SubParsersAction)), None)
    summaries = {a.dest: a.help for a in (commands._choices_actions if commands else ())}
    seen: set[int] = set()
    for name, command in commands.choices.items() if commands else ():
        command._initialize_parent_help = summaries.get(name, "")
        if id(command) in seen:  # an alias is the same parser under another name
            continue
        seen.add(id(command))
        text += "\n" + _format_command_options(name, command, shared)
    if epilog:
        formatter = parser._get_formatter()
        formatter.add_text(epilog)
        text += "\n" + formatter.format_help()
    return text


def _command_summary(name: str, command: argparse.ArgumentParser) -> str:
    """The one-line help a command was registered with, e.g. "run one or more phases"."""
    parent = getattr(command, "_initialize_parent_help", None)
    return parent or name


def _format_command_options(name: str, command: argparse.ArgumentParser, shared: set[str]) -> str:
    """One command's groups, without the options the program already listed."""
    formatter = command._get_formatter()
    formatter.add_text(f"=== {PROGRAM} {name}: {_command_summary(name, command)} ===")
    formatter.add_text(command.description)
    for group in command._action_groups:
        actions = [
            action
            for action in group._group_actions
            if not isinstance(action, argparse._HelpAction)
            and not (action.option_strings and set(action.option_strings) <= shared)
        ]
        if not actions:
            continue
        formatter.start_section(group.title)
        formatter.add_text(group.description)
        formatter.add_arguments(actions)
        formatter.end_section()
    return formatter.format_help()


def _concurrency(text: str) -> int | str:
    """``--concurrency N``: a whole number of addresses, at least one.

    The bare form arrives here too (argparse converts a string ``const``), as
    :data:`BARE_CONCURRENCY`, and is passed on as that keyword.
    """
    if text.strip().lower() == BARE_CONCURRENCY:
        return BARE_CONCURRENCY
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a whole number, got {text!r}") from None
    if value < 1:
        raise argparse.ArgumentTypeError(
            f"{value} addresses at a time would work none; use 1 or more "
            "(1 is one after another, which is also what leaving the flag out does)"
        )
    return value


def build_parser() -> argparse.ArgumentParser:
    """The whole command line: the shared options, then one parser per command.

    Every flag here is a *shortcut* onto a setting, never a second source of
    truth: it lands in the same layered configuration as everything else (see
    :func:`build_context`), which is why almost all of them default to
    ``argparse.SUPPRESS`` -- an option the operator did not type must leave no
    trace, or it would silently outrank the config file it was meant to
    override.
    """

    suppress = argparse.SUPPRESS

    def add_common(common: argparse.ArgumentParser) -> argparse.ArgumentParser:
        """The options every command takes, defined on ``common`` itself.

        Not shared through ``parents=``: before Python 3.12 argparse copies a
        mutually exclusive group out of its titled group, so ``--do-cleanup`` and
        ``--log-file`` landed in the untitled "options" group of every command.
        """
        source = common.add_argument_group("configuration")
        source.add_argument(
            "--config",
            action="append",
            default=suppress,
            metavar="FILE",
            help="extra TOML config file; repeatable, later files win",
        )
        source.add_argument(
            "--env-file",
            action="append",
            default=suppress,
            metavar="FILE",
            help="dotenv file whose variables are loaded before settings are interpolated; repeatable",
        )
        source.add_argument(
            "--set",
            action="append",
            default=suppress,
            metavar="KEY=VALUE",
            dest="overrides",
            help="override one setting, e.g. --set mcp.exhaustive=true",
        )
        source.add_argument(
            "--secure",
            action="store_true",
            default=suppress,
            help=(
                "refuse credentials given as values: only the --*-ref forms work, and an "
                "inline literal: reference is rejected wherever it comes from"
            ),
        )
        upkeep = common.add_argument_group("upkeep")
        cleanup = upkeep.add_mutually_exclusive_group()
        cleanup.add_argument(
            "--do-cleanup",
            action="store_true",
            default=suppress,
            help=(
                "at the end of the run, remove this program's own leftovers that nothing uses -- its old "
                "temporary directories and unused work files (on by default; run.cleanup)"
            ),
        )
        cleanup.add_argument(
            "--no-cleanup",
            action="store_true",
            default=suppress,
            help="keep every leftover this run and earlier runs made",
        )
        output = common.add_argument_group("output")
        output.add_argument(
            "--json",
            action="store_true",
            default=suppress,
            help="print the JSON report on stdout and nothing else",
        )
        output.add_argument("--report", metavar="FILE", default=suppress, help="also write the JSON report to FILE")
        output.add_argument(
            "--log-level",
            metavar="LEVEL",
            default=suppress,
            help="console detail: trace, debug, info, warning or error (the log file keeps logging.file_level)",
        )
        # Note that neither of these is exclusive with --quiet or --json: silencing
        # the terminal is a reason to want the file, not a reason to lose it.
        log_file = output.add_mutually_exclusive_group()
        log_file.add_argument(
            "--log-file",
            metavar="FILE",
            default=suppress,
            help="record this run in FILE instead of paths.log_file",
        )
        log_file.add_argument(
            "--no-log-file",
            action="store_true",
            default=suppress,
            help="do not record this run to a file at all",
        )
        output.add_argument(
            "--quiet",
            action="store_true",
            default=suppress,
            help="suppress the human-readable output; the log file and --report still record the run",
        )
        output.add_argument("--no-color", action="store_true", default=suppress, help="disable ANSI colour")
        return common

    parser = argparse.ArgumentParser(
        prog=PROGRAM,
        description="Initialize a Claude AI environment: install, authenticate, provision, discover MCP.",
        add_help=False,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            f"  {PROGRAM} run --dry-run\n"
            f"  {PROGRAM} run --phases 1 --install-only claude-cli,gcloud\n"
            f"  {PROGRAM} run --phases 2 --auth-backend google-cloud --headless\n"
            f"  {PROGRAM} run --phases 2 --gcloud-user-account me@example.com --force-auth\n"
            f"  {PROGRAM} run --phases 2 --auth-backend google-cloud --force-auth-with-reset\n"
            f"  {PROGRAM} run --phases 3 --provision-backend vertex --gcloud-region us-east5\n"
            f"  {PROGRAM} run --phases 4 --mcp-server example.com,https://api.example.com\n"
            f"  {PROGRAM} run --phases 4 --mcp-server example.com --exhaustive-mcp\n"
            f"  {PROGRAM} run --phases 4 --mcp-recheck example.com\n"
            f"  {PROGRAM} run --phases 4 --mcp-server example.com --the-best-mcp\n"
            f"  {PROGRAM} run --phases 4 --mcp-server example.com --no-agent\n"
            f"  {PROGRAM} run --phases 4 --mcp-server a.example,b.example --concurrency 2\n"
            f"  {PROGRAM} run --phases 4 --mcp-server example.com --agent-max-attempts 5\n"
            f"  {PROGRAM} run --secure --auth-anthropic-key-ref keyring:anthropic/prod\n"
            "\n"
            "'run' may be left out when the first argument is an option:\n"
            f"  {PROGRAM} --dry-run   is the same as   {PROGRAM} run --dry-run\n"
        ),
    )
    add_common(parser)
    parser.add_argument(
        "-h",
        "--help",
        action=FullHelpAction,
        help="show every option of every command, phase by phase, and exit",
    )
    parser.add_argument("--version", action="version", version=f"{PROGRAM} {__version__}")
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser(
        "run",
        help="run one or more phases",
        description=(
            "Run the initialization phases in order: 1 install, 2 auth, 3 provision, "
            "4 mcp. Every flag below is shorthand for a setting, so anything given here "
            "can equally be put in a config file or an environment variable. "
            "Exit status: 0 success, 1 a phase failed, 2 the command line was refused "
            "(or a partial run, with run.partial_is_failure), 130 interrupted."
        ),
    )
    add_common(run)
    # Everything that is about the run itself rather than what one phase does.
    # Titled, so the untitled "options" group argparse keeps for -h holds -h and
    # nothing else: a flag left there would read as belonging to no phase.
    control = run.add_argument_group(
        "run control",
        "Which phases run and how the run as a whole behaves, whichever phases those are.",
    )
    control.add_argument(
        "--phases",
        metavar="LIST",
        help="phases to run: numbers, names, ranges or 'all' (default: all, from run.default_phases)",
    )
    control.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="simulate every change; read-only probes still run, so the report stays truthful",
    )
    control.add_argument(
        "-a",
        "--auto-agree",
        action="store_true",
        help="answer yes to every confirmation, for unattended runs",
    )
    control.add_argument(
        "--ignore-failure",
        action="store_true",
        help="run the remaining phases even after one fails (same as run.continue_on_failure=true)",
    )
    control.add_argument(
        "--no-partial-success",
        action="store_true",
        help="stop when a phase is only partly successful, which by default is not fatal",
    )
    # All of these require a value -- --timeout here, and every SETTING_FLAGS row
    # after it. --concurrency has a bare form because its bare value only
    # changes scheduling and cannot change the result; a wrong deadline, scale,
    # turn budget or attempt count silently changes what work gets done, and a
    # convenience shorthand is not worth that. Omit any of them and nothing
    # about the run differs.
    control.add_argument(
        "--timeout",
        metavar="DURATION",
        help=(
            "wall-clock ceiling for the whole run: 45s, 90m, 2h, 1d or plain seconds. It stops "
            "new work from starting; work already under way finishes, so the run can overrun "
            "by one address. Addresses never reached are named in the report, so list the "
            "important ones first. No ceiling by default; per-operation timeouts are unchanged"
        ),
    )

    # Flags whose value more than one phase reads. They are grouped here rather
    # than filed under the first phase that happens to use them, because a flag
    # in a "phase 3" group reads as one that phase 3 acts on -- and somebody
    # running `--phases 2` would leave it off and get a different sign-in.
    shared = run.add_argument_group(
        "any phase",
        "Read by more than one phase; give them with whichever phases you run.",
    )
    # Two spellings of one flag: --gcloud-project reads as a pair with
    # --gcloud-region, and --vertex-project says which of this program's three
    # backends it applies to. Neither is wrong, so neither is taken away.
    shared.add_argument(
        "--gcloud-project",
        "--vertex-project",
        dest="gcloud_project",
        metavar="ID",
        help=(
            "Google Cloud project for the whole run: phase 2 signs in against it and points "
            "gcloud at it, phase 3 bills Vertex to it. Without it: ANTHROPIC_VERTEX_PROJECT_ID, "
            "GOOGLE_CLOUD_PROJECT or CLOUDSDK_CORE_PROJECT, then gcloud's active project"
        ),
    )

    # One group per registered phase, keyed by the section it reads, so the
    # titles cannot drift from --phases and a flag table can route a row by the
    # setting it writes (see SettingFlag.section).
    groups = {"run": control, "shared": shared, **_phase_groups(run)}
    install = groups["install"]
    install.add_argument(
        "--install-only",
        metavar="LIST",
        help=(
            "install only these components: comma-separated component keys or group names "
            "(base, runtime, claude, cloud, browser, desktop, mcp-tooling, mcp-runtimes); "
            f"list them with '{PROGRAM} config components'"
        ),
    )
    install.add_argument(
        "--install-skip",
        metavar="LIST",
        help="skip these components; same syntax as --install-only, and it wins over it",
    )
    # A value is required: which toolchains to fetch ahead of time is a choice
    # with three answers, and none of them is obviously what a bare flag means.
    install.add_argument(
        "--install-prefetch",
        choices=PREFETCH_MODES,
        metavar="{" + ",".join(PREFETCH_MODES) + "}",
        help=(
            "also install what phase 4 launches MCP servers with: 'launchers' the programs "
            "behind install.demand_sources (npx, uvx, ...), 'all' every mcp-runtimes toolchain "
            "(go, rust, deno, bun, docker) too, 'none' neither. Optional; --install-only wins. "
            "Default: install.prefetch (launchers)"
        ),
    )
    install.add_argument(
        "--force-install",
        action="store_true",
        help="reinstall every selected component, even those that already verify",
    )
    install.add_argument(
        "--break-dependencies",
        action="store_true",
        help="let pip and uv install into a PEP 668 externally managed Python (--break-system-packages)",
    )
    install.add_argument(
        "--downgrade",
        action="store_true",
        help="install the known-good releases pinned in install.pins instead of the latest",
    )

    auth = groups["auth"]
    # Singular to read as a pair with --provision-backend, and because naming
    # one backend is the common case. The dest stays plural: the value is a
    # list, and 'all' expands to three of them.
    auth.add_argument(
        "--auth-backend",
        dest="auth_backends",
        metavar="LIST",
        help=(
            "credentials to obtain, comma separated: google-cloud (what Vertex reads), "
            "claude-oauth (the claude.ai session), anthropic-api, or 'all' (the default)"
        ),
    )
    auth.add_argument(
        "--force-auth",
        action="store_true",
        help=(
            "sign in again even where a working credential was found; the existing one is "
            "still reported, and is not removed"
        ),
    )
    auth.add_argument(
        "--force-auth-with-reset",
        action="store_true",
        help=(
            "--force-auth, after clearing the existing credentials: revoke the gcloud login "
            "and application-default credential, log the claude CLI out, and empty this "
            "program's own browser profile. Your own browser stays signed in; the sign-in says "
            "how to get one that asks. Confirms first; destructive and host-wide"
        ),
    )
    # One or the other: given both, the last on the command line would silently
    # win, and the operator would have no way to tell which flow they got.
    browser_mode = auth.add_mutually_exclusive_group()
    browser_mode.add_argument(
        "--headless",
        action="store_true",
        help=(
            "you open the URL yourself: every backend prints a URL -- no browser is opened -- and "
            "waits for the code the page shows. Without it the code is pasted back too, and the "
            "URL is opened for you where a browser can be shown"
        ),
    )
    browser_mode.add_argument(
        "--browser-loopback-redirect",
        action="store_true",
        help=(
            "redirect sign-in (opt-in): every backend opens a browser that hands the result back "
            "to this machine by itself, with no code to fall back on if it never arrives. Not "
            "with --headless"
        ),
    )
    # Orthogonal to the pair above: those choose the sign-in flow, these decide
    # whether the browser at the end of it is usable. Both matter even under
    # --headless, because the URL that gets printed is usually pasted into a
    # browser on this same machine.
    auth.add_argument(
        "--browser",
        metavar="COMMAND",
        help=(
            'command that opens a URL, %%s standing for it ("firefox %%s"). Default: the '
            "first known browser on PATH. Opens the sign-in URL, and is exported as BROWSER for "
            "sign-ins that open it themselves"
        ),
    )
    auth.add_argument(
        "--browser-input-fix",
        choices=("auto", "always", "never"),
        metavar="WHEN",
        help=(
            "give the browser GTK's built-in input method: auto|always|never. Use 'always' if "
            "what you type into the sign-in form vanishes when the field loses focus"
        ),
    )
    auth.add_argument(
        "--browser-profile",
        choices=("auto", "always", "never"),
        metavar="WHEN",
        help=(
            "sign in with a browser profile of this program's own: auto|always|never. No "
            "extensions, no earlier sign-in. A reset turns 'auto' into 'always'"
        ),
    )
    auth.add_argument(
        "--sign-in-capture",
        choices=("auto", "always", "never"),
        metavar="WHEN",
        help=(
            "capture the printed sign-in URL whole into a file and the clipboard, so a wrapped "
            "URL never has to be selected: auto|always|never. 'never' gives the sign-in your "
            "terminal directly"
        ),
    )
    # Identities, not credentials: an account name is public, so these are
    # ordinary value-taking flags rather than entries in SECRET_FLAGS.
    auth.add_argument(
        "--gcloud-user-account",
        metavar="EMAIL",
        help=(
            "Google account gcloud must be signed in as; any other active account counts as "
            "not signed in, so phase 2 signs in again"
        ),
    )
    auth.add_argument(
        "--gcloud-service-account",
        metavar="EMAIL",
        help=(
            "impersonate this service account from the signed-in user, with no key file; "
            "not with --gcloud-service-account-file"
        ),
    )
    # A path, not a payload: the key never passes through this process, gcloud
    # reads the file itself. So it is safe under --secure, unlike a raw value.
    auth.add_argument(
        "--gcloud-service-account-file",
        metavar="PATH",
        help=(
            "sign in as the service account whose key JSON is at PATH; gcloud reads the file "
            "itself, so the key never passes through this program"
        ),
    )
    auth.add_argument(
        "--claude-user-account",
        metavar="EMAIL",
        help=(
            "claude.ai account this machine should be signed in as; named in the sign-in "
            "prompt, and a mismatch is warned about"
        ),
    )

    provision = groups["provision"]
    provision.add_argument(
        "--provision-backend",
        metavar="NAME",
        help="API Claude Code is configured against: vertex, anthropic-api, bedrock or auto (the default)",
    )
    provision.add_argument(
        "--use-vertex",
        choices=["0", "1"],
        metavar="0|1",
        help=("allow Vertex at all (1, the default); 0 leaves it out of 'auto' and refuses --provision-backend vertex"),
    )
    provision.add_argument(
        "--gcloud-region",
        metavar="NAME",
        help=("Vertex location, e.g. us-east5. 'global' (the default), 'us' and 'europe' are multi-region"),
    )
    provision.add_argument(
        "--default-model",
        metavar="TIER",
        help=(
            "tier Claude Code starts on: opus, sonnet or haiku (a model id is accepted and "
            'mapped to its tier). Written as settings.json "model". Default: '
            "provision.default_tier (opus)"
        ),
    )
    for tier in MODEL_TIER_FLAGS:
        provision.add_argument(
            f"--default-{tier}-model",
            metavar="ID",
            help=(f"pin the {tier} tier to this model id instead of the best {tier} the backend's catalogue offers"),
        )

    mcp = groups["mcp"]
    mcp.add_argument(
        "--mcp-server",
        action="append",
        default=[],
        metavar="LIST",
        help=(
            "servers to find an MCP endpoint for, comma separated and repeatable, written "
            "however you have them (example.com, https://api.example.com/mcp, localhost:3000); "
            "each is normalised to one base URL first"
        ),
    )
    mcp.add_argument(
        "--mcp-recheck",
        action="append",
        default=[],
        metavar="LIST",
        help=(
            "prove these addresses again after acting on the report: only their existing and "
            "last working entries are tried, with the usage checks and repairs -- nothing is "
            "searched for. Written like --mcp-server; 'all' re-checks every entry of the project "
            "file (e.g. a .mcp.json copied from another machine)"
        ),
    )
    mcp.add_argument(
        "--stages",
        metavar="LIST",
        help="stages to try, in order, comma separated: endpoint, package, bridge, strategy (default: all)",
    )
    # What to come back with. The two are opposites -- every working endpoint,
    # or the single best one -- so asking for both is asking for two different
    # runs; argparse says so rather than letting one quietly win.
    mcp.add_argument(
        "--concurrency",
        nargs="?",
        type=_concurrency,
        const=BARE_CONCURRENCY,
        default=suppress,
        metavar="N",
        help=(
            "work N addresses at once; bare --concurrency uses "
            "mcp.discovery.target_concurrency_bare. Shortens the wait, not the work or "
            "tokens; output stays in address order. Default: one at a time"
        ),
    )
    mcp.add_argument(
        "--mcp-provision-missing",
        choices=PROVISION_MODES,
        metavar="{" + ",".join(PROVISION_MODES) + "}",
        help=(
            "when a candidate needs a program that is not installed (go, cargo, uvx, ...), "
            "install it from the phase 1 catalog, once per run: 'auto' only without a password "
            "prompt, 'always' privileged strategies too, 'never' reject it as before. Never "
            "changes which server is chosen. Default: mcp.packages.provision_missing (auto)"
        ),
    )
    mcp.add_argument(
        "--mcp-trust-ca",
        metavar="FILE[,FILE]",
        help=(
            "extra certificate authorities (PEM) to trust for every probe, registered stdio "
            "server and generated bridge in this run, for targets behind an internal CA. "
            "Node needs the self-signed root, not only an intermediate. "
            "Default: mcp.trust.extra_ca (none)"
        ),
    )
    mcp.add_argument(
        "--mcp-trust-pin",
        choices=PIN_MODES,
        metavar="{" + ",".join(PIN_MODES) + "}",
        help=(
            "when a target's certificate is not trusted here, 'targets' learns the chain it "
            "presents (addresses you asked for only; kept under mcp.trust.store_dir and "
            "reported with its fingerprint), 'never' does not. "
            "Default: mcp.trust.pin_presented_chain (targets)"
        ),
    )
    selection = mcp.add_mutually_exclusive_group()
    selection.add_argument(
        "--exhaustive-mcp",
        action="store_true",
        help=(
            "register every working MCP endpoint per address, not just the first: runs every "
            "stage and probes every candidate path. Slower; not with --the-best-mcp"
        ),
    )
    selection.add_argument(
        "--the-best-mcp",
        action="store_true",
        help=(
            "search widely, score what answers (function, ease, setup, stability, security, "
            "protocol, provenance) and register only the best per address. Weights: "
            "[mcp.ranking]; not with --exhaustive-mcp"
        ),
    )
    mcp.add_argument(
        "--no-reuse-existing-mcp",
        action="store_true",
        help=(
            "do not offer servers already registered here (.mcp.json, `claude mcp list`) as "
            "candidates for the address they point at. Default: mcp.discovery.reuse_existing (true)"
        ),
    )
    # Discovery needs no flag to work: every stage is a Claude subagent that
    # retries with what went wrong until it settles the stage or runs out of
    # ways to try, and the terminal says how each stage ended. The flag below is
    # for deciding *against* the agent, which is a choice no last resort can
    # make for you.
    mcp.add_argument(
        "--no-agent",
        action="store_true",
        help=(
            "traditional mode: deterministic probing and rule-based reasoning, no Claude "
            "subagent. Same stages and report; an unusual server is likelier to go unfound. "
            + " and ".join(f.flag for f in SETTING_FLAGS if f.agent_only)
            + " then have no effect"
        ),
    )
    _add_setting_flags(groups)
    _add_secret_flags(groups)

    add_common(
        sub.add_parser(
            "phases",
            help="list the available phases",
            description="List the registered phases, their numbers and whether each is enabled.",
        )
    )
    config_cmd = sub.add_parser(
        "config",
        help="show the effective configuration",
        description=(
            "Print the configuration after every layer has been merged and every "
            "${VAR} interpolated. Credentials are redacted. Exits 1 if KEY is not set."
        ),
    )
    add_common(config_cmd)
    config_cmd.add_argument(
        "key",
        nargs="?",
        help="dotted key or subtree to print, e.g. provision.vertex.region; omit for the whole document",
    )
    add_common(
        sub.add_parser(
            "palette",
            help="preview the console colour schemes",
            description=(
                "Print every colour scheme as a swatch, plain and bold, so the choice can "
                "be made by eye. Set the winner with --set console.palette=NAME, "
                f"{ENV_PREFIX}CONSOLE_PALETTE, or console.palette in a config file."
            ),
        )
    )
    add_common(
        sub.add_parser(
            "env",
            help="show the environment initialize-claude-ai has recorded",
            description=(
                "Print the binary paths and versions phase 1 recorded, plus the line to source so this shell can see them."
            ),
        )
    )

    return parser


def _add_secret_flags(groups: dict[str, argparse._ArgumentGroup]) -> None:
    """Declare both forms of every credential in :data:`SECRET_FLAGS`."""
    for flag in SECRET_FLAGS:
        group = groups[flag.group]
        group.add_argument(
            flag.ref,
            metavar="REF",
            dest=flag.ref_dest,
            help=f"{flag.help}, read from REF: env:NAME, file:PATH or keyring:svc/user",
        )
        group.add_argument(
            flag.plain,
            metavar="VALUE",
            dest=flag.dest,
            help=f"{flag.help}, given as the value itself (visible in ps; refused by --secure)",
        )


def _phase_groups(run: argparse.ArgumentParser) -> dict[str, argparse._ArgumentGroup]:
    """One argument group per registered phase, keyed by its ``enable_key``.

    Titled from the phase itself -- its number and the slug ``--phases``
    accepts -- so a phase added, renumbered or renamed gets a matching heading
    without anyone editing this file.
    """
    return {
        cls.enable_key or cls.slug: run.add_argument_group(phase_group_title(cls), phase_group_description(cls))
        for cls in all_phases()
    }


def phase_group_title(cls: type) -> str:
    """``phase 4 (mcp) - MCP auto-discovery``: the number and slug ``--phases`` takes."""
    return f"phase {cls.number} ({cls.slug}) - {cls.title}"


def phase_group_description(cls: type) -> str:
    """The first sentence of the phase's own description, and when its flags apply."""
    summary = cls.description.split(". ", 1)[0].rstrip(".")
    lead = f"{summary}. " if summary else ""
    return f"{lead}These flags take effect only when the phase runs (--phases {cls.number} or {cls.slug})."


def _add_setting_flags(groups: dict[str, argparse._ArgumentGroup]) -> None:
    """Declare every flag in :data:`SETTING_FLAGS`, each under its phase.

    No ``nargs`` and no ``const``, so argparse refuses a bare flag; no default,
    so an absent flag is ``None`` and :func:`_setting_overrides` writes nothing.
    The group is the phase whose section the key lies in, or run control.
    """
    for flag in SETTING_FLAGS:
        group = groups.get(flag.section, groups["run"])
        group.add_argument(flag.flag, metavar=flag.metavar, type=flag.kind, dest=flag.dest, help=flag.help)


@lru_cache(maxsize=1)
def _options_taking_a_value() -> frozenset[str]:
    """Flags that consume the following token, e.g. ``--config FILE``.

    Read off the parser rather than listed here, so a new flag can never fall
    out of step with the scan below.
    """
    return frozenset(
        option for action in build_parser()._actions if action.nargs != 0 for option in action.option_strings
    )


def _normalise_argv(argv: Sequence[str]) -> list[str]:
    """Allow ``initialize-claude-ai --dry-run`` as shorthand for ``initialize-claude-ai run``.

    The scan stops at the first token that could be the subcommand.  A token
    that is really an option's value must not count as one, or
    ``--config x config http`` would be read as an argument-less ``run``.
    """
    args = list(argv)
    valued = _options_taking_a_value()
    expecting_value = False
    for token in args:
        if expecting_value:
            expecting_value = False
            continue
        if token in COMMANDS:
            return args
        if not token.startswith("-"):
            break
        if token in ("--version", "-h", "--help"):
            return args
        expecting_value = token in valued
    return [DEFAULT_COMMAND, *args]


# ----------------------------------------------------------------------------
# context assembly
# ----------------------------------------------------------------------------


def _overrides_from_args(args: argparse.Namespace) -> list[str]:
    """Map convenience flags onto configuration keys.

    Everything the CLI can change is a setting, so a flag and a config file entry
    are always the same thing -- there is no second source of truth.
    """
    mapping: list[tuple[str, object]] = [
        ("install.only", getattr(args, "install_only", None)),
        ("install.skip", getattr(args, "install_skip", None)),
        ("auth.backends", getattr(args, "auth_backends", None)),
        ("auth.google_cloud.account", getattr(args, "gcloud_user_account", None)),
        # One flag, both phases: signing in is not project-neutral (quota, API
        # enablement and Vertex access all attach to a project), so phase 2 gets
        # the same answer phase 3 provisions against. Two keys rather than one
        # shared key because a config file may still set them apart -- an
        # organisation that signs in through one project and bills another.
        ("auth.google_cloud.project", getattr(args, "gcloud_project", None)),
        (
            "auth.google_cloud.impersonate_service_account",
            getattr(args, "gcloud_service_account", None),
        ),
        ("auth.claude_oauth.account", getattr(args, "claude_user_account", None)),
        ("auth.browser.command", getattr(args, "browser", None)),
        ("auth.browser.input_method_fix", getattr(args, "browser_input_fix", None)),
        ("auth.browser.clean_profile", getattr(args, "browser_profile", None)),
        ("auth.signin.pty", getattr(args, "sign_in_capture", None)),
        ("provision.backend", getattr(args, "provision_backend", None)),
        # "0"/"1" rather than a store_true pair, because the setting behind it
        # has three states as far as the CLI is concerned: on, off, and "not
        # mentioned here, so whatever the config files said". A flag defaulting
        # to 1 in argparse would beat an env file that said 0.
        ("provision.vertex.enabled", getattr(args, "use_vertex", None)),
        ("provision.vertex.project", getattr(args, "gcloud_project", None)),  # and phase 2, above
        # Google Cloud only, as the name says. Bedrock's region is its own
        # coordinate in another cloud, set by provision.bedrock.region or
        # AWS_REGION; one flag cannot honestly name both.
        ("provision.vertex.region", getattr(args, "gcloud_region", None)),
        ("provision.settings.model", getattr(args, "default_model", None)),
        ("mcp.stages", getattr(args, "stages", None)),
        ("install.prefetch", getattr(args, "install_prefetch", None)),
        ("mcp.packages.provision_missing", getattr(args, "mcp_provision_missing", None)),
        ("mcp.trust.extra_ca", getattr(args, "mcp_trust_ca", None)),
        ("mcp.trust.pin_presented_chain", getattr(args, "mcp_trust_pin", None)),
    ]
    for tier in MODEL_TIER_FLAGS:
        pinned = getattr(args, f"default_{tier}_model", None)
        mapping += [(f"{section}.model_aliases.{tier}", pinned) for section in MODEL_ALIAS_SECTIONS]
    overrides = [f"{key}={value}" for key, value in mapping if value]
    overrides += _credential_overrides(args)

    key_file = getattr(args, "gcloud_service_account_file", None)
    if key_file:
        # Compiled to a file: reference rather than passed bare, so a path that
        # happens to contain a colon cannot be misread as some other scheme.
        overrides.append(f"auth.google_cloud.service_account_key_ref=file:{_absolute(key_file)}")

    if getattr(args, "do_cleanup", False):
        overrides.append("run.cleanup.enabled=true")
    if getattr(args, "no_cleanup", False):
        overrides.append("run.cleanup.enabled=false")
    if getattr(args, "force_install", False):
        overrides.append("install.force=true")
    if getattr(args, "break_dependencies", False):
        overrides.append("install.allow_break_system_packages=true")
    if getattr(args, "downgrade", False):
        overrides.append("install.downgrade=true")
    if getattr(args, "force_auth", False):
        overrides.append("auth.force=true")
    if getattr(args, "force_auth_with_reset", False):
        # Both, because clearing a credential and then not signing in again
        # would leave the machine worse off than it started.
        overrides.append("auth.force=true")
        overrides.append("auth.reset=true")
    if getattr(args, "headless", False):
        overrides.append("auth.browser_mode=headless")
    if getattr(args, "browser_loopback_redirect", False):
        overrides.append("auth.browser_mode=browser")
    if getattr(args, "no_agent", False):
        overrides.append("mcp.agentic=false")
    concurrency = getattr(args, "concurrency", None)
    if concurrency is not None:
        # Bare --concurrency is passed through as the keyword, not as a number
        # the parser made up: the phase resolves it from settings, which this
        # function runs before.
        overrides.append(f"mcp.discovery.target_concurrency={concurrency}")
    if getattr(args, "timeout", None):
        # Parsed here so "2h" fails at the command line with the accepted
        # spellings, rather than reaching a phase as an unreadable setting.
        overrides.append(f"run.deadline_seconds={parse_duration(args.timeout)}")
    overrides += _setting_overrides(args)
    if getattr(args, "exhaustive_mcp", False):
        overrides.append("mcp.exhaustive=true")
    if getattr(args, "mcp_recheck", None):
        overrides.append("mcp.recheck=true")
    if getattr(args, "no_reuse_existing_mcp", False):
        overrides.append("mcp.discovery.reuse_existing=false")
    if getattr(args, "the_best_mcp", False):
        # Only the intent is set here. Phase 4 widens the search itself, because
        # what has to change to make a comparison possible is its business.
        overrides.append("mcp.pick_best=true")
    if getattr(args, "log_level", None):
        overrides.append(f"logging.level={args.log_level}")
    if getattr(args, "log_file", None):
        overrides.append(f"paths.log_file={_absolute(args.log_file)}")
    if getattr(args, "no_log_file", False):
        # An empty path is how "record nothing" is already spelled in the
        # settings, so the flag and a config file say it the same way.
        overrides.append("paths.log_file=")
    if getattr(args, "secure", False):
        # Not only the flags below: an inline credential is refused whatever
        # layer it arrives from, so --secure means the same thing everywhere.
        overrides.append("secrets.allow_literal=false")
    return overrides


def _setting_overrides(args: argparse.Namespace) -> list[str]:
    """Compile every :data:`SETTING_FLAGS` flag that was given onto its setting.

    ``is not None`` rather than truthiness, so an out-of-bounds 0 is refused
    here instead of being mistaken for "not given" and quietly ignored.
    """
    overrides: list[str] = []
    for flag in SETTING_FLAGS:
        raw = getattr(args, flag.dest, None)
        if raw is None:
            continue
        value = flag.kind(raw)
        if not flag.accepts(value):
            raise ConfigError(f"{flag.flag} must be {flag.bound()}, not {raw}", remediation=flag.remediation)
        overrides.append(f"{flag.key}={value}")
    return overrides


def inert_setting_flags(args: argparse.Namespace, settings) -> list[str]:
    """Warnings for flags given in a run where nothing will read them.

    Not a refusal: ``--no-agent`` with an agent budget was accepted before and
    is harmless, and a script that passes both should keep working. But a
    number that changes nothing deserves one line, or the operator is left
    wondering why raising it did not help.
    """
    if settings.bool_("mcp.agentic", True):
        return []
    return [
        f"{flag.flag} has no effect: the phase-4 agent is off (--no-agent or mcp.agentic = "
        "false), and traditional mode has no attempts or turns to budget"
        for flag in SETTING_FLAGS
        if flag.agent_only and getattr(args, flag.dest, None) is not None
    ]


#: Flags that only one phase reads: ``dest -> (flag, that phase's enable_key)``.
PHASE_FLAGS = {
    "install_prefetch": ("--install-prefetch", "install"),
    "mcp_provision_missing": ("--mcp-provision-missing", "mcp"),
    "mcp_trust_ca": ("--mcp-trust-ca", "mcp"),
    "mcp_trust_pin": ("--mcp-trust-pin", "mcp"),
}


def inert_phase_flags(args: argparse.Namespace, phases: Sequence[type]) -> list[str]:
    """Warnings for a phase's flag given in a run that does not include that phase.

    Accepted all the same, like :func:`inert_setting_flags`: the setting is
    still applied, it just has nothing to act on in this run.
    """
    running = {cls.enable_key or cls.slug for cls in phases}
    by_key = {cls.enable_key or cls.slug: cls for cls in all_phases()}
    warnings: list[str] = []
    for dest, (flag, key) in PHASE_FLAGS.items():
        if getattr(args, dest, None) is None or key in running:
            continue
        owner = by_key.get(key)
        where = f"phase {owner.number} ({owner.slug})" if owner else key
        warnings.append(f"{flag} has no effect: {where} is not in this run (--phases)")
    return warnings


def _credential_overrides(args: argparse.Namespace) -> list[str]:
    """Compile the credential flags onto their settings.

    A value given inline becomes a ``literal:`` reference so that it travels the
    same path as every other credential.  It is registered for redaction here,
    before anything has had the chance to log it.
    """
    overrides: list[str] = []
    for flag in SECRET_FLAGS:
        reference = getattr(args, flag.ref_dest, None)
        value = getattr(args, flag.dest, None)
        if reference:
            overrides.append(f"{flag.key}={reference}")
        elif value:
            redact.register(value)
            overrides.append(f"{flag.key}=literal:{value}")
    return overrides


def _absolute(path: str) -> str:
    """Expand ``~`` and anchor a relative path, so it survives a later chdir."""
    return os.path.abspath(os.path.expanduser(str(path).strip()))


def check_service_account_flags(args: argparse.Namespace) -> str | None:
    """Refuse the two mutually exclusive ways to be a service account.

    They select different acquisition methods, and the key file wins the
    tie-break -- so the impersonation would be configured, reported as
    unsatisfied on every run, and never actually applied.  A phase that can
    never converge is worse than a rejected command line.
    """
    if getattr(args, "gcloud_service_account", None) and getattr(args, "gcloud_service_account_file", None):
        return (
            "--gcloud-service-account impersonates from your own credential and "
            "--gcloud-service-account-file authenticates as the key's owner; give one, not both"
        )
    return None


def check_credential_flags(args: argparse.Namespace) -> str | None:
    """Reject a contradictory or forbidden credential flag; ``None`` when fine."""
    secure = bool(getattr(args, "secure", False))
    for flag in SECRET_FLAGS:
        value = getattr(args, flag.dest, None)
        if not value:
            continue
        if getattr(args, flag.ref_dest, None):
            return f"{flag.plain} and {flag.ref} are alternatives; give one, not both"
        if secure:
            return (
                f"--secure refuses {flag.plain}, which puts the value in ps output and "
                f"the shell history; use {flag.ref} with env:NAME, file:PATH or keyring:svc/user"
            )
    return None


def _open_transcript(settings, date_format: str, argv: Sequence[str]) -> Transcript | None:
    """Open the run's log file and write its header, or return ``None``.

    The header goes in here rather than at the first line of output, so that a
    run which produces nothing -- refused by a later check, or dead on its first
    probe -- still leaves a dated entry saying it was attempted.
    """
    path = settings.path("paths.log_file")
    if not path:
        return None
    # Anchored now, against the directory the command was run from. The default
    # is relative on purpose; resolving it here means the banner and the summary
    # both name a path that can be pasted somewhere, and that a later chdir --
    # phase 1 unpacking an archive, say -- cannot move the file mid-run.
    transcript = Transcript.open(
        Path(_absolute(str(path))),
        max_bytes=settings.int_("logging.max_bytes", 5000000),
        backups=settings.int_("logging.backups", 3),
        date_format=date_format,
    )
    if transcript is not None:
        # The header records the arguments this call was given -- which is not
        # always sys.argv, since main() takes an argv -- and records them
        # redacted: a run that put a credential on the command line must not be
        # the reason it ends up on disk.
        transcript.start(PROGRAM, __version__, argv)
    return transcript


def build_context(args: argparse.Namespace) -> RunContext:
    """Assemble the one object every phase is handed.

    This is where the run acquires its settings, its console and log
    transcript, its process runner, its secret resolver, its detected platform
    and its report -- in that order, because each depends on the ones before
    it. Redaction is configured first of all, before any code path can log a
    value.
    """
    settings = load_settings(
        config_files=args.config,
        env_files=args.env_file,
        overrides=[*args.overrides, *_overrides_from_args(args)],
    )

    # Redaction is configured before anything else can log a value.
    redact.configure(settings.str_list("redaction.patterns"))

    date_format = settings.str_("logging.date_format", DEFAULT_DATE_FORMAT)
    transcript = _open_transcript(settings, date_format, getattr(args, "argv", None) or sys.argv[1:])
    # Built before the logger and bound to stderr, because that is the stream
    # the log goes to: the badge on a WARNING has to be decided by what stderr
    # can render, which is not always what stdout can. Nothing is written
    # through it -- the handler owns the writing -- so it needs no transcript.
    log_style = Console(
        stream=sys.stderr,
        color=False if args.no_color else None,
        glyph_set=settings.str_("console.glyphs", "auto"),
        palette=settings.str_("console.palette", "auto"),
    )
    setup_logging(
        level=settings.str_("logging.level", "info"),
        transcript=transcript,
        file_level=settings.str_("logging.file_level", "debug"),
        fmt=settings.str_("logging.format", "%(asctime)s %(levelname)-7s %(name)s: %(message)s"),
        date_format=date_format,
        stream=sys.stderr,
        style=log_style if settings.bool_("logging.badges", True) else None,
    )

    console = Console(
        stream=sys.stdout,
        color=False if args.no_color else None,
        quiet=args.quiet,
        json_mode=args.json,
        glyph_set=settings.str_("console.glyphs", "auto"),
        palette=settings.str_("console.palette", "auto"),
        width=settings.int_("console.width", 80),
        # Independent of --no-color: the frames are plain characters, and piping
        # to a file is a reason to drop the escape codes, not the structure.
        decorate=settings.bool_("console.decorate", True),
        show_progress=settings.bool_("console.progress", True),
        # The same clock and the same format as the log records underneath, so
        # the two halves of the transcript read as one timeline.
        timestamps=settings.bool_("console.timestamps", True),
        date_format=date_format,
        # The screen is a view of the run; the file is the record of it. Turning
        # the mirror off leaves an ordinary log of records behind instead.
        transcript=transcript if settings.bool_("logging.record_output", True) else None,
    )

    dry_run = bool(getattr(args, "dry_run", False))
    info = platform_detect.detect()

    state_dir = settings.path("paths.state_dir") or Path.home() / ".local/state/initialize-claude-ai"
    env = EnvStore(
        state_dir=state_dir,
        prefix=settings.str_("env.prefix", "INITIALIZE_CLAUDE_AI_"),
        passthrough=set(settings.str_list("env.passthrough")),
        dry_run=dry_run,
    ).load()
    env.apply()

    extra_paths = [os.path.expanduser(d) for d in settings.str_list("paths.extra_bin_dirs")]
    runner = Runner(
        dry_run=dry_run,
        default_timeout=settings.float_("run.command_timeout_seconds", 600.0),
        extra_paths=[*env.bin_dirs(), *extra_paths],
        stop_signals=settings.str_list("run.stop_signals", ["TERM", "KILL"]),
        stop_grace_seconds=settings.float_("run.stop_grace_seconds", 5.0),
        out_of_space_markers=settings.str_list("run.disk.out_of_space_markers", list(diskspace.OUT_OF_SPACE_MARKERS)),
    )
    secrets = SecretResolver(
        runner,
        allow_execution=settings.bool_("secrets.allow_execution", True),
        allow_prompt=settings.bool_("secrets.allow_prompt", True) and sys.stdin.isatty(),
        allow_literal=settings.bool_("secrets.allow_literal", True),
        strict_file_permissions=settings.bool_("secrets.strict_file_permissions", True),
        command_timeout=settings.float_("secrets.command_timeout_seconds", 60.0),
        gcloud_commands={
            name: settings.str_list(f"auth.google_cloud.commands.{name}") for name in ("print_token", "print_adc_token")
        },
        gcloud_timeouts={
            name: settings.float_(f"auth.google_cloud.command_timeout_seconds.{name}")
            for name in ("print_token", "print_adc_token")
        },
    )
    report = RunReport(
        version=__version__,
        dry_run=dry_run,
        platform=info.to_dict(),
        config_sources=list(settings.sources),
    )

    ctx = RunContext(
        settings=settings,
        console=console,
        runner=runner,
        secrets=secrets,
        platform=info,
        env=env,
        report=report,
        dry_run=dry_run,
        auto_agree=bool(getattr(args, "auto_agree", False)),
        # Started here rather than at the first phase, so the ceiling covers the
        # whole run the way the operator means it -- including the time spent
        # detecting the platform and loading the environment above.
        deadline=Deadline(limit=max(0.0, settings.float_("run.deadline_seconds", 0.0))),
    )

    # Values that are selections rather than settings travel in the state bag.
    # The raw strings go through as typed: phase 4 owns the one parser that turns
    # an address into a URL, so a --mcp-server value and an mcp.servers entry
    # cannot end up being read two different ways.
    if getattr(args, "mcp_server", None):
        ctx.state["mcp_servers"] = list(args.mcp_server)
    if getattr(args, "mcp_recheck", None):
        ctx.state["mcp_servers"] = [*ctx.state.get("mcp_servers", []), *args.mcp_recheck]
    if getattr(args, "install_only", None):
        ctx.state["install_only"] = _split(args.install_only)
    if getattr(args, "install_skip", None):
        ctx.state["install_skip"] = _split(args.install_skip)
    if getattr(args, "auth_backends", None):
        ctx.state["auth_backends"] = _split(args.auth_backends)
    return ctx


def _split(value: str | None) -> list[str]:
    return [part.strip() for part in str(value or "").split(",") if part.strip()]


# ----------------------------------------------------------------------------
# commands
# ----------------------------------------------------------------------------


def command_run(args: argparse.Namespace) -> int:
    """``run`` -- the default command: execute the selected phases in order.

    Prints the banner and the effective invocation first, so the operator can
    see what the run was told before it acts on any of it, then runs each
    phase, then the summary. The exit code comes from the report, not from the
    last phase.
    """
    ctx = build_context(args)
    tokens = _split(args.phases) if args.phases else ctx.settings.str_list("run.default_phases", ["all"])
    phases = select(tokens)

    _banner(ctx, phases)
    # Before the first phase, and recorded in the report: what this run was told,
    # and by which layer. A phase acting on a value nobody remembered setting is
    # the most common way a run surprises its operator.
    ctx.report.invocation = _invocation_facts(ctx, args, phases)
    _show_invocation(ctx, ctx.report.invocation)
    for warning in [*inert_setting_flags(args, ctx.settings), *inert_phase_flags(args, phases)]:
        log.warning(warning)

    continue_on_failure = args.ignore_failure or ctx.settings.bool_("run.continue_on_failure", False)
    continue_on_partial = not args.no_partial_success and ctx.settings.bool_("run.continue_on_partial", True)

    for index, phase_cls in enumerate(phases):
        # The coarsest admission point: a phase not yet begun is not begun. A
        # phase already running is left alone -- the deadline stops the run
        # taking on new work, it does not interrupt work in progress.
        if ctx.deadline.expired:
            _out_of_time(ctx, phases[index:])
            break
        ctx.deadline.phase = phase_cls.slug
        short = _disk_preflight(ctx, phase_cls)
        if short is not None:
            ctx.report.add(short)
            _abort(ctx, short, phases[index + 1 :])
            break
        result: PhaseResult = phase_cls(ctx).run()
        ctx.report.add(result)
        if ctx.state.get("out_of_space"):
            # A full disk is not a phase's failure to retry or step past: every
            # later phase writes too, and would only fail in ways that look like
            # something else. Stop, and say it once.
            _out_of_space(ctx, result, phases[index + 1 :])
            break
        if result.status == Status.FAILED and not continue_on_failure:
            _abort(ctx, result, phases[index + 1 :])
            break
        if result.status == Status.PARTIAL and not continue_on_partial:
            _abort(ctx, result, phases[index + 1 :])
            break

    # Whatever the phases recorded should outlive the process.
    try:
        ctx.env.save()
    except OSError as exc:
        if not diskspace.is_out_of_space(exc, ctx.settings):
            raise
        ctx.console.status(Status.FAILED, "environment", f"not saved: {exc}")
    ctx.report.environment = ctx.env.summary()
    _cleanup(ctx)
    return _finish(ctx, args)


def _disk_preflight(ctx: RunContext, phase_cls: type) -> PhaseResult | None:
    """Stop before ``phase_cls`` when a filesystem the run writes to is short of space (``run.disk``).

    A run started on a full disk, reported OK for installs that had just
    failed for want of room, and broke in phase 4 where it could no longer write
    its own log. Checked before every phase, because a run fills its caches.
    """
    if not ctx.settings.bool_("run.disk.check", True):
        return None
    shortfalls = diskspace.check(ctx)
    if not shortfalls:
        return None
    result = PhaseResult(phase=phase_cls.slug, title=phase_cls.title)
    result.add(
        "free space",
        Status.FAILED,
        "; ".join(s.words() for s in shortfalls),
        remediation=diskspace.remediation(ctx, shortfalls),
        error="out_of_space",
    )
    result.fatal = True
    result.finalize()
    ctx.console.write("")
    ctx.console.status(
        Status.FAILED, "free space", f"phase {phase_cls.slug} not started: " + "; ".join(s.words() for s in shortfalls)
    )
    for line in diskspace.remediation(ctx, shortfalls):
        ctx.console.bullet(line, indent=4)
    return result


def _out_of_space(ctx: RunContext, result: PhaseResult, remaining: Sequence[type]) -> None:
    """The disk filled while ``result``'s phase ran: stop, name it once, and say what to free."""
    reason = str(ctx.state.get("out_of_space") or "no space left on device")
    ctx.console.write("")
    ctx.console.status(Status.FAILED, "free space", f"the disk filled during phase {result.phase}: {reason}")
    for line in diskspace.remediation(ctx, diskspace.check(ctx)):
        ctx.console.bullet(line, indent=4)
    for phase_cls in remaining:
        skipped = PhaseResult(phase=phase_cls.slug, title=phase_cls.title, status=Status.SKIPPED)
        skipped.skipped_reason = "the disk is full"
        ctx.report.add(skipped)


def _cleanup(ctx: RunContext) -> None:
    """Remove this program's own leftovers nothing uses (``--do-cleanup``, on by default; ``run.cleanup``)."""
    if not ctx.settings.bool_("run.cleanup.enabled", True):
        return
    try:
        done = diskspace.cleanup(ctx)
    except Exception as exc:  # noqa: BLE001 - tidying up never fails a run
        log.warning("cleanup did not finish: %s", exc)
        return
    if done.removed or done.errors:
        ctx.console.status(
            Status.CHANGED if done.removed else Status.PARTIAL,
            "cleanup",
            f"removed {len(done.removed)} leftover(s) of earlier runs, {done.freed_mb} MB"
            + (f"; kept {len(done.kept)} an MCP entry uses" if done.kept else "")
            + (f"; {len(done.errors)} could not be removed" if done.errors else ""),
        )
    for path in done.removed:
        log.info("cleanup: removed %s", path)
    for path in done.unproven:
        log.info("cleanup: left %s -- named like this program's leftovers, but nothing shows it made it", path)
    for problem in done.errors:
        log.warning("cleanup: %s", problem)


def command_phases(args: argparse.Namespace) -> int:
    """``phases`` -- list the phases and whether configuration enables each."""
    ctx = build_context(args)
    rows = []
    for phase_cls in all_phases():
        enabled = ctx.settings.bool_(f"{phase_cls.enable_key}.enabled", True) if phase_cls.enable_key else True
        rows.append([str(phase_cls.number), phase_cls.slug, phase_cls.title, "yes" if enabled else "no"])
    if args.json:
        ctx.console.json({"phases": [dict(zip(("number", "slug", "title", "enabled"), r)) for r in rows]})
        return 0
    ctx.console.table(rows, headers=["#", "name", "title", "enabled"])
    return 0


def command_palette(args: argparse.Namespace) -> int:
    """Print every palette, so the choice can be made by eye rather than by name.

    Colour is the one setting this program cannot pick well on someone else's
    behalf: it depends on the terminal theme, the ambient light and the reader.
    So rather than argue for a default, show all of them and name the setting.
    """
    ctx = build_context(args)
    # Default first -- it is the one most readers are deciding whether to keep --
    # then the rest in declaration order, which runs loudest to quietest.
    names = [DEFAULT_PALETTE, *(n for n in PALETTES if n != DEFAULT_PALETTE)]
    aliases = {name: sorted(a for a, t in PALETTE_ALIASES.items() if t == name) for name in names}
    if args.json:
        ctx.console.json(
            {
                "default": DEFAULT_PALETTE,
                "palettes": names,
                "aliases": {a: t for a, t in sorted(PALETTE_ALIASES.items())},
                "badges": {role: mark.label for role, mark in BADGES.items()},
            }
        )
        return 0

    console = ctx.console
    # In the order the reports use them, so the swatch reads like a real summary.
    hues = (
        ("green", "passed"),
        ("yellow", "needs attention"),
        ("red", "failed"),
        ("cyan", "changed"),
        ("blue", "information"),
        ("magenta", "dry run"),
        ("dim", "skipped, and every detail line"),
    )
    for name in names:
        # One Console per palette: the swatch has to be drawn with the codes it
        # is advertising, not with whatever the ambient configuration resolved to.
        sample = Console(stream=sys.stdout, color=console.color, palette=name, glyph_set=console.glyph_set)
        suffix = {
            DEFAULT_PALETTE: "  (default where the terminal can render it)",
            LIGHT_PALETTE: "  (default instead, on a light background)",
        }.get(name, "")
        if aliases[name]:
            suffix += f"  [also: {', '.join(aliases[name])}]"
        console.write("")
        console.write(console.style(f"{name}{suffix}", "bold"))
        for hue, meaning in hues:
            console.write(
                f"  {sample.style(f'{meaning:<32}', hue)}"
                f"{sample.style('the quick brown fox', hue, 'bold')}"
                f"{sample.style(' jumps over the lazy dog', hue)}"
            )
        # The swatch above is the hues; this is what the reader actually sees,
        # since no line in a report is a bare colour -- it is a sign, a word and
        # a colour together, and the three have to be judged together.
        for row in BADGE_ROWS:
            console.write("  " + " ".join(sample.badge(role) for role in row))

    console.write("")
    console.write("Choose one with:")
    for name in names:
        console.write(f"  {PROGRAM} run --set console.palette={name}")
    console.write("")
    console.write("Or make it permanent:")
    console.write(f"  export {ENV_PREFIX}CONSOLE_PALETTE={DEFAULT_PALETTE}")
    console.write("... or set console.palette in initialize-claude-ai.toml.")
    return 0


def command_config(args: argparse.Namespace) -> int:
    """Print the merged configuration -- always as data, since that is its use."""
    ctx = build_context(args)
    # Printed rather than written through the console because the answer is data
    # for a shell to consume, and must stay on stdout even under --quiet. It is
    # still recorded: "what the run printed" includes what it was asked to print.
    if args.key:
        value = ctx.settings.get(args.key)
        if value is None:
            missing = f"{args.key} is not set"
            record(missing)
            print(missing, file=sys.stderr)
            return 1
        rendered = redact.redact(value if isinstance(value, str) else _dumps(value))
    else:
        rendered = redact.redact(_dumps({"sources": ctx.settings.sources, "config": ctx.settings.as_dict()}))
    record(rendered)
    print(rendered)
    return 0


def command_env(args: argparse.Namespace) -> int:
    """``env`` -- show what earlier runs recorded about this machine.

    Values are printed through the same redaction as everything else, so a
    variable whose name looks like a credential is reported as present rather
    than shown.
    """
    ctx = build_context(args)
    summary = ctx.env.summary()
    if args.json:
        ctx.console.json({"state_dir": str(ctx.env.state_dir), "variables": summary})
        return 0
    if not summary:
        ctx.console.write("No environment has been recorded yet. Run phase 1 first.")
        return 0
    for name, value in summary.items():
        ctx.console.kv(name, value, width=40)
    ctx.console.write("")
    ctx.console.write(f"  {ctx.env.shell_hint()}")
    return 0


# ----------------------------------------------------------------------------
# output helpers
# ----------------------------------------------------------------------------


def _banner(ctx: RunContext, phases: Sequence[type]) -> None:
    console = ctx.console
    rows = [
        ("Platform", ctx.platform.pretty_name or ctx.platform.system),
        ("Packages via", ctx.platform.package_manager or "none detected"),
        ("Phases", ", ".join(f"{p.number}:{p.slug}" for p in phases) or "none selected"),
        ("Mode", "DRY RUN -- nothing will be changed" if ctx.dry_run else "live"),
        ("State", str(ctx.state_dir)),
        ("Log", _log_location()),
        ("Config from", ", ".join(str(s) for s in ctx.settings.sources) or "built-in defaults"),
    ]
    pad = max([LABEL_WIDTH, *(len(label) + 2 for label, _ in rows)])
    console.write("")
    console.box(
        [Line(f"{label.ljust(pad)}{value}", hang=pad) for label, value in rows],
        title=f"{PROGRAM} {__version__}",
        frame_styles=BANNER_FRAME_STYLE,
    )


def _log_location() -> str:
    """Where this run is being recorded, in the words the banner uses."""
    transcript = active_transcript()
    return str(transcript.path) if transcript else "not recorded (--no-log-file)"


# ----------------------------------------------------------------------------
# what this run is actually configured with
#
# Every value the program acts on arrives from one of four layers -- the command
# line, the environment, a config or env file, or a packaged default -- and once
# the run is under way there is nothing on screen that says which one won. A
# phase that provisions the wrong region, skips a component or signs in to the
# wrong project is nearly always a layer somebody forgot was set, and the whole
# diagnosis is one block of text printed before the first phase.
#
# What is *not* printed: the merged document in full (hundreds of keys, and one
# still holding its packaged default explains nothing -- `config` prints it all),
# and any value that looks like a credential, which is masked here exactly as it
# is everywhere else this program writes.
# ----------------------------------------------------------------------------


#: Namespace attributes that are not options: bookkeeping put there by main().
_NOT_AN_OPTION = frozenset({"command", "argv"})


@lru_cache(maxsize=1)
def _flag_for_dest() -> dict[str, str]:
    """``dest -> flag``, read off the parser so no flag can go unnamed here.

    The longest spelling, because that is the documented one -- ``--dry-run``
    rather than ``-n``, and ``--gcloud-project`` rather than its alias.
    """
    out: dict[str, str] = {}

    def walk(parser: argparse.ArgumentParser) -> None:
        for action in parser._actions:
            if action.option_strings:
                out.setdefault(action.dest, max(action.option_strings, key=len))
            if isinstance(action, argparse._SubParsersAction):
                for sub in action.choices.values():
                    walk(sub)

    walk(build_parser())
    return out


def _render(value: object) -> str:
    """One option or setting value, as a line of a report rather than a repr."""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (list, tuple)):
        return ", ".join(str(item) for item in value) or "(empty)"
    # A setting deliberately emptied -- --no-log-file does exactly that -- would
    # otherwise print as a gap the reader has to interpret.
    return str(value) if str(value) != "" else "(empty)"


def _invocation_facts(ctx: RunContext, args: argparse.Namespace, phases: Sequence[type]) -> dict[str, object]:
    """Everything this run was told, gathered once for the screen and the report."""
    settings = ctx.settings
    argv = list(getattr(args, "argv", None) or sys.argv[1:])
    flags = _flag_for_dest()

    given: dict[str, str] = {}
    for dest in sorted(vars(args)):
        if dest in _NOT_AN_OPTION:
            continue
        value = getattr(args, dest)
        # An option that was not given is not a fact about this run. Nearly every
        # flag defaults to None precisely so that the layers underneath get their
        # say, so "not None" is the same question as "was it given".
        if value in (None, False, "", [], ()):
            continue
        name = flags.get(dest, f"--{dest.replace('_', '-')}")
        given[name] = _render(value)

    environment = {
        name: value
        for name, value in sorted(settings.environ.items())
        if any(fnmatch.fnmatchcase(name, p) for p in settings.str_list("run.invocation.env_patterns"))
    }

    return {
        "command_line": " ".join(shlex.quote(token) for token in [PROGRAM, *redact.redact_argv(argv)]),
        "phases": [f"{p.number}:{p.slug}" for p in phases],
        "dry_run": ctx.dry_run,
        # Only when they were asked for. A run with neither is a run in which
        # these do not exist, and saying "deadline: none, scale: 1.0" on every
        # invocation would be two lines of nothing on every screen.
        **({"deadline": format_duration(ctx.deadline.limit)} if ctx.deadline.set else {}),
        **({"timeout_scale": settings.timeout_scale()} if settings.timeout_scale() != 1.0 else {}),
        "options": redact.redact_mapping(given),
        "options_given": len(given),
        "options_available": len(flags),
        # Masked by the setting's own name, through the same rule that masks a
        # mapping anywhere else: a token that was written into a config file is
        # a credential on this screen exactly as it is in the report. The field
        # is "setting" rather than "key" for the same reason -- "key" is one of
        # the names that rule masks, and a dotted setting name is not a secret.
        "settings": [
            {
                "setting": key,
                "value": redact.redact_mapping({key: _render(value)})[key],
                "origin": origin,
            }
            for key, value, origin in settings.overrides(ignore_env=settings.str_list("run.invocation.ignore_env"))
        ],
        "environment": redact.redact_mapping(environment),
        # Named, not valued: these are this program's own last answers, handed
        # back to it by the Claude Code that launched it, and the surprise they
        # cause is entirely about *which* names are in play. See demote_echo.
        "demoted": sorted(settings.demoted),
        "config_sources": list(settings.sources),
    }


def _origin_phrase(origin: str) -> str:
    """``env:CLOUD_ML_REGION`` -> ``from CLOUD_ML_REGION``, for a human column."""
    kind, _, rest = origin.partition(":")
    if kind == "env":
        return "from " + ", ".join(rest.split(","))
    if kind == "cli":
        return "from the command line"
    if kind in ("file", "envfile"):
        return f"from {rest}"
    return f"from {origin}"


def _show_invocation(ctx: RunContext, facts: dict[str, object]) -> None:
    """Print what the run is configured with, before it changes anything."""
    if not ctx.settings.bool_("run.invocation.show", True):
        return
    limit = max(1, ctx.settings.int_("run.invocation.max_rows", 24))
    lines: list[Line] = []

    def section(title: str) -> None:
        if lines:
            lines.append(Line(""))
        lines.append(Line(title, ("bold",)))

    def rows(pairs: Sequence[tuple[str, str]], empty: str) -> None:
        if not pairs:
            lines.append(Line(f"  {empty}", ("dim",), hang=2))
            return
        # Aligned on the widest name. A value that will not fit what is left of
        # the line goes underneath its name instead of being wrapped inside the
        # narrow column -- these are paths and project ids, read character by
        # character, and a word broken across two lines is read wrong.
        column = max(len(label) for label, _ in pairs[:limit]) + 2
        room = ctx.console.width - 4
        for label, value in pairs[:limit]:
            if column + len(value) + 4 > room and len(value) + 6 <= room:
                lines.append(Line(f"  {label}", hang=2))
                lines.append(Line(f"      {value}", hang=6))
                continue
            lines.append(Line(f"  {label.ljust(column)}  {value}", hang=column + 4))
        if len(pairs) > limit:
            lines.append(Line(f"  ... and {len(pairs) - limit} more", ("dim",), hang=2))

    section("Command line")
    lines.append(Line(f"  {facts['command_line']}", hang=2))

    options = dict(facts["options"])  # type: ignore[arg-type]
    section(f"Options given ({facts['options_given']} of {facts['options_available']})")
    rows(
        sorted(options.items()),
        "none -- every value below comes from the configuration layers",
    )

    changed = list(facts["settings"])  # type: ignore[arg-type]
    section(f"Settings this host decides ({len(changed)})")
    rows(
        [(str(item["setting"]), f"{item['value']}   {_origin_phrase(str(item['origin']))}") for item in changed],
        "none -- every setting is the packaged default",
    )

    environment = dict(facts["environment"])  # type: ignore[arg-type]
    section(f"Environment this run reads ({len(environment)})")
    rows(sorted(environment.items()), "none of the variables this program reads is set")

    demoted = list(facts["demoted"])  # type: ignore[arg-type]
    if demoted:
        lines.append(
            Line(
                f"  {len(demoted)} ignored as an echo of the settings file this run rewrites: "
                + ", ".join(str(name) for name in demoted),
                ("dim",),
                hang=2,
            )
        )

    ctx.console.write("")
    ctx.console.box(
        lines,
        title="HOW THIS RUN IS CONFIGURED",
        footer=[
            Line(f"The merged configuration in full:  {PROGRAM} config", ("dim",)),
            Line(f"Every option and every variable:   {PROGRAM} run --help", ("dim",)),
        ],
        frame_styles=BANNER_FRAME_STYLE,
    )


def _out_of_time(ctx: RunContext, remaining: Sequence[type]) -> None:
    """The deadline expired between phases: record what will not be attempted.

    Every remaining phase is reported as skipped, with the ceiling and the
    elapsed time named, so the transcript answers "why did it stop here" without
    anybody having to remember what they typed.
    """
    if not remaining:
        return
    names = ", ".join(p.slug for p in remaining)
    ctx.deadline.note_skipped(*(p.slug for p in remaining))
    ctx.console.write("")
    ctx.console.status(
        Status.SKIPPED,
        f"out of time after {format_duration(ctx.deadline.elapsed)}",
        f"--timeout was {format_duration(ctx.deadline.limit)}; not run: {names}",
    )
    reason = (
        f"the run reached its --timeout of {format_duration(ctx.deadline.limit)} "
        f"after {format_duration(ctx.deadline.elapsed)}"
    )
    for phase_cls in remaining:
        skipped = PhaseResult(phase=phase_cls.slug, title=phase_cls.title, status=Status.SKIPPED)
        skipped.skipped_reason = reason
        ctx.report.add(skipped)


def _abort(ctx: RunContext, result: PhaseResult, remaining: Sequence[type]) -> None:
    if not remaining:
        return
    names = ", ".join(p.slug for p in remaining)
    # Name the flag that actually governs *this* stop; the two are not
    # interchangeable and pointing at the wrong one sends the operator in circles.
    hint = (
        "use --ignore-failure to override"
        if result.status == Status.FAILED
        else "drop --no-partial-success to keep going"
    )
    ctx.console.write("")
    ctx.console.status(
        Status.SKIPPED,
        f"stopping after phase {result.phase}",
        f"not run: {names} ({hint})",
    )
    for phase_cls in remaining:
        skipped = PhaseResult(phase=phase_cls.slug, title=phase_cls.title, status=Status.SKIPPED)
        skipped.skipped_reason = f"an earlier phase reported {result.status}"
        ctx.report.add(skipped)


def _finish(ctx: RunContext, args: argparse.Namespace) -> int:
    payload = ctx.report.to_dict()
    if args.json:
        rendered = redact.redact(_dumps(payload))
        # The report is output too, and in --json mode it is the *only* output,
        # so a log missing it would be a log of a run that appeared to say nothing.
        record(rendered)
        print(rendered)
    if args.report:
        path = Path(args.report).expanduser()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            write_private(path, redact.redact(_dumps(payload)) + "\n")
            log.info("report written to %s", path)
        except OSError as exc:
            log.error("cannot write report to %s: %s", path, exc)

    if not args.json:
        _run_summary(ctx)

    return ctx.report.exit_code(partial_is_failure=ctx.settings.bool_("run.partial_is_failure", False))


def _run_summary(ctx: RunContext) -> None:
    """One table for the whole run: a phase per row, the verdict underneath.

    Deliberately the same shape as the per-phase summary, so that reading one
    teaches you how to read the other.
    """
    console = ctx.console
    glyphs = console.glyphs
    phases = ctx.report.phases
    lines: list[Line] = []
    total = 0.0

    # Size the verdict column to the widest word actually present rather than to
    # the widest possible one: an all-PASSED run should not be padded out to fit
    # "PASSED WITH CHANGES".
    verdicts = [verdict_of(p.status) for p in phases]
    column = max([len(v) for v in verdicts] or [0]) + 2

    for phase, verdict in zip(phases, verdicts):
        # A phase that worked through a list is counted on the list here too, so
        # this row and that phase's own summary cannot disagree about how well
        # it went: "10 server(s) passed" in both, never 58 in one and 10 in the
        # other.
        grouped = phase.groups()
        tally = phase.group_tally() if grouped else phase.tally()
        unit = f" {phase.group_noun}(s)" if grouped else ""
        # The sign comes from the badge table, so a phase that ran PARTIAL is the
        # same triangle here as it was on the step that made it partial.
        mark = glyphs[glyph_for(phase.status) if phase.steps else glyph_for(Status.SKIPPED)]
        counted = (
            phase.skipped_reason
            if phase.skipped_reason and not phase.steps
            else f"{tally['passed']}{unit} passed, {tally['skipped']} skipped, {tally['failed']} failed"
        )
        total += phase.duration
        head = f"{mark} {str(_number_of(phase.phase)).ljust(2)} {phase.phase.ljust(10)} {verdict.ljust(column)}"
        lines.append(Line(f"{head}{counted}", styles_for(phase.status), hang=len(head)))

    status = ctx.report.status
    sep = f"  {glyphs['sep']}  "
    footer: list[Line] = [
        Line(
            f"OVERALL RESULT: {verdict_of(status)}",
            (*styles_for(status, ("bold",)), "bold"),
        ),
        Line(
            sep.join(
                [
                    f"{len(phases)} phase(s) run",
                    format_duration(total),
                    *(["dry run"] if ctx.dry_run else []),
                ]
            ),
            ("dim",),
        ),
    ]

    footer.extend(_safety_footer(ctx))

    failed = [p for p in phases if p.status in BAD]
    if failed:
        numbers = ",".join(str(_number_of(p.phase)) for p in failed)
        footer.append(Line(""))
        footer.append(Line("Re-run just what did not work:", ("bold",)))
        footer.append(Line(f"  {glyphs['sep']} {PROGRAM} run --phases {numbers}", hang=4))

    transcript = active_transcript()
    if transcript is not None:
        # Last line of the run, because this is the one worth copying out of the
        # terminal: everything above scrolls away, and the file does not.
        footer.append(Line(""))
        footer.append(Line(f"Full output recorded in {transcript.path}", ("dim",), hang=2))

    console.write("")
    console.box(
        lines,
        title="Run summary",
        footer=footer,
        # Same as the per-phase boxes: the verdict is in the frame as well as in
        # the words, so the shape of the run is readable before it is read.
        frame_styles=FRAME_STYLE.get(status, ("dim",)),
    )


def _safety_footer(ctx: RunContext) -> list[Line]:
    """The run-level answer to "is it safe to go ahead?", and what qualifies it.

    The per-phase boxes already carry their own assertion, but they scroll away.
    This is the one the operator reads last and quotes to whoever asked, so it
    repeats the caveats rather than referring back to them.
    """
    glyphs = ctx.console.glyphs
    phases = ctx.report.phases
    level = ctx.report.safety
    color, marker = SAFETY_STYLE.get(level, ("bold", "sep"))
    notable = [p for p in phases if p.safety != Safety.SAFE]

    lines = [Line(""), Line(f"{glyphs[marker]} {SAFETY_LABEL[level]}", (color, "bold"))]
    if not notable:
        lines.append(
            Line(
                "  Every phase did what it set out to do and verified the result. Nothing "
                "in this run needs a second look.",
                (color,),
                hang=2,
            )
        )
        return lines

    verb = "needs" if len(notable) == 1 else "need"
    lines.append(
        Line(
            f"  {len(notable)} of {len(phases)} phase(s) {verb} reading before you rely on them:",
            (color,),
            hang=2,
        )
    )
    for phase in notable:
        phase_color, phase_marker = SAFETY_STYLE.get(phase.safety, ("bold", "sep"))
        # Header and reason on separate lines rather than one hanging-indented
        # row: a deep indent under "phase 3 provision:" wastes half the width.
        lines.append(
            Line(
                f"  {glyphs[phase_marker]} phase {_number_of(phase.phase)} {phase.phase}",
                (phase_color, "bold"),
                hang=4,
            )
        )
        lines.append(Line(f"      {phase.safety_reason}", (phase_color,), hang=6))
    return lines


def _number_of(slug: str) -> int:
    for phase_cls in all_phases():
        if phase_cls.slug == slug:
            return phase_cls.number
    return 0


def _dumps(payload: object) -> str:
    import json

    return json.dumps(payload, indent=2, default=str)


# ----------------------------------------------------------------------------
# entry point
# ----------------------------------------------------------------------------

COMMANDS = {
    "run": command_run,
    "phases": command_phases,
    "config": command_config,
    "palette": command_palette,
    "env": command_env,
}


def main(argv: Sequence[str] | None = None) -> int:
    """Parse the command line, run the command, and turn anything raised into
    an exit code.

    ``argv`` is accepted so the CLI can be driven from a test or another
    program without going through ``sys.argv``; the original words are kept on
    the namespace so the log can record the command as it was actually typed.
    """
    parser = build_parser()
    given = list(argv if argv is not None else sys.argv[1:])
    args = parser.parse_args(_normalise_argv(given))
    # Kept so the log file can record the command that was actually run rather
    # than the process's argv, which is not the same thing for an embedded call.
    args.argv = given
    for name, default in COMMON_DEFAULTS.items():
        if not hasattr(args, name):
            setattr(args, name, list(default) if isinstance(default, list) else default)
    for check in (check_credential_flags, check_service_account_flags):
        complaint = check(args)
        if complaint:
            parser.error(complaint)  # exits 2, like any other refused command line
    handler = COMMANDS.get(args.command or DEFAULT_COMMAND)
    if handler is None:  # pragma: no cover - argparse rejects unknown commands
        parser.print_help()
        return 2

    code = 1
    try:
        code = handler(args)
        return code
    except InitializeClaudeAiError as exc:
        # Written to the record as well as to stderr: the reason a run stopped
        # is the single most useful line the file can hold.
        for line in (
            f"{PROGRAM}: {redact.redact(exc.message)}",
            *([f"  {redact.redact(exc.remediation)}"] if exc.remediation else []),
        ):
            record(line)
            print(line, file=sys.stderr)
        code = 1
        return code
    except KeyboardInterrupt:
        record(f"{PROGRAM}: interrupted")
        print(f"\n{PROGRAM}: interrupted", file=sys.stderr)
        code = 130
        return code
    except BrokenPipeError:  # pragma: no cover - `| head`
        code = 0
        return code
    finally:
        # Closes the handle and stamps the outcome, on every path out of here,
        # so a killed or crashed run is still bracketed in the file.
        close_transcript(code)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
