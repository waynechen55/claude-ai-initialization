# Phase 4 — MCP auto-discovery

For each `--mcp-server`, four stages run in order and stop as soon as one
**answers**
([`--exhaustive-mcp`](#--exhaustive-mcp-records-every-endpoint-that-answers) keeps
going and records every working endpoint; `--the-best-mcp` keeps going and then
[registers only the best one](#--the-best-mcp-picks-one-server-and-shows-its-working)):

1. **endpoint** (tier 1) — probe the address for a live MCP endpoint. Generates
   candidates (`/mcp`, `/api/v1/mcp`, `/sse`, …), mines the target's HTML and
   well-known documents for links, and tries both Streamable HTTP and the
   deprecated HTTP+SSE transport.
2. **package** (tier 2) — search for an existing repository, npm/PyPI package or
   registry entry that already serves this target, and set it up ([how each
   source is asked](#the-package-stage-asks-four-sources--and-pypi-has-to-be-asked-by-name)).
3. **bridge** (tier 3) — synthesise a small program that bridges the target's
   existing API into MCP.
4. **strategy** (tier 3) — reason about what else could work, then do it or emit
   instructions.

**"Answers" means a working server *or* one that proved it exists by asking for
a credential.** That distinction matters more than it sounds. A package is
[trialled without its secret](#a-missing-credential-must-not-decide-which-package-wins),
so for any target whose server wants a token the honest verdict is
*provisional*, never *verified* — and while only `verified` ended the descent,
such a target was searched all over again at tier 2 and tier 3. That cost
minutes of agent time per stage and produced a second `.mcp.json` entry
competing with the first: `jira.example.com` came out of an ordinary run with a
package entry that worked and a bridge entry that did not. The stages that no
longer need to run are reported as skipped, naming the stage that answered, so
the descent explains itself:

```
• https://jira.example.com :: bridge         skipped — the package stage already
  answered, and asked for a credential; a server that is already there is the
  answer, so lower tiers are not searched
```

Set `mcp.discovery.provisional_settles_target = false` to keep searching anyway.

<a id="handover"></a>

**A tier settles the address only until the final checks.** When a later check
withdraws the server that settled it, the address is handed over
(`[mcp.handover]`), never given up. Those checks are the final re-check, a stale
verdict judged again, every entry refused at the start check, or Claude Code
itself unable to use what was written. The hand-over tries, in order:
1. another proven server;
2. one waiting for a credential (kept until the address is settled);
3. the stages that were skipped only because the withdrawn server had settled
   the address, reopened through the same stage loop, existing registrations first.

It happens once per address. gitlab.example.net lost its entry because none of this
happened: its re-check withdrew `mcp-graphql`, and the bridge that had worked
there, one tier down, was never looked at. What an address is (its product and
API kinds) is learned before any of its candidates is judged, in both modes
(`mcp.verification.learn_job_when_judging`). A verdict reached before that is
reached again before the choice (`rejudge_stale_verdicts`).

<a id="bridge-launch"></a>

A bridge is the one result that has to *start again later*, so what gets
registered is exactly what was tested — an absolute interpreter path, and the
trust store named explicitly:

| Setting | Env | Default | What it does |
| --- | --- | --- | --- |
| `mcp.bridge.generated.interpreter` | `INITIALIZE_CLAUDE_AI_MCP_BRIDGE_PYTHON` | resolved | the interpreter written into `.mcp.json`; empty resolves this program's own, absolute |
| `mcp.bridge.generated.ca_bundle` | `INITIALIZE_CLAUDE_AI_MCP_BRIDGE_CA_BUNDLE` | detected | PEM bundle a *generated bridge* verifies TLS against. Empty falls back to the shared detection in `mcp.trust.bundle_candidates`, which every registered server now uses |

Neither is cosmetic. `"command": ["python3", …]` is resolved a second time when
Claude Code launches the bridge, in a process with a different `PATH` — so on a
host carrying a Homebrew, pyenv or conda Python ahead of the system one, the
bridge that handshaked during discovery starts under an interpreter nobody
tested, with a certificate store that does not know the site's internal CA.
Every call then fails TLS verification, which reads like the target being
unreachable and is not. Pinning the path removes the ambiguity; naming the
bundle removes the dependence on whichever store that interpreter ships with.
Verification is never switched off — an internal CA is accepted by naming the
bundle that contains it.

## Native endpoint first — and the lower tiers are gated on proving there isn't one

The stage order is a tier order, and it is enforced rather than suggested. A
target's own MCP endpoint is run by the vendor, needs nothing installed and
cannot go stale; a published package is somebody else's release cadence; a
generated bridge is code that did not exist a minute ago and that only this
machine has. Descending a tier while a higher one was available installs or
writes software to do a job the target was already doing, and someone then has
to maintain it — so **the package and bridge stages refuse to run while a native
endpoint exists** (a 401 from one counts: that is an endpoint that wants a
credential), and they refuse to run while the tier-1 checklist has a step nobody
performed.

That checklist is four steps, the same four in both modes, so an agent's "I
looked" and the deterministic pipeline's "I looked" mean the same thing:

| | step | what it takes to satisfy |
|---|---|---|
| **A** | diverse paths | every path in `mcp.policy.mandatory_paths` — `/mcp`, `/api/mcp`, `/v1/mcp`, `/v1/api/mcp`, `/rest/mcp`, `/mcp/sse`, `/sse`, `/ws`, `/v1/mcp/sse`, `?format=mcp` — on every origin, not just `/mcp` |
| **B** | schemes and TLS bypasses | both `http://` and `https://`, and a retry with certificate verification disabled, because a self-signed certificate is a fact about the network and not an answer about MCP |
| **C** | content type and handshake headers | the specification's own `Accept: application/json, text/event-stream`, and the response *body and headers* read for JSON-RPC envelopes, SSE frames, `Mcp-Session-Id`, WebSocket upgrades and `WWW-Authenticate` |
| **D** | semantic grounding | what the target says about itself: its documentation and `/llms.txt`, the official MCP registry's `remotes[]`, public repositories — and in agentic mode a web search |

Each step is reported as carried out, partial, excused or never attempted, in a
table printed per address and kept in the JSON report:

```
  Native-endpoint due diligence (probing):
  step  what it establishes                                              done  what was done
  A     diverse paths probed, not just /mcp                              yes   all 22 required paths probed
  B     both schemes tried, and TLS verification bypassed once           yes   2 scheme(s) tried
  C     spec Accept headers sent, and the responses read for SSE/WS/...  yes   3 protocol hint(s) came back
  D     the target's own documentation and the public registries consu.  part  no documentation or registry entry could be read
▲ [PARTIAL ] https://example.com :: due diligence  3/4 checks; no native endpoint answered
```

A step that genuinely cannot run here — no network, no search tool — is recorded
as excused *with the reason*, which keeps the gate honest without making the
program unusable offline. A step nobody attempted is a gap, and a gap keeps
tiers 2 and 3 shut; the blocked stage says which rule stopped it and how to
override it (`mcp.policy.require_checklist = false`,
`mcp.policy.forbid_lower_tiers_when_native = false`).

**A hypothesis does not get to forbid the work that would test it.** Tier 1
closes the lower tiers when it finds something — but "found" has to mean more
than "saw an address". Against `confluence.example.com` the endpoint stage returned
`https://mcp.atlassian.com/v2/mcp`: Atlassian **Cloud**, which serves
`*.atlassian.net` tenants and not a customer's self-hosted Data Center wiki — as
that same stage's step D had already worked out for itself, following the SSO
redirect to `idp-saml.example.com` and writing down *"none is architecturally
possible for a Data Center"*. It never handshaked. It still shut tiers 2, 3 and
4, so `mcp-atlassian` — which does talk to Data Center, given `CONFLUENCE_URL`
and a PAT — was never looked for. The one target with a real tier-2 answer got
the only wrong tier-1.

So a credential wall on a host belonging to neither the target nor its
organisation leaves the lower tiers open
(`mcp.policy.lower_tiers_when_wall_is_foreign`). A *proven* endpoint still
closes them wherever it lives: `api.githubcopilot.com` serving `github.com` is
the ordinary shape of a hosted offering, and it handshaked.

**Closing a tier is a commitment, and it is checked.** The gate shuts tiers 2–4
the moment the checklist records a native endpoint, which is only defensible if
that endpoint then *becomes* something. Once it did not. Against
`developers.google.com/knowledge/api` a single run said all three of these:

```
SKIP  endpoint       no MCP endpoint answered on 166 probed path(s)
SKIP  bridge         a native MCP endpoint exists at
                     https://developerknowledge.googleapis.com/mcp
OK    due diligence  4/4 checks; native endpoint found at
                     https://developerknowledge.googleapis.com/mcp
```

— and registered nothing, having refused every alternative on the strength of an
entry nobody wrote. The registry publishes that endpoint with `remotes[]` and no
`packages[]`, on a sibling domain, and a host filter in the discovery path
dropped the URL before anything could probe it while step D recorded it anyway.

Two things changed. A registry `remotes[]` entry on **another host is probed**
rather than dropped — it is the vendor saying where their endpoint lives, which
is exactly the provenance a bare off-host URL lacks; an on-host remote still
ranks ahead of it, and `mcp.discovery.registry_remote_other_hosts` (3) caps how
many other hosts one registry answer may send the run to. And before any address
is given up on, **anything the checklist believes in is probed once more**:

```
• the checklist recorded 1 endpoint(s) that nothing registered -- probing them
  before giving up
```

If it answers it becomes an ordinary candidate, judged by the ordinary rules. If
it does not, the contradiction is reported rather than left as an empty result
that reads like an honest "nothing found". Both pipelines run the check.

**An endpoint counts as the target's only if it could be registered for it.**
Against `confluence.example.com` the registry's name search for `confluence`
returned "0DTE Confluence Pro" at `https://mcp.0dteconfluence.com/mcp` — a
trading tool whose name contains the word. It handshaked with 37 tools, was
recorded as the native endpoint, and shut tiers 2–4; the writer then refused it
because nothing tied it to the target, and the summary still said "native
endpoint found". Now:

- An endpoint is recorded in the checklist only if it passes the same
  registration check the writer applies. One that fails is **ruled out** with
  the reason, closes no tier, and cannot be recorded again by any route — the
  probe, an agent's checklist, or a merge
  (`mcp.policy.native_must_be_registrable`).
- After every stage, before the next gate, any endpoint the checklist believes
  in but no verified candidate backs is probed; one that does not answer, or
  cannot be registered, is ruled out (`mcp.policy.vet_record_before_gate`).
- A stage the policy skipped because of an endpoint later ruled out is run
  after all, marked `reopened` in its findings
  (`mcp.policy.reopen_when_native_retracted`).
- The due-diligence line says "no native endpoint of this target answered (N
  ruled out …)" instead of "native endpoint found", and the agent's retry is
  told which endpoints were ruled out and why.

Set any of the three to `false` for the previous behaviour.

Step D is the one that differs between the modes, because a deterministic run
has no model to search with. In traditional mode it reads three deterministic
sources instead: the target's own documentation paths (`/llms.txt` first — the
emerging convention for "what an agent should know about this site", and
increasingly where MCP endpoints are announced), the official MCP registry's
`remotes[]` filtered to the target's own host, and public repositories. A real
search engine can be added with
`INITIALIZE_CLAUDE_AI_MCP_SEARCH_URL='https://…?q={query}'`, and it is empty by
default on purpose: there is no default search provider, because sending a
customer's internal hostname to a third party is your decision to make rather
than this program's.

Two deliberate departures from a strict reading of "native endpoint always wins":

- **HTTP and SSE endpoints are registered as they are.** `claude mcp add-json`
  speaks `http` and `sse` natively, so there is no reason to put a stdio bridge
  in front of a remote endpoint — the bridge would be tier-3 code wrapping a
  tier-1 server.
- **A WebSocket endpoint does unlock the bridge stage.** Claude Code has no
  WebSocket MCP transport, so that native endpoint cannot be dialled as it
  stands. The bridge is then built *for the target's own endpoint* rather than
  as a substitute for it, which is the policy working rather than an exception
  to it.

**An upgrade alone is not an MCP endpoint.** Many services keep a WebSocket for
their own pages — live job updates, notifications — and a `101` from one of
those used to close the lower tiers and ask for a bridge to something that was
never MCP. With `mcp.discovery.websocket_requires_mcp_evidence` (on by default)
an upgrade counts only when the server accepts one of
`websocket_mcp_subprotocols` in `Sec-WebSocket-Protocol`, or the route matches
one of `websocket_mcp_path_patterns`. Any other upgrade is reported as a hint
and decides nothing, and a refusal status is never an upgrade, whatever headers
come with it.

**A reply that proves nothing is recorded as proving nothing.** A `2xx` that is
neither a refusal nor JSON-RPC — a gateway warming up, a cache or proxy
answering for the endpoint — used to be written down as "does not speak MCP",
which threw away the one endpoint `openqa.opensuse.org` has. Such a reply is now
asked again (`mcp.discovery.inconclusive_retries`, `…_retry_delay_seconds`
apart). If it still says nothing, it is recorded as *inconclusive*, and every
rejection records what actually came back: status, content type and the first
`mcp.discovery.reply_excerpt_chars` characters of the body, redacted. That lets
you tell a proxy's placeholder from a real refusal.

Everything in `[mcp.policy]` in `initialize_claude_ai/data/40-mcp.toml` is
configuration, including which stage belongs to which tier and what step A
requires.

## The package stage asks four sources — and PyPI has to be asked by name

Once the gate opens, stage 2 looks for a server somebody has already published.
Three of its four sources take a *query*, rendered from
`mcp.packages.query_templates` against the **product** in the address — the
leftmost meaningful label, because an internal host is
`<product>.<organisation>.<tld>`:

| Address | Searched for |
| --- | --- |
| `https://api.example.com/v1` | `example` |
| `https://confluence.example.com` | `confluence`, then `example` |
| `https://smelt.example.net` | `smelt`, then `example` |

Which labels count as noise is `mcp.packages.generic_host_labels`, and that
list is load-bearing. With `.de` missing from it, `smelt.example.net` was once
searched for as the single word **"de"** — which is Spanish — and npm answered
with a Colombian regulatory-compliance server that was duly registered as
SMELT's MCP endpoint. Both Atlassian hosts were asked about as `"example"`, so
`confluence-datacenter-mcp` and `jira-datacenter-mcp` could never be found.

| Source | Asked | What a hit becomes |
| --- | --- | --- |
| official MCP registry | `registry_query_templates` (the bare product word) | an exact command, built from the entry's own `packages[]`/`remotes[]`, with any declared address variable set to the target |
| npm | `query` | `npx -y <package>` |
| **PyPI** | **each name from `pypi_name_templates`** | `uvx <package>` |
| GitHub | `query` | a lead: the repository is named and the operator (or stage 4) takes it from there |

PyPI is the odd one out because it has no usable search API — its JSON endpoint
answers about one exact distribution name and nothing else. So the names are
produced rather than searched for: each `mcp.packages.pypi_name_templates` entry
is rendered against the product name, normalised the way PyPI normalises a
distribution name (PEP 503: lower-cased, runs of `-`, `_` and `.` collapsed to a
single `-`, so `MCP_Server.Example` and `mcp-server-example` are one question),
and looked up. For `api.example.com` the packaged templates ask about
`mcp-server-example`, `example-mcp-server`, `mcp-example` and `example-mcp`.

Two consequences worth knowing:

- **Every template is MCP-specific on purpose.** A bare `{target}` would match
  an unrelated package that merely shares the product's name, and the stage
  would then propose running it.
- **The names asked about are recorded** under `pypi_names` in the stage's
  findings, in the log and in the JSON report — because a search hit carries its
  own query with it, while a lookup that matched nothing leaves no trace, and
  "no published MCP server matched this target" is only useful if you can see
  what was tried.

The lookups run after the query searches and are not subject to their early
exit: they cost the same short list of requests however much npm turned up, and
a Python-published server should not be invisible to a run that happened to find
Node packages first.

| Setting | Default | What it does |
| --- | --- | --- |
| `mcp.packages.pypi_package_url` | `https://pypi.org/pypi/{name}/json` | the lookup endpoint; empty turns the PyPI route off entirely — no requests, not requests whose answers are discarded |
| `mcp.packages.pypi_name_templates` | `mcp-server-{target}`, `{target}-mcp-server`, `mcp-{target}`, `{target}-mcp` | the names to ask about; an empty list also turns the route off |
| `mcp.packages.pypi_max_lookups` | `6` (`INITIALIZE_CLAUDE_AI_MCP_PYPI_LOOKUPS`) | ceiling on the lookups one target may cost, however long the template list is; `0` removes the cap |
| `mcp.packages.registry_launchers` | `npm = "npx"`, `pypi = "uvx"`, `oci = "docker"` | ecosystem → launcher, for both registry entries and plain search hits; an ecosystem with no launcher is reported as a lead rather than guessed at |
| `mcp.packages.registry_query_templates` | `{target}` | what the official registry is asked; its `search=` is a substring match on server *names*, so a phrase such as "confluence mcp server" matches nothing |
| `mcp.packages.registry_timeout_seconds` | `30` (`INITIALIZE_CLAUDE_AI_MCP_REGISTRY_TIMEOUT`) | the registry's own timeout; it is slower than npm, and a timeout is reported under `search_problems`, never read as "nothing exists" |
| `mcp.packages.address_fill.*` | on | fill a registry entry's declared address variable (`…_HOST`, `…_URL`, `…_ENDPOINT` …) with the target's host or base URL, when it names the product or is the only one; `exclude_patterns` keeps proxies, callbacks and OAuth issuers out, and secrets are never filled. A generic sole variable is filled but is not evidence the package serves the target (`sole_fill_grounds = false`); a product-named one is |
| `mcp.packages.credential_probe.*` | on | the credential test below; `placeholder` is the stand-in value, `timeout_seconds` follows `trial_timeout_seconds` |
| `mcp.packages.credential_probe.companion_suffixes`, `…max_companion_trials` | `USER`, `USERNAME`, `LOGIN`; `3` | for a credential in parts: endings tried after the secret's prefix to find its other part, and how many launches that may cost (`0` turns it off) |
| `mcp.packages.failure_excerpt_chars` | `300` | how much of a rejected trial's error the log quotes, on one line and redacted |
| `mcp.verification.reuse_verdicts.*` | on | remember each target's launch verdicts; `transient_markers` and `transient_elapsed_fraction` decide which failures are worth trying again |

## A server that waits for its credential is tested with a stand-in

A stdio server is trialled without its credential, because most read it only
when a tool is called and list their tools regardless. Some do not.
`mcp-atlassian` lists no tools until it is configured, and
`@atlassian-dc-mcp/confluence` exits without its token and without saying why.
Both were filed as failures, and `confluence.example.com` finished with nothing.

So when a trial without the credential lists nothing, or fails without naming
a variable, the candidate is started once more with each withheld variable set
to `mcp.packages.credential_probe.placeholder`:

- **it starts and lists tools**: the credential is what it waits for. It is
  registered as an endpoint waiting for that variable, with `${NAME}` in the
  entry. The report says "only once X was set (to a stand-in value)" and does
  not claim it is usable.
- **nothing changes**: the variable is proved *not* to be the missing piece,
  and the verdict says so. The agent is told to look at the other variables.
- **it fails differently**: the server used the stand-in and wants the rest of
  a credential in parts. openSUSE's `osc-mcp` said "password not found"
  without `OSC_MCP_PASSWORD`, and "user not set for apiurl api.opensuse.org"
  with a stand-in for it. That counts only when the new failure is
  credential-shaped (the `startup_credential` markers). The other part is then
  looked for by name: variables the failure names, then the secret's prefix
  with each of `credential_probe.companion_suffixes` (`OSC_MCP_USER`). Each is
  tried with a stand-in, at most `max_companion_trials` times. The part that
  makes it start gets a slot of its own, and the entry waits for all of them.
  A server that names its credential when it refuses to start gets the same
  stand-in trial before its words are taken alone, so a credential in parts
  is found there too.

The stand-in is never written to `.mcp.json`, the report or the log, even if
the server echoes it. It may cost one failed sign-in at the target. A trial
that timed out is not repeated.

Three other changes came from the same run:

- **A launch is trialled once per target.** The agent proposed the same two
  launches for over an hour, and each was downloaded and started again. The
  same command, variables, headers and install now get the recorded verdict
  back ("already trialled"). Timeouts and other passing failures are the
  exception.
- **"Stop at the first" means the first that can be registered.** The stage
  stopped at a server that started but named no address. The registration
  gate then refused it, and sixteen candidates behind it were never tried.
  Now the search carries on past a candidate the gate would refuse.
- **A rejected trial is quoted in the log**, as one redacted line of what the
  server said.

A candidate is only ever *proposed* by this stage. Launching it is a trial run
governed by `mcp.packages.trial_run`, and a dry run never launches anything —
so a `uvx` package on a host without `uv` installed is reported as
`uvx is not installed` rather than half-attempted.

## A package must have something to do with the target

A registry search is a keyword match, and an unjudged keyword match will
cheerfully answer *"which MCP server talks to Confluence?"* with a TypeScript
code generator. `@kubb/mcp` was once registered for Confluence, Bugzilla **and**
build.opensuse.org — one package standing in for three unrelated services —
because all three searches were keyword matches and it handshakes.

So a candidate has to be relevant, and there are exactly two honest ways to be
(`mcp.packages.require_relevance`, on by default):

1. **It names the product** — `confluence-datacenter-mcp` for
   `confluence.example.com`.
2. **It is a generic server configured for this host** — `mcp-graphql` says
   nothing about SMELT anywhere, and is the right answer for `smelt.example.net`
   because it is pointed at `https://smelt.example.net/graphql/`. Relevance lives in
   the configuration, not the name.

A gate that knew only the first would throw away that working SMELT entry, which
is why both clauses exist. Discarded hits are listed in the report rather than
dropped silently — a gate nobody can inspect is a gate nobody can correct.

**A substring is not a name.** "Names the product" means the word stands on its
own: `example-mcp` names Example, `examplep_administradores` does not, and
`githubcopilot` does not name GitHub. A registry name such as
`io.github.mcp-dir/examplep_administradores-mcp` is read as what it is — the
account `mcp-dir` publishing on GitHub, not GitHub publishing — for every
namespace in `mcp.packages.hosting_namespaces` (`io.github`, `io.gitlab`, …); a
reversed domain like `com.googleapis.developerknowledge` names its domain. A
host is matched by whole labels, so `smelt.example.net.evil.net` does not name
`smelt.example.net`. One remote on `api.mcp.ai` was once registered for seven Example
hosts and github.com because "examplep" contains "example" and `io.github.mcp-dir`
contains "github". The same word-boundary rule is used by the relevance gate,
by grounding's "the vendor said so" (which also never reads a URL's *path* as a
name), and by both modes.

**What the server says it is outranks what led to it.** A server is a target's
only if something about the target says so: *where it is* (the target's host or
organisation), *what it is configured for* (the entry names the target), or
*what it says it is* (its handshake's `serverInfo.name`, `instructions` or tool
names name the target or its organisation as a whole word). A search hit, a
registry listing, an agent's report and an entry an earlier run registered are
claims: they can nominate a server, never make it the target's. So a candidate
on an unrelated host that answered is refused when its own answer names nothing
of the target, and always when it describes itself as a front for other servers
(`mcp.registration.identity.gateway_markers`: `marketplace`, `catalog`, …). The
`api.mcp.ai` remote answered `MCP.AI platform … consultas` with tools
`marketplace` and `examplep_administradores_consultar`; no claim could make that
Example's. A candidate that never answered is left to the other rules
(`mcp.registration.identity.accept_unanswered`).

An entry already in `.mcp.json` or the `claude` CLI that fails this test — it
points at an unrelated host and its own answer names something else — is
reported as `<target> :: retire <name>` and removed from both, including the
CLI's copy of a file entry (`mcp.registration.retire_refused_existing`). It is
judged once more first, on everything the run found by then. Only names this
program gives the address (`<base>` or `<base>-…`, and never a name another
address in the run gives) are removed; anything else is left in place, reported
with the command to remove it by hand, and makes the verdict PARTIAL. The step
is CHANGED when the entry was removed, OK when a dry run says it would be, and
PARTIAL when it was left. Both modes record it under `stale_registrations`, with
the hosts it connects to (never its URL or command line) and a `result` of
`removed`, `would be removed` or `kept`; the phase's data lists them as
`mcp_retired` and `mcp_wrong_registrations_kept`. The agents are told the entry
is NOT THIS ADDRESS'S.

## A server already registered here is tried first

A later run used to search from nothing while `.mcp.json`, or an earlier
`claude mcp add`, already held a working entry for the very address asked
about. `smelt.example.net` had a working `mcp-graphql` entry and a run ended with
nothing for it. So every registration this machine already has is read back
(`mcp.discovery.reuse.sources`: the project `.mcp.json`, and what
`claude mcp list` / `claude mcp get` report) and matched to an address by host.

Each match is a candidate at its own tier, and nothing more:

- a remote entry is a tier-1 endpoint candidate. The tier-1 checklist still
  runs in full, and the reused endpoint is recorded next to what it found;
- a local entry is a tier-2 package. It is a tier-3 bridge if its launch line
  matches `mcp.bridge.generated.filename_template` or one of
  `mcp.discovery.reuse.bridge_patterns`.

It is verified, grounded and gated like anything discovery found, so an entry
that has stopped working settles nothing. The agent of that tier is told what
was tried and how it ended, and is asked to improve on it rather than copy it.

**No credential is carried over.** A literal secret in the registration's `env`
or headers becomes a `${VAR}` reference, named by
`mcp.discovery.reuse.literal_env_template` (`{NAME}_{KEY}` by default). A note
tells you which variable to export. An entry with a secret inside an argument
cannot be rewritten like that, so it is not reused at all.

**...but the credential it uses is checked.** That literal -- or the default of
a `${NAME:-…}` whose `NAME` is unset -- is what Claude Code sends every time it
loads the entry. sd.example.com's entry held a token its service refused, and a run
that checked only the exported variable (unset) said nothing about it. The
value is now kept in memory, redacted from every log, for one read-only login
call (`mcp.verification.login_check.check_in_use`), and the verdict names its
source: "rejected the value written in the existing entry 'sd-example-com'", or,
if it is accepted, "that value is the one to export". It is still never written.

| Setting | Env | Default | What it does |
| --- | --- | --- | --- |
| `mcp.discovery.reuse_existing` | `INITIALIZE_CLAUDE_AI_MCP_REUSE_EXISTING` | `true` | offer existing registrations as candidates; `--no-reuse-existing-mcp` turns it off |
| `mcp.discovery.reuse.sources` | — | `project_file`, `claude_cli` | where registrations are read from |
| `mcp.discovery.reuse.confidence` | — | `0.9` | ordering hint for a reused registration beside what discovery found; verification is what decides |
| `mcp.discovery.reuse.bridge_patterns` | — | empty | extra regexes that mark a local entry as a bridge |
| `mcp.discovery.reuse.literal_env_template` | — | `{NAME}_{KEY}` | the variable a literal credential is replaced by |
| `mcp.discovery.reuse.opaque_value_min_chars`, `…opaque_value_min_entropy` | — | `24`, `4.0` | a literal that is one random-looking token at least this long and this varied (Shannon bits per character) is *suspect*: kept out of `.mcp.json` and kept working, its value held in Claude Code's settings env (`Candidate.kept_out`). Suspect is not a credential: it is never asked of you, and a value equal to the server's own default (read from its source) is configuration however random it looks. A 44-character token under `SMELT_EXAMPLE_NET_HEADERS` was once copied into `.mcp.json` because "headers" is not a secret's name; a bridge's User-Agent was once mistaken for a token. Paths, URLs, `${…}` references and pinned package specs never count |
| `mcp.discovery.reuse.list_line_pattern`, `…get_fields` | — | see TOML | how the `claude` CLI's output is read, so a CLI that prints differently is a TOML change |

## A target is searched for by the kind of API it serves

`smelt.example.net` has no MCP endpoint, no OpenAPI document and no server named
after it, so discovery said "the target publishes no machine-readable API
description" and searched the registries for "smelt". It serves GraphQL, and
`mcp-graphql`, a generic server for *any* GraphQL API, works as soon as its
`ENDPOINT` points there.

An API has a *kind*, each kind is recognised in its own way, and each kind may
already have generic servers. Each kind is one table,
`[mcp.discovery.api_kinds.<name>]`:

| Key | What it is |
| --- | --- |
| `paths`, `method`, `body`, `content_type` | the recognising request, sent to each path on the target's origin |
| `content_types`, `required_keys`, `success_markers` | what the reply must be. Only a `2xx` counts. It must be served as one of `content_types` (when listed), and its JSON must hold every dotted key of `required_keys` (`data.__schema` for GraphQL; `version`, `platformVersion`, `_links.self` for a service desk). It must also carry one of `success_markers`; a marker the request itself contains (its URL or body) never counts, because error pages repeat the request. A `200` from a path that merely exists is not an API |
| `doc_pages`, `link_patterns` | pages for people (GraphiQL, a playground) that name the endpoint; only same-host links count (`mcp.discovery.api_kinds_same_host_only`) |
| `finding` | instead of asking: the stage finding another detector records (`openapi_url` for OpenAPI) |
| `search_terms` | searched for as well as the product's name |
| `adapters` | generic published servers: `package` on a `registry`, with `env` / `args` templates filled with `{api_url}`, `{base_url}` and `{host}` |
| `operations`, `security` | for a kind no server speaks and that publishes no OpenAPI document: its documented read-only operations (`name`, `path` with `{param}`s, `summary`, `query`) and the HTTP scheme its credential is sent under. The bridge stage generates a bridge from them, in both modes, and the agents of tiers 2 and below are shown them. An operation marked `public = true` needs no login. A bridge generated from the kind (or one an agent wrote and marked with `"api_kind"`) must answer it with real content (its `required_keys`, or the kind's) before it counts. An error page means the bridge is broken, not waiting for a credential |

Once a path is recognised, the same request goes to a sibling path that cannot
exist (`mcp.discovery.api_kind_control_name`, `""` turns the control off). If
the sibling is recognised too, the server answers everything that way and the
kind is not recognised. smelt.example.net's HTML 404 for `/rest/servicedeskapi/info`
repeats the path. When the path was the marker and any status counted, smelt
"served" a Jira Service Management customer API, and the bridge built for it
replaced the working `mcp-graphql` entry. A kind an agent records is held to
the same test before anything is built for it.

In both modes, what was found is recorded in the endpoint stage's
`findings.api_kinds`. The package stage then trials each adapter next to its
search hits, and the bridge stage names the API it would bridge instead of
denying there is one. The playbook does the same. The agents of tiers 2 and
below are told the kind, what to search by, and the exact launch that was
trialled (`mcp.agent.api_kind_brief`). With the package stage turned off
(`mcp.packages.enabled = false`), the bridge stage trials the adapters instead.

An adapter is pinned to the version its registry calls current at run time
(`mcp.packages.version_lookup.<registry>`, looked up once per run), so what is
written is what was trialled:

```json
"smelt-example-net-mcp-graphql": {"command": "npx", "args": ["-y", "mcp-graphql@2.0.4"],
                              "env": {"ENDPOINT": "https://smelt.example.net/graphql/"}}
```

**Proof decides, not order.** Several rules apply in both modes:
- **The documented bridge is always tried.** When the target serves an API kind
  with documented operations, the bridge stage generates that bridge before the
  address is settled. Only proved access skips it: an accepted login, or a
  server that works with nothing left unproved. An agent's own bridge that is
  only waiting for a credential, or was refused, no longer ends the stage
  (`mcp.discovery.generated_bridge_settles_first`).
- **Candidates are ranked by proof.** The order is: accepted, not yet checked,
  unproved, refused.
- **A refusal is checked before the credential is blamed.** A refusal from a
  bridge whose way of sending the credential nobody checked (no API
  description, 401 challenge, configured `security` or published code behind
  it) is unconfirmed and doesn't end the search
  (`mcp.verification.login_check.unconfirmed_rejection_settles`). If one
  candidate's service accepts a credential and another's refuses the same one,
  the refusing candidate is discarded as broken. The two are compared by
  fingerprint, never by value.
- **Unproved existing entries are replaced everywhere.** An existing entry
  whose access never proved anything (every call empty) is replaced, in the
  file and in the claude CLI, when a bridge for the target's other API proves
  access (`mcp.registration.replace_unproved_existing`).
- **Undeclared credentials are named.** Credential-like variables a script
  reads but does not declare are named to the operator as needed
  (`mcp.registration.name_undeclared_credentials`).
- **One credential, one variable.** A candidate proved with the credential
  another entry for the same host reads is written to read that entry's
  variable (`mcp.verification.login_check.reuse_variable_names`).

- **Proof decides across tiers.** A candidate whose login was accepted takes
  the slot of the least-proved candidate an earlier tier left, for example one
  whose login was never checked.
- **Bridges are judged by their tools.** A bridge whose tools are named as a
  detected kind's operations (`mcp.discovery.api_kind_min_shared_tools`) is
  that kind's bridge, whoever wrote it.
- **The in-use credential reaches bare hosts.** The credential this machine
  already sends a host is offered to a server whose entry names that host bare
  (`JIRA_HOST=sd.example.com`).
- **Slow is not broken.** An existing registration that times out is tried
  again with a longer deadline (`mcp.packages.slow_start_retries`,
  `slow_start_timeout_factor`), and a timeout never replaces an entry
  `claude mcp list` reports connected. The finished entry's own start check
  gets the same patience: a server still running at every deadline is *not
  confirmed*, never "does not start" -- it is written, not repaired against
  or refused, and the report says so. gitlab.example.net's GraphQL server spent
  7 to 68 s introspecting a large schema; refused after one 90 s try, its
  address was left with no entry.

**A setting is not a credential; a public server works without a login.**
- **Unset settings.** In a trial, a setting's `${NAME}` that nothing sets (a
  mode, a selector such as `BUGZILLA_AUTH_MODE`) means the setting is launched
  without; only credentials make a server wait. The writer writes such a
  setting `${NAME:-}`, or leaves it out when the server started without it
  (`mcp.registration.optional_unset_settings`). The healing repair
  `drop_unset_setting` fixes entries that crash on the literal text, which is
  what Claude Code passes for an unset `${NAME}`.
- **Public servers.** A server whose read calls succeed with no credential is
  working without a login (`login_check.try_without_credential`,
  `public_works_without_login`). Its credential slot is written `${NAME:-}`
  and isn't counted as missing.
- **A URL entry is probed with what Claude Code will send.** Its header's
  `${NAME}` is resolved from the environment and Claude Code's settings env,
  where this program keeps credentials (`mcp.verification.resolve_from_claude_settings`).
  Probed unexpanded, openQA's entries went out anonymously and stayed
  "waiting for a credential" although the token was in place.
- **"No credential" is enforced, not assumed.** A trial launched without a
  credential withholds every credential it is not given from the environment
  it would inherit: the pending variables, unsupplied slots, and credential
  names its source reads (`mcp.verification.withhold_unsupplied`). github.com's
  trial once inherited the token, answered, and the token was called optional.
- **A value is a credential by evidence.** One rule
  (`McpWriter.credential_evidence`) decides for every guard. A *proven*
  credential (by name, inline, or already in use) is a `${NAME}` you supply. A
  value equal to the server's own default (`mcp.env_contract`, including
  `os.environ.get("X") or "lit"`) is configuration. One that merely looks
  random is kept out of the file and kept working, and never asked of you.
- **Existing entries.** An existing registration counts as answering only the
  way Claude Code runs it (`mcp.verification.judge_existing_as_written`):
  launched exactly as written, and not reported failing by `claude mcp list`.
  A broken one is replaced in the file and the CLI. bugzilla-opensuse-org went
  missing when its broken CLI copy was kept as "answered during this run".

**Checks lead to a working server: repair, client bridges, a checklist.** A
check that fails isn't where the story ends.
- **Repair.** Each failure is classified (`[mcp.repair.classes]`):
  unread_credential, header_shape, credential_not_sent, client_only,
  no_account, rejected. Its repairs are tried in order:
  - `move_to_read_variable`: the credential moves to the variable the
    server's own source reads (a header list gets
    `Authorization: <scheme> ${VAR}`);
  - `fix_header_name`;
  - `client_bridge`;
  - `operator`: nothing to repair; the checklist says what to do.

  Each variant is proved again by every check and kept only if it passes
  (`max_variants`).
- **Trials expand like Claude Code.** `${NAME}` inside a setting is expanded
  in a trial the way Claude Code expands it, in memory.
- **The client bridge.** For a login only a client performs (api.example.net's
  `Signature`, Kerberos, NTLM), the bridge stage generates a stdio server
  that drives the service's own client (`[mcp.discovery.clients.<name>]`; `osc`
  is configured):
  - argv only, never a shell, and only the listed read operations;
  - it **never depends on how the client is set up on this machine**: at
    start-up it writes the client's own configuration (`config`, `lines`) from
    the entry's variables into a private temporary directory, points the client
    at it (`OSC_CONFIG`), and points HOME and the XDG directories there
    (`isolate`), so `~/.config/osc/oscrc` is never read, whether it exists or
    not;
  - the login comes from named variables, by `method_order`: `<SERVICE>_USERNAME`
    plus `<SERVICE>_PASSWORD` wherever a password login is offered, and
    `<SERVICE>_USERNAME` plus `<SERVICE>_SSH_PUBLIC_KEY` (the registered public
    key's path) only for a service that offers nothing but `Signature`. The
    host is the one that offered that login, the API host first
    (api.opensuse.org, not build.opensuse.org's own login);
  - a key-path variable holding anything but an existing `.pub` file is named
    plainly, by the value-shape check and by the bridge before osc runs
    (`file_parts`);
  - it waits for those variables like any other entry. The same entry works
    on any machine with the same variables.
- **Checklist.** The report's *what only you can do* step lists the remaining
  steps, numbered, each followed by `initialize-claude-ai run --phases 4
  --mcp-recheck <host>` (`mcp.registration.recheck_command`).
- **Recheck.** `--mcp-recheck` (`mcp.recheck`) proves an address again from
  its existing and last working entries only, with the usage checks and
  repairs, in seconds. `--mcp-recheck all` does it for every entry of the
  project file. An entry's address is the one its last proof recorded, else the
  host it is named after; one neither ties to an address is named, never guessed.

**The same user name and password on several servers.** One login (single
sign-on) may work on many servers, so the same user name and password in
several entries is never reported. A server that doesn't accept it says so in
its own login check. A token or API key is issued by one server, so the same
one in entries for two different servers is reported
(`mcp.discovery.auth.instance_bound_patterns`).

**What worked is remembered.** An entry that did the job is recorded
(`[mcp.known_good]`, `mcp-known-good.json` in the state directory, owner-only).
That means its usage contract passed, or a login was accepted by a call that
fails without it, and Claude Code itself had no complaint.
- The record holds the entry as written, with `${…}` references and never a
  value, plus the tasks it passed. An entry holding a literal credential is
  never recorded.
- On the next run the last working entry is tried first, at its own tier
  (`try_first`). build.opensuse.org's `osc-mcp` worked on Oct 14, and the next
  day's run knew nothing of it.
- When nothing works for an address any more, the report's *what changed since
  it last worked* step lists the differences: launch, package and version,
  variables read, settings. Names and plain settings only.
- An entry whose only successes were calls anybody can make is reported as
  never having proved its login.

**Every way to log in, and which to use.** Every `WWW-Authenticate` challenge
is read, on the target and on its API hosts (`api_host_patterns`, `api.{domain}`),
at the product's `protected_paths`. Each scheme is classified in
`[mcp.discovery.auth.methods]`:
- header-capable: basic, bearer, token;
- client-only: `Signature`, `Negotiate`, Kerberos, NTLM, digest.

A client-only scheme is never "a token" again. The product is recognised from
the catalogue (`[mcp.discovery.products.<name>]`: Open Build Service, Jira,
Confluence, GitLab, Bugzilla, openQA, GitHub). The catalogue says where a
token is created, how it's sent, which scope reading needs, which token types
cannot read, and which client speaks it.

No kind of login is preferred (`methods_equal`, on by default): an SSH-key
signature, a token, an API key and a password count the same, and every
feasible one is built and tried. Proof decides: a call that needs the login
accepted, the usage checks passed, a new terminal's Claude Code using it.
Before anything is proved, logins are ordered by evidence only -- what the host
accepted or refused before (`[mcp.discovery.auth.history]`), which logins its
API host itself offers, and what this machine holds. `method_order` is then just
the order alternatives are listed in; switching `methods_equal` off restores
the old ranking (token, API key, password, client login last) and the
`penalties.password_when_token` penalty. A password is chosen only where a
challenge offered it (`offered_only_methods`). The report's *how to log in* step
gives the exact advice. For build.example.net it reads: "log in through osc,
signing requests with your SSH key (the service asks for Signature at
api.example.net) … `osc -A https://api.example.net whois`", and it notes that
build-service tokens can't read.

**Working means doing the job: the usage contract.** A server is accepted
only when it passes real reads (`[mcp.contract]`):
- **each kind's own tasks** (`[mcp.contract.products.<name>]`). A task that
  sends nothing of its own is asked of any server that lists the tool, such as
  a Jira server listing projects or a GraphQL server describing its schema. A
  task that sends a product's own words (GitLab's `{ currentUser { username } }`)
  belongs to that product: it is asked only where the address is that product,
  either recognised as it or serving it as an API kind (`scope = "address"`, or
  any task with `arguments` under `scope_by_arguments`). smelt.example.net's
  GraphQL bridge was once asked GitLab's question, failed it, and was left out
  of `.mcp.json`. `scope_product_tasks = false` restores the old reading;
- **your own checks for each address** (`[mcp.contract.targets."<host>"]`), in
  `~/.config/initialize-claude-ai/config.toml` or `./initialize-claude-ai.toml`,
  never in the packaged file:

  ```toml
  [mcp.contract.targets."sd.example.com"]
  checks = [{ name = "read one request", tools = ['^get_request$'],
              arguments = { 'issueIdOrKey' = "SD-204666" }, expect = ["SD-204666"] }]
  ```

How tasks are judged:
- A task fails on an error, an empty answer, a web page, a missing expected
  value, or a mismatch with the target read directly (`truth = { path, fields }`).
- Expected values are never printed back.
- Tasks run only when the server has what it needs, and never with
  `login_check.mode = "off"`.
- A call refused as a request the server doesn't have (a 400, "Cannot query
  field", an unknown argument: `not_fitting_markers`) means the task doesn't
  fit this server. It is neither a pass nor a failure, and only the tasks that
  fit decide. A refused login (401/403) still counts
  (`misfit_is_not_a_failure`).
- A declared credential slot isn't proof that a login is needed. A server whose
  usage tasks pass with nothing in the slot has it written as optional
  (`${NAME:-}`); a refusal still keeps it waiting
  (`login_check.job_done_is_no_login_needed`).
- The handshake and login check are diagnostics; the contract decides.

What follows from the verdict:
- A replacement must pass more of the contract than the entry it replaces.
- An answer that says "try again later" (`429`, `502`/`503`/`504`, a rate
  limit; `login_check.temporary_markers`, `temporary_statuses`) is asked once
  more after `temporary_retry_seconds` and is then **inconclusive** -- never a
  failure and never a reason to repair. So is a connection that dropped or
  stalled on the way (`unexpected EOF`, `TLS handshake timeout`, resets,
  transient DNS). `fetch failed` and certificate errors are not: they are a
  trust store to repair (`transport_failure_markers`, `heal_transport`).
- Claude Code's own check outranks the program's probes. A failed check through
  Claude Code is asked once more (`registration.claude_check.retries_on_failure`).
  A target whose entry Claude Code still cannot use is written but reported
  PARTIAL and never counted as working. Calls to each host are paced
  (`[mcp.verification.pacing]`) and the same call is asked once per run, so the
  run's own checks do not provoke one.
- A repair is kept only when the original, asked again, still falls short.
- **Settings proved by the tool that reads them.** Each read tool that couldn't be
  called is asked once with stand-in arguments, only to learn whether it accepts
  its settings (`mcp.verification.login_check.probe_settings`). Address settings
  are also tried on the hosts that serve the address's API
  (`mcp.verification.address_forms.try_api_hosts`), including when a login is
  refused on the website host.
- **Identified even behind a login.** An address that refuses anonymous callers
  everywhere is still recognised: a `401`/`403` in an API's form, while a path
  that cannot exist answers otherwise. Client bridges are built for every login
  route the service offers. The SSH key is taken from your standard key files,
  each reported as refused or unusable, never asked for as `SSH_AUTH_SOCK`.
- **The file is checked at the end.** After each address and once more at the end
  of the run, every address that has a server must have an entry in `.mcp.json`
  (`mcp.registration.check_in_file`). A lost entry is written again, a kept
  working one is recorded, and anything still missing is reported beside the
  final verdict with the reason. A copy of a server this machine already has (for
  example another version pin of the same package) is treated as that server.
- **A working server never vanishes from `.mcp.json`.** A proven server refused
  because nothing yet tied it to the address is judged again once the run finds
  evidence, such as the target documenting it (`mcp.registration.rejudge_identity`).
  Copies of one server share that evidence and the credential slots of the copy
  that works (`inherit_credential_slots`). A working entry kept in the claude CLI
  is written to `.mcp.json` too, portably (`record_kept_entries`).
- **"Try again later" while identifying settles nothing.** If the address answers
  `429`/`5xx` while its product, logins and APIs are being worked out, it is
  asked again (`mcp.discovery.identify_retries`). While still undecided, nothing
  unproven settles it (`undecided_keeps_searching`), and the report says to re-run
  later.
- **A replacement must prove more.** An existing entry that passed part of its job
  stays over one that passed less and proved no login
  (`mcp.registration.keep_more_proven_existing`). When every login you gave is
  refused alike, the report says so once, with what to check.
- **The job comes from what the address is.** These are the tasks of its
  recognised product and every API kind detected for it
  (`mcp.contract.address_job`). A server counted as working that cannot be asked
  them settles nothing, and "works without a login" needs a passed task, not a
  public call like `/about` (`public_needs_the_job`).
- **What you already export is used.** Variables named for the address
  (`BUILD_EXAMPLE_NET_…`) are read by name and shape, never printed
  (`[mcp.discovery.auth.inventory]`). They are offered to a server waiting for a
  login and, once accepted, the entry reads them. A slot taking
  `Authorization:Basic <base64 of user:password>` gets your encoded variable, or
  is built at start-up from your user name and password by a small composer
  (`[mcp.registration.compose]`). A login you export for another address of the
  same organisation is tried once (single sign-on).
- **Logins by evidence.** `api.{domain}` counts only when it is the same product.
  A method a challenge doesn't name is tried with a stand-in nobody owns, and
  methods you hold credentials for come first. A refusal from a token other
  hosts' entries send is reported as `foreign_credential`, not as this address's
  verdict.
- **Claude Code's view counts.** Entries the claude CLI holds are read from
  `~/.claude.json`, because `claude mcp get` hides `${NAME:-default}`. An entry
  `claude mcp list` reports connected is replaced only by a server that proved
  the job. CLI copies are removed scope by scope, and a first start slower than
  `MCP_TIMEOUT` is reported (`[mcp.registration.client_timeout]`).
- **One server, one entry, in every mode** (`mcp.registration.fold_copies`):
  the same package or URL reaching the same hosts, launched with other flags,
  is a copy and is folded into the best-proved one. Different servers are never
  folded; how many an address keeps is still the default's,
  `--exhaustive-mcp`'s or `--the-best-mcp`'s.
- After an entry is written, **Claude Code itself uses it**
  (`mcp.registration.check_through_claude`, default on,
  `[mcp.registration.claude_check]`): a `claude -p` session is given exactly
  that entry and asked to call one task's tool.
  - The tool's own answer, read from the session's events, is judged; the
    model's summary never is.
  - Variables the entry needs are checked first, with no model request: "export
    it in the shell you start Claude Code from, then restart Claude Code".
  - A check that can't run is inconclusive and never fails an entry.

**A login is proved by a call that fails without it.**
- **Each login check is repeated with no credential at all**
  (`mcp.verification.login_check.compare_with_anonymous`). A success anybody
  gets proves nothing; build-example-net "worked" for days on `get_about`. A refusal
  identical to an anonymous caller's is reported as *the credential may never
  have been sent*, and the remedy (`remedies.same_as_anonymous`) checks how it's
  sent before asking for a new one. The generated bridge's own direct check
  does the same.
- **A credential under a name the server never reads is flagged.** The rule
  (`flag_unread_credentials`) applies when the server's source, the script or
  the installed package (`[mcp.env_contract] read_packages`,
  `package_sources`, `package_read_patterns`), never mentions the name and reads
  a credential under another one. `mcp-openapi-proxy` reads `API_KEY` and
  `EXTRA_HEADERS`, not `BUILD_EXAMPLE_NET_BASE64_OF_USER_PASSWORD`.
- **A header-name variable must hold a header name.**
  `MCP_BRIDGE_AUTH_HEADER=Bearer` sent a header *named* Bearer
  (`value_shapes`, `forbidden`).
- **Stand-ins never sit beside a real user name.** Identity parts are replaced
  too (`[mcp.packages.credential_probe] identity_patterns`,
  `placeholder_user`), and a credential refused
  `max_refused_logins_per_credential` times in a run isn't tried again, so the
  run's own checks never lock an account.
- **The same login sent to two services is reported.** Values are compared by
  fingerprint, including the parts of a Basic credential
  (`mcp.registration.compare_credential_values`).

**The request's form is tried before the account is blamed.** A failure
answered as a web page is the gateway's, not the API's
(`gateway_page_patterns`) -- and with a real login it may be the *request*
the gateway rejects. build.opensuse.org's bridge sent `Accept:
application/xml, text/xml;q=0.9, */*;q=0.1`: every logged-in call got the
gateway's HTML 404 page, no login and a made-up one got 401, and the same calls
with `Accept: */*` returned the data. The account was fine. So when a
logged-in call is answered by a web page with one of
`mcp.verification.request_forms.statuses` (404, 406, 415), each request-form
variable the launch reads (`request_forms.variables`, `MCP_BRIDGE_ACCEPT` →
`*/*`) is tried, one at a time, and the first under which the login is
accepted is written into the entry and reported.

**Only then is a login with nothing behind it an account problem.** When
every call still fails alike with the credential, the calls are repeated with
a stand-in. If the stand-in is refused or answered differently, the login was
recognised and the service has no account behind it: *no API access*, with
its own remedy (sign in once to the service's web interface, or ask its
administrators; `mcp.verification.login_check.compare_with_standin`,
`remedies.no_account`). A `404` *in the API's own form* from a tool that reads
the account's own record means the same (`identity_tool_patterns`); a `404`
web page never does. A server every call of which fails (at least
`failed_calls_min` calls) is not working, however well it started
(`failed_calls_unverify`).

sd.example.com needed all of these. mcp-atlassian answered `[]` with a working
token and with a dead one alike. In agent mode, the agent's own bridge (Basic
auth guesses, undeclared reads) was refused and ended the bridge stage, the
report blamed the token, and the bridge generated from the documented
operations was never built.

**A working entry keeps its place.** An existing entry that answered during
the run is not overwritten by a candidate the run did not prove: one only
waiting for a credential, whose login was never accepted. The candidate is held
back and reported with how to prove it (`mcp.registration.keep_answering_existing`,
default `true`). An entry the run proved unusable (it does not start, or its
account has no API access) is still replaced, and so is one with the same launch.

**An account that may not use one API may use another.** sd.example.com's token
was valid -- its Service Desk API answered it -- but the account is a
service-desk *customer*, so every call mcp-atlassian makes to the core Jira API
was sent to the customer portal. Three things now follow from that verdict
(`no API access`, see the login check):

1. **The search goes on.** A server whose account has no API access does not
   settle the tier descent the way a credential wall does
   (`mcp.verification.login_check.no_access_settles = false`); a rejected
   credential still does, because a new token is its fix.
2. **The API the account can use is bridged.** `jira_service_management`
   recognises Jira Service Management's customer API at
   `/rest/servicedeskapi/info`, which answers without a login, and lists its
   read-only operations -- `get_my_requests`, `get_request`,
   `get_request_comments`, … -- so the bridge stage generates a bridge for it.
3. **It is proved with the token already in use for that host.** The bridge
   asks for its own variable (`SD_EXAMPLE_COM_TOKEN`); the token this machine
   already sends sd.example.com is tried on it, in memory, with the one read-only
   call (`mcp.verification.login_check.borrow_in_use`), and the verdict says
   which value to export. The bridge then takes the unusable server's slot, and
   its name: an existing CLI registration proved unusable is replaced, since
   the CLI never re-adds a name it knows and would otherwise shadow the file.

A version string that does not look like one is not pinned to. To switch
pinning off, set `mcp.packages.pin_versions = false`. A new kind (OData, gRPC
reflection, JSON-RPC) is a new table; nothing in the code names one.

## A missing toolchain is installed, not held against the candidate

A local candidate launched with `go run …` on a machine without Go used to be
rejected as `go … not installed` — true, and avoidable, since phase 1's catalog
knows how to install Go here. `--mcp-provision-missing` (setting
`mcp.packages.provision_missing`) lets the verifier install it first, in both
agentic and `--no-agent` runs:

| Mode | What happens when a launch command or install step is missing |
| --- | --- |
| `auto` (default) | its catalog component is installed, skipping any strategy that needs root unless this process is root or `sudo -n`/doas answers without a prompt |
| `always` | every catalog strategy may run, prompting for a password if it must |
| `never` | nothing is installed; the candidate is rejected, as before this existed |

What it does not do matters as much:

* **Picking is unchanged.** Candidates are found, scored and ranked exactly as
  before; the agent's prompts do not mention it. Provisioning happens only at
  the moment a chosen candidate is trialled.
* **Only catalog strategies run.** The program is mapped to a catalog component
  (the same way prefetching does); a command a candidate wrote never causes an
  install by itself. A program no component provides is left missing.
* **The verdict text is unchanged.** If the install fails, the candidate is
  rejected in exactly the words it always was, so retries and the ranking cannot
  tell the difference. What was tried, and why it failed, goes to the report.
* **Once per toolchain per run.** The result is shared by every address and both
  pipelines; under `--concurrency` a second address needing Go waits for the
  first one's install and takes its result.
* **Declined, with a reason,** under `--dry-run`, when `--install-skip` names the
  component, when its entry says `on_demand = false` (or
  `install.on_demand_default` is false), or once the `--timeout` deadline has
  passed.

Anything installed is recorded in `env.json`/`env.sh` as phase 1 would record it,
and the report gains one `toolchains` step listing what was installed, what could
not be (with the attempts and the `run --phases 1 --install-only <component>`
command that installs it by hand), and which registered servers depend on which
toolchain. The step is never a failure, so it cannot change the phase's verdict.

## A missing credential must not decide which package wins

A stdio server reads its credential when a **tool is called**, not at start-up.
So one trialled without that credential still completes the handshake and still
advertises everything it can do — `confluence-datacenter-mcp` lists 115
`confluence_*` tools with no token set at all.

This program used to refuse to launch such a package, which meant the correct
candidate was disqualified for honestly declaring that it wants
`CONFLUENCE_API_TOKEN`, while an irrelevant package that declared no secrets
started, handshaked, and was registered in its place. A candidate punished for
describing its own requirements is a scoring bug.

Now it is trialled anyway (`mcp.packages.trial_without_secrets`) and reported
for exactly what it proved:

```
  connected  yes   atlassian-confluence-mcp 0.5.0 over stdio, protocol 2025-06-18
  tools      yes   115 tool(s) -- listed without a credential
  two-way    yes   tools/list answered after the handshake
  usable     -     not yet: CONFLUENCE_API_TOKEN must be set before any tool call will work
```

It is registered **provisionally**, with the slot empty, exactly like an
endpoint that answered `401`, and is never counted as working.

**And only when it actually listed something.** The whole reason that trial is
worth running is the `tools yes 115 tool(s)` line: a server kept waiting for a
token still says what it can do. When the listing comes back *empty*, that
justification is gone — nothing was learned, and nothing points at the
credential as the reason.

`mcp-atlassian` for `confluence.example.com` handshaked, advertised zero tools, and
was filed as "a real MCP endpoint that refused an anonymous client". That made
it provisional, which settled the target, which closed tiers 3 and 4 — and the
entry named `PERSONAL_TOKEN`, a variable that package does not read. The
operator set it and still had no tools, because the credential was never what
was missing.

An empty listing has several causes and a missing token is only one: a variable
named wrongly, a base URL the server rejected, a required argument nobody
passed. So it is now reported as *started, exposed nothing, cause unknown*
(`mcp.packages.credential_wall_needs_tools`), which lets the verification rule
say the true thing — "server exposes no tools, resources or prompts" — and lets
the lower tiers have their turn.

## …but a server that refuses to *start* is allowed to say why

The rule above handles the package that starts. There is a second one that does
not, and `did it answer?` cannot tell its two cases apart:

- `@ivotoby/openapi-mcp-server` exited `rc=1` inside `ToolsManager.initialize`.
  That is breakage with no bearing on the credential, and filing it as a wall
  shipped an entry whose `${…}` nobody can fill — the process dies before it
  reads the variable.
- A server that prints `GITLAB_TOKEN is required` and exits has read its
  configuration, found the variable absent and declined. That is the strongest
  statement available that the credential *is* the missing piece.

Treating every such exit as breakage is what lost `gitlab.example.net`. The bridge
stage had already wrapped a server in a launcher, pinned `GITLAB_URL` at the
target, pointed TLS at a self-contained CA bundle and verified it end-to-end;
the trial without `GITLAB_TOKEN` was recorded as a plain failure, nothing
downstream could recover a good server from that verdict, and the target
finished with no entry at all.

What separates them is the standard applied everywhere else here — evidence
from the server rather than inference about it, the same requirement that stops
an intranet's blanket `401` from counting as an endpoint. Either the message
**names a variable that was withheld**, which needs no configuration because
the names come from the candidate, or it matches a wording in
`mcp.packages.startup_credential.markers`. Markers are the weaker rule and are
deliberately narrow: one that also appears in ordinary breakage would turn
every crash back into a credential wall.

Such a server is registered **provisionally** with the slot empty, exactly like
one that answered `401`, and is never counted as working — `connected` stays
`no`, because it never handshaked and nothing here pretends otherwise. The
`${VAR}` slot is *kept*, which is the point: the server has just said it needs
it. Setting `mcp.packages.startup_credential.enabled = false` restores the
older behaviour of calling every failed trial a plain failure.

This verdict is only as good as the text it reads, and two bugs used to destroy
exactly that text. A trialled server's stderr was capped at 40 lines by refusing
to append past the cap, which keeps the **head** — and a traceback puts the only
line naming a cause at the very end, so a startup chain longer than forty lines
lost its exception before anything could read it. What survived was summarised
as the last three lines joined oldest-first, which takes the tail of the head: a
frame from the middle. GitLab's bridge really died on `ValueError: GitLab token
is required. Set one of: GITLAB_TOKEN, …` and was logged as
`user_lifespan_result = await stack.enter_async_context(self._…`. The buffer now
keeps the **last** 200 lines and the summary leads with the **exception line**,
so shortening it for a console cannot lose it. And an install step
that is prose rather than a command is dropped where it is parsed, instead of
reaching the point where word zero of a sentence is looked up as a program and
reported as `prerequisite needs the, which is not installed`.

## Addresses are canonicalised before anything is probed

`--mcp-server` takes a comma-separated list and can be repeated, so
`--mcp-server example.com,localhost:3000` and `--mcp-server example.com
--mcp-server localhost:3000` are the same run. Each entry is written however you
have it — every line below is accepted, and the right-hand column is what phase 4
actually probes:

| Written | Probed | Why |
| --- | --- | --- |
| `example.com` | `https://example.com` | https is assumed off a local network |
| `localhost:3000`, `192.168.1.5:8080`, `myserver` | `http://localhost:3000`, … | TLS is the exception on a private host, not the rule |
| `HTTPS://Example.COM/` | `https://example.com` | the host is case-insensitive, the trailing slash is not a path |
| `https://example.com:443/mcp/` | `https://example.com/mcp` | the default port is implied by the scheme |
| `api.example.com/mcp` | `https://api.example.com/mcp` | a supplied path is tried first, before any guessed one |
| `https:/example.com` | `https://example.com` | a slash lost in a paste is not a different server |
| `münchen.de` | `https://xn--mnchen-3ya.de` | the wire wants punycode |
| `[::1]:8080`, `::1` | `http://[::1]:8080`, `http://[::1]` | IPv6 literals, bracketed or not |
| `https://example.com/a?token=x#top` | `https://example.com/a` | the query and fragment are dropped — discovery appends its own paths |
| `https://user:pw@example.com` | `https://example.com` | **credentials in an address are discarded**, never logged or registered; use `mcp.discovery.auth.bearer_token_ref` |
| `ftp://example.com` | *refused* | discovery speaks http and https, and says so rather than guessing |

Text can only go so far, so the canonical URL is then *confirmed* with one cheap
request: a redirect is the server telling you its own address, and following it
means `example.com` is probed as `https://www.example.com` if that is what it
really is, instead of following the same redirect on every one of the hundred-odd
candidate paths. The step is reported before discovery starts:

```
✔ [   OK   ] address example.com   https://www.example.com (no scheme given; assuming https://; https://example.com redirects to https://www.example.com)
```

Confirmation is an improvement, never a requirement. A host that does not answer
at its root is still discovered exactly as written — an MCP server on `:8080`
behind a closed port 443 is ordinary — and the only correction taken from a
redirect is the origin, because a landing page's own path is no better a base to
append `/mcp` to than the one you gave. One asymmetry is deliberate: when the
address does not answer at all, the other scheme is tried, but http is only ever
*added* for a plausibly local host. Upgrading http to https is free; quietly
downgrading a public address to cleartext because TLS failed would send every
later probe, and any configured bearer token, in the clear. Set
`mcp.address.resolve = false` to skip the request entirely (air-gapped hosts), and
see `[mcp.address]` in `initialize_claude_ai/data/40-mcp.toml` for the timeout.

## Tell it which zones are yours

`--mcp-server` takes **any** address — a vendor's public endpoint, a container on
`localhost:3000`, an appliance on an internal zone nothing has ever heard of —
and four decisions turn on the one question *is this host on our own network?*:

| | local | public |
| --- | --- | --- |
| A bare name defaults to | `http://` | `https://` |
| A configured bearer token over cleartext is | sent | withheld |
| Cleartext in the ranking is | `local network, no TLS` | `−0.6 cleartext HTTP` |
| `mcp.discovery.local_ports` (3000, 8080, …) are | probed | not probed |

Loopback, RFC 1918, link-local, single-label names, `.local`, `.internal`, `.lan`
and `.home.arpa` are recognised. Those are conventions, not a fact about your
network, so if your internal zone is `.corp`, `.intra` or a registered domain
served split-horizon, say so:

```bash
export INITIALIZE_CLAUDE_AI_MCP_LOCAL_SUFFIXES=.corp,.intra,acme-internal.net
```

or `mcp.address.local_suffixes` in a config file. It is additive — `.corp` as
well as loopback, not instead of it — and `local_suffixes_replace = true` drops
the built-in *suffixes* for the rare host where one of them is somebody else's
public domain (loopback stays: it is this machine on anybody's network).
Nothing is inferred from your resolver: guessing wrong in the *public*
direction costs one withheld token and a ranking note, guessing wrong the other
way puts a credential on a wire that is not yours.

## Discovery is agentic, and the agent keeps working

There is nothing to choose for it to work. Every stage is a Claude subagent
driven by a skill in `initialize_claude_ai/skills/`, and that agent owns the
stage until it settles it or genuinely runs out of ways to try.

**A disappointing first answer is not a reason to stop being an agent.** Most
first answers fail for a correctable reason — a package name that does not
exist, a transport the server does not speak, a launch command that needed a
shell — and the agent is the thing best placed to correct them, provided
somebody tells it what happened. So each stage is worked up to
`mcp.agent.max_attempts` times (3 by default,
`INITIALIZE_CLAUDE_AI_AGENT_MAX_ATTEMPTS`), and every retry is *different* from
the attempt before it:

| What failed | What changes next time |
| --- | --- |
| the answer did not verify | the agent is handed each candidate that did not complete a handshake and its error, each candidate that was discarded before it was tried, and an instruction not to send them back unchanged |
| the runtime itself | the other runtime takes over — a defect in the SDK is not a defect in agent mode, and the CLI drives the same models |
| overload or a rate limit | the same request again, because the failure was about this moment rather than this request |
| the request was rejected before the model read it | the brief gets **shorter** (see below), and the run rotates to another model |
| the clock ran out | the brief gets shorter *and* the turn budget is cut, so the next attempt aims at an answer it can finish |

```
  - endpoint: delegating to mcp-endpoint-scout (attempt 1 of 3, sdk)
  - endpoint: attempt 1 of 3 did not settle it (the agent proposed 2 candidate(s); none
      completed a handshake -- https://example.com/mcp: HTTP 404); retrying with what went wrong
  - endpoint: delegating to mcp-endpoint-scout (attempt 2 of 3, sdk)
```

The reason is the verifier's, not the agent's. "None completed a handshake" on
its own does not say whether the URL was wrong, the host was down or the
transport was not the one being spoken — and that is the difference between a
retry worth making and three identical ones.

Retrying cannot lose ground: the furthest-progressed attempt is the one kept, so
a second try that comes back empty never erases a first one that found
something. An endpoint that answered 401 outranks any number of candidates that
did not answer at all. How hard the agent worked is on the record — `attempts`,
`attempt_history`, `retry_changes` and `prompt_register` in the stage's
findings.

**A retry has to be able to find something the last attempt did not.** Working a
stage until it is settled is the point; working one that is already settled is
just time. So a stage stops as soon as an endpoint has *answered*, and an
endpoint that answered `401` or `403` has answered: it is the target's own MCP
server proving it is there, it is
[written out with its credential slot left empty](#an-endpoint-that-wants-a-credential-is-written-out-with-the-slot-left-empty),
and the only thing another attempt can turn up is the same URL refusing the
same anonymous client. The stage still reports `PARTIAL`, because no handshake
completed and nothing here pretends one did — settled is not the same as
working.

| Setting | Env | Default | What it does |
| --- | --- | --- | --- |
| `mcp.agent.credential_wall_settles_stage` | `INITIALIZE_CLAUDE_AI_AGENT_CREDENTIAL_WALL_SETTLES` | `true` | `false` spends the remaining attempts looking for an endpoint that needs no credential at all |

## A model credential that stops working stops the agents once, not per stage

`smelt.example.net` lost its agents part-way through a run: the Google Cloud
credential behind Vertex expired, and every agent call answered "Could not load
Google Cloud credentials". No pattern knew that sentence, so each stage retried
it three times, for every target after it, and the advice was to raise
`mcp.agent.max_attempts`.

Each credential is now a profile, `[mcp.agent.credential_failures.<name>]`,
with `markers` (the wordings that mean this credential is the problem), a
non-interactive `refresh` command, and the `remedy` an operator runs.
`google_cloud` and `aws` ship; another provider is one table. When a profile
recognises a failure:

1. its refresh is tried **once per run** (`mcp.agent.credential_refresh`,
   `credential_refresh_timeout_seconds`). If that works, the stage retries
   without spending an attempt;
2. if it does not work, agents stop for the rest of the run, on every model.
   The remaining stages run deterministically, and each is recorded as
   `agent_unavailable`;
3. the final verdict says the agent stopped part-way, not that it never ran.
   It prints the remedy and the command that re-runs exactly the addresses that
   lost it:

```
  initialize-claude-ai run --phases 4 --mcp-server a.example.com,b.example.com
```

<a id="prompt-registers"></a>

## A retry after an unread failure makes the brief shorter, not longer

Two of those failures have something in common: the model never read the brief.
A request rejected before delivery and a request that ran past the clock both
end without anyone having considered the question — and the obvious retry,
appending a paragraph that explains the work, makes both of them worse. Every
word that produced the ending is still there, now with more text around it, and
a three-attempt loop produces the same ending three times. That is what a log
full of `[fallback]` is: not an agent that tried three things and failed, an
agent that was never reached, three times.

So the brief is built at one of three **registers**, and an unread failure moves
the next attempt one rung *down*:

| Register | What the brief carries |
| --- | --- |
| `full` | everything: the objective, the skill, the tier rules with the conventional path list spelled out, and what earlier stages found pasted inline |
| `plain` | the same work, said shortly. Earlier findings stay in the workspace context file for the agent to read; the path list becomes a sentence |
| `minimal` | the target, what to establish, the turn budget, and the shape of the answer |

Every rung asks for the same work and enforces the same rules — the hard rules
about credentials, verification and read-only access are in all three, because a
shorter brief that is also a laxer one is not worth having, and the tier gate is
enforced in code either way. What shortens is the surrounding material.

| Setting | Env | Default | What it does |
| --- | --- | --- | --- |
| `mcp.agent.prompt_register` | `INITIALIZE_CLAUDE_AI_AGENT_PROMPT_REGISTER` | `full` | which rung the *first* attempt starts at. Set it to `plain` on a deployment where the full brief is always rejected, and stop paying for the attempt that rediscovers that |
| `mcp.agent.max_list_items` | — | `8` | longest list pasted into a brief verbatim. Past this the agent gets a count, this many examples, and the file holding the rest |
| `mcp.agent.max_prompt_findings_chars` | — | `4000` | ceiling on the whole earlier-findings block in a brief |
| `mcp.agent.timeout_turn_scale` | — | `0.5` | what the turn budget is multiplied by after the clock runs out |

That last pair matters more than it looks. One endpoint stage records every path
it asked about — a hundred and sixty URLs on a large target — and pasting that
list into the *next* stage's brief spends the context window on data nobody will
act on. The count and three examples say the same thing in one line, and the
full list is still in the report and in the workspace context file.

**Rotating the model.** "Change your model" is the other remedy a rejection
names, and it used to be unavailable on a host where nobody had filled in
`mcp.agent.retry_models` — which is every host, because that list is empty by
default and naming a model there would pin a model name into the program. It is
now derived instead: phase 3 already resolved one real model id per tier against
the live backend and exported each under that backend's
`[provision.*.env_names]` `model_tier` variable, so the rotation reads those
back, in `mcp.agent.retry_model_tiers` order (`sonnet`, `haiku`, `opus`). Your
own `retry_models` still wins when you set one; an empty `retry_model_tiers`
turns the derivation off and leaves the shorter brief as the only remedy.

**The deterministic pipeline is the last resort, not the second option.** It
runs when agents cannot run at all (no runtime, a dry run, `--no-agent`), when
no agent owns a stage, or when every attempt above has been spent — and then it
says so, naming the attempt count. Set
`mcp.agent.fallback_to_traditional = false` and even that goes away: an
exhausted agent stage reports its own verdict instead. Anything the agent did
find is carried into the deterministic pass rather than thrown away.

Because that decision is made per stage at run time, the terminal says which
path each stage actually took, in its own colour:

| Tag | Meaning |
| --- | --- |
| `[agentic]` (magenta) | a Claude subagent drove the stage and its candidates were verified |
| `[traditional]` (blue) | deterministic probing and rule-based reasoning, **start to finish** — no agent involved. This is the only one `--no-agent` can select |
| `[fallback]` (amber) | internally `agentic+traditional`: the agent worked the stage, spent every attempt without settling it, and the deterministic pass finished the job |
| *(blank)* | the stage **never ran** — the tier gate or the descent rule skipped it. No mode is true of work nobody did, so the column is empty and the detail says which rule applied |

The blank matters, and it was not always blank. A skip used to carry a mode,
and because the descent rule lives on the deterministic pipeline — shared, so
one rule governs both — an *agentic* run's skips were stamped `[traditional]`.
A real log then showed five `[traditional]` lines for a run in which the agent
had driven every stage it executed, which reads as "the agent gave up here" and
is the opposite of what happened. `[fallback]` is also easy to misread: it is
not "the agent was skipped", it is "the agent was there and came up short".

```
✔ [   OK   ] discovery mode  [agentic]  every stage starts as a Claude subagent via the claude CLI, ...
✔ [   OK   ] https://example.com :: endpoint  [fallback]  1 candidate verified
  - why the agent did not settle it: the request was rejected before a model read it: ...
```

**A fallback says why.** The tag on its own tells you the agent did not finish
and nothing about what to do, and the ways a stage can end have different
answers — so the reason goes on the page under the stage line, into
`agentic_inconclusive` in the JSON report, and into a "Next steps" line naming
the setting that addresses it: `retry_models` for a rejection,
`timeout_seconds` for the clock, "worth simply running again" for an overloaded
service, `max_attempts` for anything else.

**And one of them is not about the stage at all.** If the runtime cannot serve
*any* request — the model is not enabled on this deployment, the credential is
refused, the quota is gone — then no attempt, on any stage, for any address, can
succeed. That class (`unusable`) stops the retry loop immediately, latches for
the run so no later stage pays for the same answer, and is reported **once**, in
the final verdict, as `THE AGENT NEVER RAN`.

It exists because its absence cost a whole run. `claude-opus-5-5` was not
enabled on the operator's Vertex project, so every call returned `404` carrying
its own fix — *"Try --model to switch to claude-opus-5"*. The wording matched no
class (the transient wordings match **unavailable**; the text said **not available**),
so it was retried three times a stage across thirteen addresses, every one
degraded to the deterministic pipeline in silence, and no `.mcp.json` was
written. The remedy appeared twenty-nine times in the log and never once as a
conclusion — and never legibly either, because the capture took the *last* 600
characters of a JSON result object and so began mid-key. Failure messages are
now parsed for their message field before anything truncates them.

The wordings of every class are settings, `[mcp.agent.failure_patterns]`
(`unusable`, `refusal`, `timeout`, `transient` — regular expressions, tested in
that order), so a runtime that words its failures differently is a TOML change.
`mcp.agent.unusable_markers` and the credential-failure markers add plain
wordings to `unusable` on top. The free retry for a transient failure asks the
same classifier, markers included, so a failure an operator has marked unusable
is never retried for free.

A `discovery mode` step at the top says how the run expects to proceed, each
stage line is tagged with what it did, the summary table gains a `ran as`
column, and the closing box repeats the tally (`stages ran: agentic 3,
fallback 1, skipped 5`) for when the per-stage lines have scrolled away. The
same values are in the JSON report under `modes` and on each stage's `mode`.
Skipped stages are counted under their own heading rather than by mode: the
tally exists to say how much the agent did, and work nobody did is not part of
that answer.

**A rejected request is counted separately**, because it is the one agent
failure that costs the stage outright — the other three mean the agent worked
and came up short, which the fallback then completes, while a rejection means
no agent worked at all:

```
2 agent request(s) were rejected before a model read them, so those stages fell
back without the agent having worked on them -- set mcp.agent.retry_models to
give the retry somewhere else to go
```

It is in the JSON report as `agent_refusals`. Previously this was visible only
by grepping the log for a stack trace.

**`--no-agent` is the way to choose traditional mode — and it is meant to be the
only one.** The last-resort fallback covers the case where an agent *cannot*
run; it cannot decide that one *must not* — a policy about cost, repeatability,
or what is allowed to start on this host. So that is a flag, and it is a real
one: the deterministic pipeline runs directly,
no runtime is looked for, no workspace is made, and no turn is ever spent.

```bash
initialize-claude-ai run --phases 4 --mcp-server example.com --no-agent
```

```
│ Discovery     traditional (--no-agent) -- deterministic probing and          │
│               rule-based reasoning for every stage; no Claude subagent is    │
│               started                                                        │
...
✔ [   OK   ] discovery mode  [traditional]  --no-agent: every stage runs deterministically -- probing and rule-based reasoning only, no subagent is started
```

The banner says it before the phase goes quiet, the `discovery mode` step says
it again, and every stage line carries `[traditional]`. The phase does the same
work and reports the same way — it just will not reason about a target it cannot
probe, so a server whose endpoint is somewhere convention would not put it is
likelier to come back unfound. `INITIALIZE_CLAUDE_AI_MCP_AGENTIC=false` is the
same decision for a whole machine or a CI job.

**Every rule about what may be registered applies identically in both modes**,
and that is deliberate rather than incidental: traditional mode is the path that
runs when there is no agent to supervise it, so it cannot be the one with the
weaker guarantees. The checks live in shared modules, not in the agentic
pipeline:

| Rule | Where it lives |
| --- | --- |
| the tier order and the four-step checklist | `policy.py` |
| what counts as evidence in a `401`, and the control probe | `probes.py` |
| an entry must name a service, and the right one | `grounding.py` |
| an entry must outlive this machine | `portability.py` |
| a refusal keeps the entry out of the file | `writer.py` |
| no TLS-weakening variable in an entry | `writer.py` |
| credential naming, shape, and the empty slot | `writer.py` |
| a package must have something to do with the target | `registry_client.py` |
| a substring is not a name; a publisher's account is not the vendor | `grounding.py` (`text_names`, `registry_owner`) |
| one endpoint claimed for unrelated targets belongs to none of them | `writer.py` |
| a claim never outweighs what the server says it is; a catalog is nobody's server | `grounding.py` (`identity_doubt`) |
| a trust-store file an entry names must reach a self-signed root, judged the way its runtime reads it | `trust_store.py` (`chain_complete`, `runtime_store`), `mcp_auto_discovery/trust.py` (`check_named_bundles`), `writer.py` (`bundle_check`, `trust_refusal`) |
| an existing registration proven wrong is retired, not kept | `traditional.py` (`note_stale`), `writer.py` (`retire`) |
| a credential never decides which candidate wins | `verifier.py` |
| one target, one answer | `traditional.py`, called by both |
| a generated bridge asks for the credential the API uses, by named parts | `bridge_auth.py` (`method_from_spec`, `method_from_challenge`), `bridge.py` (`auth_method`, `_declare_credential`, `_check_login`) |
| a server's tool definitions must be ones the client can load | `probes.py` (`_check_tool_schema`, `schema_refusal`), `verifier.py` (`schema` check), `bridge.py` (`_client_safe`) |
| a credential never hides as the default of a `${NAME:-…}` reference | `writer.py` (`_without_secret_defaults`) |
| an entry must start before it settles a stage — waiting for a credential or not; a refusal hands over to the next proven candidate | `traditional.py` (`heal_or_discard`), `writer.py` (`start_backups`, `_keep_existing`), `healing.py` (`standins`, `launch_difference`) |
| **what counts as a credential at all** | `secret_policy.py` |

The last one was the exception that proved the rule. Those primitives used to
live in `agentic.py`, so they judged what a *subagent* said; the deterministic
pipeline answered the same question by reading a package registry's own
`isSecret` flag. A registry entry declaring
`{"name": "Authorization", "value": "Bearer sk-live-…", "isSecret": false}`
would have had that value written verbatim into `.mcp.json`, while the identical
header from an agent was dropped. Both now import one rule, and an outside
flag may only make the program more careful — `true` is believed, `false` is
checked.

What is genuinely agent-only is only what needs an agent: turn budgets
(`--agent-max-turns`), attempt counts (`--agent-max-attempts`), the prompt
register ladder, and the retry plan.

**A working agent and a wedged one look identical, so neither is left to guess
at.** A stage delegates to a subagent and then has nothing to report until that
agent finishes, which against a large target is routinely many minutes. Two
things bound that:

```
  - package: delegating to mcp-package-scout (sdk)
      package: still working (0m30s)
      package: using WebSearch: github mcp server        <- --set mcp.agent.verbose=true
      package: still working (1m00s)
```

- A heartbeat every `mcp.agent.progress_interval_seconds` (30 by default; 0, or
  `console.progress = false`, turns it off). With `mcp.agent.verbose = true`
  each tool call the agent makes is named too, redacted like every other line.
- A wall-clock ceiling, `mcp.agent.timeout_seconds` (900 by default; 0 disables
  it), enforced by both runtimes. A turn cap is not a time cap — one turn can
  spend ten minutes in a web search — so without it a stage has no upper bound
  at all. When the ceiling fires, whatever the agent had already said is kept
  and the attempt counts as a transient failure — the same request goes out
  again, and only an exhausted stage reaches the deterministic pass.

**Nothing is accepted on an agent's say-so.** A candidate becomes a result only
after completing a real JSON-RPC `initialize` handshake:

```
✔ [   OK   ] endpoint  https://mcp.deepwiki.com/mcp -- DeepWiki 2.14.3 via streamable-http, protocol 2025-11-25, 3 tool(s)
```

That extends to the commands a tier-2 candidate says have to run first. Each
prerequisite is executed on its own, as argv, with no shell, so a step carrying
a pipe, a redirection or half a sentence is not a step that fails — it is a step
that was never runnable, and it is dropped before anything runs, noted against
the candidate by command name only. What survives is resolved on the `PATH`
immediately before it runs (per step, because one step is often what installs
the next), and a step that does fail is reported with what the command actually
said, so the report distinguishes a candidate that is broken from one we
mis-ran.

## Verification: connected, callable, and answering both ways

An `initialize` that returns is the cheapest thing an endpoint can do. Before
anything is written, every endpoint has to pass all of these, and the run prints
the result of each one per endpoint:

| Check | What it proves | How |
| --- | --- | --- |
| `connected` | there is an MCP server there | `initialize` negotiated a protocol version, over the transport named |
| `tools` | Claude Code will have something to call | `tools/list` (and `resources`/`prompts` where advertised) returned at least one entry |
| `two-way` | the session carries traffic in both directions | a request sent *after* the handshake, on the session it opened, came back with the id this client chose — `tools/list` where there is one, otherwise the specification's own `ping` |
| `usable` | it can be *called*, not just reached | `-` where a server handshaked but named a variable that has to hold a value before any tool call works. Not a failure — a state |
| `configured` | the settings it parses are in a form it accepts | every tool the check called answered without a setting error ("misconfigured" -- an address with a scheme where a bare host is wanted). A failing one makes each address-valued variable be tried in other forms (`mcp.verification.address_forms`) until the same tool accepts one, which is kept and written. Read tools nobody could supply arguments for are named as **not exercised** |
| `authenticated` | its service **accepts the credential** | with every credential variable holding a real value, one read-only tool is called on the session — annotated `readOnlyHint`, or named like `get_me`, `get_server_info`, `list_projects`, never one that changes anything — and the answer judged: `yes` accepted, `no` rejected or no API access, `-` not proved or not asked. `mcp.verification.login_check` |
| `starts` | the **entry**, as written, comes up | the finished `.mcp.json` entry is launched with its `${VAR}` placeholders resolved the way Claude Code resolves them. `-` for a remote entry, which starts no process |
| `claude` | Claude Code itself can reach it | `claude mcp get <name>` after registration, which connects exactly as the next session will |

**An empty answer is not access, and a refusal outranks acceptance.**
sd.example.com's customer account got `[]` from mcp-atlassian's
`jira_get_all_projects` while every issue lookup went to the portal. Taken as
"accepted", that would have made mcp-atlassian *verified* and settled the
address on a server the account cannot use. A structurally empty answer is now
"answered with nothing"; a "rejected" or "no API access" from any call outranks
"accepted" from another; and when access stays unproved while the target
serves another API kind with documented operations, the search goes on, the
alternative is bridged and checked, and the one with proven access takes the
slot. With nothing to check with at all, the report names the alternative so
the first run already says what to do, and a replacement entry's briefing
lists its tools beside the ones it replaces.

**...but one operation's 403 is not the account's.** A call accepted *with content* proves the credential. A NO_ACCESS on another operation that carries no account-wide sign (a redirect to a portal, login or sign-on page, `login_check.account_wide_markers`) is reported as a permission limit on that operation, not as "the account has NO API ACCESS" (`login_check.content_outranks_scoped_refusal`). Whether the server does its job is still the usage contract's verdict. confluence.example.com's search returned the operator's page and its usage task passed, while user search answered 403. A refused credential (401) still outranks everything, and sd.example.com's portal redirect still outranks acceptance.

**A listing is not a login.** sd.example.com's `uvx mcp-atlassian` started,
listed 49 tools and answered `ping` — and every tool call then failed with
`500 Server Error … /servicedesk/customer/portals`: the instance sent the
request to its customer portal because it did not accept the caller as a Jira
user. `authenticated` is the check that sees it. A server whose credential is
refused (`rejected`) or whose account is sent away from the API (`no API
access`) is registered as **waiting for a working credential** — written with
its slot, never counted as working, the remedy named — rather than as working
(`mcp.verification.login_check.mode = "pending"`; `"warn"` only reports it,
`"off"` calls nothing, `INITIALIZE_CLAUDE_AI_MCP_LOGIN_CHECK`).

The first three judge the *candidate*; `starts` judges the *artefact*. They are
not the same object — the candidate is trialled with the environment discovery
assembled, the entry ships with placeholders that may resolve to nothing — and
three servers passed all of the first three and then died on start-up in the gap
between them.

`claude` judges neither: it asks the **real client**.

`starts` and `claude` used to disagree, and the disagreement had a cause worth
naming: our launch inherited `os.environ`, while Claude Code passes the entry's
own `env` plus a narrow passthrough and nothing else. A check more permissive
than the thing it predicts cannot predict it — so an entry could pass here on a
variable from the operator's shell, be written, and fail there. That is exactly
how two dead openQA entries shipped while the run reported `PARTIAL`.

So `starts` now launches the entry the way the client does
(`mcp.verification.client_env`), and **only an entry that starts is accepted**.

<a id="healing-an-entry-that-will-not-start"></a>

## Healing an entry that will not start

*Where* this happens is the design. It first ran at publish, after every stage
had finished — too late for either useful thing: the agent's turn was over, and
the tier descent had already happened, so a candidate whose entry would never
start still occupied its target's answer and tiers 3 and 4 were never asked.

It runs inside the stage loop now, beside the registrability check, where *"we
found a server"* and *"we can write this server down"* were already asked
together. The order of remedies mirrors the tier policy — the better-informed
party first:

1. **An agent**, when there is one — the strategist, briefed with the entry,
   the failure, how the proven launch differs from it and what discovery knew
   about the server, so it can change an argument, a variable, an interpreter
   or a file a bridge generated. It does **not** get to say whether the fix
   worked: whatever it returns is launched again and kept only if it starts,
   and the candidate then carries that launch, so the entry written at publish
   is the repaired one.
2. **The deterministic repairs**, which need no model and are all `--no-agent`
   has. Each is one known difference between our launch and the client's:

   | repair | the difference it closes |
   | --- | --- |
   | `declare_inherited_env` | a variable our trial inherited and the entry never declared — by far the commonest |
   | `absolute_command` | a bare command resolved through *our* `PATH`, not the client's |
   | `absolute_args` | a relative path resolved against *our* working directory |
   | `withdraw_optional_reads` | a `${NAME:-}` slot, for a server that treats "set but empty" unlike "unset" |
   | `isolate_user_config` | the server reads the operator's own configuration (a dotfile under `HOME` / `XDG_CONFIG_HOME`) before its entry's, and that configuration breaks it: the entry gets a configuration scope of its own (`[mcp.healing.isolation]`) |
   | `restore_path_defaults` | the entry names this program's own path variables (`ICA_MCP_*`) without their `${NAME:-default}`; one whose file is not on this machine is reported as copied from another machine instead |

   **A server can read the operator's configuration before its entry's.**
   osc-mcp takes its API address from the operator's oscrc ahead of
   `OSC_MCP_API`, and refuses any example.net address by design. On a machine set
   up for build.example.net, every launch for build.opensuse.org died with "failed
   to get credentials" -- with a stand-in credential as without one -- and the
   address finished with nothing. Now a start-up failure that a stand-in
   credential does not change is not a credential wall
   (`mcp.packages.credential_probe.volatile_patterns` keeps timestamps from
   making two identical failures differ); the server is launched once more with
   a configuration scope of its own, and if that starts it, or brings it to ask
   for its credential, the candidate keeps the scope, the entry writes it
   relocatably, and the report says the operator's configuration was the cause.
   The operator's files are never read for values and never edited. Existing
   entries naming this program's own variables (`${ICA_MCP_PYTHON}`) are
   resolved as this program defines them, and a candidate the start gate
   discarded is still the best lead, ahead of a URL that answered 404.

   **A credential is recognised by context and by use, never written into the
   file, and checked against its slot.** build.example.net's entry was written with
   its login token as the default of `API_HEADERS`: the name has no credential
   word and the hex token did not look random by an absolute measure. Now a
   header-list or basic-auth name carries a credential by its shape
   (`carrier_patterns`), randomness is judged against the value's own alphabet,
   and a value equal to a credential an existing entry uses is a credential
   under any name -- and the writer turns any literal credential left in an
   entry into a `${…}` reference as its last step. A known value is held up
   against its slot's shape (`value_shapes`): a bare token where Name:value
   header pairs are read is reported with the exact form. And a registration
   that does not start is replaced in the claude CLI too -- "already known to
   the CLI" no longer leaves the broken copy loading in place of the file's.

3. **Neither** — the candidate is dropped, everything tried is recorded, the
   stage does not settle, and **the lower tiers get their turn**. That tier
   fallback is not arranged; it falls out of asking the question at the right
   moment.

Every repair is re-proved before the next is tried, so one that did not help is
discarded rather than accumulated and whatever finally ships was launched in
exactly that form. An entry that needed a repair is still a good entry; the
point is that the change was verified.

The writer asks the same question once more at publish, through the same
`EntryDoctor`, because the file is written from *that* answer — one
implementation, so the stage and the file cannot disagree
(`mcp.healing`, `mcp.registration.require_entry_starts`).

**A server waiting for a credential is asked too.** It settles its target just
as a working one does, so the stage launches its entry as written before it
may — with the stand-in credential its trial used in the slot
(`mcp.packages.credential_probe.placeholder`), so the entry is judged as it will
run once the value is exported, and the stand-in is scrubbed from whatever the
server says. Candidates are examined best first until the address's quota
(`mcp.discovery.auth.max_provisional`) has passed; one that fails hands its place
to the next, and if none starts the stage does not settle and the lower tiers
run (`mcp.healing.examine_pending`). confluence.example.com lost its server
without this: both of its candidates were waiting for a token, neither was
launched as written until publish, and the one chosen there died with
`FileNotFoundError: [Errno 2] No such file or directory | ) | ^` while the
other was never tried.

**At publish, a refusal hands over rather than ending the address.** If an
entry is still refused there because it does not start, the next candidate this
run proved for the address — working ones first, then ones waiting for a
credential, in tier order — is tried under the same name
(`mcp.registration.start_fallbacks`). And if nothing at all can be written, a
registration the machine already holds that answered during the run is kept as
it was and said so — `<target> :: keep <name>`, `kept_existing` in the report,
`mcp_kept_existing` in the phase data — so a run never reads as having left an
address empty while Claude Code still has a server for it.

**A failure says how the two launches differ.** The trial inherits this run's
environment and runs in the install sandbox; the client passes only the entry's
`env` and `mcp.verification.client_env.passthrough`, from its own directory. So
an entry that cannot be made to start is reported with that gap, by name only:
variables the proven launch set that the entry does not declare, variables it
inherited that each name a file a client would not pass, and the directory each
ran from (`mcp.healing.report_differences`). The agent repairing it is told the
same. The server's own error keeps the frame that raised it — the
`File "…", line N` and the call beneath it — and drops lines that are only
layout (`^^^^`, `)`), which is what had crowded out the useful line.

**When nothing can repair it, the run says which files it depended on.** A
bridge is a script plus the data it reads, and the commonest way one breaks is
that something regenerated half of it. The evidence is in the filesystem — a
script modified today beside a manifest from yesterday — and nobody looks,
because nothing says to look. So a failure lists the host files with their size
and age, taken from the entry *and from the server's own complaint* (a bridge
locates its manifest itself, so the entry never names it), and calls out a set
that sits in one directory with timestamps that disagree:

```
· /home/…/mcp_bridge_openqa_example_net.py -- 11337 bytes, modified 2026-09-23 14:53
· /home/…/openqa_example_net.operations.json -- 16987 bytes, modified 2026-09-22 17:11
· in /home/…: mcp_bridge_openqa_example_net.py, openqa_example_net.operations.json were
  last written 21 hours apart. Files that sit together and are generated
  together should not; one of them is probably stale
```

Only files that live *together* are compared — an interpreter from last year
beside a CA bundle from last month is an ordinary machine, not a stale pair
(`mcp.healing.report_files`, `…files_agree_within_seconds`).

**If it still cannot be written, the file is not silently left as it was.** An
entry of that name may already exist from an earlier run; it is kept, because
this run has nothing better to put there — and that is said out loud, because
silence would read as "the file is as this run left it" while the operator is in
fact looking at an entry nothing verified today.

```
  name                           endpoint                      connected  tools  two-way  usable  authenticated  configured  starts  claude
  -----------------------------  ----------------------------  ---------  -----  -------  ------  -------------  ----------  ------  ------
  mcp-deepwiki-com-mcp           https://mcp.deepwiki.com/mcp  yes        yes    yes      -       -              -           -       yes
  mcp-deepwiki-com-mcp-devin-ai  https://mcp.devin.ai/mcp      yes        yes    yes      -       -              -           -       yes
    - mcp-deepwiki-com-mcp -- DeepWiki 2.14.3 over streamable-http, protocol 2025-11-25
      - tools: 3 tool(s)
      - two-way: tools/list was sent after the handshake and answered
      - claude: Claude Code reports: connected
✔ [   OK   ] https://mcp.deepwiki.com :: verification  2/2 endpoint(s) are connected, expose something callable and answered a request after the handshake
```

The first three are requirements — `require_initialize`, `allow_empty_tools`
and `require_round_trip` in `[mcp.verification]`, each of which can be relaxed.
The fourth is reported but never required: a machine without the `claude` CLI is
not evidence against a server, so it shows as `-` rather than a failure. The
whole set runs again at the end of the phase (`final_recheck`), so a server that
has gone away between being found and being written is not written. The re-check
asks what proved each server -- the usage tasks that passed, nothing new
(`recheck_proved_tasks`) -- and says "no longer answers" only when it doesn't.
One that answers but no longer does its job is said so, one that now waits for a
credential is kept waiting, and an address left with nothing is handed over
([how](#handover)).

## What is registered has to survive this machine

`.mcp.json` exists to be **copied** — that is why the briefing was taken out of
it. Verification is still per machine: start-up time, where credentials are
kept and which paths exist all belong to the machine. After copying the file,
run `initialize-claude-ai run --phases 4 --mcp-recheck all` on the new machine.
It proves every entry there, moves any credential pasted into the file to Claude
Code's settings env, and raises Claude Code's start-up wait where a server
needs it. Discovery can undo that in one move by writing an absolute path into an
entry, and it did: a bridge was once registered as

```json
"command": "…/work/agent-workspace/artifacts/openqa-mcp-bridge/venv/bin/python3"
```

which handshaked perfectly when it was verified, and on another machine was
`ENOENT: no such file or directory` with nothing to explain it. Verification
proved the command worked *at that instant*; nothing asked whether it would
still work afterwards, or anywhere else.

Every entry is now graded before it is written:

| Grade | What it looks like | What happens |
| --- | --- | --- |
| **portable** | a URL, or a launcher that fetches what it needs — `npx -y`, `uvx`, `uv tool run`, `docker run` | registered, and preferred in the ranking |
| **host-bound** | a real file on this machine: a generated bridge, a system interpreter | registered, and the briefing says plainly that copying the file will not carry it |
| **disposable** | a path inside a virtualenv built to trial a package, inside the trial sandbox, or simply not there | **refused**, with the reason and the launcher form to use instead |

The line between the last two is concrete rather than a judgement: a virtualenv
is identifiable (an ancestor holding `pyvenv.cfg`), and the sandbox is a
configured path. A `.py` file an agent wrote is a real artifact and is kept; the
venv it pip-installed a package into is not.

Being a venv is not the same as being a trial venv. A venv at or under one of
`mcp.registration.durable_venv_roots` is kept on purpose, so a path inside it is
**host-bound**. As shipped, those roots are phase 1's own venv
(`install.venv_path`), pipx's venvs, `uv tool`'s tools, and
`$INITIALIZE_CLAUDE_AI_MCP_DURABLE_VENV_ROOT` when set. The venv this program
runs from counts too, unless `mcp.registration.trust_running_venv = false`.
Without this, a host whose Python is externally managed (PEP 668) lost its
generated bridges. Phase 1 had installed into its own venv, so the bridge
interpreter was inside it, and the entry was refused. The trial sandbox is
always checked first and still wins.

Both refusals, grounding and portability, are also asked **before a stage
settles**, in both agent and traditional modes. An entry that would be refused
does not close the tiers beneath it, and the agent is told why while it still
has attempts left.

**A bridge is a directory, and it finds its own parts.** A generated bridge is
three files that are useless apart — the script, its operations manifest, and
any CA chain. Naming all three in the entry made it describe this machine's
directory layout, and said nothing about the files having to travel at all. The
script now resolves its companions against its own `__file__`, so the entry
names one path:

```json
"command": "${ICA_MCP_PYTHON:-/usr/bin/python3}",
"args": ["${ICA_MCP_ARTIFACTS:-/root/.local/state/…/artifacts}/openqa-example-net-bridge/bridge.py"],
"env": { "MCP_BRIDGE_BASE_URL": "https://openqa.example.net" }
```

`MCP_BRIDGE_MANIFEST` and `MCP_BRIDGE_CA_BUNDLE` still win when set, and a
bundle that is *not* in the bridge directory — a pinned file, the system trust
store — is still named, because nothing could find it otherwise. Moving a bridge
is: copy the directory, export one variable. A companion file that is missing
exits with a sentence on stderr naming what it looked for, rather than an
`ENOENT` inside `json.load` that reaches the operator as `Connection closed`.

**And the pair has to prove it is a pair.** The script and its manifest are
generated together and are only valid together, which nothing used to check. An
openQA bridge was regenerated while its manifest was not; the older manifest
held `path_params` as a list where the newer script called `.items()` on it, and
the bridge died inside `tools/list` with `AttributeError: 'list' object has no
attribute 'items'`. Both files were valid. The *pairing* was wrong, and the
pairing was the one thing nothing recorded.

So the manifest now carries the generation that wrote it
(`{"schema": 1, "script": "mcp_bridge_<slug>.py"}`), a mismatch is refused by
name, and the operation fields the script will index are validated before
anything uses them — reporting the operation and the field rather than the line
that happened to touch it first. A manifest with no stamp predates the check and
is still accepted, because every bridge already on disk is one of those.

Underneath both is one rule: **a traceback is not an error message here.**
Anything that escapes a stdio server reaches the client as a process that
exited, and the client says `Connection closed` — the same three words for a
missing file, a bad manifest and a syntax error. The generated bridge's entry
point is wrapped so the last thing on stderr is always one sentence with a cause
in it. The bridge-authoring skill requires the same of an agent-written bridge,
which is where this one came from.

**Every absolute path in an entry is named by variable, so it can move.** Not
only the ones this run owns — that limit is exactly what kept letting paths
through. Four separate reports were needed to arrive at the general rule, each
naming a kind of path nobody had covered:

| Reported | The path | Why nothing caught it |
| --- | --- | --- |
| bridge bundle | `…/artifacts/…/mcp_bridge.py` | covered — a configured root |
| wrong interpreter | `/usr/bin/python3` | pinned, then given `ICA_MCP_PYTHON` |
| `failed · Connection closed` | `/root/.local/bin/uvx` | a launcher binary: under no root, not the interpreter |
| still not portable | `OPENQA_VERIFY=/home/…/chain.pem` | a third-party package's own file: under no root, matching no binary |

Enumerating kinds of path is a losing game — the next entry holds a kind nobody
enumerated. So the rule is now *every* absolute path, and the only remaining
question is what to call the variable:

1. **A configured root** it sits under — `${ICA_MCP_ARTIFACTS:-…}/rest`, so a
   bundle of files moves by one export rather than one each.
2. **A binary we know** — the interpreter, or a launcher from
   `[mcp.packages.launchers]`: `${ICA_MCP_UVX:-/home/linuxbrew/.linuxbrew/bin/uvx}`.
3. **The environment variable holding it** —
   `"MCP_BRIDGE_CA_BUNDLE": "${MCP_BRIDGE_CA_BUNDLE:-/etc/ssl/ca-bundle.pem}"`.
   The product already chose that name and documented it; nothing has to be
   invented.
4. **Its own basename**, prefixed — `${ICA_MCP_HELPER_TOOL:-/opt/vendor/helper-tool}`.

This is safe to apply broadly because an unset `${VAR:-default}` yields exactly
the original string, so even a value that merely *looks* like a path is
unchanged in effect. The earlier, narrower rule argued that a variable per
system file would be noise; the evidence went the other way, and
`/etc/ssl/ca-bundle.pem` — the example it was argued from — is openSUSE's
spelling, not Debian's. An entry with four variables can be moved; an entry
with four absolute paths cannot, without first working out which files matter.

The briefing lists every one of them by name, read back out of the entries
rather than recomputed, because a variable an operator must export is no use if
nothing tells them it exists.

Claude Code expands that form in `command`, `args` and `env` — verified against
the installed CLI before it was adopted: unset, the default applies and the
server connects; set to a relocated copy, it connects from there; set to a path
that does not exist, it fails. So the entry is unchanged in effect where it was
written, and moving it is *copy the directory, export one variable* — which the
briefing prints by name.

The roots are configuration (`[mcp.registration.path_variables]`, most specific
first) and an empty table restores plain absolute paths. Only paths under a root
we own are rewritten: `/etc/ssl/ca-bundle.pem` is the system's and exists on the
next machine too. The interpreter is the one exception, because unlike a system
file it is a *choice this program made* — pinned absolute so a bare `python3`
cannot resolve to a different trust store — and `${ICA_MCP_PYTHON:-…}` keeps the
pin while letting another host say where its own lives.

`mcp.ranking.penalties.host_bound` (0.3) is what makes `uvx openqa-mcp` win over
a path into our own state directory — the same server, with nothing to rot.
`mcp.registration.require_paths_exist` turns the existence check off.

A **search path** is not a file. A value of absolute directories joined by the
platform's list separator (`PATH=/usr/bin:/bin`) is a list of places to look,
and a directory missing from it is skipped by whoever searches it. Read whole,
it was "a file that is not there", and openSUSE's own `osc-mcp` was refused for
build.opensuse.org twice before it was tried. Each directory is still checked
for scratch space and trial virtualenvs, and one under a configured root moves
with it; a missing one never refuses the entry.

<a id="an-entry-has-to-name-the-server-it-talks-to"></a>

## Every variable a server reads is in its `env` block

An entry is a contract with whoever copies it: *this is everything the server
needs*. The writer used to honour half of it — it wrote the variables a
candidate **declared**, and a candidate declares what its author remembered. The
openQA bridge read six variables and its entry named one. Its manifest and CA
bundle were found by a fallback in the source
(`os.environ.get("MCP_BRIDGE_MANIFEST") or os.path.join(HERE, …)`); on another
machine the directory also held an older manifest under a near-identical name,
the bridge opened that one, rejected its schema and exited, and Claude Code
reported `Connection closed`. Nothing in the entry said a manifest existed,
where it was, or which variable chose it.

So the variables are now read from the source. For every file the entry
launches that is text on this disk, `[mcp.env_contract]` finds the reads — one
regex per idiom (Python, JavaScript, Ruby, Go, Rust, shell), with a `name` and
an optional `default` group — and each one is written into `env` with **the
value the script would have used anyway**:

| The script says | The entry says |
| --- | --- |
| `os.environ.get("MCP_BRIDGE_TIMEOUT", "30")` | `"${OPENQA_EXAMPLE_NET_MCP_BRIDGE_TIMEOUT:-30}"` |
| `… or os.path.join(HERE, "x.operations.json")` | `"${OPENQA_EXAMPLE_NET_MCP_BRIDGE_MANIFEST:-/abs/x.operations.json}"`, or the configured root form |
| `os.environ.get("MCP_BRIDGE_API_KEY")` | `"${OPENQA_EXAMPLE_NET_MCP_BRIDGE_API_KEY:-}"` |

The key is what the script reads and never changes; the variable it expands is
namespaced by server, because two bridges that both read `MCP_BRIDGE_MANIFEST`
must not share one export. Unset, every value is exactly what the script used
on its own, so nothing changes on the machine that wrote it; on any other, each
setting is visible and can be changed by exporting one variable. The briefing
lists them per server under `settings_in_its_env_block`.

Two forms are avoided on purpose, and both were checked against Claude Code
rather than assumed. A bare `${NAME}` that is unset reaches the server as those
**literal characters** — which is how `mcp-bugzilla` was handed an auth mode
called `${BUGZILLA_AUTH_MODE}` — so an empty slot is always `${NAME:-}`. And a
nested default (`${A:-${B:-x}}`) is not expanded at all, so a path default goes
through the ordinary path rules below instead of being wrapped twice. The same
`${NAME:-}` form now carries a credential the server was proven to start
without, which used to be left out of the entry entirely.

The entry is then launched as written before it is accepted. A server that
treats "set but empty" differently from "unset" fails that check, and the
`withdraw_optional_reads` repair removes exactly the `${NAME:-}` slots and proves
it again. The traditional bridge generator names its manifest and CA bundle
explicitly for the same reason, and leaves the rest to the scanner.

What the scanner cannot see is code it does not read — a package run by `npx` or
`uvx`, a module a script imports. For those the list the agent reports is the
only one there is, and the skills require it to be complete.

## An entry has to name the server it talks to

The only question anyone asks of a line in `.mcp.json` is *is this pointed at my
server?* Three real entries could not answer it:

```json
"confluence-example-com": {"command": "npx", "args": ["-y", "@kubb/mcp"]}
```

`@kubb/mcp` is a TypeScript code generator. It is a real MCP server — it
handshakes, which is why it got this far — and it has no way to reach Confluence
and never did. Nothing in the entry says otherwise, and the *name* proves
nothing, because this program chose the name.

```json
"confluence-example-com": {"type": "http", "url": "https://confluence.example.com"}
```

The right host, and still wrong: that is the site's front door. It reached the
file because the probe URL carried `?format=mcp` — [a query this program
invented](#dont-tidy-what-you-recorded) — which was then tidied away.

```json
"smelt-example-net": {"command": "npx", "args": ["-y", "mcp-graphql@2.0.4"],
                  "env": {"ENDPOINT": "https://smelt.example.net/graphql/"}}
```

The good case. A generic server that says nothing about SMELT in its name and
does not need to: the address is in the environment, where the server reads it.

So every entry must carry an address — an FQDN, a URL or a URI — and that
address must be the target's, **or** one that answered as an MCP endpoint for
the target when we probed it:

```
Believed if BOTH:
  (a) the entry contains an address — in its url, its arguments, or its
      environment; AND
  (b) that address is either the target's own host (or a subdomain), or a
      different host that answered as an MCP endpoint when probed.
```

The floor in (a) is not a separate check — it falls out of the two ways to pass,
since naming the target means its hostname is in the entry and being served
elsewhere means a URL is. It holds by construction rather than by a rule anyone
could forget.

**There is a third way to pass, and it is the strongest: ask the server.** Every
test above reads the *entry*, which is what somebody configured. But by the time
grounding runs, the server has handshaked and described itself — its
`instructions`, its tool and prompt descriptions, its resource URIs, the
defaults in its input schemas — and all of that was being discarded except the
bare names.

`build.opensuse.org` is what it cost. Tier 2 returned `@ateam-ai/mcp`, an npm
keyword-search hit naming no address; it was refused, correctly, and the address
finished the run with nothing. The real Open Build Service server would have
been refused by the same rule for the same reason — no address in the entry —
even though it lists resource URIs under `https://build.opensuse.org/…`. The
entry cannot tell those two apart. The handshake can:

```
Also believed if:
  (c) the running server names the target in what it advertises — a resource
      URI, an instruction, a tool description, a schema default.
```

That is better evidence than (a) or (b), because it is the server saying what it
*does* rather than a claim about what it was configured to do. It sits below
them in the order only because they are cheaper and readable without running
anything.

Two guards keep it honest: it matches by **host**, through the same
`same_service` comparison used everywhere else — so
`build.opensuse.org.evil.test` in a description is not the target — and it reads
only a handshake that **completed**, because evidence from a session that did
not happen is not evidence.

**A different hostname is ordinary, and is believed.** This is where the rule
was first written too strictly and had to be corrected against real hosts:

```
github.com             is served from  api.githubcopilot.com
developers.google.com  is served from  developerknowledge.googleapis.com
```

Both are correct servers, and the first version of this check refused both. A
vendor serving MCP from a sibling domain is the normal shape of a hosted
offering. Such an entry is believed because *we contacted that address and it
replied the way only an MCP endpoint replies* — a handshake, or a `401`/`403`
carrying protocol-shaped evidence. It is then listed in the briefing under
`these_entries_are_served_from_another_host`, so the hop is visible rather than
discovered one day in `claude mcp list`.

**The address does not have to be the target's — but something has to tie it
there.** A hostname cannot decide this, and three successive attempts to make it
decide refused eleven working servers in one run: Google's own endpoint,
GitHub's own, openSUSE's own `osc-mcp`, a hosted Bugzilla.
`api.githubcopilot.com` is a vendor's and `bugzilla.fastmcp.app` is a stranger's,
and nothing in the two names says which.

Four things can tie an address to the target, in descending order of strength:

| | Example |
| --- | --- |
| **the entry names the target** | `--bugzilla-server https://bugzilla.example.com` |
| **the run found that host answering** for the target | the endpoint stage's `401` from `developerknowledge.googleapis.com` |
| **the same organisation** — a shared registrable domain (public suffixes: `mcp.grounding.*`, below) | `api.opensuse.org` serving `build.opensuse.org` |
| **the vendor published it** — its documentation, `.well-known`, or a registry entry for this target | GitHub's docs naming `api.githubcopilot.com` |

A keyword search is *not* one of them. That is how `@kubb/mcp` became
Confluence's server, and it is why `bugzilla.fastmcp.app` — which will serve
**any** Bugzilla and has to be told which — is refused unless the entry says
`bugzilla_url=…`. A multi-tenant host with no instance named is not an answer,
however well it handshakes.

The floor still applies underneath all of it: an entry naming no address at all
is refused however apt its name, `npx -y @modelcontextprotocol/server-github`
for `github.com` included.

**And the check runs early enough to be acted on.** It used to run only at
publish, after every stage had finished. An agent's bridge for
`developers.google.com` verified with three tools and was then refused for
carrying no address — one environment variable's worth of repair, with two
attempts left, and no way for the agent to learn of it. The rule is now applied
when a candidate is ingested as well: an entry that cannot be registered does
not settle its stage, and the reason is handed to the next attempt. The
publish-time check remains the last word, and is what the deterministic pipeline
relies on, but a gate nobody can act on is a veto rather than a check.

This is deliberately a **dumb invariant rather than a judgement**: the
[relevance gate](#a-package-must-have-something-to-do-with-the-target) is where
intelligence is applied, and it is what let `@kubb/mcp` through, so the second
check is one that cannot be reasoned around.

`mcp.registration.require_address_in_entry` turns it off;
`mcp.registration.allow_bare_origin` permits an origin with no path.

**What "refused" means.** Nothing is written and nothing is registered — the
entry is absent from `.mcp.json` rather than present and broken — and the run
says so on its own line, with the reason:

```
• https://confluence.example.com :: refused confluence-example-com   not registered --
  nothing in this entry mentions confluence.example.com: not the command, not the
  arguments, not the environment
```

It is also named in the briefing under `these_were_found_but_not_registered`,
and carried in the `--json` report as `refused`. A refused entry is left out of
the verification table too, since there is nothing to verify.

This is worth stating because for a while it was not true. Both gates set a
reason, skipped the rest of their loop, and then the entry was written anyway:
`publish` passed every registration to the writer, refused or not, and the
registration step overwrote the reason with something about the CLI. The gates
reported decisions they never carried out. The tests were green because they
asserted the *verdict* — `DISPOSABLE`, `UNGROUNDED` — and never that the file
was clean.

<a id="dont-tidy-what-you-recorded"></a>

## Don't tidy what you recorded

`?format=mcp` is one of `mcp.discovery.query_templates` — a string this program
invents and tries against every origin on the chance some product uses it. When
`https://confluence.example.com?format=mcp` answered `401`, writing that URL down
as the server's address would have been circular: the query came from us and
nothing acknowledged it.

So the query was stripped before writing. That was worse. Removing it does not
undo the probe — it invents a **second** URL, one that was never requested, and
records that instead. The entry became `https://confluence.example.com`: untested,
a login page, and unfixable by any credential.

The URL that was probed is now the URL that is written, exactly as probed. The
circularity is dealt with where it belongs — a bare origin is refused unless it
actually handshaked — and the candidate carries a note saying the query was ours,
so nobody mistakes it for the vendor's.

## An endpoint that wants a credential is written out with the slot left empty

A 401 or 403 is the server answering. It exists, it is listening, and it
declined to talk to an anonymous client — which is more than a 404 ever tells
you, and it is the ordinary case for an internal GitLab, a Jira, or any vendor
API worth reaching. Such an endpoint counts as a native endpoint for the
[tier gate](#native-endpoint-first--and-the-lower-tiers-are-gated-on-proving-there-isnt-one)
— whichever pipeline found it, agentic or deterministic, it goes into the same
tier-1 record, so the package and bridge stages do not then go looking for a
replacement for a server that has already been found. What it also gets is an
entry:

```json
"gitlab-example-com": {
  "type": "http",
  "url": "https://gitlab.example.com/api/v4/mcp",
  "headers": { "Authorization": "Bearer ${GITLAB_EXAMPLE_COM_TOKEN}" }
}
```

The credential slot is **empty** — a `${NAME}` placeholder and nothing else,
exactly as for a verified server that needs a token. The point is arithmetic: an
operator who goes and obtains a token then runs one `export`, instead of a
second full discovery run to rediscover an endpoint this run already found.

**For a remote entry.** A local one is different, and the difference matters. A
remote server reads its credential from a header, so the placeholder is the only
way it can arrive. A stdio server is a *process*, and a process launched with an
extra unset variable is running a different command from the one that was
trialled. Two were: `mcp-bugzilla` was proven to start without
`BUGZILLA_AUTH_MODE`, written with `"BUGZILLA_AUTH_MODE": "${BUGZILLA_AUTH_MODE}"`,
and exited during start-up on the operator's machine —
`claude mcp list` said `failed · Connection closed`.

So a stdio entry carries a slot only for a variable the proven launch actually
carried. The rest are named in the briefing, with what each one should hold, and
exporting them and re-running phase 4 trials the server *with* them and writes
the slots it proved. Nothing is lost; the claim just waits for the evidence.

Whatever is written, the finished entry is then **launched exactly as written**
before the run says anything about it (`mcp.registration.verify_as_written`).
Every other gate judges the candidate; this one judges the artefact, and they
are not the same object. So the artefact is written *as the trial ran it*: a
command named bare that the client's `PATH` does not find, but this run
resolved and launched, is written as that path (`${ICA_MCP_UVX:-…}`;
`mcp.registration.resolve_unfound_commands`) -- a name the client finds stays
bare. A trust variable holding the system bundle or one in this run's trust
store reads `${ICA_MCP_CA_BUNDLE:-…}`, never a path into the state directory.
A server that writes to stdout before its handshake is tolerated and said
(`Handshake.stdout_noise`), so a stricter client can be explained.

**How such an endpoint gets found.** The tier-1 agent is told explicitly not to
drop an endpoint that refuses it: it proposes the endpoint with
`"requires_auth": true`, one sentence of `auth_evidence`, and the `secret_env`
slot the writer would choose (`mcp.agent.auth_wall_brief`, which names the
variable, the value's shape and where one comes from). The proposal is then
probed independently. A `2xx` that proves nothing either way (see
[inconclusive replies](#phase-4--mcp-auto-discovery)) is treated like a `401`
with evidence only when someone has a reason to believe the URL wants a
credential. That reason can be the candidate's own claim
(`mcp.discovery.auth.credit_inconclusive_with_claim`) or an auth profile that
matches the host (`…profile_is_auth_claim`). The reason is written into the
evidence in words.

It is never passed off as working. No handshake completed, so the verification
row reads `-` for every check rather than `no` — "no tools" would be a finding
about the server, and the truth is that nobody asked it — `claude mcp get` is
not run against it at all, and the endpoint is counted and reported apart from
the verified ones everywhere: its own `needs token` column in the summary, its
own status line, and an `ACTION REQUIRED` line in the final verdict naming the
variable. The verdict for an address that yielded only this is `PARTIAL`: a real
endpoint was found, and what is missing is a token rather than another search.

The proof stays credential-free — but the status on its own is **not** the
proof. A Jira, a Confluence, a service desk or any intranet answers `401` or
`403` on every URL it owns, including paths that do not exist, so on those hosts
the status says nothing about MCP. Believing it registers the *website* as a
server: an entry no credential can ever make work, which looks like progress and
sends you hunting for a token that would not have helped.

So the rejection also has to have carried something protocol-shaped
(`mcp.discovery.auth.require_mcp_evidence`, on by default). Any **one** of these
is enough:

| Evidence | Setting |
| --- | --- |
| an `Mcp-Session-Id` or `MCP-Protocol-Version` header on the rejection | `evidence_headers` |
| `resource_metadata=` or `mcp` in the `WWW-Authenticate` challenge | `evidence_challenge_markers` |
| a JSON-RPC envelope in the body | `evidence_body_markers` |
| any content type that is not a web page | `disqualifying_content_types` |

Deliberately generous: a bare `401` with `application/json` and no headers still
counts, because discarding a real endpoint is the worse mistake. That is not a
theoretical preference — `openqa.opensuse.org/mcp` answers

```json
{"error":"no api key","error_status":403}
```

with no MCP header, no challenge and no JSON-RPC envelope, and it is a genuine
tier-1 endpoint. Demanding a protocol-shaped refusal would throw it away and
send the run off to build a bridge for a host that already speaks MCP.

**But a login page is a login page in any content type.** The generosity rests
on that last row, *"any content type that is not a web page"* — which is true of
every JSON API and therefore of every JSON error. `confluence.example.com` answers
an anonymous POST with

```json
{"statusCode":401,"message":"You are not logged in. <a href=\"/login.action\">Log in</a>"}
```

and passed on it, and was registered. So phrases that tell a *person* to sign in
— `mcp.discovery.auth.login_page_markers`: `not logged in`, `login.action`,
`<form`, `<a href` — now **overrule** the evidence instead of adding to it. No
MCP endpoint has any reason to say them, and nothing else about the test was
made stricter, so openQA above still passes. When one fires, the report says
which phrase gave it away.

**And there is a control.** Before trusting any refusal, discovery asks the host
a path that *cannot* exist (`mcp.discovery.auth.control_probe`, one request per
origin, cached). If the invented path is refused exactly the way `/mcp` was, the
refusal was the site's habit and carries no information — so the evidence is
discarded whatever it looked like. This is the check that needs to know nothing
about the product on the other end: a list of hosts whose 401s are meaningless
would need an entry for every Jira, Confluence, service desk and intranet ever
deployed, while a control probe works on a host nobody has heard of. It also
catches what the content-type rule cannot — a gateway that answers every path
with the same JSON error.

**The scheme is not copied blindly either.** A challenge naming a scheme in
`mcp.discovery.auth.header_schemes` (`Bearer`, `Basic`, `Token`, `ApiKey`) is
honoured, so a server asking for `Basic` is not sent a bearer token. Anything
else falls back to the configured template with a note. Jira advertises
`WWW-Authenticate: OAuth realm="…"` — that is OAuth 1.0a request signing, and a
Jira personal access token is still a **Bearer** token, so
`Authorization: OAuth <PAT>` would simply be a second `401` to diagnose.

**And the URL that gets written is the server's, not ours.** `?format=mcp` is a
string this program appends while probing (`mcp.discovery.query_templates`).
Recording an origin plus our own query as the endpoint's address is circular, so
for an unverified candidate that decoration is stripped before the entry is
written, and if nothing else distinguished the URL from the bare origin the
report says it was the site's front door. A URL that *verified* keeps exactly
what verified — that is the address which demonstrably worked.

`…__KEY` in these tables abbreviates the setting's full variable, built from its
own dotted path as [Configuration](configuration.md#configuration) describes:
`mcp.healing.repairs` is `INITIALIZE_CLAUDE_AI_MCP__HEALING__REPAIRS`.

| Setting | Env | Default | What it does |
| --- | --- | --- | --- |
| `mcp.discovery.auth.register_authenticated` | `INITIALIZE_CLAUDE_AI_MCP__DISCOVERY__AUTH__REGISTER_AUTHENTICATED` | `true` | off records such an endpoint in the report only |
| `mcp.discovery.auth.token_env_template` | `…__TOKEN_ENV_TEMPLATE` | `{SLUG}_TOKEN` | how the empty slot is named — `{SLUG}` is the server's own name, so two authenticated targets in one run cannot collide |
| `mcp.discovery.auth.profiles.<name>` | — | one entry (openQA) | products whose credential is not a single opaque token — see below |
| `mcp.discovery.auth.name_shapes` | — | `_HEADERS?$`, `_URL$`, … | what a variable's *name* says its value looks like, when nothing was discovered about it and no profile matched. Patterns to sentences, so adding a kind of value is a line of TOML and no product is named in code. `API_HEADERS` wants `Name:value` pairs; described as "may call it an API token", it received a bare token and the server exited on start-up |
| `mcp.discovery.auth.max_provisional` | `…__MAX_PROVISIONAL` | `1` | how many credential-walled endpoints to register **per address**, across every stage that runs. A host entirely behind a login answers `401` on every path probed; one entry for the best-ranked is a lead, a dozen is noise. `--exhaustive-mcp` keeps them all |
| `mcp.discovery.provisional_settles_target` | `INITIALIZE_CLAUDE_AI_MCP_PROVISIONAL_SETTLES_TARGET` | `true` | an endpoint that answered by demanding a credential ends the tier descent, the way a working one does. `false` keeps searching the lower tiers anyway |
| `mcp.discovery.auth.login_page_markers` | `…__LOGIN_PAGE_MARKERS` | `not logged in`, `login.action`, `<form`, … | phrases that mean "a human should sign in here". Finding one **overrules** every other piece of evidence — a login page served as `application/json` is still a login page ([why](#an-endpoint-that-wants-a-credential-is-written-out-with-the-slot-left-empty)) |
| `mcp.registration.require_address_in_entry` | `INITIALIZE_CLAUDE_AI_MCP_REQUIRE_ADDRESS` | `true` | refuse an entry that never names the service it talks to ([why](#an-entry-has-to-name-the-server-it-talks-to)) |
| `mcp.registration.allow_bare_origin` | `…__ALLOW_BARE_ORIGIN` | `false` | permit `https://host` with no path as an HTTP endpoint even though it never handshaked |
| `mcp.registration.refuse_shared_claims` | — | `true` | refuse an entry whose only tie to its target is a claim (grounding basis in `mcp.registration.shared_claim_bases`, `vendor` and `unprobed` by default) when the same endpoint or launch was already accepted for an unrelated address — and withdraw the earlier one from `.mcp.json` and, if this run added it, from `claude mcp` (`mcp.registration.remove_command`). Addresses of one organisation may share an entry; an entry that names its target, or whose server said so, is never affected. One claim for several unrelated services is evidence for none of them |
| `mcp.registration.retire_refused_existing` | — | `true` | remove an entry already in `.mcp.json` or the `claude` CLI, under a name this program gives, when it points at an unrelated host and its own answer names something else. `false` only reports it, with the command to remove it by hand |
| `mcp.registration.identity.enabled` | — | `true` | refuse a candidate on an unrelated host whose own handshake never names the target, whatever claim led to it |
| `mcp.registration.identity.gateway_markers` | — | `marketplace`, `catalog`, `catalogue` | whole words in a server's `instructions` or tool names that mean it fronts other servers; such a server is never a target's on a claim |
| `mcp.registration.identity.accept_unanswered` | — | `true` | leave a candidate that has not answered to the other rules. `false` refuses it on an unrelated host too |
| `mcp.agent.sdk_cli` | `INITIALIZE_CLAUDE_AI_SDK_CLI` | `auto` | which Claude Code the SDK runtime drives: `auto` the installed `claude` when it is at least as new as the SDK's own copy, `installed`, or `bundled` ([why](phase-1-install.md#present-is-not-usable)) |
| `mcp.packages.check_launcher_usable` | `…__CHECK_LAUNCHER_USABLE` | `true` | before a trial, ask the catalog's `usable` checks of the program a server is launched with; one that cannot work here (Docker's daemon stopped) is not launched, and never called broken |
| `mcp.registration.launcher_variable_template` | `…__LAUNCHER_VARIABLE_TEMPLATE` | `ICA_MCP_{LAUNCHER}` | how a launcher binary's absolute path is made overridable. `uvx` found at `/home/linuxbrew/.linuxbrew/bin/uvx` is written `${ICA_MCP_UVX:-/home/linuxbrew/.linuxbrew/bin/uvx}`. Whole path, not the containing directory, matching `interpreter_variable`: one variable means one binary. Empty turns it off |
| `mcp.healing.enabled` | `INITIALIZE_CLAUDE_AI_MCP__HEALING__ENABLED` | `true` | repair an entry that does not start, inside the stage that produced it ([why](#healing-an-entry-that-will-not-start)). `false` turns off the repairs only: the entry is still launched as written and judged |
| `mcp.healing.agent_first` | `…__AGENT_FIRST` | `true` | ask the agent before the rules. It reads the entry, the failure and what discovery knew, and can change more than any one rule; `false` runs the rules first and asks the agent only if they all fail |
| `mcp.healing.agent_enabled` | `…__AGENT_ENABLED` | `true` | let the agent repair at all. `false` leaves the deterministic repairs, which is also what `--no-agent` gets |
| `mcp.healing.report_files` | `…__REPORT_FILES` | `true` | when an entry cannot be made to start, list the host files it depends on with their size and age |
| `mcp.healing.files_agree_within_seconds` | `…__FILES_AGREE_WITHIN_SECONDS` | `3600` | files in one directory written further apart than this are called out as probably stale |
| `mcp.healing.examine_pending` | `…__EXAMINE_PENDING` | `true` | launch the entry of a local server waiting for a credential as written — stand-in in the slot — before it may settle its target, best first, so one that does not start hands its place to the next ([why](#healing-an-entry-that-will-not-start)) |
| `mcp.healing.report_differences` | `…__REPORT_DIFFERENCES` | `true` | when an entry cannot be made to start, name how the proven launch differs from it: variables set or inherited (those naming a file) that the entry lacks, and the directory each ran from. Names only |
| `mcp.healing.isolation.enabled`, `…env`, `…directory` | — | `true`; `HOME`, `XDG_CONFIG_HOME`; `<workspace>/artifacts/isolated-config/{slug}` | relaunch a server that fails at start-up, not for a credential, with a configuration scope of its own; keep it when that helps |
| `mcp.packages.credential_probe.volatile_patterns` | — | timestamps, pids, addresses, `/tmp` paths | removed before a trial with a stand-in credential is compared with one without; identical means the credential is not what it is missing |
| `mcp.agent.placeholder_patterns` | — | `/abs/path`, `path/to/`, `<UPPER CASE>`, `YOUR_…` | text an agent copied from an example; a candidate carrying one is refused and the next attempt is told which |
| `mcp.healing.repairs` | `…__REPAIRS` | `declare_inherited_env`, `absolute_command`, `absolute_args`, `withdraw_optional_reads`, `isolate_user_config`, `restore_path_defaults` | the deterministic repairs, in order. Each is one known difference between our launch and the client's; empty turns the rule-based half off |
| `mcp.verification.client_name_pattern` | `…__CLIENT_NAME_PATTERN` | `^[a-zA-Z0-9_.-]{1,64}$` | what the client accepts as a tool's input property key. One key outside it makes the Anthropic API refuse every request of a Claude Code session once that tool is loaded -- openSUSE's osc-mcp declares `path_repository jsonschema:` |
| `mcp.verification.client_tool_name_pattern` | `…__CLIENT_TOOL_NAME_PATTERN` | empty | the same for tool names; empty because Claude Code rewrites MCP tool names itself |
| `mcp.verification.schema_problems` | `…__SCHEMA_PROBLEMS` | `refuse` | a server whose tools break those rules: `refuse` (not registered, as a server that does not work), `warn` (registered, the tool and key named in the report), `off` |
| `mcp.verification.client_env.strict` | `INITIALIZE_CLAUDE_AI_MCP__VERIFICATION__CLIENT_ENV__STRICT` | `true` | launch a finished entry with only what a client passes (its `env` plus the passthrough below), never this run's whole environment. A candidate's own trial still inherits it, which is what `declare_inherited_env` repairs from |
| `mcp.verification.client_env.passthrough` | `…__PASSTHROUGH` | `PATH`, `HOME`, … | what a client hands a stdio server on top of the entry's own `env`. A server needs `PATH` to find its interpreter; which names those are is a property of the platform |
| `mcp.registration.verify_as_written` | `…__VERIFY_AS_WRITTEN` | `true` | launch each finished entry exactly as written — `${NAME}` and `${NAME:-default}` resolved the way the client resolves them — and report whether it starts. Every other gate judges the candidate the entry came from; they are not the same object, and three servers died in the gap. This check reports; `require_entry_starts` decides whether a failure withholds the entry |
| `mcp.registration.require_entry_starts` | `…__REQUIRE_ENTRY_STARTS` | `true` | refuse an entry that still will not start after every repair — in the stage, where the refusal reopens the lower tiers, and at publish, where `start_fallbacks` hands its place on. `false` keeps the candidate in both, writes it anyway and reports the failure |
| `mcp.registration.start_fallbacks` | `…__START_FALLBACKS` | `2` | when an entry is refused at publish because it does not start, how many other candidates this run proved for the address to try in its place (working first, then waiting for a credential, in tier order). `0` tries none |
| `mcp.registration.keep_answering_existing` | `…__KEEP_ANSWERING_EXISTING` | `true` | an existing entry that answered during the run keeps its place against a candidate the run did not prove (only waiting for a credential, login never accepted); the candidate is held back and reported. An entry proved unusable, or one of the same launch, is still replaced |
| `mcp.registration.replace_unproved_existing` | `…__REPLACE_UNPROVED_EXISTING` | `true` | an existing entry whose access never proved anything (every call empty or unchecked) is replaced, in the file and the claude CLI, when a bridge for the target's other API proved access |
| `mcp.registration.name_undeclared_credentials` | `…__NAME_UNDECLARED_CREDENTIALS` | `true` | credential-like variables a candidate's script reads but does not declare are named to the operator as needed, unless it is proved to work without them or they are already exported |
| `mcp.discovery.generated_bridge_settles_first` | `…__GENERATED_BRIDGE_SETTLES_FIRST` | `true` | the bridge generated from a detected API kind's documented operations is tried before the bridge stage is skipped or ended, unless something already proved access; both modes |
| `mcp.verification.login_check.unconfirmed_rejection_settles` | `…__UNCONFIRMED_REJECTION_SETTLES` | `false` | whether a refusal from a candidate whose way of sending the credential nobody checked (an agent's bridge) ends the descent; a credential accepted by one candidate and refused by another always marks the refusing one broken |
| `mcp.verification.login_check.reuse_variable_names` | `…__REUSE_VARIABLE_NAMES` | `true` | a candidate proved with the credential another entry for the same host reads is written to read that entry's variable — one export per instance |
| `mcp.verification.login_check.compare_with_standin` | `…__COMPARE_WITH_STANDIN` | `true` | when every call fails alike with the credential, repeat them with a stand-in; refused or answered differently means the login was recognised and the service has no account behind it |
| `mcp.verification.login_check.compare_with_anonymous` | `…__COMPARE_WITH_ANONYMOUS` | `true` | repeat each accepted or refused login check with no credential: a success anybody gets proves nothing; an identical refusal may mean the credential was never sent |
| `mcp.verification.login_check.flag_unread_credentials` | `…__FLAG_UNREAD_CREDENTIALS` | `true` | flag a credential variable the server's own source (script or installed package) never mentions while reading a credential under another name |
| `mcp.env_contract.read_packages`, `…max_package_files`, `…package_sources`, `…package_read_patterns` | — | `true`, `400`, uv/pipx and npx caches, language idioms | read the variables an installed package reads, from its own source; nothing is downloaded for it |
| `mcp.packages.credential_probe.identity_patterns`, `…placeholder_user`, `…max_refused_logins_per_credential` | — | user/login/e-mail names, a stand-in user, `2` | stand-ins replace who logs in as well; a credential refused that often in a run is not tried again |
| `mcp.registration.compare_credential_values` | `…__COMPARE_CREDENTIAL_VALUES` | `true` | report the same credential value (parts of a Basic credential included, by fingerprint) sent to different services |
| `mcp.contract.enabled`, `…max_tasks`, `…truth_timeout_seconds` | — | `true`, `4`, `15` | the usage contract: a server is accepted only when its tasks pass |
| `mcp.contract.products.<name>.tasks` | — | Jira, service desk, GraphQL, OBS, GitLab, GitHub, Bugzilla, openQA | each kind's value-free tasks, asked of any server listing the tool |
| `mcp.contract.targets."<host>".checks` | — | none (yours) | your own checks per address, in your configuration |
| `mcp.registration.check_through_claude`, `[mcp.registration.claude_check]` | `…__CHECK_THROUGH_CLAUDE` | `true` | after writing, Claude Code itself uses the entry once; missing variables found first; inconclusive when it cannot run |
| `mcp.known_good.enabled`, `…try_first`, `…file`, `…keep` | — | `true`, `true`, `mcp-known-good.json`, `5` | record entries that did the job; try the last one first; say what changed when nothing works |
| `mcp.discovery.auth.read_challenges`, `…method_order`, `…offered_only_methods`, `…api_host_patterns`, `…challenge_paths` | — | `true`, token→apikey→basic→client, `basic`, `api.{domain}`, `/` | read every login challenge on the target and its API hosts and choose how to log in |
| `mcp.discovery.auth.methods.<scheme>` | — | basic/bearer/token header; signature/negotiate/kerberos/ntlm/digest client | how each challenge scheme is performed |
| `mcp.discovery.products.<name>` | — | OBS, Jira, Confluence, GitLab, Bugzilla, openQA, GitHub | recognition, protected paths, and how the product issues credentials |
| `mcp.ranking.penalties.password_when_token` | — | `0.2` | a password candidate ranks lower when a token can read |
| `mcp.verification.pacing.enabled`, `…min_gap_seconds`, `…max_per_minute` | — | `true`, `0.2`, `40` | calls to one host share this budget, so the run's checks do not get it rate-limited |
| `mcp.verification.login_check.temporary_markers`, `…temporary_statuses`, `…temporary_retry_seconds`, `…temporary_retry_max_seconds` | — | see the file, `[429, 502, 503, 504]`, `5`, `30` | "try again later": asked once more, then inconclusive |
| `mcp.discovery.api_kinds_behind_login`, `…auth.recognise_behind_login`, `…reuse.check_manifests`, `mcp.secret_policy.location_words`, `mcp.verification.public_call_login_markers` | — | see the file | identification behind a login; entry/manifest agreement; locations are not credentials |
| `mcp.env_contract.session_variables` | — | `SSH_AUTH_SOCK`, `KRB5CCNAME`, … | variables the login session sets: written `${NAME:-}` under their own name, never renamed, never asked for |
| `mcp.registration.identity.organisation_needs_tie` | — | `true` | a sibling host of the address's organisation counts only with a tie (generic labels, a product word, or the server naming it) |
| `mcp.discovery.reuse.recheck_when_blocked`, `mcp.discovery.auth.prefer_detected_api` | — | `true`, `true` | a policy-closed tier still re-checks existing entries; with nothing proved, the server for the detected API waits in the slot |
| `mcp.discovery.reuse.keep_operator_references`, `mcp.registration.fold_script_extensions` | — | `true`, `.py`, `.js`, … | an existing entry's `${NAME}` is written back, never its value; one generated script is one server wherever it lives |
| `mcp.discovery.clients_install_missing`, `mcp.discovery.clients.<name>.install` | — | `true`, zypper/dnf/apt-get/pipx | a client a bridge drives is installed before the trial; one that cannot be is broken |
| `mcp.registration.prefer_installed_program` | — | `true` | an agent's workspace build gives way to the installed program when it answers alike |
| `mcp.registration.name_by_role`, `…name_by_role_parts`, `mcp.bridge.generated.auth.account` | — | `true`, `USERNAME`/`PASSWORD`, see the file | login slots named and described for what they hold, and whose account |
| `mcp.verification.login_check.name_other_service_login` | — | `true` | a login refused here and accepted by another organisation's service is named as that one's |
| `mcp.registration.complete_slots`, `mcp.discovery.reuse.repair_missing_slots` | — | `true`, `true` | every entry carries every slot what it launches reads; an entry missing only a slot is repaired and kept |
| `mcp.registration.keep_server_slot_keys`, `…describe_from_manifest` | — | `true`, `true` | a local server's slot keeps the key it reads; a kept bridge is described from its manifest |
| `mcp.registration.read_trial_source` | — | `true` | a slot declared as another variable that holds a value (`env:GITLAB_EXAMPLE_NET_TOKEN`) is written reading that variable -- the one the trial read |
| `mcp.known_good.try_current_too`, `mcp.registration.current_keeps_name`, `…same_address_one_instance` | — | `true`, `true`, `true` | the remembered entry never skips the current one; the current one keeps its name; one address shares its credential variables |
| `mcp.discovery.reuse.refresh_generated_scripts` | — | `true` | an older copy of a generated bridge is refreshed to the current template before its trial |
| `mcp.registration.verify_written`, `…replace_divergent_cli_copies`, `…say_how_to_move` | — | `true`, `true`, `true` | the written file and the CLI copies are read back and repaired; each machine-specific path names the variable that moves it |
| `mcp.verification.login_check.name_other_routes`, `…other_service_counts_kept` | — | `true`, `true` | a refused login names the service's other routes and their setup; kept entries' logins count when comparing services |
| `[provision.fresh_terminal]`, `provision.<backend>.login_env`, `[provision.anthropic_api.key_helpers]` | — | see the file | after phase 3, claude is started as a new terminal would and must log in on its own; login settings go into settings.json; a file/keyring/command API key gets an apiKeyHelper |
| `auth.claude_oauth.long_lived_token`, `auth.google_cloud.impersonate_adc` | `INITIALIZE_CLAUDE_AI_CLAUDE_LONG_LIVED_TOKEN` | `false`, `true` | headless claude.ai sign-in stores a login unless a printed token is asked for; impersonation is written into the credential Claude Code reads |
| `mcp.registration.retrial_rewritten`, `mcp.verification.heal_transport`, `…transport_failure_markers` | — | `true`, `true`, see the file | an entry is judged again after a file it names is rewritten; a connection failure is repaired before the server is judged |
| `run.disk.check`, `…min_free_mb`, `…paths`, `…out_of_space_markers`, `[run.disk.cache_cleanup]` | — | `true`, `1024`, see the file | before each phase every filesystem the run writes to must have room; a full disk stops the run with one verdict and what to free |
| `run.cleanup.enabled`, `…temp_prefixes`, `…require_owner_marker`, `…work_subtrees`, `…work_budget_mb` | `INITIALIZE_CLAUDE_AI_DO_CLEANUP` | `true`, see the file, `true`, see the file, `512` | this program's leftovers removed at the end of a run (`--do-cleanup` / `--no-cleanup`); a temporary directory only when it holds the program's marker, never by name alone |
| `mcp.verification.login_check.missing_credential_markers` | — | `not set`, `no ssh key`, … | a refusal meaning nothing was sent: reported as missing, with what to supply per route |
| `mcp.registration.check_in_file` | — | `true` | every address with a server ends with an entry in `.mcp.json`; anything missing is reported |
| `mcp.registration.transport_aliases`, `…entry_shapes`, `…validate_entries`, `…validate_with_claude`, `…restore_on_failure` | — | see the file, `true`, `true`, `true` | an entry's type is the proved transport; an entry Claude Code refuses is never written; a CLI copy removed for a replacement is put back if the add fails |
| `[mcp.registration.claude_check] connect`, `…waiting_markers`, `mcp.registration.client_timeout.samples`, `…persist_to` | — | `true`, see the file, `3`, `both` | Claude Code connects to each written entry; slow starts are sampled and a wait that fits is set in settings.json |
| `[mcp.registration.load_check]` | — | see the file | written servers Claude Code will not load (invalid, `disabledMcpjsonServers`, unapproved) are named with the file and key |
| `mcp.verification.login_check.refusal_needs_a_sent_credential`, `…job_refused_markers`, `[mcp.verification.delivery]` | — | `true`, see the file, `true` | a verdict needs a sent credential; a refused query is a failure; an optional slot must be read by the server |
| `[mcp.noninteractive]` | — | see the file | phase 4 runs unattended: no desktop for anything it starts, URL openers record instead of opening; a candidate wanting a browser login is a step for you, never a wait |
| `mcp.registration.held_must_be_recordable`, `…repair_missing_entries`, `mcp.verification.no_credential_call_timeout_seconds` | — | `true`, `true`, `20` | an entry kept in place of a replacement is written to `.mcp.json`; the end-of-run check writes an address's answered entry rather than leaving it empty; calls with no credential wait briefly |
| `mcp.discovery.auth.methods_equal`, `mcp.verification.login_check.same_as_anonymous_is_not_a_refusal`, `mcp.verification.align_slots_to_source`, `mcp.registration.connected_is_not_working`, `mcp.discovery.reuse.connected_counts_as_worked`, `[mcp.verification.login_check.user_steps]` | — | `true`, `true`, `true`, `true`, `false`, see the file | no login kind preferred, proof then evidence decides; a credential that may not have arrived is never counted as refused; slots renamed to what the script reads; connected alone holds no place; steps only you can take named in the server's own words |
| `[mcp.registration.fresh_env]`, `mcp.registration.keep_supplied_values`, `[mcp.discovery.auth.history]` | — | see the file, `true`, see the file | checks through Claude Code see only what a new terminal has; values an earlier entry supplied are kept in settings.json env; each host's accepted/refused logins order the next choice |
| `mcp.registration.keep_credentials_in`, `…reshape_entries`, `mcp.discovery.auth.prefer_in_use_methods`, `mcp.discovery.offered_unproved_keeps_searching` | — | `settings`, `true`, `true`, `true` | a supplied credential is kept in Claude Code's settings.json env, never in `.mcp.json`; an entry Claude Code skips for its shape is reshaped; the login plan asks for the credential already in use; an unproven server given that credential settles nothing |
| `[[mcp.discovery.api_kinds.<kind>.adapters]] credential_env`, `mcp.verification.resolve_from_claude_settings` | — | see the file, `true` | an adapter takes the address's held credential the way it declares; trials expand `${NAME}` from Claude Code's settings env too |
| `mcp.discovery.reuse.recheck_when_settled_unproven`, `…worked_before_wins`, `mcp.agent.max_name_length`, `…stop_when_nothing_changes` | — | `true`, `true`, `64`, `true` | an unproven server hides nothing that worked here; agent names and refusals kept sane |
| `mcp.registration.rejudge_identity`, `…inherit_credential_slots`, `…record_kept_entries` | — | `true`, `true`, `true` | belonging re-judged with new evidence; copies share credentials; kept working entries written to the file |
| `mcp.discovery.identify_retries`, `…undecided_keeps_searching`, `mcp.registration.keep_more_proven_existing` | — | `2`, `true`, `true` | a busy address is asked again and not settled unproven; a replacement must prove more |
| `mcp.contract.address_job`, `mcp.verification.login_check.public_needs_the_job` | — | `true`, `true` | the address's product and API kinds name the job; a public call is not it |
| `[mcp.discovery.auth.inventory]`, `mcp.verification.login_check.borrow_held` | — | see the file, `true` | what you already export, by name and shape, offered to waiting servers |
| `mcp.discovery.auth.try_unoffered_methods`, `…api_host_same_product`, `…prefer_held_methods` | — | `true`, `true`, `true` | logins by evidence and by what you hold |
| `mcp.discovery.reuse.claude_config`, `…connected_outranks_trial` | — | `{config_dir}/.claude.json`, `true` | CLI entries as Claude Code keeps them; connected entries are not judged by the run's launch |
| `[mcp.registration.client_timeout]`, `[mcp.registration.compose]`, `mcp.registration.replace_literal_cli_copies` | — | see the file | Claude Code's start-up wait, slots built from parts, literal CLI copies |
| `mcp.registration.fold_copies` | — | `true` | copies of one server (same package/URL, same hosts, other flags) are written once |
| `mcp.repair.enabled`, `…max_variants`, `…header_name`, `[mcp.repair.classes]` | — | `true`, `4`, `Authorization`, see the file | classify each failure and try its repairs in order; each variant is proved again |
| `mcp.discovery.clients.<name>` | — | `osc` | clients a bridge can drive: binary, read command, read-only operations, its own `config` (never this machine's), `isolate`, `logins` per scheme, optional passthrough |
| `mcp.discovery.auth.instance_bound_patterns` | — | token/key names | credentials one server issues; the same one on two servers is reported. A user name and password never is |
| `mcp.recheck`, `mcp.registration.recheck_command` | — | `false`, `initialize-claude-ai run --phases 4 --mcp-recheck {host}` | prove addresses again without searching; how the checklist says to |
| `mcp.verification.judge_existing_as_written` | `…__JUDGE_EXISTING_AS_WRITTEN` | `true` | an existing entry counts as answering only as Claude Code runs it: launched as written, not failing in `claude mcp list` |
| `mcp.verification.login_check.try_without_credential`, `…public_works_without_login` | — | `true`, `true` | a server whose reads work with no credential is working; its credential becomes optional |
| `mcp.registration.optional_unset_settings` | `…__OPTIONAL_UNSET_SETTINGS` | `true` | a setting's `${NAME}` nothing sets is written `${NAME:-}` or left out, never bare |
| `mcp.verification.login_check.identity_tool_patterns` | — | `get_me`, `whoami`, `get_my_account`, … | tools reading the account's own record; a 404 from one means no account for this login |
| `mcp.verification.login_check.gateway_page_patterns` | — | `<!doctype html`, `<html` | a failure answered as a web page is the gateway's, never the API's verdict |
| `mcp.verification.login_check.failed_calls_unverify`, `…failed_calls_min` | — | `true`, `2` | a server every call of which failed (at least that many calls) is not working |
| `mcp.discovery.api_kind_min_shared_tools` | `…__API_KIND_MIN_SHARED_TOOLS` | `2` | tools named as a detected kind's operations that make a bridge that kind's, whoever wrote it |
| `mcp.packages.slow_start_retries`, `…slow_start_timeout_factor` | — | `1`, `2.0` | an existing registration that timed out is tried again with a longer deadline before "does not start" may replace it |
| `mcp.discovery.api_kind_control_name` | `…__API_KIND_CONTROL_NAME` | `initialize-claude-ai-control` | last path segment of the control request sent beside a recognised API-kind path; if that is recognised too, the kind is not. `""` turns the control off |
| `mcp.verification.advertised` | — | see TOML | how much of what a server says about itself is kept as evidence: which listing keys carry the server's own words (`describing_keys`), which JSON-Schema keys do (`schema_keys`), and the bounds on all of it. Third-party output, so it is capped and redacted on the way in |
| `mcp.trust.enabled` | `INITIALIZE_CLAUDE_AI_MCP__TRUST__ENABLED` | `true` | name this host's trust store to every registered stdio server, so a target behind an internal CA is reachable |
| `mcp.trust.bundle` | `INITIALIZE_CLAUDE_AI_MCP_TRUST_BUNDLE` | detected | pin a CA bundle; empty takes the first of `bundle_candidates` that exists |
| `mcp.trust.runtimes` | — | node/python/… | launcher name → the trust-store variables that runtime reads. A runtime nobody has met yet is a line of TOML |
| `mcp.trust.extra_ca` | `INITIALIZE_CLAUDE_AI_MCP_TRUST_EXTRA_CA` | empty | PEM files (comma-separated) trusted for this run on top of the system store; `--mcp-trust-ca` |
| `mcp.trust.check_named_bundles` | `…__CHECK_NAMED_BUNDLES` | `true` | check every trust-store file an entry names against the https hosts it reaches, partial chains off, and replace one that does not reach a self-signed root ([why](#a-certificate-this-host-does-not-trust-is-handled-once-for-the-whole-run)) |
| `mcp.trust.runtime_stores.<VARIABLE>` | — | `NODE_EXTRA_CA_CERTS` → `node -e …rootCertificates…` | for a variable that adds to its runtime's own store, the command that makes the runtime print that store as PEM; the file is judged together with it |
| `mcp.trust.root_required_variables` | `…__ROOT_REQUIRED_VARIABLES` | `NODE_EXTRA_CA_CERTS` | variables whose runtime accepts only a chain ending at a self-signed root; with no bundle that carries one, the entry cannot work |
| `mcp.trust.refuse_incomplete_chain` | `…__REFUSE_INCOMPLETE_CHAIN` | `true` | refuse such an entry, in the stage and at publish; `false` writes it with the reason in its notes |
| `mcp.trust.pin_presented_chain` | `INITIALIZE_CLAUDE_AI_MCP_TRUST_PIN_PRESENTED_CHAIN` | `targets` | learn an untrusted target's presented chain (`targets`) or not (`never`); `--mcp-trust-pin` |
| `mcp.trust.pin_verify_codes` | — | `2, 19, 20` | the OpenSSL verify results a chain may be learned for — a missing issuer or an unknown root. An expired, revoked or wrong-name certificate is never learned |
| `mcp.trust.store_dir` | `INITIALIZE_CLAUDE_AI_MCP_TRUST_DIR` | `<state_dir>/trust` | where learned chains (`anchor_subdir`) and the run's combined bundle (`bundle_filename`) live |
| `mcp.trust.include_certifi`, `mcp.trust.partial_chain` | — | `true` | also trust certifi's roots in probes, and accept a chain that ends at a trusted intermediate (Python only; Node cannot) |
| `mcp.trust.chain_command` | — | `openssl s_client …` | how the presented chain is read when this Python cannot read it itself; `{host}` and `{port}` are filled in |
| `mcp.registration.tls_weakening_env` | `…__TLS_WEAKENING_ENV` | `NODE_TLS_REJECT_UNAUTHORIZED`, `PYTHONHTTPSVERIFY`, … | variables that switch certificate verification off. Never written into an entry: `.mcp.json` is copied between machines, and the remedy for a private CA is the trust store, not a flag |
| `mcp.registration.tls_weakening_patterns` | `…__TLS_WEAKENING_PATTERNS` | `NO_VERIFY`, `SKIP_VERIFY`, `REJECT_UNAUTHORIZED`, `INSECURE` | the same, for names nobody has listed yet: any variable whose name contains one of these is treated as switching verification off |
| `mcp.bridge.generated.accept` | `…__ACCEPT` | `*/*` | the single Accept value a generated bridge sends for an operation whose API description names no single media type (never a weighted list: OBS answered a logged-in multi-value Accept with its 404 page); one that names exactly one is asked for it, a read-only call answered 404/406/415 is retried once with `accept_fallbacks` (`accept_retry_statuses`), and `MCP_BRIDGE_ACCEPT` overrides at launch. build.example.net's bridge asked for JSON alone and Open Build Service, which serves XML, answered every call with 406 |
| `mcp.bridge.generated.auth.from_spec`, `…probe_challenge` | — | `true`, `true` | where a generated bridge's authentication method comes from: the API description's security scheme, else the server's 401 challenge to a documented GET |
| `mcp.bridge.generated.auth.variable_template` | — | `{SLUG}_{PART}` | the variable each part is exported under -- `BUILD_EXAMPLE_NET_USERNAME`; the bridge reads the same name |
| `mcp.bridge.generated.auth.parts.<method>`, `…descriptions.<PART>` | — | basic → `USERNAME`, `PASSWORD`; bearer/token → `TOKEN`; apikey → `API_KEY` | a method's parts, and what each holds in words (`your user name on {host}`) |
| `mcp.bridge.generated.auth.check_login` | — | `true` | when every part is exported, make one authenticated GET and report whether the login was accepted |
| `mcp.packages.github_token_ref` | `INITIALIZE_CLAUDE_AI_GITHUB_TOKEN_REF` | `env:GITHUB_TOKEN` | where the token for the GitHub search comes from, as a [reference](credentials.md#credentials) (`env:NAME`, …), never a value. Optional: it only raises GitHub's anonymous rate limit |
| `mcp.grounding.multi_part_suffixes` | — | `co.uk`, `com.au`, `co.jp`, … | public suffixes longer than one label, so `example.co.uk` is an organisation and `co.uk` is not. Always in force |
| `mcp.grounding.public_suffix_file` | `INITIALIZE_CLAUDE_AI_PUBLIC_SUFFIX_FILE` | `/usr/share/publicsuffix/public_suffix_list.dat` | the full [public suffix list](https://publicsuffix.org) (package `publicsuffix`), added when the file exists; missing or `""` leaves the list above |
| `mcp.grounding.public_suffix_sections` | — | `icann` | which sections of that file count; adding `private` makes two customers of one hosting platform (`alice.github.io`, `bob.github.io`) two organisations |
| `mcp.verification.login_check.mode` | `INITIALIZE_CLAUDE_AI_MCP_LOGIN_CHECK` | `pending` | check a real credential with one read-only tool call; `pending` registers one whose service refused it as waiting for a working credential, `warn` only reports, `off` calls nothing |
| `mcp.verification.login_check.empty_answers_prove_nothing`, `…empty_total_fields`, `…empty_list_fields` | — | `true`; `total`, `size`, `count`, …; `values`, `items`, `results`, … | a successful answer that is structurally empty proves nothing about access -- it is "answered with nothing", never "accepted" |
| `mcp.verification.login_check.unproved_settles` | — | `false` | whether a server whose access stays unproved ends the tier search when the target also serves a bridgeable API kind it does not speak; `false` keeps searching, and a candidate with proven access takes the slot |
| `mcp.instructions.replacement_tools_shown` | — | `8` | when an entry replaces an earlier one of the same name with a different server, how many of each one's tools the briefing names |
| `mcp.secret_policy.carrier_patterns` | — | `(^|_)HEADERS?$`, `(^|_)BASIC(_AUTH)?$` | names whose shape carries a credential, whatever words they use: a header list carries the Authorization header. A value under one is never written as a literal or a `${NAME:-default}` |
| `mcp.discovery.reuse.opaque_value_min_entropy_ratio` | — | `0.85` | a value also counts as a random token when its entropy reaches this share of what its own alphabet allows -- a hex token can never reach 4 bits per character |
| `mcp.discovery.auth.value_shapes.<name pattern>` | — | `_HEADERS` = `Name:value` pairs, `BASIC_AUTH` = `user:password` | the shape a known value must have for its slot; a mismatch is "misconfigured", and the remedy gives the exact form for the service's login method (from its API description, else its 401 challenge -- `mcp.discovery.auth.spec_value_patterns`) |
| `mcp.ranking.penalties.encoded_credential` | — | `0.3` | per credential the operator must encode by hand; a server that asks for the parts by name and builds the header itself ranks above one that needs `Authorization:Basic <base64>` typed in |
| `mcp.agent.kind_aliases` | — | `package`/`local` → `stdio`, `remote`/`http` → `url` | an agent's word for a candidate's kind, normalised instead of discarding the candidate |
| `mcp.verification.login_check.read_verbs` | — | `get`, `list`, `search`, `show`, … | names beginning with one are eligible too: one accepted call proves only its own code path, so up to `max_calls` read tools are asked |
| `mcp.verification.login_check.misconfiguration_markers` | — | "unexpected api address format", "invalid url", "missing protocol scheme", … | a failed answer that means a setting is in a form the server rejects -- the `configured` column |
| `mcp.verification.address_forms.forms`, `…default_scheme`, `…max_variants`, `…enabled` | — | `{scheme}://{host}`, `{host}`, `{scheme}://{host}/`, `{scheme}://{host}{path}`; `https`; `6`; `true` | the forms an address-valued variable is tried in when a call answers "misconfigured"; the first the same tool accepts is kept |
| `mcp.packages.address_fill.description_host_patterns`, `…description_url_patterns` | — | "hostname", "domain" / "URL", "https://" | a registry variable's own description decides whether its address is a bare host or a URL, before its name does |
| `mcp.verification.login_check.tool_patterns`, `…never_patterns`, `…arguments`, `…max_calls` | — | identity, then info, then listings; mutating verbs never; `pattern`/`query`… = `{product}`; `3` | which tool may be called: a server-annotated `readOnlyHint` tool, or a name matching `tool_patterns` and no `never_patterns`, with every required argument fillable from `arguments` |
| `mcp.verification.login_check.check_in_use` | — | `true` | also check the credential an existing entry uses right now (a literal, or the default of an unset `${NAME:-…}`), in memory, with the same read-only call; never written |
| `mcp.verification.login_check.borrow_in_use` | — | `true` | offer the credential existing entries already use for a host to a new server for that host whose one missing credential it may be (same host, one value, the read-only call only) |
| `mcp.verification.login_check.no_access_settles` | — | `false` | whether a server whose account has no API access ends the tier descent; `false` keeps searching for a server for an API the account can use |
| `mcp.verification.login_check.failure_fields`, `…rejected_markers`, `…no_access_markers`, `…remedies.*` | — | see TOML | how an answer is judged — only a failure (JSON-RPC error, `isError`, or a body like `{"success": false}`) is read for markers, so a listing that mentions 4031 is not a 403 — and what the operator is told per verdict |
| `mcp.registration.shared_secret_template` | — | `{SLUG}_{NAME}` | the variable an entry reads when another service's entry already reads the plain name: jira.example.com keeps `JIRA_PERSONAL_TOKEN`, a second instance gets `SD_EXAMPLE_COM_JIRA_PERSONAL_TOKEN`. A credential belongs to one instance; every instruction names the variable the entry reads |
| `mcp.discovery.response_scan_chars` | — | `4000` | how much of a reply's body is searched for markers — credential refusals, protocol hints, authentication evidence; `0` searches all of it |
| `mcp.secret_policy.sensitive_words`, `…selector_words` | — | `key`, `token`, `secret`, … / `mode`, `type`, `scheme`, … | the whole words of a name that make it a credential, and the last words that make it a choice instead (`BUGZILLA_AUTH_MODE`). One rule for both pipelines |
| `mcp.secret_policy.inline_credential_patterns` | — | `Bearer …`, `--token=…`, `?api_key=…` | a credential carried inline in an argument, URL or header value, whatever it is called |
| `mcp.secret_policy.not_a_secret_words`, `…not_a_secret_max_digits` | — | `true`, `false`, …; `10` | values that are configuration however the variable is named (`SESSION_TIMEOUT=30` keeps its value) |
| `mcp.secret_policy.not_opaque_starts`, `…pasted_value_min_chars` | — | `/`, `~`, `$`, `{`, …; `24` | how a random-looking literal that is really configuration starts, and how long a run in a description is taken for a pasted credential |
| `mcp.agent.failure_patterns.<class>` | — | see TOML | the wordings of each agent failure class (`unusable`, `refusal`, `timeout`, `transient`); a class left out keeps the built-in wordings |
| `mcp.protocol.versions` (in a generated bridge) | — | newest first | a generated bridge answers `initialize` in the client's revision when it is one of these, else the newest, and reports this program's version as its own |

**A target behind an internal CA is reachable, without weakening anything.** The
remedy above — "the trust store, not a flag" — used to be advice with nothing
behind it for most entries. `build.example.net` is signed by `Example CA 2023.1`.
The host trusts it, so `curl` fetches its OpenAPI spec with `HTTP 200`; Node
ships its **own** certificate store and does not, so the registered server could
not load the spec, exited during start-up, and Claude Code reported:

```
build-example-net  ·  ✘ failed  ·  Connection closed
```

Nothing in that mentions a certificate, and the diagnosis went to the one
variable in the entry anyone could suspect — `API_HEADERS` — which turned out
never to have been involved.

The argument for naming the host's bundle was already here, for generated
bridges: *it never weakens verification — it is the set of authorities this host
has been told to trust, which is a superset of a vendored one.* That was right,
and it was wired to the one kind of entry this program writes itself. Every
published package it registers, which is most of them, got nothing.

So every stdio entry now carries the variable its runtime reads, defaulting to
the detected bundle:

```json
"env": { "NODE_EXTRA_CA_CERTS": "${NODE_EXTRA_CA_CERTS:-/etc/ssl/ca-bundle.pem}" }
```

`NODE_EXTRA_CA_CERTS` is *additive* — it can add authorities, never remove them.
Three things were measured rather than assumed: with the bundle the fetch
succeeds; an empty value is inert; and an unexpanded `${NODE_EXTRA_CA_CERTS}`
costs one warning on stderr and the server keeps running. That last one is why
the lever is written even where no bundle was found — silence is what made this
cost days — and whatever is written, the entry is then launched exactly as
written, so the claim is checked rather than argued.

## The job is matched by what a tool does; a verified server is never dropped for nothing

- **A usage task's tool is found by name, then by behaviour.** If no tool
  matches by name, the program looks at what a safe read tool says it does
  (the task's `describes`, `mcp.contract.match_descriptions`). Failing that, it
  asks up to `answer_probe_max_tools` safe read tools with no arguments and
  takes one whose answer carries the API's own words (`answer_markers`, e.g.
  GraphQL's `__schema`; `match_answers`). Only tools marked read-only, or named
  with a read verb, are ever asked this way. smelt.example.net's agent-written
  bridge named its introspection tool otherwise: it was "unable to be asked the
  job", settled nothing, and the address got no entry.
- **An address is never left empty while a verified server exists**
  (`mcp.registration.never_empty_when_verified`). One that settled nothing is
  written, and the verdict names it under NOT PROVED with the reason.
- **The agent is told why a verified candidate didn't count.** The reason is
  given so it fixes that instead of starting over.
- **An unknown `kind` word ("bridge") is decided by the candidate's shape**
  (`mcp.agent.kind_from_shape`): a command means stdio, a url means url.
- **Lost roots are found again.** Completing a certificate chain also searches
  this program's own copies: the known-good file copies and the servers' own
  folders (`mcp.trust.issuers_from_program_dirs`).

## An entry's files travel with the file; its credential reaches its script; the verdict reads the file

- **Servers' files live beside `.mcp.json`.** That covers generated bridges,
  manifests and CA bundles (`mcp.registration.artifacts_home`, default
  `mcp-servers`, taken relative to `mcp.mcp_json_path`). Entries name them as
  `${ICA_MCP_SERVERS:-…}`, and phase 1 gives that variable this machine's path
  (`install.machine_paths`). Copying the project carries the servers. Before
  this, a copied `.mcp.json` arrived without its gitlab bridge.
- **The final file check compares every entry's credential slots with the
  script it launches** (`mcp.registration.align_slots_in_file`). This covers
  written, kept and hand-edited entries alike.
  - A slot the script never reads, beside exactly one credential variable it
    does read, is renamed to that variable. Its value goes with it.
  - Any other unread slot is reported: "the credential in X never reaches the
    server".
  - A claude CLI copy that is so affected is replaced by the file's.
  - gitlab-example-net passed `GITLAB_EXAMPLE_NET_TOKEN` to a bridge reading
    `GITLAB_TOKEN`, connected, and never logged in.
- **The verdict counts what the file holds for an address**
  (`mcp.registration.verdict_reads_file`). build-example-net and
  build-opensuse-org were kept, working and in the file, and were reported
  "Not in ./.mcp.json".

## The program's folder may have any name

Clone it as `claude-ai-initialization`, keep it as `claude_ai_initialization`,
or move it: nothing depends on the folder's name.
- Every path inside the folder holding `.mcp.json` is written as
  `${ICA_MCP_PROJECT:-<absolute>}/...`. Phase 1 or phase 4 sets
  `ICA_MCP_PROJECT` to where the folder is now when the written default is not
  there (never for `/` or your home folder, `path_root_never`).
- An `.mcp.json` written before a rename or move is re-pointed on the next run.
  Each missing path is matched by its own tail (`bridges/<file>`) under the
  program's folders, whatever the old folder was called
  (`repoint_by_relative_path`).
- You never have to set `ICA_MCP_PROJECT` yourself. The fallback after `:-` is
  the folder's path when the entry was written, and the program updates both.

## A launcher is not the program

- A package installed with `uv tool install`, pipx or `npm -g` is written by its
  launcher form -- `uvx <pkg>==<version>`, `npx -y <pkg>@<version>` -- never by
  the installer's private path, whose script runs only beside that install
  (`[mcp.launch_form]`). Phase 1 and phase 4's final check rewrite such an
  entry in a copied `.mcp.json` (`install.portable_launchers`,
  `mcp.launch_form.repair_in_file`).
- "Installed" means runnable: the command's interpreter exists and imports
  what it starts. Copying a launcher script does not copy its program.

## Every server keeps its login; every entry reads its own names

- **The server's own login variable is its slot**
  (`[mcp.verification.server_credential]`). A server whose candidate declared
  no credential gets the variable its own code reads a login from
  (`API_ACCESS_TOKEN`, `BUGZILLA_API_KEY`), else the key the same package's
  other entries read. It is written optional (`${NAME:-}`) when the server
  works without it, so an anonymous answer never costs the login. The final
  check adds it to file entries too (`slot_missing_logins_in_file`).
- **Names only the entry reads** (`own_reference_names`). The key the server
  reads stays; the variable it expands is `{ENTRY}_{NAME}`, e.g.
  `"BUGZILLA_API_KEY": "${BUGZILLA_OPENSUSE_ORG_BUGZILLA_API_KEY:-}"`, so a
  shell's export of a generic `BUGZILLA_API_KEY` can never win over it. A name
  that already names the instance (`GITHUB_…` for github.com), session
  variables and `ICA_MCP_*` are kept. A trust bundle that is the machine's own
  reads `ICA_MCP_CA_BUNDLE`, set per machine by phase 1. A login your Claude
  Code settings hold under the old name is copied to the new one; a literal
  credential in `.mcp.json` moves to your settings env.
- **Anonymous is not logged in.** An answer naming no account
  (`"currentUser": null`) is empty, and an entry working without its login is
  reported as "public answers only -- not logged in: set X".
- **Every kind of terminal.** A login shell (SSH, console, `su -`) and a
  terminal window read different startup files; both are asked, and a value
  broken in either is repaired.
- **Every `claude mcp remove` names its scope** (`--scope local|user`); a
  project entry is never treated as a CLI copy.
- **A trial sends what the entry will send**: it resolves the entry's own
  variable name, never a generic name your shell exports for something else.
- **A server on someone else's host must show it serves your address.** A
  domain that merely begins with the organisation's name (githubcard.com) is
  not GitHub's unless listed in `[mcp.registration.identity.organisation_domains]`
  or named in GitHub's own documentation.
- On every machine `.mcp.json` is copied to, run phase 1: it sets the
  `ICA_MCP_*` values for that machine (`ICA_MCP_CA_BUNDLE` included) and
  repairs CA variables any kind of terminal sets to a missing file
  (`install.machine_paths.repair_trust`). Phase 4's final check does the same
  and logs where each value an entry reads comes from.

## One keep gate; never an empty address; deleting needs ownership

- **One keep gate** (`[mcp.registration] one_keep_gate`). An existing entry
  keeps its place only if this run found it usable, it is a different server
  from the candidate, and it can be written. The same server, rebuilt,
  refreshes its old copy. Values it supplies count for both when compared
  (`compare_after_carrying`).
- **Never an empty address** (`never_an_empty_address`). The final file check
  writes the best candidate held back only for an existing entry's sake when
  the address would otherwise have no entry.
- **Deleting needs ownership; a failed step leaves the tool working**
  (`[install] deleting_needs_ownership`, `owned_marker`,
  `restore_after_failed_step`). A failed reinstall's leftovers are removed only
  when they carry this program's marker. If the tool no longer runs, a runnable
  copy the step left is put back, and the broken folder is kept aside.

## An endpoint is proved, never claimed; kept means written

- **A claim needs a control.** An inconclusive reply (a login page, an HTML
  answer) and a claim that the URL wants a credential make a credential wall
  only when a made-up path on the same host is answered differently
  (`[mcp.discovery.auth] claim_needs_control`).
- **An agent's report closes no tier.** Endpoints an agent reports are kept as
  `claimed_by_agent_unproven`; only a probe here closes the lower tiers
  (`[mcp.policy] agent_claims_close_tiers`).
- **Not MCP to Claude Code means withdrawn.** If Claude Code says what answered
  is not MCP (`not_mcp`: "Unexpected content type: text/html"), the entry
  leaves `.mcp.json` again, the one it replaced comes back, and the address is
  handed over (`withdraw_not_mcp`).
- **A header credential is a value to supply**
  (`count_header_credentials`).
- **Status holds no place.** "Connected" or "answered" keeps nothing
  (`connected_holds_place = false`). An existing entry stays only by proof
  this run, or by ranking above on evidence. It stays only when a copy of it
  was judged this run and can be written to `.mcp.json`, or it is already
  there (`unjudged_is_keepable = false`). A copy that failed this run holds
  nothing, and the copy keeping the place replaces it in the file
  (`failed_copy_holds_nothing`).
- **A downloading launcher is slow, not broken**
  (`launcher_warming_patterns`, `launcher_warm_timeout_seconds`). A launch
  that failed while uvx or npm was still fetching packages is launched once
  more with room to finish.

## Certificate trust reaches the runtime that connects; per-machine values reach every place

- **A URL entry's trust lives in Claude Code's environment** (`[mcp.trust.consumer]`).
  - Claude Code opens a URL entry itself, on Node, which trusts its own roots,
    the system store and `NODE_EXTRA_CA_CERTS`, not this program's bundle.
  - For each https URL entry Claude Code cannot verify on its own, the run
    writes `trust/claude-code-ca.pem` beside `.mcp.json`. It holds the bundle
    Claude Code is already given plus each chain up to its root.
  - `NODE_EXTRA_CA_CERTS` is pointed at it. A bundle that already works is
    kept; one lacking the root is merged.
- **One writer for every per-machine variable** (`[machine_env]`).
  - **Places:** Claude Code's `settings.json` env, `~/.bashrc`, `~/.profile`,
    `/etc/profile.d/initialize-claude-ai.sh`, and every startup file that
    already sets the variable.
  - **End state in each place:**
    - a commented line is uncommented;
    - a missing line is added, inside the block between
      `# >>> initialize-claude-ai: per-machine values` markers;
    - a wrong line is corrected where it stands, also when another tool wrote it;
    - a right line is recognised.
  - **Safety:** files are backed up first (`<file>.<time>.bak`). A file the
    run may not write is reported with the exact line to add.
  - **Effective value:** a fresh login shell and a fresh interactive shell are
    then asked what they end with.
- **The log says it every run.** Each variable gets a line saying what it is
  for, then one line per place (`added`, `uncommented`, `corrected` with the
  old value and why it was wrong, `already correct`, `not permitted`), then
  what each fresh shell ends with. Nothing of this goes into `.mcp.json`.
- **Broken CA variables are corrected** (`repair_broken`). A
  `NODE_EXTRA_CA_CERTS`, `SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE` or
  `CURL_CA_BUNDLE` naming a missing file is corrected. A store-replacing one
  goes back to the system's own bundle.
- **A failed connection is said with its reason** (`read_failure_reason`,
  `failure_classes`): trust, credential, temporary or server. A trust failure
  is repaired as above and Claude Code is asked again.

## Every setting needs a reason; the file runs on other machines

- **Runtime tuning is dropped unless needed** (`[mcp.verification.runtime_tuning]`).
  A variable that tunes the runtime rather than configures the server
  (`NODE_OPTIONS`, `JAVA_TOOL_OPTIONS`, and the others in
  `runtime_option_variables`) fits the machine it was written on.
  smelt-example-net's `--network-family-autoselection-attempt-timeout=30000` took
  5 s here and 53 s on the next machine, past Claude Code's 30 s start-up wait.
  Every candidate carrying one is launched once without it, and it is dropped
  only when the server's *job* (its usage tasks and real calls) works as well
  without it, not merely its start. An unknown job keeps it. One passing
  launch is not enough: every one of `drop_samples` (3) must pass. If Claude
  Code's own use then fails the way a network does (`restore_on`), the setting
  comes back for that machine (`restore`) and the check is asked again.
- **A needed runtime setting is per machine** (`per_machine`). The entry reads
  `${ICA_MCP_<SERVER>_<KEY>:-}` and the value is kept in the run machine's
  Claude Code settings env. Another machine leaves it unset and gets the
  runtime's default: smelt's network wait was needed on one machine and held
  the start past 30 s on the next.
- **HTTPS over HTTP** (`mcp.verification.prefer_https`): an `http://` endpoint
  moves to `https://` when it answers there, so a token is never sent in the
  clear.
- **An unknown login ranks below an offered one**, and **a challenge seen
  before is remembered** when a host gives none this run
  (`unknown_below_offered`, `remember_challenges`).
- **A launcher's momentary failure is retried, not judged**
  (`mcp.verification.launcher_retries`). Two `npx` launches racing on a fresh
  cache ("npm error enoent … package.json") said nothing about the entry.
- **Start-time hazards are flagged** (`flag_start_hazards`): the final file
  check reports a runtime setting holding a wait at or past the start-up limit
  (`[mcp.registration.client_timeout]`).
- **One placement rule for every file mover** (`paths_home.server_destination`),
  so a restored folder is never nested in itself.

## A place is earned by proof; one comparison decides

Two wrong entries an earlier version wrote kept their places on a machine with
no credentials.

- **Claude Code's status is read one way.** A warning status
  (`! Connected · tools fetch failed — Request timed out`) is not connected
  (`[mcp.registration] failed_markers`, `degraded_patterns`).
- **A place is kept by proof, never by status**
  (`mcp.registration.keeping_needs_proof`). An existing entry that proved
  nothing this run (no login accepted, no task done) keeps its place only while
  no candidate ranks above it. Being registered, answering, or "connected" is no
  evidence.
- **One comparison for every chooser**
  (`mcp.discovery.auth.evidence.one_comparison`). The per-stage pick, the
  cross-stage slot and keeping an existing entry all rank by the same evidence:
  proof, the host's history with the login, being built for the API the address
  serves, having worked here, the login the address asks for, then values
  still to supply. Tier order and confidence only break exact ties.
- **Header and token logins are recognised** (`value_schemes`,
  `method_schemes`): `Authorization:Bearer …` is a bearer login whatever its slot
  is called, and a plan that chose tokens counts them as offered.
- **The report never asks for a value that cannot work**
  (`mcp.registration.say_unoffered_login`).

## Judged right on a machine that holds no credentials

A run on a machine with nothing in Claude Code's settings env could prove no
login, so order and appearance decided. It replaced three working servers and
left gitlab with no slot.

- **A refusal in words is a refusal**
  (`mcp.verification.login_check.failure_text_patterns`). An answer whose first
  line states an HTTP failure counts as a failure even when the server does not
  flag it, and its status decides: 401 means refused, 403 no access, 429/5xx
  try again later. mcp-openapi-proxy's "401 Client Error: Unauthorized" had
  "passed" the account's own record anonymously.
- **A path is a setting, not a secret** (`mcp.discovery.reuse.paths_are_settings`).
  `${NAME:-/root/.ssh/id_ed25519}` is where the key lies, so the trial launches
  with it. build-example-net's SSH-signing bridge had been trialled without its key.
  The rule lives where every guard asks (`[mcp.secret_policy]
  path_is_never_a_secret`), so no step moves a key path out of the file as a
  "literal credential" either.
- **The login plan ranks candidates that are all waiting for a credential.**
  The scheme is also read from setting slots (an `SSH_KEY` slot means
  `signature`), and a scheme the address offers nowhere goes last
  (`mcp.discovery.auth.evidence.unoffered_last`).
- **What was detected stays detected.** API kinds found by any pipeline of the
  run are shared and kept on the outcome. sd.example.com's customer API had been
  detected, yet a waiting mcp-atlassian settled the address.
- **Moved files are found before an entry is judged**
  (`mcp.registration.repoint_before_trial`), not only in the final check.
- **A login slot even with nothing held**
  (`mcp.discovery.reuse.slot_when_short_of_login`). An adapter's copy that
  answered while a task needing the account failed gets its slot, optional and
  in the adapter's form. The final file check enforces this for every entry,
  whichever branch kept it.
- **A launcher is never the server** (`[mcp.env_contract] launcher_names`).
  Homebrew's `npx` is a JavaScript file; its own `process.env[key]` was read as
  the server's, and gitlab's `HEADERS` slot was "renamed" to `key`. A key an
  API-kind adapter declares is never renamed.
- **The better proven of two same-named copies is written and counted.**
- **A task only the account can pass proves the login** (`needs_login = true`
  on a usage task: the current user, the account's own record).
- **Every data file an entry names lives beside `.mcp.json`**
  (`mcp.registration.entry_files_beside_file`). The run's trust bundle and a
  repair's config folder are copied into `mcp-servers/<server>/`. Programs are
  phase 1's job and are never copied.

## A login is never thrown away; the file is judged as Claude Code runs it

- **A credential slot holds the credential itself, in the form its server
  takes** (`mcp.discovery.reuse.credential_in_taken_form`,
  `credential_variable = "{SLUG}_TOKEN"`). mcp-graphql takes its token inside
  the JSON in `HEADERS`. The agent proposed `HEADERS: ${GITLAB_EXAMPLE_NET_HEADERS:-}`,
  a JSON object nobody exports, and empty it is no JSON at all: the server
  exited at start-up. Now a slot holding a single bare reference is written as
  the adapter's `credential_env` form around a token variable, for example
  `{"Authorization": "Bearer ${GITLAB_EXAMPLE_NET_TOKEN:-}"}`. That reads the token
  the address already holds, even when the entry holding it is broken, and it
  stays valid while the variable is empty. Every stdio candidate, whichever path
  found it, is given that held token before it is judged. A variable that
  already has a value where Claude Code runs is the operator's whole value, and
  is kept.
- **What the operator holds includes Claude Code's settings env**
  (`mcp.discovery.auth.inventory.include_claude_settings`). Held logins used
  to be looked up in this run's shell only, while the operator keeps them in
  settings.json env, which is what a new terminal's Claude Code has. So
  gitlab.example.net's token was never seen as held.
- **A repair never removes a credential slot** (`mcp.healing.keep_credential_slots`).
  `withdraw_optional_reads` used to drop the empty `HEADERS`, the only place
  the token could go. Credential slots are now rewritten in their form, or kept
  and the start failure reported.
- **A login the operator supplied is never dropped**
  (`mcp.registration.carry_supplied_credentials`). bugzilla-example-com's new
  entry worked anonymously, and the claude CLI copy carrying the API key was
  then replaced by it. A credential key that an existing entry for the address
  carries is now written into the new entry as optional `${NAME:-}`, when the
  new server reads it (its source names it) or has the same launch. Its value
  goes to Claude Code's settings env, never into the file. A CLI copy is
  replaced only after the file's entry carries the slot too.
- **Of two copies with one name, the best proven one is written**
  (`mcp.registration.best_copy_speaks_for_name`). gitlab-example-net was a
  logged-in server in the claude CLI and an anonymous one in the file. The CLI
  copy kept its place, but the file kept the anonymous copy. Now the copy that
  proved most (verified, usage score) speaks for the name, and when it keeps
  its place, it replaces the file's copy. A kept copy that was proved with a
  credential its file entry has no slot for (mcp-graphql given the held token
  in `HEADERS`) is written as it was proved, not "kept as it was".
- **The final file check reads every entry as Claude Code will run it on this
  machine** (`mcp.registration.check_as_run`). A `${` that is not `${NAME}` or
  `${NAME:-default}` is reported. A launched file, or a file an env value names,
  that does not exist once expanded is pointed at the one file of that name in
  this program's folders, or reported with the variable that relocates it.
  gitlab-example-net named a bridge three folders deep that no longer existed.
- **The report lists what travels with `.mcp.json`**
  (`mcp.registration.report_what_travels`): the folders beside it that its
  entries name (`mcp-servers/`, `bridges/`, `trust/`), and the paths each
  machine supplies itself, with the variable that moves each one.
- **A variable read through a constant counts as read**
  (`mcp.env_contract.constant_patterns`, `indirect_read_patterns`).
  `TOKEN_VAR = "GITLAB_EXAMPLE_NET_TOKEN"` followed by `os.environ.get(TOKEN_VAR)`
  is no longer reported as "never reaches the server".

## A runtime's own trust store is judged too; lost files come back

- **A trial names the trust bundle its entry will.** That is the run's own
  bundle where a host needed it, else this host's (`mcp.trust.trial_names_host_bundle`).
  Node keeps its own certificate store and ignores the host's.
  gitlab.example.net's `mcp-graphql` was trialled with no `NODE_EXTRA_CA_CERTS`,
  failed with "fetch failed", and the address ended with nothing, although the
  entry it would have become named the host bundle.
- **A candidate that never reached the service is repaired and proved
  again** (`mcp.verification.heal_transport_everywhere`). "fetch failed", a
  certificate its runtime couldn't verify, DNS or a refused connection count
  as "never reached". This applies in every stage, and before another agent
  attempt is spent. The agent's retry brief says the failure is this
  machine's, not the server's.
- **This program's own bridge backs up a kind's generic servers.** An API kind
  can describe its operations as data. GraphQL's are "introspect_schema" and a
  read-only "graphql_query" posted to `{api_path}`, with mutations refused.
  Once the kind's generic servers were tried and none passed, the bridge tier
  generates its own bridge from them (`api_kind_bridge_when_generic_fails`).
- **Entries keep their files, and lost ones come back.**
  - A copy of the files a working entry launches is kept with its known-good
    record (`mcp.known_good.keep_files`, `snapshot_roots`, `snapshot_max_mb`).
  - A file that is gone later is restored into the server's own folder
    (`restore_missing_files`), and the entry is proved again before it is
    called broken.
  - An existing entry still launching files from the scratch area has them
    moved out when it is found (`mcp.registration.adopt_existing_files`).
  - A claude CLI copy whose files are gone is replaced by the written entry
    (`replace_lost_files_cli_copies`).

## With nothing proved, evidence breaks the tie, and the others are kept

A run with no credential leaves every candidate waiting. build.example.net's run
built two routes: a password bridge to build.example.net, whose API refuses
passwords for that account, and osc signing with an SSH key at api.example.net,
which works and needed only the user name. Confidence picked the password
bridge, although the run had logged "logins offered: basic@build.example.net,
Signature@api.example.net; chosen: client".

How waiting candidates are now handled:
- **Ties are broken by evidence** (`[mcp.discovery.auth.evidence]`). That
  applies within a stage and when a later stage claims the slot. In order:
  - a scheme this host refused before goes last (login history);
  - the scheme the address's API host itself asks for comes first;
  - fewer values still to supply comes first. A key file the machine holds
    counts as supplied.

  The API host's login counts only when the candidate sends it there
  (`host_matters`): its URL settings (`url_setting_keys`) say where, or, failing
  those, a file it names (`url_assignment_pattern`, e.g. an osc client's
  `apiurl`). A waiting server aimed at another host of the product is tried on
  the API host and switched when it does its job there as well
  (`[mcp.verification.address_forms] try_api_hosts_when_waiting`), and only to
  an API host that asks for the login it sends. A bridge that drives the
  product's own client (osc) counts as built for the address's API, like one
  generated from its API kind (`client_bridge_is_built_for_api`), so between
  the two the login decides.
- **A bridge's declared login is its credential slots** (`[mcp.discovery.reuse]
  manifest_login_slots`): read back from an entry, each login variable its
  manifest declares that is still empty waits for a value, whatever its name.
- **A key file is a credential** (`[mcp.registration]
  credential_files_may_be_absent`, `credential_file_slots`). An SSH key or
  client certificate named in a credential slot may be absent on the run
  machine; the entry keeps its path and waits for it, never refused as naming
  a missing file.

  Only then come confidence and list order.
- **The other waiting servers are kept** (`[mcp.alternatives]`,
  `mcp-alternatives.json` in the state directory). Each is stored as an entry
  holding references, never values. Every later run, `--mcp-recheck` included,
  tries them again at their tier, so a credential supplied since then can prove
  one. An address written with a working server clears its record.

## The status a server states outranks the client's wording

mcp-atlassian reports a 403 as "Authentication failed … (403). Token may be
expired or invalid". 403 means the token was accepted and its account may not
do this. When a failure states exactly one status, that status decides
(`login_check.status_outranks_wording`, `status_verdicts`): 401 means the
credential was refused, 403 means the account has no access.

A refused single-secret login is also asked once more of the service itself
(`login_check.confirm_by_own_record`). The check reads a `protected_paths`
page of the address's catalogue product, with the credential sent as the
catalogue's `sent_as` says.

## A written entry owns its files; a project file holds no values

- **Files leave the scratch area.** Bridges, manifests and CA bundles a server
  launches or reads from the run's scratch area (`paths.work_dir`) are moved to
  `mcp.registration.artifacts_home/<server>/` (default `mcp-servers` beside
  `.mcp.json`, variable `ICA_MCP_SERVERS`) before the entry is written. The
  server's own folder becomes that folder itself, never a folder nested inside
  it, and a file already there is never moved again. Cleanup never touches
  that folder.
- **Plain-text credentials leave the project file.** A credential `.mcp.json`
  holds as plain text, or as the default of `${NAME:-…}`, is moved to Claude
  Code's `settings.json` env (mode 0600), and the entry reads `${NAME}`
  (`mcp.registration.move_literal_secrets`). A value already in settings env
  under that name is never overwritten; such an entry is left in place and
  named.

## A certificate this host does not trust is handled once, for the whole run

That was half of it. On another machine `build.example.net` was still missing from
`.mcp.json`, and the log showed why: the host's own store did not hold Example's
CA either, discovery's HTTP client trusted certifi only, the preflight reported
the certificate failure as "did not respond at all", and every agent that got
further extracted the chain on its own and threw it away with its workspace.

A chain is also judged the way the strictest runtime reads it. The run's
bundle once carried smelt.example.net's chain only to a cross-signed "Example CA
Root": Python accepts that partial chain, Node does not. Now a chain that falls
short is completed up to a self-signed root (`mcp.trust.complete_to_root`),
using issuers this machine already has: the host's store, `--mcp-trust-ca`,
the anchor folders in `mcp.trust.issuer_sources`, and the CA files that MCP
entries here already name. The completed chain is kept only when it then
verifies strictly. A certificate on a target's chain that expires within
`mcp.trust.expiry_warning_days` (default 60) is named in the report.

Now every target's certificate is examined once per run, before anything is
probed, by one trust store (`core/trust_store.py`) that every HTTP request,
every registered stdio entry, every trial launch and every generated bridge
reads:

1. **Trusted here** (system store, certifi, `--mcp-trust-ca`): nothing changes,
   and nothing is said — the prompts and entries for such hosts are byte for
   byte what they were.
2. **Not trusted, but fixable by what the server itself presents** — the
   verify result is a missing issuer or unknown root (`pin_verify_codes`) and
   the server sent its intermediates: with `--mcp-trust-pin targets` (the
   default) those are learned, **for addresses you asked for only**, kept under
   `mcp.trust.store_dir`, and reported as a `certificate <host>` step with
   their subject and SHA-256 fingerprint. That is trust on first use: review
   the fingerprint, delete the directory to forget it, or turn it off with
   `--mcp-trust-pin never`.
3. **Neither**: reported as exactly that — "certificate from *issuer* not
   trusted here; use `--mcp-trust-ca FILE`" — never as an absent host.

A host whose chain was learned or given gets the run's combined bundle in its
entry instead of the system bundle, in the variable its runtime reads, and the
agent's brief names that file so it does not extract one of its own. Other
hosts' entries are unchanged.

**Node is stricter than Python.** Python accepts a chain that ends at a
trusted intermediate; Node insists on a self-signed root. `build.example.net`
sends its intermediates but not the root they chain to, so a learned chain
serves Python servers and bridges, and the report says when a Node server
needs the root passed with `--mcp-trust-ca`. Registration still depends on a
server existing that works; this removes the certificate as the reason it
does not.

**Every bundle an entry names is checked the way its runtime reads it.** This
program verifies with partial chains on (`mcp.trust.partial_chain`), which is
how Python 3.13 and curl verify; a server it registers may not. smelt.example.net's
entry named `NODE_EXTRA_CA_CERTS=…/ca-bundle.pem`, a file holding "Example CA all
2023.1" and "Example CA Root" but not the "Example Trust Root" above them. Every check
here passed, the server listed its tools, and every request failed with
`fetch failed`. So each trust-store file an entry names is checked against
every https host the entry reaches with partial chains off. That includes this
program's own files and a file the entry brought with it: a reused
registration, an agent's proposal, or `--mcp-trust-ca`. A variable that adds to
its runtime's store (`NODE_EXTRA_CA_CERTS`) is judged together with that
store, and the store is asked of the runtime itself (`mcp.trust.runtime_stores`;
certifi is the host's store on some distributions and would vouch for roots
Node does not ship). A file that falls short is replaced by the host bundle or
this run's combined bundle, whichever carries the chain for every host, and the
note names the missing root. When neither does, a variable whose runtime needs
a self-signed root (`mcp.trust.root_required_variables`) gets the entry
refused, in the stage and at publish (`mcp.trust.refuse_incomplete_chain`).
Any other variable is only noted. The trial uses the same replacement, so the
launch tried is the one written. The check is off with
`mcp.trust.check_named_bundles = false`.

**API descriptions that are not at a fixed path, or not JSON.** OBS publishes
`OBS-v2.10.50.yaml`, named only in the initializer script of its Swagger UI
page at `/apidocs/`. Traditional discovery now reads YAML (with PyYAML, when
installed; otherwise it reports where the description is) and, when no
conventional path answers, reads the pages in `mcp.discovery.api_doc_pages`
and the scripts they load (`api_doc_script_patterns`, at most
`api_doc_max_scripts`) for a link matching `api_spec_link_patterns`, from the
target's own host only (`api_spec_same_host_only`).


**Not every product issues a token.** `OPENQA_OPENSUSE_ORG_TOKEN` is a complete
instruction only if openQA has something called a token to give you. It does
not: its API authenticates with an API key and secret over HMAC, and its MCP
endpoint takes `username:key:secret` — the three parts joined by colons.
Told only a variable name, you would go looking for a thing that does not
exist, and the server would answer `{"error":"invalid personal access token"}`.

So the exceptions are data. Each entry under `[mcp.discovery.auth.profiles.*]`
matches a hostname and says what to call the variable, what the value looks like
and where to get one:

```toml
[mcp.discovery.auth.profiles.openqa]
match  = "(^|\\.)openqa\\."
env    = "{SLUG}_PAT"
format = "username:key:secret -- your openQA login name, then the API key and secret, joined by colons"
obtain = "https://{host}/api_keys (log in first; the key and secret are shown once when created)"
```

**The variable is named after the shape, so the name is the instruction.** A
value of `username:key:secret` produces
`OPENQA_OPENSUSE_ORG_USERNAME_KEY_SECRET`, not `..._API_KEY` — because the
latter reads as "put the key here", and an operator who did exactly that got a
`403` with nothing to explain it. A shape with no separator is one opaque token
and keeps the shorter `..._TOKEN`.

The shape does not have to come from the table above. A discovering agent
reports it alongside the reference, which is what makes this work for the
products nobody has written an entry for:

```json
"secret_env": {
  "JIRA_EXAMPLE_COM_API_KEY": {
    "reference": "env:JIRA_EXAMPLE_COM_API_KEY",
    "shape": "email:api_token",
    "obtain": "https://id.atlassian.com/manage/api-tokens"
  }
}
```

→ registered as `JIRA_EXAMPLE_COM_EMAIL_API_TOKEN`. A shape is a *description*
and is scrubbed like everything else an agent returns: anything that looks like
a value is discarded with a note.

Precedence, highest first: your own `bearer_token_ref` — nothing renames a
variable you named — then a discovered shape, then the name the agent proposed,
then a profile, then the generic template.

`format` and `obtain` are printed beside the variable name in the briefing and
in the "next steps", so what reaches you is:

```
OPENQA_OPENSUSE_ORG_PAT -- the value is username:key:secret … not a single
opaque token; get it from https://openqa.opensuse.org/api_keys
```

`header_template` may also be set, for a product that wants something other than
`Bearer {token}`. A target with no matching profile gets the generic name, which
is right for most of them — and a name discovery already found, or your own
`bearer_token_ref`, still outranks any profile.

A name that discovery already found beats the template, and
`mcp.discovery.auth.bearer_token_ref` beats it too: an agent that read the
vendor's documentation knows the product calls it `GITLAB_TOKEN`, and an
operator who said where their token lives has already answered the question.
Give the run a real token with `--mcp-token-ref env:MY_VAR` and the handshake is
attempted with it instead, in which case the endpoint verifies normally and none
of this applies.

## What gets written, and where

Every verified server — and every endpoint
[registered with an empty credential](#an-endpoint-that-wants-a-credential-is-written-out-with-the-slot-left-empty)
— goes to **both** destinations, always:

* **`.mcp.json` in the directory you ran the command from.** That is the file
  Claude Code reads for a project's MCP servers, so it works where it lands
  rather than being a record you have to act on. It is written first, because it
  does not need the CLI to exist.
* **`claude mcp add-json <name> ... --scope local`**, so the servers are usable
  in the next session without moving anything.

Names are built to be read side by side in `claude mcp list`: the base is the
address's **full FQDN** with dashes for dots (`mcp.deepwiki.com` →
`mcp-deepwiki-com`), never a shortened form that two addresses on one domain
could share. When an address yields several endpoints — which is what
[`--exhaustive-mcp`](#--exhaustive-mcp-records-every-endpoint-that-answers) is for
— *every* one of them takes a suffix naming which endpoint it is (its path, or
its host when the endpoint lives elsewhere): `mcp-deepwiki-com-mcp` and
`mcp-deepwiki-com-mcp-devin-ai` are a pair you can choose between, whereas a
bare name beside a suffixed one reads as though the second were an afterthought.

**`.mcp.json` holds `mcpServers` and nothing else.** The file exists to be
copied — into another project, into a commit, onto a machine that has not been
set up yet — and that copy is usually made by selecting the whole thing, so
anything in it that is not configuration is something the next reader has to
notice and delete first. It is merged, not overwritten: servers already in it are
kept, the discovered ones are added or updated, and it is written atomically. A
credential is never in it — an entry that needs one carries `${VARIABLE}`, which
Claude Code expands from the environment you start it in. Put the file somewhere
else with `mcp.mcp_json_path` (absolute pins it; relative is resolved from the
working directory) or `INITIALIZE_CLAUDE_AI_MCP_JSON`.

Everything the servers cannot say about themselves is printed instead, in its own
framed block at the end of the phase — and, because the console writes into the
transcript, the same block is in the log file and the whole of it is in the
`--json` report under `mcp_briefing`:

```
╭─ 2 MCP SERVER(S) -- HOW TO USE ./.mcp.json ──────────────────────────────────╮
│ Written by initialize-claude-ai. ./.mcp.json holds 'mcpServers' and nothing  │
│ else, so you can copy the whole file into another project as it is.          │
│ Everything below is here, in the report and in the log -- not in the file.   │
│                                                                              │
│ Export these before you start Claude Code                                    │
│ export EXAMPLE_API_TOKEN=...                                                 │
│ Each ${NAME} in ./.mcp.json is expanded from your environment when the       │
│ server starts. The file can be committed; the values must not be.            │
│                                                                              │
│ Install these first                                                          │
│   pip install --user httpx                                                   │
│                                                                              │
│ Written by this run -- these servers stop working without them               │
│   /home/you/.local/share/initialize-claude-ai/bridges/mcp_bridge_example.py  │
│                                                                              │
│ Servers                                                                      │
│   example-com-bridge                                                         │
│     what it is         generated from the target's OpenAPI description       │
│     found by           bridge                                                │
│     needs              EXAMPLE_API_TOKEN                                     │
│     install first      pip install --user httpx                              │
├──────────────────────────────────────────────────────────────────────────────┤
│ » Start Claude Code from this directory: it reads ./.mcp.json on startup and │
│   asks you once to approve the servers in it.                                │
│ » For another project, copy the file into that project's root: cp            │
│   ./.mcp.json /path/to/project/.mcp.json                                     │
│ » To have them in every project instead of this one, register them for your  │
│   user: claude mcp add-json <name> '<entry>' --scope user                    │
│ » Already registered by this run at --scope local; `claude mcp list` shows   │
│   them, `claude mcp remove <name>` undoes it.                                │
╰──────────────────────────────────────────────────────────────────────────────╯
↻ [CHANGED ] mcp configuration  written to ./.mcp.json: mcp-deepwiki-com-mcp, mcp-deepwiki-com-mcp-devin-ai
```

Silence the block with `mcp.instructions.enabled = false` or
`INITIALIZE_CLAUDE_AI_MCP_BRIEFING=false` — the `--json` report still carries it.
Earlier versions wrote all of this *into* `.mcp.json` under a top-level
`_instructions` key. A file left over from one is cleaned up at the **start of
every phase 4 run** — before the run can end early, so a file on a machine where
discovery finds nothing is still made copy-and-paste clean — and again whenever
the file is written. The removal is reported as its own `configuration file`
step, because it is your file and the run changed it:

```
↻ [CHANGED ] configuration file  removed _instructions from ./.mcp.json: an older version of
             this program wrote its instructions into the file, and the file has to stay
             copy-and-paste configuration. The same instructions are printed below and kept
             in the log.
```

`mcp.instructions.strip_keys` (or `INITIALIZE_CLAUDE_AI_MCP_STRIP_KEYS`, comma
separated) is the list of names removed on sight — only those names, because any
other top-level key in that file is yours.

**Duplicates are called out, not silently created.** An entry already in
`.mcp.json` that points at the same service as one just discovered — the same
host and port for a remote server, the same launch command for a local one — is
reported as its own step and named in the briefing. Nothing complains about two
entries for one service otherwise: Claude Code will load both, offer both sets of
tools, and leave you to work out which of two near-identical names the model just
called. Disable the check with `mcp.instructions.warn_on_duplicates = false`.

**The phase ends with one verdict line.** By the time the page has scrolled, the
question left is "did this work, and is there anything I have to do about it" —
so the last step repeats the three things that make a registered server fail
later (a credential that has to be exported, a generated script somebody has to
keep, a duplicate entry) and ends with one word:

```
  Final verdict:
    - 1/1 address(es) yielded a server that completed an MCP handshake; 1 entry registered as mcp-deepwiki-com
    - No API key, token or other credential is needed by any registered server.
✔ [   OK   ] final verdict  1/1 address(es) yielded ... Verdict: SUCCESS
```

The summary box above it is counted the same way — one section per address, not
one row per probe — so a server found by the third stage reads as a pass with
its earlier stages listed underneath:
[a phase that works through a list](usage.md#a-phase-that-works-through-a-list-is-counted-on-the-list).

`SUCCESS` when every address yielded a working server and nothing is left to
decide, `PARTIAL` when something works but an address came up empty or has only
a server waiting for a credential, a duplicate needs resolving, or a
registration proved wrong was left in place, `FAILED` when nothing handshook and
nothing answered. Only entries actually written count: a candidate every gate
refused is reported under `refused`, never as a working server. The word is the last token
of the line so a wrapping script can grep for it, and it is in the JSON report
as `data.mcp_verdict`; the variables still to be exported are in
`data.mcp_required_environment` (working servers) and
`data.mcp_pending_environment` (endpoints waiting on a token).

**A generated bridge asks for the credential the API uses.** build.example.net's
bridge used to be registered with `MCP_BRIDGE_AUTH_HEADER` and
`MCP_BRIDGE_AUTH_VALUE`. Those names said neither that the server wants HTTP
Basic (`WWW-Authenticate: basic realm="API login"`) nor that the value had to
be `Basic` plus the base64 of `user:password`, and the value there could never
have logged in. The method now comes from evidence: the API description's
security scheme, else the server's 401 challenge to a documented GET asked
without credentials. Its parts (`[mcp.bridge.generated.auth.parts]`: basic is
a user name and a password, bearer a token, an API key a key) are exported
under names built from `variable_template` (`BUILD_EXAMPLE_NET_USERNAME`,
`BUILD_EXAMPLE_NET_PASSWORD`) and described in words (`descriptions`). The bridge
reads them under those same names, which its manifest gives it, and builds the
header itself. So the entry, the report and the shell all name the same
variable, and the entry carries only the parts the API needs. When the parts
are already exported, one authenticated GET checks the login, and the report
says whether it was accepted or rejected (`check_login`). With no evidence of
a method, the bridge sends no credential and says so.

**What a server advertises has to be loadable by its client.** A handshake
proves a server answers; it does not prove the client can use what it lists.
Every tool name and input property key is checked against the client's rules
(`mcp.verification.client_name_pattern`). openSUSE's osc-mcp declares a
property named `path_repository jsonschema:`, and once that tool was loaded the
Anthropic API refused every request of the session with "Property keys should
match pattern". Such a server is refused like one that does not start
(`mcp.verification.schema_problems`; `warn` registers it and says so). The
check is the verification table's `schema` column and applies to candidates,
to entries launched as written and to existing registrations. The verdict names
the server, tool and key (`mcp_schema_problems`). A generated bridge exposes a
cleaned-up name for an API parameter the client would reject and sends the
original to the API.

**Every view sorts addresses into the same three kinds.** An address is
*working* (a server completed a handshake and is written), *waiting for a
credential* (an entry is written with its slot empty), or has *nothing
registered*. The summary box adds a row for each kind under its shared
three-row scoreboard, whose "failed" row has to hold everything short of
working. The summary line, the safety note and the final verdict count the
same way. An address with nothing registered says so in its own line, naming
the entry the file lacks and the candidate that came closest (`best_lead`:
what it is, what it needs, what it said). The verdict names it under
`Not in ./.mcp.json:`, and the JSON has it in `data.mcp_not_registered`. A
registration the machine already had for an address, which did not start during
the run and was left in place, is its own step (`<target> :: existing <name>`)
and a verdict line, with the command to fix or remove it (`broken_existing`).
build.opensuse.org used to sit in one "failed" pile with eight addresses that
had entries, and nothing said its entry was missing.

## `--concurrency` overlaps addresses, and nothing else

Phase 4 is **~98% agent thinking** — measured on a real run: 256 minutes, of
which 250 were agent turns and 9 were everything else put together. Addresses
are independent of one another, so overlapping them is the only change that
meaningfully shortens a long run.

```bash
initialize-claude-ai run --phases 4 --mcp-server a.example.com,b.example.com   # one at a time
initialize-claude-ai run --phases 4 --mcp-server … --concurrency               # target_concurrency_bare at a time (4)
initialize-claude-ai run --phases 4 --mcp-server … --concurrency 8
```

**It reduces waiting, not work.** The same turns happen and the same tokens are
spent; they are merely overlapped. On the run above, four at a time would bring
four hours to roughly one.

**Output stays grouped by server.** Each address's narration is held back and
printed as one contiguous block, in the order the addresses were given — so a
concurrent run reads exactly like a sequential one, just sooner. Nothing
interleaves, and the report and registration order are unchanged.

**Without the flag, nothing changes at all.** At a concurrency of 1 no thread is
started: the sequential path is not a second implementation kept in step with
this one, it is this one with the strategy set to "inline". A test asserts that
the default spawns no threads.

Two things *are* different when you raise it, and both exist to keep the agentic
rules intact rather than despite them:

| | Why |
| --- | --- |
| `mcp.agent.max_budget_usd` is applied when an address is **admitted**, not inside a stage | otherwise one address's agent loop could be cut short by what another address spent — the fallback becoming a first excuse |
| a **transient** failure no longer consumes one of the agent's attempts (`mcp.agent.transient_free_retries`, 3) | N model sessions at once provokes rate limiting, and the agent must exhaust its own loop, not one that rate limiting exhausted for it |

The second is a latent bug fix that applies whether or not you use the flag.

Everything else is untouched: the tier order, the four-step checklist, one
stage at a time within an address, verification, and `--no-agent`.

**Addresses share nothing that belongs to one of them.** One pipeline serves
every thread, so what outlives an address is guarded by what is shared — the
workspace (prepared once, skills swapped in by rename), an origin's negative
control probe (asked once, the others wait for the answer), an identical
install command (run one at a time) — never by which server is sharing it.
What belongs to one address is kept per thread: its runtime, its retry model
and the models it has been refused on, and its own
`agent-workspace/artifacts/<address>` directory. A refusal on one address
therefore cannot change the model another address's request is already using,
and an agent stage that crashes is done by the deterministic stage instead of
closing every tier below it. `tests/test_mcp_concurrency_shared_state.py` runs
the real pipeline one at a time and four at a time and asserts the outcomes are
identical.

<a id="--agent-max-turns-is-the-lever-that-reduces-work"></a>

## `--agent-max-turns` is the lever that reduces work

Each subagent has its own turn budget, and those are the numbers that matter:

| Agent | turns |
| --- | --- |
| `mcp-endpoint-scout` | 10 |
| `mcp-package-scout` | 10 |
| `mcp-bridge-engineer` | 12 |
| `mcp-strategist` | 10 |

`mcp.agent.max_turns` (24) is a *fallback* for an agent that sets none — and
since all four set one, changing it does nothing. `--agent-max-turns` therefore
overrides the definitions outright rather than feeding into that fallback; a
flag pointed at the fallback would have been equally inert.

**Lowering it is the interesting direction.** Phase 4 is ~98% agent thinking and
turns are the unit of that thinking, so this is the only lever that makes a long
run genuinely *cheaper*. `--concurrency` reduces waiting; this reduces work. It
is also what the run itself already advises when a stage keeps running out of
wall clock:

```
Every attempt ran past mcp.agent.timeout_seconds. Raise it … or lower
mcp.agent.max_turns so the agent commits to an answer sooner
```

— advice that was unfollowable until this flag existed, because the key it names
is the dead fallback.

```bash
# a cheaper sweep: commit sooner, four addresses at a time
initialize-claude-ai run --phases 4 --concurrency 4 --agent-max-turns 6

# bounded and unattended
initialize-claude-ai run --phases 4 --agent-max-turns 6 --timeout 30m
```

The trade is real: fewer turns means the agent occasionally commits to "nothing
found" where it would have found something.

**Raising it usually needs `--timeout-scale` as well.** Turns and
`mcp.agent.timeout_seconds` (900) are independent budgets and the agent stops at
whichever runs out first, so a bigger turn budget alone is often cut off around
turn six — and `timeout_turn_scale` (0.5) then halves the budget for the next
attempt. To give one stubborn address genuine room:

```bash
initialize-claude-ai run --phases 4 --mcp-server confluence.example.com \
    --agent-max-turns 30 --timeout-scale 2
```

**There is a gauge for the dial.** Every stage records `turns_used` and
`turn_budget` in the report, and says so on screen when the budget was actually
spent:

```
• endpoint: used all 10 turn(s) -- the budget, not the target, may be what
  ended this attempt (--agent-max-turns)
```

That line is the difference between tuning on evidence and tuning on instinct:
an agent that finished at 4 of 10 will not be helped by 30, and one that stopped
at 10 of 10 might be. Note also that `mcp.agent.max_attempts` is 3 as shipped
(`--agent-max-attempts` changes it), so a stage can spend up to that many times
this budget, and an address runs up to four stages.

<a id="--agent-max-attempts-is-how-many-corrections-a-stage-gets"></a>

## `--agent-max-attempts` is how many corrections a stage gets

A stage the agent does not settle on its first answer is tried again, and every
retry is told exactly what went wrong with the last one. `mcp.agent.max_attempts`
(3 as shipped) is how many tries that is. `--agent-max-attempts N` sets it for one
run. It does exactly what `--set mcp.agent.max_attempts=N` does, but the value is
checked on the command line:

```bash
# one try per stage: the cheapest agent run
initialize-claude-ai run --phases 4 --mcp-server example.com --agent-max-attempts 1

# more room for a server that keeps nearly working
initialize-claude-ai run --phases 4 --mcp-server confluence.example.com --agent-max-attempts 5
```

- **A value is required.** A bare `--agent-max-attempts` is rejected by the
  parser, and so is anything that is not a whole number. 0 is refused, because
  a stage with no attempts would be skipped without anyone deciding to skip it.
- **Leaving it out changes nothing.** No override is written, so
  `INITIALIZE_CLAUDE_AI_AGENT_MAX_ATTEMPTS`, a `--config` file or `--set` decide
  exactly as they did before. If both the flag and `--set` are given, the flag
  wins.
- **The same count applies to every server and every stage**, whatever was
  given to `--mcp-server`.
- **Retries after temporary failures are extra.** A rate limit or an
  unavailable service is retried without using up an attempt; that allowance is
  set separately by `mcp.agent.transient_free_retries`. An endpoint that answered
  401 or 403 settles its stage at once, so no further attempts are spent on it
  (`mcp.agent.credential_wall_settles_stage`).
- **Traditional mode has no attempts.** With `--no-agent`, or `mcp.agentic =
  false`, the deterministic pipeline runs each stage once. The flag is still
  accepted, so existing scripts keep working, but the run warns that it has no
  effect. `--agent-max-turns` behaves the same way.

The cost grows with the count: a stage can take up to *attempts × turn budget*
(see above) before the traditional fallback is asked to finish it.

<a id="--timeout-and---timeout-scale-are-two-different-questions"></a>

## `--timeout` and `--timeout-scale` are two different questions

The obvious design — one `--timeout N` substituted into every timeout in the
program — is the one design that cannot work. There are 21 operation timeouts
here and they span **5 seconds to 1800**, a 360× range, because they measure
different things:

| What it bounds | Value |
| --- | --- |
| a TCP connect | 5s |
| one probe request | 6–8s |
| one HTTP call | 15–30s |
| one handshake, or a process starting | 20–25s |
| one subprocess (a package trial, a `claude mcp` call) | 90s |
| one discovery stage | 180s |
| one package install, one agent turn | 600–900s |
| waiting for a person at a browser | 1800s |

`--timeout 30` would kill agent turns at 30 seconds and time out your browser
login while you were typing. `--timeout 1800` would leave every dead TCP connect
hanging for half an hour, and with thirteen addresses the run would never end.

So the two things people actually want are separate flags.

**`--timeout` is a ceiling for the whole run**, and it is not expressible as a
timeout on anything smaller. Phase 4 took 259 minutes against thirteen addresses
with every individual timeout comfortably unmet — 259 minutes is simply what a
few hundred well-behaved operations add up to.

It stops the run **starting** new work; it does not interrupt work in progress:

```
• out of time after 2h 0m    --timeout was 2h; 5 address(es) not started
```

An address already being worked is left to finish, because killing an agent
mid-turn throws away tokens already paid for and leaves half an answer behind.
The honest cost is overrun: the run can exceed the ceiling by as long as one
address takes, which on a slow target is the better part of an hour. If you need
a hard stop at a wall-clock instant, this is not that, and nothing here pretends
otherwise.

The verdict becomes `PARTIAL` and every address never attempted is named, in
order, with the command to resume them:

```
Out of time: the run reached its --timeout of 2h and never started
https://d.example.com, https://e.example.com. Nothing already registered is
affected. Continue with: --mcp-server https://d.example.com,https://e.example.com
```

**Order matters once you set one.** Addresses are worked in the order you give
them, so the ones dropped are the tail of the list. Put the ones you care about
first.

**`--timeout-scale` is the honest form of "one global timeout value".** It
multiplies every operation timeout, preserving the relationships: `2` doubles
them all, `0.5` halves them. This is the flag for "this machine, or this link,
is slower than you assumed" — and it pairs with `--concurrency`, where several
addresses contend for the same CPU and network and a timeout that was
comfortable at N=1 starts biting at N=8.

It applies to every genuine timeout — including each command's entry in
`[auth.google_cloud.command_timeout_seconds]` and
`[auth.claude_oauth.command_timeout_seconds]` — and not to the five settings
that merely look like them: `progress_interval_seconds` is a heartbeat,
`latency_target_seconds` and `latency_ceiling_seconds` are thresholds for
*scoring* a server when ranking — stretching those would change which server
wins, which is not what anybody means by "allow more time" —
`stop_grace_seconds` is the gap between `SIGTERM` and `SIGKILL`, and
`timeout_turn_scale` is itself a multiplier. The rule is a suffix match on
`timeout_seconds` (`run.timeout_scale_suffixes`), on a key or on the table
holding it, which is also why the deadline
setting is called `run.deadline_seconds`: the one duration that must mean
exactly what you typed is kept outside the rule by its name, rather than by an
exception list somebody has to maintain.

**Used together**, `--concurrency 4 --timeout 2h --timeout-scale 2` means four
addresses at a time, every operation allowed twice as long, stop admitting at
two hours — and finish by roughly two and a half, because scaling doubles the
agent turn and with it the overrun. Note that the last two pull against each
other: a larger scale means each address takes longer, so fewer of them fit
inside a fixed deadline. That is not an error and is not refused, but it is
worth knowing before setting both aggressively.

**Neither flag has a bare form.** `--concurrency` does, because its bare value
only changes scheduling and cannot change the result; a wrong deadline or a
wrong scale silently changes what work gets done, and a convenience shorthand is
not worth that.

**With neither flag, nothing changes.** No deadline is installed — the admission
checks are not added to the loop — and at a scale of `1.0` the resolver returns
each setting untouched, with no arithmetic at all, so an `8` cannot become an
`8.0` on its way to a subprocess. `tests/test_timeout.py` pins every one of the
26 time-related settings to the value it had before these flags existed.

## `--exhaustive-mcp` records every endpoint that answers

By default the search stops at its first success — the first path that answers,
in the first stage that produces anything — because one working server is all it
takes to finish. A host can expose more than one, though: a Streamable HTTP
endpoint and a legacy `/sse` one, a documentation server alongside an API
server, a published package as well as a live endpoint. `--exhaustive-mcp` is
how you see all of them.

It turns on `mcp.exhaustive` (run every stage, not just up to the first that
works) and turns off `mcp.discovery.stop_at_first` (probe every candidate path,
not just up to the first endpoint that answers), and announces both in a
`selection` step, because the run takes noticeably longer as a result. Every
endpoint that passes [verification](#verification-connected-callable-and-answering-both-ways)
is then written to `.mcp.json` and registered under
[its own name](#what-gets-written-and-where) — one endpoint reached twice
(probed under both transports, or found again by a later stage) is still one
server; the duplicate becomes a note on the survivor.

```
✔ [   OK   ] selection  recording every working endpoint per address; every stage runs, not just up to the first that works; every candidate path is probed, not just up to the first endpoint
...
    - https://mcp.example.com/mcp (found by the endpoint stage) -- Example 2.1 via streamable-http, protocol 2025-11-25, 12 tool(s)
    - https://mcp.example.com/sse (found by the endpoint stage) -- Example 2.1 via sse, protocol 2024-11-05, 12 tool(s)
    - mcp-server-example (npm) (found by the package stage) -- Example (local) via stdio, protocol 2025-06-18, 8 tool(s)
✔ [   OK   ] https://mcp.example.com :: 3 working endpoint(s)  every endpoint that answered is being recorded (--exhaustive-mcp)
```

`--exhaustive-mcp` and `--the-best-mcp` are opposites — record them all, or keep
exactly one — so they cannot be combined; the CLI says so rather than letting
one of them quietly win.

## `--the-best-mcp` picks one server, and shows its working

By default every server that completes a handshake is registered: a working
server is a working server, and the run stops looking as soon as it has one.
`--the-best-mcp` asks a different question — *which* of them should Claude Code
actually use — so it changes the run in two ways.

**It widens the search first.** There is no "best of one". The flag widens the
search exactly as
[`--exhaustive-mcp`](#--exhaustive-mcp-records-every-endpoint-that-answers) does,
and announces it in the same `selection` step, because the run takes noticeably
longer as a result.

**Then it scores what it found.** Nothing here is an opinion about a vendor;
every aspect comes from something the run observed, and each is scored 0.00–1.00
before its weight is applied:

| Aspect | What it measures | Column |
| --- | --- | --- |
| functionality | tools, resources and prompts offered, plus advertised capabilities | `funcs` |
| usability | a URL you call versus a local process to install, launch and keep alive; whether the server names itself and ships instructions | `use` |
| configuration | one-time wiring: install commands, variables to export, custom headers | `config` |
| stability | **measured**: the handshake is repeated (`stability_probes`, default 2) and both success rate and worst latency count | `stable` |
| security | TLS, whether third-party code runs locally, whether the endpoint is even on the host you asked about | `secure` |
| protocol | streamable-http over the deprecated SSE transport, and how recent the negotiated protocol version is | `proto` |
| provenance | which stage found it — the vendor's own endpoint outranks a published package, which outranks a bridge generated a minute ago | `source` |

```
  server                            score  funcs  use   config  stable  secure  proto  source
  --------------------------------  -----  -----  ----  ------  ------  ------  -----  ------
  https://mcp.example.com/mcp        0.91   0.83   1.00   1.00    0.97    1.00   1.00    1.00
  https://mcp.example.com/sse        0.74   0.83   1.00   1.00    0.91    1.00   0.45    1.00
  mcp-server-example (npm)           0.58   0.50   0.60   0.60    1.00    0.65   0.85    0.75
    - set aside: https://mcp.example.com/sse (0.74) -- protocol 0.45: sse, protocol 2024-11-05; functionality 0.83: 10 tool(s)
    - set aside: mcp-server-example (npm) (0.58) -- security 0.65: runs third-party code locally; usability 0.60: a local process to keep alive
    - run without --the-best-mcp to register every server that answered, or retune the weights in [mcp.ranking]
✔ [   OK   ] https://mcp.example.com :: best of 3  mcp.example.com/mcp scored 0.91/1.00 -- 12 tool(s), TLS, 3/3 handshake(s)
```

The winner's line says what carried it; each `set aside` line says where that
server lost the most ground, weighted — repeating what it shares with the winner
would explain nothing. One endpoint found twice (probed under both transports,
or found again by a later stage) is one server: the duplicate is folded into a
note on the survivor rather than given a second row and a second registration.

Only the winner is written out — exactly one entry per address in `.mcp.json`,
under the address's bare FQDN. The losers are not discarded: the full scorecard
is in the JSON report under each address's `ranking`, and the closing summary
says how many were set aside.

**The weights are yours.** Every weight, penalty and target lives in
`[mcp.ranking]` in `initialize_claude_ai/data/40-mcp.toml` and follows the normal
precedence rules, so a site that cares more about security than breadth changes
a number rather than the code:

```bash
initialize-claude-ai run --phases 4 --mcp-server example.com --the-best-mcp \
  --set mcp.ranking.weights.security=6 --set mcp.ranking.stability_probes=5
```
