---
name: mcp-package-discovery
description: Find an existing MCP server package, container image or repository that already covers a target service, and work out the exact command to launch it. Use after direct endpoint discovery has failed.
---

# Finding an MCP server someone already wrote

The target has no MCP endpoint of its own. Before anyone writes code, find out
whether the server already exists. For anything popular it usually does.

## You are tier 2, and you only run because tier 1 came up empty

The endpoint stage has already carried out the four-step native-endpoint
checklist for this target and found nothing that answers; the caller refuses to
start this stage otherwise. "Answers" there includes *an endpoint that demanded
a credential* — one that exists but wants a token is an answer, not a gap — so
if you are running at all, tier 1 found neither. That has two consequences for
you:

- **If anything you find points back at a native endpoint — a `remotes[]` URL, a
  documentation page naming an endpoint the earlier stage missed — report that
  URL as your candidate and say where it came from.** It outranks any package.
  Installing someone's package to talk to a server that already speaks MCP is
  the failure this whole ordering exists to prevent.

  **On the target's own host or not.** A registry entry with `remotes[]` and no
  `packages[]` is the vendor saying where their endpoint lives, and it is often
  a sibling domain — `developers.google.com` is served from
  `developerknowledge.googleapis.com`, `github.com` from
  `api.githubcopilot.com`. Report it as a candidate with the URL exactly as
  published.

  **Recording it is not proposing it.** Listing the URL in
  `due_diligence.native_endpoints` closes the package, bridge and strategy
  stages for the address.
  If the URL is not also in `candidates`, nothing proposes it for registration,
  and the address can end with no server at all — every alternative refused on
  the strength of something nobody proposed. A URL only in prose is not read. That is a real run,
  and it is the worst outcome this stage can produce.
- Everything you propose is an install: someone else's code, on this machine,
  that somebody will have to keep working. That is fine — it is what tier 2 is
  — but it is why "a package exists" is not on its own a reason to pick it. See
  *Judging a candidate*.

## Where to look, best source first

1. **The official MCP registry** — `https://registry.modelcontextprotocol.io/v0/servers?search=<query>`.
   Curated and structured. Each entry gives you everything you need:

   - `packages[]` — `registryType` (`npm` / `pypi` / `oci`), `identifier`,
     `version`, `runtimeHint` (`npx` / `uvx` / `docker`), `transport.type`,
     `runtimeArguments[]`, `packageArguments[]`, and `environmentVariables[]`
     with `isRequired` and `isSecret` flags.
   - `remotes[]` — hosted endpoints you can connect to directly, with the
     headers they need. A `remotes[]` URL the target's vendor publishes — on
     its own host or a sibling domain (above) — is a tier-1 finding that
     arrived late: report it as the candidate and say so.

   An entry here is worth ten search hits. Prefer it.

   **Search it with the bare product word.** `search=` is a substring match on
   the server's *name*, not a full-text search. `search=confluence` returns ten
   entries, including `@atlassian-dc-mcp/confluence`; `search=confluence mcp
   server` returns nothing, because no server is called that. Phrases belong to
   npm and GitHub, which do search descriptions. An empty answer or a timeout
   from the registry is not "nothing exists" -- say which one you got.

2. **npm** — `https://registry.npmjs.org/-/v1/search?text=<query>`. Where most
   community servers live. Look for `mcp-server-*`, `*-mcp-server`, `@vendor/mcp-*`.
3. **PyPI** — `https://pypi.org/pypi/<name>/json`. Same naming conventions, but
   this is a lookup and not a search: the endpoint answers about one exact
   distribution name, so you have to ask by name. Try `mcp-server-<product>`,
   `<product>-mcp-server`, `mcp-<product>` and `<product>-mcp`, lower-cased with
   `_` and `.` written as `-` (PEP 503) — a 404 means that name, not that there
   is no Python server. The deterministic pipeline asks those four; a name the
   vendor's own docs suggest is worth adding to them.
4. **GitHub** — repository search. The long tail, and the only place you'll find
   something that was written but never published.

Search several phrasings: the product name plus "mcp server", `mcp-server-<product>`,
the vendor's org name. Vendors also publish under their own scope, so check
`@<vendor>/` on npm directly.

### Search by the kind of API, not only by the name

Most products have no MCP server named after them, and many do not need one.
`smelt.example.net` has no endpoint, no OpenAPI description and no server called
anything like SMELT — and it serves GraphQL, which `mcp-graphql` bridges for
any GraphQL API once `ENDPOINT` points at it. That working entry was found by
the operator, not by a search for the product's name.

So find out what kind of API the target serves — a GraphQL endpoint answers the
introspection query `{"query":"query { __schema { queryType { name } } }"}`
with `"__schema"`; an OpenAPI description sits at `/openapi.json` or behind a
Swagger UI page — and search the registries by that kind too (`graphql`,
`openapi`). Only a 2xx answer in the API's own form shows the kind. A 404 page,
or any page that repeats the path you asked for, shows nothing. The caller
confirms each kind with a control request to a path that cannot exist before
it lists the kind in your brief. When your brief says the target serves one, it lists the generic
servers already configured for it with the exact launch the run would trial:
try those first. A generic server is relevant because it is **pointed at the
target** — its `ENDPOINT` / URL variable holds the target's address — not
because its name says so.

### A registration this machine already has was tried before you

If `.mcp.json` or `claude mcp list` already holds an entry for this address,
it was verified before you started and your brief says how it answered. A
working one is already a candidate: report something only if it is better. A
failed one is a lead — a moved endpoint, an older version, a missing variable
— so propose the corrected launch, not a copy of it. Its credential is never
carried: a literal token in it becomes a `secret_env` reference.

One marked **NOT THIS ADDRESS'S** points at an unrelated host and its own answer
names something else — an earlier run registered it on a claim. It is retired
in this run if this program gave it its name, and otherwise reported with the
command to remove it; either way, do not propose it, its host, or anything it
lists for this address.
An earlier registration is a claim, not evidence.

## Judging a candidate

Not every package that mentions MCP is one, and not every one that is, is
maintained. Weigh:

- **Does it target the right thing?** A server for "GitHub" is not a server for
  "your self-hosted Gitea". Read the description, not just the name.
- **Is it alive?** Last publish date, open issues, whether the version looks
  like a real release or an abandoned `0.0.1`.
- **Who published it?** A vendor-official package beats a fork beats an
  unattributed one-off. A registry name `io.github.<account>/<server>` was
  published by that GitHub account — it is not GitHub, and it is not the vendor
  unless the account is theirs. Only a domain-verified namespace
  (`com.googleapis.developerknowledge/mcp`) is the domain's owner speaking.
- **Does it name the target, or only contain its letters?** `example` is not in
  `examplep_administradores`. Relevance is a whole word, a host label or a
  registry owner — never a substring — and a hosted remote on a third party's
  domain that names neither the target nor its organisation is not the
  target's server, however well it handshakes. One such remote was registered
  for six Example hosts and `github.com` at once; if the same answer would fit
  several unrelated targets, it fits none of them.
- **Does anything about the target vouch for it?** Where it is (the target's
  host or organisation), what it is configured for (it is pointed at the
  target), or what it says it is (its `serverInfo`, `instructions` or tool
  names name the target). A registry listing, a search hit or a README is a
  claim; it never outweighs the server's own answer. A server describing
  itself as a `marketplace` or `catalog` of other tools is no target's server.
- **What does it need?** A server requiring six environment variables you don't
  have is a worse lead than one that needs none, even if it is a better match.

Say which of these you weighed. "Picked X because the vendor publishes it and it
needs only a base URL" is a useful finding; "found X" is not.

## Building the launch command

Compose it from the registry data rather than guessing:

| registryType | runtimeHint | command |
|---|---|---|
| npm | npx | `npx -y <identifier>@<version>` |
| pypi | uvx | `uvx <identifier>` |
| oci | docker | `docker run -i --rm <identifier>:<version>` |

Append `runtimeArguments` then `packageArguments`, in that order, using each
argument's `value` (skip ones whose value is a placeholder the operator must
fill — a trial that fails loudly is better than one that silently misbehaves).

A `docker run` candidate is launched only where the container engine works on
this machine (its daemon running, its socket open to this user); elsewhere the
trial says "not launched" with the fix, which is no verdict on the server. When
a server publishes an npm or PyPI package as well as an image, propose that form
too, so a machine without a working engine still has a candidate it can prove.

## Credentials — the hard rule

Environment variables flagged `isSecret: true` are **never** given a value by
you. Put the variable *name* in `secret_env` mapped to a reference string like
`"env:GITHUB_TOKEN"`, and let the operator supply the value at run time. Do not
read a secret from the environment to "check" it, do not echo one, do not write
one into a config file, and do not put one on a command line where it would show
up in `ps`.

The key is the name the server reads; the reference may name another variable.
When your brief says the operator already holds this address's credential under
a name of its own, map the server's key to it
(`"GITLAB_PERSONAL_ACCESS_TOKEN": "env:<that name>"`). The trial reads that
variable, and the entry is written reading the same one, so the key never needs
renaming and the operator exports nothing new.

Non-secret required variables go in `env` **with the value they should hold** --
most often the target's own address, which is also what ties the entry to the
target (see below). A variable whose value you do not know is left out and named
in your notes; a name with no value is dropped before the trial.

**Say what each part is.** Give every credential variable a `shape` in words
(`"your user name on build.example.net"`, `"the password for it"`,
`"username:key:secret"`) so the report can tell the operator exactly what to
export. A variable named only `…_VALUE` or `…_HEADER` tells nobody anything.

**A credential in parts is declared in parts.** A user and a password, a login
and a key: every part goes in `secret_env`, each under the exact variable name
the server reads -- taken from its `--help`, its README or its source, never
invented. openSUSE's `osc-mcp` reads `OSC_MCP_USER` *and* `OSC_MCP_PASSWORD`
(plus `OSC_MCP_API` for the address, in `env` -- as the bare host
`api.opensuse.org`, the form its own default uses; its `search_packages`
rejects `https://api.opensuse.org`); proposed with the password alone
it said "user not set" and was nearly lost, and described in prose as needing
"OBS_USER and OBS_PASSWORD" it could not be tried at all. The caller trials
with a stand-in for each part and, when a stand-in moves the failure on to
another part, looks for that part by name
(`mcp.packages.credential_probe.companion_suffixes`) -- but only for a secret
you declared. Declare them all, and propose the server as a candidate.

### A generic variable name is renamed for the operator

If the package reads its credential from a variable with a generic name --
`HEADERS`, `API_KEY`, `TOKEN` -- report it exactly as the package spells it:
that key is what the server looks for and must not change. The caller points it
at a namespaced variable in the entry. A file with three servers all wanting
`${HEADERS}` is one the operator cannot satisfy.

### Judged with no credential at hand

The machine a run is on may hold no credentials at all, so nothing can be
proved by logging in. Then:

- an answer stating "401 Client Error: Unauthorized" in its text is a refusal,
  even when the server doesn't flag it as an error;
- a path in an entry (`${…_SSH_KEY:-/root/.ssh/id_ed25519}`) is where the key
  lies, so the trial launches with it;
- among candidates that only wait for a credential, the one that logs in the
  way the address asks (its API host's challenge, at that host) is preferred,
  and one sending a kind of credential the address offers nowhere goes last.

Propose a server for the address's API host and its login method: a generic
header proxy on the web host could never log in to build.example.net, whose API
host only accepts an SSH signature.

### Every setting needs a reason

`.mcp.json` is copied to other machines. A runtime-tuning setting
(`NODE_OPTIONS` network timeouts or family selection, JVM or memory flags) fits
the machine it was written on: smelt-example-net carried
`--network-family-autoselection-attempt-timeout=30000`, which took 5 s here and
53 s on the next machine, past Claude Code's 30 s. Propose one only when a
trial without it failed. The caller launches every candidate once without such
settings, and drops them only when the server's *job* (its usage tasks and real
calls) works as well without, not just its start. A setting the job needs is
kept per machine: the entry reads a per-machine variable whose value lives in
that machine's Claude Code settings. smelt-example-net's mcp-graphql started fine
without `NODE_OPTIONS` on one machine, but every real call there failed
("fetch failed").

### An existing registration is a candidate, not the answer

"Connected" in Claude Code is not "works". A status with a warning
(`! Connected · tools fetch failed`) means its tools could not even be listed,
and an existing registration that proved nothing this run keeps no place over
a candidate that logs in the way the address asks, or is built for the API it
serves. When the address serves an API type no published server speaks (a
service desk's customer API), a server for the product's general API is no
answer for it, because those accounts are turned away there.

### A credential carried inside a form is written in that form

`HEADERS` for mcp-graphql is a JSON object, not a token. Proposed as
`"HEADERS": "env:GITLAB_EXAMPLE_NET_HEADERS"`, it asked the operator for a whole JSON
object, and empty it was no JSON at all: the server exited at start-up, and the
repair that followed removed the slot -- with it, the only place the token could
go. Where a server takes its credential in a form (a header object, a
`Name:value` list), say so in `shape`. The caller writes the form its API-kind
adapter declares (`credential_env`) around a variable that holds the credential
itself -- `{"Authorization": "Bearer ${GITLAB_EXAMPLE_NET_TOKEN}"}` -- reading the
token the address already holds when there is one. A credential slot is never
removed to make a server start, and a login an existing entry supplies is
carried into the new entry, optional, even when the server also works without
it.

A variable the vendor documents -- `GITHUB_PERSONAL_ACCESS_TOKEN`,
`GITLAB_TOKEN` -- keeps its name, because that is what their instructions tell
people to export.

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

**A shape belongs to a variable, not to a server.** One package can want two
variables that hold two different kinds of thing, so give each its own `shape`.
`@ivotoby/openapi-mcp-server` reads `API_HEADERS` as a comma-separated
`Name:value` list -- `Authorization:Bearer <token>` -- and it was reported with
no shape at all, so the operator was told what every credential is told: "may
call it an API token". They exported a bare token into it, the parser rejected
the value, and the server exited during start-up with `.mcp.json` looking
perfectly fine.

### Not everything that sounds like a credential is one

`BUGZILLA_AUTH_MODE` selects *how* to authenticate -- `apikey`, `basic`,
`anonymous`. It is not a key and has no secret value, but it went into
`secret_env`, so its value was discarded and the entry shipped
`"BUGZILLA_AUTH_MODE": "${BUGZILLA_AUTH_MODE}"` for a variable nobody could
fill. The server exited on start-up.

A variable whose last word is `MODE`, `TYPE`, `SCHEME`, `METHOD`, `FORMAT`,
`STYLE`, `KIND`, `STRATEGY`, `ALGORITHM`, `VERSION` or `FLAVOUR` (packaged as `mcp.secret_policy.selector_words`; the program applies that setting to what you return) is a choice between named alternatives. Put it in `env` **with the
value it should hold**, not in `secret_env`. If you do not know which value it
should hold, leave it out entirely and say so in your notes -- an empty
placeholder is a claim that a value is required, and all you knew was a name.

**Report every one of them.** The caller writes each variable name into the
generated `.mcp.json` so the operator knows what to export. A server registered
without its variables listed fails at first use, in front of the user, with no
explanation — which is worse than not registering it. Before you list a variable,
check whether the environment already provides it (`env | grep -i <vendor>`);
say which ones are already present and which the operator still has to supply.

**Its tool definitions must be ones the client can load.** Every tool name and
input property key must be a plain identifier -- letters, digits, `_`, `.`, `-`,
at most 64 characters (`mcp.verification.client_name_pattern`). openSUSE's
`osc-mcp` declares a property named `path_repository jsonschema:` on
`search_packages`; once that tool is loaded, the API refuses *every* request of
the Claude Code session. Look at its `tools/list` before you propose a server;
the caller checks it too and refuses such a server by default
(`mcp.verification.schema_problems`).

**A credential belongs to one instance -- name it per instance.** One package
can serve several instances of a product: `mcp-atlassian` was registered for
jira.example.com *and* sd.example.com, both reading `JIRA_PERSONAL_TOKEN`. A personal
access token is valid on the instance that issued it, so one exported value
could only ever serve one of them. Keep the key the package reads in
`secret_env`; the caller points it at a per-instance variable
(`mcp.registration.shared_secret_template`, `SD_EXAMPLE_COM_JIRA_PERSONAL_TOKEN`)
when another entry already reads the plain name for a different host, and
every instruction names that variable. Say in your `notes` which instance a
credential is for.

## Verifying

**Listing tools is not proof that the credential works.** When the operator has
exported a real credential, the caller calls **one read-only tool** with it --
the server's `readOnlyHint` annotation, or a name such as `get_me`, `myself`,
`get_server_info`, `list_projects` (`mcp.verification.login_check`), never one
that could change anything -- and judges the answer: *accepted*, *rejected*
(the service refused the token) or *no API access* (the service sent the
account to a login page or a customer portal). sd.example.com's `mcp-atlassian`
listed 49 tools and failed every call with
`500 ... /servicedesk/customer/portals`: the instance did not accept the caller
as a Jira user. A server whose credential is refused is registered as waiting
for a *working* credential, with the remedy named -- not as working. Prefer a
package that offers a read-only identity or listing tool, so its login can be
proved, and never claim a credential works because the tools were listed.

**An empty answer is not access.** `[]` from a listing tool says nothing: for a
restricted account "nothing to show you" and "you may not see anything" are the
same answer. sd.example.com's customer account got `[]` from
`jira_get_all_projects` while every issue lookup went to the customer portal.
Prove access with a call that must return the account's own data (its
requests, its profile, one known item), and prefer the API the account can see
into -- a service desk's customer API for a customer account. The caller counts
an empty answer as unproved, lets a refusal from any call outrank acceptance
from another, and keeps searching when access is unproved and the target serves
another API (`mcp.verification.login_check.empty_answers_prove_nothing`,
`unproved_settles`).

**No API access is a lead, not an answer.** When the verdict for a server is
*no API access*, the credential is valid but the account may not use the API
that server speaks -- sd.example.com sent its customer account from the core Jira
API to the portal, while its Service Desk customer API answered the same token.
Do not settle on that server, and do not propose another server for the same
API. Look for one that speaks an API the account *can* use; the caller will not
end the search on it (`mcp.verification.login_check.no_access_settles`), and
a bridge from the target's documented operations
(`mcp.discovery.api_kinds.<name>.operations`) is generated if you find nothing.

**`[]` everywhere proves nothing about the token.** sd.example.com's
mcp-atlassian answered `[]` to every read tool, both with a working token and
with a dead one, so a refused token and an account that sees nothing look the
same. Don't report the token as valid or as refused from such answers. Say that
access is unproved; the caller checks it through the target's other API (the
bridge built from its documented operations). When that bridge proves access,
the unproved server is replaced, in the claude CLI as well as the file
(`mcp.registration.replace_unproved_existing`).

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

**Proof decides across tiers.** A server whose login was never checked doesn't
keep an address from one, a tier down, whose login was accepted. When a server
needs its credential just to start, the caller still offers it the credential
this machine already sends that host, even when the entry names the host bare
(`JIRA_HOST=sd.example.com`), and checks the login. A registration that times out
once is retried with a longer deadline: slow is not broken, and a timeout never
replaces an entry `claude mcp list` reports connected.

**A working registration keeps its place.** Do not propose a candidate to
replace an existing registration that answered during this run unless the
candidate proved more: verified, or its login accepted. One that is only
waiting for a credential is held back and reported
(`mcp.registration.keep_answering_existing`). smelt.example.net's working
`mcp-graphql` entry was overwritten by a bridge that waited for a token.

**A server must not depend on the operator's own configuration.** Some read a
dotfile before their entry -- osc-mcp takes its API address from the
operator's oscrc ahead of `OSC_MCP_API`, and refuses any example.net address by
design, so on a machine set up for build.example.net every launch for
build.opensuse.org died with "failed to get credentials". Give such a server
a configuration scope of its own (`HOME` / `XDG_CONFIG_HOME` pointing at a
directory it owns -- the caller tries this itself, `isolate_user_config`), and
never edit, copy or read values out of the operator's files. A failure that
is identical with and without the credential is not a credential wall,
whatever its words.

**A setting's form comes from the server, and is proved by a call.** Whether an
address takes a scheme, a trailing slash or a path -- and whether it is the API
host or the web host -- is not in the variable's name. Read it from the
server's own default, its `--help`, its README or source, and say where in
your `notes`. A handshake never parses it: the caller calls read-only tools
(`mcp.verification.login_check`), reports "misconfigured" when one answers
that a setting is in the wrong form, and tries the address in other forms
(`mcp.verification.address_forms`). A tool that needs an argument nobody can
supply is listed as not exercised -- one accepted call is not "every tool works".

**Build from current upstream, and check the tool list before proposing.** A
tagged release can carry a bug the client refuses: osc-mcp v0.2.1 declares a
property named `path_repository jsonschema:`. If you build from source, use the
current upstream revision and look at `tools/list` against
`mcp.verification.client_name_pattern` before you propose it.

**Never copy a value out of an existing entry or any file.** Not into `env`,
not as a `${NAME:-default}`, not under another name. build.example.net's login token
was copied from the old entry into `API_HEADERS`, and written into the file as a
setting's default. The caller now treats any value equal to a credential in use
on this machine as that credential, whatever it is called, and writes only
`${NAME}` -- report the variable, never the value.

**A header-list variable takes `Name:value` pairs, never a bare token.**
`API_HEADERS` for `@ivotoby/openapi-mcp-server` is
`Authorization:Basic <base64 of user:password>` for a server that asks for HTTP
Basic, `Authorization:Bearer <token>` for one that asks for a bearer token. The
caller checks a known value against its slot's shape
(`mcp.discovery.auth.value_shapes`) and reports the exact form. Prefer a server
that asks for the credential's *parts* by name (`USERNAME`, `PASSWORD`, `TOKEN`)
and builds the header itself: an encoded header is something the operator can
get wrong, and it ranks lower (`mcp.ranking.penalties.encoded_credential`).

**A candidate's `kind` is `stdio` (a process) or `url` (an address).** Other words
are mapped (`mcp.agent.kind_aliases`), but say what you mean.

**The credential an existing entry uses is checked too, and never copied.** An
entry already on the machine may hold a literal token, or a `${NAME:-default}`
whose default is one; that is what Claude Code sends every time it loads the
file. The caller keeps that value in memory for the one read-only login call
(`mcp.verification.login_check.check_in_use`) and writes the entry with
`${NAME}` only. sd.example.com's entry carried a token its service refused, while
the exported variable was unset -- the first run that checked only the variable
said nothing about it. Never repeat, move or "fix" such a value in your reply;
report the verdict you are given.

**A candidate that starts and lists nothing has not asked for a credential
either.** The reason trialling without the token is worth doing is that a stdio
server reads its credential when a tool is *called*, so one launched without it
still lists everything it can do. If the tool list comes back **empty**, that
justification is gone — you learned nothing, and nothing points at the
credential as the reason.

`mcp-atlassian` for `confluence.example.com` handshaked, advertised zero tools, and
was reported as an endpoint waiting for a token. It was registered on that
basis, the bridge and strategy stages were skipped behind it, and the variable
named in the entry — `PERSONAL_TOKEN` — is not one that package reads. The
operator set it and still had no tools, because the credential was never the
missing piece.

An empty list has several causes and a missing token is only one:

- **the variable name is wrong** — the commonest, and the one to check first.
  A package reads a *specific* name (`CONFLUENCE_PERSONAL_TOKEN`,
  `JIRA_API_TOKEN`); a generic `PERSONAL_TOKEN` or `API_KEY` is usually your
  paraphrase rather than its documentation. Quote where you found the name.
- the base URL was rejected, so the server started with nothing to talk to;
- a required argument was not passed.

**The pipeline now runs one more trial to tell these apart.** When a
candidate with a withheld credential lists nothing, or exits without naming
why, it is started once more with each withheld variable set to a stand-in
value (`mcp.packages.credential_probe.placeholder`; never a real secret, and
never written anywhere):

- **it now starts and lists tools** -- the credential *is* what it waits for.
  It is registered as an endpoint waiting for that variable, with the slot
  kept as `${NAME}`. `mcp-atlassian` and `@atlassian-dc-mcp/confluence` are
  both this case: one hides its tools until it is configured, the other exits
  without saying why.
- **nothing changes** -- the variable is proved *not* to be the missing piece.
  The verdict says so ("changed nothing"); look at the other variables it
  reads, most often the target's address, before proposing it again.

The stand-in may cost one failed sign-in at the target. A trial that timed out
is not repeated, since a slow download says nothing about a credential.

Report an empty listing as "started, exposed nothing, cause unknown" and say
which of those you ruled out. That leaves the lower tiers free to run, which is
what such a target needs.

**A candidate that crashes has failed — unless it says the credential is why.**
Two servers exit non-zero when you launch them without a token, and they mean
opposite things. Read the error before deciding which you have.

- `@ivotoby/openapi-mcp-server` exited `rc=1` inside `ToolsManager.initialize`.
  Nothing about that message concerns a credential; it was recorded as an
  endpoint waiting for a token anyway, registered with an empty slot nobody
  could fill, and the process dies before it ever reads the variable. **Report
  the error, not a credential requirement.**
- A server that prints `GITLAB_TOKEN is required` and exits has read its
  configuration, found the variable absent and declined. That is the strongest
  statement available that the credential *is* the missing piece. Reporting it
  as breakage is what cost `gitlab.example.net` a bridge that had already been
  built, pinned at the target and verified end-to-end — the target finished
  with no entry at all.

The line between them is evidence from the server, the same standard a 401 has
to meet: **the message names a variable you withheld, or it is auth-shaped.**
A traceback in someone else's connection pool is neither. Quote the line you
judged on, and quote the *exception*, not a frame above it — a stack trace's
middle is the one part that never says what went wrong.

Note that "it handshaked first" is good evidence of a lazily-read credential
but is not the only evidence there is; a server is allowed to want its token at
start-up and to say so.

If you can, run the candidate and speak MCP to it over stdio: send
`initialize`, then `notifications/initialized`, then `tools/list`, one JSON
object per line on stdin, reading one per line from stdout. A server that starts
and handshakes is proven. One that prints a help banner and exits is not.

Bound every such launch -- `timeout -k 5 60 <command>` (`gtimeout` on macOS) --
and feed it input that ends. A server that ignores stdin closing, or a launcher
(`npx`, `uvx`, `docker run`) whose child keeps the output pipe open, otherwise
holds your turn until it is cut off. A candidate that only stops when it is
killed is worth reporting as such: it will leave a process behind every time
Claude Code closes it.

If you cannot run it — the runtime is missing, a required secret isn't
available, or the caller is in dry-run mode — say so explicitly and report the
candidate unverified. Do not claim it works.

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
- Build what you install to test under the artefacts directory the brief names
  for this address, and nowhere else in the workspace. Other addresses may be
  worked at the same moment by other agents sharing the same workspace; the
  skills directory and other addresses' context files are theirs too, so read
  them if you like but never change them.

If you installed something in order to *test* it, that was the right thing to
do — but report the command that will work for someone else, not the one that
happened to work for you.

### A TLS failure is a finding, not a dead end

If a candidate cannot reach the target because of a certificate, say so and say
*which* certificate — it is one of the most fixable failures there is, and the
one that looks least like itself.

`build.example.net` is signed by `Example CA 2023.1`. The host trusts it; `curl`
fetches the OpenAPI spec with `HTTP 200`. Node ships its **own** certificate
store and does not, so `@ivotoby/openapi-mcp-server` could not load the spec,
exited during start-up, and the operator saw `failed · Connection closed` —
a message that mentions no certificate at all, and sent the diagnosis to the
wrong variable entirely.

When a launch fails, check whether the runtime's trust store is the reason:

```bash
curl -sS -o /dev/null -w '%{http_code}\n' https://host/path    # the host's store
node -e "fetch('https://host/path').then(r=>console.log(r.status)).catch(e=>console.log(e.cause?.code))"
openssl s_client -connect host:443 -servername host </dev/null 2>/dev/null | openssl x509 -noout -issuer
```

`curl` succeeding while the runtime fails with `UNABLE_TO_GET_ISSUER_CERT_LOCALLY`
(or Python's `CERTIFICATE_VERIFY_FAILED`) is the signature.

The caller now names the host's trust store in every stdio entry it writes —
`NODE_EXTRA_CA_CERTS` for Node, `SSL_CERT_FILE` / `REQUESTS_CA_BUNDLE` for
Python — so you usually need do nothing but report the finding. When the
target's chain is not in the host's store at all, the caller learns or is
given it and your brief names the bundle it verifies the target with; that
bundle goes in the entry, and you extract nothing. Two things are still yours:

- **Never set a variable that switches verification off** — `NODE_TLS_REJECT_UNAUTHORIZED`,
  `PYTHONHTTPSVERIFY`, `REJECT_UNAUTHORIZED`. They are stripped from the entry,
  and they are the wrong answer to this: pointing a runtime at the host's trust
  store and telling it to stop checking are opposite acts.
- **If the package reads a bundle from a variable of its own, report that
  variable by name** in `env`. `@ivotoby/openapi-mcp-server` has `CA_CERT_PATH`,
  and it is *not* sufficient on its own — it is applied to the API client while
  the spec loader uses plain `fetch`. Report both when a package does this.
- **A bundle you name must reach a self-signed root.** Node accepts only a
  chain that ends at a self-signed root it holds; `curl` and Python 3.13 accept
  one that stops at any trusted intermediate, so `curl` succeeding proves
  nothing for Node. smelt.example.net's entry named a bundle with "Example CA all
  2023.1" and "Example CA Root" but not the "Example Trust Root" above them: the
  server listed its tools and failed every request with `fetch failed`. Test a
  bundle with the `node -e` line above and `NODE_EXTRA_CA_CERTS` set to it, or
  `openssl s_client -CAfile <bundle> -verify_return_error`. The caller checks
  every bundle an entry names this way and swaps in one that carries the chain
  (`mcp.trust.check_named_bundles`); when none on the machine does, the entry
  is refused -- say which root is missing.

### The entry must carry everything the server needs — your shell is not passed on

When you trial a candidate, it inherits **your** environment. When Claude Code
launches the registered entry, it passes the entry's own `env` and a narrow
passthrough (`PATH`, `HOME`, …) and **nothing else**.

So a server that works for you because something happened to be exported will
not work for the operator. It also starts from the client's directory, not
yours, so a relative path or anything that depends on the working directory
breaks too. The caller launches every finished entry that way before accepting
it -- **including one that is only waiting for a credential**, launched with a
stand-in in the credential slot exactly as its trial was. That one settles the
target just as a working one does, so it has to start too: confluence.example.com
lost its server when two such candidates were only launched as written at the
very end, the chosen one died with a `FileNotFoundError`, and the address ended
empty.

If an entry does not start, an agent is asked to repair it -- the strategist,
given the entry, the failure, how the launch you proved differs from it (by
variable name) and your notes. Deterministic repairs run after that, and if
none of it works the candidate is dropped and the next one you proved takes its
place, or the target falls to the next tier rather than registering a line that
fails. So report every candidate you proved, not only your favourite, and say
in your notes what you guessed at: that is what the repair starts from.

You do not get to declare the repair good: whatever you hand back is launched
again and kept only if it starts. So change the thing you think is wrong and say
what you changed — a wrong guess costs nothing but a relaunch, while claiming
success costs the target its answer.

Practically: after a candidate handshakes, ask *what did it read from the
environment?* Everything it reads goes in `env` (a value) or `secret_env` (a
reference) -- not only what it needs to start, but every setting its
documentation or source says it reads, and every file it opens (a config, a CA
bundle, a schema) by the variable it is read from, with its absolute path. The
caller scans a script it can read and declares what you missed, but it cannot
see inside an `npx`/`uvx` package: for those, the list you give is the only one
there is. A setting the entry does not name is a setting nobody who copies the
file can find. If you are unsure whether a variable was load-bearing, say so in
your notes rather than leaving it out — the caller can repair an entry by
declaring a variable the candidate knew about, and it cannot repair one nobody
mentioned.

Two openQA entries shipped without this. They started for the program that
built them and failed in Claude Code, and the run reported `PARTIAL` rather
than "this does not work".

### Report a path absolutely, and report every one of them

Where an absolute path is unavoidable — a launcher binary, a certificate
bundle, a data file the package reads — give the **real absolute path**, in
`command`, `args` or `env` as appropriate. The caller rewrites every one of
them into `${VARIABLE:-<that path>}` before writing the entry, so it works
unchanged here and the operator can relocate it by exporting a variable. Do not
invent variables yourself; the caller cannot resolve or check them.

Four separate reports were needed to reach that rule, each a kind of path
nobody had covered: a generated bridge's own files, `/usr/bin/python3`,
`/root/.local/bin/uvx`, and a certificate bundle in a third-party package's
`OPENQA_VERIFY`. The last one is the instructive one — it sat under no
directory this program owned and matched no binary it knew, so nothing caught
it. Assume the same is true of whatever you are holding: report it, absolutely,
and let the caller make it portable.

The counterpart rule: **do not shorten a path to a bare name** to look tidier.
A bare `python3` resolved to a different interpreter with a different trust
store on the machine that launched it, and every call failed on TLS.

## Ask the server what it serves, and report what it said

When you launch a candidate and speak MCP to it, you have the best evidence
anyone will get about whether it serves your target — and it is not in the
package name or the command line. It is in what the server says once it is
talking:

- `initialize` → `instructions`, and `serverInfo.name`
- `resources/list` → **`uri`**, the single clearest signal. An Open Build
  Service server lists `https://build.opensuse.org/…`
- `tools/list` → each tool's `description`, and its `inputSchema` — a
  `base_url` whose `default` is the target is the server telling you what it
  points at
- `prompts/list` → descriptions

Quote the lines that name the target in your `notes`. The caller re-verifies
everything and reads the same fields itself, so this is not the only route —
but a candidate whose relevance you can quote is one that survives, and a stage
that hands up an irrelevant package closes the tiers beneath it.

**This is also how you tell a real find from a keyword collision.**
`@ateam-ai/mcp` came back from an npm search for `build.opensuse.org`,
handshaked perfectly, and had nothing whatever to do with Open Build Service —
it named no address, advertised no resource on that host, and mentioned it
nowhere. A package that *does* serve your target will say so somewhere in that
list. If nothing in the handshake mentions the target and nothing in the entry
names it, you have found a package, not an answer: report it as a non-answer and
let the next tier run.

The same holds for a registry hit whose *name* matches. `search=` is a match
on names, so `search=confluence` returns anything called Confluence-something —
"0DTE Confluence Pro" is a trading tool. A name match is how you found it, not
why it is relevant; it must pass the checks above like any other candidate.

## Your entry must contain the target's address

Whatever you propose is written into `.mcp.json`, and the only question an
operator asks of a line in that file is *is this pointed at my server?* A
launcher is **never probed at an address** — verification starts a process and
speaks to its pipes, so a successful handshake proves a process runs and says
nothing whatever about which service it reaches. The only evidence is the
configuration you write down.

So a launcher entry is believed **only** when the target's own hostname appears
in it — in an argument or an environment value. Anything else is refused before
it is written. There is no weaker way to pass, and there is no exception for a
package whose *name* suits the target.

This is not hypothetical. `npx -y @kubb/mcp` was registered as the MCP server
for `confluence.example.com`. `@kubb/mcp` is a TypeScript code generator; it is a
real MCP server, it handshakes, and it could never reach Confluence.

So for every candidate, make the address explicit:

```json
{"command": "npx", "args": ["-y", "mcp-graphql@2.0.4"],
 "env": {"ENDPOINT": "https://smelt.example.net/graphql/"}}
```

A generic server configured for the target is a perfectly good answer — that is
the entry above, and it is correct. A vendor's own package is *not* exempt:
`npx -y @modelcontextprotocol/server-github` with only a token in `env` is
refused, because there is nothing in it to check. Set the host variable it
accepts (`GITHUB_HOST`, `GITLAB_API_URL`, `JIRA_URL`, `CONFLUENCE_URL` …) even
when the package would default to the right place. One line, and the entry
becomes self-describing.

**When a registry entry declares an address variable, it is filled for you.**
`environmentVariables[]` naming `CONFLUENCE_HOST`, `JIRA_URL`, `…_BASE_URL` or
`…_ENDPOINT` gets the target's host or base URL (`mcp.packages.address_fill`):
a variable that names the product, or the only address-shaped variable the
entry declares. Proxies, callbacks, OAuth issuers and anything secret are left
alone. A package you found elsewhere is not filled -- set its variable
yourself.

**A fill is not evidence.** A variable that names the product (`CONFLUENCE_HOST`)
is the package saying what it serves, so filling it grounds the entry. The only
generic variable an entry declares (`BASE_URL`, `API_ENDPOINT`) is filled too, so
the server can start pointed at the target — but the program put the address
there, so it does not show the package belongs to the target
(`mcp.packages.address_fill.sole_fill_grounds = false`). Such a package still
needs its own evidence: the handshake naming the target, the vendor publishing
it, a product-named variable. Without that it is refused, not registered.

If the package reads its target from a config file rather than the environment,
say so in `findings` and prefer a variant that can be told on the command line.
An entry that depends on a file outside itself is one the operator cannot check
and cannot copy.

**A server that can serve *any* instance must be told which one.** A hosted
multi-tenant MCP -- `bugzilla.fastmcp.app` will talk to anybody's Bugzilla --
is only an answer for this target once the entry says which instance:
`bugzilla_url=https://bugzilla.opensuse.org` in a header, or the equivalent
argument or variable. Without it the entry says nothing about whose data comes
back, and it is refused. The same is true of `mcp-graphql`, `mcp-bugzilla`,
`mcp-atlassian` and every other generic server: their whole design is that they
are pointed somewhere.

**An address on the vendor's own sibling host is fine.** `api.opensuse.org` is
Open Build Service's API and is where a server for `build.opensuse.org`
connects; `api.githubcopilot.com` is GitHub's. Report those as you find them --
they are accepted, and named in the briefing so the operator sees the hop.

**A URL is judged differently, and more leniently** — see the endpoint skill. If
what you find is an *endpoint* rather than a launcher, it may legitimately live
on another hostname, because a URL that answered is itself the evidence.

## Naming and effort

- Name candidates after the service they talk to, derived from its fully
  qualified domain name: `bugzilla-example-com`, not `bugzilla`. Two services with
  the same short name are the commonest way a working configuration silently
  starts pointing at the wrong host.
- The registered name is **composed**, not taken from you verbatim: the address
  is prefixed, and your name distinguishes one find from another at the same
  address. Say what the thing *is* — `atlassian-dc-mcp`, `graphql-bridge` — and
  do not worry about repeating the address, because a whole repeated address is
  dropped rather than registered twice. (It used to be registered twice:
  `jira-example-com-jira-example-com-atlassian-dc-mcp-b1ff` is a real entry.)
- Batch your searches — one command hitting the registry, npm and PyPI beats
  three rounds of one each. You have a turn budget, and a search that ran out of
  turns reports less than a narrower one that finished.

## Writing `install`

`install` is the list of prerequisite commands run, in order, immediately
before the candidate is launched. Each entry is **one executable and its
arguments**, run directly — there is no shell, so a pipeline, a redirection, a
`&&`, a substitution or a half-sentence is not a step that fails, it is a step
that was never runnable. Steps like that are dropped before they run and noted
against your candidate.

- Write each step as a list of words: `["npm", "install", "-g", "@vendor/mcp-server"]`.
- One command per step. `curl … | tar …` is two steps, with a real file
  between them, or it is nothing.
- Do not emit checksum or signature verification steps. They cannot be
  expressed as a single command here, and a failed one rejects an otherwise
  working candidate.
- Prefer a launcher that needs no install at all — `npx -y`, `uvx` — and leave
  `install` empty. The shortest install list is the best one.
- Never install a runtime or toolchain (node, npm, python, uv, gcloud) in
  `install`, and never a second copy of one beside the copy that is there. The
  program installs a missing one from its own catalog
  (`--mcp-provision-missing`), through whatever owns the existing copy.

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

A single fenced JSON block, nothing after it:

```json
{
  "summary": "one sentence: what you found and why you chose it",
  "candidates": [
    {
      "kind": "stdio",
      "name": "vendor-mcp",
      "command": ["npx", "-y", "@vendor/mcp-server@1.2.0"],
      "env": {"VENDOR_BASE_URL": "https://target.example.com"},
      "secret_env": {"VENDOR_API_KEY": "env:VENDOR_API_KEY"},
      "transport": "stdio",
      "source": "official MCP registry entry io.github.vendor/mcp-server",
      "confidence": 0.85,
      "install": [],
      "notes": ["published by the vendor", "needs VENDOR_BASE_URL and an API key"]
    }
  ],
  "findings": {
    "sources_searched": ["mcp-registry", "npm", "github"],
    "rejected": [{"name": "other-pkg", "why": "targets the SaaS product, not self-hosted"}]
  },
  "due_diligence": {
    "grounding": {"done": true, "detail": "registry, npm, PyPI and GitHub searched for the vendor",
                  "evidence": ["registry: 0 hits for bugzilla", "npm: 3 hits, none targeting this host"]}
  },
  "proposals": ["what to try if none of these verify"]
}
```

`due_diligence` is optional here and additive: include it only for a checklist
step your searching genuinely advanced — most often `grounding`, because the
registries you query are the same ones step D asks about. The record keeps the
stronger of each step's two reports, so your evidence can complete a step the
endpoint stage left partial or failed -- which is exactly why a step claimed
without evidence counts only as partial. If you came across the target's own
MCP endpoint, list it in `due_diligence.native_endpoints` and as a URL candidate;
a URL anywhere else in your reply is not read.

Include the rejected candidates and why. The next stage uses that reasoning, and
so does the operator.

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
