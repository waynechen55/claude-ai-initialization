"""--secure: a credential must be named, never handed over as a value.

Two halves that have to agree. The command line refuses the value-taking flags,
and the resolver refuses an inline ``literal:`` reference whichever layer it
arrived from -- so the guarantee does not depend on the operator having used the
command line to supply the credential in the first place.
"""

from __future__ import annotations

import pytest

from initialize_claude_ai import cli
from initialize_claude_ai.core import redact
from initialize_claude_ai.core.errors import SecretPolicyError
from initialize_claude_ai.core.proc import Runner
from initialize_claude_ai.core.secrets import SecretResolver

DEMO = "sk-ant-not-a-real-key-0000"


def parse(*argv: str):
    return cli.build_parser().parse_args(cli._normalise_argv(list(argv)))


def declared_options() -> set[str]:
    """Every option string, including the ones on the subparsers."""
    import argparse

    found: set[str] = set()
    stack = [cli.build_parser()]
    while stack:
        parser = stack.pop()
        for action in parser._actions:
            found.update(action.option_strings)
            if isinstance(action, argparse._SubParsersAction):
                stack.extend(action.choices.values())
    return found


# -- the flag table ----------------------------------------------------------


def test_every_credential_flag_comes_in_both_forms() -> None:
    """The registry is what makes --secure total, so it must be complete."""
    options = declared_options()
    for flag in cli.SECRET_FLAGS:
        assert flag.plain in options, f"{flag.plain} is declared in the table but not the parser"
        assert flag.ref in options, f"{flag.ref} is declared in the table but not the parser"


def test_no_credential_flag_escapes_the_table() -> None:
    """A --*-ref option that is not in SECRET_FLAGS would not be governed."""
    known = {flag.ref for flag in cli.SECRET_FLAGS}
    assert {o for o in declared_options() if o.endswith("-ref")} == known


#: Suffixes that name a credential rather than an identity or a location.
CREDENTIAL_SUFFIXES = ("-key", "-token", "-secret", "-password", "-passwd")


def test_no_credential_shaped_flag_escapes_the_table() -> None:
    """The other half of the guard above, from the value side.

    ``--secure`` only refuses what the table declares, so a flag added later
    that *looks* like a credential -- ``--foo-token`` -- would be accepted with
    its value in ps output and nobody would notice.  ``--gcloud-service-account-file``
    is deliberately not caught: it names a path, and the key it points at is
    opened by gcloud, never read into this process.
    """
    known = {flag.plain for flag in cli.SECRET_FLAGS}
    suspicious = {
        option for option in declared_options() if option.endswith(CREDENTIAL_SUFFIXES) and option not in known
    }
    assert suspicious == set(), (
        "add these to cli.SECRET_FLAGS, or rename them if they carry a reference or an identity rather than a value"
    )


# -- command line ------------------------------------------------------------


def test_secure_refuses_a_value_bearing_flag() -> None:
    complaint = cli.check_credential_flags(parse("run", "--secure", "--auth-anthropic-key", DEMO))
    assert complaint and "--auth-anthropic-key-ref" in complaint


@pytest.mark.parametrize("flag", [f for f in cli.SECRET_FLAGS])
def test_secure_refuses_every_plain_form(flag: cli.SecretFlag) -> None:
    assert cli.check_credential_flags(parse("run", "--secure", flag.plain, DEMO))
    assert cli.check_credential_flags(parse("run", "--secure", flag.ref, "env:NAME")) is None


def test_the_two_forms_of_one_credential_are_mutually_exclusive() -> None:
    complaint = cli.check_credential_flags(
        parse("run", "--auth-anthropic-key", DEMO, "--auth-anthropic-key-ref", "env:NAME")
    )
    assert complaint and "not both" in complaint


def test_without_secure_the_plain_form_is_allowed() -> None:
    assert cli.check_credential_flags(parse("run", "--auth-anthropic-key", DEMO)) is None


def test_secure_is_accepted_on_either_side_of_the_subcommand() -> None:
    assert parse("--secure", "run").secure is True
    assert parse("run", "--secure").secure is True


# -- compilation onto settings -----------------------------------------------


def test_a_reference_is_passed_through_untouched() -> None:
    overrides = cli._credential_overrides(parse("run", "--auth-anthropic-key-ref", "env:MY_KEY"))
    assert overrides == ["auth.anthropic_api.api_key_refs=env:MY_KEY"]


def test_a_value_becomes_a_literal_reference() -> None:
    """One resolution path for both forms; the value never gets a private route."""
    overrides = cli._credential_overrides(parse("run", "--auth-anthropic-key", DEMO))
    assert overrides == [f"auth.anthropic_api.api_key_refs=literal:{DEMO}"]


def test_a_value_is_registered_for_redaction_at_parse_time() -> None:
    """Before anything has had the chance to log it."""
    cli._credential_overrides(parse("run", "--auth-anthropic-token", "tok-secure-mode-check"))
    assert redact.redact("upstream said tok-secure-mode-check") == f"upstream said {redact.MASK}"


def test_the_key_and_the_token_are_different_settings() -> None:
    """They travel in different headers, so they cannot share a list."""
    key = cli._credential_overrides(parse("run", "--auth-anthropic-key-ref", "env:K"))[0]
    token = cli._credential_overrides(parse("run", "--auth-anthropic-token-ref", "env:T"))[0]
    assert key.startswith("auth.anthropic_api.api_key_refs=")
    assert token.startswith("auth.anthropic_api.auth_token_refs=")


def test_secure_disables_inline_references_everywhere() -> None:
    """Not just the flags: config, env and --set are covered by the same switch."""
    assert "secrets.allow_literal=false" in cli._overrides_from_args(parse("run", "--secure"))


# -- the resolver ------------------------------------------------------------


def test_a_literal_resolves_when_inline_credentials_are_allowed() -> None:
    resolver = SecretResolver(Runner(dry_run=True), allow_prompt=False)
    secret = resolver.resolve(f"literal:{DEMO}")
    assert secret is not None and secret.reveal() == DEMO


def test_a_literal_is_refused_under_secure() -> None:
    resolver = SecretResolver(Runner(dry_run=True), allow_prompt=False, allow_literal=False)
    with pytest.raises(SecretPolicyError) as raised:
        resolver.resolve(f"literal:{DEMO}", label="Anthropic API key")
    assert DEMO not in str(raised.value), "the refusal must not quote the payload"


def test_a_refusal_is_not_mistaken_for_an_absent_credential(monkeypatch) -> None:
    """first_available steps over an empty reference but must not swallow a refusal.

    Otherwise --secure would read as 'no credential found' and the operator
    would go looking for a missing key instead of a forbidden one.
    """
    monkeypatch.setenv("REAL_KEY", "value-from-env")
    resolver = SecretResolver(Runner(dry_run=True), allow_prompt=False, allow_literal=False)
    with pytest.raises(SecretPolicyError):
        resolver.first_available([f"literal:{DEMO}", "env:REAL_KEY"], label="Anthropic API key")


def test_an_empty_reference_is_still_stepped_over(monkeypatch) -> None:
    monkeypatch.delenv("MISSING_KEY", raising=False)
    monkeypatch.setenv("REAL_KEY", "value-from-env")
    resolver = SecretResolver(Runner(dry_run=True), allow_prompt=False, allow_literal=False)
    secret = resolver.first_available(["env:MISSING_KEY", "env:REAL_KEY"])
    assert secret is not None and secret.reveal() == "value-from-env"


def test_describe_never_shows_an_inline_value() -> None:
    assert SecretResolver.describe(f"literal:{DEMO}") == "literal:<inline>"
