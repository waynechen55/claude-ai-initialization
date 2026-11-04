# Contributing

Thanks for looking. This file is short on ceremony and specific about the two
or three conventions that actually matter here.

## Getting set up

```bash
git clone https://github.com/waynechen55/claude-ai-initialization
cd claude-ai-initialization
python3 -m venv .venv && . .venv/bin/activate
make install        # pip install -e ".[dev]": pytest, xdist, coverage, ruff, build, twine
make check          # what CI checks: lint + format, tests, generated docs, links, names, version
```

`make help` lists the single targets (`make test`, `make lint`, `make docs`, …).
Without `make`, each target is one command in the [Makefile](Makefile).

The suite is hermetic: fixtures redirect `HOME`, the XDG directories and the
state directory into `tmp_path`, unset every variable the packaged defaults
interpolate, and remove the program's own variables (`ICA_MCP_*`,
`MCP_BRIDGE_*`) and the trust-store variables (`SSL_CERT_FILE`,
`NODE_EXTRA_CA_CERTS`, …) a shell may export. It needs no credentials, no
network and no Claude installation, it passes on openSUSE's and Debian's CA
layouts alike, and it cannot touch your real `~/.claude`. If a test of yours
needs any of those, it is testing the machine rather than the code. A test that
genuinely needs the internet is marked `@pytest.mark.network`; those are
excluded by default and run with `pytest -m network`.

To try a change against reality without changing anything:

```bash
python3 -m initialize_claude_ai run --dry-run
```

## The two rules the whole codebase is built on

Both came from the project's original brief, and a change that breaks either is
not going to be merged however good the rest of it is.

**1. Nothing is hardcoded.** Every value — a path, a timeout, a command line, a
URL template, a retry count, a signal name — is a key in
`initialize_claude_ai/data/*.toml` with a comment saying what it is for, and is
overridable from the environment. Adding a constant to a `.py` file is almost
always the wrong move; add a setting and read it.

Configuration layers, later winning over earlier: packaged defaults → system →
XDG → `./initialize-claude-ai.toml` → `--config` → `--env-file` →
`INITIALIZE_CLAUDE_AI_<SECTION>__<KEY>` → `--set`.

**2. No credential value appears in code, logs, reports, command lines or
generated files.** Secrets travel as *references* and are resolved at the moment
of use. See [SECURITY.md](SECURITY.md) for the details — it documents the
guarantees, so a change that weakens one should update that file too.

## The mistake this codebase keeps making

Every serious bug found in this project so far has been the same bug wearing a
different hat: **what got recorded was not what got proved.** Not a crash, not a
typo — a claim that outran its evidence, which is the failure mode a program
that exists to *verify things* is most exposed to.

The full record — every such failure, what had actually been proved, and the
rule adopted for its class — is in [docs/lessons.md](docs/lessons.md). Read it
before changing a verdict, a check or a writer: most of its rows were "fixed"
once for one server before the class was fixed for all of them.

The habits below follow from that table, and they are worth more than any of the
individual fixes — each one was learned by getting it wrong against a real host:

**Two paths that do the same job share one function.** Agent mode's fallback
called the deterministic stage directly, and was patched one check at a time
(`discard_unregistrable` once, never the job). Steps every stage takes live in
`begin_stage`/`finish_stage`, and checks every verdict needs live where the
verdict is reached (the verifier), so a new path cannot skip them.

**Install for what runs; present is not usable.** What a later step will
start decides what is installed -- the entries Claude Code loads, not only the
program's own templates -- and a component is proved by the check its consumer
needs, not by its file being there.

**Delete only what you can prove you made.** A prefix, a glob, an age or a
shape is a place to look, not proof of ownership; anything removed carries the
program's own marker, and a test fails when a new creation site skips it.

**A late check never ends an address by itself.** Whatever removes the server
that settled an address hands over (`[mcp.handover]`); adding a gate that can
remove one means adding it to that path and testing the hand-over.

**Fix the class where it lives, not the case where it showed.** Most rows above
were "fixed" once already, for the one server, the one variable name or the one
code path that showed them. The next server then hit the same class: role naming
for the secret half of a login only, "try again" for a list of status codes
only, a credential guard tuned to one token. Before writing a fix, name the
invariant that was broken ("the launch tested is the launch shipped", "a launch
without a credential lacks it", "an instruction names what the entry reads"),
enforce it at the one place every path goes through, and test it with a case
unlike the one that found it. A heuristic (entropy, a name pattern) may *flag*
a value; it never alone decides a verdict that breaks an entry. **When the test
harness has to scrub, stub or work around something to stay green, that is a
program bug the harness is hiding:** fix the program, and keep the harness's
guard only as a second line.

**Register what you verified, not a recipe for finding it again.** If a step
resolved something — an interpreter, a URL, a scheme — write the resolved value
down. Anything left to be resolved later will be resolved in a different
process, with a different `PATH`, by someone else.

**A credential must never decide which candidate wins.** `@kubb/mcp` beat
`confluence-datacenter-mcp` because it declared no secrets and so could be
launched, while the correct package honestly declared that it wants
`CONFLUENCE_API_TOKEN` and was disqualified before it ever started. A candidate
punished for describing its own requirements is a scoring bug, not a finding.
Verify as far as the missing credential allows -- a stdio server reads its
token when a *tool is called*, so it will usually handshake and list everything
it has -- then register it provisionally with the slot empty.

**What you register has to outlive the run, and the machine.** `.mcp.json`
exists to be copied — that is why the briefing is not in it. An absolute path
into our own state directory undoes that silently, and a virtualenv we built to
trial a package does not survive even here. Prefer a launcher that fetches what
it needs at start-up; accept a durable file and *say* it is host-bound; refuse
scratch. See `mcp_auto_discovery/portability.py`.

**One target, one answer — and "answered" includes "asked for a credential".**
The tier order only means something if the descent stops when a tier answers.
It stopped on `verified` alone, which quietly stopped meaning anything once
packages were trialled without their secrets: the honest verdict for a target
whose server wants a token became *provisional*, so every such target was
searched again at tier 2 and tier 3, and `jira.example.com` came back with two
entries from an ordinary run. When you change what counts as a *result*, go and
look at everything that branches on the old word.

**A limit needs to be spent where it is meant to apply.** `max_provisional = 1`
read as one entry per target and was enforced once per stage, which with four
stages is not a limit at all. A cap belongs at the scope it describes —
`TargetOutcome.absorb`, not `StageOutcome.note_provisional`.

**Say a thing once.** A name assembled from two places — the agent that
discovered the server and the writer that registers it — will say the address
twice unless one of them checks. `McpWriter._compose` is that check, and it is
deliberately conservative: it drops a *whole* repeated address and never merely
the labels two hosts have in common, because trimming the overlap turns the
second host of a pair into `mcp-deepwiki-com-devin-ai` and the name stops
naming its host. A little repetition beats a name that is wrong.

**An entry has to carry an address, and it has to be the right one.** The only
question anyone asks of a line in `.mcp.json` is *is this pointed at my server?*
The server's name proves nothing — this program chose it. So an entry must
contain an FQDN, URL or URI, and that address must be the target's own host or
one that answered as an MCP endpoint for the target.
`mcp_auto_discovery/grounding.py` enforces it at write time. It is deliberately
a dumb invariant rather than a judgement: the relevance gate is where
intelligence is applied and it let `@kubb/mcp` through, so the second check is
one that cannot be reasoned around.

**But a stricter rule is not a safer one — check it against what actually
exists.** The first version of that gate required a URL's host to *equal* the
target's, and refused `api.githubcopilot.com/mcp/` for `github.com` and
`developerknowledge.googleapis.com/mcp` for `developers.google.com`. Both are
the vendors' own MCP servers; serving MCP from a sibling domain is the ordinary
shape of a hosted offering, and the rule destroyed two working entries on its
first live run. The tell was in the log: the same run had blocked tiers 2, 3 and
4 for `github.com` *because a native endpoint exists at that address*, and then
refused to write it. **When two parts of the program disagree about whether
something is real, one of them is a bug** — and it is usually the newer, more
confident one.

The asymmetry that resolves it: a URL that answered **is** the service, because
we contacted it at that address. A launcher is never probed at an address — the
verifier starts a process and speaks to its pipes — so a handshake proves a
process runs and nothing about where it points. Hence a different host is
believed for a URL and refused for a launcher, and a launcher with no address is
refused however well its name suits the target.

**Don't tidy what you recorded.** Stripping our own `?format=mcp` before writing
looked like honesty — the query came from us, so recording it as the server's
address is circular. But removing it does not undo the probe, it invents a
*second* URL that was never requested. `https://confluence.example.com?format=mcp`
became the entry `https://confluence.example.com`: untested, a login page, and
unfixable by any credential. If a recorded value embarrasses you, the answer is
to refuse the finding, not to edit it into something presentable.

**Test the effect, not the decision.** Both registration gates were covered by
tests, both were green, and neither refusal did anything: `publish` handed every
registration to `_write_json`, which wrote all of them. The tests asserted that
the *verdict* was `DISPOSABLE` or `UNGROUNDED` — which was true — and never that
the entry was absent from the file. A gate is not a check on a dataclass field;
it is a check on what ends up on disk, so assert on the bytes.

The same mistake had a second half: `skipped` meant both "we refused to write
this" and "the CLI step did not run", so the refusal reason was overwritten by
`the claude CLI is not installed` and the operator was told neither. One field,
two questions, is how a decision disappears. They are now `refused` and
`skipped`.

**Believing in something is a commitment to act on it.** The tier gate closes
tiers 2, 3 and 4 the moment due diligence records a native endpoint. That is
correct, and it is only correct if the endpoint then *becomes* a candidate.
Once it did not: the registry published `developers.google.com`'s endpoint on a
sibling domain, a host filter dropped the URL before anything probed it, step D
recorded it regardless, and the address finished having refused every
alternative on the strength of an entry nobody wrote. The log contained all
three facts and no one of them was wrong on its own.

Two habits follow. **A filter on the discovery path is a decision about what may
be registered**, so it answers to the same standard as `grounding.py` — and the
answer to "is this URL related to the target" is provenance, not hostname.
**And any belief strong enough to close a tier is checked before the target is
given up on**: `TraditionalPipeline.reconcile` probes whatever the checklist
recorded, and if it still cannot be reached it says so, rather than leaving an
empty result that looks like an honest "nothing found".

**A shared method must not stamp its owner's identity on the caller's work.**
`settled()` and `gate()` live on `TraditionalPipeline` because both pipelines
obey one descent rule — that part is right. But they built their skip as
`StageOutcome(mode=self.mode)`, so an agentic run's skips were labelled
`traditional`, and a log showing five of them read as "the agent gave up here"
in a run where the agent had driven every stage it executed. When a method is
shared deliberately, check what of the owner leaks into the result.

**Do not answer a question that does not apply.** `Mode` says *how a stage was
carried out*; a skipped stage was not carried out, so every possible value is
false and the honest output is none. Same for the tally: counting work nobody
did, in a mode nobody used, made the one figure that says how much the agent
did untrue in the direction that mattered.

**Moving a check earlier changes the question it can answer.** The grounding
rule was moved from publish to ingest so an agent could act on it — a good
change that broke a working one. Two of its tests rest on whether the candidate
answered, and at ingest nothing has been dialled yet: `answered_as_mcp` was
false for everything, so every off-host URL was refused *before it could be
probed*. Google's own endpoint went that way three times in one run, GitHub's
once, and the run produced no `.mcp.json` at all.

Before relocating a predicate, list its inputs and ask which of them exist at
the new call site. Where one does not, the honest answer is **undecided**, not
**no** — `assess(..., probed=False)` returns `UNDECIDED`, which is registrable,
and the real verdict is taken later where the evidence exists.

And note what let it through: every test attached a handshake before calling
`assess`, so all of them exercised the publish-time world. **A check that runs
at two call sites needs a test per call site**, driven through the real path
rather than by calling the predicate directly.

**A failure is not a diagnosis.** `credential_pending` was set whenever a
declared secret was missing, regardless of whether the server had answered — so
a process that died on startup was recorded as "a real MCP endpoint that refused
an anonymous client", became provisional, settled its stage, closed the tiers
below it, and wrote an entry whose `${...}` nobody can fill. When a thing fails
while some condition happens to hold, the condition is not thereby the cause.
Say what happened; do not promote a coincidence to an explanation.

**A variable an operator must export needs a name they can use.** `HEADERS` is
what `mcp-graphql` reads, and as a name for the operator's own environment it is
useless: generic, collides with every other server in the file, and says nothing
about which service wants it. The *key* is the server's and must not change; the
variable it points at is ours to name, so it becomes
`${SMELT_EXAMPLE_NET_HEADERS}`. A documented vendor variable
(`GITHUB_PERSONAL_ACCESS_TOKEN`) keeps its name — renaming that surprises
everyone who already exports it.

**Do not infer belonging from a hostname.** Three times I added a test for
"does this address belong to the target", each cleverer than the last, and each
refused working servers: `developerknowledge.googleapis.com` (Google's own, ×5),
`api.githubcopilot.com` (GitHub's own, ×4), `api.opensuse.org` (openSUSE's own,
×2), `bugzilla.fastmcp.app`. Hostnames cannot answer it — `api.githubcopilot.com`
is the vendor's and `bugzilla.fastmcp.app` is a stranger's, and nothing in the
two names says which.

What *can* answer it, in descending order of strength:

1. **The entry names the target.** `--bugzilla-server https://bugzilla.example.com`.
   Strongest, always sufficient, and the form to push agents towards.
2. **The run's own evidence.** A host it found answering as this target's MCP
   endpoint.
3. **The same organisation.** `api.opensuse.org` and `build.opensuse.org` share
   a registrable domain: one service's API host and its web interface.
4. **The server said so.** What the running server advertises after the
   handshake — its `instructions`, its tool and prompt descriptions, its
   resource URIs, the defaults in its input schemas — `server_said_so`. Below
   the two above only because they are cheaper and readable without running
   anything; as *evidence* it is stronger than either, being the server's own
   account of what it does.
5. **The vendor said so.** The target's own documentation, its `.well-known`,
   or a registry entry returned for this target — `vendor_said_so`.

And what cannot: a keyword search. That is how `@kubb/mcp` became Confluence's
server, and how `bugzilla.fastmcp.app` — which will serve *any* Bugzilla and
must be told which — looked like an answer. A multi-tenant host with no
instance named in the entry is refused, and rightly.

**Ask the thing itself.** Every test above reads the *entry* — a command line,
an environment value, a URL. That is what somebody configured. Meanwhile the
server has been running, has completed a handshake, and has described itself:
`instructions`, tool and prompt descriptions, resource URIs, the defaults in its
input schemas. All of it was collected and then thrown away except the bare
names.

It is better evidence than anything in the entry, because it is the running
server saying what it *does* rather than a claim about what it was configured
to do — and it is what separates the two candidates the entry-only rule could
not tell apart. An Open Build Service server lists resource URIs under
`https://build.opensuse.org/…`; `@ateam-ai/mcp`, which reached that target on an
npm keyword search, lists nothing of the kind. Both were refused, and
`build.opensuse.org` ended the run with nothing.

So `server_said_so` joins the list, above vendor provenance and below the entry
naming the target (which is cheaper and readable without running anything). The
same instinct as *prefer a control to a list*: ask a question whose answer you
can check, rather than reasoning about names.

Two guards keep it from being a way in. It matches by **host**, through
`same_service`, so `build.opensuse.org.evil.test` in a description is not the
target. And it only reads a handshake that **completed** — evidence from a
session that did not happen is not evidence.

**Evidence may not be its own source.** Number 2 above is only evidence about a
*different* candidate. `endpoint_hosts` is built from the tier-1 checklist and
from the candidates themselves, so a URL candidate was normally inside its own
`served_by`, and the rule read that back as corroboration:
"`mcp.atlassian.com` is served from `mcp.atlassian.com`, which this run found
answering for the target." A claim restated is not a claim confirmed. Only the
candidate's own address is withdrawn, and only for a URL — a bridge relaying to
a host the endpoint stage found separately is corroborated by a genuinely
different finding, which is what `served_by` is for.

**Number 5 has to say *whose*.** "Documentation" matched *Atlassian's*
documentation while the target was `confluence.example.com`: the vendor of the
product describing the vendor's own multi-tenant cloud, which does not serve a
customer's private installation of it. `vendor_said_so` exists to answer *who
said so*, and it was not asking. It now requires the target's own organisation
to appear — in the clause making the claim, or in the address itself, which is
where a hosted offering usually puts it. That keeps `api.githubcopilot.com` for
`github.com` and refuses `mcp.atlassian.com` for somebody's self-hosted wiki.

This is not the hostname inference the section above warns about. Belonging is
still decided by the probe and by the provenance marker; the hostname decides
only *whose claim it is*, which is the one question a hostname can answer.

**A hypothesis must not forbid the work that would test it.** The real damage at
`confluence.example.com` was not the wrong entry — it was that "tier 1 exists"
closed tiers 2, 3 and 4, so `mcp-atlassian`, which would have worked, was never
looked for. An unproven endpoint on another organisation's domain no longer
closes them. A *proven* one still does, wherever it lives.

**A check that cannot be acted on is a veto, not a check.** The grounding rule
ran once, at publish, after every stage had finished. So when an agent's bridge
for `developers.google.com` verified with three tools and was refused for
carrying no address -- one environment variable's worth of repair -- the agent
had two attempts left and no way to learn of it. The address ended with nothing.

Ask of any new gate: *who could fix this, and are they still listening?* If the
answer is an agent, the check belongs where its retry can read the reason
(`_drop_ungrounded`, feeding `_correction`), not only where the entry is
written. Keep the late check too -- it is the last word, and the deterministic
pipeline has no retry to feed -- but the early one is what makes it repairable
rather than merely fatal.

The same reasoning applies to a stage's own success. A stage must not settle on
a candidate that will be thrown away later: "we found a server" and "we can
write this server down" are different questions, and letting the first one pass
while the second fails is how an address reports success and produces nothing.

**Sharing the constant is not sharing the rule.** The habit below was already
written down, and the very next fix broke it anyway. The selector guard that
spares `BUGZILLA_AUTH_MODE` went into `is_secret_name`; the agentic pipeline
imported the same `SENSITIVE_NAME` object and called `.search()` on it directly
in two places, so the fix reached only `--no-agent` runs. The test asserting
both pipelines use the same regex *object* passed the whole time — because they
did. What diverged was the decision made with it.

Import the predicate, not the pattern. `test_mcp_secret_classification.py` now
parses the package and fails if any module outside `secret_policy` calls
`SENSITIVE_NAME.search()`, which is the version of this habit a reviewer cannot
forget.

**A rule that only one pipeline enforces is not a rule.** Phase 4 has two
implementations — the agentic one and the deterministic one `--no-agent` uses
and every stage falls back to. Anything that decides what reaches `.mcp.json`
has to live where both can reach it, or the weaker path becomes the real policy.

This went wrong exactly once and it is worth remembering how. The
credential-classification primitives lived in `agentic.py`, so they judged what
a *subagent* claimed. The deterministic pipeline answered the same question by
reading the package registry's own `isSecret` flag — a third party's opinion
about our second rule. A registry entry saying

```json
{"name": "Authorization", "value": "Bearer sk-live-…", "isSecret": false}
```

would have had its value copied verbatim into the file, while the identical
header from an agent was dropped. One rule, two strengths, and the weaker one on
the path that runs when there is no agent supervising.

They now live in `mcp_auto_discovery/secret_policy.py` and both import it — and
a test asserts the two modules hold *the same objects*, not merely matching
regexes, because a copy is how a rule quietly diverges again. A flag from
outside is a hint that may only make us more careful: `true` is believed,
`false` is checked.

When you add a check, ask which module it belongs in. `writer.py`, `probes.py`,
`grounding.py`, `portability.py`, `verifier.py`, `registry_client.py` and
`secret_policy.py` are shared and are usually the right answer;
`agentic.py` is only for things that are genuinely about having an agent — turn
budgets, prompt registers, retry plans.

**Prefer a control to a list.** Faced with "Jira's 401s are meaningless", the
tempting fix is a list of hosts to distrust; it needs a new entry for every
product ever deployed. The fix that generalises is to ask the host a question
whose answer you already know — probe a path that cannot exist — and see whether
it answers differently. `McpProber.control_answer` is that idea, and it works on
a host nobody has heard of. Reach for the experiment before the lookup table.

**When you do not know, say so in the output.** A provisional entry for a
product with no credential profile does not claim the value is a token; it says
the shape depends on the product and to check its documentation. An admission
costs a line and is true of every product nobody has written a profile for — a
confident guess costs somebody an afternoon.

**One sentence per variable, not per server.** The admission above was written
once and shown for everything a server needed. `API_HEADERS` takes
comma-separated `Name:value` pairs; described as "may call it an API token", it
got a bare token, the parser rejected it, and the server exited on start-up.
Guidance that is actionable is guidance that gets acted on, so it has to be
right for the specific thing it names. Ask each variable.

**Where a check runs decides what it can do about the answer.** The entry gate
— *does this actually start?* — was correct from the day it was written and
nearly useless, because it ran at publish. By then every stage had finished, so
it could report a failure and nothing else: the agent's turn was over, and the
tier descent had already happened, which meant a candidate whose entry would
never start still occupied its target's answer and tiers 3 and 4 were never
asked. The check was right and late, and late made it a bystander.

Moving it into the stage loop, beside `discard_unregistrable`, changed nothing
about the question and everything about the consequences: the agent can be asked
to repair, the rules can be tried after it, and a candidate that survives
neither is simply dropped — at which point the lower tiers run *because nothing
settled the stage*. The tier fallback was not implemented. It fell out.

When you add a gate, ask the second question too: **who could act on this, and
are they still in the room?** If the answer is "nobody, it is too late", the gate
belongs earlier. This is the same lesson as *a check that cannot be acted on is a
veto* and *a check nobody acts on is decoration* — three faces of one mistake,
which is why they are all in this file.

**Self-healing is a tier order, not a special case.** The remedies are ranked
the way discovery itself is: the better-informed party first. The agent knows
what it installed and what it guessed at; the rules know the handful of
differences between our launch and the client's; neither is trusted to say
whether it worked. Every repair is re-proved, and one that did not help is
discarded rather than accumulated — otherwise "we tried three things and it
works now" cannot tell you *which* thing, and the entry that ships is one nobody
launched in that exact form.

**Two files generated together must be able to prove they belong together.** A
tier-3 bridge is a script plus a manifest, and nothing checked that the pair
matched. One was regenerated and the other was not; the older manifest had
`path_params` as a list where the newer script called `.items()` on it, and the
bridge died inside `tools/list` with

```
AttributeError: 'list' object has no attribute 'items'
```

which the operator saw as `Connection closed`. Both files were valid. Neither
was wrong on its own. What was wrong was the pairing, and the pairing was the
one thing nothing recorded.

Whenever you emit two artefacts that are only valid together, stamp one with the
identity of the other and check it on load. It costs a field and turns "the
process exited" into a sentence naming both files.

**A traceback is not an error message here.** Anything that escapes a stdio MCP
server reaches the client as a process that exited, and the client reports
`Connection closed` — the same three words for a missing file, a bad manifest,
a syntax error and a refused credential. So validate up front and name the
*field*, not the line; and wrap the entry point so the last thing written to
stderr is one sentence with a cause in it. This is the same lesson as *a failure
is not a diagnosis*, at the other end of the pipe.

**A cap has a direction, and the default direction is usually wrong.** A
trialled server's stderr was bounded by `if len(self._stderr) < 40: append` —
which reads as "keep 40 lines" and means "keep the **first** 40". A traceback
puts the only line that names a cause at the very end, so the cap was a rule
for discarding precisely the part anyone would want. What survived was then
summarised as the last three lines joined oldest-first, taking the tail of the
head: a frame from the middle. `gitlab.example.net`'s bridge really died on
`ValueError: GitLab token is required. Set one of: GITLAB_TOKEN, …` and was
reported as `user_lifespan_result = await stack.enter_async_context(self._…`,
which says nothing at all. When you bound a buffer, say which end you are
keeping and why — `deque(maxlen=…)` at least makes the choice visible.

**A summary is a parser, so it decides what the next rule can know.** Both of
the above would be cosmetic bugs if nothing read them. But the rule deciding whether a
failed trial was a credential wall read the same string, so the summariser
silently set the ceiling on what that rule could ever conclude, and the target
lost a server the bridge stage had already verified end-to-end. This is *a
truncation is a parser* again, one layer up: **before shortening anything, ask
which later decision reads it, and put the load-bearing part where a truncation
cannot reach.**

**Two wrong answers do not make the midpoint right.** A crash during a trial
without a credential used to be recorded as "an endpoint waiting for a token",
which shipped entries whose `${…}` nobody could fill. The fix said a crash is a
failure — and that lost `gitlab.example.net`, because a server is also entitled to
read its configuration, find the variable absent and *say so* before exiting.

Both rules were guesses dressed as policy, and swapping one for the other only
moved which servers were wrong. The way out was not a better guess but a
different question: **ask what evidence would distinguish the cases, and require
it.** Here it already existed elsewhere in the file — the standard that stops an
intranet's blanket `401` from counting as an endpoint. Either the server names a
variable that was withheld, or it says something auth-shaped. When you find
yourself reversing a rule, check whether the two cases are actually
distinguishable before picking the other default; if they are, neither default
was the answer.

**Declared is not read.** A candidate's `env` is what its author remembered to
mention; the script's source is what it actually reads. The openQA bridge read
six variables and declared one, and the five it did not were settled by
fallbacks buried in the code -- including the manifest, which on another machine
resolved to a stale file. When a list of requirements comes from a person or an
agent and the thing itself can be read, read the thing. And a setting the entry
does not name is a setting nobody who copies the file can find, however
sensible its default.

**Check the expansion, do not assume it.** `${NAME}` and `${NAME:-}` look like
the same idea and are not: Claude Code hands an unset bare `${NAME}` to the
server as those literal characters, and does not expand nested defaults at all.
Both facts came from launching an entry under an isolated `HOME` and printing
what the server received. When correctness depends on how another program
interprets a string you write, run that program on it once.

**Data you already fetched is not data the operator has seen.** Phase 3 queried
the backend's model catalogue, used it to choose a model and to check the chosen
one, and put it in the JSON report — so the run held the answer to "which models
can I use?" and never said it. Nobody reads the JSON report during a run.

When you fetch something to make a decision, ask whether the *person* wants it
too. It costs a step and no extra work, and the alternative is a question the
run could have answered being answered later by a failure.

**Say "offered", not "available", unless something called it.** The same
catalogue is a good example of the difference. It listed twelve models, one was
chosen from it, and every later call to that model returned *"not available on
your vertex deployment"* — a publisher catalogue describes what the provider
publishes, not what a given project may call. Both statements were true; only
one of them was the one the operator needed.

Word a report by the evidence behind it. If a list comes from an index, it is
what is *published*; if it comes from a call that succeeded, it is what *works*.
Using the stronger word for the weaker evidence is the same mistake as the table
at the top of this file, in a place that looks like prose rather than logic.

**Distinguish a failure of the request from a failure of the installation.**
Retry classes are about *how to try again* — reword, shrink, repeat, change
approach. There was no class for "trying again cannot help", and the absence
cost a whole run.

`claude-opus-5-5` was not enabled on the operator's Vertex project, so every
agent call returned `404` carrying its own fix: *"Try --model to switch to
claude-opus-5."* That matched none of the classes — note `_TRANSIENT` matches
**unavailable** and the text said **not available**, one word apart — so it fell
to `other`, whose remedy is another attempt. Three attempts a stage, four stages
a target, thirteen targets, each costing a real agent invocation to be told the
same thing. Every target degraded to the deterministic pipeline in silence, and
`.mcp.json` was never written.

Ask of any failure class: *could a different attempt produce a different
answer?* If the answer is no for the whole run — a model that is not enabled, a
credential that is refused, a quota that is gone — the remedy is to stop, latch
it, and say so once. `AgenticPipeline._runtime_unusable` is that latch, and
`available()` honours it for every later stage and target.

**A truncation is a parser.** The same run had the answer in its output
twenty-nine times and never legibly, because the capture took the *last* 600
characters of a JSON document — which is guaranteed to begin mid-key:

```
ackground":0,"foreground":0,"unset":0,"started_in_background":0,...
```

The classifier that should have caught the 404 was reading that slice, so the
unreadable message and the missing class were the same bug twice. When you cut
text you did not produce, ask what shape it is first: parse it and take the
field, and fall back to slicing only for text that has no structure — a stack
trace, where the tail really is the informative part.

**When you relax a rule, carry its justification with you.** Trialling a package
without its secret was a good fix for a real bug: a stdio server reads its
credential when a tool is *called*, so one launched without it still handshakes
and *still lists everything it can do* — far more than refusing to start it
teaches. The branch that recorded the result was written from that reasoning and
then tested only the first half of it: `handshake.ok and a secret was missing`.
Its own hint said "handshaked and **listed its tools**", and nothing checked
that it had listed any.

So a server that started and advertised nothing became "a real MCP endpoint that
refused an anonymous client" — provisional, settling the target, closing tiers 3
and 4, and shipping an entry naming a variable the package does not read. The
operator supplied it and still had no tools, because the credential was never
the missing piece.

The tell is that the justifying sentence contains a condition the code does not.
When you write "this is safe *because* X", make X an assertion. Here it is one
line: the credential-wall branch now requires that something was advertised, and
an empty listing is reported as "started, exposed nothing, cause unknown" —
which lets `accepts()` say the true thing and lets the lower tiers run.

**A check nobody acts on is decoration.** This one is worse than the veto it is
the mirror of. `claude mcp get <name>` is the only check in phase 4 performed by
the *real client*, connecting exactly as the next session will — the README
calls it "the second opinion that matters". It returned `Check("claude", False,
"Claude Code reports: failed to connect")`, that value was appended to a list,
rendered as a column, and read by nothing:

```
connected  tools  two-way  usable  starts  claude
yes        yes    yes      -       yes     no
```

Two servers shipped from that run. The phase said `PARTIAL`, which reads as
"mostly fine", and the operator found out in the next session.

*A check that cannot be acted on is a veto* warned about running a gate too
late to repair. This is the other failure: running it at the right time and
then discarding the answer. When you add a check, follow the value — if nothing
downstream branches on it, you have added a column, not a gate. Grep for the
field you just set.

And when two checks disagree, **say so** — that is usually the most useful
sentence in the report. `starts: yes` beside `claude: no` means the entry runs
when this program launches it and not when the client does, which narrows the
cause to the environment and the working directory. Left as two ticks and a
cross, the operator has to infer it.

**Test the artefact, not the thing it came from.** Every gate in phase 4 judged
a `Candidate`. Nothing launched the `.mcp.json` entry. They are not the same
object — the candidate is trialled with the environment discovery assembled, the
entry ships with `${VAR}` placeholders that may resolve to nothing — and three
servers passed every check and then died on start-up in the gap between them.
`verify_as_written` closes it by launching what is about to be written.

The habit generalises past MCP: when a program derives an artefact and then
verifies the derivation, it has not verified the artefact. Ask what the user
will actually run, and run that.

**A capability wired to one caller is a capability the others do not have.**
`BridgeBuilder.ca_bundle` detected the host's trust store and argued for it
well — *"it never weakens verification: it is the set of authorities this host
has been told to trust, which is a superset of a vendored one."* Entirely
correct, and reachable by exactly one thing: the bridges this program writes
itself. Every published package it registered, which is most of what it
registers, got nothing — so a generated bridge could talk to a host behind an
internal CA and a tier-2 package could not.

Nothing was wrong with the code. What was wrong was where it lived. When you
write a helper on a class, ask who *else* needs that answer; if the honest
answer is "the other pipeline, or the other kind of output", it belongs in a
module both can import. This is the sibling of *a rule that only one pipeline
enforces is not a rule*, and it is harder to spot, because there is no second
implementation to diverge from — just an absence.

**Refusing the wish is half the job; answer the need.** The same area had a
strong, correct rule: variables that switch TLS verification off are stripped
from every entry. It had been enforced for months. But an operator reaching for
`NODE_TLS_REJECT_UNAUTHORIZED` is not being reckless — they have a server that
will not start and a message, `Connection closed`, that mentions no certificate.
Removing the flag without supplying the trust store left them with the same
broken server and one fewer option.

When you find yourself blocking something, look for what the person was trying
to achieve and whether the program can do it properly. A prohibition with no
supported path is a prohibition people route around.

**Stop enumerating kinds; name the category.** The portability rule was fixed
four times, and the first three fixes were all the same mistake: each named one
more *kind* of path. A root for the bridge bundle. Then `ICA_MCP_PYTHON` for the
interpreter. Then `ICA_MCP_{LAUNCHER}` for `uvx` and friends. Each was correct,
each shipped with tests, and each was followed by a report of a path it did not
cover — most tellingly a certificate bundle in a third-party package's
`OPENQA_VERIFY`, which sat under no directory we owned and matched no binary we
knew.

The category was always "an absolute path in an entry", and the honest rule is
that *all* of them are facts about one machine. The fourth fix says so, and the
only remaining question is what to call each variable.

When you find yourself adding a third special case, that is the signal. Ask what
the cases are instances *of*, and whether the general rule is affordable. Here
it was almost free, because an unset `${VAR:-default}` yields exactly the
original string — so applying it too widely costs nothing, while applying it too
narrowly costs a broken entry on somebody else's machine. Check for that
asymmetry: when being wrong in one direction is free and wrong in the other is
not, the general rule is the cheap one.

A related tell: the argument for the narrow rule was "a variable per system file
would be noise in a file people read". It sounded like judgement and was
untested, and the example it rested on — `/etc/ssl/ca-bundle.pem` — is
openSUSE's spelling, not Debian's. An aesthetic objection outranked a
correctness one. Notice when that is happening.

**Adding a variable is changing the command.** It is tempting to think an unset
`${NAME}` is inert. It is not: the process gets a variable it did not get during
the trial, and `mcp-bugzilla` exited over one. If the proven launch did not
carry it, do not write it — name it in the briefing, and let the next run,
with the variable exported, trial it and write the slot it proved. A placeholder
is a claim that a value is required; sometimes all you have is a name.

**Name the kind, and let configuration list the members.** SMELT was missed
because discovery knew one kind of API description, and lost its agents because
it knew no wording for one expired credential. In both cases the fix is not to
add GraphQL, or Google Cloud, to the code. It is to make the category a TOML
table (`[mcp.discovery.api_kinds.<name>]`, `[mcp.agent.credential_failures.<name>]`),
so the next kind is a table and not a patch. A new API kind needs a recogniser
that cannot be fooled by a page that merely exists (`success_markers`); a new
credential profile needs a refresh that never prompts.

If you are fixing something in this family, start with
**`tests/test_mcp_regression_corpus.py`** — the real hosts that broke this
program, with the responses they actually sent and what must be concluded from
them. Alongside it: `tests/test_mcp_auth_evidence.py` (the Jira and
service-desk 401s, verbatim), `tests/test_mcp_bridge.py` (the interpreter and
trust store), `tests/test_mcp_provisional.py` (credential shape, the retry loop)
`tests/test_mcp_settings_reachable.py` (every setting the code reads is one the
defaults define) and `tests/test_mcp_refusal.py` (a refused entry is absent from
the file on disk, for both gates).

## House style

- **Comments explain why, not what.** The code says what it does. A comment
  earns its place by recording the reason a non-obvious choice was made — the
  failure it prevents, the alternative that was tried. Several modules are
  deliberately heavy on module-level docstrings for the same reason: the design
  reasoning lives next to the code, not in a wiki.
- **Test names are sentences.** `test_an_endpoint_that_asked_for_a_credential_settles_the_stage`,
  not `test_provisional_2`. The suite doubles as the specification.
- **A new timeout setting needs a fourth thing**: a decision about whether
  `--timeout-scale` applies to it. The rule is a suffix match on
  `timeout_seconds` (`run.timeout_scale_suffixes`), so naming it
  `*_timeout_seconds` opts it in and any other name opts it out — which is why
  the run deadline is `run.deadline_seconds` rather than
  `run.timeout_seconds`. If it is a duration that must mean exactly what was
  typed, or a threshold rather than a limit, keep it out of the suffix and add
  it to the unchanged list in `tests/test_timeout.py`.
- **A new setting needs three things**: the default with its comment in the
  right `data/*.toml`, an entry in `.env.example`, and a mention in `README.md`
  if an operator would ever reach for it. Check the indentation of the first of
  those — a key written one table too far down belongs to that table, the code's
  own fallback answers instead, and nothing fails.
  `tests/test_mcp_settings_reachable.py` now catches exactly that, by requiring
  every literal key the source reads to exist in the defaults.
- **A flag that sets one number is a row, not code.** `--agent-max-attempts`,
  `--agent-max-turns` and `--timeout-scale` are all rows in `cli.SETTING_FLAGS`.
  Each row names the flag, the setting, the type and the lowest accepted value.
  The parser, the bounds check and the override are all built from that one
  row. Written out by hand, each copy is a chance to allow a bare form by
  accident, to let 0 through, or to misspell the key. `tests/test_cli.py` checks
  every row: the key must exist, a bare flag must be refused, a missing flag must
  write nothing, and a value below the minimum must be refused. Mark a row
  `agent_only` when only the phase-4 agent reads it, so `--no-agent` warns that
  it has no effect. Then add the flag to the README's flag table; a test checks
  that too.
- **A flag's `--help` group is derived, not chosen.** Phase groups are built
  from the registered phases (`phase N (slug)`). A `SETTING_FLAGS` row lands in
  the group of the phase whose `enable_key` is the first segment of its key; a
  key outside every phase goes to `run control`. Put hand-written flags in a
  titled group too: argparse's untitled "options" group reads as "no phase", and
  a test allows only `-h` there. `initialize-claude-ai --help` prints every
  command's groups from the parsers themselves, so a new flag needs no list
  updated. `tests/test_cli.py` checks both.
- **A command gets its timeout from configuration, always.** A phase-2
  backend reads `self.timeout("<command>")`, which looks the command's name up
  in `[auth.<backend>.command_timeout_seconds]` beside its template; anything
  else reads a `*_timeout_seconds` key of its own section. A literal
  `timeout=30.0` cannot be changed without editing code and escapes
  `--timeout-scale` altogether. `tests/test_timeouts_configured.py` refuses one.
- **A code fallback is the shipped value.** `float_("x", 15.0)` is only
  acceptable when the TOML also says 15; otherwise the code and the defaults
  answer differently depending on whether the key is found. "Neutral" is not an
  exemption: a `0` turn budget, a `0.0` ceiling and an empty URL were each a
  second default. An empty fallback is accepted only where mirroring would copy
  a table, a long list or a paragraph. `tests/test_config_fallbacks.py`
  resolves every read through the `section(...)` it was made from and compares
  the fallback, implicit ones included, with the packaged default.
- **What the program judges by is configuration, not code.** Word lists,
  wordings, regular expressions, suffix lists and size windows that decide a
  verdict -- what counts as a credential (`[mcp.secret_policy]`), how an agent
  failure is classed (`[mcp.agent.failure_patterns]`), which suffixes are public
  (`[mcp.grounding]`), how much of a reply is read
  (`mcp.discovery.response_scan_chars`) -- live in TOML with the code's
  constant as the equal fallback. A judgement the callers cannot reach settings
  from is configured once per run by phase 4, which both modes run
  (`secret_policy.configure`, `grounding.configure_suffixes_from`), and is then
  asked through a predicate, never a pattern imported at load time, which would
  keep the packaged value. Prefer what the machine already knows (the system's
  public suffix list, `shutil.which`, a glob over architectures) to one name
  written down. The only exceptions are values needed before settings load
  (`platform_detect`), each with a comment saying so.
- **One helper per job.** `config.fill` expands `{token}` placeholders,
  `config.truthy` reads an on/off string, `fileio.write_private` writes
  anything that may hold a credential (created `0600` before the first byte,
  replaced atomically), and `types.clip` shortens a finding. A second copy is a
  second place for the rule to drift.
- **A skill's JSON example uses only keys the code reads.** The agent follows
  the example to the letter, so a key the pipeline ignores is a finding thrown
  away. `tests/test_mcp_agent_vocabulary.py` checks every `due_diligence` key in
  every `SKILL.md` example.
- Line length 120, import order enforced; `ruff check` is the arbiter and its
  configuration is in `pyproject.toml`.

## Where things live

[docs/development.md](docs/development.md) describes the layout of the package and the tests; `tools/` holds the checks CI runs. The two files that carry
most of the reasoning are `mcp_auto_discovery/policy.py` (the tier order, and
why it is enforced rather than suggested) and `core/config.py` (where every
value in the program comes from).

## Pull requests

CI enforces the shape of a pull request, so it is worth knowing before you push:

- **Exactly one commit.** Squash your branch before opening the pull request
  and whenever you update it (`git reset --soft origin/main && git commit`, or
  `git rebase -i`), then `git push --force-with-lease`. Merge commits fail.
- **A title and a body.** The first line says what changed (72 characters at
  most), a blank line follows, then a body says why — what failure the change
  prevents. Trailers such as `Signed-off-by:` do not count as a body. "Fixes a
  bug" tells a reviewer nothing; "a bridge registered as `python3` starts under
  a different interpreter when Claude Code launches it" tells them everything.
  Check locally with `make commits` (`python tools/check_commits.py origin/main HEAD`).
- **Green CI.** Lint and format (`ruff`), the tests on Python 3.10–3.13 and on
  openSUSE Tumbleweed, the package build and install, the secret and
  internal-name scan, and the generated-docs, link and version checks. The
  single required check is **CI passed**. `make check` runs the same locally.
- **Tests with the change.** New behaviour has a test; a fix has a test that
  failed before it. One concern per pull request.
- **Docs with the change.** If you add or change an option or a setting, run
  `make docs` and commit `docs/cli-reference.md` / `docs/config-reference.md`.
  A user-visible change gets a line under `## [Unreleased]` in
  [CHANGELOG.md](CHANGELOG.md).
- **No internal names.** Examples use `example.com`, `example.net` and
  `/home/user`; `tools/check_names.py` fails on the names it guards (stored as
  fingerprints only).

Pull requests are merged by squash, so `main` gets one commit per pull request.
Releases are described in [docs/release-process.md](docs/release-process.md).

By contributing you agree your work is licensed under the
[MIT License](LICENSE). Please follow the [Code of Conduct](CODE_OF_CONDUCT.md).
