"""What ``--mcp-server`` accepts, and the URL each spelling turns into.

Everything downstream of this -- a hundred-odd candidate paths, the handshake,
the name a server is registered under -- is built by appending to the URL these
functions return. An address that is wrong by one character is not a smaller
mistake here than anywhere else; it is the whole search pointed at the wrong
host, reported as "no MCP server found".
"""

from __future__ import annotations

import pytest

from initialize_claude_ai.core.http import Response
from initialize_claude_ai.core.report import PhaseResult, Status
from initialize_claude_ai.mcp_auto_discovery.address import (
    AddressResolver,
    configure_local_zones,
    is_local,
    is_loopback,
    local_zones,
    parse,
    parse_all,
    split_servers,
)
from initialize_claude_ai.phases.phase4_mcp_auto_discovery import McpPhase

# ----------------------------------------------------------------------------
# splitting
# ----------------------------------------------------------------------------


def test_one_flag_can_carry_a_comma_separated_list() -> None:
    assert split_servers(["a.test,b.test"]) == ["a.test", "b.test"]


def test_repeating_the_flag_and_listing_them_come_to_the_same_thing() -> None:
    listed = split_servers(["a.test,b.test,c.test"])
    repeated = split_servers(["a.test", "b.test", "c.test"])
    assert listed == repeated == ["a.test", "b.test", "c.test"]


def test_stray_whitespace_is_a_separator_too() -> None:
    # A URL cannot contain a space, so treating one as a separator costs nothing
    # and rescues the copy-and-paste that brought a newline with it.
    assert split_servers(["a.test, b.test\nc.test"]) == ["a.test", "b.test", "c.test"]


def test_the_same_server_twice_is_discovered_once() -> None:
    assert split_servers(["a.test,a.test"]) == ["a.test"]


def test_duplicates_are_caught_after_normalising_not_before() -> None:
    # Spelled three ways, one server: discovering it three times would also
    # register it three times.
    assert [a.url for a in parse_all(["example.test", "https://example.test/", "EXAMPLE.test"])] == [
        "https://example.test"
    ]


# ----------------------------------------------------------------------------
# parsing
# ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("written", "probed"),
    [
        # A bare domain is the common case, and https is the safe reading of it.
        ("example.test", "https://example.test"),
        ("www.example.test/mcp", "https://www.example.test/mcp"),
        # The host is case-insensitive; a trailing slash is not a path.
        ("HTTPS://Example.TEST/", "https://example.test"),
        # A port the scheme already implies is noise.
        ("http://example.test:80/", "http://example.test"),
        ("https://example.test:443/mcp/", "https://example.test/mcp"),
        # A slash lost in a paste is not a different server.
        ("https:/example.test", "https://example.test"),
        ("https:example.test", "https://example.test"),
        # Copied out of HTML, or out of a chat client that wrapped it.
        ("//example.test/mcp", "https://example.test/mcp"),
        ("<https://example.test>", "https://example.test"),
        ("https://example.test,", "https://example.test"),
        # The wire wants punycode.
        ("münchen.test", "https://xn--mnchen-3ya.test"),
        # An absolute name, with the root dot people rarely type on purpose.
        ("example.test.", "https://example.test"),
        # Discovery appends its own paths, so a query is never part of the base.
        ("https://example.test/a?token=x#top", "https://example.test/a"),
        ("https://example.test//a//b/", "https://example.test/a/b"),
    ],
)
def test_every_way_of_writing_an_address_reaches_the_same_url(written: str, probed: str) -> None:
    address = parse(written)
    assert address.ok, address.error
    assert address.url == probed


@pytest.mark.parametrize(
    ("written", "probed"),
    [
        ("localhost:3000", "http://localhost:3000"),
        ("127.0.0.1:8080/mcp", "http://127.0.0.1:8080/mcp"),
        ("192.168.1.5:8080", "http://192.168.1.5:8080"),
        ("dev.local", "http://dev.local"),
        # A single-label name is a machine on this network, not a public site.
        ("myserver", "http://myserver"),
    ],
)
def test_a_private_address_is_assumed_to_be_plain_http(written: str, probed: str) -> None:
    """Nobody puts a certificate on the server they started thirty seconds ago.

    Assuming https there would fail the TLS handshake on every probe, and the
    report would blame the absence of an MCP server for the absence of a
    certificate.
    """
    assert parse(written).url == probed


@pytest.mark.parametrize(
    ("written", "probed"),
    [
        ("[::1]:8080", "http://[::1]:8080"),
        ("::1", "http://[::1]"),
        ("http://[2001:db8::1]:9000/mcp", "http://[2001:db8::1]:9000/mcp"),
    ],
)
def test_ipv6_literals_survive_with_their_brackets(written: str, probed: str) -> None:
    assert parse(written).url == probed


def test_a_credential_in_the_address_is_dropped_and_never_echoed() -> None:
    """It would otherwise reach the report, the log and the registered server."""
    address = parse("https://user:hunter2@example.test/mcp")
    assert address.url == "https://example.test/mcp"
    assert "hunter2" not in address.url
    assert not any("hunter2" in note for note in address.notes)
    assert "hunter2" not in repr(address.to_dict()["notes"])


@pytest.mark.parametrize(
    "written",
    ["ftp://example.test", "file:///tmp/x", "mcp://example.test", "", "   ", "https://"],
)
def test_what_cannot_be_probed_is_refused_rather_than_guessed_at(written: str) -> None:
    address = parse(written)
    assert not address.ok
    assert address.error


def test_the_reason_an_address_was_refused_names_it() -> None:
    assert "ftp" in parse("ftp://example.test").error


def test_every_correction_is_written_down() -> None:
    """The operator gets the reasoning, not just the answer."""
    address = parse("example.test:443/a?x=1")
    assert address.url == "https://example.test/a"
    joined = "; ".join(address.notes)
    assert "assuming https" in joined
    assert "443" in joined
    assert "query" in joined


def test_an_address_that_needed_nothing_reports_itself_plainly() -> None:
    assert parse("https://example.test/mcp").describe() == "https://example.test/mcp"
    assert parse("example.test").describe() == "example.test -> https://example.test"


# ----------------------------------------------------------------------------
# resolution
# ----------------------------------------------------------------------------


class FakeClient:
    """An HTTP client that answers from a table, and records what it was asked."""

    def __init__(self, answers: dict[str, Response]) -> None:
        self.answers = answers
        self.asked: list[str] = []

    def __enter__(self) -> "FakeClient":
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False

    def _answer(self, url: str) -> Response:
        self.asked.append(url)
        return self.answers.get(url, Response(url=url, status=0, error="connection refused"))

    def head(self, url: str, **_kwargs: object) -> Response:
        return self._answer(url)

    def get(self, url: str, **_kwargs: object) -> Response:
        return self._answer(url)


def resolver(ctx, monkeypatch, answers: dict[str, Response]) -> tuple[AddressResolver, FakeClient]:
    client = FakeClient(answers)
    monkeypatch.setattr(ctx, "http", lambda **_kwargs: client)
    return AddressResolver(ctx), client


def test_a_redirect_is_the_server_naming_its_own_address(ctx, monkeypatch) -> None:
    """Following it once beats following it on every candidate path."""
    resolve, _client = resolver(
        ctx,
        monkeypatch,
        {
            "https://example.test": Response(
                url="https://example.test",
                final_url="https://www.example.test/en/home",
                status=200,
            )
        },
    )
    address = resolve.resolve(parse("example.test"))
    assert address.url == "https://www.example.test"
    assert address.resolved
    assert any("redirects to" in note for note in address.notes)


def test_only_the_origin_is_taken_from_the_redirect(ctx, monkeypatch) -> None:
    """A landing page's path is no better a base to append ``/mcp`` to."""
    resolve, _client = resolver(
        ctx,
        monkeypatch,
        {
            "https://example.test/mcp": Response(
                url="https://example.test/mcp",
                final_url="https://www.example.test/login?next=/mcp",
                status=200,
            )
        },
    )
    assert resolve.resolve(parse("example.test/mcp")).url == "https://www.example.test/mcp"


def test_an_address_that_answers_as_written_is_left_exactly_as_written(ctx, monkeypatch) -> None:
    resolve, _client = resolver(
        ctx, monkeypatch, {"https://example.test": Response(url="https://example.test", status=404)}
    )
    address = resolve.resolve(parse("example.test"))
    assert address.url == "https://example.test"
    assert address.resolved


def test_a_host_that_says_nothing_is_still_discovered_as_written(ctx, monkeypatch) -> None:
    """An MCP server on :8080 behind a closed port 443 is ordinary, not an error."""
    resolve, _client = resolver(ctx, monkeypatch, {})
    address = resolve.resolve(parse("example.test"))
    assert address.url == "https://example.test"
    assert not address.resolved
    assert any("no response" in note for note in address.notes)


def test_http_is_upgraded_when_the_address_as_given_is_dead(ctx, monkeypatch) -> None:
    resolve, client = resolver(
        ctx, monkeypatch, {"https://example.test": Response(url="https://example.test", status=200)}
    )
    address = resolve.resolve(parse("http://example.test"))
    assert address.url == "https://example.test"
    assert "http://example.test" in client.asked


def test_a_public_address_is_never_quietly_downgraded_to_cleartext(ctx, monkeypatch) -> None:
    """Falling back to http would put every later probe -- and any bearer token
    -- on the wire in the clear, on the strength of a TLS failure that is far
    more likely to mean the host is simply down."""
    resolve, client = resolver(
        ctx, monkeypatch, {"http://example.test": Response(url="http://example.test", status=200)}
    )
    address = resolve.resolve(parse("https://example.test"))
    assert address.url == "https://example.test"
    assert "http://example.test" not in client.asked


def test_a_local_host_may_fall_back_to_http_because_that_is_where_it_lives(ctx, monkeypatch) -> None:
    resolve, _client = resolver(ctx, monkeypatch, {"http://dev.local": Response(url="http://dev.local", status=200)})
    assert resolve.resolve(parse("https://dev.local")).url == "http://dev.local"


def test_a_redirect_off_tls_is_followed_in_host_but_not_in_scheme(ctx, monkeypatch) -> None:
    resolve, _client = resolver(
        ctx,
        monkeypatch,
        {
            "https://example.test": Response(
                url="https://example.test", final_url="http://www.example.test/", status=200
            )
        },
    )
    assert resolve.resolve(parse("example.test")).url == "https://www.example.test"


def test_head_is_tried_first_and_get_only_if_it_is_refused(ctx, monkeypatch) -> None:
    """Plenty of servers answer GET and nothing else; one retry is cheap."""
    resolve, client = resolver(
        ctx, monkeypatch, {"https://example.test": Response(url="https://example.test", status=405)}
    )
    resolve.resolve(parse("example.test"))
    assert client.asked == ["https://example.test", "https://example.test"]


def test_the_confirming_request_can_be_switched_off(ctx, monkeypatch) -> None:
    ctx.settings.set("mcp.address.resolve", False)
    resolve, client = resolver(ctx, monkeypatch, {})
    address = resolve.resolve(parse("example.test"))
    assert client.asked == []
    assert address.url == "https://example.test" and not address.resolved


def test_an_address_that_never_parsed_is_not_probed(ctx, monkeypatch) -> None:
    resolve, client = resolver(ctx, monkeypatch, {})
    assert not resolve.resolve(parse("ftp://example.test")).ok
    assert client.asked == []


# ----------------------------------------------------------------------------
# where the phase gets its addresses
# ----------------------------------------------------------------------------


def test_the_flag_is_read_the_same_way_as_the_setting(ctx) -> None:
    """One parser for both, so a config file and a flag cannot disagree."""
    ctx.settings.set("mcp.servers", ["from.config"])
    ctx.state["mcp_servers"] = ["from.flag, second.flag"]
    assert [a.url for a in McpPhase(ctx)._addresses()] == [
        "https://from.flag",
        "https://second.flag",
    ]


def test_the_setting_is_used_when_the_flag_is_absent(ctx) -> None:
    ctx.settings.set("mcp.servers", ["from.config", "other.config"])
    assert [a.url for a in McpPhase(ctx)._addresses()] == [
        "https://from.config",
        "https://other.config",
    ]


def test_the_old_setting_name_still_works_and_says_it_is_old(ctx, caplog) -> None:
    """Ignoring a config file that says exactly what the operator wants would
    cost them a run to discover."""
    ctx.settings.set("mcp.targets", ["legacy.config"])
    with caplog.at_level("WARNING"):
        assert [a.url for a in McpPhase(ctx)._addresses()] == ["https://legacy.config"]
    assert "mcp.servers" in caplog.text


def test_an_unusable_address_is_reported_and_left_out_of_the_search(ctx, monkeypatch) -> None:
    monkeypatch.setattr(ctx, "http", lambda **_kwargs: FakeClient({}))
    phase = McpPhase(ctx)
    result = PhaseResult(phase=phase.slug, title=phase.title)
    usable = phase._canonicalise(result, parse_all(["ftp://nope.test", "good.test"]))
    assert [a.url for a in usable] == ["https://good.test"]
    assert [(s.name, s.status) for s in result.steps] == [
        ("address ftp://nope.test", Status.FAILED),
        ("address good.test", Status.OK),
    ]


def test_addresses_that_resolve_to_one_server_are_searched_once(ctx, monkeypatch) -> None:
    moved = Response(url="https://example.test", final_url="https://www.example.test/", status=200)
    monkeypatch.setattr(
        ctx,
        "http",
        lambda **_kwargs: FakeClient({"https://example.test": moved, "https://www.example.test": moved}),
    )
    phase = McpPhase(ctx)
    result = PhaseResult(phase=phase.slug, title=phase.title)
    usable = phase._canonicalise(result, parse_all(["example.test", "www.example.test"]))
    assert [a.url for a in usable] == ["https://www.example.test"]
    # Both are still reported: the operator asked about two, and gets two answers.
    assert len(result.steps) == 2


# ----------------------------------------------------------------------------
# which network the address is on
# ----------------------------------------------------------------------------
#
# `--mcp-server` takes any address -- a vendor's public endpoint, a container on
# localhost, an appliance on an internal zone nothing has heard of -- and four
# decisions turn on this one answer: the default scheme, whether a bearer token
# may cross cleartext, the ranking's cleartext penalty, and whether the
# developer ports are probed. It used to be three near-identical copies of a
# hardcoded suffix list in three modules; these tests are what keeps it one.


@pytest.mark.parametrize(
    "host",
    [
        "localhost",
        "localhost.localdomain",
        "127.0.0.1",
        "127.0.1.1",
        "::1",
        "0.0.0.0",
        "10.0.0.1",
        "192.168.1.50",
        "172.16.3.4",
        "169.254.1.1",
        "fd00::1",
        "printer.local",
        "api.internal",
        "nas.lan",
        "box.home.arpa",
        "mcpbox",
    ],
)
def test_the_conventional_local_zones_are_recognised(host: str) -> None:
    assert is_local(host), f"{host} should be local"


@pytest.mark.parametrize(
    "host",
    ["example.com", "mcp.example.com", "8.8.8.8", "2606:4700::1111", "example.corp"],
)
def test_a_public_address_is_not_mistaken_for_a_local_one(host: str) -> None:
    assert not is_local(host)


def test_an_empty_host_is_not_local() -> None:
    """It is not anything. Saying "local" would send a token to nowhere."""
    assert not is_local("")


def test_an_operator_can_name_a_zone_this_program_has_never_heard_of() -> None:
    """The built-in list is conventions, not a fact about somebody's network."""
    assert not is_local("mcp.corp")
    configure_local_zones([".corp", "intra"])
    assert is_local("mcp.corp")
    assert is_local("build.intra"), "a suffix written without its dot still works"


def test_naming_a_zone_adds_to_the_defaults_rather_than_replacing_them() -> None:
    configure_local_zones([".corp"])
    assert is_local("printer.local"), "loopback and .local are not a trade"
    assert is_local("localhost")


def test_the_defaults_can_be_dropped_when_one_of_them_is_somebody_elses(ctx) -> None:
    configure_local_zones([".corp"], replace_defaults=True)
    assert is_local("mcp.corp")
    assert not is_local("nas.lan"), "a .lan that is really a public domain"
    assert is_local("localhost"), "loopback is never somebody else's"


def test_a_zone_named_twice_is_only_checked_once() -> None:
    configure_local_zones([".corp", "corp", ".CORP "])
    assert local_zones().suffixes.count(".corp") == 1


def test_an_extra_name_for_this_machine_can_be_declared() -> None:
    configure_local_zones((), loopback_hosts=["mcpbox.example.com"])
    assert is_loopback("mcpbox.example.com")
    assert is_local("MCPBOX.EXAMPLE.COM"), "hosts are matched case-insensitively"


def test_the_local_answer_decides_the_scheme_a_bare_name_gets() -> None:
    assert parse("mcp.corp").url == "https://mcp.corp"
    configure_local_zones([".corp"])
    assert parse("mcp.corp").url == "http://mcp.corp"


def test_the_phase_takes_the_operators_word_before_parsing_anything(ctx) -> None:
    """Ordering matters: the default scheme is chosen while the address is being
    parsed, so a declaration that arrives afterwards is a declaration ignored."""
    ctx.settings.set("mcp.address.local_suffixes", ".corp")
    McpPhase(ctx)._declare_local_zones()
    assert parse("mcp.corp").is_local


def test_the_phase_leaves_the_defaults_alone_when_nothing_is_declared(ctx) -> None:
    before = local_zones()
    McpPhase(ctx)._declare_local_zones()
    assert local_zones() == before


def test_a_credential_in_the_address_is_masked_in_what_is_echoed_back() -> None:
    """``raw`` exists so the operator can see the input their output came from,
    and it reaches the report, the transcript and the log. The username stays --
    it is not the secret, and it is what makes the line recognisable."""
    address = parse("https://admin:hunter2@box.test/mcp")
    assert "hunter2" not in address.raw
    assert "hunter2" not in address.describe()
    assert "hunter2" not in repr(address.to_dict())
    assert address.raw == "https://admin:***@box.test/mcp"


@pytest.mark.parametrize(
    "written",
    [
        "http://token-only@box.test/mcp",  # no username/password split
        "https://:hunter2@box.test/mcp",  # password, no username
        "admin:hunter2@box.test",  # no scheme
        "ftp://admin:hunter2@box.test",  # rejected before it is parsed
    ],
)
def test_no_way_of_writing_a_credential_gets_it_echoed(written: str) -> None:
    address = parse(written)
    assert "hunter2" not in repr(address.to_dict())
    assert "token-only" not in repr(address.to_dict())


def test_an_at_sign_further_along_the_address_is_not_a_credential() -> None:
    assert parse("https://box.test/mcp?user=a@b").raw == "https://box.test/mcp?user=a@b"


def test_a_scoped_package_name_is_not_mistaken_for_a_credential() -> None:
    """``@acme/mcp-server`` is an ordinary --mcp-server value: the endpoint stage
    will find nothing and the package stage will search the registries for it."""
    assert parse("@acme/mcp-server").raw == "@acme/mcp-server"


# ----------------------------------------------------------------------------
# a response that never arrived
# ----------------------------------------------------------------------------


def test_no_response_at_all_is_not_reported_as_a_status() -> None:
    """``status`` is 0 when nothing came back. Printing that as "HTTP 0" reads as
    a server answering strangely, and sends the reader to the wrong end of the
    wire -- it was DNS, TLS, a refused connection or a timeout."""
    response = Response(url="https://registry.test/v0/servers", status=0, error="connection timed out")
    assert response.problem == "connection timed out"
    assert "HTTP" not in response.problem


def test_a_transport_failure_with_nothing_to_say_still_says_which_end() -> None:
    assert Response(url="https://registry.test", status=0).problem == "no response (transport error)"


def test_a_real_status_is_still_a_real_status() -> None:
    assert Response(url="https://registry.test", status=404).problem == "HTTP 404"


def test_the_registry_says_why_it_got_nothing(ctx, monkeypatch, caplog) -> None:
    import logging

    from initialize_claude_ai.mcp_auto_discovery.registry_client import RegistryClient

    url = "https://registry.test/v0/servers?search=x"
    client = FakeClient({url: Response(url=url, status=0, error="connection timed out")})
    monkeypatch.setattr(type(ctx), "http", lambda self, **k: client)

    with caplog.at_level(logging.DEBUG):
        assert RegistryClient(ctx)._get_json(url) is None

    assert "connection timed out" in caplog.text
    assert "HTTP 0" not in caplog.text
