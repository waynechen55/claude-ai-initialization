"""A 401 is only a find when it looked like one.

An authentication wall is treated as proof that the target runs an MCP server:
it closes the lower tiers and gets written into ``.mcp.json`` with an empty
credential slot. That is right for a host which mostly answers 200 and refuses
one path. It is badly wrong for a Jira, a Confluence, a service desk or any
intranet, which answers 401 or 403 on *every* URL it owns -- including paths
that do not exist. There the status says nothing whatever about MCP, and taking
it at face value registers the website as a server: an entry no credential can
ever make work, which looks like progress and sends the operator hunting for a
token that would not have helped.

Two real entries this program produced, and what was wrong with each:

    "jira-example-com": {"url": "https://jira.example.com?format=mcp",
                      "headers": {"Authorization": "OAuth ${JIRA_EXAMPLE_COM_TOKEN}"}}
    "sd-example-com":   {"url": "https://sd.example.com?format=mcp",
                      "headers": {"Authorization": "Bearer ${SD_EXAMPLE_COM_TOKEN}"}}

Three faults, one per section below: the 401 was an HTML login page; the scheme
was copied out of a challenge advertising OAuth 1.0a, which no single token can
satisfy; and ``?format=mcp`` is a query string this program appends while
probing, so it was the only thing making either URL look like an endpoint.
"""

from __future__ import annotations

from initialize_claude_ai.mcp_auto_discovery.probes import McpProber
from initialize_claude_ai.mcp_auto_discovery.types import Candidate, Handshake, Transport
from initialize_claude_ai.mcp_auto_discovery.writer import McpWriter


class Response:
    """The parts of an HTTP response the evidence test looks at."""

    def __init__(self, headers: dict[str, str] | None = None, text: str = "") -> None:
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}
        self.text = text
        self.status = 401

    @property
    def content_type(self) -> str:
        return (self.headers.get("content-type") or "").split(";")[0].strip().lower()


#: What Jira actually sent us, trimmed. Atlassian's own 401 page.
JIRA_401 = Response(
    headers={
        "content-type": "text/html;charset=UTF-8",
        "www-authenticate": 'OAuth realm="https%3A%2F%2Fjira.example.com"',
        "x-arequestid": "616x26407x1",
    },
    text="<html><head><title>Unauthorized (401)</title></head><body>...</body></html>",
)

#: What a real MCP endpoint behind a gateway sends.
MCP_401 = Response(
    headers={"content-type": "application/json"},
    text='{"jsonrpc":"2.0","error":{"code":-32001,"message":"unauthorized"}}',
)

#: What confluence.example.com actually sends to an anonymous POST: a login page
#: wearing a JSON content type. Every clause of the evidence test passed it,
#: because the only one that fired was "it answered application/json, not a web
#: page" -- which is true of every JSON API and so of every JSON error.
CONFLUENCE_401 = Response(
    headers={"content-type": "application/json;charset=UTF-8"},
    text=('{"statusCode":401,"message":"You are not logged in. <a href=\\"/login.action\\">Log in</a> to see more."}'),
)

#: What openqa.opensuse.org/mcp actually sends: no MCP markers, no challenge
#: header, nothing but a plain JSON refusal -- and a real endpoint all the same.
#: The login-page rule has to let this through or it costs a working server.
OPENQA_403 = Response(
    headers={"content-type": "application/json"},
    text='{"error":"no api key","error_status":403}',
)


def evidence(ctx, response: Response) -> list[str]:
    result = Handshake(transport=Transport.STREAMABLE_HTTP, status=401, requires_auth=True)
    McpProber(ctx)._note_auth_evidence(result, response)
    return result.auth_evidence


# -- fix 1: what counts as evidence --------------------------------------------


def test_an_html_login_page_is_not_evidence_of_anything(ctx) -> None:
    assert evidence(ctx, JIRA_401) == []


def test_a_login_page_wearing_a_json_content_type_is_not_either(ctx) -> None:
    """confluence.example.com, verbatim, and the reason it reached `.mcp.json`.

    The content-type clause -- "it answered application/json, not a web page" --
    is the generous one that makes the whole test work for real endpoints, and
    it is true of every JSON *error* ever sent. So the body has to be able to
    overrule it: a message telling a person to log in is not something an MCP
    endpoint has any reason to say.
    """
    assert evidence(ctx, CONFLUENCE_401) == []


def test_the_refusal_says_which_phrase_gave_it_away(ctx) -> None:
    from initialize_claude_ai.mcp_auto_discovery.probes import McpProber
    from initialize_claude_ai.mcp_auto_discovery.types import Handshake, Transport

    result = Handshake(transport=Transport.STREAMABLE_HTTP, status=401, requires_auth=True)
    McpProber(ctx)._note_auth_evidence(result, CONFLUENCE_401)
    assert any("login page" in hint for hint in result.protocol_hints), result.protocol_hints


def test_a_plain_json_refusal_with_no_markers_still_counts(ctx) -> None:
    """openqa.opensuse.org/mcp: a real endpoint with nothing MCP-shaped to say.

    This is the cost side of the rule above. Tightening the evidence test until
    it demanded a protocol-shaped refusal would throw away a working tier-1
    endpoint and send the run off to build a bridge instead -- which is exactly
    what the tier order exists to prevent. So the login markers disqualify, and
    nothing else was made stricter.
    """
    assert evidence(ctx, OPENQA_403), "a JSON API refusing an anonymous client is a finding"


def test_a_json_rpc_rejection_is(ctx) -> None:
    found = evidence(ctx, MCP_401)
    assert found, "a protocol answered, even though it said no"


def test_an_mcp_header_on_the_rejection_is(ctx) -> None:
    found = evidence(ctx, Response(headers={"content-type": "text/html", "Mcp-Session-Id": "abc"}))
    assert any("Mcp-Session-Id" in f for f in found), found


def test_a_resource_metadata_pointer_is(ctx) -> None:
    """The MCP spec's own OAuth discovery hint, as opposed to a website login."""
    found = evidence(
        ctx,
        Response(
            headers={
                "content-type": "text/html",
                "www-authenticate": 'Bearer resource_metadata="https://x/.well-known/o"',
            }
        ),
    )
    assert any("resource_metadata" in f for f in found), found


def test_any_content_type_that_is_not_a_web_page_is(ctx) -> None:
    """Deliberately generous: losing a real endpoint is the worse mistake."""
    assert evidence(ctx, Response(headers={"content-type": "text/plain"})), "plain text still counts"
    assert evidence(ctx, Response(headers={"content-type": "application/json"}))


def test_which_content_types_disqualify_is_configuration(ctx) -> None:
    ctx.settings.set("mcp.discovery.auth.disqualifying_content_types", "application/json")
    assert evidence(ctx, Response(headers={"content-type": "application/json"})) == []


def test_the_whole_check_can_be_turned_off(ctx) -> None:
    """false restores the older, credulous behaviour for anyone who wants it."""
    ctx.settings.set("mcp.discovery.auth.require_mcp_evidence", "false")
    assert evidence(ctx, JIRA_401) == ["not required by configuration"]


def test_a_login_page_says_so_in_the_hints(ctx) -> None:
    """The sentence that stops somebody going to look for a token."""
    result = Handshake(status=401, requires_auth=True)
    McpProber(ctx)._note_auth_evidence(result, JIRA_401)
    assert any("not a protocol answer" in h for h in result.protocol_hints)


def test_a_candidate_is_only_credential_walled_with_evidence() -> None:
    def refused(**shake) -> Candidate:
        return Candidate(kind="url", url="https://x/mcp", handshake=Handshake(status=401, requires_auth=True, **shake))

    assert refused().needs_credential is False, "a bare 401 is not a find"
    assert refused(auth_evidence=["it answered application/json, not a web page"]).needs_credential


# -- fix 2: the scheme in the challenge ----------------------------------------


def header_for(ctx, hint: str) -> str:
    candidate = Candidate(
        kind="url",
        url="https://jira.example.test/mcp",
        transport=Transport.STREAMABLE_HTTP,
        handshake=Handshake(
            status=401,
            requires_auth=True,
            auth_hint=hint,
            auth_evidence=["it answered application/json, not a web page"],
        ),
    )
    writer = McpWriter(ctx)
    writer.prepare_provisional(candidate, "jira-example-test")
    return writer.entry_for(candidate)["headers"]["Authorization"]


def test_a_scheme_we_can_speak_is_honoured(ctx) -> None:
    assert header_for(ctx, 'Basic realm="x"').startswith("Basic ")


def test_oauth_1a_is_not_copied_into_the_entry(ctx) -> None:
    """Jira advertises `OAuth realm=...`; a personal access token is a Bearer.

    Sending `Authorization: OAuth <PAT>` is a second 401 to diagnose, which is
    exactly what happened to jira.example.com.
    """
    assert header_for(ctx, 'OAuth realm="https%3A%2F%2Fjira.example.test"').startswith("Bearer ")


def test_the_operator_is_told_the_server_asked_for_something_else(ctx) -> None:
    candidate = Candidate(
        kind="url",
        url="https://jira.example.test/mcp",
        transport=Transport.STREAMABLE_HTTP,
        handshake=Handshake(
            status=401,
            requires_auth=True,
            auth_hint="OAuth realm=x",
            auth_evidence=["it answered application/json, not a web page"],
        ),
    )
    writer = McpWriter(ctx)
    writer.prepare_provisional(candidate, "jira-example-test")
    writer.entry_for(candidate)
    assert any("OAuth" in note and "Bearer" in note for note in candidate.notes), candidate.notes


def test_which_schemes_are_speakable_is_configuration(ctx) -> None:
    ctx.settings.set("mcp.discovery.auth.header_schemes", "Bearer")
    assert header_for(ctx, 'Basic realm="x"').startswith("Bearer ")


# -- fix 3: our own probe decoration -------------------------------------------


def walled_at(url: str) -> Candidate:
    return Candidate(
        kind="url",
        url=url,
        transport=Transport.STREAMABLE_HTTP,
        handshake=Handshake(
            status=401,
            requires_auth=True,
            auth_evidence=["it answered application/json, not a web page"],
        ),
    )


def test_the_url_that_was_probed_is_the_url_that_is_written(ctx) -> None:
    """The probe query is ours, and removing it invents a URL nobody tested.

    Stripping ``?format=mcp`` looked like honesty -- the query came from us, so
    recording it as the server's address is circular. But removing it does not
    undo the probe; it fabricates a *second* address.
    ``https://confluence.example.com?format=mcp`` answered 401 and was written out
    as ``https://confluence.example.com``, which nothing had ever requested, which
    is a login page, and which no credential could make work. Registering what
    you verified has to include the URL.
    """
    writer = McpWriter(ctx)
    probed = "https://jira.example.test?format=mcp"
    assert writer.registered_url(walled_at(probed)) == probed


def test_a_query_the_server_owns_is_left_alone(ctx) -> None:
    writer = McpWriter(ctx)
    url = "https://api.example.test/mcp?tenant=acme"
    assert writer.registered_url(walled_at(url)) == url


def test_a_url_that_verified_keeps_exactly_what_verified(ctx) -> None:
    """Whatever answered is the address that demonstrably works."""
    candidate = walled_at("https://api.example.test?format=mcp")
    candidate.handshake = Handshake(ok=True, server_name="Real", tools=["a"], round_trip=True)
    assert McpWriter(ctx).registered_url(candidate) == "https://api.example.test?format=mcp"


def test_a_bare_front_door_says_the_query_was_ours(ctx) -> None:
    """The note is what stops the entry reading like a discovery."""
    candidate = walled_at("https://jira.example.test?format=mcp")
    McpWriter(ctx).registered_url(candidate)
    assert any("this program added while probing" in n for n in candidate.notes), candidate.notes


def test_a_query_of_the_servers_own_earns_no_such_note(ctx) -> None:
    candidate = walled_at("https://api.example.test/x?tenant=acme")
    McpWriter(ctx).registered_url(candidate)
    assert not any("while probing" in note for note in candidate.notes), candidate.notes


def test_the_front_door_is_refused_rather_than_tidied(ctx) -> None:
    """End to end: what should have happened for confluence.example.com.

    The old code wrote the origin out with the query removed. The entry now
    never gets written at all, and the reason names the thing it actually is.
    """
    from initialize_claude_ai.mcp_auto_discovery.types import TargetOutcome

    candidate = walled_at("https://jira.example.test?format=mcp")
    writer = McpWriter(ctx)
    registrations = writer.publish(TargetOutcome(target="https://jira.example.test", provisional=[candidate]))

    assert registrations[0].refused, "the site's front door was registered as an endpoint"
    assert "front door" in registrations[0].refused
    assert registrations[0].written is False


# -- the control probe: the part that needs to know nothing about the product ---
#
# Everything above asks "does this answer look like MCP?". The control asks the
# better question -- "does this host answer anything differently?" -- and it is
# the only check here that generalises to a product nobody has heard of. A list
# of hosts whose 401s are meaningless would need an entry per Jira, Confluence,
# service desk and intranet ever deployed. An invented path costs one request.


class Recorder:
    """An http client that answers every path the same way, like a login wall."""

    def __init__(self, response: Response, status: int = 401) -> None:
        self.response, self.status, self.asked = response, status, []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def post(self, url, **kwargs):
        self.asked.append(url)
        reply = Response(self.response.headers, self.response.text)
        reply.status = self.status
        return reply


def prober_with(ctx, client) -> McpProber:
    prober = McpProber(ctx)
    ctx.http = lambda **kwargs: client  # type: ignore[assignment]
    return prober


def test_the_control_asks_a_path_that_cannot_exist(ctx) -> None:
    client = Recorder(Response({"content-type": "application/json"}, "{}"))
    prober = prober_with(ctx, client)

    prober.control_answer("https://host.example.test/mcp")

    assert len(client.asked) == 1
    assert "initialize-claude-ai-control-" in client.asked[0]
    assert client.asked[0].startswith("https://host.example.test/")


def test_it_is_asked_once_per_origin_however_many_paths_are_probed(ctx) -> None:
    client = Recorder(Response({"content-type": "application/json"}, "{}"))
    prober = prober_with(ctx, client)

    for path in ("/mcp", "/api/mcp", "/v1/mcp"):
        prober.control_answer(f"https://host.example.test{path}")

    assert len(client.asked) == 1, "one request per origin, cached for the run"


def test_a_host_that_refuses_everything_identically_proves_nothing(ctx) -> None:
    """The case the content-type rule alone cannot catch.

    A gateway answering every path with the same JSON error passes the
    "not a web page" test -- and still tells you nothing about MCP.
    """
    same = Response({"content-type": "application/json"}, '{"error":"unauthorized","jsonrpc":"2.0"}')
    prober = prober_with(ctx, Recorder(same, status=401))

    result = Handshake(transport=Transport.STREAMABLE_HTTP, url="https://walled.example.test/mcp")
    result.status, result.requires_auth = 401, True
    prober._note_auth_evidence(result, same)

    assert result.auth_evidence == [], "identical to a path that does not exist"
    assert any("refused identically" in h for h in result.protocol_hints), result.protocol_hints


def test_a_host_that_answers_differently_keeps_its_evidence(ctx) -> None:
    """404 for nonsense, 401 for /mcp -- that difference is the finding."""
    prober = prober_with(ctx, Recorder(Response({"content-type": "text/plain"}, "no"), status=404))

    result = Handshake(transport=Transport.STREAMABLE_HTTP, url="https://real.example.test/mcp")
    result.status, result.requires_auth = 401, True
    prober._note_auth_evidence(result, MCP_401)

    assert result.auth_evidence, "the control answered 404; this path answered 401"


def test_a_control_that_cannot_be_run_does_not_block_a_decision(ctx) -> None:
    """Absent evidence is not evidence of absence; carry on without it."""

    class Broken:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def post(self, *a, **k):
            raise OSError("network is down")

    prober = prober_with(ctx, Broken())
    assert prober.control_answer("https://x.example.test/mcp") is None

    result = Handshake(transport=Transport.STREAMABLE_HTTP, url="https://x.example.test/mcp")
    result.status, result.requires_auth = 401, True
    prober._note_auth_evidence(result, MCP_401)
    assert result.auth_evidence, "still decided, on the evidence it did have"


def test_the_control_can_be_turned_off(ctx) -> None:
    ctx.settings.set("mcp.discovery.auth.control_probe", "false")
    client = Recorder(Response({"content-type": "application/json"}, "{}"))
    assert prober_with(ctx, client).control_answer("https://x.example.test/mcp") is None
    assert client.asked == [], "no request at all"
