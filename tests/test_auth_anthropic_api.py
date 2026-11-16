"""Which header the Anthropic credential travels in.

An API key and a gateway bearer token are different credentials with different
headers. Sending one as the other reads upstream as an invalid credential, so
the kind that resolved has to decide -- these tests pin that.
"""

from __future__ import annotations

import pytest

from initialize_claude_ai.authenticate.anthropic_api import AnthropicApiAuth
from initialize_claude_ai.core.config import Settings
from initialize_claude_ai.core.report import Status

KEY = "sk-ant-not-a-real-key-0000"
TOKEN = "gateway-bearer-token-0000"


class FakeResponse:
    ok = True
    status = 200
    error = ""

    @staticmethod
    def json() -> dict:
        return {"data": [{"id": "claude-sonnet-5"}]}


@pytest.fixture
def backend(ctx, monkeypatch):
    """A backend whose probe records the headers instead of making a call."""
    auth = AnthropicApiAuth(ctx)
    sent: dict[str, str] = {}

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def get(self, _url, headers=None, **_kwargs):
            sent.update(headers or {})
            return FakeResponse()

    monkeypatch.setattr(ctx, "http", lambda **_kwargs: FakeClient())
    auth.sent = sent  # type: ignore[attr-defined]
    return auth


def configure(backend: AnthropicApiAuth, monkeypatch, **overrides) -> None:
    section = backend.settings.section(backend.config_key)
    patched = Settings(data={**section.data, **overrides}, environ=section.environ)
    monkeypatch.setattr(type(backend), "config", property(lambda _self: patched))


def test_an_api_key_is_sent_as_x_api_key(backend, monkeypatch) -> None:
    configure(backend, monkeypatch, api_key_refs=[f"literal:{KEY}"], auth_token_refs=[])
    outcome = backend.verify(backend.inspect())
    assert outcome.verified is True
    assert backend.sent["x-api-key"] == KEY
    assert "Authorization" not in backend.sent


def test_a_bearer_token_is_sent_as_authorization(backend, monkeypatch) -> None:
    """Regression: everything went out as x-api-key, so a gateway token was
    reported as rejected when it was merely in the wrong header."""
    configure(backend, monkeypatch, api_key_refs=[], auth_token_refs=[f"literal:{TOKEN}"])
    outcome = backend.verify(backend.inspect())
    assert outcome.verified is True
    assert backend.sent["Authorization"] == f"Bearer {TOKEN}"
    assert "x-api-key" not in backend.sent


def test_the_api_key_is_preferred_when_both_are_present(backend, monkeypatch) -> None:
    configure(
        backend,
        monkeypatch,
        api_key_refs=[f"literal:{KEY}"],
        auth_token_refs=[f"literal:{TOKEN}"],
    )
    backend.verify(backend.inspect())
    assert backend.sent["x-api-key"] == KEY and "Authorization" not in backend.sent


def test_the_header_names_are_data(backend, monkeypatch) -> None:
    """A gateway that wants something else is a config edit, not a code change."""
    configure(
        backend,
        monkeypatch,
        api_key_refs=[],
        auth_token_refs=[f"literal:{TOKEN}"],
        headers={"auth_token": "X-Gateway-Auth", "auth_token_prefix": "Token "},
    )
    backend.verify(backend.inspect())
    assert backend.sent["X-Gateway-Auth"] == f"Token {TOKEN}"


def test_no_credential_skips_rather_than_fails(backend, monkeypatch) -> None:
    configure(backend, monkeypatch, api_key_refs=[], auth_token_refs=[], api_key_helper="")
    outcome = backend.inspect()
    assert outcome.status == Status.SKIPPED and not outcome.authenticated


def test_the_identity_never_carries_the_value(backend, monkeypatch) -> None:
    configure(backend, monkeypatch, api_key_refs=[f"literal:{KEY}"], auth_token_refs=[])
    outcome = backend.inspect()
    assert KEY not in outcome.identity and KEY not in outcome.detail
    assert "sha256:" in outcome.identity
