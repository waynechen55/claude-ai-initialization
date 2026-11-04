---
name: mcp-endpoint-discovery
description: Work out which URL on a service speaks the Model Context Protocol, by reasoning about the service rather than guessing, and record a four-step account of whether one exists. Use when asked to find out whether a host already speaks MCP, or to locate its endpoint URL and transport.
---

# Finding an MCP endpoint

You are configuring this operator's own Claude Code installation to connect to a
service they already use and are authorised to reach. The question is which URL
on that service — if any — accepts an MCP connection, and the way to answer it
is the way any MCP client answers it: send the connection request the
specification defines and read what comes back.

Your job is to find that URL — or to establish, **with evidence**, that there
isn't one. A guess is not a finding. An endpoint counts as found only when an
`initialize` request gets a JSON-RPC result back.

You are tier 1, and that makes this stage load-bearing for the whole pipeline.
Tier 2 (installing a package someone published) and tier 3 (writing a bridge
from scratch) are **forbidden** while the target has a native endpoint, and they
stay shut until your four-step record shows the search was actually done.
Concluding "no MCP here" after trying `/mcp` and stopping is the failure this
skill exists to prevent: it sends the run off to install or write software to do
a job the target was already doing, and somebody then has to maintain it.

## What you are looking for

MCP has two HTTP transports:

**Streamable HTTP** (current). One URL handles everything. You `POST` a JSON-RPC
message to it with **both** media types in the Accept header:

```
POST /mcp
Content-Type: application/json
Accept: application/json, text/event-stream

{"jsonrpc":"2.0","id":1,"method":"initialize","params":{
  "protocolVersion":"2025-06-18","capabilities":{},
  "clientInfo":{"name":"initialize-claude-ai","version":"0.1.0"}}}
```

A real MCP server replies with `result.protocolVersion` and `result.serverInfo`,
either as a JSON body or as a single SSE `data:` frame. It usually also returns
an `Mcp-Session-Id` header, which you must echo on later requests.

**HTTP+SSE** (deprecated, still widely deployed). You `GET` an SSE URL, and the
first event is named `endpoint` and carries the URL to POST messages to.

Omitting `text/event-stream` from Accept is the single most common reason a real
endpoint looks dead. Always send both.

## The four steps — all of them, every time

These are mandatory and you must report what each one did. You may stop early
only because you **found** an endpoint, never because you ran out of ideas.

An endpoint that answered `401` or `403` **and showed some sign of speaking the
protocol** counts as found for this purpose. It is a real MCP endpoint on the
target and it settles this stage — the caller will not run the search again — so
once you have one, finish the checklist you were in the middle of, report it,
and stop. Spending your remaining turns hunting for an unauthenticated endpoint
on a host that has already told you it wants a credential is the commonest way
this stage runs out of clock.

A bare `401` from a host that refuses everything is **not** that, and does not
settle anything — see the table below.

### A — a range of paths

Do not stop at `/mcp`. There is no registered location for an MCP endpoint, so
servers put theirs where their own URL scheme suggests. The caller has already
asked the conventional places itself; repeat any of them only to confirm what
it found, and spend your requests on what only you can do: the paths the
target's own documentation and API layout point to. The conventional places
are:

`/mcp`, `/api/mcp`, `/v1/mcp`, `/v1/api/mcp`, `/rest/mcp`, `/mcp/sse`, `/sse`,
`/ws`, `/v1/mcp/sse`, and the query form `?format=mcp`.

Then add the paths the target's *own* URL shape suggests: if its REST API lives
under `/api/v3/`, `/api/v3/mcp` is likelier than anything on the list above.
Reasoning from the service's own layout is what makes this a search rather than
a recitation, and it is what finds endpoints the list never would.

### B — schemes and certificates

Try both `http://` and `https://`. When TLS fails, a self-signed or internal CA
certificate is a fact about this network, not an answer about MCP. Verify it
against the trust bundle your brief names (`curl --cacert <bundle>`); without
one, report the issuer (below) and the URL as a lead. **Do not switch
verification off** (`-k` / `--insecure`): the caller's own probe has already
retried once without verification and recorded what answered as
`insecure_tls`, and a brief that sweeps paths with verification disabled reads
like a scan rather than a client being configured — one was refused by the
model's safeguards for exactly that. Never send a credential unencrypted to a
non-local host.

**Name the issuer when this happens.** "TLS failed" and "this host uses an
internal CA that the runtime does not ship" lead to different fixes, and the
second is the common one on a corporate network:

```bash
openssl s_client -connect host:443 -servername host </dev/null 2>/dev/null \
  | openssl x509 -noout -issuer
```

It matters beyond this stage. A tier-2 package launched by Node or Python
carries its *own* certificate store, so a target the host trusts perfectly well
can still be unreachable — `build.example.net` answered `curl` with `HTTP 200` and
gave Node `UNABLE_TO_GET_ISSUER_CERT_LOCALLY`, which reached the operator as
`failed · Connection closed` with no mention of a certificate anywhere. The
caller fixes that by naming the host's trust store in the entry, but only if it
knows the target is behind an internal CA. Your note is where it learns that.

The caller examines every target's certificate before you start. When your
brief says initialize-claude-ai verifies the target with a trust bundle of its
own, that file already holds what is needed — use it by name, and do not
extract a chain yourself. When the brief says nothing vouches for the
certificate, report the issuer: the operator fixes that with `--mcp-trust-ca`.

### C — content type and handshake headers

Send `Accept: application/json, text/event-stream` and `Content-Type:
application/json` on every request, and then **read what came back** — body
*and* headers, not just the status code. Look for: a JSON-RPC envelope (even an
error), `text/event-stream` content type or `data:`/`event:` frames, an
`Mcp-Session-Id`, a `101` upgrade or `Upgrade: websocket`, a `WWW-Authenticate`
challenge. Each of those says an endpoint is there.

### D — semantic grounding

Search for what the target says about itself: the vendor's documentation, their
blog, `/llms.txt`, `/.well-known/mcp.json`, the official MCP registry
(`registry.modelcontextprotocol.io`) and public repositories. A vendor
announcement naming the URL beats fifty requests, and this is the step that
finds endpoints on hosts you would never have guessed.

A step you genuinely cannot run here — no network, no search tool — is reported
with `unavailable` and the reason. A step you simply did not attempt is a gap,
and a gap keeps tiers 2 and 3 shut. **Do not claim a step you did not run.**

#### The vendor's cloud is not the customer's deployment

Step D is where this goes wrong, because searching for "Confluence MCP" finds
Atlassian, and Atlassian has an MCP endpoint. It is not this target's.

`https://mcp.atlassian.com/v2/mcp` serves Atlassian **Cloud** tenants —
`*.atlassian.net`. `confluence.example.com` is a self-hosted Data Center instance,
which a run established for itself by following the SSO redirect to
`idp-saml.example.com`, wrote down in step D, said in its summary — *"no native MCP
endpoint exists and none is architecturally possible for a Data Center"* — and
then reported the Atlassian Cloud URL as the target's native endpoint anyway.
It closed the package, bridge and strategy stages, so `mcp-atlassian`, which does talk to Data Center
given `CONFLUENCE_URL` and a PAT, was never looked for. The one target with a
real tier-2 answer got the only wrong tier-1.

So, before reporting an endpoint on a host that is not the target's:

- **Whose address is it?** A vendor serving its own service from a sibling
  domain is ordinary — `github.com` from `api.githubcopilot.com`,
  `developers.google.com` from `developerknowledge.googleapis.com`. A vendor
  serving *its own* cloud while your target is somebody's private installation
  of that vendor's product is a different service that happens to share a name.
- **Can it even know which instance you mean?** A multi-tenant endpoint has to
  be *told*, and if nothing in the entry says `confluence.example.com`, nothing
  will. Report it only if you can name the tenant.
- **Did your own evidence already answer?** If step D concluded the target is
  self-hosted, do not then report the vendor's hosted endpoint as its native
  one. Leave it out of `due_diligence.native_endpoints` and `candidates`, and let tier 2 run.

Your finding is re-judged downstream, and an unproven endpoint on another
organisation's domain no longer closes the lower tiers — but you are the one
who can say it plainly, and saying it saves a run.

#### A name match is not a native endpoint

The registry's `search=` matches on the server's *name*. `search=confluence`
returned "0DTE Confluence Pro" at `https://mcp.0dteconfluence.com/mcp` — a
trading tool whose name happens to contain the word. It handshaked, listed 37
tools, was recorded as `confluence.example.com`'s native endpoint, and closed the
package, bridge and strategy stages; the writer then refused it, because nothing
tied it to the target, and the address finished with nothing.

An endpoint found by searching is this target's only when something ties it to
the target: it is on the target's host or organisation, the vendor publishes it
for this deployment, or the server itself names the target (a resource URI, a
tool default, its instructions). A matching word in a name or a search hit ties
nothing. Endpoints are now checked against the same registration rules before
they count: one that fails is **ruled out**, recorded with the reason, closes no
tier, and will not be accepted again from any route — so proposing it again
only costs a turn. If your retry prompt says an endpoint was ruled out, look
elsewhere, or propose it only with evidence that ties it.

#### A substring is not a name, and a publisher is not the vendor

The official registry's `search=example` returned `io.github.mcp-dir/examplep_administradores-mcp`,
a Brazilian insurance regulator's tool hosted at
`https://api.mcp.ai/p_examplep_administradores`. It handshaked, and it was
registered for `gitlab.example.net`, `jira.example.com`, `confluence.example.com`,
`openqa.example.net`, `sd.example.com`, `build.example.net` — and `github.com` — because
three tests read substrings:

- **"example" is not in "examplep".** A product or organisation is named by a whole
  word (`example-mcp`, `Example's`), a host label (`docs.github.com`), or the
  registrable domain a vendor serves from (`api.githubcopilot.com`,
  `developerknowledge.googleapis.com`) — never by a string that happens to
  contain it.
- **`io.github.<account>/…` is that account, not GitHub.** A registry namespace
  under a hosting service names whoever logged in to publish; only a
  domain-verified namespace (`com.googleapis.developerknowledge/…`) is the
  domain's owner speaking. The part after the slash is the publisher's own
  choice of name.
- **A URL path is nobody's name.** `/p_examplep_administradores` says nothing
  about who runs `api.mcp.ai`.

And the check that would have caught it whatever the spelling: **one endpoint
that fits several unrelated targets fits none of them.** If the same remote
would be your answer for two services of different organisations and names
neither, it is a generic platform, not this target's server. The writer now
refuses the second claim and withdraws the first (`mcp.registration.refuse_shared_claims`).

#### What the server says it is outranks what led you to it

The pattern behind that incident, for every server and every target: **a server
is this target's only if something about the target says so.** There are three
such facts, and at least one must hold:

1. **Where it is** — its host is the target's, a sibling of it, or a domain of
   the target's organisation.
2. **What it is configured for** — the entry itself names the target (a base
   URL, a host argument, a variable that points at it).
3. **What it says it is** — once it answers, its `serverInfo.name`,
   `instructions` or tool names name the target or its product as a whole word.

Everything else is a claim: a search hit, a registry listing, a README, your
own reasoning, and **an entry this program registered on an earlier run**. A
claim can lead you to a server; it never makes the server the target's. The
`api.mcp.ai` endpoint answered as `MCP.AI platform … consultas` with tools
`marketplace`, `connect`, `examplep_administradores_consultar` — nothing in that
answer is Example, so no claim could make it Example's.

- **A catalog is nobody's server.** A remote whose own answer calls itself a
  `marketplace` or `catalog` of other tools fronts whatever it lists; it is
  never a target's server on a claim alone, even if one of its listings has the
  right name (`mcp.registration.identity.gateway_markers`).
- **Quote the answer.** When you propose a remote on a host that is not the
  target's, say what its `instructions` and tools say it is. If that answer
  never names the target, drop it — the writer will refuse it anyway
  (`identity_doubt` in `grounding.py`).
- **An earlier registration proven wrong is removed.** When an entry this
  program named for an address points at an unrelated host and its own answer
  names something else, the run that proves it reports `retire <name>` and
  removes it from `.mcp.json` and the `claude` CLI
  (`mcp.registration.retire_refused_existing`). Only names this program gives
  the address are removed; any other name is the operator's and is reported
  with the command to remove it.

## Interpreting what comes back

| Response | What it means |
|---|---|
| `result.protocolVersion` present | Found it. Record the URL, transport, server name and version. |
| 401 / 403 **with something protocol-shaped** — a JSON-RPC body, an `Mcp-Session-Id` or `MCP-Protocol-Version` header, `resource_metadata=` in the challenge, any content type that is not HTML | **An endpoint exists** and needs a credential. A positive finding — report it as such, with the `WWW-Authenticate` value, **and report it as a candidate** (see below). It still forbids tiers 2 and 3. |
| 401 / 403 that is just an **HTML login page** | Not a finding. A Jira, a Confluence, a service desk or any intranet answers this on every URL it owns, including ones that do not exist, so it says nothing about MCP. Do **not** report it as a candidate: the caller would register the website as a server, and no credential could ever make that work. Say in your notes that the host walls everything, which is why probing it cannot settle the question. |
| 401 / 403 whose **body tells a person to log in**, whatever content type it arrived as | The same thing wearing a disguise, and the one that got through. `confluence.example.com` answers an anonymous POST with `{"statusCode":401,"message":"You are not logged in. <a href=\"/login.action\">Log in</a>"}` — `application/json`, so "not a web page" was true, and it was registered as an MCP endpoint. Read the body, not just the header: "not logged in", "please sign in", a `<form>`, an `<a href>`. No MCP endpoint has any reason to say those. |
| 404 / 405 | Not this path. Move on; don't retry with other protocol versions. |
| 406 | You probably omitted `text/event-stream` from Accept. Retry with both. |
| 426, or `101` with `Upgrade: websocket`, **and a sign the socket carries MCP** — it accepted the `mcp` subprotocol you offered in `Sec-WebSocket-Protocol`, the route is named for MCP (`/mcp/ws`), it answered a JSON-RPC message, or the product's documentation says so | A native endpoint on a transport Claude Code cannot dial. Report the URL and say a stdio bridge is needed **for that endpoint** — this is the one case where tier 3 is the right answer. |
| An upgrade with **no** such sign, or an `Upgrade: websocket` header on a 4xx | The service's own socket — live updates for its web pages — not an MCP server. `openqa.opensuse.org`'s `/api/v1/ws/` answers `404` with `Upgrade: websocket`; counted as MCP, it closed the package tier and asked for a bridge to something that has never spoken JSON-RPC. Mention it in your notes; do **not** put it in `websocket_urls` or `endpoints_needing_a_bridge`. Every URL you put there is probed again, and one without MCP evidence is ruled out. |
| 200 or 400 whose JSON body refuses a credential — `{"error":"no api key"}`, "invalid personal access token" | A credential wall that did not use 401. Treat it exactly like a protocol-shaped 401/403: an endpoint that needs a credential, reported as a candidate (see below). |
| 2xx that is neither JSON-RPC nor a refusal | **Inconclusive**, not "no MCP here" — a proxy or front end may have answered for the endpoint. Say what came back (status, content type, the first words). If the product's documentation says this URL takes a credential, propose it with `requires_auth` (see below). |
| JSON-RPC error `-32602` | The server speaks MCP but not the version you offered. Retry with an older one from the list you were given. |
| TLS handshake failure | Not an answer. Verify against the run's trust bundle, or name the issuer, before concluding anything (step B). |
| 200 with HTML | A web page, not an endpoint. Mine it for links and move on. |
| Connection refused / DNS failure | Say so plainly. Do not report a network problem as "no MCP support". |

## An endpoint that will not talk to you without a credential

A 401 or 403 is the single most useful "failure" you can get — **when it came
from something that speaks the protocol.** The server has told you it is there,
it is listening, and it wants to know who you are, which is more than a 404 ever
tells you.

First make sure that is what happened. Plenty of hosts refuse anonymous clients
on *every* URL, including paths that do not exist — try one you invented and see.
If the invented path is refused the same way, the status is the site's login
wall and carries no information about MCP, and a candidate reported from it
becomes a `.mcp.json` entry pointing at a web page. Look for a JSON-RPC envelope,
an `Mcp-Session-Id` or `MCP-Protocol-Version` header, `resource_metadata=` in the
challenge, or any content type that is not HTML. One of those is enough; an HTML
login page and nothing else is not.

Do not stop at recording the URL in `due_diligence.auth_required_endpoints`.
**Report it as a candidate as well**, with:

- `confidence` below 0.5 — you did not complete a handshake and must not imply
  you did;
- `secret_env` naming the variable the token should come from: the name your
  brief gives you if it gives one (it is the name the entry will read), else
  whatever the vendor's own documentation calls it (`GITLAB_TOKEN`,
  `JIRA_API_TOKEN`), else nothing — the caller derives a name from the server's
  own name when you leave it out;
- a line in `notes` saying which scheme the `WWW-Authenticate` header asked for.

The caller writes such an endpoint into `.mcp.json` with the credential slot
left **empty** — `"Authorization": "Bearer ${GITLAB_TOKEN}"` and no value
anywhere — reports it as needing a token, and never counts it as working. That
turns "the operator finds a token" into a one-line export rather than a second
full discovery run. Leaving the endpoint out of your candidates is what throws
that away. The slot holds the credential itself, inside whatever form the
server takes it in, never one variable that must hold the whole form. It is
never removed to make something start, and a login an existing entry already
supplies for the address is kept, optional, even where the server also works
without it. An answer whose text states "401" or "403" is a refusal even when
it isn't flagged as an error: never report it as a working call.
"Connected" in Claude Code is not "works" either: a warning status ("! Connected
· tools fetch failed") means its tools could not be listed.
Never add runtime tuning (network timeouts, family selection) to a candidate's
settings unless a trial without it failed; the entry runs on other machines.
Propose the `https://` form of an endpoint that takes a credential. A token is
never sent over plain HTTP: openQA answers "personal access token can only be
used via HTTPS", and the caller moves an `http://` endpoint to `https://`
whenever it answers there.

### Name the variable after the *shape* of the value

If the credential is **not** a single opaque token, say so — report its `shape`
and where to get one, and never name the variable after one part of a
multi-part value:

```json
"secret_env": {
  "OPENQA_OPENSUSE_ORG_API_KEY": {
    "reference": "env:OPENQA_OPENSUSE_ORG_API_KEY",
    "shape": "username:key:secret",
    "obtain": "https://openqa.opensuse.org/api_keys"
  }
}
```

The caller renames the variable from the shape — `..._USERNAME_KEY_SECRET` —
so the name itself tells the operator what to set. This is not cosmetic: openQA
was registered as `..._API_KEY`, somebody set just the key, and the server
answered 403 with nothing to explain it.

`shape` is a description, never a value: `username:key:secret`,
`email:api_token`, `username:password`. A plain string still works when the
credential really is one opaque token.

You still never send a credential yourself, and you never invent one to test
with. The proof that this is an MCP endpoint is the challenge itself, plus
anything credential-free that corroborates it: a `WWW-Authenticate` naming an
OAuth realm, `/.well-known/oauth-protected-resource`, an `Mcp-Session-Id` or
`MCP-Protocol-Version` header on the rejection, a JSON-RPC error envelope
rather than an HTML login page.

Then **propose it** — even though you have no credential to get past it. An
agent that met a wall and proposed nothing lost the one endpoint the target had.
Report it as a candidate with:

- `"requires_auth": true`;
- `"auth_evidence"`: one sentence on what showed it — the refusal you saw, the
  documentation page that says the endpoint takes a token;
- `"secret_env": {"NAME": "env:NAME"}`, named as above (your brief's name
  first, auth profile included) — **a reference, never a value**.

The caller probes it independently. A protocol-shaped refusal, or an
inconclusive reply backed by your `auth_evidence`, is registered with an empty
`${NAME}` slot for the operator to fill; a login page is still refused.

**Its tool definitions must be ones the client can load.** Every tool name and
input property key must be a plain identifier -- letters, digits, `_`, `.`, `-`,
at most 64 characters (`mcp.verification.client_name_pattern`). openSUSE's
`osc-mcp` declares a property named `path_repository jsonschema:` on
`search_packages`; once that tool is loaded, the API refuses *every* request of
the Claude Code session. Look at its `tools/list` before you propose a server;
the caller checks it too and refuses such a server by default
(`mcp.verification.schema_problems`).

## Rules

- Never report an endpoint you did not successfully handshake with **as
  verified**. If you found something promising but could not complete
  `initialize`, report it as a lead with `confidence` below 0.5 and say exactly
  what happened. An endpoint that answered 401 or 403 is exactly such a lead,
  and it must be reported — see the section above.
- **Report the URL you actually probed, exactly as you probed it** — every
  query parameter, every path segment. Do not tidy it, and do not report a
  "cleaner" URL you did not request. `https://confluence.example.com?format=mcp`
  answered 401; the query was tidied away before writing and the entry became
  `https://confluence.example.com`, an address nothing had ever tested, which is a
  login page.
- **A bare origin is not an endpoint.** `https://host` with no path will only be
  registered if it genuinely completed a handshake there. MCP endpoints live at
  a path; a site's front door refusing an anonymous POST looks identical to one
  and is not one. If the only thing that made a URL look like MCP was a query
  string *you* added while probing, say so in your notes.
- **If you believe an endpoint exists, it must appear in `candidates` — not
  only in your prose.** This is the most expensive mistake available to you.
  Listing X in `due_diligence.native_endpoints` closes the package, bridge and
  strategy stages for the whole address, because the caller takes you at your
  word; if X is not also a candidate, nothing proposes it for registration, and
  the address can finish with **nothing at all** having refused every
  alternative on your say-so. That is exactly what happened to
  `developers.google.com`: the endpoint was recorded but never proposed, the
  lower tiers were shut, and the file got no entry. (A URL only in prose closes
  nothing -- and is never probed either.) A URL you are confident enough to
  mention is a URL you are confident enough to propose.
  Every endpoint the record believes in is now probed before the next stage
  runs, and one that does not answer — or that could not be registered for this
  target — is ruled out and the tiers it closed reopen. That is a safety net,
  not a substitute: proposing it is what gets it registered.
- **The endpoint may be on a different hostname, and that is fine** — report it.
  A vendor commonly serves MCP from a sibling domain: `github.com` is served
  from `api.githubcopilot.com`, `developers.google.com` from
  `developerknowledge.googleapis.com`. Such a URL is believed because *you
  probed that address and it answered* — a completed handshake, or a `401`/`403`
  carrying protocol-shaped evidence. Say in `findings` where you learned of it
  (the vendor's documentation, a `.well-known` document, a registry `remotes[]`
  entry), because that is what turns "some other host" into "this host's MCP
  endpoint". A URL on another host that never answered is not a finding.
- Read-only: `GET`, `HEAD`, and the JSON-RPC `initialize` / `tools/list` calls,
  and nothing else. Do not call any tool the server advertises. Asking a server
  what it can do is discovery; using it is not.
- Never put a credential in a URL, a command line, or your output. If a target
  needs a token, name the environment variable it should come from and put it in
  `secret_env`.
- Name servers after the service, from the fully qualified domain name:
  `bugzilla-example-com`, never `bugzilla`. Short names collide, and a collision
  means a working configuration silently points at the wrong host. The final
  registered name is composed from the address plus your name, and a whole
  repeated address is dropped rather than written twice — so repeating it is
  harmless, and saying what the endpoint *is* is what helps.
- **The workspace is shared; your address's corner of it is not.** Other
  addresses may be worked at the same moment by other agents in the same
  workspace. Anything you write goes under the artefacts directory the brief
  names for *this* address; the skills directory and other addresses' context
  files are read-only to you.
- **Group your requests.** One command that works through several paths costs
  one turn; the same paths one at a time cost several. You have a turn budget,
  and running out of turns halfway through produces a worse answer than a
  narrower search that finished and said so.

## If you are being retried

The prompt will say so, and it will list what went wrong last time: which
candidate did not complete a handshake and with what error, which one was
discarded before it was tried. That list is the point of the retry. Do not send
the same candidates back unchanged, and do not restate the same conclusion in
different words.

Pick up where the last attempt stopped:

- An endpoint listed as **ruled out** is closed. It answered, but not as this
  target's server; no environment variable fixes that. Do not propose it again
  without evidence that ties it to the target.
- A candidate that failed for a fixable reason is worth fixing — a package name
  that does not exist, a transport the server does not speak, a missing
  environment variable, a launch command that needed a shell.
- A lead you had not followed yet is worth following now. Another registry, the
  vendor's own documentation, a path you did not try.
- An empty answer is a real answer, but only once the leads are genuinely gone.
  When you report one, say which ones you exhausted, so the next attempt — and
  the person reading the report — does not repeat your search.

## Another kind of API is not an endpoint — but write it down

While you probe you may meet an API that is not MCP: a GraphQL endpoint (it
answers `{"query":"query { __schema { queryType { name } } }"}` with
`"__schema"`), an OpenAPI description, a Swagger UI page. None of them is a
tier-1 answer, and none of them closes tier 1. But each is what tiers 2 and 3
need: a generic published server for that kind of API (`mcp-graphql` for
GraphQL), pointed at the URL you found, is often the whole answer for a target
that has no MCP server of its own. Record each in `findings.api_kinds` as
`{"<kind>": {"label": "<Kind>", "url": "<its URL>"}}`, on the target's own host.
The caller checks it independently and hands it to the tiers below.

The same user name and password (single sign-on) commonly works on several
servers; that's expected, not a misconfiguration. A public server that answers
reads with no login is working, and its credential is optional.

Read the login challenge of the target and of its API host (`api.{domain}`):
a `Signature`, `Negotiate` or `NTLM` challenge is a login only a client can
perform, and the tiers below must know it.

An endpoint is accepted only when it passes the usage contract (`[mcp.contract]`):
real reads, the operator's own checks included, not just a handshake.

**A setting is proved by the tool that reads it, on the binary that runs.**
build-opensuse-org shipped with `OSC_MCP_API` set to the website
(`https://build.opensuse.org`). Only `search_packages` reads that setting, and it
needs three arguments, so nothing ever called it. A refused login was also never
tried on the API host. Now:
- every read tool nobody could call is asked once with stand-in arguments, and
  only "does it accept its settings" is read from the answer (`probe_settings`);
- address settings are also tried on the hosts that serve the address's API
  (`try_api_hosts`);
- a login refused while an address setting points off the API host is tried on
  that host before the credential is blamed;
- a literal credential that equals a variable you export makes the entry read
  that variable.

Which form is right can differ between versions of one server: never settle it by
reading source code, only by calling the tool.

**A login wall is evidence, and every route and key is tried.** sd.example.com came to
refuse every API path to anonymous callers. Recognition and API detection read
only a `2xx`, so "product unknown", the customer API never found, and a token
chosen for a password service. Now:
- a path that answers `401`/`403` in an API's form, while a path beside it that
  cannot exist answers otherwise, is the product or API behind a login
  (`api_kinds_behind_login`, `recognise_behind_login`);
- a timeout while identifying counts as undecided;
- a "needs no login" call answered with a login refusal means the bridge is
  waiting for a credential, not broken (`public_call_login_markers`);
- a client bridge is built for every route the service offers (password,
  SSH signature), each with its own manifest, and an entry whose manifest reads
  other variables is broken (`check_manifests`);
- the SSH key is optional: the client tries the standard key files the way ssh
  does, and reports each key as refused or unusable, never "set SSH_AUTH_SOCK";
- a username and password held for another address of the organisation is
  offered where none is held, all slots at once.

A location (`*_SOCK`, `*_FILE`) is not a credential. When every login you were
given is refused, say so plainly and name what to check.

**Every address that has a server ends with an entry in `.mcp.json`.**
bugzilla-opensuse-org, developers-google-com and confluence-example-com each went
missing from the file a different way. Confluence's existing `uvx mcp-atlassian`
was "held" against the agent's `mcp-atlassian@0.23.1`, a copy of itself. So:
- a copy of the existing server (same package and hosts, or same URL and
  transport) counts as the existing entry, and takes its credential slots;
- after each address, and again at the end of the run, the file is checked
  (`mcp.registration.check_in_file`). A lost entry is written again, and a
  working kept one is recorded. An address that has a server but no entry is
  reported with every refusal;
- a stage whose working server was refused reads PARTIAL, never OK.

When this machine already has a server for the address, propose that same package
or URL: it is one server, and it must reach the file.

**Belonging is judged again as evidence arrives, and a kept entry is written.**
developers-google-com (connected, and verified with its credential) was refused at
reuse, "nothing ties it to developers.google.com", before the endpoint stage found
developers.google.com documenting that very URL. The agent's copy then came without
the credential and got 401, and nothing was written: the server stayed in the CLI
only. Now:
- a proven server refused only for want of evidence is kept aside and judged again
  (`mcp.registration.rejudge_identity`);
- what ties a server to the address covers every copy of it, so the kept, working
  copy carries the evidence and the credential slots of the others
  (`inherit_credential_slots`);
- a rediscovered remote copy takes the existing entry's slots;
- a working entry kept in place is written to `.mcp.json` too, as `${VAR}`
  (`record_kept_entries`).

When you propose a server this machine already has, propose the same URL or
launch, so the two are recognised as one server, and name the credential it
already sends.

**An address identified only partly settles nothing; a replacement must prove more.**
sd.example.com answered `502` while it was being identified ("product unknown"), its
customer API went undetected, and a waiting mcp-atlassian closed the bridge stage.
build.example.net's openapi-proxy, which still searched packages, was replaced by a
server that could do nothing until an unset variable was exported. So
identification cut short by "try again later" is asked again
(`mcp.discovery.identify_retries`). While it stays undecided, nothing unproven
settles the address (`undecided_keeps_searching`). An existing entry stays over a
replacement that passed less of its job and proved no login
(`mcp.registration.keep_more_proven_existing`). Don't propose a replacement that is
less proven than what is there. When every login you were given is refused alike,
say so plainly; the entry is not what is wrong.

**Judge by the address's job, and use what the operator holds.** build-example-net
"worked" through mcp-openapi-proxy on a public `/about`: none of its path-named
tools matched the build service's task, so its login was written optional, while
the shell already exported `BUILD_EXAMPLE_NET_BASE64_OF_USER_PASSWORD`. sd-example-com
was offered only jira.example.com's token and a password ruled out by an `OAuth`
challenge. So the caller:
- takes the job from what the address is (`[mcp.contract.products.*]` of its
  recognised product and detected API kinds, `mcp.contract.address_job`). A
  server counted as working that cannot be asked that job settles nothing, and
  "works without a login" needs a passed task, not a public call
  (`public_needs_the_job`), from a launch that really lacks the credential:
  the caller withholds every credential a launch is not given from what it
  would inherit (`withhold_unsupplied`) -- github.com's "no credential" trial
  inherited the token and the token was called optional. When you test
  without one yourself, unset it (`env -u NAME …`): your shell may export it;
- asks a product's own task only where the address is that product
  (`[mcp.contract] scope_product_tasks`; a task with `arguments`, or
  `scope = "address"`, belongs to its product). GitLab's `currentUser` query is
  never put to smelt's GraphQL API. A call refused as a request the server
  doesn't have (400, "Cannot query field") means the task doesn't fit, which is
  not a failure. Don't declare a credential slot for an API that answered
  anonymously: a slot whose tasks pass without it is written optional
  (`job_done_is_no_login_needed`);
- breaks ties among unproved candidates by evidence, never by confidence: a
  scheme the host refused before goes last, the login the API host asks for
  and fewer values to supply come first (`[mcp.discovery.auth.evidence]`). The
  others are kept for the next run (`[mcp.alternatives]`). So propose every
  route the service offers. A stated HTTP status outranks wording: 403 means no
  access, not an invalid token (`status_outranks_wording`). A CA bundle must
  reach a self-signed root (`mcp.trust.complete_to_root`). Files you build are
  moved out of the scratch area when written (`own_files`). Never write a
  credential value into an entry (`move_literal_secrets`);
- knows that the program checks free space before every install and stops on
  a full disk (`install.check_space`, `install.stop_on_full_disk`). Never work
  around a "disk is full" verdict; report it;
- reads "fetch failed" or a certificate error as the runtime never reaching
  the service. That is this machine's trust or network, not the server. The
  program names the runtime's trust variable (Node keeps its own store:
  `NODE_EXTRA_CA_CERTS`) and retries (`heal_transport_everywhere`). Never send
  the same launch back unchanged for it. A kind with operations as data
  (GraphQL) gets this program's own bridge when its generic servers fail;
- names each credential variable in `env` exactly as the script you launch
  reads it. The final file check renames a slot the script never reads, or
  reports it (`align_slots_in_file`). Generated files end up beside
  `.mcp.json` (`mcp-servers/`, `ICA_MCP_SERVERS`);
- names each tool you write for what it does, and describes it plainly
  (`introspect_schema` -- "describe the GraphQL API"). The address's job is
  found by a tool's name, its description, or an answer in the API's own form
  (`match_descriptions`, `match_answers`). Give every candidate `"kind":
  "stdio"` (a command) or `"url"`. A verified candidate that didn't settle the
  stage comes back with the reason: keep it and fix exactly that;
- never installs a runtime or toolchain itself, or a second copy beside the
  one that is there: the program installs a missing one from its catalog,
  through whatever owns the existing copy (`--mcp-provision-missing`);
- credits an `api.{domain}` host only when it is the same product
  (`api_host_same_product`), and tries a method a challenge left out with a
  stand-in nobody owns (`try_unoffered_methods`);
- reads what the operator already exports (`[mcp.discovery.auth.inventory]`,
  names and shapes only) and offers it to a waiting server. A slot taking an
  encoded login gets `Authorization:Basic ${THEIR_VARIABLE}`, or is built at
  start-up from the parts (`[mcp.registration.compose]`). A login held for
  another address of the same organisation is tried once (single sign-on);
- treats a refusal from a token other hosts' entries send as no verdict here
  (`foreign_credential`);
- keeps an entry Claude Code reports connected unless a replacement proves the
  job, reads CLI entries from Claude Code's own configuration (`claude mcp get`
  hides `${NAME:-default}`), and removes CLI copies scope by scope;
- reports a first start slower than Claude Code's wait (`MCP_TIMEOUT`).

Propose servers that can do the address's job. Name its credential by the
parts the operator holds. Never ask for a new variable when one they export fits.

**"Try again later" is not a verdict, and one server is one entry.** A
`429 Too Many Requests`, a `502`/`503`/`504` or "rate limit" says nothing about
the entry: bugzilla.example.com's working entry was judged broken on a 429 that
the run's own checks had caused, "repaired", and written a second time beside an
agent's copy of it. The caller paces its calls to each host
(`[mcp.verification.pacing]`), asks once more after a short wait, and treats an
answer that still says "later" as inconclusive (`temporary_markers`,
`temporary_statuses`) -- never a failure, never a reason to repair. Space your
own calls to a service the same way. The same package (or URL) reaching the same
host with other flags -- `--read-only` added, another version -- is a copy of
one server, not another server (`mcp.registration.fold_copies`): propose a copy
only when it does more than the existing one, and expect only one of them to be
written. A repair is kept only when the original, asked again, still fails.

**A damaged file is repaired, not mistaken for a broken server.** Every file
this program generates (manifests, bridge scripts) is written atomically, so a
full disk can never leave one half-written. An existing entry that failed and
names a file this run has since rewritten is judged again before anything is
written (`mcp.registration.retrial_rewritten`). A reused generated bridge knows
the API kind it serves from its manifest's operations, and a stage picks, among
equally proved candidates, the one built for an API this host serves
(`prefer_detected_api`). A call that never reached the service -- `fetch failed`,
a certificate error, a refused connection (`transport_failure_markers`) -- is a
connection to repair: the entry's trust store is pointed at a bundle that works
and it is proved again before it is judged (`heal_transport`).

**An entry is judged the way Claude Code loads it.** The written `type` is the
transport the handshake was proved over -- never an agent's word: a url entry is
`http` (or `sse`), a command entry `stdio`, and a url entry carries no `env`
(`mcp.registration.transport_aliases`). openqa-example-net and developers-google-com
were written `"type": "stdio"` beside a `url`, and Claude Code skipped both.
Every entry is checked against Claude Code's own rules -- its shape
(`entry_shapes`), then Claude Code itself, in a throw-away configuration
(`validate_with_claude`) -- and one it refuses is never written or registered;
a CLI copy removed for a replacement is put back if the add fails
(`restore_on_failure`). After writing, Claude Code connects to each entry with
no credential (`claude_check.connect`): a server slower than its start-up wait
is started several times, the slowest counts (`client_timeout.samples`), and a
wait that fits is set in Claude Code's own `settings.json` (`persist_to`). Then
every written server is checked for whether Claude Code will load it -- switched
off by `disabledMcpjsonServers` in a settings file, or waiting for approval
(`load_check`) -- and the file and key that decide are named.

**What was sent decides a verdict.** A 401 to a call that carried no credential
means the slot is missing, never "REJECTED"; a success with none means public,
never "accepted" (`refusal_needs_a_sent_credential`). A server that only waits
for a credential settles nothing over an entry known to work here: the lower
tier's existing entries are re-checked (`recheck_when_settled_unproven`) and the
one that worked keeps its place (`worked_before_wins`). A tier that settles the address settles it only until the final checks: a server withdrawn later (the re-check, the start check, Claude Code's own check) hands the address over -- another proven server, one waiting for a credential, then the tiers it had closed, existing registrations first (`[mcp.handover]`) -- and a verdict reached before the address was known (its product, its API kinds) is reached again under what it is (`rejudge_stale_verdicts`). So what you propose is judged against the address's own tasks, never a generic one alone: gitlab.example.net's mcp-graphql passed "describe the API", settled the package tier, and was withdrawn once GitLab's "read the current user" was asked. A tool whose query the
service refuses -- "Query too large" -- cannot do its job with any credential
(`job_refused_markers`). An optional credential slot must be one the server
reads: a stand-in there has to change its answers, else it is moved to the
variable the server's `--help` names (`mcp.verification.delivery`). One user
name and password on several services is allowed, each entry with its own
address; a refusal is only that service's answer. A credential an entry
carried as a literal is never written to `.mcp.json`: it is kept in Claude
Code's own `settings.json` `env` under the variable the entry reads
(`keep_credentials_in`), and the login plan asks for the kind of credential an
entry already sends the host before another address's login
(`prefer_in_use_methods`). Checks through
Claude Code see only what a new terminal's Claude Code has
(`[mcp.registration.fresh_env]`); a value an earlier entry supplied is kept in
Claude Code's settings env (`keep_supplied_values`); and a login a host refused
before is tried last (`[mcp.discovery.auth.history]`). No kind of login
is preferred -- an SSH-key signature, a token and a password count the same,
and proof decides (`methods_equal`); a refusal identical to the anonymous answer
means the credential may not have arrived, and is never counted as refused; a
credential slot must be a variable the script's own source reads
(`align_slots_to_source`); and connected alone holds no place
(`connected_is_not_working`). An existing
entry kept in place of a replacement is always written to `.mcp.json` too, and
an address with a server never ends without an entry there
(`held_must_be_recordable`, `repair_missing_entries`). Discovery is
unattended: nothing it starts can open a browser (`[mcp.noninteractive]`); do
not propose a launch that needs a browser login unless the service also offers
a token route, and say which.

**An entry must survive the next run.** Write → reuse → write is a fixed point:
an optional `${NAME:-}` that expands to nothing for a trial is written back as
that reference, never dropped -- build-example-net's username slot was deleted by
every run's reuse this way (`tests/test_mcp_entry_round_trip.py`). A login
refused on one route is answered with the other routes the service offered, each
with its own setup step (`name_other_routes`): build.example.net refused a Example
password over basic auth while api.example.net asked for an SSH-key signature. A
refusal that says nothing was sent ("not set", "no SSH key") never ranks a
candidate as refused.

**The result is checked, not a path.** Once everything is written, every entry
of the run's addresses is read back from the file and compared with what it
launches reads, and repaired in place (`mcp.registration.verify_written`). The
claude CLI's copy -- which loads before the file's -- is replaced by the file's
entry when it lacks a slot, or differs from an entry this run verified
(`replace_divergent_cli_copies`). Each server's summary names the variable that
moves every machine-specific path in its entry (`say_how_to_move`), and a
credential written as a `${NAME:-default}` default is reported as removed, with
where to keep it instead (the shell, or Claude Code's settings `env`).

**An entry is checked as written, and repaired before it is replaced.** Every
entry, new or kept, is compared with what it launches reads -- a generated
bridge's manifest -- and a missing credential slot is added as `${NAME:-}`
(`mcp.registration.complete_slots`); an existing entry whose only fault is such
a slot is repaired and kept (`mcp.discovery.reuse.repair_missing_slots`). A
local server's slot keeps the key the server reads; only the variable it points
at is named for the operator (`keep_server_slot_keys`). A generated bridge says
"the entry does not pass X -- add it to its env" when a variable never arrived,
and "X not set -- export it" when it arrived empty: a client passes a server
only its entry's env. A kept bridge's credential is described from its manifest
(`describe_from_manifest`). An installed program replaces a workspace build when
it reaches the same outcome -- both work, or both wait for the same login.
Template fixes reach kept entries: an older copy of a bridge this program
generated is refreshed before it is tried (`refresh_generated_scripts`). What
last worked is tried first, never instead of what is registered now
(`mcp.known_good.try_current_too`), and the current registration keeps its name.

**What a server runs, where it lives, and whose login it is.** A bridge that
drives a client (`osc`) needs that client at every call: the caller installs it
first (`[mcp.discovery.clients.<name>] install`, `mcp.discovery.clients_install_missing`),
and a bridge whose client cannot be installed is broken, never "waiting for a
credential". Never point an entry at a program you built in your workspace when
the same program is installed: the caller switches to the installed one when it
answers alike (`mcp.registration.prefer_installed_program`). A login slot is
named for what it holds -- `{SLUG}_USERNAME`, `{SLUG}_PASSWORD`
(`mcp.registration.name_by_role`) -- so one export serves every route to the
service, and described as the account its organisation issues
(`mcp.bridge.generated.auth.account`). A login one service refuses that a
service of another organisation accepts is reported as that service's login
(`name_other_service_login`): build.example.net needs a Example account, not an
openSUSE one.

**Same organisation is not the same service; broken waits for nothing.** One
organisation runs many services: Example's QA dashboard (`dashboard.qa.example.net`)
is not smelt.example.net, though both end in `example.net`. A sibling host counts for
an address only with a tie -- it carries only generic labels (`api.`, `mcp.`),
or one of the address's product words, or the server's own answer names one
(`mcp.registration.identity.organisation_needs_tie`). Propose a sibling only
with that tie, and say what it is. A stage the tier policy closes still
re-checks the registrations already on this machine at that tier
(`mcp.discovery.reuse.recheck_when_blocked`), so a working entry is compared
with what closed the stage, never hidden by it. A candidate already proven
broken -- it does not start, or its entry disagrees with the manifest it
launches -- is never written as "waiting for a credential". Variables the
login session sets (`SSH_AUTH_SOCK`, `KRB5CCNAME`; `mcp.env_contract.session_variables`)
keep their own names as `${NAME:-}`: never rename one per server, never ask
the operator to export one. An API detected behind its login is the same API
wherever it is confirmed; between two servers waiting for a credential with
nothing proved, the one built for the API this host was seen to serve takes
the slot (`mcp.discovery.auth.prefer_detected_api`). A refusal that says the
credential is "not set" or that there is "no SSH key" means nothing was sent:
it is reported as missing, with what to supply per route
(`missing_credential_markers`), never as a refused login.

A page anyone can read (`/about`, `/version`) proves the service is there,
never that a login works: the caller proves logins only with calls that fail
without them.

Record one only from a **2xx** answer in that API's own form — JSON that names
what the API names (`data.__schema` for GraphQL; `version`, `platformVersion`
and `_links.self` for a service desk's `/rest/servicedeskapi/info`). An answer
that contains the path you asked for is not evidence: error pages repeat it.
smelt.example.net's HTML 404 for `/rest/servicedeskapi/info` names that path, and
a run that took the path as proof built a service-desk bridge for a GraphQL
service — and wrote it over the entry that worked. The caller asks your URL
again, and asks a path beside it that cannot exist; if both answer alike, your
record is dropped.

## What to output

Finish with a single fenced JSON block, and nothing after it:

```json
{
  "summary": "one sentence on what you found and how",
  "candidates": [
    {
      "kind": "url",
      "name": "target-example-com",
      "url": "https://target.example.com/mcp",
      "transport": "streamable-http",
      "source": "named in the target's documentation at /docs/mcp",
      "confidence": 0.95,
      "secret_env": {"TARGET_API_TOKEN": "env:TARGET_API_TOKEN"},
      "notes": ["server identified itself as example-mcp 1.4.0"]
    },
    {
      "kind": "url",
      "name": "target-example-com-authenticated",
      "url": "https://target.example.com/v2/mcp",
      "transport": "streamable-http",
      "source": "answered 403 {\"error\":\"no api key\"} to an anonymous initialize",
      "confidence": 0.4,
      "requires_auth": true,
      "auth_evidence": "403 with a JSON refusal naming an API key; /docs/mcp says the endpoint takes a personal access token",
      "secret_env": {"TARGET_EXAMPLE_COM_TOKEN": "env:TARGET_EXAMPLE_COM_TOKEN"}
    }
  ],
  "due_diligence": {
    "paths": {"done": true, "detail": "14 paths on 2 origins", "evidence": ["/mcp 404", "/api/mcp 404", "/v1/mcp 200"]},
    "bypasses": {"done": true, "detail": "http and https; internal CA verified with the run's bundle", "evidence": ["https: issuer CN=Example Internal CA", "answered with --cacert <run bundle>"]},
    "handshake": {"done": true, "detail": "spec Accept headers on every request", "evidence": ["/v1/mcp returned Mcp-Session-Id"]},
    "grounding": {"unavailable": "no web search tool was available in this run"},
    "native_endpoints": ["https://target.example.com/mcp"],
    "auth_required_endpoints": [],
    "endpoints_needing_a_bridge": []
  },
  "findings": {
    "documentation_url": "",
    "websocket_urls": [],
    "openapi_url": "",
    "api_kinds": {"graphql": {"label": "GraphQL", "url": "https://target.example.com/graphql"}},
    "paths_probed": 0
  },
  "proposals": ["what you would try next if none of the above verified"]
}
```

Every checklist entry is either `{"done": …, "detail": …, "evidence": [...]}` or
`{"unavailable": "why"}`. The three URL lists beside them are what the caller's
gate reads: `native_endpoints` (a handshake completed), `auth_required_endpoints`
(a protocol-shaped 401/403, never an HTML login wall) and
`endpoints_needing_a_bridge` (a 426 or WebSocket upgrade **with MCP evidence**;
a URL in `findings.websocket_urls` is counted here too, and every one is probed
again before it closes anything). A URL named only in prose or in
some other field is not seen by the gate at all. A step reported without evidence is recorded as
partial, and a missing step blocks the package and bridge stages entirely.

The caller re-verifies every candidate independently before accepting it, so
report honestly — an unverified guess costs nothing, but a false positive
wastes the whole pipeline.

## Key files, login hosts and runtime settings

- **A key file is a credential.** An SSH key or client certificate named in a
  credential slot (`OBS_SSH_KEY=/root/.ssh/id_ed25519`) may be absent on the
  machine that runs discovery; it is a credential the operator supplies there,
  like a token. Never reject or replace a server because its key file is missing.
- **A login counts only at the host that asked for it.** Basic asked for at
  `api.{domain}` and sent to the web host is refused. Point the server's
  base-URL setting (or the client's `apiurl`) at the host that asked; the
  caller ranks a login by its type and its destination together, and tries a
  waiting server on the API host when it aims elsewhere.
- **Drop a runtime setting only on repeated proof.** One passing launch
  without `NODE_OPTIONS` does not prove it unneeded; the caller asks several
  times, and restores it for that machine when a later call fails the way a
  network does ("fetch failed", `ETIMEDOUT`).

## Certificate trust reaches the runtime that connects

- **A URL entry is opened by Claude Code itself.** Its trust lives in Claude
  Code's environment -- `NODE_EXTRA_CA_CERTS` in its `settings.json` env and
  the shell profiles a new terminal reads -- never in `.mcp.json`. The caller
  writes it there and logs every place; never propose an `env` block for a URL
  entry, nothing would read it.
- **A trust file must exist and hold the root.** Node ignores a missing
  `NODE_EXTRA_CA_CERTS` file with only a warning; a missing `SSL_CERT_FILE`
  leaves a runtime trusting nothing. Do not propose a path you have not seen.
- **Read why a connection failed before repairing it**: a certificate, a
  credential, or a temporary error each needs a different repair. Judge a
  connection the way the consuming runtime makes it, not by this program's own
  trust store.

## An endpoint is proved, never claimed

- **A login redirect or an HTML page is not an MCP endpoint.** Before calling a
  URL "an endpoint behind authentication", request a made-up path on the same
  host (`/no-such-path-<random>`): if it is answered the same way -- the same
  redirect to the login page, the same status and content type -- the URL is
  the website, not a server. confluence.example.com's `/mcp` redirected to
  `login.action` exactly like a path that does not exist.
- **Only an MCP-shaped refusal counts**: a `WWW-Authenticate` challenge, a
  JSON-RPC error body, protected-resource metadata, or an answer that differs
  from the made-up path's. Report what you saw; the caller probes it again,
  and an endpoint you only *claim* closes no lower tier.
- **"Connected" is not proof.** An existing registration keeps its place only
  by what this run proved of it, or by ranking above on evidence -- and only
  one this run judged and can write to `.mcp.json`.

## One keep rule; never an empty address

- An existing registration keeps its place only when **all** hold: this run
  found it usable (it did not fail to connect or start), it is a **different**
  server from the one being proposed, and a copy of it can be written to
  `.mcp.json`. The same server rebuilt -- the same bridge, the same manifest --
  refreshes its old copy and carries its supplied values.
- Values an existing entry already supplies (a default user name) count for
  both sides when two servers are compared: they go into whatever is written.
- An address never ends with no entry while a candidate works or waits for its
  credential; the final file check writes it.

## The program's folder may have any name

- Never rely on the folder being called `claude_ai_initialization` (or
  `claude-ai-initialization`, the GitHub name). Name a file by where it is now;
  the caller writes every path inside the folder holding `.mcp.json` as
  `${ICA_MCP_PROJECT:-<absolute>}/...` and re-points an existing entry naming
  an old location by the path's own tail (`bridges/<file>`), whatever the old
  folder was called.

## Every server keeps its login; every entry reads its own names

- Declare the variable your server reads its login from (in `secret_env`)
  even when it answers without one. Read the server's own code for it:
  `graphql-mcp-server` reads `API_ACCESS_TOKEN`, `mcp-bugzilla` reads
  `BUGZILLA_API_KEY`. The caller adds it from the source when you don't, and
  writes it optional (`${NAME:-}`) when the server works without it: an
  anonymous answer never costs the login.
- Anonymous is not logged in. A server that answers with `"currentUser": null`
  (or `authenticated: false`) sent no login; that answer is empty, never
  "accepted". Such an entry is reported as "public answers only -- not logged
  in", never as working for the account.
- Never point an entry at a generic name other programs export
  (`BUGZILLA_API_KEY`, `JIRA_PERSONAL_TOKEN`, `SSL_CERT_FILE`). The caller
  keeps the key the server reads and writes the reference under a name only
  that entry reads (`{ENTRY}_{NAME}`); a trust bundle that is the machine's
  own reads `ICA_MCP_CA_BUNDLE`, set per machine.
- A 429 or 5xx while identifying an address is "try later": ask again after
  its `Retry-After`, and meanwhile use what it was when it last answered.
- A login shell (SSH, console, `su -`) and a terminal window read different
  startup files. Judge with both; a value broken in either is broken.
- When calls fail, read the server's own stderr: "Failed to resolve
  dependency" hid a missing CA file. A trust or network cause is repaired,
  never blamed on the server.
- `claude mcp list`/`get` show project entries too. "Scope: Project config"
  is the file's own entry, never a CLI copy; never run `claude mcp remove`
  without `--scope local|user` -- without one it removes the project entry.
- A domain that merely begins with a product's or organisation's name
  (`githubcard.com`) is not that organisation's. A server on a third party's
  host belongs to an address only when the vendor says so by name (its own
  documentation, its own registry namespace) or the server's own answer names
  the address itself -- a service *about* GitHub names GitHub too.
- A server whose every login variable names another organisation
  (`APIFY_TOKEN` offered for github.com) works on that organisation's side --
  it is a service about the target, not the target's server, unless its entry
  is configured for the target's own address.
- A bot check or challenge page ("Making sure you're not a bot!", "Just a
  moment...") is "try later", never a verdict on the server behind it -- and
  never a reason to fall back to `http://`: an endpoint is used over https
  whenever its https side answers at all.

## A launcher is not the program

- Propose a registry package by its launcher form -- `uvx <pkg>==<version>`
  (or `uvx --from <pkg>==<version> <script>`), `npx -y <pkg>@<version>` --
  never the path an installer gave it (`~/.local/share/uv/tools/<pkg>/bin/…`,
  a pipx venv, a global npm bin). Such a script's first line runs that
  install's own interpreter; copied to another machine it fails with "no such
  file", and pointed at another Python it cannot import its package. The
  caller rewrites such a command to its launcher form before trialling it.
- A file being there is not the program being runnable: its interpreter must
  exist and import what it starts.
