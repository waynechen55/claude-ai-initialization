---
name: mcp-strategy-synthesis
description: Work out how a target could be reached over MCP after direct discovery, package search and bridging have all failed, and either set it up or hand back a concrete plan. Use as the last stage of MCP discovery.
---

# When nothing obvious worked

Three stages have run before you. There is no endpoint on the host, no package
someone already wrote, and no API description to generate a bridge from. Your
job is to be inventive about what is left, and to be honest about which of those
ideas you can actually execute here.

You are the stage that turns "we found nothing" into "here is what to do".

## Start by reading the record — including the checklist

You are given what the earlier stages learned: the paths tried, the responses,
the documentation that was found, the packages that were rejected and why, and
the four-step tier-1 checklist (`due_diligence`) saying how thoroughly the
target's own endpoint was ruled out. Read all of it before proposing anything.
Half of a good strategy is noticing what the earlier stages saw but couldn't use
— a 401 on `/mcp` is not "no endpoint", it's "an endpoint you need to
authenticate to"; a rejected package that targets the SaaS version of the
product may work fine against self-hosted with a base URL override.

**If the checklist has a gap, closing it is your first proposal and possibly
your first action.** A step marked `unavailable` because no search tool was
available, or a set of paths nobody reached before the stage timed out, is a
cheaper and better lead than any workaround: the ordering exists because the
target's own endpoint beats everything you can propose here. Say plainly in your
summary whether tier 1 was genuinely ruled out or merely not finished.

## Angles worth working through

Go through these deliberately rather than picking the first one:

- **Auth was the blocker, not absence.** If any request returned 401/403, or a
  `.well-known/oauth-protected-resource` document exists, the server is there.
  The task is obtaining a credential — an OAuth flow via `mcp-remote`, a personal
  access token from the product's settings page, a service account. Such an
  endpoint is already written into `.mcp.json` with its credential slot empty,
  so your proposal is *where this operator gets a token for this product*, named
  as concretely as you can manage (the exact settings page, the exact scope) —
  not "configure authentication".
- **A generic server already covers it.** The target speaks a protocol somebody
  wrote a generic MCP server for: HTTP/REST, GraphQL, PostgreSQL, MySQL,
  SQLite, S3, Git, filesystem, SSH, Kubernetes. A generic server pointed at the
  target beats a bespoke one you'd have to maintain. When the record carries
  `api_kinds` — the target serves GraphQL, say — the generic servers
  configured for that kind were already trialled in tier 2; read why each
  failed (a missing variable, a version) before proposing another. A kind is
  listed there only after a 2xx answer in its own form, plus a control request
  to a path that cannot exist that was *not* answered the same way. Never infer
  a kind from a page that merely repeats the path you asked for. Never propose
  an unproven candidate in place of a registration that answered: it is held
  back (`mcp.registration.keep_answering_existing`). When the kind has
  documented operations, the run builds that bridge itself (its credential
  sent the documented way) before the address is settled. A strategy of your
  own competes with it on proof, not on order. A `401` from a bridge whose way
  of sending the credential nobody checked is not a verdict on the credential
  (`mcp.verification.login_check.unconfirmed_rejection_settles`). A `404`
  for every path with the credential, while there's a `401` without it, is an
  account with nothing behind it on that service; no strategy fixes it, so
  report it. A strategy succeeds only when its server passes the usage
  contract (`[mcp.contract]`): the kind's tasks and the operator's own checks,
  then Claude Code itself using the written entry. Start from the entry that
  last worked (`[mcp.known_good]`) and from the login method the service really
  offers: a client-only challenge (`Signature` at api.example.net) means the
  product's client, never a header or token server. The caller generates that
  client bridge itself (`[mcp.discovery.clients.*]`) and repairs correctable
  failures (`[mcp.repair]`). Your strategy is for what neither covers. The
  client bridge never uses the client's setup on this machine; the same user
  name and password may work on several servers. A server whose reads work
  with no credential is working (its credential optional). A setting nobody
  sets is never written as a bare `${NAME}`. A login is proved only by a call that fails without it (a public
  `get_about` proves nothing). A refusal identical to an anonymous one may mean
  the credential was never sent (an unread variable, an unimplemented auth
  type, a header under the wrong name), so check how it's sent before asking
  for a new one.
- **The interface is a CLI, not an API.** If the product ships a command-line
  tool, a thin stdio server that shells out to a fixed allowlist of subcommands
  is a small, reviewable amount of code.
- **The data is a file or a database.** Logs, a SQLite file, a config tree, an
  export endpoint. Point an existing filesystem or database MCP server at it.
- **Wrong layer.** Maybe MCP belongs in front of the thing behind the target —
  its database, its message queue, its object store — rather than in front of
  the target itself.
- **Upstream has it and you're behind.** Check whether MCP support exists in a
  newer version, a beta channel, a plugin, or an enterprise tier. "Upgrade to
  4.2 and enable the `mcp` feature flag" is a perfectly good answer.
- **Ask the vendor.** If there is genuinely nothing, an open issue or a feature
  request is a real outcome. Say so rather than inventing something unworkable.

## Execute what you safely can

Prefer doing over describing, within limits. You may:

- send read-only requests and run searches;
- install and launch an MCP server package to test it;
- write files into the artefacts directory the brief names for this address
  (other addresses may be worked beside you in the same workspace, so a shared
  name is not yours to use, and the skills directory and other addresses'
  context files are read-only to you).

Do not change the target system, do not write outside your workspace, and do not
install anything system-wide. If the caller is in dry-run mode, execute nothing
that mutates and say which steps you skipped.

Anything you do execute, verify the same way every other stage does: launch it,
send `initialize`, then `notifications/initialized`, then `tools/list`. A
handshake is the only evidence that counts.

## Writing the plan

For everything you could not execute, leave instructions the operator can follow
without you. That means:

- concrete commands, not "install the appropriate package";
- the actual names of environment variables that need values, and where the
  operator gets each value from — never a value itself;
- what success looks like, so they know whether it worked;
- effort and prerequisites, honestly stated.

Order the proposals by expected payoff. Two well-argued options beat eight
generic ones. If you think the right answer is "this target isn't a good fit for
MCP", say that plainly and explain why — it is a legitimate finding and it saves
the operator a week.

## Rules

- **"Does as well without it" means doing the job, and what's needed stays per
  machine.** Dropping a runtime setting because the server still *started*
  replaced smelt-example-net's working mcp-graphql on a machine whose network
  needed it for every call. Now:
  - a setting is dropped only when usage tasks and real calls give the same
    result without it, and an unknown job keeps it (`unknown_job_keeps`);
  - a kept setting is read from a per-machine variable whose value is in that
    machine's settings (`per_machine`);
  - an `http://` endpoint moves to `https://` when it answers there, and a
    token is never sent in the clear (`prefer_https`);
  - an unknown login type ranks below one the address offers
    (`unknown_below_offered`);
  - a challenge seen before counts when a host gives none this time
    (`remember_challenges`).
  - a launcher's momentary failure ("npm error enoent" on a racing cache) is
    launched again, never taken as the entry not starting (`launcher_retries`).

- **Every setting needs a reason.** smelt-example-net carried
  `NODE_OPTIONS=--network-family-autoselection-attempt-timeout=30000` from an
  old entry: 5 s on the machine that ran the program, 53 s on the next, past
  Claude Code's 30 s. Now:
  - every candidate is launched once without its runtime-tuning settings, and
    they're dropped when the server does as well (`runtime_tuning`);
  - the final file check flags a setting holding a wait at or past the
    start-up limit (`flag_start_hazards`);
  - one placement rule serves every file mover, so nothing nests again.

- **A place is earned by proof; one comparison decides.** Two wrong entries an
  earlier version wrote kept their places: a proxy that is "! Connected · tools
  fetch failed", and a core-Jira server for a service desk's customer API. Now:
  - a warning status is not connected;
  - an existing entry that proved nothing keeps no place over a candidate that
    ranks above it (`keeping_needs_proof`);
  - every chooser ranks by the same evidence: proof, history, the API it is
    built for, the login the address asks for (`one_comparison`);
  - an entry sending a login the address offers nowhere is reported as such,
    never with "export X" (`say_unoffered_login`).

- **Judged where no credential is at hand.** A machine with nothing in Claude
  Code's settings env can prove no login, and three working servers were once
  replaced there by ones that never could log in. Now:
  - a refusal stated in text ("401 Client Error") is a refusal, never a pass
    (`failure_text_patterns`);
  - a path an entry names is a setting, and trials launch with it
    (`paths_are_settings`);
  - the login plan ranks waiting candidates, and a scheme the address offers
    nowhere goes last (`unoffered_last`);
  - an API kind any pipeline detected stays known, so its bridge is built
    before a waiting generic server settles the address;
  - moved files are found before an entry is judged (`repoint_before_trial`);
  - an adapter short of a login gets its slot, optional
    (`slot_when_short_of_login`);
  - every data file an entry names is copied beside `.mcp.json`
    (`entry_files_beside_file`).
  - a launcher (npx, uvx, python) is never read as the server's source
    (`launcher_names`);
  - a passed task only the account can pass (`needs_login`) proves the login.

- **A login is never thrown away to make something start.** gitlab-example-net's
  mcp-graphql was proposed with `HEADERS` as one variable holding a whole JSON
  object. Empty, the server would not start, and the repair removed the slot, so
  the token had nowhere to go. bugzilla-example-com's new entry worked anonymously,
  and the copy that carried the API key was then replaced by it. Now:
  - a credential slot holds the credential itself, inside the form its server
    takes (`credential_env`), reading the token the address already holds;
  - a repair never removes a credential slot (`keep_credential_slots`);
  - a login an existing entry supplies is carried into the new entry, optional
    (`carry_supplied_credentials`);
  - the final file check reads every entry as Claude Code will run it on this
    machine (`check_as_run`), and the report lists what travels with `.mcp.json`.

- **A setting is proved by the tool that reads it, on the binary that runs.**
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

- **A login wall is evidence, and every route and key is tried.** sd.example.com came to
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

- **Every address that has a server ends with an entry in `.mcp.json`.**
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

- **Belonging is judged again as evidence arrives, and a kept entry is written.**
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

- **An address identified only partly settles nothing; a replacement must prove more.**
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

- **Judge by the address's job, and use what the operator holds.** build-example-net
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

- **"Try again later" is not a verdict, and one server is one entry.** A
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

- **Its tool definitions must be ones the client can load.** Every tool name and
  input property key must be a plain identifier -- letters, digits, `_`, `.`, `-`,
  at most 64 characters (`mcp.verification.client_name_pattern`). openSUSE's
  `osc-mcp` declares a property named `path_repository jsonschema:` on
  `search_packages`; once that tool is loaded, the API refuses *every* request of
  the Claude Code session. Look at its `tools/list` before you propose a server;
  the caller checks it too and refuses such a server by default
  (`mcp.verification.schema_problems`).

- **A server you would recommend is a candidate, not a sentence.** If you know
  the server -- a package, a launch line -- put it in `candidates` with every
  variable it reads: the address in `env`, every part of its credential in
  `secret_env` under the exact names it reads (`osc-mcp`: `OSC_MCP_USER` and
  `OSC_MCP_PASSWORD`, not "OBS_USER and OBS_PASSWORD"). Only a candidate is
  trialled; a recommendation in prose can only be reported. build.opensuse.org's
  strategist named the right server in prose three times and it was never written.

- Never invent a package, endpoint or flag you haven't confirmed exists. A
  plausible-sounding wrong command is worse than an honest gap.
- Never put a credential in a command, a file or your output. Name the variable
  and its source. Every variable a proposal depends on must be named explicitly —
  the operator cannot export something you only alluded to.
- Distinguish clearly, in every claim, between what you verified and what you
  are proposing.
- The tier order holds for your proposals too: the target's own endpoint first,
  then something published, then something written here. A proposal that starts
  "write a custom server" while an unexplored path or an unauthenticated
  documentation page remains is out of order, and the operator will follow it.
- Name anything you propose registering after the service's fully qualified
  domain name — `bugzilla-example-com`, not `bugzilla`. Short names collide, and a
  collision points a working configuration at the wrong host. The registered
  name is composed from the address plus yours, and a whole repeated address is
  dropped rather than written twice.
- **An endpoint you believe in must be proposed, not merely recorded.** Listing
  a native endpoint in `due_diligence` closes the lower tiers for the whole
  address. If it is not also a candidate, nothing proposes it for registration,
  and the address can end empty having refused every alternative on your word.
  A URL only in your reasoning is not read at all. `developers.google.com` finished exactly that way.
  An endpoint the record believes in is probed before the next stage; one that
  does not answer, or is not this target's (a name match is not evidence), is
  ruled out and closes nothing.
- **Anything you propose registering must contain an address** — an FQDN, a URL
  or a URI. The server's *name* is not one: this program chose the name, so it
  proves nothing. An entry with no address is refused before it is written,
  because nobody can tell what it connects to. `npx -y @kubb/mcp` was registered
  as the MCP server for `confluence.example.com`, and nothing in that entry could
  ever have reached Confluence.
- **For a launcher that address must be the target's own host**; for a URL it
  may be a different host, provided that address answered as an MCP endpoint
  when probed. A vendor often serves MCP from a sibling domain — `github.com`
  from `api.githubcopilot.com` — and a URL that replied is its own evidence,
  whereas a launcher is never probed at an address and so has none.
- **A different host must still be the target's.** A sibling domain of the
  target's own organisation, or an address the target's organisation publishes,
  is; a word that merely contains the product's name (`examplep` is not `example`), a
  registry namespace `io.github.<account>` (the publisher's account, not the
  vendor) or a URL path is not. One endpoint proposed for several unrelated
  targets is refused for all of them.
- **Recommend nothing that only a claim vouches for.** A server is the target's
  when where it is, what it is configured for, or what it says it is names the
  target. A scout's report, a registry listing or an earlier registration is a
  claim; if the server's own answer names something else — or calls itself a
  marketplace or catalog of other tools — it is not this target's, and an
  existing entry marked NOT THIS ADDRESS'S is never kept or recommended — it is
  retired in this run if this program gave it its name, and otherwise reported
  with the command to remove it.
- **Read a failure before retrying it.** A message saying a model is not
  available, a credential is refused or a quota is gone describes the machine,
  not your request — repeating the request spends minutes to be told the same
  thing, and these errors usually name their own fix. Report it plainly and stop
  rather than burning your attempts on it.
- **Ask a candidate what it serves, and quote the answer.** Once a server is
  talking it describes itself — `instructions`, resource URIs, tool
  descriptions, input-schema defaults — and that is stronger evidence of
  relevance than any package name. `@ateam-ai/mcp` handshaked perfectly for
  `build.opensuse.org` and had nothing to do with it: it named no address and
  advertised no resource on that host. If nothing in the handshake mentions the
  target and nothing in the entry names it, you have a package rather than an
  answer — say so, rather than letting it close the tiers beneath it.
- **A certificate failure is a finding with a fix, not a dead end.** A runtime
  that ships its own trust store — Node, anything using certifi — cannot reach
  a target behind an internal CA even where the host trusts it perfectly well.
  `build.example.net` answered `curl` with `HTTP 200` and gave Node
  `UNABLE_TO_GET_ISSUER_CERT_LOCALLY`; the operator saw `Connection closed` and
  no mention of a certificate. Report the issuer (`openssl s_client … | openssl
  x509 -noout -issuer`). The caller names the host's trust store — or the run
  bundle it verified the target with — in the entry;
  never propose switching verification off, which is the opposite act and is
  stripped anyway.
- **Give every path absolutely, and let the caller make it portable.** A
  binary, a certificate bundle, a script, a data file — report the real
  absolute path in `command`, `args` or `env`, and do not invent `${VARIABLE}`
  names of your own, which the caller cannot resolve or check. It rewrites
  every absolute path into `${VARIABLE:-<that path>}` itself, so the entry
  works unchanged here and moves by an export. Nor should you shorten a path to
  a bare name to look tidy: a bare `python3` resolved to a different
  interpreter with a different trust store, and every call failed on TLS.
- **Propose the launch you actually ran, and no more.** Every variable you add
  to `env` is part of the command; an unset `${VAR}` is a *different* command
  from the one you tested. Two servers were proposed with one extra variable
  each, handshaked in the trial, and exited during start-up on the operator's
  machine — `claude mcp list` said `failed · Connection closed`. If you did not
  launch it with the variable, do not put the variable in.
- **Name every variable the server reads, and every file it opens.** A script
  you write is scanned and its reads declared for you, with their defaults; a
  package is not. Put each setting in `env` with the value you launched it with,
  and each companion file -- config, CA bundle, manifest -- under the variable
  it is read from, by absolute path (the caller makes paths relocatable). A file
  found only by a fallback in the source is a setting nobody can find when the
  entry is copied.
- **A listing is not a login.** With a real credential exported, the caller
  calls one read-only tool (`mcp.verification.login_check`) and reports the
  credential as accepted, rejected, or without API access. A plan step that
  says "verified" must not rest on `tools/list`; say how the login was proved,
  or that it was not.
- **A setting's form is evidence, not a guess.** Scheme or bare host, trailing
  slash, API host or web host: take it from the server's own default or
  documentation and name the source. The caller proves it with read-only calls
  and tries other forms when one answers "misconfigured".
- **The operator's configuration is not the entry's.** A server that reads a
  dotfile (oscrc, netrc, `~/.config/<tool>`) before its entry must be given a
  configuration scope of its own; a failure identical with and without the
  credential is not a credential wall. Build servers from current upstream
  and check `tools/list` before proposing one.
- **An empty answer is not access.** A listing that returns `[]` proves nothing
  for a restricted account; plan a check that must return the account's own
  data, and prefer the API the account can see into when access to the first
  stays unproved.
- **No API access means another API.** A valid credential whose account is sent
  away from one API (a portal, a 403) may still reach another on the same host
  -- sd.example.com's customer account reached `/rest/servicedeskapi` and not
  `/rest/api/2`. Plan for the API the account can use; the caller keeps
  searching (`no_access_settles = false`) and can bridge an API kind from its
  documented `operations`.
- **Never copy a value from an existing entry or file**, under any name or as a
  default; header-list variables take `Name:value` pairs (the caller checks the
  shape and gives the exact form); prefer servers that take credential parts by
  name. Kinds are `stdio` / `url`.
- **The credential in use is the one to judge.** A token written into an
  existing entry (a literal, or a `${NAME:-default}`) is checked in memory with
  one read-only call (`mcp.verification.login_check.check_in_use`), never copied.
  A rejected one needs a new token on that instance; never repeat its value.
- **One credential, one instance.** Two instances of one product (jira.example.com,
  sd.example.com) need two credential variables even when the package reads one
  key; the caller writes `${<SLUG>_<KEY>}` for the second
  (`mcp.registration.shared_secret_template`). Name the instance each token is for.
- **Not everything that sounds like a credential is one.** A variable whose last
  word is `MODE`, `TYPE`, `SCHEME`, `METHOD`, `FORMAT`, `STYLE`, `KIND`,
  `STRATEGY`, `ALGORITHM`, `VERSION` or `FLAVOUR` (packaged as
  `mcp.secret_policy.selector_words`, which is what the program applies)
  selects between named alternatives — `BUGZILLA_AUTH_MODE` is `apikey`/`basic`/`anonymous`. Put it in
  `env` *with the value it should hold*, never in `secret_env`. If you do not
  know the value, leave it out and say so: an empty placeholder is a claim that
  a value is required, and all you had was a name.
- **Give each secret its own `shape`.** One server's two variables can want two
  different kinds of thing. `API_HEADERS` takes comma-separated `Name:value`
  pairs; described with the generic "may call it an API token", it received a
  bare token and the parser rejected it.
- Batch your investigation; you have a turn budget, and the proposals are worth
  more than the exploration that produced them.

## If you are asked to repair an entry

You are also the agent asked to fix a `.mcp.json` entry that does not start —
for a working server or one only waiting for a credential, from any stage. The
brief gives you the entry exactly as written, the failure, how the launch that
was proven differs from it (variable names and directories, never values) and
what discovery knew about the server.

- The server answered during discovery, so the service works; change the
  **entry** — a variable, an argument, an interpreter, an absolute path, a file
  a bridge generated — not your opinion of the service.
- The entry is launched the way the client launches it: its own `env` plus a
  narrow passthrough, from the client's directory, with a stand-in in any
  credential slot the trial filled. Nothing from your shell carries over.
- A failure that says "try again later" (429, 502/503/504, a rate limit) is
  not a fault in the entry; the caller does not ask you about one. If the entry
  works when asked again, your fix is not kept.
- Return the corrected candidate in `candidates`, as the brief's output format
  says. You do not decide whether it worked: it is launched again and kept only
  if it starts, and then it *is* the candidate — the entry written at publish
  is built from your launch. If you cannot fix it, return no candidate; the
  deterministic repairs run next, then the next proven candidate is tried.

## If you are being retried

The prompt will say so, and it will list what went wrong last time: which
candidate did not complete a handshake and with what error, which one was
discarded before it was tried. That list is the point of the retry. Do not send
the same candidates back unchanged, and do not restate the same conclusion in
different words.

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

```json
{
  "summary": "the recommended route in one sentence, and whether you executed it",
  "candidates": [
    {
      "kind": "stdio",
      "name": "target-example-com-postgres",
      "command": ["npx", "-y", "@modelcontextprotocol/server-postgres"],
      "env": {"PGHOST": "db.target.example.com", "PGDATABASE": "target"},
      "secret_env": {"PGPASSWORD": "env:TARGET_DB_PASSWORD"},
      "transport": "stdio",
      "source": "generic Postgres server pointed at the target's own database",
      "confidence": 0.6,
      "notes": ["verified: handshake ok, 4 tools"]
    }
  ],
  "findings": {
    "blocker": "the target has no HTTP API; its only interface is a CLI",
    "executed": ["installed and handshook the postgres server"],
    "not_executed": ["OAuth enrolment — needs an operator with admin rights"]
  },
  "due_diligence": {
    "grounding": {"done": true, "detail": "vendor changelog and issue tracker read",
                  "evidence": ["changelog 4.2 adds /api/v2/mcp"]}
  },
  "proposals": [
    "Obtain a read-only PAT from Settings → Access Tokens, export it as TARGET_TOKEN, then re-run discovery — the 401 on /mcp indicates an endpoint is already there.",
    "Upgrade the target to 4.2+, which ships an MCP endpoint at /api/v2/mcp per the vendor changelog."
  ]
}
```

`findings` is free-form: it is kept in the report and handed to the next
attempt. Only a few keys are read -- `findings.artifacts` is listed in the
report and `findings.api_kinds` names the kind of API the target serves. Anything else the caller must act on belongs
in `candidates`, `due_diligence` or `proposals`.

Even when you have no candidates, the `proposals` list must not be empty. Being
out of ideas is not an acceptable output; saying "MCP is not a good fit here,
because X" is.

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
