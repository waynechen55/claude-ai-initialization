---
name: mcp-bridge-authoring
description: Write and test a bridge that exposes an existing API, CLI or service over the Model Context Protocol. Use when a target has no MCP server and none exists to install, but it does have some other interface worth wrapping.
---

# Building a bridge

Nothing off the shelf fits, but the target *does* expose something — a REST API,
a CLI, a database, a socket. A bridge is a small MCP server that translates.
Your job is to build one that actually runs and handshakes, not to describe one.

## You are tier 3 — the last resort, and you only run because the first two failed

Code you write here did not exist a minute ago, only this machine has it, and
nobody maintains it. That is why the caller refuses to start this stage at all
while the target has a native MCP endpoint (tier 1) or while the tier-1 checklist
has a step nobody performed. A package that refuses this particular instance —
an allow-list or embargo of hosts it will not serve — is one of the ways tier 2
fails, and exactly why you run; check its help for an override first, and
report it if there is one. By the time you are running, both have been ruled
out and recorded — and so has anything tier 2 found, *including a package that
handshaked and then asked for a credential*. A server that exists and wants a
token is an answer; writing a second server for the same target because the
first one needs configuring is how one address ends up with two entries, one of
which does not work.

So: if anything you discover while poking at the target turns out to be a native
MCP endpoint the earlier stages missed, **stop and report that URL instead**. It
is a better answer than anything you could write, and reporting it costs one line.
Report it as a *candidate*, not only as a remark or a `due_diligence` entry: a
URL in prose is never read, and one recorded but not proposed can close the
lower tiers while nothing registers it. It may be on another hostname --
a vendor often serves MCP from a sibling domain -- and that is fine.
What makes it the target's is evidence tying it -- the vendor publishing it,
the server naming the target -- not a matching word in its name; one without
that is ruled out and closes nothing.

There is exactly one case where tier 3 is the *right* answer rather than the last
one: a native endpoint that Claude Code cannot dial — a WebSocket route. Then the
bridge goes in front of the target's own endpoint and relays to it. That is the
policy working. What is forbidden is a bridge that reimplements what an endpoint
already does.

## Reach for an existing adapter first

Writing code is the third-best answer. Check these two first, because they are
maintained by other people and you are not:

- **`npx -y mcp-remote <url>`** — the target has a remote HTTP/SSE MCP endpoint
  but the client needs stdio, or the endpoint needs an OAuth flow. `mcp-remote`
  does both. An endpoint stage 1 already found behind a 401 keeps this stage
  shut, so this is for one you uncover yourself -- and then the endpoint's own
  URL goes in as a candidate too.
- **`npx -y supergateway --stdio "<command>"`** — the reverse: a stdio server
  that needs to be reachable over HTTP/SSE.

If either applies, use it and stop. Report it as a candidate with the exact
command line.

The same holds for a kind of API with a generic MCP server of its own. A
GraphQL API is bridged by `npx -y mcp-graphql` with `ENDPOINT=<its URL>` —
pinned to a version, as your brief shows — and that is maintained by someone
else too. When your brief says the target serves such an API, try the servers
it lists before writing code; a bridge you write for GraphQL would only be a
worse copy of one.

## Otherwise: generate from the API description

If the target publishes OpenAPI/Swagger — check `/openapi.json`,
`/swagger.json`, `/v3/api-docs`, `/.well-known/openapi.json` — its operations
*are* a tool list. The description may be YAML rather than JSON, and it may sit
at no conventional path at all: a Swagger UI or ReDoc page (`/apidocs/`,
`/api-docs/`, `/docs/`, `/redoc/`) names it, often only inside the initializer
script the page loads (`urls: [{ url: "…yaml" }]`). Read the page and that
script before concluding there is no description. Only take a description
from the target's own host — Swagger UI's stock initializer points at a public
demo. Map each operation:

- `operationId` (or `method_path`) → tool name, lowercased, non-alphanumerics to
  underscores
- `summary` / `description` → tool description
- `parameters` in `query` / `path` / `header` → `inputSchema` properties, with
  `required` preserved

**Default to read-only.** Expose `GET`, `HEAD` and `OPTIONS` only, unless the
operator has explicitly opted into mutating methods. An unattended run must not
produce a tool that can `DELETE` something.

Cap the tool count — forty is plenty. A tool list with four hundred entries is
unusable and blows out the context of whatever connects to it.

## Writing the server

Keep it boring:

- **Standard library only.** The bridge has to run wherever Python runs, on a
  machine whose dependency state you don't control.
- **Configuration from the environment, read at run time.** Base URL, timeout,
  and any auth header come from environment variables. Nothing is baked into the
  file. This is not a style preference — a generated file with a URL-plus-token
  in it is a credential leak onto disk.
- **Read each credential variable by its name, or say where the name is.**
  The final file check compares an entry's credential slots with what its
  script reads. A name read through a constant (`TOKEN_VAR = "X"` and then
  `os.environ.get(TOKEN_VAR)`) is understood; a name built at run time is
  understood only when the manifest the entry names lists it.
- **No runtime tuning in the entry.** Don't put `NODE_OPTIONS`, JVM flags or
  network timeouts in a bridge's `env` unless a trial without them failed: the
  entry runs on other machines. The caller drops what the server's job doesn't
  need, and keeps what it does need per machine, never as a literal in the file.
- **Your bridge is judged by evidence, not by arriving first.** Among bridges
  and servers that all wait for a credential, the one built for the API the
  address serves, logging in the way the address asks, ranks first, whatever
  tier found it.
- **Write files into your own folder, never into one nested inside it.** What
  an entry names is moved to `${ICA_MCP_SERVERS}/<server>/` beside the project
  file. A folder repeated inside itself (`<server>/<server>/…`) once grew a level
  on every run, until the entry named a file that was no longer there.
- **Separate data from code.** Put the operation list in a JSON manifest and
  keep the server generic. One reviewable file, any number of targets.
- **Errors are results, not crashes -- and flagged as errors.** An upstream 500
  should come back as `{"isError": true, "content": [...]}`, not a traceback
  that kills the process. Start the text with the status ("401 Unauthorized:
  ..."). A refusal returned as an ordinary result was once taken for the
  account's own record. The caller now reads a stated status in text too, but
  a bridge that flags its failures leaves nothing to guess.

The protocol surface you must implement is small:

| Method | Response |
|---|---|
| `initialize` | `{"protocolVersion": ..., "capabilities": {"tools": {}}, "serverInfo": {"name": ..., "version": ...}}` |
| `notifications/initialized` | nothing — it's a notification, no `id`, no reply |
| `tools/list` | `{"tools": [{"name", "description", "inputSchema"}]}` |
| `tools/call` | `{"content": [{"type": "text", "text": ...}]}` |
| anything else with an `id` | JSON-RPC error `-32601` |

For `protocolVersion`, answer with the client's requested revision when your
bridge supports it, and otherwise with the newest one it supports -- the
specification's negotiation, and what the program's own generated bridges do
(they take their list from `mcp.protocol.versions`). A bridge that serves tools
only supports every revision in that list. Never hardcode one revision as the
only answer: a client that asked for another may refuse the session.

Transport is newline-delimited JSON on stdin/stdout. **Never write anything but
JSON-RPC to stdout** — a stray `print()` corrupts the stream and the client
disconnects. Log to stderr.

## Testing it — this is the part that counts

**A candidate that crashes has failed — unless it says the credential is why.**
Two servers exit non-zero when you launch them without a token, and they mean
opposite things. Read the error before deciding which you have.

- `@ivotoby/openapi-mcp-server` exited `rc=1` inside `ToolsManager.initialize`.
  Nothing about that message concerns a credential; it was recorded as an
  endpoint waiting for a token anyway, and registered with an empty slot nobody
  could fill, because the process dies before it reads the variable. **Report
  the error, not a credential requirement.**
- A bridge that prints `GITLAB_TOKEN is required` and exits has read its
  configuration, found the variable absent and declined. That is the strongest
  statement available that the credential *is* the missing piece.

This stage is where the second case bites hardest, because by the time you
trial it **you built the thing**. For `gitlab.example.net` this stage wrapped a
server in a launcher, pinned `GITLAB_URL` at the target, pointed TLS at a
self-contained CA bundle and verified it end-to-end — and the trial without
`GITLAB_TOKEN` was filed as breakage, so the target shipped with no entry at
all. A bridge you have already proved works is not disproved by its refusal to
run without the credential you deliberately withheld.

The line between the two is evidence from the server: **the message names a
variable you withheld, or it is auth-shaped.** A traceback in someone else's
connection pool is neither. Quote the line you judged on, and quote the
*exception* — a stack trace's middle frames are the one part that never says
what went wrong.

So: if your bridge needs its credential at start-up, say so plainly in the
report *and* make it say so on stderr before it exits. A bridge that dies
silently, or with a bare traceback, is indistinguishable from a broken one and
will be treated as broken.

Run the bridge and drive it by hand before you report it:

```bash
PY=$(command -v python3)          # the absolute path -- see below
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"t","version":"0"}}}' \
  '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
  | timeout -k 5 60 "$PY" ./bridge.py   # gtimeout on macOS
```

**Bound every launch you make, and make your bridge exit when stdin closes.**
A server that keeps running after its input ends -- or a launcher (`npx`, `uvx`,
`docker run`) whose child keeps the output pipe open -- never lets the pipe
above finish, and your turn ends there. The same shape once stalled a whole
run: a trialled package ignored stdin closing and the caller waited on it
indefinitely. The caller now takes the server's whole process group down after
`mcp.verification.stdio_shutdown_grace_seconds`, but a bridge that needs that is
a bridge that will leave processes behind in Claude Code too: read stdin until
EOF, then exit.

Then test it the way it will actually be launched: with **exactly the `env`
block you are going to report and nothing else** —
`env -i PATH="$PATH" HOME="$HOME" MCP_BRIDGE_MANIFEST=/abs/x.operations.json … "$PY" ./bridge.py`.
That block names every variable the script reads, companion files included
(below), so a launch that only works because your shell happened to export
something is caught here rather than on the operator's machine.

You should see two JSON-RPC responses. Then call one read-only tool and confirm
the upstream request actually happened. A bridge that lists tools but 404s on
every call is not working.

### Report the interpreter you tested with, by absolute path

Put the **absolute path** of that interpreter in `command` — `/usr/bin/python3`,
not `python3`. The bridge you prove here is launched later by Claude Code, with
a different `PATH`, and bare `python3` is resolved again at that moment: on a
host with a Homebrew, pyenv or conda Python earlier on the path, the bridge that
handshaked for you starts under an interpreter you never tested. The usual
symptom is a TLS failure on every call, because that interpreter trusts a
different certificate bundle and does not know the site's internal CA. Pin what
you tested and the question cannot arise.

If the target's certificate is signed by an internal authority, name the bundle
that verifies it. When your brief names a trust bundle initialize-claude-ai
verifies the target with, that is the one — put it in `MCP_BRIDGE_CA_BUNDLE`
and extract nothing. Otherwise a system bundle (`/etc/ssl/ca-bundle.pem`,
`/var/lib/ca-certificates/ca-bundle.pem`) goes in `MCP_BRIDGE_CA_BUNDLE`,
because nothing could find it otherwise. A chain you extracted yourself belongs
*in the bridge directory* as a `.pem`, where the script picks it up with no
variable at all and it travels with the bridge. Never disable verification
instead.

If the target needs auth to test, and you don't have a credential, say so.
Report the bridge as built-but-unverified with the exact command to test it.

### An existing server you wrap is declared with every part of its credential

When the answer turns out to be an existing server -- you wrap it in a launcher
rather than write one -- report it with every variable it reads: the address,
and each part of its credential in `secret_env` under the name it reads
(`osc-mcp` needs `OSC_MCP_USER` and `OSC_MCP_PASSWORD`). Do not set `PATH`
unless the server needs a directory the client's `PATH` lacks; if you do, it is
read as a search path, one directory at a time.

### Ask the API for what it serves, and never hide a credential in a default

Send ONE `Accept` value: the single media type an operation documents, or
`*/*` -- never JSON by assumption, and never a weighted list such as
`application/xml, text/xml;q=0.9, */*;q=0.1`. Open Build Service answered
`Accept: application/json` alone with 406, and answered a logged-in request
with any multi-value Accept with its gateway's HTML 404 page while `*/*`
returned the data; anonymous calls got 401 either way, so a run without a
credential never sees it. Read the override from `MCP_BRIDGE_ACCEPT`, as the
generated bridge does (`mcp.bridge.generated.accept`), so the verifier can
correct it (`mcp.verification.request_forms`); and retry a read-only call
answered 404/406/415 once with `*/*`. Test a real call, not only the
handshake -- and a 404 *web page* is never evidence about the account.

A credential is never the default of a reference: `${NAME:-<token>}` puts the
token in the file. Declare it in `secret_env`; the caller strips a
credential-shaped default from any reference it writes.

### Ask for the credential the API uses, by names a person can fill

Find out how the API authenticates before you write a line: its description's
security scheme, or the `WWW-Authenticate` header of a 401 to a documented GET
asked without credentials. build.example.net answers `basic realm="API login"` -- a
user name and a password. Then read each part under a name that says what it
is, named after the target (`BUILD_EXAMPLE_NET_USERNAME`,
`BUILD_EXAMPLE_NET_PASSWORD`), declare each in `secret_env`, and build the header in
the bridge. Never ask the operator for "a header" and "a value": that is how
build.example.net's bridge ended up with a value that could never log in, because
nobody knew it had to be `Basic` plus the base64 of `user:password`. The
generated bridge does this (`[mcp.bridge.generated.auth]`), and checks a login
once the parts are exported.

### Ask for credential parts, never a hand-built header

Your bridge takes the credential's parts by name -- `<SLUG>_USERNAME`,
`<SLUG>_PASSWORD`, `<SLUG>_TOKEN` -- and builds the header itself. A server that
wants `API_HEADERS=Authorization:Basic <base64>` typed by the operator is one
they can get wrong: build.example.net's held a bare token, which no header list can
use. Never copy a value from an existing entry or any file into your entry,
under any name or as a default.

### Take every setting's form from the server, not from its name

An address variable may want `https://host`, `host`, `host:port` or a full URL
with a path, and the API host may differ from the web host
(api.opensuse.org beside build.opensuse.org). osc-mcp's `search_packages`
rejected `OSC_MCP_API=https://api.opensuse.org` while its other tools accepted
it. Read the form from the component's own default or documentation; the
caller exercises several read-only tools and tries other forms of an address
when a call answers "misconfigured". Give your own bridge's read tools
argument defaults or examples in their schema, so they can be exercised.

### Never depend on the operator's own configuration

A script or server that reads a dotfile -- an oscrc, a netrc, `~/.config/<tool>` --
works on the machine whose file happens to fit and fails on the next. osc-mcp
read an oscrc pointing at a example.net API and refused to start for
build.opensuse.org. Take everything from the entry's `env`; where a component
insists on a config file, give it a scope of its own (`HOME` /
`XDG_CONFIG_HOME` pointing at a directory under your artifacts) and never edit
or copy the operator's file. If you build a server from source, build current
upstream and check its `tools/list` against the client's naming rules first.

### Prove access with the account's own data

An empty listing proves nothing: sd.example.com's customer account got `[]` from
the core Jira API and could still read its own requests through
`/rest/servicedeskapi`. Give your bridge a read tool that returns the
account's own data (`get_my_requests`, `get_me`) and test with it; the caller
counts an empty answer as unproved and keeps searching when another API is
available.

### Bridge the API the account can use

When you are told a server's account has *no API access*, the credential is
valid and the API was the wrong one for it: sd.example.com sent its customer
account from the core Jira API to the portal, while
`/rest/servicedeskapi` answered the same token. Bridge the API that answers.
Where the caller lists documented operations for it
(`mcp.discovery.api_kinds.<name>.operations`, shown in your brief), expose
exactly those, read-only, with their path parameters; the run generates the
same bridge if you do not. Name an identity-like listing `get_my_…` so the
login check calls it first, not a public "info" endpoint that proves nothing
about the login. The token this machine already sends that host is tried on
your bridge in memory (`borrow_in_use`); never write it anywhere.

Say which kind it is: `"api_kind": "<name>"` in the candidate, with the name
from your brief. Then the caller proves your bridge the way it proves the
generated one: the operation marked `public = true` (one that needs no login,
such as the service desk's `get_service_desk_info`) is called before anything
else, and must return real content — the keys that API answers with. An HTML
page, a 404 or an error means the bridge does not reach the API it was built
for. It is discarded as broken, not registered as waiting for a credential.
smelt.example.net got a service-desk bridge for an API it does not serve, and the
info call would have shown it at once. Call that tool yourself before you
propose the bridge.

**A setting is not a credential, and a public server works without a login.**
- **A setting nobody sets.** A setting (a mode, a selector such as
  `BUGZILLA_AUTH_MODE`) whose `${NAME}` nothing sets is launched without, and
  written `${NAME:-}` or left out. Claude Code passes an unset `${NAME}` as
  literal text, and the server exits on it.
- **A public server.** When a server's read calls succeed with no credential
  at all (a public Bugzilla), it works. Its credential is optional, for what
  needs a login.
- **Existing entries.** An existing entry counts as working only the way
  Claude Code runs it: launched exactly as written, and not reported failing
  by `claude mcp list`. A broken one is replaced, never kept.

**Checks lead to a working server, not to a refusal.** A failed check is
classified (`[mcp.repair]`) and repaired where the program can fix it:
- a credential under a name the server never reads is moved to the one it
  reads;
- a scheme word used as a header name is fixed;
- a service whose login only its client performs gets a **client bridge**
  that drives that client (`[mcp.discovery.clients.<name>]`, e.g. `osc` for
  SSH-signed api.example.net requests). The bridge writes the client's own
  configuration from the entry's variables (user name and SSH public key path) and
  never uses the client's setup on this machine.

Each variant is proved again. What only the operator can do (log a client in,
export a value, enable an account) lands on a checklist with
`--mcp-recheck <host>`. So propose servers that can be *made* to work: name
what they read, how they log in, and what the operator must do once.

**The same login may work on several servers.** One user name and password
(single sign-on) commonly works on many servers; that's fine and is never
reported. A token or API key belongs to the server that issued it.

**Look at what worked before, and how the service is logged in to.**
- **What worked is recorded.** The caller records every entry that did the job
  (`[mcp.known_good]`) and tries the last one first on the next run. When an
  address stops working, the report says what changed since. Your brief may
  name that entry; start from it before searching.
- **Every login challenge is read.** That's on the target and its API hosts
  (`api.{domain}`), so read them too. A `Signature`, `Negotiate` or `NTLM`
  challenge is a login only a client performs (api.example.net signs requests with
  the operator's SSH key, which `osc` does). No variable can carry it, so never
  propose a header or token server for it.
- **Choose the login method in the configured order.** That order
  (`mcp.discovery.auth.method_order`) is: a token that can read, an API key and
  secret, a user name and password, and only last a login a client performs
  (an SSH key). The operator may have no key registered, so a password the
  service offers comes first. Use the host that offered the chosen login, the
  API host first. The product catalogue
  (`[mcp.discovery.products.*]`) says where tokens are created, how they're
  sent, which scope reading needs, and which token types cannot read, such as
  the build service's operation tokens.

**Working means doing the job.** The caller accepts a server only when it
passes the **usage contract** (`[mcp.contract]`). That's real reads:
- each kind of server's own tasks (a Jira server lists projects, a GraphQL
  server describes its schema), asked of any server that lists the tool;
- the operator's own checks for the address (`[mcp.contract.targets."<host>"]`
  in their configuration), such as reading a known request or searching a known
  package.

An empty answer, an error, a web page, or an answer that doesn't match the
target read directly fails the task. When you propose a server, name its tools
the way the kind's tasks expect, and check that it can answer the operator's
reads, not just that it starts. After an entry is written, Claude Code itself
is asked to use it (`mcp.registration.check_through_claude`), so an entry that
needs a variable Claude Code doesn't have is reported as such. A replacement
must pass more of the contract than what it replaces.

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
a token route, and say which. When you write a bridge, declare in
`secret_env` exactly the environment names your script reads its credential
from -- the same spelling your code uses in `os.environ` -- or the credential
never reaches it.

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

**Prove the login with a call that fails without it.** A call that succeeds
with nobody logged in proves nothing. build-example-net "worked" for days on
`get_about`, which build.example.net answers anyone, while every call that needed a
login failed. The caller repeats each login check with no credential at all
(`mcp.verification.login_check.compare_with_anonymous`), so pick tools that need
the login: the account's own record, its requests, a private listing.

When a call is refused exactly as an anonymous one is, the credential may never
have been sent. There are three usual causes:
- **an unread variable:** the value sits under a variable the server never
  reads. `mcp-openapi-proxy` reads `API_KEY` or `EXTRA_HEADERS`; it ignored
  `BUILD_EXAMPLE_NET_BASE64_OF_USER_PASSWORD`;
- **an unimplemented auth type:** the proxy's `API_AUTH_TYPE=Basic` is a stub
  that sends nothing;
- **a header under the wrong name:** `MCP_BRIDGE_AUTH_HEADER=Bearer` sent a
  header *named* Bearer. The name is `Authorization`, and the scheme word
  starts the value.

So put a credential only under a name the server reads. Check its source; the
caller does too, from the installed package. Never reuse one service's login
for another: the same login sent to two instances is reported. Stand-ins never
sit beside a real user name, and a credential refused twice is not tried again
in a run. Repeated refusals lock accounts.

**Your bridge is judged by what it is.** A bridge whose tools are named as a
detected kind's documented operations, at least
`mcp.discovery.api_kind_min_shared_tools` of them, or whose launch names the
kind's path on the target, is that kind's bridge, whether or not you said
`"api_kind"`. Name the tools exactly as the brief lists the operations.

**`404` for every path with a credential, `401` without one, is an account
problem.** build.opensuse.org answered its entry's login with an HTML "Error
404" page for every path, `openSUSE:Factory` included. No login, and a made-up
one, got `401`. So the login was recognised and the service had no account
behind it: the account had never signed in to its web interface, or was
disabled. Don't "fix" this by changing paths, hosts or API versions, and don't
report the server as working. Report the account problem. The caller confirms
it by repeating the calls with a stand-in credential
(`mcp.verification.login_check.compare_with_standin`). A `404` from a tool that
reads the account's own record (`get_my_account`, `whoami`) means the same
thing. A failure answered as a web page comes from the gateway in front of the
API, not the API. A server whose every call fails is not working, however well
it started (`failed_calls_unverify`).

**Send the credential the way the kind documents it, and declare everything.**
A kind with documented operations names how its credential is sent (`security`
in its table, shown in your brief: a bearer token for the service desk). Use
exactly that. Don't guess Basic auth or a different header. Declare every
variable your script reads in `secret_env` or `env`, and ask for one
credential, not a token *or* a user name and password. sd.example.com's
agent-written bridge read `SD_EXAMPLE_COM_USERNAME` and `SD_EXAMPLE_COM_PASSWORD`,
declared only a token, and was refused with `401`. The report blamed the token,
and the run's own bridge was never built. Now:
- the run always builds its generated bridge for the kind beside yours, and
  keeps whichever one a call proves;
- a refusal from a bridge whose method nobody checked does not end the search;
- when the same credential is accepted by one candidate and refused by another,
  the refusing one is discarded as broken;
- variables a script reads but doesn't declare are named to the operator as
  needed.

### Give it a read-only tool the login can be checked with

A bridge that lists its tools has proved it starts; only a call proves the API
takes the credential. With the operator's credential exported, the caller
calls one read-only tool -- one annotated `readOnlyHint`, or named like
`get_me`, `whoami`, `get_about`, `list_projects` (`mcp.verification.login_check`)
-- and reports *accepted*, *rejected* or *no API access*. Expose a parameterless
GET of the API's own identity or about endpoint under such a name, and mark
every GET tool `readOnlyHint: true`. A credential belongs to one instance:
name its variables after the target (`{SLUG}_{PART}`), never a name another
instance's entry might already read.

### Every name your bridge exposes must be one the client can load

A tool name or input property key outside `mcp.verification.client_name_pattern`
(`^[a-zA-Z0-9_.-]{1,64}$` by default) makes the API refuse every request of the
session once the tool is loaded -- openSUSE's `osc-mcp` broke a session with a
property named `path_repository jsonschema:`. An API parameter called
`package[]` or `X-Foo Bar` is exposed under a clean name and sent to the API
under its own; the generated bridge does this (`arg` in the manifest). Do the
same in a bridge you write.

### A CA bundle you point the bridge at must end at a self-signed root

A bridge in Node verifies like Node: the chain has to end at a self-signed root
in the bundle (or in Node's own store). An intermediate is not enough, though
`curl`, Python 3.13 and this program's own probes accept one. Before you report
a bundle, test it with `openssl s_client -connect <host>:443 -CAfile <bundle>
-verify_return_error`, or with your bridge itself making a real request. The
caller checks every bundle an entry names against the hosts the entry reaches
and replaces one that falls short with a bundle that carries the chain
(`mcp.trust.check_named_bundles`); if no bundle on the machine does, the entry
is refused rather than shipped to fail its first request.

## What you register must outlive this run, and this machine

`.mcp.json` exists to be **copied** — into another project, into a commit, onto a
machine that has not been set up yet. An entry that names a path on this host
does not travel, and one that names a path in scratch space does not even
survive here.

This is not hypothetical. A bridge was once registered as:

```json
"command": "…/work/agent-workspace/artifacts/openqa-mcp-bridge/venv/bin/python3"
```

It handshaked perfectly when it was verified. On the next machine it was
`ENOENT: no such file or directory`, with nothing to say why.

**In order of preference:**

1. **A launcher that fetches what it needs at start-up** — `npx -y <package>`,
   `uvx <package>`, `uv tool run <package>`, `docker run …`. Nothing to leave
   behind, works on any machine. *Always prefer this.*
2. **A URL** plus `${VARIABLE}` placeholders. Equally portable.
3. **A durable file with an absolute path** — a bridge you generated, run by a
   system interpreter. Host-bound, accepted, and reported as not portable.

### A bridge is a directory, and it must find its own parts

If your bridge needs companion files — an operations manifest, a PEM chain, a
schema — write them **into the same directory as the script**, and make the
script locate them relative to its own `__file__`:

```python
HERE = os.path.dirname(os.path.abspath(__file__))
manifest = os.environ.get("MCP_BRIDGE_MANIFEST") or os.path.join(HERE, "x.operations.json")
```

Environment variables still win when set; the sibling file is the fallback for
somebody who runs the script by hand.

**And name every companion file in `env` anyway**, by the variable the script
reads it from, with its absolute path:
`"MCP_BRIDGE_MANIFEST": "/abs/…/x.operations.json"`,
`"MCP_BRIDGE_CA_BUNDLE": "/abs/…/chain.pem"`. The fallback alone is a setting
nobody can find. An openQA bridge relied on it; copied to another machine, the
directory also held an older manifest under a near-identical name, the script
opened that one, rejected its schema and exited — and the operator saw
`Connection closed` with nothing in the entry saying a manifest existed, where
it was, or which variable chose it.

The old objection to naming them — absolute paths into this program's state
directory that mean nothing elsewhere — is the caller's to solve, and it does:
every absolute path in the entry is rewritten as `${VARIABLE:-<the path>}`, and
files under a configured root share one variable, so "copy the directory and
export one variable" is still the whole of the instructions. Say in `findings`
that the bridge is a directory and must be moved whole.

The caller also reads your script and declares any variable it reads that you
did not, with the default the script uses — but that is a safety net, not a
substitute: it cannot see a module your script imports, and it cannot know
what a variable means. List every one yourself.

If a companion file is *missing*, exit with a sentence on stderr naming what was
not found and where it looked. `ENOENT` from inside `json.load` reaches the
operator as `Connection closed`, which says nothing at all.

### Your script and its manifest are one artifact — make the pair checkable

If you split the operations into a sibling JSON file, the two are **generated
together and only valid together**. Nothing else enforces that, so you must:

1. **Stamp the manifest** with the generation that wrote it —
   `{"schema": 1, "script": "mcp_bridge_<slug>.py", ...}`.
2. **Check the stamp on load**, and exit naming the mismatch when it differs.
3. **Validate the shape before using it** — every field you will later index or
   iterate — and report the *operation and field*, not the line that blew up.
4. **Regenerate every file of the set, every time.** Never reuse a manifest
   from an earlier run, and never write only one of the pair -- this is the
   failure that keeps recurring, so treat it as the rule rather than the
   guidance. If you rewrite the script, rewrite the manifest in the same breath,
   even when you believe the data is unchanged.
5. **Own your directory.** Put the set in a directory of its own, named for the
   target, and take responsibility for everything in it. An older file of the
   same name sitting beside your new one is not inert: it is what the next
   launch will read.

`openqa.example.net` has now broken this way twice. The first time the script was
regenerated against a day-old manifest whose `path_params` was a list where the
new script wanted a mapping, and it died with `AttributeError: 'list' object has
no attribute 'items'`. The second time the script carried a stamp check -- so it
refused cleanly instead of crashing -- and the manifest beside it was *still*
the day-old one. The check improved the message; only regenerating the set fixes
the cause.

The caller will now list the files an entry depends on, with their ages, when it
cannot be made to start, and flag a set whose timestamps disagree. If you see
that in a repair brief, the answer is almost always "rewrite the stale half".

That first `AttributeError` was thrown from inside `tools/list`, and the
operator saw `openqa-example-net · ✘ failed · Connection closed` — naming neither
the file, nor the field, nor the fact that two files had drifted apart. The
bridge had handshaked perfectly when it was built. A stamp check makes the
script say so itself:

```python
SCHEMA = 1
manifest = json.load(open(path))
stamped = manifest.get("schema")
if stamped is not None and stamped != SCHEMA:
    raise SystemExit(
        f"{path} was written for schema {stamped}; this bridge reads {SCHEMA}. "
        "They are generated as a pair -- regenerate the bridge."
    )
```

### Never let a traceback be the error message

A bridge that dies with an unhandled exception is reported by the client as
**`Connection closed`** and nothing more: it saw a process exit. Whatever the
cause — a missing file, a bad manifest, an import error — wrap `main()` and
reduce it to one sentence on stderr that names the cause and the likely remedy.
stdout is the protocol channel and must stay clean; stderr is where you talk to
the operator, and it is the only chance you get.

**And if you cannot tell which companion is yours, stop rather than guess.**
"The first `*.operations.json` in the directory" is right for a bridge that owns
its directory and wrong the moment two share one — which happens as soon as an
operator collects several bridges in a working directory. One openQA bridge
loaded the *other* openQA bridge's manifest that way: a real file, for a
different host, accepted in silence. A bridge that answers confidently about the
wrong service is worse than one that will not start. Prefer the file named after
your script, accept a bare match only when there is exactly one, and otherwise
exit naming the candidates and the variable that resolves the ambiguity.

Name your files after the target for the same reason — `mcp_bridge_<slug>.py`
beside `<slug>.operations.json` — so "which one is mine" has an answer that does
not depend on what else is in the directory.

**Never do this:**

- Do not build a virtualenv and register a path inside it. If a Python package
  is what you need, register `uvx <package>` or `uv tool run <package>` — the
  same server, with nothing to rot. A path inside a venv you built is
  **refused**, and so is one inside any venv not under
  `mcp.registration.durable_venv_roots`. Those roots — this program's own venv
  (`install.venv_path`, which it may well be running from), pipx's and
  `uv tool`'s — are kept on purpose, so an interpreter there is host-bound and
  accepted like `/usr/bin/python3`. The interpreter you were given to test
  with is always one of them.
- The refusal reaches you *before* the stage settles, as `not_registrable`,
  so you can still change the command; it is not a reason to give up.
- Do not register anything under the trial sandbox. That directory is scratch.
- Do not register a path that does not exist at the moment you report it.

If you installed something in order to *test* it, that was the right thing to
do — but report the command that will work for someone else, not the one that
happened to work for you.

## Rules

- No credential in the generated file, ever. Names of environment variables
  only.
- No mutating tools without explicit opt-in.
- **Point the bridge at the target's own API, nowhere else.** Its base URL is
  the address you were given or a host of the same organisation. A hosted
  gateway, marketplace or catalog of other tools is not this target's API,
  whatever it lists — the writer refuses an entry whose location, configuration
  and own answer all name something else.
- Write into the output directory you were given, nowhere else. In the agent
  workspace that is the artefacts directory the brief names *for this address*:
  other addresses may be worked beside you by other agents in the same
  workspace, and a venv or script written to a shared name would be written
  over. The skills directory and other addresses' context files are read-only
  to you.
- If you can't build something that handshakes, say that clearly and hand the
  problem to the strategy stage. A broken bridge reported as working is worse
  than no bridge.
- **Say what you wrote and what it needs.** List every generated file in
  `findings.artifacts` with its absolute path, every install command in
  `install`, and every environment variable in `env` / `secret_env`. The
  variables go into the `.mcp.json` entry; the install commands and the files
  go into the briefing printed after the run and kept in the report and the
  log, because this is code with no package behind it: whoever inherits the
  machine has only those to tell them the server is a script, where it lives,
  and what it needs to start.
- Name the server after the service, from its fully qualified domain name:
  `bugzilla-example-com-bridge`, not `bridge` or `bugzilla`. The registered name is
  composed from the address plus yours, and a whole repeated address is dropped
  rather than written twice, so the repetition costs nothing.
- Absolute paths everywhere in `command`, `env` and `findings.artifacts` — the
  interpreter, the script and the manifest. Nothing here is resolved relative to
  a working directory or a `PATH` you control.
- **Put the address the bridge relays to in the entry**, as
  `MCP_BRIDGE_BASE_URL`, and make it the target's own host. An entry that does
  not name the target is refused before it is written, and rightly: a script
  path tells the operator nothing about what the script connects to, and they
  cannot open every file in `.mcp.json` to find out. Your bridge is a launcher,
  so it gets no benefit of the doubt — a launcher is never probed at an address,
  so only what you write down shows where it points. Baking the URL into the
  script instead of the entry gets the entry refused.
- **Report the paths you tested, absolute — all of them.** The caller rewrites
  *every* absolute path into `${VARIABLE:-<that path>}` before it is written:
  one under its own state directory by the root it sits in, a binary by its own
  name, anything else by the environment variable holding it or by its
  basename. So the entry means the same thing here and can be relocated by
  exporting variables. That is the caller's job — give it the real absolute
  paths and do not invent variables of your own, which it cannot resolve or
  check. Do not "helpfully" shorten a path to a bare name either: a bare
  `python3` resolved to a different interpreter with a different trust store,
  which is the defect the pinning exists for.
- **Report every path the bridge uses** -- the script in `command`, and each
  companion file in `env` under the variable the script reads it from (above).
  The caller makes all of them relocatable; a path you leave out is one the
  operator has to find by reading your source.
- **Put every variable the bridge reads into `env`** -- every one, regardless
  of where it is read or whether it has a default: a setting with a default in
  `env` with that default as its value, a credential in `secret_env`, and an
  optional setting with no sensible value as `""` (it is written as
  `${NAME:-}`, empty until exported). Write each default **in the source** in a
  form the caller reads -- `os.environ.get("X", "30")`, `os.environ.get("X") or
  "30"`, `process.env.X || "30"`: a value in `env` equal to the source's own
  default is configuration, however random it looks (a User-Agent such as
  `mcp-bridge-<slug>/1` scores like a token). A value that only *looks* secret
  and is no default of yours is kept out of the file in Claude Code's settings,
  never asked of the operator; a credential always goes in `secret_env`. Your test inherits your
  shell; Claude Code passes the entry's `env` and a narrow passthrough and
  nothing else. The caller launches the finished entry that way before
  accepting it. If it does not start, an agent (the strategist) is asked to
  repair it -- given the entry, the failure and how your launch differs from it
  -- and may rewrite the script and the files you generated as well as the
  entry. If neither it nor the deterministic repairs can fix it, the bridge is
  dropped and the target falls to the next tier.
  Test yours with a clean environment (`env -i PATH="$PATH" HOME="$HOME" …`)
  and from another directory (`cd /` first) before you report it, and you will
  usually not be asked twice: the client starts it from its own directory, so a
  relative path breaks there. A bridge that waits for a credential is launched
  this way too, with a stand-in in the slot, before it may settle the target.
- **Do not put a variable in `env` that you did not launch the bridge with.**
  Whatever you tested is the only launch anyone has evidence for, and an unset
  `${VAR}` is a different command. Two servers were registered with one extra
  variable each and exited during start-up on the operator's machine.
- **Never set a variable that disables certificate verification** —
  `NODE_TLS_REJECT_UNAUTHORIZED`, `PYTHONHTTPSVERIFY`, `GIT_SSL_NO_VERIFY` and
  the rest. They are stripped from the entry if you do. `.mcp.json` is copied
  between machines, and a file that quietly turns off TLS wherever it lands is
  not something to hand anyone. If the target uses a private CA, name the
  bundle in `MCP_BRIDGE_CA_BUNDLE` and say in `findings` that the certificate
  has to be trusted — that is a fact the operator can act on.
- Batch your work and watch the turn budget: generate, run once, fix, run again.
  Exploratory commands that cannot change the file you are writing are turns you
  will want back when the handshake fails.

## If you are being retried

The prompt will say so, and it will list what went wrong last time: which
candidate did not complete a handshake and with what error, which one was
discarded before it was tried. That list is the point of the retry. Do not send
the same candidates back unchanged, and do not restate the same conclusion in
different words.

**An unchanged launch is not trialled again.** Each target remembers the
verdict of every launch it has tried (`mcp.verification.reuse_verdicts`). The
same command, arguments, variables, headers and install get the same verdict
straight back, marked "already trialled". Only a timeout or a similar passing
failure is tried again. So a candidate is worth resending only if something in
its launch has changed. Renaming it does not count.

Pick up where the last attempt stopped:

- A candidate that failed for a fixable reason is worth fixing — a package name
  that does not exist, a transport the server does not speak, a missing
  environment variable, a launch command that needed a shell.
- A lead you had not followed yet is worth following now. Another registry, the
  vendor's own documentation, a path you did not try.
- An empty answer is a real answer, but only once the leads are genuinely gone.
  When you report one, say which ones you exhausted, so the next attempt — and
  the person reading the report — does not repeat your search.

## What to output

A single fenced JSON block:

Every `<…>` below is a placeholder for a value you found or produced: the
caller refuses a candidate that still carries one (`mcp.agent.placeholder_patterns`)
-- an agent once proposed `/abs/path/osc-mcp`, copied from this example.

```json
{
  "summary": "what you built and whether it handshakes",
  "candidates": [
    {
      "kind": "stdio",
      "name": "target-bridge",
      "command": ["/usr/bin/python3", "<ABSOLUTE PATH OF THE BRIDGE SCRIPT>"],
      "//": "every variable the script reads: companion files by absolute path, settings with their defaults",
      "env": {
        "MCP_BRIDGE_BASE_URL": "https://target.example.com",
        "MCP_BRIDGE_MANIFEST": "<ABSOLUTE PATH OF THE OPERATIONS MANIFEST>",
        "MCP_BRIDGE_CA_BUNDLE": "<ABSOLUTE PATH OF THE CA BUNDLE>",
        "MCP_BRIDGE_TIMEOUT": "30"
      },
      "secret_env": {"MCP_BRIDGE_API_KEY": "env:MCP_BRIDGE_API_KEY"},
      "transport": "stdio",
      "source": "generated from the target's OpenAPI description",
      "confidence": 0.8,
      "install": [],
      "notes": ["12 read-only tools", "verified: initialize + tools/list + one live call"]
    }
  ],
  "findings": {
    "artifacts": ["<ABSOLUTE PATH OF THE BRIDGE SCRIPT>", "<ABSOLUTE PATH OF THE OPERATIONS MANIFEST>"],
    "tool_count": 12
  },
  "proposals": []
}
```

`install` holds the commands that must run before the bridge will start, one
executable and its arguments per step -- `["npm", "install", "-g", "@vendor/sdk"]`.
A plain string is split into words, never run through a shell. Leave it empty:
a bridge is standard-library only, and a dependency is one more thing that is
not on the next machine.

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
