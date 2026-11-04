"""Which variables are credentials, and which only sound like one.

A real entry, from a real run:

.. code-block:: json

    "env": {
      "ALLOW_MUTATIONS": "false",
      "ENDPOINT": "https://smelt.example.net/graphql/",
      "NODE_TLS_REJECT_UNAUTHORIZED": "${NODE_TLS_REJECT_UNAUTHORIZED}"
    }

``NODE_TLS_REJECT_UNAUTHORIZED`` is not a key, a token or a secret. It became a
``${...}`` placeholder because ``SENSITIVE_NAME`` matched ``auth`` as a bare
substring inside ``UNAUTHORIZED``, so the variable was filed as a credential and
its value thrown away -- leaving a reference to a variable nobody sets, standing
in for a value that was never secret.

Two independent defects, so two independent fixes, and both are tested here:

1. the words must match as words, and a value that is plainly a flag or a
   number is not a credential whatever the name suggests;
2. a variable that switches TLS verification off never reaches an entry at all,
   correctly classified or not -- ``.mcp.json`` is copied between machines.
"""

from __future__ import annotations

import pytest

from initialize_claude_ai.mcp_auto_discovery.secret_policy import NOT_A_SECRET_VALUE, SENSITIVE_NAME
from initialize_claude_ai.mcp_auto_discovery.types import Candidate, Transport
from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter


@pytest.mark.parametrize(
    "name",
    [
        "OPENQA_API_KEY",
        "JIRA_PERSONAL_TOKEN",
        "GITHUB_PERSONAL_ACCESS_TOKEN",
        "CONFLUENCE_SECRET",
        "DB_PASSWORD",
        "SESSION_COOKIE",
        "AUTH_HEADER",
        "BEARER_TOKEN",
        "MY_PRIVATE_KEY",
    ],
)
def test_a_credential_is_still_recognised(name: str) -> None:
    """The fix must not open a hole; these are the names it exists for."""
    assert SENSITIVE_NAME.search(name), f"{name} must be treated as a credential"


@pytest.mark.parametrize(
    "name",
    [
        "NODE_TLS_REJECT_UNAUTHORIZED",  # the one that caused this
        "AUTHOR",
        "AUTHORITY",
        "GIT_AUTHOR_NAME",
        "KEYSTONE_URL",
        "MONKEY_PATCH",
        "ENDPOINT",
        "ALLOW_MUTATIONS",
    ],
)
def test_a_word_that_merely_contains_one_is_not_a_credential(name: str) -> None:
    assert not SENSITIVE_NAME.search(name), f"{name} is not a credential"


@pytest.mark.parametrize("value", ["true", "false", "0", "1", "30", "none", "OFF"])
def test_a_flag_or_a_number_is_never_a_credential(value: str) -> None:
    """The second guard: ask the value, not only the name.

    ``SESSION_TIMEOUT=30`` matches on the word ``session`` and always will. No
    credential looks like ``30``, so the value settles it -- the same instinct
    as the control probe, and cheaper than a list of exempt names.
    """
    assert NOT_A_SECRET_VALUE.match(value)


@pytest.mark.parametrize("value", ["Nzk1NjMwMjcyODMw", "hunter2hunter2", "u:k:s"])
def test_but_an_actual_value_is_not_waved_through(value: str) -> None:
    assert not NOT_A_SECRET_VALUE.match(value)


def test_the_smelt_environment_survives_classification(ctx) -> None:
    """End to end on the real entry: the two real settings keep their values."""
    from initialize_claude_ai.mcp_auto_discovery.agentic import AgenticPipeline

    plain, secrets, notes, _shapes, _sources = AgenticPipeline(ctx)._split_env(
        {
            "ENDPOINT": "https://smelt.example.net/graphql/",
            "ALLOW_MUTATIONS": "false",
            "NODE_TLS_REJECT_UNAUTHORIZED": "0",
        },
        {},
    )
    assert plain["ENDPOINT"] == "https://smelt.example.net/graphql/"
    assert plain["ALLOW_MUTATIONS"] == "false"
    assert "NODE_TLS_REJECT_UNAUTHORIZED" not in secrets, "a TLS switch is not a credential"


# --------------------------------------------------------------------------- #
# and regardless of classification, it does not reach the file
# --------------------------------------------------------------------------- #


def graphql(env: dict[str, str]) -> Candidate:
    return Candidate(
        kind="stdio",
        name="smelt",
        transport=Transport.STDIO,
        command=["npx", "-y", "mcp-graphql@2.0.4"],
        env=env,
    )


def test_a_tls_switch_never_reaches_the_entry(ctx) -> None:
    candidate = graphql(
        {
            "ENDPOINT": "https://smelt.example.net/graphql/",
            "ALLOW_MUTATIONS": "false",
            "NODE_TLS_REJECT_UNAUTHORIZED": "0",
        }
    )
    entry = McpWriter(ctx).entry_for(candidate)

    assert "NODE_TLS_REJECT_UNAUTHORIZED" not in entry["env"]
    assert entry["env"]["ALLOW_MUTATIONS"] == "false"
    assert entry["env"]["ENDPOINT"] == "https://smelt.example.net/graphql/"

    # The pairing is the point, and worth asserting together: the variable that
    # switches verification *off* is removed, and the one that tells the runtime
    # what this host *trusts* is added. An internal CA is the real reason
    # somebody reaches for the first, so the entry answers the need without
    # granting the wish.
    # Read through the one per-machine trust name (own_reference_names): a
    # NODE_EXTRA_CA_CERTS a login shell exports never replaces it.
    assert entry["env"]["NODE_EXTRA_CA_CERTS"].startswith("${ICA_MCP_CA_BUNDLE")
    assert set(entry["env"]) == {
        "ALLOW_MUTATIONS",
        "ENDPOINT",
        "NODE_EXTRA_CA_CERTS",
    }


def test_dropping_it_is_explained_rather_than_silent(ctx) -> None:
    candidate = graphql({"ENDPOINT": "https://smelt.example.net/", "NODE_TLS_REJECT_UNAUTHORIZED": "0"})
    McpWriter(ctx).entry_for(candidate)

    assert any("disables TLS certificate verification" in n for n in candidate.notes)
    assert any("system trust store" in n for n in candidate.notes), "say what to do instead"


@pytest.mark.parametrize("name", ["PYTHONHTTPSVERIFY", "GIT_SSL_NO_VERIFY", "SSL_VERIFY", "CURL_INSECURE"])
def test_the_others_go_the_same_way(ctx, name: str) -> None:
    """One product's switch is every product's switch; the list is configuration."""
    candidate = graphql({"ENDPOINT": "https://smelt.example.net/", name: "0"})
    assert name not in McpWriter(ctx).entry_for(candidate).get("env", {})


def test_an_ordinary_variable_is_untouched(ctx) -> None:
    candidate = graphql({"ENDPOINT": "https://smelt.example.net/", "NODE_OPTIONS": "--max-old-space=512"})
    entry = McpWriter(ctx).entry_for(candidate)
    # Never a credential: not in secret_env, its value never withheld. A runtime
    # setting is this machine's, so the entry reads it per machine and the
    # value is kept here (test_mcp_settings_need_a_reason).
    assert "NODE_OPTIONS" not in candidate.secret_env
    assert entry["env"]["NODE_OPTIONS"].startswith("${ICA_MCP_") and entry["env"]["NODE_OPTIONS"].endswith(":-}")
    assert "--max-old-space=512" in candidate.machine_values.values()


# --------------------------------------------------------------------------- #
# one rule, both pipelines
# --------------------------------------------------------------------------- #
#
# These primitives lived in agentic.py, so they judged what a *subagent* said
# and nothing else. The deterministic pipeline reached the same decision by
# reading the package registry's own `isSecret` flag -- somebody else's
# judgement about this project's second rule. The registry's flag is now a hint
# that can only make us more careful: true is believed, false is checked.


def registry_candidate() -> Candidate:
    return Candidate(kind="url", name="r", url="https://x.test/mcp", transport=Transport.STREAMABLE_HTTP)


def test_a_registry_header_carrying_a_token_is_not_copied_into_the_entry() -> None:
    """The sharpest case: this value goes verbatim into a file people commit."""
    from initialize_claude_ai.mcp_auto_discovery.registry_client import RegistryClient

    candidate = registry_candidate()
    RegistryClient._absorb_headers(
        candidate,
        [{"name": "Authorization", "value": "Bearer sk-live-abcdefghijklmnop", "isSecret": False}],
    )
    assert candidate.headers == {}, "a live token was written into .mcp.json"
    assert any("bearer_token_ref" in note for note in candidate.notes)


def test_an_ordinary_registry_header_is_still_copied() -> None:
    from initialize_claude_ai.mcp_auto_discovery.registry_client import RegistryClient

    candidate = registry_candidate()
    RegistryClient._absorb_headers(candidate, [{"name": "Accept", "value": "application/json"}])
    assert candidate.headers == {"Accept": "application/json"}


def test_a_registry_variable_named_like_a_credential_is_treated_as_one() -> None:
    """A registry that forgets isSecret must not downgrade the guarantee."""
    from initialize_claude_ai.mcp_auto_discovery.registry_client import RegistryClient

    candidate = registry_candidate()
    RegistryClient._absorb_env(candidate, [{"name": "GITHUB_TOKEN", "isRequired": True, "isSecret": False}])
    assert candidate.secret_env == {"GITHUB_TOKEN": "env:GITHUB_TOKEN"}
    assert "GITHUB_TOKEN" not in candidate.env
    assert any("treated as a credential here" in note for note in candidate.notes)


def test_a_registry_variable_that_is_plainly_configuration_is_left_alone() -> None:
    """The word-boundary and value tests apply here too, not just to agents."""
    from initialize_claude_ai.mcp_auto_discovery.registry_client import RegistryClient

    candidate = registry_candidate()
    RegistryClient._absorb_env(
        candidate,
        [
            {"name": "NODE_TLS_REJECT_UNAUTHORIZED", "isRequired": True},
            {"name": "ENDPOINT", "isRequired": True},
        ],
    )
    assert candidate.secret_env == {}
    assert sorted(candidate.env) == ["ENDPOINT", "NODE_TLS_REJECT_UNAUTHORIZED"]


def test_isSecret_true_is_always_believed() -> None:
    """The flag may only make us more careful, never less."""
    from initialize_claude_ai.mcp_auto_discovery.registry_client import RegistryClient

    candidate = registry_candidate()
    RegistryClient._absorb_env(candidate, [{"name": "REGION", "isSecret": True}])
    assert candidate.secret_env == {"REGION": "env:REGION"}


def test_both_pipelines_import_the_same_rule() -> None:
    """Not "the same wording" -- the same rule, and no private copy of it.

    The regression this guards is a copy of the regex diverging from the
    original, which is how one rule quietly acquires two strengths. The
    patterns are configured now (``[mcp.secret_policy]``), so a copy imported
    at load time would also miss the configuration: the agentic pipeline holds
    none and asks the predicates.
    """
    from initialize_claude_ai.mcp_auto_discovery import agentic, secret_policy

    for name in ("SENSITIVE_NAME", "SELECTOR_NAME", "NOT_A_SECRET_VALUE", "INLINE_CREDENTIAL"):
        assert not hasattr(agentic, name), name
    assert agentic.SAFE_REFERENCE_SCHEMES is secret_policy.SAFE_REFERENCE_SCHEMES


def test_nobody_matches_the_raw_pattern_instead_of_asking_the_rule() -> None:
    """Sharing the regex was not enough; the *decision* has to be shared too.

    Importing the same ``SENSITIVE_NAME`` and then calling ``.search()`` on it
    directly reimplements the classifier minus whatever the real predicate has
    learned since. That is exactly what happened: the selector guard sparing
    ``BUGZILLA_AUTH_MODE`` went into ``is_secret_name``, the agentic pipeline
    matched the pattern by hand in two places, and the fix reached only the
    deterministic path -- one rule, two strengths, again, with the test above
    passing throughout.

    So the check is structural. Match the pattern inside ``secret_policy``,
    where the rule lives; everywhere else, ask it.
    """
    import ast
    import pathlib

    root = pathlib.Path(secret_policy_module().__file__).parent
    offenders: list[str] = []
    for path in sorted(root.rglob("*.py")):
        if path.name == "secret_policy.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            target = node.func.value
            patterns = {"SENSITIVE_NAME", "SELECTOR_NAME", "INLINE_CREDENTIAL", "NOT_A_SECRET_VALUE"}
            if isinstance(target, ast.Name) and target.id in patterns:
                offenders.append(f"{path.name}:{node.lineno} calls {target.id}.{node.func.attr}()")

    assert offenders == [], (
        "these match the credential pattern directly instead of calling "
        "is_secret_name()/holds_a_credential(), so a change to the rule will "
        "not reach them: " + "; ".join(offenders)
    )


def secret_policy_module():
    from initialize_claude_ai.mcp_auto_discovery import secret_policy

    return secret_policy
