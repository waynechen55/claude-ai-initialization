"""The output sink that makes 'never print a credential' true by construction."""

from __future__ import annotations

import os

from initialize_claude_ai.core import redact
from initialize_claude_ai.core.config import load_settings


def test_registered_values_are_masked_anywhere_they_appear() -> None:
    redact.register("zz-registered-value-zz")
    assert redact.redact("prefix zz-registered-value-zz suffix") == f"prefix {redact.MASK} suffix"


def test_short_values_are_not_registered() -> None:
    """Masking a two-character value would turn every log line into noise."""
    before = redact.registered_count()
    redact.register("ab")
    assert redact.registered_count() == before


def test_default_patterns_catch_common_credential_shapes(sandbox) -> None:
    redact.configure(load_settings(environ=dict(os.environ)).str_list("redaction.patterns"))
    samples = [
        "sk-ant-" + "A" * 40,
        "ya29." + "b" * 40,
        "ghp_" + "c" * 30,
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",  # gitleaks:allow (synthetic test value)
    ]
    for sample in samples:
        assert sample not in redact.redact(f"value: {sample}"), sample


def test_labelled_secret_keeps_its_label(sandbox) -> None:
    redact.configure(load_settings(environ=dict(os.environ)).str_list("redaction.patterns"))
    masked = redact.redact('api_key="s3cr3t-value-here"')
    assert "s3cr3t-value-here" not in masked
    assert "api_key" in masked


def test_mapping_is_redacted_by_key_name() -> None:
    out = redact.redact_mapping({"authorization": "Bearer abc", "url": "https://example.test"})
    assert out["authorization"] == redact.MASK
    assert out["url"] == "https://example.test"


def test_mapping_redaction_recurses() -> None:
    out = redact.redact_mapping({"outer": {"client_secret": "x"}, "list": [{"token": "y"}]})
    assert out["outer"]["client_secret"] == redact.MASK
    assert out["list"][0]["token"] == redact.MASK


def test_argv_masks_the_value_after_a_secret_flag() -> None:
    out = redact.redact_argv(["server", "--api-key", "abcd1234", "--port", "8080"])
    assert out == ["server", "--api-key", redact.MASK, "--port", "8080"]
    # The '--flag=value' form carries the value in the same argument.
    assert redact.redact_argv(["server", "--token=abcd1234"]) == [
        "server",
        f"--token={redact.MASK}",
    ]


def test_empty_values_stay_empty() -> None:
    assert redact.redact_mapping({"password": ""}) == {"password": ""}
    assert redact.redact(None) == ""


def test_a_credential_written_into_a_url_is_masked_in_place(sandbox) -> None:
    """--mcp-server takes any address, and an appliance behind basic auth is
    routinely handed round as https://admin:pw@box. The line still has to read
    as the address it is, so only the password goes."""
    redact.configure(load_settings(environ=dict(os.environ)).str_list("redaction.patterns"))
    masked = redact.redact("probing https://admin:hunter2-secret@box.corp/mcp")
    assert "hunter2-secret" not in masked
    assert masked == f"probing https://admin:{redact.MASK}@box.corp/mcp"


def test_an_address_without_a_credential_is_left_alone(sandbox) -> None:
    redact.configure(load_settings(environ=dict(os.environ)).str_list("redaction.patterns"))
    for url in ["https://mcp.example.test/mcp", "http://127.0.0.1:8080/mcp?a=b@c"]:
        assert redact.redact(url) == url


def test_the_url_credential_survives_the_packaged_pattern_list(sandbox) -> None:
    """The defaults in code and the ones in 00-core.toml are two lists, and the
    TOML one wins at runtime -- so it is the one that has to be checked."""
    patterns = load_settings(environ=dict(os.environ)).str_list("redaction.patterns")
    redact.configure(patterns)
    assert "s3cr3t-pass" not in redact.redact("http://u:s3cr3t-pass@10.0.0.5:8080/mcp")


def test_the_patterns_before_configuration_are_the_configured_ones(settings) -> None:
    """A secret logged before the settings load is masked exactly as one logged after."""
    assert list(redact._DEFAULT_PATTERNS) == settings.str_list("redaction.patterns")
