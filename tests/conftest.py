"""Shared fixtures.

Every test here runs offline and touches nothing outside ``tmp_path``: the
fixtures point HOME, the state directory and the config search path at a
sandbox, so a test can never read the developer's real ``~/.claude``.
"""

from __future__ import annotations

import functools
import logging
import os
import re
from pathlib import Path

import pytest

from initialize_claude_ai.core import config as config_mod
from initialize_claude_ai.core import trust_store
from initialize_claude_ai.core.config import ENV_PREFIX, load_settings
from initialize_claude_ai.core.context import RunContext
from initialize_claude_ai.core.envfile import EnvStore
from initialize_claude_ai.core.logging_setup import LOGGER_NAME, Console, close_transcript
from initialize_claude_ai.core.platform_detect import detect
from initialize_claude_ai.core.proc import Runner
from initialize_claude_ai.core.report import RunReport
from initialize_claude_ai.core.secrets import SecretResolver
from initialize_claude_ai.mcp_auto_discovery import address, grounding, secret_policy


@pytest.fixture(autouse=True)
def _pristine_logger():
    """Undo whatever ``setup_logging`` did to the package logger.

    It is a CLI logger, so it rightly claims its own handlers and stops
    propagating -- but a test that calls ``cli.main`` would otherwise leave
    that state (and a handler bound to an already-closed capture stream)
    behind for every later test, including the ones that read ``caplog``.
    """
    logger = logging.getLogger(LOGGER_NAME)
    handlers, propagate, level = list(logger.handlers), logger.propagate, logger.level
    yield
    # A test that builds a context without going through ``main`` never reaches
    # the close in its ``finally``, and would leave the file handle open.
    close_transcript()
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
    for handler in handlers:
        logger.addHandler(handler)
    logger.propagate, logger.level = propagate, level


@pytest.fixture(autouse=True)
def _pristine_local_zones():
    """Undo any ``configure_local_zones`` call, for the same reason as above.

    Which DNS zones count as "our own network" is process-wide state, so a test
    that declares ``.corp`` local would otherwise change what every later test
    thinks about cleartext, bearer tokens and port scanning.
    """
    before = address.local_zones()
    yield
    address._ZONES = before


@pytest.fixture(autouse=True)
def _pristine_public_suffixes():
    """Undo any ``configure_suffixes`` call: which suffixes are public is process-wide too."""
    before = grounding._SUFFIXES
    yield
    grounding._SUFFIXES = before


@pytest.fixture(autouse=True)
def _pristine_secret_policy():
    """Undo any ``secret_policy.configure`` call -- the rule is process-wide state."""
    names = (
        "SENSITIVE_NAME",
        "SELECTOR_NAME",
        "INLINE_CREDENTIAL",
        "NOT_A_SECRET_VALUE",
        "_NOT_OPAQUE_START",
        "_PASTED_MIN",
        "_CARRIERS",
    )
    before = {name: getattr(secret_policy, name) for name in names}
    in_use = set(secret_policy._IN_USE)
    yield
    for name, value in before.items():
        setattr(secret_policy, name, value)
    secret_policy._IN_USE.clear()
    secret_policy._IN_USE.update(in_use)


@pytest.fixture(autouse=True)
def _no_certificate_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep phase 4's certificate check off the network.

    It opens a TLS connection to every address it is given, which would make a
    test of address handling depend on DNS. Here every host is unreachable;
    the trust store's own tests replace this with the answers they need.
    """
    monkeypatch.setattr(trust_store, "_handshake", lambda host, port, context, timeout: ("net", 0, "offline (tests)"))
    monkeypatch.setattr(trust_store.TrustStore, "_presented_chain", lambda self, host, port, timeout: [])


@pytest.fixture(autouse=True)
def _no_claude_cli_registrations(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep phase 4's reading of existing registrations off the real ``claude`` CLI.

    ``claude mcp list`` connects to every server it knows before it prints, so
    a stage test would otherwise wait on the developer's own servers. The
    project file is already sandboxed (the working directory is ``tmp_path``);
    the tests of :mod:`.existing` put back the CLI answers they need.
    """
    from initialize_claude_ai.mcp_auto_discovery.existing import ExistingRegistrations

    monkeypatch.setattr(ExistingRegistrations, "_cli_names", lambda self: [])


#: The program's own variable namespaces: per-machine paths (``path_variable_prefix``),
#: its generated bridges' and client bridges' settings.
PROGRAM_VARIABLE_PREFIXES = ("ICA_MCP_", "MCP_BRIDGE_", "MCP_CLIENT_")


@functools.lru_cache(maxsize=1)
def trust_variables() -> frozenset[str]:
    """Every trust-store variable the packaged configuration names (runtimes and Claude Code's own)."""
    data = config_mod._load_toml(config_mod._DATA_DIR / "40-mcp.toml")["mcp"]["trust"]
    names = {"SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "NODE_EXTRA_CA_CERTS"}
    for values in (data.get("runtimes") or {}).values():
        names.update([values] if isinstance(values, str) else list(values or []))
    names.update((data.get("consumer") or {}).get("trust_variables") or [])
    return frozenset(str(n) for n in names if str(n).strip())


def host_variables() -> set[str]:
    """Every host variable the packaged defaults read, read from the defaults.

    Derived rather than listed, because a hand-kept list is exactly the thing
    that goes stale: adding ``${DEFAULT_MODEL:-...}`` to a default would
    otherwise make a passing suite depend on whether the developer happens to
    have that variable exported. Comments are excluded by walking the parsed
    values, so the ``${ENV_VAR}`` in a documentation line is not mistaken for
    one that is actually read.
    """
    names: set[str] = set()

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
        elif isinstance(node, str):
            names.update(re.findall(r"\$\{([A-Z][A-Z0-9_]*)", node))

    for path in sorted(config_mod._DATA_DIR.glob("*.toml")):
        walk(config_mod._load_toml(path))
    return names


@pytest.fixture(autouse=True)
def _no_host_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run as if on a machine that has never been configured.

    A developer's shell is very likely to export ``GOOGLE_CLOUD_PROJECT``,
    ``CLOUD_ML_REGION`` and friends -- this program's own phase 3 puts them
    there. Those feed the same settings the tests are asserting about, so
    without this the suite quietly tests the host rather than the code, and
    passes or fails depending on whose terminal it is run from.
    """
    for name in host_variables():
        monkeypatch.delenv(name, raising=False)
    # Read by name, not through a ${...} default: Claude Code's start-up wait.
    # A Claude Code session exports what its settings.json env holds to every
    # child -- this suite included -- so a wait phase 4 once set must not
    # decide what the tests see.
    monkeypatch.delenv(
        str(
            config_mod._load_toml(config_mod._DATA_DIR / "40-mcp.toml")["mcp"]["registration"]["client_timeout"].get(
                "variable"
            )
            or "MCP_TIMEOUT"
        ),
        raising=False,
    )
    # Credentials the host's Claude Code session exports from its settings.json
    # env (this program puts operators' values there): a fake server that reads
    # JIRA_PERSONAL_TOKEN found the real one and answered as if logged in.
    from initialize_claude_ai.mcp_auto_discovery.secret_policy import is_secret_name

    for name in list(os.environ):
        if is_secret_name(name) and not name.startswith(("ANTHROPIC_", "CLAUDE_", "GOOGLE_", "CLOUD_ML")):
            monkeypatch.delenv(name, raising=False)
    # Any override of the program's own, whether or not a default mentions it:
    # INITIALIZE_CLAUDE_AI_PROVISION__REQUIRE_MODEL reaches a setting by its
    # generated name alone, so there is nothing in the defaults to derive.
    for name in list(os.environ):
        if name.startswith(ENV_PREFIX):
            monkeypatch.delenv(name, raising=False)
    # The variables the program's own entries read -- its per-machine paths,
    # its bridges' and client bridges' settings -- and every trust-store
    # variable a runtime reads. A machine that has run the program exports them
    # (Claude Code passes its settings env to every child): an inherited
    # MCP_BRIDGE_CA_BUNDLE naming a file another layout lacks made a generated
    # bridge fail its start-up in the suite, and an inherited
    # ICA_MCP_GITHUB_MCP_SERVER changed what an entry resolved to.
    trust_names = trust_variables()  # parsed once per session, not per variable
    for name in list(os.environ):
        if name.startswith(PROGRAM_VARIABLE_PREFIXES) or name in trust_names:
            monkeypatch.delenv(name, raising=False)
    # One terminal for every test: the console picks its colour depth from
    # TERM/COLORTERM, and a CI runner or a bare container has neither. A test
    # about another terminal sets its own.
    monkeypatch.setenv("TERM", "xterm-256color")
    for name in ("COLORTERM", "NO_COLOR", "FORCE_COLOR", "CLICOLOR_FORCE"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _no_host_configuration) -> Path:
    """Isolate HOME, XDG and the initialize-claude-ai state directory."""
    home = tmp_path / "home"
    home.mkdir()
    for name in ("HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_DATA_HOME"):
        monkeypatch.setenv(name, str(home if name == "HOME" else home / name.lower()))
    monkeypatch.setenv("INITIALIZE_CLAUDE_AI_STATE_DIR", str(tmp_path / "state"))
    # A stray config file in the working directory would leak into every layer test.
    monkeypatch.setenv("INITIALIZE_CLAUDE_AI_SYSTEM_CONFIG", str(tmp_path / "absent.toml"))
    # The last check starts a real Claude Code session (mcp_auto_discovery.claude_check);
    # never from the test suite unless a test turns it on with a fake claude.
    monkeypatch.setenv("INITIALIZE_CLAUDE_AI_MCP__REGISTRATION__CHECK_THROUGH_CLAUDE", "false")
    # Validating an entry runs Claude Code against a throw-away configuration;
    # the shape rules still apply, the real CLI is never asked from the suite.
    monkeypatch.setenv("INITIALIZE_CLAUDE_AI_MCP__REGISTRATION__VALIDATE_WITH_CLAUDE", "false")
    # Reading login challenges makes real requests to each address and its API
    # hosts (mcp_auto_discovery.login_methods); tests that want it turn it on.
    monkeypatch.setenv("INITIALIZE_CLAUDE_AI_MCP__DISCOVERY__AUTH__READ_CHALLENGES", "false")
    # Pacing and the "try again later" wait slow every fake server down; tests that
    # want them turn them on.
    monkeypatch.setenv("INITIALIZE_CLAUDE_AI_MCP__VERIFICATION__PACING__ENABLED", "false")
    monkeypatch.setenv("INITIALIZE_CLAUDE_AI_MCP__VERIFICATION__LOGIN_CHECK__TEMPORARY_RETRY_SECONDS", "0")
    # The per-machine writer edits shell profiles and /etc/profile.d (core/machine_env.py):
    # never from the suite unless a test turns it on with places under tmp_path.
    monkeypatch.setenv("INITIALIZE_CLAUDE_AI_MACHINE_ENV__ENABLED", "false")
    # ...nor read a fresh shell's profiles, nor probe hosts for Claude Code's trust.
    monkeypatch.setenv("INITIALIZE_CLAUDE_AI_MCP__REGISTRATION__FRESH_ENV__INCLUDE_SHELL_PROFILES", "false")
    monkeypatch.setenv("INITIALIZE_CLAUDE_AI_MCP__TRUST__CONSUMER__ENABLED", "false")
    # The developer's own CA variables never reach a test: the program expands
    # ${NODE_EXTRA_CA_CERTS:-...} from the environment, and an exported bundle
    # changed what an entry was judged to need.
    for name in ("NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def settings(sandbox: Path):
    return load_settings(environ=dict(os.environ))


@pytest.fixture
def ctx(sandbox: Path, settings) -> RunContext:
    """A dry-run context: read-only probes still run, nothing is mutated."""
    runner = Runner(dry_run=True, default_timeout=10.0)
    state_dir = sandbox / "state"
    state_dir.mkdir(exist_ok=True)
    return RunContext(
        settings=settings,
        console=Console(color=False, quiet=True),
        runner=runner,
        secrets=SecretResolver(runner, allow_prompt=False),
        platform=detect(),
        env=EnvStore(state_dir=state_dir, dry_run=True),
        report=RunReport(dry_run=True),
        dry_run=True,
    )
