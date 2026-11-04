# Security policy

## Reporting a vulnerability

Please report security issues **privately**, not as a public issue: use
[GitHub's private vulnerability reporting][advisory] on this repository.

[advisory]: https://github.com/waynechen55/claude-ai-initialization/security/advisories/new

Include what you did, what happened, and what you expected. If a credential of
yours was exposed by the behaviour you are reporting, rotate it first — do not
send it to us.

## What this program does with credentials

It is worth being explicit, because this is a tool whose whole job is handling
other people's credentials.

**It never stores a secret value of its own choosing.** Everywhere a credential
is configured, the configuration holds a *reference* — `env:NAME`, `file:/path`, `keyring:service`,
`command:...`, `gcloud-adc`, `gcloud-token`, `prompt:` — and the value is
resolved at the moment it is used. A bare string is read as `env:`. `literal:`
exists for testing and is refused outright under `--secure`.

**Nothing secret reaches disk, a log, a report or a command line.**

- Values are passed to child processes through the environment at launch, never
  as arguments, so they do not appear in `ps` output to other users on the host.
- Everything printed or logged goes through a redactor; the JSON report is
  redacted on the way in.
- `.mcp.json` records credentials only as `${NAME}` placeholders. A discovered
  endpoint that needs a token is registered with the slot **empty** and the
  variable named, so supplying it is an `export` rather than an edit.
- **The one place a value is written is Claude Code's own private settings
  `env`** (mode 0600): the user's `settings.json`, or with
  `provision.scope = "project"` the project's `.claude/settings.local.json`,
  never the shared `.claude/settings.json`. Only three kinds of value go there:
  a credential the operator pasted into `.mcp.json` (moved out of it), one an
  earlier entry supplied, and a value kept out of the file because it *looks*
  secret although nothing proved it is (`Candidate.kept_out`). The settings
  writer's backups are written 0600 beside the file, and `.gitignore` covers
  them and every copy of `.mcp.json`.
- Whether a value is a credential is decided by evidence: its slot's name, an
  inline credential, or its use on this machine. A value that only looks random
  is kept out of the file and kept working, and is never asked of the operator;
  one equal to the server's own default is configuration.
- A trial launched *without* a credential withholds every credential it is not
  given from the environment it would inherit
  (`mcp.verification.withhold_unsupplied`), so a token in the operator's shell
  never makes a server look public.
- A slot is written only where the launch that was *proven* carried it. A local
  server is a process, and one started with an extra unset variable is running a
  different command from the one that was tested — `mcp-bugzilla` was proven to
  start without `BUGZILLA_AUTH_MODE`, written with it, and exited on start-up on
  the operator's machine. Variables the trial did without are named in the
  briefing instead, and the next run, with them exported, trials the server
  carrying them and writes the slot it proved. Nothing is lost; the claim waits
  for the evidence.
- Every finished entry is launched exactly as written before the run says
  anything about it, with `${NAME}` and `${NAME:-default}` resolved the way the
  client resolves them (`mcp.registration.verify_as_written`). A failure is
  reported rather than hidden — but also rather than used to discard a server
  that is genuinely there.
- A candidate proposed with a credential baked into its command line is
  discarded, not sanitised.
- Every absolute path in an entry — a binary, a script, a certificate bundle, a
  data file — is written `${VARIABLE:-<the path here>}`. That is a portability
  measure rather than a secrecy one, but it matters here too: a path is a fact
  about one machine, and `.mcp.json` is a file people commit. Naming the
  variable lets the next reader point it somewhere of their own instead of
  inheriting yours. An unset variable yields exactly the original string, so
  nothing changes where it was written.
- A declared HTTP header whose name or value carries a credential is dropped
  from the entry and replaced by advice to name it through
  `mcp.discovery.auth.bearer_token_ref`.

**Everything from outside is untrusted input, and by the same rule.** Phase 4
takes descriptions of servers from two kinds of outsider — a Claude subagent,
and public package registries — and neither is trusted with the question *is
this a secret?*. Values under credential-shaped names become references,
reference schemes are checked against a known list, and every proposed server
must complete a real MCP handshake before it is registered.

A registry marks its variables and headers with `isSecret`. That flag is
somebody else's judgement about the guarantee above, so it is treated as a hint
that may only make the program **more** careful: `true` is believed, `false` is
checked against the same test applied to an agent's output. The test itself
lives in one place, `mcp_auto_discovery/secret_policy.py`, because a rule
enforced at two strengths is enforced at the weaker one — and the weaker path
here was the deterministic pipeline, which is what runs when there is no agent
supervising.

Sharing the constant is not sharing the rule, and that distinction cost a fix.
The agentic pipeline imported the same pattern object and then matched it by
hand, so a later refinement reached only `--no-agent` runs while the test
pinning both to the same object kept passing. Modules ask
`is_secret_name()` / `holds_a_credential()`; a structural test parses the
package and fails if anything outside `secret_policy.py` matches the pattern
itself.

**Erring towards "secret" has a cost too, and it is not zero.** A variable
wrongly filed as a credential has its value thrown away and replaced by a
placeholder nobody can fill. `BUGZILLA_AUTH_MODE` selects *how* to authenticate
— `apikey`, `basic`, `anonymous` — and `auth` is a whole word in it, so it was
classified as a secret, emptied, and the operator was told to export it "with
your real token". The server exited during start-up. A name whose final word
selects between named alternatives (`MODE`, `TYPE`, `SCHEME`, `METHOD`,
`FORMAT`, …) is configuration and keeps its value; `AUTH_HEADER` and `API_KEY`
are untouched. The guarantee is about secret *values*, and a variable that holds
no value is not protected by emptying it — only broken.

**Third-party code is executed only deliberately.** Phase 4 can launch a package
someone else published in order to test it. That is a mutating action: it is
skipped in `--dry-run`, it can be turned off with `mcp.packages.trial_run =
false`, and it runs in the sandbox directory rather than your working tree.

**TLS verification is never silently disabled.** A probe that fails on the
certificate is retried once with verification off *for discovery only*, and an
endpoint found that way is flagged `insecure_tls`, penalised in the ranking and
reported with the remediation — it is not registered as though it had verified
normally. `mcp.discovery.tls_insecure_retry = false` removes even that.

**The trust store is the answer, and now it is supplied rather than suggested.**
Telling an operator "use the trust store, not a flag" only works if something
does. A runtime that ships its own certificate store — Node's, certifi's —
cannot reach a target behind an internal CA even where the host trusts it
perfectly: `build.example.net` answers `curl` with `HTTP 200` and gives Node
`UNABLE_TO_GET_ISSUER_CERT_LOCALLY`. The server exits during start-up and the
operator is shown `Connection closed`, which mentions no certificate at all —
so the pressure to reach for `NODE_TLS_REJECT_UNAUTHORIZED` comes from a
failure that never explains itself.

Every registered stdio entry now names this host's trust store in the variable
its runtime reads (`mcp.trust`). That is the opposite of weakening:
`NODE_EXTRA_CA_CERTS` is additive — it can add authorities and never remove
one — and the bundle it points at is the set this host has already been told to
trust. The need is answered so the wish stops being tempting.

Nor can a registered entry carry that decision forward. Variables that switch
certificate checking off — `NODE_TLS_REJECT_UNAUTHORIZED`, `PYTHONHTTPSVERIFY`,
`GIT_SSL_NO_VERIFY` and the rest of `mcp.registration.tls_weakening_env` — are
stripped from the entry with the trust store named as the remedy. `.mcp.json` is
meant to be copied, and a file that quietly disables TLS on whichever machine it
lands on is not something to hand anyone. One reached a real entry as
`"${NODE_TLS_REJECT_UNAUTHORIZED}"`, an unset placeholder that would have done
nothing until the day somebody exported that name for an unrelated reason.

## What is out of scope

- The security of the services you point the tool at.
- Credentials you place in your own shell, `.env` file or configuration.
- Anything a bridge generated by phase 3 of MCP discovery does against a target
  you configured it for. Generated bridges are read-only by default
  (`mcp.bridge.generated.safe_methods`); widening that is your decision.

## Supported versions

This project is pre-1.0. Security fixes land on `main` and ship in the next
release; only the latest release on
[GitHub Releases](https://github.com/waynechen55/claude-ai-initialization/releases)
is supported, and there are no maintained release branches.

| Version | Supported |
| ------- | --------- |
| latest 0.x release | yes |
| older releases | no — upgrade |

Each release lists SHA-256 checksums of its files (`SHA256SUMS`); verify a
download with `sha256sum -c SHA256SUMS --ignore-missing`.
