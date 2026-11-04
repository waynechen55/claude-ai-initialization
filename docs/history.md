# Development history

The detailed record of how the program reached its first release: every
defect found while verifying it against real infrastructure, what caused it,
and the general rule adopted so the whole class could not come back. It was
the changelog during development; release-by-release notes are in
[CHANGELOG.md](../CHANGELOG.md).

Host names here are neutral examples (`example.com`, `example.net`); the
failures they describe were real.

## Changes after the first tagged snapshot

### Fixed

- **A launcher is not the program** -- bugzilla-opensuse-org was written to run
  `/root/.local/share/uv/tools/mcp-bugzilla/bin/mcp-bugzilla`, a script whose
  first line runs that `uv tool`'s private Python. Copied to another machine it
  never started (ENOENT; pointed at the system's Python: "No module named
  mcp_bugzilla"). An installer's private launcher (a `uv tool` or pipx
  environment, an npm global module -- `[mcp.launch_form.families]`) is now
  launched by its portable form, the package pinned to the installed version
  through `uvx` / `npx -y`: before each trial (what is trialled is what is
  written), in `entry_for`, in the final check of `.mcp.json`
  (`repair_in_file`) and in phase 1 on every machine the file is copied to
  (`install.portable_launchers`). The private install step is dropped.
- **Installed means runnable** -- every "is it installed" check (the trial,
  phase 1's programs, the final check) asks whether the command can run: it is
  executable, the interpreter its first line names exists, and a Python
  launcher's interpreter imports the module it starts (`import_check`). The
  reason is said: "its first line runs …, which is not on this machine".
- **Honest travel advice** -- a private launcher is never "export X=<where it
  is there>": the report says not to copy it and names its launcher form.
- **Every remove names its scope** -- the setup guidance said "`claude mcp
  remove <name>` undoes it" (and "registered by this run at --scope local" for
  entries only in the file), and phase 4's duplicate advice gave a bare remove;
  without a scope the CLI removes the project file's entry. Both now go through
  the one scoped builder, and a test fails on any other remove text.

- **Every server keeps a way to log in** -- gitlab-example-net (`graphql-mcp-server`)
  and bugzilla-opensuse-org (`mcp-bugzilla`) were written with no credential
  slot: their candidates declared none, both answered anybody, and nothing
  asked what they read a login from.
  - Every stdio candidate with no way to send a login gets the variable its
    own server's code reads one from (`API_ACCESS_TOKEN`, `BUGZILLA_API_KEY`)
    -- one credential read, or the one naming the address's product -- else
    the one key the same package's other entries read
    (`[mcp.verification.server_credential]`). It is trialled like a declared
    slot and written optional (`${NAME:-}`) when the server works without it.
    A choice of logins is reported, never guessed.
  - A package's credential reads are withheld from its trial too, so a shell's
    export of the generic name cannot decide it.
  - The final check gives every file entry the same treatment
    (`slot_missing_logins_in_file`).
- **Every entry reads names only it reads** -- bugzilla-example-com read
  `${BUGZILLA_API_KEY:-…}` and a shell's revoked `BUGZILLA_API_KEY` won over
  the key given for it; `${SSL_CERT_FILE:-…}` lost to a login shell's deleted
  /tmp bundle, and every tool failed.
  - One rule (`McpWriter.owned_reference`) at the end of `entry_for`, where
    every path's entry passes: the key the server reads is kept; the variable
    it expands becomes `{ENTRY}_{NAME}` unless it already names the instance
    (its domain's own label, a whole word), is a session variable, or is one of
    this program's (`ICA_MCP_*`, configured roots, operator-configured names).
    A trust bundle that is the machine's own reads `ICA_MCP_CA_BUNDLE`, which
    phase 1 sets per machine (`own_reference_names`, `trust_reference`).
  - A credential Claude Code's settings hold under the old name is kept under
    the new one; a literal credential in the file moves to Claude Code's
    settings env (`move_literal_credentials_in_file`), never over another value.
- **Anonymous is not logged in** -- a success naming no account
  (`"currentUser": null`, `authenticated: false`;
  `login_check.no_identity_patterns`) is an empty answer, never a login
  accepted. A server working without its optional login is reported "public
  answers only -- not logged in", with its own briefing key.
- **"Later" is not an answer** -- identification waits a busy host's
  `Retry-After` (else a doubling pause), asks an undecided address once more
  later in the run (`reidentify_undecided`), and meanwhile uses the product it
  was when it last answered (`remembered_product_when_busy`).
- **Every kind of new terminal** -- `[machine_env.effective_shells]` (login
  shell and terminal window) is the one definition everywhere. Where they
  disagree, a check judges with the value that cannot work; a CA variable
  broken in either is repaired -- by phase 1 too, on every machine `.mcp.json`
  is copied to (`install.machine_paths.repair_trust`).
- **The server's own log names the cause** -- when calls fail, the last
  stderr line matching `[mcp.verification.stderr_causes]` is attached to each
  failed call -- and to a launch that offered nothing. A trust cause, or a
  trust variable in the entry naming a file that is not on this machine (a CA
  bundle path copied from another machine left graphql-mcp-server listing no
  tools), is repaired with the machine's own bundle
  (`mcp.repair.classes.trust_file`), never blamed on the server.
- **The CLI's scopes** -- `claude mcp get`'s "Scope: Project config" is the
  file's own entry, never a CLI copy (`project_scope_patterns`); copies are
  compared with `${X:-default}` read as `${X}` on both sides; every `claude mcp
  remove` names its exact scope, and none runs when no CLI scope holds the
  name -- a scopeless remove took build-opensuse-org out of `.mcp.json`. A
  project-only entry is reported as loaded from the file after approval.
- **A trial reads what the entry will** -- a credential reference to the
  server's own generic name (`BUGZILLA_API_KEY: env:BUGZILLA_API_KEY`) resolves
  the entry's own name; the generic one only from Claude Code's settings, never
  from a shell's export (`CandidateVerifier.resolve_owned`, URL headers too). A
  name chosen for another key (`EXTRA_HEADERS` from `${BUILD_BASE64}`) is read
  as written. Found by a real run: the trial sent the
  run's exported key for something else.
- **A service about a product is not its server** -- github.com was given
  `githubcard.com/mcp` (a card maker) because its domain begins with "github"
  and its answer names GitHub. A domain merely beginning with the
  organisation's word is the organisation's only when listed
  (`[mcp.registration.identity.organisation_domains]`, `prefix_needs_listing`)
  or vouched for by the vendor by name; on a third party's host with nobody
  speaking for the target, the server's own answer must name the target's
  address (`third_party_needs_host`). A launcher believed only on its own
  words whose every login variable names another organisation
  (`APIFY_TOKEN` for github.com: a scraper about GitHub) logs in there, not
  to the target (`foreign_login_refuses`, `login_generic_words`).
- **A bot check is "try later"** -- openqa.opensuse.org answered its MCP path
  with "Making sure you're not a bot!" for a while; the working https entry was
  judged "does not start" and replaced by a cleartext `http://` endpoint. A
  page in front of a server asking to come back later
  (`reuse_verdicts.try_later_markers`) is transient everywhere and never takes
  away an entry that worked before; an endpoint moves to `https://` whenever
  the https side answers at all (`https_when_it_answers`).
- **Where each value comes from** -- the final check logs, per entry, whether
  each variable comes from Claude Code's settings, a startup file's line, the
  entry's default, or nowhere (`report_value_origins`). Names only.

- **The program's folder may have any name** -- `claude_ai_initialization`
  here, `claude-ai-initialization` once cloned from GitHub, or moved anywhere.
  - A path inside the folder holding `.mcp.json` is written as
    `${ICA_MCP_PROJECT:-<absolute>}/...` (`[mcp.registration.path_variables]
    ICA_MCP_PROJECT = "."`), never for `/` or the home folder
    (`path_root_never`). Phase 1 and phase 4 set it to where the folder is now
    whenever the written default is not there.
  - An existing entry naming a missing path is re-pointed by the path's own
    tail under this program's folders (`bridges/<file>`), whatever the old
    folder was called (`repoint_by_relative_path`). Two matches are never
    guessed between.
  - Per-machine path values (`ICA_MCP_*`) go through the one per-machine
    writer: settings env and the shell profiles, logged place by place.
  - Every check that judges an existing entry as written sees it with its
    files found again (`McpWriter.repointed_entry`). Claude Code's status from
    before the run is set aside when the entry's files only moved. gitlab-example-net's
    bridge had been re-pointed for its trial, then called "does not start" from
    the old path and replaced by an untried package.
  - No folder name is written into the code; the log keeps its name,
    `claude_ai_initialization.log`, in whatever folder the program runs from.
  (`writer.py`, `entry_needs.py`, `tests/test_mcp_folder_name_independent.py`.)

- **build-opensuse-org was left out of `.mcp.json`; `--force-install` left a
  machine without gcloud.**
  - One gate every keep rule passes (`[mcp.registration] one_keep_gate`). An
    existing copy may hold its place only when nothing proved it unusable, it
    is a different server from the candidate (across every copy of the name),
    and it can be written. The keep rule added last round had none of the
    older rules' guards: build-opensuse-org's fresh client bridge was refused
    for an unusable copy of itself.
  - Values an existing entry supplies (a default user name) count for both
    sides when two are compared (`compare_after_carrying`).
  - Never an empty address (`never_an_empty_address`). When the final file
    check finds an address with a server but no entry, it writes the best
    candidate that was held back only for an existing entry's sake, never one
    a gate refused.
  - Deleting needs ownership (`[install] deleting_needs_ownership`,
    `owned_marker`). What a failed reinstall left is removed only when it
    carries this program's marker, never by its name. gcloud's own
    `google-cloud-sdk.staging` was deleted by name and gcloud was gone.
  - A failed step must leave the tool working (`restore_after_failed_step`).
    When it worked before and does not now, a runnable copy the step left
    beside it is put back, and what stood there is kept as
    `<folder>.broken-<time>`.
  (`writer.py`, `install/space.py`, `phases/phase1_install.py`,
  `tests/test_mcp_one_keep_gate.py`, `tests/test_install_space_and_full_disk.py`.)

- **confluence-example-com was written as `https://confluence.example.com/mcp`, a login
  redirect taken for an MCP endpoint; gitlab-example-net was left out of `.mcp.json`.**
  - An endpoint is proved, never claimed. An inconclusive reply plus a claim
    ("the discovery agent says it needs a credential") counts as a credential
    wall only when the reply differs from what a made-up path on the same host
    gets (`[mcp.discovery.auth] claim_needs_control`). Confluence redirects
    `/mcp` to its login page exactly like a path that does not exist.
  - An agent's report of a native or authenticated endpoint is a claim. It is
    recorded and reported (`claimed_by_agent_unproven`) and closes no tier;
    only an endpoint a probe here showed to speak MCP does
    (`[mcp.policy] agent_claims_close_tiers`).
  - Claude Code saying a URL is not MCP ("Unexpected content type: text/html")
    is its own failure class (`not_mcp`). The entry is taken out of
    `.mcp.json` again, the entry it replaced comes back, its URL is ruled out,
    and the address is handed over (`withdraw_not_mcp`).
  - A header credential counts as a value to supply
    (`[mcp.discovery.auth.evidence] count_header_credentials`). The URL entry
    had won "fewer values" against mcp-atlassian.
  - "Connected" holds no place
    (`[mcp.registration] connected_holds_place = false`). Neither does
    "answered", without a copy judged this run. Kept means written: an
    existing entry may keep its place only when a copy of it was judged this
    run and can be recorded in `.mcp.json`, or it is already there
    (`unjudged_is_keepable = false`). gitlab.example.net's bridge had been refused
    for an old CLI copy that this run had discarded for naming a missing file.
  - Of several existing copies sharing a name, one that works or waits for its
    credential speaks for the name before one that failed. A copy that failed
    this run holds no place, and the copy keeping the place replaces it in
    `.mcp.json` (`failed_copy_holds_nothing`). confluence-example-com's failing
    URL copy had spoken for the name over its waiting mcp-atlassian copy.
  - A launcher still fetching what the server needs (a cold uv or npm cache)
    is slow, not broken. A launch that failed while its output showed a
    download is launched once more with `launcher_warm_timeout_seconds` to
    finish (`[mcp.verification] launcher_warming_patterns`). mcp-atlassian "did
    not start" as written while uvx downloaded pydantic-core, and a server
    taking 87 s was written instead.
  (`verifier.py`, `policy.py`, `claude_check.py`, `evidence.py`, `writer.py`,
  `probes.py`, `tests/test_mcp_endpoint_proved_and_kept_written.py`.)

- **openqa-example-net failed in Claude Code with `UNABLE_TO_GET_ISSUER_CERT_LOCALLY`
  on a machine without the internal Example root; the shell profiles there named
  deleted `/tmp` CA bundles.**
  - Trust follows the runtime that opens the connection
    (`mcp_auto_discovery/consumer_trust.py`, `[mcp.trust.consumer]`). A URL
    entry is opened by Claude Code itself, so its trust lives in Claude Code's
    environment, never in the entry. For every https URL entry Claude Code
    cannot verify on its own, a bundle is written beside `.mcp.json`
    (`trust/claude-code-ca.pem`). It holds what Claude Code is already given
    plus each chain up to its self-signed root, and `NODE_EXTRA_CA_CERTS` is
    pointed at it. A bundle it is given that already works is kept; one
    lacking the root is merged, never replaced.
  - One writer for every per-machine variable (`core/machine_env.py`,
    `[machine_env]`). Claude Code's `settings.json` env, `~/.bashrc`,
    `~/.profile`, `/etc/profile.d/initialize-claude-ai.sh`, and every startup
    file that already sets the variable (`scan`) are brought to the same value:
    - a commented line is uncommented;
    - a missing one is added in a marked block;
    - a wrong one is corrected in place, also another tool's;
    - a right one is recognised.

    Files are backed up first. A place the run may not write is reported with
    the exact line. Afterwards a fresh login shell and a fresh interactive
    shell are asked what they end with. Every place and its outcome is logged
    on every run; nothing goes into `.mcp.json`. `MCP_TIMEOUT` and the
    per-machine runtime settings go through the same writer.
  - A CA variable naming a file that cannot work (missing, or holding no
    certificate) is corrected (`repair_broken`). A store-replacing one
    (`SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE`, `CURL_CA_BUNDLE`) goes back to the
    system's own bundle. No variable is added that nobody set.
  - Why Claude Code could not connect is read from `claude mcp list` and
    classed as trust, credential, temporary or server
    (`[mcp.registration.claude_check] read_failure_reason`,
    `failure_classes`). A trust failure gives Claude Code the trust and asks
    again.
  - A check through Claude Code sees what a new terminal's shell profiles
    export (`[mcp.registration.fresh_env] include_shell_profiles`,
    `shell_variables`).
  - Tests never see the developer's own CA variables.
  (`machine_env.py`, `consumer_trust.py`, `claude_check.py`, `writer.py`,
  `tests/test_machine_env.py`, `tests/test_mcp_trust_reaches_claude_code.py`.)

- **build-example-net lost its SSH-signing bridge to a missing key file;
  build-opensuse-org sent Basic to the host that does not ask for it; smelt's
  `NODE_OPTIONS` went on one passing launch.**
  - A key or certificate file in a credential slot is a credential: absent on
    the run machine, the entry is written with its path kept and waits for the
    credential, never refused as "names a file that is not there"
    (`[mcp.registration] credential_files_may_be_absent`, `credential_file_slots`).
  - The login plan ranks by the login's type and the host it is sent to
    together: "asked at the API host" counts only when the candidate's URL
    settings -- or, failing those, the files it names (an osc client's
    `apiurl`) -- reach that host (`[mcp.discovery.auth.evidence] host_matters`,
    `url_setting_keys`, `url_assignment_pattern`).
  - A server waiting for its login whose base URL points at another host of
    the product is tried with the URL on the API host, and switched when it
    does its job there as well
    (`[mcp.verification.address_forms] try_api_hosts_when_waiting`).
  - A bridge driving the product's own client counts as built for the
    address's API, as a bridge generated from its API kind does, so the login
    decides between them: build-example-net's password bridge (OBS kind) had
    outranked the osc bridge signing at api.example.net before the logins were
    compared (`client_bridge_is_built_for_api`, `client_bridge_markers`). A
    waiting server is moved only to an API host that asks for its own kind of
    login.
  - Every waiting candidate is ranked by evidence before any chooser reads it,
    on every path into the slot (`TraditionalPipeline.absorb`, used by both
    modes). A stage holding an unrelated search hit that completed a
    handshake returned before ranking, and confidence plus start time chose
    between build-example-net's osc signing bridge and the Basic one -- a
    different winner from run to run.
  - Each login variable a generated bridge's manifest declares (a client
    bridge's `parts`, a bridge's `auth.variables`) is a credential to supply
    when its entry is read back, whatever its name: osc signing needs only the
    user name, and the signing bridge read back from the entry that last
    worked launched with nothing, was judged "not accepted", and lost to a
    password bridge (`[mcp.discovery.reuse] manifest_login_slots`).
  - An origin that did not answer its first probe is asked once more before
    the login plan is made; still silent, the plan comes from the challenges
    it gave before. build.example.net timed out once under `--concurrency` and got
    no plan, so its waiting bridges were ranked with no login known
    (`[mcp.discovery.auth] reask_unreachable_origin`). The ranking asks for
    the plan rather than reading whatever is there.
  - A runtime setting is dropped only when every one of `drop_samples` launches
    without it does the job as well. When Claude Code's own use fails the way a
    network does after a drop (`restore_on`: "fetch failed", `ETIMEDOUT`, …),
    the setting comes back through the per-machine variable and the check is
    asked again (`[mcp.verification.runtime_tuning] drop_samples`, `restore`,
    `restore_on`).
  (`portability.py`, `writer.py`, `evidence.py`, `verifier.py`, `types.py`,
  `tests/test_mcp_logins_reach_their_host.py`.)

- **A setting smelt's server needed for its job was dropped; openqa-example-net
  was written over plain HTTP; build-opensuse-org lost its Basic login to a
  proxy.**
  - "Does as well without it" is the job: a runtime-tuning setting is dropped
    only when the usage tasks and real calls give the same result without it,
    and an unknown job keeps it (`[mcp.verification.runtime_tuning] unknown_job_keeps`).
    Last round's check compared only the handshake, and smelt's mcp-graphql
    lost the `NODE_OPTIONS` its machine needed for every call.
  - A runtime setting that stays is this machine's: the entry reads
    `${ICA_MCP_<SERVER>_<KEY>:-}` and the value is kept in this machine's
    Claude Code settings env, never in the file (`per_machine`,
    `machine_variable_template`).
  - An `http://` endpoint moves to `https://` when it answers there, so a
    credential is never sent in the clear (`mcp.verification.prefer_https`).
  - An unknown login type ranks below a type the address offers, before
    "values still to supply" (`mcp.discovery.auth.evidence.unknown_below_offered`).
  - A host's login challenge seen in an earlier run counts when it gives none
    this time (`mcp.discovery.auth.remember_challenges`).
  - A launcher's own momentary failure (two `npx` launches racing on a fresh
    cache: "npm error enoent … _npx/…/package.json") is launched again, never
    judged as the entry not starting (`mcp.verification.launcher_retries`,
    `launcher_transient_patterns`).
  (`verifier.py`, `writer.py`, `evidence.py`, `login_methods.py`,
  `traditional.py`, `types.py`, `tests/test_mcp_settings_need_a_reason.py`,
  `tests/test_mcp_place_is_earned.py`.)

- **smelt-example-net could not start within Claude Code's 30 s on the machine its
  `.mcp.json` was copied to.** Its entry carried
  `NODE_OPTIONS=--network-family-autoselection-attempt-timeout=30000` from an
  old entry, never re-judged. It took 5 s on the machine that ran the program
  and 53 s or more on the next.
  - Every stdio candidate carrying a runtime-tuning variable is launched once
    without it. The variable is dropped when the server does as well: same
    tools, same credential wait, starts no slower
    (`[mcp.verification.runtime_tuning]`).
  - The final file check flags a runtime setting holding a wait at or past
    Claude Code's start-up limit (`flag_start_hazards`).
  - One placement rule serves every file mover (`paths_home.server_destination`):
    restoring a lost folder no longer nests it in itself.
  - Agents are told never to add runtime tuning to an entry unless a trial
    without it failed.
  (`verifier.py`, `writer.py`, `known_good.py`, `core/paths_home.py`,
  `tests/test_mcp_settings_need_a_reason.py`.)

- **Wrong entries an earlier version wrote kept their places.** On a machine
  with no credentials, build-opensuse-org kept an OpenAPI proxy that can never
  log in, and sd-example-com got a core-Jira server its customer account is turned
  away from.
  - Claude Code's status is read one way everywhere: a warning
    (`! Connected · tools fetch failed`), "tools fetch failed" or "timed out"
    is not connected (`[mcp.registration] failed_markers`, `degraded_patterns`).
  - A place is kept by proof, never by status. An existing entry that proved
    nothing this run keeps it only while no candidate ranks above it on what is
    known (`mcp.registration.keeping_needs_proof`).
  - One comparison for every chooser: the per-stage pick, the cross-stage slot,
    and an existing entry keeping its place (`types.evidence_key`,
    `mcp.discovery.auth.evidence.one_comparison`). Order is proof, history, the
    API a candidate is built for, having worked here, the login the address
    asks for, then values still to supply.
  - A slot's login type is read from its value's form (`Authorization:Bearer …`)
    and from token-named slots, and the plan's chosen method counts as offered
    (`value_schemes`, `method_schemes`).
  - An entry whose login the address offers nowhere is reported as such, never
    with "export X" (`mcp.registration.say_unoffered_login`).
  - Agents are told that "connected" is not "works", that an existing
    registration that proved nothing is only a candidate, and that a detected
    API type with no published server is served by its bridge, not by the
    product's general API.
  (`claude_check.py`, `existing.py`, `writer.py`, `types.py`, `evidence.py`,
  `40-mcp.toml`, `tests/test_mcp_place_is_earned.py`.)

- **On a machine holding no credentials, three working servers were replaced
  and gitlab got no slot.** With nothing to log in with, order and appearance
  decided.
  - A text answer stating an HTTP failure ("401 Client Error: Unauthorized for
    url: ...", "Request failed with status code 404") is now a failure, and its
    status decides the verdict (`mcp.verification.login_check.failure_text_patterns`).
    mcp-openapi-proxy's anonymous 401 had "passed" the account's own record and
    replaced build-opensuse-org's client bridge.
  - A value that is an absolute path (a key file, a bundle) is a setting, and
    existing entries are trialled with it (`mcp.discovery.reuse.paths_are_settings`).
    The rule lives in the one place every guard asks
    (`[mcp.secret_policy] path_is_never_a_secret`), so the "literal credentials"
    step no longer moves a key path out of the file either. build-example-net's
    SSH-signing bridge had been trialled without its key.
  - The login plan ranks credential-waiting candidates: the scheme is also
    read from setting slots (`signature` from `SSH_KEY`), and a scheme the
    address offers nowhere goes last (`mcp.discovery.auth.evidence.unoffered_last`).
  - API kinds detected by any pipeline of the run are known to every other, and
    stay on the outcome. sd.example.com's customer API had been detected, yet a
    waiting mcp-atlassian settled the address.
  - A file an existing entry names that moved is found again before the entry
    is trialled (`mcp.registration.repoint_before_trial`).
  - An adapter's copy that answered while a task needing the account failed is
    given its credential slot, optional, in the adapter's form. The final file
    check enforces this for every entry, whichever branch kept it
    (`mcp.discovery.reuse.slot_when_short_of_login`).
  - Files an entry names in the state or scratch area (the trust bundle, a
    repair's config folder) are copied beside `.mcp.json` before writing.
    Programs are never copied (`mcp.registration.entry_files_beside_file`,
    `entry_files_max_bytes`).
  - The launcher in front of a server (npx, uvx, python…) is never read as
    the server's source, even when it is a text file
    (`[mcp.env_contract] launcher_names`). Homebrew's npx is JavaScript, and its
    `process.env[key]` had "renamed" gitlab's HEADERS slot to `key`. A key an
    API-kind adapter declares is never renamed.
  - Of two copies with one name, the better proven one is written *and*
    counted. The lesser copy is no longer refused while the better one goes
    unwritten.
  - A usage task only the logged-in account can pass (`needs_login = true`:
    the current user, the account's own record) proves the login when it passes.
    gitlab-example-net read its own user and was still "not working".
  (`login_check.py`, `existing.py`, `secret_policy.py`, `evidence.py`,
  `traditional.py`, `api_kinds.py`, `env_contract.py`, `contract.py`,
  `probes.py`, `types.py`, `writer.py`, `tests/test_mcp_proof_without_credentials.py`.)

- **gitlab-example-net logged in as nobody, and bugzilla-example-com lost its API key.**
  - mcp-graphql was proposed with `HEADERS: ${GITLAB_EXAMPLE_NET_HEADERS:-}`, a
    variable that would have to hold a whole JSON object. Empty, the server
    exited at start-up ("HEADERS must be a valid JSON string").
    `withdraw_optional_reads` then removed the slot, so the token had nowhere to
    go. The held `GITLAB_EXAMPLE_NET_TOKEN` was never given, because the key
    "was already set".
  - A credential slot holding a single bare reference is now written in the
    adapter's `credential_env` form around a token variable: the held one, else
    `{SLUG}_TOKEN` (`mcp.discovery.reuse.credential_in_taken_form`,
    `credential_variable`). Every stdio candidate is given the held token
    before it is judged.
  - What the operator holds is also read from Claude Code's settings.json env,
    not only from this run's shell (`mcp.discovery.auth.inventory.include_claude_settings`).
    The token was kept there, so it was never seen as held.
  - A repair never removes a credential slot (`mcp.healing.keep_credential_slots`).
  - Of two existing copies sharing one name (a logged-in one in the claude CLI,
    an anonymous one in the file), the best proven speaks for the name. When it
    keeps its place, it is the one written to the file
    (`mcp.registration.best_copy_speaks_for_name`). A kept entry proved with a
    credential its file copy has no slot for is written as it was proved.
  - A login an existing entry supplies is carried into the new entry as an
    optional slot, its value kept in settings env, and a CLI copy is replaced
    only once the file's entry carries it
    (`mcp.registration.carry_supplied_credentials`).
  - The final file check reads every entry as Claude Code will run it
    (`mcp.registration.check_as_run`): malformed references, and launched or
    named files that are not there.
  - The report lists what travels with `.mcp.json`
    (`mcp.registration.report_what_travels`).
  - Moving files never nests a server's folder inside itself.
  - Names read through a constant count as read
    (`mcp.env_contract.constant_patterns`, `indirect_read_patterns`).
  (`api_kinds.py`, `healing.py`, `writer.py`, `verifier.py`, `env_contract.py`,
  `phase4_mcp_auto_discovery.py`, `tests/test_mcp_credential_slots_kept.py`.)

- **gitlab-example-net connected without its token, and kept entries were
  reported missing.** Written entries' files lived in the state folder, so a
  copied `.mcp.json` lost them. The entry was then pointed at a script reading
  another variable, and the "script reads this slot" check only ran in trials.
  The verdict counted only what the run registered.
  - Files are kept beside the file (`mcp.registration.artifacts_home = "mcp-servers"`,
    `ICA_MCP_SERVERS` per machine).
  - Every entry's credential slots are checked against its script in the
    final file check (`align_slots_in_file`).
  - Verdicts count the file's entries for an address (`verdict_reads_file`).
  (`core/paths_home.py`, `writer.py`, `known_good.py`, `core/trust_store.py`,
  `phase4_mcp_auto_discovery.py`, `tests/test_mcp_files_travel_and_slots_in_file.py`.)

- **Phase 1 installs for what Claude Code will run, and checks that it works.**
  After the copied `.mcp.json` reached this machine, five entries failed at once.
  Three of them were install problems that phase 1 never looked at:
  - **Programs registered entries run** (`install.demand_from_entries`). Phase 1
    read only its own launcher templates. Now the commands of the entries in
    `./.mcp.json` and Claude Code's local and user scopes, and what a client bridge
    calls (`osc`), are installed from the catalog when missing, or with the
    client's own `install` steps. A program nothing here installs
    (`github-mcp-server`) is named with `--mcp-recheck <address>`. One reader serves
    phase 1 and phase 4 (`existing.loaded_entries`, `claude_config_entries`), and
    one install routine serves both (`client_bridge.install_with_ways`).
  - **This machine's paths for copied entries** (`install.machine_paths`).
    `${ICA_MCP_UVX:-/root/.local/bin/uvx}` fell back to the other machine's path,
    and Claude Code said `ENOENT: posix_spawn '${ICA_MCP_UVX}'` for
    bugzilla-opensuse-org and confluence-example-com while `uvx` was in `/usr/bin`.
    Each program variable an entry reads, whose value here names nothing that
    exists, gets the path this program would name for it. The path must serve
    every entry that reads the variable. It goes into Claude Code's
    `settings.json` env and is never written over a value already set; then
    Claude Code itself is asked whether each entry connects. Files generated on
    the other machine are left to phase 4, with the command that regenerates them.
  - **Present is not usable.** Catalog checks can say `purpose = "usable"`: run
    once the component is there, never a reason to install, PARTIAL with the fix
    from `remedies`. Docker checks `docker info`, and phase 4 no longer trials a
    server whose launcher cannot work: "not launched" with the fix
    (`mcp.packages.check_launcher_usable`); a container candidate had been
    rejected as "not accepted" with the daemon stopped. The agent SDK checks which
    Claude Code it runs (`agent_cli`).
  - **The SDK runs the installed Claude Code** (`mcp.agent.sdk_cli = auto`).
    claude-agent-sdk 0.2.152 ships Claude Code 2.1.259, which answered "does not
    support this model; version 2.1.280 or newer is required", so every SDK
    attempt in agent mode failed first. The SDK is now given the installed
    `claude` (`cli_path`) when it is at least as new as the bundled copy. That is
    the one phase 2 logs in and the CLI runtime runs. Proved here with a real
    call: the bundled CLI failed and the installed one answered.
  - Phase 1's verdict is CAUTION, not SAFE, when a step is PARTIAL.
  (`install/verify.py`, `phases/phase1_install.py`,
  `mcp_auto_discovery/entry_needs.py`, `existing.py`, `client_bridge.py`,
  `runtime.py`, `verifier.py`, `provisioning.py`, `writer.py`,
  `data/10-components.toml`, `data/40-mcp.toml`, `.env.example`,
  `tests/test_install_for_what_runs.py`.)
- **An address is never given up while anything could take the withdrawn server's place.**
  gitlab.example.net vanished from `.mcp.json` a third time. In agent mode the package
  agent proposed nothing, and the deterministic fallback stage trialled
  `mcp-graphql` *before* the address was identified as a GitLab, so it verified
  on "describe the API" alone and settled the package tier. The bridge tier,
  where the working `gitlab-example-net` bridge was registered, was skipped, and
  `mcp-gitlab-python` (waiting for `GITLAB_TOKEN`) was discarded. The final
  re-check then asked GitLab's "read the current user" anonymously, called the
  server one that "no longer answers", and the address ended FAILED with "no
  detail recorded". The cause was that each recent fix had made `mcp-graphql`
  likelier to win the higher tier, while nothing guaranteed a hand-over when a
  later check withdrew it.
  - **The job is known before anything is judged.** One shared stage start
    (`TraditionalPipeline.begin_stage`) runs in both modes, before the tier gate,
    and the verifier asks the address's learner itself when nothing recorded the
    job (`mcp.verification.learn_job_when_judging`). One shared stage finish
    (`finish_stage`) does the rest.
  - **One standard per server per run.** A verdict reached before the address was
    identified is reached again, under what it is, before the choice
    (`mcp.verification.rejudge_stale_verdicts`).
  - **The re-check asks what proved a server.** Only the usage tasks that passed
    are asked again (`mcp.verification.recheck_proved_tasks`). It says "no longer
    answers" only when the server doesn't; one that now waits for a credential is
    kept, waiting.
  - **A withdrawal hands over** (`[mcp.handover]`): another proven server, one
    waiting for a credential (now kept until the address is settled), then the
    stages skipped only because the withdrawn server had settled the address,
    reopened through each pipeline's own stage loop with existing registrations
    first (`reopen_withdrawn`). Once per address, and also after publishing when
    nothing written works. A server Claude Code itself could not use no longer
    settles the descent.
  - **The file check repairs from registrations never tried**
    (`mcp.registration.repair_from_untried_existing`).
  - **Every address names its reason**; "no detail recorded" no longer appears.
  - **Found by this change's own real run:** the "no credential" comparison
    launch dropped the token from the entry's env, and the child inherited it
    back (a Claude Code session exports its settings env to every child). The
    gitlab bridge "answered the same" and its proven login was called "proves
    nothing". This is the leak fixed for trial launches last round, through
    another path: now the one function that builds those launches
    (`_without_credentials`) withholds every name it drops.
  - **Also found by the real run:** two servers were written for one address in
    an ordinary run (`efficient-gitlab`, every call public, and the proven bridge).
    The descent continues past a verified tier only because nothing there proved
    access. A server below whose login a call accepted now takes the place of the
    ones that never proved access (`mcp.discovery.proven_replaces_unproved`;
    off under `--exhaustive-mcp`). This generalises the sd.example.com rule, which
    applied only to API-kind bridges.
  - **Found by the agent-mode real run: the entry read a variable the trial never
    read.** The package agent declared `GITLAB_PERSONAL_ACCESS_TOKEN` as
    `env:GITLAB_EXAMPLE_NET_TOKEN`, the variable in Claude Code's settings. The trial
    read it and the login was accepted, but the writer named the variable from the
    key and wrote `${GITLAB_PERSONAL_ACCESS_TOKEN}`, which nothing sets. A new
    terminal would have started the server with no token (verdict PARTIAL). A slot
    whose reference names another variable that holds a value is now written
    reading that variable (`McpWriter._trial_source`,
    `mcp.registration.read_trial_source`).
  (`traditional.py`, `agentic.py`, `verifier.py`, `writer.py`, `types.py`,
  `phases/phase4_mcp_auto_discovery.py`, the four skills,
  `tests/test_mcp_hand_over_never_empty.py`,
  `tests/test_mcp_shipped_reads_what_trial_read.py`.)
- **The final verdict no longer calls a supplied credential unneeded.** With
  every credential already set where Claude Code reads it, the verdict said "No
  API key, token or other credential is needed by any registered server", even
  though gitlab-example-net's login reads `GITLAB_EXAMPLE_NET_TOKEN`. It now names what is
  already supplied, and says nothing is left to export.
- **Cleanup removes only directories this program made.** It matched `/tmp` by
  name prefix alone. `ica-` is generic, so a real run removed somebody's
  `/tmp/ica-baseline`. No name pattern can tell the two apart: `baseline` even has
  the shape of a tempfile suffix. Every temporary directory the program makes now
  carries an owner marker (`.initialize-claude-ai-owned`, written by
  `diskspace.temporary_directory` and by the client bridge). Cleanup removes only
  marked ones (`run.cleanup.require_owner_marker`) and logs the rest as left
  alone. A test fails when a new creation site skips the marker. Leftovers from
  versions without the marker are no longer removed automatically.
  (`core/diskspace.py`, `cli.py`, every creation site,
  `tests/test_disk_space_and_cleanup.py`.)
- **A value is a credential by evidence, never by how it looks.** gitlab.example.net's
  generated bridge set its User-Agent, `mcp-bridge-gitlab-example-net/1`, to the
  bridge's own default. It scored as random, so the writer's last guard turned it
  into a bare `${GITLAB_EXAMPLE_NET_MCP_BRIDGE_USER_AGENT}` that nothing set and made
  it a credential slot. Claude Code warned "Missing environment variables", and
  the report asked for it to be exported "with a valid token". Every guard that
  keeps a value out of `.mcp.json` now asks one rule,
  `McpWriter.credential_evidence`:
  - *proven* (the name says credential, an inline one, or one already in use) →
    a `${NAME}` the operator supplies, as before;
  - *configuration* when it equals the server's own default, read from its source
    (`mcp.env_contract.fallback_literal_patterns` now also reads
    `os.environ.get("X") or "lit"` / `|| "lit"`, also across a line break);
  - *suspect* when it only looks random → kept out of the file **and kept
    working** (`Candidate.kept_out`). The entry reads `${NAME}`, the value goes
    to Claude Code's settings env when the entry is written, and the start check
    launches with it, so the launch tested is the launch shipped. It is never
    reported as a credential to supply.

  (`mcp_auto_discovery/writer.py`, `env_contract.py`,
  `tests/test_mcp_evidence_not_appearance.py`, `tests/test_mcp_env_contract.py`.)
- **"Without a credential" means without it.** github.com's trial without its
  token inherited the token from the run's environment. `get_me` answered, the
  stand-in then got 401, and the run concluded the token was optional. A trial
  launch now withholds every credential it is not given from what it inherits:
  the pending variables, the slots left out for want of a value, every
  unsupplied credential slot, and each credential-named variable its source
  reads (`mcp.verification.withhold_unsupplied`; `probe_stdio(withhold=…)`). A
  stand-in or real login given on purpose is kept. What was withheld is part of
  the launch's identity, so an answer given with an inherited credential is
  never reused for a launch without it. The test suite had met the same leak and
  scrubbed its own environment; the program now enforces it.
  (`probes.py`, `verifier.py`.)
- **A dropped connection is "try again", never an answer.** `unexpected EOF`,
  `TLS handshake timeout`, `i/o timeout`, `context deadline exceeded`, resets,
  `socket hang up` and transient DNS failures join
  `mcp.verification.login_check.temporary_markers`. They are retried once, then
  inconclusive: never a refused credential, a failed task or a reason to replace
  an entry. `fetch failed` and certificate errors stay a trust store to repair
  (`transport_failure_markers`, `heal_transport`). "No answer at all" still
  counts as a failed call; that is how a hang is recognised.
- **Claude Code's own verdict outranks ours.** A failed check through Claude
  Code is asked once more (`mcp.registration.claude_check.retries_on_failure`).
  Claude Code words a tool's error its own way ("has an output schema but did
  not return structured content"). A target whose entry Claude Code still
  cannot use is written, shown as PARTIAL, and never counted as working
  (`TargetOutcome.consumer_failed`). github-com had been reported "PASSED"
  beside that failure. (`claude_check.py`, `writer.py`, `types.py`,
  `tests/test_mcp_transient_and_consumer_verdicts.py`.)
- **Both halves of a login are named alike, and an instruction names what the
  entry reads.** The role naming (`{SLUG}_{PART}`) covered only the secret half
  of build-example-net's login, so its user name became `BUILD_EXAMPLE_NET_OBS_USERNAME`
  while the operator's settings held `BUILD_EXAMPLE_NET_USERNAME`. The report said
  "needs OBS_USERNAME", which the entry did not read. A name the operator
  already sets (shell or Claude Code settings env) is never renamed.
  (`writer.py`, `tests/test_mcp_login_parts_named_alike.py`.)
- **A URL entry is probed with the header Claude Code will send.** Found by this change's own real run: once the openQA tokens were kept in Claude Code's settings env (as this program does), the entries read `Bearer ${OPENQA_EXAMPLE_NET_AUTHORIZATION}`. Probed with the reference unexpanded, they went out anonymously, got 403 and stayed "waiting for a credential" forever. The report also asked for `OPENQA_EXAMPLE_NET_USERNAME_KEY_SECRET`, which the entry did not read. Header references are now resolved as Claude Code resolves them, from the environment and its settings env (`mcp.verification.resolve_from_claude_settings`, `CandidateVerifier.resolved_headers`); unresolved ones stay anonymous as before. An instruction for a URL entry names what its header reads.
- **One operation's 403 is not the account's.** Found by this change's own real run: confluence-example-com's usage task passed and Claude Code's own check passed, but `confluence_search_user` answered 403. "A refusal outranks acceptance" then called the whole credential "NO API ACCESS" and reported the entry "waiting for a credential", with "set  to use it" and "until  is set" printed with an empty list. A call accepted with content now proves the credential, and a NO_ACCESS with no account-wide sign is a permission limit on that operation (`login_check.content_outranks_scoped_refusal`, `account_wide_markers`). sd.example.com's portal redirect still outranks acceptance, and a 401 still outranks everything. A message never asks for nothing: an entry whose credential is in place says what stands between it and working.
- **The instance an entry serves stays in the entry.** Found by this change's
  own real run: the operator's shell exported
  `BUGZILLA_SERVER=https://bugzilla.opensuse.org`, and the rule "a literal equal
  to what the operator exports is an earlier expansion" rewrote
  `bugzilla-opensuse-org`'s instance URL as `${BUGZILLA_SERVER}`, which was then
  kept in Claude Code's global settings. A value naming a host is never replaced
  by a reference to a variable that isn't namespaced to the entry, and an
  existing reference of that kind is written back as its value
  (`mcp.discovery.reuse.keep_instance_literal`).
  (`existing.py`, `tests/test_mcp_instance_identity_stays.py`.)
- **A credential never goes to a shared settings file.** With
  `provision.scope = "project"`, credentials (and kept-out values) go to
  `.claude/settings.local.json` (`provision.project_local_settings_filename`),
  not the committed `.claude/settings.json`. The fresh-terminal check reads both.
  `.gitignore` now covers every copy of `.mcp.json` and the settings writer's
  backups.

### Added

- **`--mcp-recheck all`** re-checks every entry of the project file: a
  `.mcp.json` copied from another machine is proved on this one (its paths, its
  start-up time against Claude Code's wait, where its credentials are kept)
  without typing every address. An entry's address is the one its last proof
  recorded, else the host it is named after. An entry neither ties to an address
  is named, never guessed (`mcp.recheck_all_word`). (`tests/test_mcp_recheck_all.py`.)

### Changed

- **The endpoint agent configures a client; it does not sweep.** Its brief and
  skill no longer ask it to retry with certificate verification off. The
  deterministic probe already does that once (`tls_insecure_retry`, marked
  `insecure_tls`), and a brief asking a model to sweep paths with verification
  disabled was refused by its safeguards. The agent verifies against the run's
  trust bundle or names the issuer. Checklist step B reads "both schemes tried,
  and a certificate failure explained (its issuer named)"; its key stays
  `bypasses`.

### Added

- **Discovery searches by the kind of API a target serves.** Each
  `[mcp.discovery.api_kinds.<name>]` table recognises one kind (a request,
  `success_markers` that the reply must carry, pages that name the endpoint)
  and lists generic published servers for it. GraphQL (introspection answering
  `__schema`, adapter `mcp-graphql` with `ENDPOINT = "{api_url}"`) and OpenAPI
  ship. Both modes record what was found in `findings.api_kinds`. The package
  stage trials the adapters, and the bridge stage and playbook name the API
  instead of saying there is none. The agents are told the kind and the launch
  (`mcp.agent.api_kind_brief`). An adapter is pinned to its registry's current
  version (`mcp.packages.pin_versions`, `mcp.packages.version_lookup.*`).
  (`mcp_auto_discovery/api_kinds.py`, `tests/test_mcp_api_kinds.py`.)
- **A server already registered on this machine is a candidate at its own
  tier.** Existing entries in `.mcp.json` and in `claude mcp list` / `get` are
  matched to an address by host and verified like anything discovery found
  (`mcp.discovery.reuse_existing`, `INITIALIZE_CLAUDE_AI_MCP_REUSE_EXISTING`,
  **`--no-reuse-existing-mcp`**; `[mcp.discovery.reuse]`). A literal credential
  is replaced by a `${VAR}` reference and never copied. The agents are told
  what was tried and how it ended. (`mcp_auto_discovery/existing.py`,
  `tests/test_mcp_reuse_existing.py`.)
- **A model credential that stops working stops the agents once.**
  `[mcp.agent.credential_failures.<name>]` profiles (`google_cloud`, `aws`)
  recognise the failure, try a non-interactive refresh once per run
  (`mcp.agent.credential_refresh`, `credential_refresh_timeout_seconds`), and
  otherwise latch agents off for the rest of the run. The verdict names the
  addresses that lost them, the remedy, and the command that re-runs exactly
  those addresses. (`mcp_auto_discovery/agent_credentials.py`,
  `tests/test_mcp_agent_credential_breaker.py`.)
- **The endpoint agent proposes auth-walled endpoints** instead of dropping them
  (`mcp.agent.auth_wall_brief`). An inconclusive anonymous reply counts as a
  credential wall only with an independent reason: the candidate's claim, or a
  matching auth profile (`mcp.discovery.auth.credit_inconclusive_with_claim`,
  `profile_is_auth_claim`).

- **One trust store for the whole run** (`core/trust_store.py`). Every
  target's certificate is examined once, before discovery; every HTTP request,
  registered stdio entry, trial launch and generated bridge uses the same
  answer. **`--mcp-trust-ca FILE[,FILE]`** (`mcp.trust.extra_ca`,
  `INITIALIZE_CLAUDE_AI_MCP_TRUST_EXTRA_CA`) adds authorities;
  **`--mcp-trust-pin {targets,never}`** (`mcp.trust.pin_presented_chain`,
  `INITIALIZE_CLAUDE_AI_MCP_TRUST_PIN_PRESENTED_CHAIN`) learns the chain an
  untrusted target presents, for requested addresses and missing-issuer
  results only, kept under `mcp.trust.store_dir`
  (`INITIALIZE_CLAUDE_AI_MCP_TRUST_DIR`) and reported with its fingerprint.
  Hosts that already verify see no change in entries or prompts.
- **API descriptions in YAML and behind Swagger UI / ReDoc.** Traditional
  discovery parses YAML (PyYAML, when installed), and reads
  `mcp.discovery.api_doc_pages` and the scripts they load for a same-host
  description link (`api_spec_link_patterns`, `api_doc_script_patterns`).

- **`--install-prefetch {none,launchers,all}`** (setting `install.prefetch`,
  `INITIALIZE_CLAUDE_AI_INSTALL_PREFETCH`) lets phase 1 install what later
  phases launch with. `launchers` (the default) adds the component behind the
  first word of every command template in `install.demand_sources`; `all` also
  adds every `install.prefetch_groups` component. Prefetched components are
  optional (a failure is SKIPPED, never FAILED), `--install-skip` wins over it
  and `--install-only` turns it off. On the shipped configuration `launchers`
  adds nothing new, so existing runs install exactly what they did.
- **A new `mcp-runtimes` component group:** go, rust, deno, bun and docker, with
  distribution packages, Homebrew, and upstream download/script fallbacks. Only
  installed when named, prefetched with `all`, or provisioned on demand. Go's
  download version and every minimum version are settings
  (`INITIALIZE_CLAUDE_AI_GO_VERSION`, `INITIALIZE_CLAUDE_AI_<X>_MIN_VERSION`).
- **`--mcp-provision-missing {auto,always,never}`** (setting
  `mcp.packages.provision_missing`, `INITIALIZE_CLAUDE_AI_MCP_PROVISION_MISSING`)
  lets phase 4 install a toolchain a local candidate needs but this machine
  lacks, from phase 1's catalog, once per run, in both agentic and `--no-agent`
  runs. `auto` (the default) never prompts for a password. Picking, scoring and
  the agent's prompts are unchanged, and a candidate whose toolchain cannot be
  installed is rejected in exactly the words it was before. A new `toolchains`
  report step (never a failure) says what was installed or not, and why.
- **Catalog components can declare `provides`, `on_demand`, `prefetch` and
  `bin_dir_queries`** (all overridable through `[install.overrides]`). `npm`
  provides `npx`, and `uv` now also verifies `uvx`. A toolchain's own bin
  directory (Go's `$GOBIN`/`$GOPATH/bin`) is recorded as
  `INITIALIZE_CLAUDE_AI_BINDIR_<COMPONENT>` and put on the search path of later
  runs; `paths.extra_bin_dirs` gains the cargo, deno, bun and user Go bin
  directories.
- **A phase flag given for a phase that is not in `--phases` now prints a
  warning** naming the phase, as agent-only flags do under `--no-agent`.
- **`--agent-max-attempts N`** sets how many times the phase-4 agent may work
  one stage. It is exactly `--set mcp.agent.max_attempts=N`, with the value
  checked on the command line: a value is required, a bare flag is refused, and
  N must be a whole number of at least 1. Leave it out and nothing changes; the
  setting still decides (3 as shipped, or whatever
  `INITIALIZE_CLAUDE_AI_AGENT_MAX_ATTEMPTS`, `--config` or `--set` say). The
  "agent was exhausted" advice now names the flag.
- **Numeric setting flags are declared in one table, `cli.SETTING_FLAGS`.**
  `--agent-max-attempts`, `--agent-max-turns` and `--timeout-scale` are rows in
  it. The parser entry, the bounds check and the override are all built from
  each row, and `tests/test_cli.py` checks every row. So the next such flag
  cannot allow a bare form by accident, skip its bounds check or write to a key
  that does not exist. The two existing flags behave as before. The only visible
  change is wording: an out-of-range `--timeout-scale` now says "greater than 0"
  where it said "greater than zero".
- **An agent-only flag given with `--no-agent` now prints a warning.**
  Traditional mode has no attempts or turns, so `--agent-max-attempts` and
  `--agent-max-turns` have no effect when the agent is off (`--no-agent` or
  `mcp.agentic = false`). The flag is still accepted, so existing scripts keep
  working.

- **Phase 3 ends by saying which models the credential can reach.** The
  catalogue was already fetched — to choose a model and to check the chosen one
  — and then went into the JSON report and nowhere else, so the phase finished
  without ever answering "which models do I have?" on screen. It is now listed,
  grouped by tier and capped per tier (`provision.catalogue.per_tier`), with the
  documentation URL and a command the operator can run themselves printed every
  time: capping the display is only fair if the rest is one link away.

  Both pointers are per-backend TOML entries (`[provision.catalogue.docs]`,
  `[provision.catalogue.commands]`), so supporting another backend is
  configuration rather than code — and they are shown **even when the listing
  fails**, which is when they matter most. Bedrock has no discovery route here,
  an offline host has none either, and "I could not list them" is half an answer.

  The step says *offered*, not *available to you*, and warns that the two differ.
  A publisher catalogue lists what the provider publishes, not what a project may
  call: a real run reported `12 model(s) offered by global`, picked
  `claude-opus-5-5` from that list, and every phase-4 call to it then returned
  "not available on your vertex deployment". Only a call settles entitlement and
  this step does not make one.


- **Portable paths.** Every path in an entry that lies under a directory this
  program owns is written as `${VARIABLE:-<the path it has here>}` — the bridge
  script, its manifest, its CA chain, and the interpreter. Claude Code expands
  that form in `command`, `args` and `env`, which was verified against the
  installed CLI before adoption: unset, the default applies and the server
  connects; set to a relocated copy, it connects from there; set to a
  nonexistent path, it fails. So an entry is unchanged in effect where it was
  written and relocatable everywhere else — copy the directory, export one
  variable — and the briefing now prints exactly which directory and which
  variable. System files such as `/etc/ssl/ca-bundle.pem` are left alone; the
  interpreter is included because unlike those it is a choice this program made
  (`[mcp.registration.path_variables]`, `mcp.registration.interpreter_variable`).

And one found by auditing the deterministic pipeline against the agentic one:

- **A registry's `isSecret` flag was deciding whether a credential reached the
  file.** The primitives that classify a name or value as a credential lived in
  `agentic.py`, so they judged what a *subagent* claimed; traditional mode
  answered the same question by trusting the package registry's own flag. An
  entry declaring `{"name": "Authorization", "value": "Bearer sk-live-…",
  "isSecret": false}` would have had that value copied verbatim into
  `.mcp.json`, while the identical header from an agent was dropped — one rule
  with two strengths, the weaker one on the path that runs when there is no
  agent supervising. The rule now lives in
  `mcp_auto_discovery/secret_policy.py`, both pipelines import it, and an
  outside flag may only make the program more careful: `true` is believed,
  `false` is checked. A test asserts the two modules hold the *same objects*,
  since a copied regex is how one rule quietly becomes two.

And the one that made three of the others cosmetic:

- **A refused entry is now actually refused.** Both registration gates —
  grounding and portability — set a reason and skipped the rest of their loop
  iteration, and neither prevented anything: `publish` handed *every*
  registration to `_write_json`, which looped over all of them and wrote
  `servers[name] = entry`. `_register` then ran on the refused entry too and
  overwrote its reason with `the claude CLI is not installed`. So both gates
  announced decisions they never carried out, and the operator was told neither
  the refusal nor why. `skipped` has been split into `refused` (no entry was
  written) and `skipped` (the entry is in the file; the CLI step did not run);
  only entries that were not refused are written, registered, verified or
  briefed on; and the refusal is reported as its own step with its reason. The
  tests that missed this asserted the *verdict* rather than the contents of the
  file — `tests/test_mcp_refusal.py` asserts on the bytes on disk.

- **`--timeout DURATION`** — a wall-clock ceiling for the whole run (`45s`,
  `90m`, `2h`, `1d`, or plain seconds). Not a per-operation timeout and not
  expressible as one: phase 4 took 259 minutes against thirteen addresses with
  every individual timeout comfortably unmet, because that is what a few hundred
  well-behaved operations add up to. It stops the run *starting* new work rather
  than interrupting it — an address already being worked is left to finish,
  since killing an agent mid-turn discards tokens already paid for — so a run
  can overrun the ceiling by as long as one address takes. The verdict is then
  `PARTIAL`, every address never attempted is named in order, and the resume
  command is printed. Addresses are worked in the order given, so with a
  deadline the ones dropped are the tail of the list. No ceiling by default
  (`run.deadline_seconds`).
- **`--agent-max-turns N`** — one turn budget for every phase-4 subagent,
  overriding each one's own (endpoint 10, package 10, bridge 12, strategy 10).
  It overrides the definitions rather than feeding into `mcp.agent.max_turns`,
  because that key is a *fallback* used only by an agent that sets none — and
  all four set one, so raising it has never done anything and a flag pointed at
  it would have been equally inert. Lowering the budget is the interesting
  direction: phase 4 is ~98% agent thinking and turns are its unit, so this is
  the only lever that makes a long run genuinely cheaper rather than merely more
  overlapped, and it is what the run already advises when a stage keeps running
  out of wall clock — advice that was unfollowable until now. Raising it usually
  wants `--timeout-scale` too, since the stage timeout binds first. Explicit
  value required, minimum 1, off by default (`mcp.agent.max_turns_override`).
- **A gauge for that dial.** Every stage now records `turns_used` and
  `turn_budget`, and says on screen when the budget was spent in full:
  `used all 10 turn(s) -- the budget, not the target, may be what ended this
  attempt`. Nothing previously said whether an agent stopped because it had run
  out of ideas or out of room, and the two want opposite adjustments.
- **`--timeout-scale FACTOR`** — multiplies every per-operation timeout,
  preserving their relationships. This is the honest form of "one global timeout
  value": the 21 timeouts here span 5 seconds for a TCP connect to 1800 for a
  person at a browser, so a single number *substituted* everywhere would kill
  agent turns at 30 seconds or leave dead connections hanging for half an hour.
  Pairs with `--concurrency`, where several addresses contend for one CPU and
  link. It does not touch the five settings that only look like timeouts —
  a heartbeat, two thresholds used to *score* servers when ranking, and the
  `SIGTERM`→`SIGKILL` grace (`run.timeout_scale`,
  `run.timeout_scale_suffixes`).
- Both flags require an explicit value; neither has a bare form. `--concurrency`
  has one because its bare value only changes scheduling and cannot change the
  result, whereas a wrong deadline or scale silently changes what work gets
  done. With neither flag nothing changes: no deadline is installed, and at a
  scale of 1.0 the resolver returns each setting untouched with no arithmetic,
  so an `8` cannot become an `8.0`. `tests/test_timeout.py` pins all 26
  time-related settings to their previous values.
- `tests/test_mcp_settings_reachable.py`: every literal settings key the source
  reads must be defined by the packaged defaults, section-relative reads
  included. This is the general form of the `max_provisional` indentation bug --
  a setting with a plausible fallback is invisible when it goes missing, and
  "nothing is hardcoded" is not true of a key that silently resolves to a
  literal in a `.py` file. It found one other: `install.python_executable` was
  read but never defined, falling back to a bare `python3`.
- `tests/test_mcp_grounding.py`, `tests/test_mcp_secret_classification.py` and
  `tests/test_mcp_refusal.py`, plus eleven corpus entries for the real
  `jira.example.com`, `confluence.example.com`, `openqa.opensuse.org` and
  `smelt.example.net` entries (`tests/test_mcp_regression_corpus.py`).

### Changed

- **What the program judges by is configuration, and its guesses got wider.**
  An audit for hardcoding closed these:
  - musl is recognised on every architecture (`/lib/ld-musl-*.so.1`), not only
    x86_64; fish's environment file is written when fish is anywhere on `PATH`.
  - The registrable domain uses `mcp.grounding.multi_part_suffixes` (151
    two-label suffixes, up from 14) plus the system's public suffix list
    (`mcp.grounding.public_suffix_file`, `INITIALIZE_CLAUDE_AI_PUBLIC_SUFFIX_FILE`;
    `public_suffix_sections`, `icann` by default), configured by phase 4 for
    both modes. `example.co.kr` and `example.kawasaki.jp` are organisations now.
  - `mcp.discovery.response_scan_chars` (4000) replaces three fixed windows.
  - `[mcp.secret_policy]` holds the credential words, selector words, inline
    credential patterns, plain values and opaque-value rules; the agentic
    pipeline asks `secret_policy.carries_inline_credential` instead of importing
    a pattern.
  - `[mcp.agent.failure_patterns]` holds the wordings of the four agent
    failure classes. The free retry for a transient failure now uses the same
    classifier as everything else, `unusable_markers` included.
  - A generated bridge answers `initialize` in the client's protocol revision
    when it is one of `mcp.protocol.versions` (else the newest), and reports
    this program's version. Both are written to the manifest; an older
    manifest keeps the old answers.
  - 93 fallbacks that disagreed with the packaged value (`0`, `0.0`, `""`,
    `[]` for configured values) now equal it. `tests/test_config_fallbacks.py`
    resolves section-relative reads and refuses a mismatch, implicit fallbacks
    included. (`tests/test_hardcoding_closed.py`.)
- **Cleanup: every value is configured, and every rule lives in one place.**
  - Every subprocess timeout is a setting, so `--timeout-scale` now reaches
    all of them. The gcloud and claude commands are keyed by command name in
    `[auth.google_cloud.command_timeout_seconds]` and
    `[auth.claude_oauth.command_timeout_seconds]` (a table whose name ends in
    `timeout_seconds` is scaled like a key that does). New keys:
    `secrets.command_timeout_seconds`, `install.venv_timeout_seconds`,
    `install.version_timeout_seconds`, `install.python_probe_timeout_seconds`,
    `install.python_import_timeout_seconds`, `install.commands.npm.query_timeout_seconds`,
    `provision.inventory.version_timeout_seconds`,
    `provision.inventory.command_timeout_seconds`,
    `auth.signin.clipboard_timeout_seconds`, `mcp.agent.help_timeout_seconds`
    and `mcp.packages.search_timeout_seconds`.
  - `gcloud-token` and `gcloud-adc` references run the configured
    `auth.google_cloud.commands.print_token` / `print_adc_token` rather than a
    command line built in code. What gcloud prints for an unset property is
    `auth.google_cloud.unset_values`, and phase 3 no longer has its own copy of
    the `get-value` commands.
  - The OAuth hint in phase 4 names the configured
    `mcp.bridge.adapters.remote_to_stdio` command rather than `npx -y mcp-remote`.
  - The agent's repair brief is `mcp.agent.repair_brief`. The bridge manifest's
    schema number is read from the generated script itself and its version is
    the program's.
  - Findings are shortened by one helper, with `mcp.agent.finding_value_chars`,
    `mcp.agent.previous_stage_findings_chars` and `mcp.discovery.well_known_chars`.
  - Claude Desktop's download URL and Flatpak ref are
    `install.claude_desktop.url` / `flatpak_ref`
    (`INITIALIZE_CLAUDE_AI_CLAUDE_DESKTOP_URL`, `…_FLATPAK_REF`).
  - The Vertex/Bedrock switches the claude.ai check looks for are
    `auth.claude_oauth.third_party_switches`. One `truthy()` reads every on/off
    variable, including `INITIALIZE_CLAUDE_AI_FORCE_HEADLESS` and `…_FORCE_DISPLAY`,
    which are now documented.
  - Every file that may hold a credential (env files, `settings.json` and its
    backups, `.mcp.json`, the sign-in URL file) is written by
    `fileio.write_private`: owner-only before the first byte, replaced
    atomically, nothing left behind on failure.
  - The redaction patterns used before configuration loads are the configured
    ones (GitHub OAuth, Google API key, Slack and JWT shapes were missing).
  - Code fallbacks are neutral or equal to the shipped default, which a test now
    enforces; several disagreed (the HTTP timeout, the response cap, the
    candidate cap).
  - A WebSocket URL an agent reports in `findings.websocket_urls` now opens the
    bridge stage, as it already did in traditional mode. The skills'
    examples use only the keys the pipeline reads.
  - Removed: settings nothing read (`mcp.strategy.allow_execution`,
    `mcp.bridge.strategy`, `mcp.bridge.generated.language`, `mcp.agent.skills`,
    and the unused per-package-manager `query` commands), and dead code
    (`pathgen.alternate_scheme_origins`, `grounding.assess(generic=)`).
  - Generated bridge scripts, manifests and CA chains in the working directory
    are gitignored.

- **An entry that will not start is repaired inside the stage that produced it,
  and the target keeps looking if it cannot be.** The gate asking *does this
  actually start?* used to run at publish, which was too late to do anything but
  report: the agent's turn was over, and the tier descent had already happened,
  so a candidate whose entry would never start still occupied its target's
  answer and tiers 3 and 4 were never asked.

  It now runs in the stage loop beside the registrability check, with remedies
  ranked the way discovery itself is -- the agent first (it installed the
  package, chose the arguments and, at tier 3, wrote the script, so it can fix
  an argument, a variable, an interpreter or a file it generated), then the
  deterministic repairs that `--no-agent` also gets, then neither, at which
  point the candidate is dropped and **the lower tiers run because nothing
  settled the stage**. That tier fallback was not implemented; it falls out of
  asking at the right moment.

  The agent does not get to say whether its own fix worked: whatever it returns
  is launched again and kept only if it starts. Every repair is re-proved before
  the next is tried, so one that did not help is discarded rather than
  accumulated. Both pipelines share one `EntryDoctor`, which the writer calls
  again at publish, so the stage and the file cannot reach different answers
  (`mcp.healing`).
- **A failure now names the files it depended on, and how old they are.** A
  bridge is a script plus the data it reads, and the commonest way one breaks is
  that something regenerated half of it -- `openqa.example.net` did it twice, once
  as `AttributeError: 'list' object has no attribute 'items'` and once, after
  the pair gained a stamp, as an honest refusal. Both times the deciding fact
  was two timestamps a day apart, and nothing said to look. Paths are collected
  from the entry *and from the server's own complaint*, since a bridge locates
  its manifest itself and the entry never names it; only files sharing a
  directory are compared, so a system interpreter beside a CA bundle is not
  mistaken for a stale set (`mcp.healing.report_files`).
- **`starts` now launches the entry the way the client does.** It inherited
  `os.environ`, while Claude Code passes the entry's own `env` plus a narrow
  passthrough and nothing else -- so an entry could pass here on a variable from
  the operator's shell and fail there, which is exactly the `starts: yes` beside
  `claude: no` that shipped two dead openQA entries. A check more permissive than
  the thing it predicts cannot predict it (`mcp.verification.client_env`).
- **A refusal to write is said out loud, and never silently leaves a stale
  entry unexplained.** An entry that cannot be made to start is not written; if
  one of that name already exists from an earlier run it is kept -- this run has
  nothing better to offer -- and that is reported explicitly, because silence
  would read as "the file is as this run left it" while the operator is looking
  at an entry nothing verified today. Every repair attempted, and what came of
  each, reaches the screen and the log.


- **A generated bridge is a directory that finds its own parts.** The script
  resolves its operations manifest and any PEM beside it against its own
  `__file__`, so an entry that carried four absolute paths into this program's
  state directory now carries one (it has since gone back to naming the
  manifest and bundle in `env` too, rewritten through the configured path
  roots, so they stay portable -- see "The generated bridge names its manifest
  and CA bundle in `env`"). Moving a bridge is "copy the directory";
  `MCP_BRIDGE_MANIFEST` and `MCP_BRIDGE_CA_BUNDLE` still override when set, and
  a pinned or system bundle is still named because nothing could find it
  otherwise. A missing companion file now exits with a sentence on stderr rather
  than an `ENOENT` the operator sees as `Connection closed`.

Three defects behind a single symptom: an ordinary run -- no `--exhaustive-mcp`,
no `--the-best-mcp` -- registered **two** entries for `jira.example.com`, under the
names `jira-example-com-jira-example-com-atlassian-dc-mcp-b1ff` and
`jira-example-com-jira-example-com-bridge`.

- **A credential-walled find now settles the target, not only the stage.** The
  tier descent stopped on `verified`, and once packages were trialled without
  their secrets, `verified` stopped being reachable for any target whose server
  wants a token -- the honest verdict is *provisional*. So every such target was
  searched again at tier 2 and tier 3: minutes of agent time per stage, chasing
  a question already answered, and a second entry competing with the first. The
  remaining stages are reported as skipped, with the stage that answered named,
  rather than vanishing from the report
  (`mcp.discovery.provisional_settles_target`).
- **`max_provisional` is a budget for the target, spent across every stage.** It
  was applied once per stage, so four stages kept one candidate each and the cap
  of 1 capped nothing. Separately, the key had been written one table too deep
  -- under `[mcp.discovery.auth.profiles.openqa]` -- so
  `mcp.discovery.auth.max_provisional` did not exist at all, the call site's own
  fallback answered, and setting the environment variable did nothing. A
  candidate that later verifies now gives its slot back, too.
- **A name says the address once.** The agent that discovers a server names it
  after the host it was asked about, and the writer prefixed the host again.
  `McpWriter._compose` drops a whole repeated address, however many times it
  repeats -- and only a whole one: trimming the labels two hosts merely share
  renders `mcp.devin.ai` as `mcp-deepwiki-com-devin-ai`, a name that no longer
  names its host.

Four more, found by reading a generated `.mcp.json` against the hosts it claims
to serve.

- **An entry must carry an address, and it must be the right one.** `npx -y
  @kubb/mcp` — a TypeScript code generator — was registered as the MCP server
  for `confluence.example.com`, with nothing in the entry that mentioned Confluence
  or could ever have reached it. An entry now has to contain an FQDN, URL or URI,
  and that address has to be either the target's own host or one that **answered
  as an MCP endpoint** for the target when probed.

  A different hostname is ordinary and is believed: `github.com` is served from
  `api.githubcopilot.com`, `developers.google.com` from
  `developerknowledge.googleapis.com`. Those entries are listed in the briefing
  under `these_entries_are_served_from_another_host` so the hop is visible.

  A launcher gets less latitude — a URL that answered *is* the service, while a
  stdio process that handshakes proves only that a process started, not where it
  points. So a launcher must name either the target's own host or one this run
  found answering for it (see below), and one with no address at all is refused
  however apt its name, `@modelcontextprotocol/server-github` for `github.com`
  included; set the host variable the package already accepts
  (`mcp.registration.require_address_in_entry`, `mcp_auto_discovery/grounding.py`).
- **A bare origin is not an endpoint, and a probe URL is not tidied.**
  `https://confluence.example.com?format=mcp` answered `401`; the query was
  *ours*, so it was stripped before writing — inventing the address
  `https://confluence.example.com`, which nothing had ever requested, which is a
  login page, and which no credential could make work. The URL that was probed
  is now the URL that is written, and an origin with no path is refused unless
  it genuinely handshaked there (`mcp.registration.allow_bare_origin`).
- **A login page is a login page in any content type.** The evidence test is an
  OR, and its most generous clause — "it answered `application/json`, not a web
  page" — is true of every JSON error ever sent. Confluence's
  `{"message":"You are not logged in. <a href=\"/login.action\">Log in</a>"}`
  passed on it. Phrases that tell a person to sign in now overrule the
  evidence rather than adding to it. Deliberately *only* that: tightening the
  test to demand a protocol-shaped refusal would discard
  `openqa.opensuse.org/mcp`, whose `{"error":"no api key"}` carries no MCP
  marker at all and is a working tier-1 endpoint
  (`mcp.discovery.auth.login_page_markers`).
- **`NODE_TLS_REJECT_UNAUTHORIZED` is not a credential, and does not belong in
  an entry.** `SENSITIVE_NAME` matched `auth` as a bare substring, so a TLS
  switch was classified as a secret and written as
  `"${NODE_TLS_REJECT_UNAUTHORIZED}"` — a placeholder for a value that was
  never secret. The same net caught `AUTHOR`, `AUTHORITY`, `GIT_AUTHOR_NAME`,
  `KEYSTONE_URL` and `MONKEY_PATCH`. The words now match as words, a value that
  is plainly a flag or a number is not treated as a credential whatever the
  name suggests, and variables that switch certificate verification off are
  dropped from the entry outright with the trust store named as the remedy
  (`mcp.registration.tls_weakening_env`).

- **An endpoint the run believed in and never wrote down.** Against
  `developers.google.com/knowledge/api` one run produced three statements,
  minutes apart, that cannot all be true: *no native endpoint answered*; *a
  native MCP endpoint exists at `https://developerknowledge.googleapis.com/mcp`*;
  and tiers 2, 3 and 4 refused **because** that endpoint exists. The address
  finished with nothing registered, having ruled out every alternative on the
  strength of an entry nobody wrote.

  The registry publishes `com.googleapis.developerknowledge/mcp` with `remotes[]`
  and no `packages[]`, and its URL is on a sibling domain — so a host filter in
  the discovery path dropped it before it could be probed, while step D recorded
  it anyway. Two fixes: a registry `remotes[]` entry on another host is now
  **probed** rather than dropped (it is the vendor saying where their endpoint
  lives, which is the provenance a bare off-host URL lacks; on-host still ranks
  ahead, and the number of other hosts one registry answer may send us to is
  capped by `mcp.discovery.registry_remote_other_hosts`). And
  `TraditionalPipeline.reconcile`, run by **both** pipelines, probes anything the
  checklist recorded but nothing registered — recovering it if it answers, and
  reporting the contradiction plainly if it does not, instead of returning an
  empty result that reads like an honest "nothing found".

- **A working server refused at the last step, with nobody left to tell.** The
  third failure against `developers.google.com/knowledge/api`, and the first
  where discovery *succeeded*: the bridge stage produced a verified server with
  three tools, and registration then refused it for carrying no address — one
  environment variable's worth of repair. The grounding check ran only at
  publish, after every stage had finished, so the agent that could have fixed it
  had two attempts left and was never told.

  The structural fix is that **the check now also runs when a candidate is
  ingested**, so an entry that cannot be registered does not settle its stage
  and the reason is handed to the next attempt (`_drop_ungrounded` feeding
  `_correction`). A veto becomes feedback. Three consequences of the same
  reasoning: a launcher may now name a host **this run found answering for the
  target** — a bridge relaying to `developerknowledge.googleapis.com` is
  believed for `developers.google.com`, because the evidence is the run's own
  rather than a guess from hostname spelling (`grounding.endpoint_hosts`);
  `reconcile` fires when everything found was *refused*, not only when nothing
  was found, since "found something" has to mean "found something writable";
  and a filename is no longer reported as an address, so a refusal says
  *"names no address at all"* rather than *"names bridge.js"*, which are
  different problems with different remedies.

- **`<form` and `<a href` removed from the login-page markers.** They are
  markers of HTML, not of a login page, and `disqualifying_content_types`
  already covers HTML-ness. Any API whose error body happens to contain a link
  would have had its protocol evidence cleared — a silent way to lose a real
  endpoint, which is precisely what that list exists to prevent in the other
  direction. Confluence's `not logged in` / `login.action` are still caught.

- **Hostnames cannot say whether an address belongs to a target, and three
  attempts to make them say it refused eleven working servers.** In a single run
  the rule threw away Google's own MCP endpoint (×5), GitHub's own (×4),
  openSUSE's own `osc-mcp` (×2, for pointing at `api.opensuse.org` rather than
  `build.opensuse.org`) and a hosted Bugzilla server. `api.githubcopilot.com` is
  a vendor's and `bugzilla.fastmcp.app` is a stranger's; nothing in the two
  names says which.

  Hostname equality is gone. An address is tied to the target by one of four
  things, strongest first: **the entry names the target**
  (`--bugzilla-server https://bugzilla.example.com`); **the run found that host
  answering** for it; **the same organisation**, meaning a shared registrable
  domain (`api.opensuse.org` for `build.opensuse.org`); or **the vendor
  published it** — its documentation, `.well-known`, or a registry entry
  returned for this target (`grounding.vendor_said_so`).

  A keyword search is deliberately not among them, which is what keeps
  `bugzilla.fastmcp.app` out: it will serve *any* Bugzilla and so must be told
  which, and an entry that does not say `bugzilla_url=…` says nothing about
  whose bugs come back. The floor is unchanged underneath — an entry naming no
  address at all is still refused.

- **A stage no longer settles on something that cannot be registered**, in
  either pipeline. `@ateam-ai/mcp` — an "AI Team Solution Platform" unrelated to
  Open Build Service — verified, so tier 2 reported `OK` for `build.opensuse.org`
  and `build.example.net`; that closed the bridge and strategy stages, and only then
  was it refused at publish for naming no address. One unchecked candidate cost
  two tiers and both addresses. Relevance is now applied to agent-proposed
  packages as well, which is where that candidate came in: the deterministic
  pipeline had always rejected it as unrelated, and the agentic path never
  asked.

- **Five stages reported as `[traditional]` in a run that used the agent
  throughout.** Every one was a `SKIP`. `settled()` and `gate()` live on
  `TraditionalPipeline` — shared on purpose, so one descent rule governs both
  pipelines — and built their skip with `mode=self.mode`, so an agentic run's
  skips carried the deterministic pipeline's label. The run had 17 `[agentic]`
  stages, 3 genuine `[fallback]`, and no stage that ran deterministically at
  all, yet read as though discovery had repeatedly fallen back.

  Three changes: a skip now carries the mode of the pipeline that *decided* it;
  the mode column is **blank** for a stage that never ran, because `Mode`
  answers "how was this carried out" and nothing carried it out; and the tally
  counts skipped stages under their own heading instead of by mode, so
  `stages ran: agentic 17, fallback 3, skipped 5` is true.

- **Rejected agent requests are now counted and reported** (`agent_refusals`).
  A refusal is the one agent failure that costs a stage outright — the other
  classes mean the agent worked and came up short, which the fallback finishes,
  while a rejection means no agent worked at all. Two occurred in one observed
  run and were visible only by grepping the log for a stack trace. The summary
  now names them and points at `mcp.agent.retry_models`.

- **An endpoint refused for not answering, before anything asked it.** Moving
  the grounding check to ingest so the agent could act on it also moved two
  tests that rest on whether the candidate *answered* — and at ingest nothing
  has been dialled: `_drop_ungrounded` runs at `agentic.py:819`, `verify_all` at
  `:837`. So `answered_as_mcp` was false for everything and every off-host URL
  was refused before it could be probed, the provenance and same-organisation
  clauses that exist for exactly those sitting unreachable below the early
  return. One run threw away Google's own endpoint three times and GitHub's
  once, and registered nothing at all. `assess()` now takes `probed`; an
  unprobed candidate whose verdict depends on an answer is `UNDECIDED` —
  registrable — and judged for real at publish. Every existing test had attached
  a handshake first, so the new ones drive the real ingest path instead.

- **A crash was being recorded as a credential wall**, which left two entries
  dead in the file. `credential_pending` was set whenever a declared secret was
  missing, *whether or not the handshake succeeded* — the `if shake.ok` guarded
  only the hint. So `@ivotoby/openapi-mcp-server`, exiting `rc=1` with a Node
  stack trace in `ToolsManager.initialize`, was reported as "an endpoint that
  answered only to say a credential is required", became provisional, settled
  its stage, closed tiers 3 and 4, and was written out with a `${...}` nobody
  can fill — the process dies before it reads the variable. `smelt.example.net` went
  the same way; both entries were disconnected. A failure is now a failure, and
  the hint says the missing variable is *not* known to be the cause. The case
  `trial_without_secrets` exists for is unaffected: `confluence-datacenter-mcp`
  handshakes without a token, so it still registers provisionally with 115 tools.

- **`REJECT_UNAUTHORIZED: "false"` reached a real entry** because the
  TLS-weakening list knew only the `NODE_TLS_` spelling. An exact list always
  lags, so `mcp.registration.tls_weakening_patterns` now catches `NO_VERIFY`,
  `SKIP_VERIFY`, `REJECT_UNAUTHORIZED` and `INSECURE` in any variable name.

- **`"HEADERS": "${HEADERS}"` is not a variable anyone can export.** It is what
  `mcp-graphql` reads, and as a name in the operator's environment it collides
  with every other server in the file. The key stays what the server reads; the
  variable it points at is namespaced — `${SMELT_EXAMPLE_NET_HEADERS}`. A variable
  the vendor documents (`GITHUB_PERSONAL_ACCESS_TOKEN`) keeps its name.

### Added

- **`--do-cleanup` / `--no-cleanup`** (global, cleanup on by default;
  `run.cleanup.enabled`, `INITIALIZE_CLAUDE_AI_DO_CLEANUP`). At the end of a
  run, this program's own leftovers that nothing uses are removed: old
  temporary directories (`run.cleanup.temp_prefixes`) and work files, oldest
  first, down to `run.cleanup.work_budget_mb`. A path any MCP entry of any
  project Claude Code knows names is never removed, nor is a shared package
  cache, and nothing is removed in a dry run. The client bridge also removes its
  own temporary directory when it stops; 683 had been left on one machine.

### Fixed

- **smelt-example-net vanished from `.mcp.json` although a bridge for it was
  verified.** The agent's Python bridge named its introspection tool
  otherwise. No usage task could find it by name, it "settled nothing", and
  the address ended empty. The Node server failed because the root it needed
  was lost with the scratch folder.
  - Tasks find their tool by description (`describes`) or by an answer of the
    API's own form (`answer_markers`), among safe read tools only
    (`mcp.contract.match_descriptions`, `match_answers`, `answer_probe_max_tools`).
  - A verified server is written rather than leaving an address empty, and
    shown under NOT PROVED (`mcp.registration.never_empty_when_verified`).
  - The retry brief says why a verified candidate did not count.
  - An unknown agent `kind` is decided by shape (`mcp.agent.kind_from_shape`).
  - Issuers are also searched in this program's own copies
    (`mcp.trust.issuers_from_program_dirs`).
  (`contract.py`, `probes.py`, `login_check.py`, `traditional.py`, `agentic.py`,
  `writer.py`, `core/trust_store.py`, `tests/test_mcp_job_by_behaviour.py`.)

- **gitlab-example-net vanished from `.mcp.json`.** Its working Python bridge had
  been deleted from the scratch area. `mcp-graphql` (Node) was trialled without
  the host's trust store, which Node ignores: "fetch failed". Nothing else was
  tried.
  - A trial names the bundle its entry will (`mcp.trust.trial_names_host_bundle`).
  - Failures that never reached the service are repaired and proved again in
    every stage and before agent retries (`mcp.verification.heal_transport_everywhere`).
    The retry brief names them as the machine's.
  - API kinds can describe request-body operations (`body`, `body_fixed`,
    `read_only`, `refuse`, `json_params`, `{api_path}`). GraphQL's
    introspect/query operations give this program's own bridge, generated once
    the generic servers failed (`api_kind_bridge_when_generic_fails`).
  - Known-good records keep copies of the files an entry launches, and a lost
    file is restored into the server's own folder (`mcp.known_good.keep_files`,
    `restore_missing_files`). Existing entries' scratch files are moved when
    found (`adopt_existing_files`). A CLI copy whose files are gone is replaced
    (`replace_lost_files_cli_copies`).
  (`verifier.py`, `traditional.py`, `agentic.py`, `api_kinds.py`, `bridge.py`,
  `known_good.py`, `writer.py`, `tests/test_mcp_runtime_trust_and_lost_files.py`.)

- **Phase 1 filled a disk with an in-place reinstall, then blamed six
  failures on it.** `gcloud components reinstall` staged a whole SDK copy on a
  machine with just over 1 GB free. Every later step said "install command
  (gcloud) … No space left on device" and kept installing.
  - Space is checked before each step: folder × `space_factor`, or the
    package's installed size, plus `install.space_margin_mb`. A reinstall that
    doesn't fit is kept, not run (`install.check_space`).
  - What a failed step left next to its folder is removed
    (`install.clean_failed_step_leftovers`).
  - A full disk is blamed only on the step whose own command hit it, and it
    stops the phase at once (`install.stop_on_full_disk`). Phase 4's on-demand
    installs do the same.
  - Each owning package is reinstalled once per run.
  - Kept components with no path are named by their title.
  - The verdict no longer asks to export a value that Claude Code's settings
    env already holds (`mcp.registration.skip_values_claude_has`).
  (`install/space.py`, `phases/phase1_install.py`, `mcp_auto_discovery/provisioning.py`,
  `tests/test_install_space_and_full_disk.py`.)

- **build-example-net was written with the route that cannot work, confluence's
  token was blamed for an account without access, and Node servers failed on a
  partial chain.**
  - Ties between unproved candidates are broken by evidence: a scheme refused
    before goes last; the scheme the API host asks for and fewer values to
    supply come first (`mcp.discovery.auth.evidence`, `mcp_auto_discovery/evidence.py`).
  - The other waiting servers are kept and proved again by later runs,
    `--mcp-recheck` included (`[mcp.alternatives]`, `alternatives.py`).
  - A stated HTTP status outranks a client's wording (403 means no access,
    401 means refused: `login_check.status_outranks_wording`, `status_verdicts`).
    A refused login is confirmed on the product's own record
    (`confirm_by_own_record`).
  - The run's trust bundle is completed to a self-signed root from issuers the
    machine has (`mcp.trust.complete_to_root`, `issuer_sources`,
    `issuers_from_entries`). Soon-expiring certificates are named
    (`expiry_warning_days`).
  - Written entries own their files: they are moved out of `work/` into
    `mcp.registration.artifacts_home` (`own_files`, `ICA_MCP_SERVERS`).
  - Plain-text credentials in `.mcp.json` are moved into Claude Code's
    settings env (`move_literal_secrets`).
  - The agent SDK's stderr is kept and named on failure
    (`mcp.agent.capture_stderr`).
  (`tests/test_mcp_evidence_trust_and_owned_files.py`.)

- **`--force-install` repairs the copy in use instead of reinstalling from
  scratch.** On a machine that already had everything, npm "failed" (its only
  openSUSE package name was `npm22`, while the machine's was `npm26`). gcloud
  "failed" (Google's script refused its own existing folder). node was
  "CHANGED" although nvm's download timed out and `/usr/bin/node` was never
  touched.
  - The owner of the binary in use is found first: a strategy whose folders
    hold it (`owns_paths`, `adds_bin_dir`), the package manager that owns the
    file or provides the command (`[install.commands] <mgr>.owner` /
    `<mgr>.provider`), or a kind recognised from the path (`[install.ownership]`).
    Only that owner reinstalls (`install.reinstall_owner_only`).
  - An owner with no in-place form keeps the copy and is reported OK
    ("kept"). The forms are `reinstall_argv` / `reinstall_shell` /
    `reinstalls_in_place`, and gcloud uses `gcloud components reinstall`.
  - A package the system doesn't have falls back to its installed provider or
    the newest name matching `package_patterns` (`<mgr>.missing_markers`,
    `<mgr>.available`), in phase 1 and in phase 4's on-demand installs.
  - CHANGED needs the binary in use to change (`install.changed_needs_proof`).
  - A failed post-install step fails its strategy.
  - Failures are reported in the installer's own last words, and timeouts say
    how far a download got (`install.progress_line_pattern`).
  - Shell code keeps its `$VAR`s for the shell. nvm's directory follows
    `$NVM_DIR`, then `$XDG_CONFIG_HOME/nvm`, then `~/.nvm`.
  - A downloaded installer that is a web page is never run
    (`install.script_web_page_markers`).
  (`install/ownership.py`, `install/installers.py`, `phases/phase1_install.py`,
  `mcp_auto_discovery/provisioning.py`, `tests/test_install_reinstall_owner.py`.)

- **smelt.example.net's working GraphQL bridge reached `.mcp.json` again.** It had
  been asked GitLab's `{ currentUser { username } }` because it listed a tool
  named `graphql_query`. It answered 400 "Cannot query field", failed "what it
  is used for", and was left out, although it did smelt's own job.
  - A product's own task is asked only where the address is that product. The
    pipelines record each address's recognised product and API kinds for the
    verifier. Tasks carry a `scope`, and a task with `arguments` belongs to its
    product (`mcp.contract.scope_product_tasks`, `scope_by_arguments`,
    `default_scope`, `[mcp.contract.products.gitlab] scope = "address"`).
  - A call refused as a request the server doesn't have is a task that doesn't
    fit, never a failure. 401/403 still count (`mcp.contract.misfit_is_not_a_failure`,
    `not_fitting_markers`).
  - A declared credential slot is not proof that a login is needed. Usage
    tasks that pass with the slot empty make it optional
    (`mcp.verification.login_check.job_done_is_no_login_needed`).
  - The agent brief and all four skills say the same.
  (`mcp_auto_discovery/contract.py`, `verifier.py`, `traditional.py`,
  `agentic.py`, `repair.py`, `tests/test_mcp_product_tasks_scoped.py`.)

- **Every server judged the way Claude Code loads it.** A run on another
  machine reported thirteen servers registered; `/mcp` showed none.
  - **The entry's type comes from proof, never from a word.** An agent's
    transport fell through a lookup to `"type": "stdio"` beside a `url`
    (openqa-example-net, developers-google-com), and Claude Code skipped both. The
    type is now the transport the handshake was proved over, normalised through
    `mcp.registration.transport_aliases`; a url entry carries no `env`; an
    agent's description in a name field is not a name (`mcp.agent.max_name_length`).
  - **An entry Claude Code refuses is never written.** Its shape
    (`mcp.registration.entry_shapes`), then Claude Code itself in a throw-away
    configuration (`validate_with_claude`). A CLI copy removed for a
    replacement is put back when the add fails (`restore_on_failure`), and a
    failed add is never reported as "registered".
  - **Claude Code connects to every written entry**, with no credential and no
    model request (`[mcp.registration.claude_check] connect`). A start slower
    than Claude Code's wait is sampled several times and the slowest counts
    (`client_timeout.samples`); a wait that fits goes into Claude Code's own
    `settings.json` too (`client_timeout.persist_to`). gitlab-example-net's
    mcp-graphql started in 12-73 s against a 30 s wait.
  - **Which written servers Claude Code will load** is checked after the last
    write: switched off by `disabledMcpjsonServers` in a settings file, or
    waiting for approval -- named with the file and key
    (`[mcp.registration.load_check]`).
- **A verdict about a credential needs one to have been sent.** sd-example-com's
  service-desk bridges, asked with no token, were ranked "REJECTED" and lost to
  an unchecked mcp-atlassian. A 401 to a call that carried no credential now
  means the slot is missing, and a success with none means public
  (`mcp.verification.login_check.refusal_needs_a_sent_credential`).
- **What worked here is not hidden by what proved nothing.** A server that only
  waits for a credential no longer hides a lower tier's existing entries: they
  are re-checked (`mcp.discovery.reuse.recheck_when_settled_unproven`), the one
  known to work here keeps its place (`worked_before_wins`), and a connected CLI
  copy is never advised away for an unproven entry. gitlab-example-net's working
  bridge had been replaced by mcp-graphql.
- **A tool whose query the service refuses cannot do the job.** "Query too
  large" is a failure, never "waiting for a credential"
  (`mcp.verification.login_check.job_refused_markers`).
- **A credential slot must reach the server.** bugzilla-example-com's key sat under
  a variable mcp-bugzilla never reads, and every anonymous call passed. An
  optional slot now has to change the server's answers with a stand-in in it,
  else it is moved to the variable the server's `--help` names
  (`[mcp.verification.delivery]`).
- **One user name and password on several services is allowed**, each entry
  with its own address; a refusal is only that service's answer, and a token
  alone counts as another instance's (`instance_bound_patterns`).
- **Discovery never waits for a person.** While phase 4 runs, nothing it starts
  -- trials, checks as written, checks through Claude Code, agents -- can reach
  the desktop, and every program that opens URLs (`xdg-open` and its kin,
  `BROWSER`) is a recorder that writes the URL down and returns
  (`[mcp.noninteractive]`). A candidate that asked to open a page is reported
  as needing a browser login on that host, a step only you can take, with the
  service's token route where there is one; it is never waited for and never
  counted as broken. A browser had opened by itself on GitHub's sign-in page
  during a run and waited. Phase 2's own sign-in is untouched.
- **What keeps its place is written; no address ends with no entry.** An
  existing entry kept in place of a refused replacement is now recorded in
  `.mcp.json` too, in its portable form, whatever it proved; one that cannot be
  recorded portably holds no place (`mcp.registration.held_must_be_recordable`);
  and the end-of-run file check repairs an address that has a server but no
  entry instead of only reporting it (`repair_missing_entries`). github-com was
  kept because it answered, never written because it was neither verified nor
  connected, and the check only said so.
- **A call made with no credential waits briefly**
  (`mcp.verification.no_credential_call_timeout_seconds`): github-mcp-server,
  asked `get_me` with no token, had held its address for four minutes.
- **Every kind of login counts the same; proof decides.** No preference for a
  token or a password over an SSH-key signature (`mcp.discovery.auth.methods_equal`):
  every feasible login is built and tried, and proof picks the winner. Before
  anything is proved, evidence orders them -- login history, which logins the
  API host itself offers, what the machine holds -- never the kind. build-example-net
  had been written with the password route because a fixed ranking put it first,
  while build.example.net's API host offers only a key signature.
- **A credential that may not have arrived is never blamed.** A refusal
  identical to the anonymous answer no longer counts against the credential's
  allowance, is not recorded as refused, and ranks like a missing credential
  (`login_check.same_as_anonymous_is_not_a_refusal`). sd.example.com's token had
  gone to an agent's bridge under a name its script never reads, and the
  bridge that works with it was then never offered it.
- **Slots follow what the script reads.** A bridge's credential slot that its
  own source never reads is renamed to the one it does
  (`mcp.verification.align_slots_to_source`); the delivery check reads the
  source first and runs `--help` only for packages, with input closed, so it
  can no longer hang for a minute.
- **Connected is not working.** An existing entry Claude Code connects to no
  longer holds its name by itself over a replacement built for the detected
  API (`mcp.registration.connected_is_not_working`), and only an entry that
  really passed here counts as having worked (`connected_counts_as_worked`).
- **Steps only you can take, in the server's own words.** A refusal that means
  an operator step (a key not registered, no API access) is named with the
  challenge the service itself sent and what was tried
  (`[mcp.verification.login_check.user_steps]`), in a "steps only you can take"
  line -- never a guessed location.
- **Checked as a new terminal's Claude Code sees it.** Every check that
  stands in for Claude Code (its connection check, a session through it, the
  variables an entry needs) runs with the system's own variables, this
  program's environment file and Claude Code's `settings.json` env -- never the
  rest of the run's shell (`[mcp.registration.fresh_env]`). build-example-net had
  "passed" because the run's shell exported its user name; a new terminal
  failed. An entry that does not work that way makes the final verdict PARTIAL.
- **Every value an earlier entry supplied is kept**, not only credentials: a
  value the new entry reads as `${NAME}` and a new terminal would lack goes into
  Claude Code's `settings.json` env (`mcp.registration.keep_supplied_values`).
- **Which login each host accepted or refused is remembered**
  (`[mcp.discovery.auth.history]`): the next run tries an accepted one first and
  a refused one last, in the login plan and the client bridge's routes.
  build.example.net's password login had been tried first in every run.
- **A credential the operator supplied keeps working.** A literal (or
  `${NAME:-default}`) credential is still never written to `.mcp.json`; it is
  now kept in Claude Code's own `settings.json` `env` (mode 0600) under the
  variable the entry reads -- Claude Code expands `${NAME}` from there -- so
  the entry is not left "waiting" for what was already given
  (`mcp.registration.keep_credentials_in`). The login plan asks for the kind
  of credential an entry already sends the host (sd.example.com's token) before
  another address's login (`mcp.discovery.auth.prefer_in_use_methods`). A
  server given that credential that still proves nothing settles nothing
  (`mcp.discovery.offered_unproved_keeps_searching`). An entry left with a
  shape Claude Code skips is reshaped after the last write
  (`mcp.registration.reshape_entries`).
- **An API-kind adapter is given the credential the address holds.** An
  adapter table may say how it takes one (`credential_env`, `{credential_ref}`
  for the `${VARIABLE}`); it is applied only when an entry here sends the
  address a credential or the shell exports a token for it. mcp-graphql 2.x
  had replaced gitlab-example-net's token-carrying entry with one that had no place
  for the token. A trial resolves `${NAME}` from Claude Code's `settings.json`
  env too (`mcp.verification.resolve_from_claude_settings`), as Claude Code
  does. The delivery check also runs when the answers with a credential match
  those without one.
- **Agent attempts are logged one by one** (model, brief, outcome), and a
  request refused with nothing left to change is not sent again
  (`mcp.agent.stop_when_nothing_changes`).
- **Cleanup reads entries as Claude Code expands them.** A bridge named as
  `${ICA_MCP_ARTIFACTS:-/…/artifacts}/gitlab-example-net/…` would have been removed
  as unused. (`tests/test_mcp_what_claude_code_loads.py`,
  `tests/test_disk_space_and_cleanup.py`.)

- **After a full disk: nothing half-written, and repair before replacement.**
  - **Every generated file is written atomically** (manifests, bridge scripts,
    the composer, the agent's context files, downloads, the report). A full
    disk had left sd-example-com's operations manifest empty, and the next run
    judged that working bridge broken.
  - **A verdict taken before its file changed is taken again.** sd-example-com's
    manifest was regenerated later in the same run, but the old "does not
    start" verdict stood. An existing entry that failed and names a file the
    run has since rewritten is now trialled again before anything is written
    (`mcp.registration.retrial_rewritten`).
  - **A reused bridge knows its API kind**, read from its manifest's
    operations. **A stage picks the server built for the detected API among
    equals**: an agent's bridge had been kept over the program's own
    customer-API bridge, and mcp-atlassian won the slot.
  - **A connection failure is repaired, not judged.** smelt-example-net's working
    mcp-graphql met `fetch failed` and was replaced. Now an existing entry whose
    calls fail to connect (`mcp.verification.transport_failure_markers`) has
    its trust store pointed at a bundle that works, and it is proved again
    first (`mcp.verification.heal_transport`).
  (`tests/test_mcp_damaged_files_and_repairs.py`.)
- **A full disk is seen, stopped on, and explained.** A run on another machine
  started on a full disk. Phase 1 reported OK for reinstalls that had just
  failed with "No space left on device", phase 4 printed a traceback per
  address, and then the log stopped and no `.mcp.json` was written.
  - **Free space is checked before every phase** (`[run.disk]`, default
    1024 MB), on every filesystem the run writes to. A short one stops the run
    and is named, with the largest directories this program owns there and
    each shared cache's own cleanup command.
  - **"No space left on device" stops the run with one verdict**, whether it
    comes as an `OSError` (ENOSPC/EDQUOT), a `shutil.Error`, or a command's
    output (`run.disk.out_of_space_markers`).
  - **A failed forced reinstall is PARTIAL**, reading "reinstall failed (…);
    kept the existing …", not OK.
  - **An agent workspace that cannot be prepared turns the agents off once**,
    with the reason, and the deterministic pipeline does the work. No traceback
    per address.
  - **A log file that stops being written is said on the terminal**, once, and
    the last lines are kept.
  - **An unwritten `.mcp.json` is a failed step with its reason** beside the
    verdict.
  (`tests/test_disk_space_and_cleanup.py`.)
- **A `claude` started in a new terminal logs in on its own, for every kind of
  login.** Phase 2 proved each login inside this program's process, which holds
  every variable the run resolved. A new terminal holds none of them.
  - **The new-terminal check.** At the end of phase 3, `claude` is started with
    only `provision.fresh_terminal.keep` (HOME, PATH, ...) in its environment,
    from an empty directory. The phase says whether it logs in on its own and,
    when it doesn't, which login variables only the run had
    (`[provision.fresh_terminal]`). It also says when the first interactive
    start will ask for a theme or folder trust, which is not a login.
  - **Login settings go where Claude Code reads them.** A service-account key's
    path (`GOOGLE_APPLICATION_CREDENTIALS`) and an AWS profile go into the
    `settings.json` `env` block (`login_env`), never only into env.sh. A
    credential-shaped value is never written there.
  - **An API key in a file, keyring or command gets an `apiKeyHelper`.** It is
    built from the reference by scheme
    (`[provision.anthropic_api.key_helpers]`), never from the value. An
    operator's own `apiKeyHelper` is kept.
  - **A headless claude.ai sign-in is stored.** It now uses `claude auth login`.
    `claude setup-token`, which only prints a token, runs only when
    `auth.claude_oauth.long_lived_token` asks for one, and the report then says
    to export `CLAUDE_CODE_OAUTH_TOKEN`.
  - **Impersonation reaches Claude Code.** The application-default credential is
    written impersonating the service account too
    (`auth.google_cloud.impersonate_adc`); before, only the gcloud CLI
    impersonated.
  - **A setting's value is no longer redacted as a secret.** An MCP trial
    expanding `${UNSET:-fallback}` registered "fallback" for redaction for the
    rest of the process. Only credentials go through the secret resolver now.
  (`tests/test_auth_survives_a_new_terminal.py`.)
- **The fifth run on the other machine (2026-10-19, pid 93893), on the current code.**
  - **The real cause of the vanishing username slot.** When an existing entry
    was reused, `${BUILD_EXAMPLE_NET_USERNAME:-}` expanded to `""` for the trial and
    no reference was kept, and an entry leaves out empty values. Every run wrote
    the slot and the next run's reuse deleted it, along with every optional
    `${NAME:-}` setting of every server. The four earlier fixes all repaired it
    downstream. Reuse now keeps the reference whenever a value came from one,
    and the slot check judges what will actually be written.
  - **Write → reuse → write is tested as a fixed point** over every entry shape
    (`tests/test_mcp_entry_round_trip.py`). It fails without the fix.
  - **A refused login names the routes not tried.** build.example.net refused a Example
    password over basic auth, while api.example.net asked for
    `Signature realm="Use your developer account"`. The remedy now names the
    service's other routes with their setup step, such as the client's
    key-registration hint (`name_other_routes`).
  - **"Nothing sent" is not "refused" when ranking.** A "not set" or "no SSH
    key" answer ranks like an unchecked login, not a refused one.
  - **Kept entries count for "a login from another service."** A credential in
    use by an entry kept because it answered or Claude Code connects to it is
    compared too, and the report says that service "uses" it
    (`other_service_counts_kept`).
- **The fourth run on the other machine (2026-10-19, pid 53581).** The log's new
  code line showed it ran `sha256:ef1cdcaa15f7`, the code from before the
  previous round's last four fixes.
  - **The result is checked, not one path.** build-example-net had its username slot
    when it was trialled, and reached the file without it by a write path the
    publish check did not guard. Now, once everything is written, every entry
    of the run's addresses is read back from the file and checked against what
    it launches reads, and missing slots are added in place
    (`mcp.registration.verify_written`).
  - **The CLI copy that loads first is checked too.** A claude CLI copy that
    lacks a slot, or differs from an entry this run verified, is replaced by the
    file's entry at the scope it was held in
    (`mcp.registration.replace_divergent_cli_copies`).
  - **How to move an entry is printed.** build-opensuse-org ran
    `/usr/local/bin/osc-mcp`, and the next machine got an ENOENT with
    `ICA_MCP_OSC_MCP` named nowhere the operator reads. Each server's summary
    now says which variable moves each machine-specific path
    (`mcp.registration.say_how_to_move`).
  - **A removed credential default is said plainly.** When a value written as a
    `${NAME:-default}` default is removed, the report says so and where to keep
    it instead: the shell, or Claude Code's settings `env`.
- **The third run on the other machine (2026-10-19, pid 3558).**
  - **No entry is written short.** build-example-net was kept without
    `BUILD_EXAMPLE_NET_USERNAME` while its own report said it needed it. Every
    entry is now checked against what it launches reads (a generated bridge's
    manifest), and a missing slot is added as `${NAME:-}`
    (`mcp.registration.complete_slots`). An existing entry whose only fault is
    such a slot is repaired and kept, not replaced
    (`mcp.discovery.reuse.repair_missing_slots`).
  - **"Not passed" is not "not set".** The bridge told the operator to export a
    variable the entry never passed, which a client never delivers. Both
    generated bridges now say "the entry does not pass X — add it to its env"
    when a variable never arrived, and "X not set — export it" when it arrived
    empty. Such answers count as a missing credential (`does not pass`).
  - **A local server's slot keeps its key.** osc-mcp's slot was renamed
    `BUILD_OPENSUSE_ORG_OSC_MCP_USER`, a variable it never reads
    (`mcp.registration.keep_server_slot_keys`).
  - **Installed versus built is judged by outcome.** `/usr/local/bin/osc-mcp`
    was refused for a workspace build that behaved the same way (both waited
    for the same login), because every detail had to be equal.
  - **A kept bridge is described from its manifest.** sd-example-com's token fell
    back to "shape unknown" although its manifest records `auth: bearer`
    (`mcp.registration.describe_from_manifest`).
  - **The log names the code that ran.** The header now carries a hash of the
    program's source and the time of its newest file, so a log can be matched
    to its code.
  - **Found by the real runs after these fixes:**
    - **What last worked never replaces what is registered unasked.** The
      remembered client bridge verified, the trials stopped, and the osc-mcp
      entry the operator had was never tried. Both are now judged
      (`mcp.known_good.try_current_too`).
    - **The current registration keeps its name.** Your osc-mcp entry would
      have become `build-opensuse-org-stdio` (`current_keeps_name`).
    - **One address, one instance.** A second server for build.opensuse.org was
      told to read `BUILD_OPENSUSE_ORG_STDIO_BUILD_OPENSUSE_ORG_PASSWORD`,
      because the two reached different hosts (`same_address_one_instance`).
    - **Template fixes reach kept entries.** An older copy of a generated
      bridge is refreshed in place before its trial, when its manifest is of the
      schema the current template reads. A script this program did not write is
      never touched (`mcp.discovery.reuse.refresh_generated_scripts`).
- **The second run on the other machine (2026-10-18, pid 60022).**
  - **A bridge's client is installed, or the bridge is broken.** Both client
    bridges drove `osc`, which that machine did not have. A missing client is
    now installed before the trial (`[mcp.discovery.clients.<name>] install`,
    `mcp.discovery.clients_install_missing`), for new entries and existing ones
    (read from their manifest). A bridge whose client can't be installed is
    broken and says what to install. It is never "waiting for a credential".
  - **The installed program, not a workspace build.** build-opensuse-org ran
    osc-mcp built in the agent workspace, beside `/usr/local/bin/osc-mcp`. When
    the installed program answers alike, the entry runs it
    (`mcp.registration.prefer_installed_program`).
  - **Login slots named for what they hold.** `OSC_MCP_USER`/`OSC_MCP_PASSWORD`
    were asked for as `BUILD_OPENSUSE_ORG_OSC_MCP_*` with "shape unknown". They
    are now `{SLUG}_USERNAME`/`{SLUG}_PASSWORD`, the names the client bridge
    reads, and are described as that account's
    (`mcp.registration.name_by_role`, `name_by_role_parts`). Tokens and keys
    keep their names.
  - **Whose login it is.** build.example.net was given build.opensuse.org's login
    and refused it. Guidance now names the account the address's organisation
    issues (`mcp.bridge.generated.auth.account`). A login refused by one service
    and accepted by a service of another organisation is reported as that
    service's login (`name_other_service_login`). Secret parts are compared by
    fingerprint, never by value.
- **Four addresses from one run, each wrong a different way (2026-10-18).**
  - **Broken waits for nothing.** build-example-net's CLI copy was found broken (its
    manifest read `BUILD_EXAMPLE_NET_PASSWORD`, which the entry never passed) and was
    written anyway as "waiting for a credential", over two bridges that could
    work. A candidate proven broken (it does not start, its entry disagrees
    with its manifest) is now left out of every waiting list and the writer.
  - **Session variables keep their own names.** Scripts reading `SSH_AUTH_SOCK`
    got `${BUILD_EXAMPLE_NET_SSH_AUTH_SOCK:-}`, a name nobody has, and it was listed
    as something to export. Variables the login session sets
    (`mcp.env_contract.session_variables`) are written as `${NAME:-}`, are
    never renamed per server, are never credentials, and are never asked for.
    An existing entry that renamed one is written back this way.
  - **The same organisation is not the same service.** `dashboard.qa.example.net`
    (Example's QA dashboard) became smelt.example.net's "native endpoint" because both
    are in `example.net`. It then closed every lower tier and replaced smelt's
    working GraphQL server. A sibling host now needs a tie
    (`mcp.registration.identity.organisation_needs_tie`): only generic labels
    (`api.`, `mcp.`), one of the address's product words, or the server naming
    one. The record's own word no longer vouches for it either.
  - **A closed tier still judges what is registered.** A stage the tier policy
    closes re-checks the existing registrations at that tier
    (`mcp.discovery.reuse.recheck_when_blocked`), in both modes.
  - **One test for an API, wherever it is asked.** sd.example.com's customer API
    was detected behind its login, then "confirmed" absent ("answered 401") from
    the agent's record. `api_kinds.confirm` now accepts the same login-wall
    evidence `detect` does. Between two servers waiting with nothing proved, the
    one built for the detected API takes the slot
    (`mcp.discovery.auth.prefer_detected_api`).
  - **Missing is not refused.** build.example.net was told it "refused every login"
    when every route answered "not set" or "no SSH key". Such refusals
    (`mcp.verification.login_check.missing_credential_markers`) are now reported
    as "nothing on this machine can log in", with what to supply for each route.
  - **A reference stays a reference.** This is where the literal user names in
    `.mcp.json` came from. Reuse expanded an existing entry's
    `${BUILD_OPENSUSE_ORG_USERNAME}` to launch it, and the writer wrote the
    expansion. The reference is now written back
    (`mcp.discovery.reuse.keep_operator_references`). A literal that equals what
    the operator exports under the same name becomes `${NAME}` again (compared
    in memory, never shown).
  - **One generated script is one server, wherever it lives.** The first real
    run after these fixes wrote build.opensuse.org's client bridge twice, once
    from the project's `bridges/` and once from the run's own directory. A
    script an interpreter runs is identified by its file name and the hosts it
    reaches (`mcp.registration.fold_script_extensions`).
  (`tests/test_mcp_broken_session_and_siblings.py`.)
- **A setting is proved by the tool that reads it, on the binary that runs.**
  build-opensuse-org shipped with `OSC_MCP_API=https://build.opensuse.org`. Only
  `search_packages` reads it, and it needs three arguments, so it was never called
  and its "unexpected api address format" never surfaced. The form trials only
  varied the website host, and a login refused on the website was blamed on the
  credential.
  - **Setting probes.** Each read tool nobody could call is asked once with stand-in
    arguments of the schema's types, read only for a setting error
    (`mcp.verification.login_check.probe_settings`, `setting_probe_max`,
    `setting_probe_values`). Never after a refused login.
  - **API hosts in the form trials.** The hosts that offered the address's login
    (same product) are tried in every form (`mcp.verification.address_forms.try_api_hosts`).
    A refused login with an address setting off the API host is tried there first;
    only acceptance clears it.
  - **Literals you export.** A literal credential equal to a variable you export
    for the address makes the entry read that variable.
  - Checked on the real osc-mcp here: from the wrong host, the program found
    `api.opensuse.org` itself and the login was accepted.
  (`tests/test_mcp_setting_proved_by_its_tool.py`.)
- **Real runs on this machine drove these: identification behind a login, every route and key, honest reports.**
  - **Behind a login.** sd.example.com came to answer `401` on every API path, and
    "product unknown" undid everything downstream. A `401`/`403` in an API's form,
    while a path that cannot exist answers otherwise, now identifies the product
    and the API kind (`mcp.discovery.api_kinds_behind_login`,
    `mcp.discovery.auth.recognise_behind_login`). Products are also recognised by
    their protected API paths (confluence.example.com's page redirects to single
    sign-on). No answer while identifying counts as undecided.
  - **Bridges.** A "needs no login" call answered with a login refusal is no longer
    "broken" (`mcp.verification.public_call_login_markers`).
  - **Client bridges.** One bridge per offered route (password, SSH signature), each
    with a manifest named by route and content. An entry whose manifest reads other
    variables is broken and replaced (`mcp.discovery.reuse.check_manifests`). The
    key file is optional: the standard key files are tried the way ssh does (the
    private key without an agent), and each key is reported as refused (not
    registered) or unusable.
  - **Credentials.** A location (`SSH_AUTH_SOCK`, `*_KEY_FILE`) is not a credential
    (`mcp.secret_policy.location_words`). The same-login trial fills every slot,
    not one. `--mcp-server a,b` is split. `${NAME:-default}` is expanded in trials
    as Claude Code expands it.
  - **Reports.** The file check, the identification note and "your login was
    refused every way" are reported for every address, and only an accepted login
    silences the last.
  (`tests/test_mcp_behind_login_and_real_runs.py`, client-bridge tests updated.)
- **Every address that has a server ends with an entry in `.mcp.json`, checked.**
  confluence-example-com went missing like developers-google-com, in the same run.
  The existing `uvx mcp-atlassian` was "held" against itself ("Claude Code connects
  to the existing confluence-example-com, and confluence-example-com proved less"), and
  the package stage read "✔ OK" for a server refused minutes later.
  - **A copy of the existing server is the existing server.** Hold rules recognise
    it by the same-server key, not only its name or an identical launch (so
    `mcp-atlassian@0.23.1` matches `mcp-atlassian`), and the copy takes the
    existing entry's credential slots.
  - **The result is checked.** After each address and again at the end of the run
    (`mcp.registration.check_in_file`): an entry written but lost is written again,
    a working kept entry is recorded, and an address that has a server but no entry
    is reported (FAILED, with every refusal) beside the final verdict.
  - **A stage whose working server was refused reads PARTIAL**, naming the refusal
    and what is left waiting.
  (`tests/test_mcp_every_server_in_the_file.py`.)
- **A working server never vanishes from `.mcp.json`: belonging re-judged, kept entries written.**
  developers.google.com/knowledge/api: the CLI's connected `developers-google-com`
  verified with its credential, but was refused at reuse ("nothing ties it to
  developers.google.com") before the endpoint stage found the target documenting
  that very URL. It was never judged again. The agent's copy came without the
  credential (401), the writer would not let it replace a connected entry, and
  nothing was written.
  - **Re-judged when evidence arrives.** A proven server refused only for want of
    evidence is kept aside (`TargetOutcome.identity_pending`) and judged again
    after every stage, counting the stage's own findings
    (`mcp.registration.rejudge_identity`). The gate's reason is now logged.
  - **Evidence and credentials belong to the server.** Copies of one server carry
    each other's provenance and, where missing, credential slots
    (`inherit_credential_slots`). A remote copy refused anonymously takes the
    slots of an existing entry for the same server.
  - **Kept means written.** A working entry kept in the CLI is written to
    `.mcp.json` too, portably (`record_kept_entries`), and never over an entry
    already there. The connected-entry rule no longer holds against the entry
    itself, and the report says "in the claude CLI only" when that is the case.
  (`tests/test_mcp_kept_entries_and_rejudged_identity.py`.)
- **"Try again later" while identifying settles nothing; replacements must prove more.**
  The run on this machine showed four more gaps.
  - **sd.example.com answered `502` while it was identified.** The run said "product
    unknown", missed the customer API, and a waiting mcp-atlassian closed the
    bridge stage. Identification cut short by a temporary answer is now asked
    again (`mcp.discovery.identify_retries`). While still undecided, nothing
    unproven settles the address (`undecided_keeps_searching`), and the report
    says when to run again.
  - **build.example.net's openapi-proxy, which still searched packages, was replaced**
    by a server waiting for an unset variable. An existing entry shown short of
    its job now stays over one that passed less and proved no login
    (`mcp.registration.keep_more_proven_existing`).
  - **Your login refused every way** (encoded login as a password, SSH signature)
    is now said once, with what to check, instead of a request for another variable.
    The in-use verdict is logged only after the anonymous comparison.
  - **"Claude Code 2.1.259 does not support this model"** is its own failure kind
    (`[mcp.agent.failure_patterns].unsupported_model`). The other runtime is tried
    with the same model, else this runtime's default, and the report says to
    update it or set `mcp.agent.model`.
  (`tests/test_mcp_undecided_and_more_proven.py`.)
- **The address's job, what the operator holds, and Claude Code's own view decide.**
  The other machine's log showed both failures. build-example-net's working client
  bridge (`claude mcp list`: connected) was judged "`${ICA_MCP_PYTHON3_13}` is not
  installed", because `claude mcp get` prints `${NAME:-default}` as `${NAME}`.
  It was replaced by an mcp-openapi-proxy that "worked" on a public `/about`:
  no task matched its path-named tools, so `EXTRA_HEADERS` was written
  `${…:-}`, while the shell exported `BUILD_EXAMPLE_NET_BASE64_OF_USER_PASSWORD`.
  sd-example-com got "OAuth@sd.example.com, Basic@api.example.com; chosen: token": a
  foreign API host, the password ruled out by the challenge, and only
  jira.example.com's token tried.
  - **Entries as Claude Code keeps them.** CLI entries are read from
    `~/.claude.json` (user, and this project's local scope), with
    `claude mcp get` as fallback (`mcp.discovery.reuse.claude_config`).
    Variables named after a file are remembered (`path_variables_file`) and
    resolved back. An entry Claude Code reports connected is never judged by
    the run's own launch (`connected_outranks_trial`), and yields only to a
    server that proved the job. CLI copies are removed scope by scope.
  - **The job comes from the address.** The tasks of its recognised product and
    detected API kinds (`mcp.contract.address_job`). A working server that
    cannot be asked them settles nothing; "works without a login" needs a
    passed task (`public_needs_the_job`). The build-service task also matches
    `get_my_notifications`.
  - **Logins by evidence.** `api.{domain}` counts only when recognised as the same
    product (`api_host_same_product`). A method a challenge left out is tried
    with a stand-in (`try_unoffered_methods`). Methods the operator holds come
    first (`prefer_held_methods`), and generated bridges follow the plan before
    an API kind's default (`mcp.bridge.generated.auth.from_plan`).
  - **What the operator holds** (`[mcp.discovery.auth.inventory]`, names and shapes
    only) fills a waiting server's slots for its login check (`borrow_held`).
    Accepted, the entry reads those variables:
    `"EXTRA_HEADERS": "Authorization:Basic ${BUILD_EXAMPLE_NET_BASE64_OF_USER_PASSWORD}"`.
    If only the parts are held, the slot is built at start-up by a small
    composer (`[mcp.registration.compose]`). A login held for another address of
    the same organisation is tried once (single sign-on) and only suggested. A
    refusal from a token other hosts' entries send is `foreign_credential`, and
    is not a verdict here.
  - **Literal credentials** in a CLI copy that shadows a proved entry are removed
    when every credential it reads is exported (`replace_literal_cli_copies`).
  - **Claude Code's start-up wait.** A first start slower than `MCP_TIMEOUT` (or
    30 s) is reported with how to warm another machine, and the program's
    environment file exports a wait that fits (`[mcp.registration.client_timeout]`).
  (`inventory.py`, `tests/test_mcp_address_job_and_held_credentials.py`.)
- **"Try again later" is not a failure; one server is one entry, in every mode.**
  bugzilla.example.com ended an ordinary run (no `--exhaustive-mcp`, no
  `--the-best-mcp`) with `bugzilla-example-com` and `bugzilla-example-com-mcp-bugzilla`:
  the existing entry got a `429 Too Many Requests` the run's own checks had
  provoked, was "repaired" once the limit ended, and an agent's copy of it
  (`--read-only` added) was written beside it, because every comparison was of
  whole command lines.
  - **Temporary verdict.** `429`, `502`/`503`/`504` and "rate limit"-style
    answers are judged `temporary` (`login_check.temporary_markers`,
    `temporary_statuses`), asked once more after `temporary_retry_seconds`, and
    then inconclusive for the login check, the usage contract and the Claude
    Code check -- never a failure, never a reason to repair.
  - **Pacing and one answer per call.** Calls to a host share one budget
    (`[mcp.verification.pacing]`: `min_gap_seconds`, `max_per_minute`); the same
    `tools/call` on the same launch is answered once per run. Waits are not
    counted as the server's latency, and `--the-best-mcp`'s repeated handshakes
    are always asked anew.
  - **A repair must be the cause.** A repaired variant is kept only if the
    original, asked again, still falls short: something left to repair, less
    proof of the login, or less of the usage contract.
  - **Copies are folded** (`mcp.registration.fold_copies`, `same_server.py`): the
    same package (by `mcp.env_contract.package_sources` launchers, versions
    aside) reaching the same hosts, or the same URL on the same transport, is one
    server. The best-proved copy is written, the existing one winning a tie; the
    others stay start-up fallbacks. Different servers are never folded, so how
    many an address keeps is unchanged in the default run and under both flags.
  - **No `claude mcp remove` for a project-file entry**, which the CLI cannot
    remove (rc=1); the file is rewritten in place.
  (`tests/test_mcp_try_again_later_and_copies.py`.)
- **Credentials you hold come before an SSH key; the right API host.**
  build-example-net and build-opensuse-org kept failing.
  - **Method order.** `method_order` is now token → API key → password → client
    (an SSH key), and a password is chosen only where a challenge offered it
    (`offered_only_methods`).
  - **API host.** The client bridge's host is the one that offered the chosen
    login, the API host first (`client_bridge.route`): api.opensuse.org, not
    build.opensuse.org's own login.
  - **Key paths are checked.** A key-path variable holding anything but an
    existing `.pub` file is named plainly, by a value-shape rule (`path =
    true`) and by the bridge before the client runs (`file_parts`).
  - **Credential defaults reported.** A credential written into an existing
    entry as a `${NAME:-value}` default is reported (*credentials written into
    entries*).

- **bugzilla-opensuse-org no longer goes missing.** A public server was
  judged "waiting", a mode setting was treated as a credential, and a broken
  CLI copy was kept in place. Now:
  - **Unset settings.** An unset `${NAME}` in a setting launches without it
    and is written `${NAME:-}` or left out
    (`mcp.registration.optional_unset_settings`). The new healing repair
    `drop_unset_setting` fixes entries that crash on the literal.
  - **Public servers.** A server whose reads work with no credential is
    working, its credential optional (`login_check.try_without_credential`,
    `public_works_without_login`).
  - **Existing entries.** An existing entry counts as answering only as Claude
    Code runs it (`mcp.verification.judge_existing_as_written`: launched as
    written, not failing in `claude mcp list`). Otherwise it's replaced, never
    kept.
  - **New tests:** `tests/test_mcp_public_server_and_unset_settings.py`.

- **Repair until it works; single sign-on is normal (plan phase 4).**
  - **Repair loop.** Every failure is classified and repaired where the
    program can (new module `repair.py`, `[mcp.repair]`): a credential moved to
    the variable the server reads, a header name fixed, a client bridge for a
    login only a client performs. Each variant is proved again. Trials expand
    `${NAME}` the way Claude Code does.
  - **The generated client bridge.** New module `client_bridge.py`,
    `[mcp.discovery.clients.osc]`. It drives the service's own client: argv
    only, read operations only. It never depends on how the client is set up
    on this machine: it writes the client's own configuration from the entry's
    named variables (user name and SSH public key path for `Signature`, user name and
    password for Basic) and isolates HOME and XDG, so it waits for those
    variables like any entry.
  - **Checklist and recheck.** The report lists *what only you can do*, with
    `--mcp-recheck <host>` (new option, `mcp.recheck`), which proves addresses
    again from their existing and last working entries only.
  - **The same user name and password on several servers** (single sign-on)
    is never reported; only a token or API key on two servers is
    (`mcp.discovery.auth.instance_bound_patterns`).
  - **New tests:** `tests/test_mcp_repair_and_client_bridge.py`.

- **Remember what worked; read every way to log in (plan phase 3).**
  - **The record.** Entries that did the job are recorded with `${…}`
    references only (`[mcp.known_good]`, new module `known_good.py`). The last
    one is tried first on every run, and the report says *what changed since
    it last worked*. Entries whose only successes were public calls are named
    as never proved.
  - **Every challenge, and the product.** Every `WWW-Authenticate` challenge
    is read on the target and its API hosts and classified
    (`[mcp.discovery.auth.methods]`, new module `login_methods.py`). The
    product is recognised from a catalogue (`[mcp.discovery.products.*]`) that
    records how it issues credentials.
  - **Choosing the method.** The method follows `method_order` (token, API
    key, client, password). Token types that can't read are excluded, and
    password candidates rank lower when a token can read. The report's *how
    to log in* step gives the advice.
  - **Signature is a client's.** `Signature` (api.example.net), Negotiate and NTLM
    are no longer read as "a token".
  - **New tests:** `tests/test_mcp_lookback_and_login_methods.py`.

- **Working means doing the job (plan phase 2).** Every server that failed
  this week had passed the program's checks.
  - **The usage contract.** A server is accepted only when it passes its
    usage tasks (`[mcp.contract]`, new module `contract.py`). These are each
    kind's value-free tasks (`[mcp.contract.products.*]`) plus the operator's
    own per-address checks (`[mcp.contract.targets."<host>"]`, in their
    configuration). They're judged on real content, optional ground truth read
    directly from the target, and expected values that are never printed.
  - **Replacement by score.** A replacement must pass more of the contract
    than the entry it replaces (`contract.score`).
  - **Claude Code's own use.** After an entry is written, Claude Code itself
    uses it (`mcp.registration.check_through_claude`, default on; new module
    `claude_check.py`). Missing variables are found before any request, the
    tool's own answer is judged from the session's events, and the check is
    inconclusive when it can't run.
  - **Fewer calls.** The login check and the contract share one answer per
    tool call within a session.
  - **New tests:** `tests/test_mcp_usage_contract.py`. The test suite never
    starts a real Claude Code session.

- **A login is proved by a call that fails without it (plan phase 1).**
  build-example-net "worked" for days on `get_about`, which build.example.net answers
  anyone; every call needing a login failed. Its login sat under a variable
  `mcp-openapi-proxy` never reads, behind an auth type the proxy doesn't
  implement, under a header named `Bearer`, and in the end it was another
  instance's login.
  - **Anonymous comparison.** Each login check is repeated with no credential
    (`mcp.verification.login_check.compare_with_anonymous`, also in the
    generated bridge's direct check). A success anybody gets is unproved; an
    identical refusal says the credential may never have been sent
    (`remedies.same_as_anonymous`).
  - **Unread credentials.** A credential variable the server's own source
    never mentions, while it reads a credential under another name, is flagged
    (`flag_unread_credentials`). Installed packages are read from the uv/pipx
    and npx caches (`[mcp.env_contract] read_packages`, `package_sources`,
    `package_read_patterns`).
  - **Header names.** A header-name variable must hold a header name, not a
    scheme word (`value_shapes` `forbidden`).
  - **Stand-ins never lock an account.** They replace the user name as well
    (`identity_patterns`, `placeholder_user`), and a credential refused twice
    in a run isn't tried again (`max_refused_logins_per_credential`).
  - **One login per instance.** The same credential value sent to different
    servers is reported when it's a token or API key
    (`mcp.registration.compare_credential_values`).
  - **New tests:** `tests/test_mcp_login_proof.py`.

- **Proof decides across tiers, and a login with nothing behind it is an
  account problem.**
  - **sd.example.com: an unchecked package beat a proved bridge.**
    `@atlassian-dc-mcp/jira` was never login-checked, because its entry named
    the host bare, yet it kept the address by tier order against the agent's
    customer-API bridge, whose login was accepted. Before that, mcp-atlassian
    had been replaced for one 93 s timeout. Now:
    - an accepted login takes the slot of the least-proved candidate across
      stages;
    - a bridge whose tools are a detected kind's operations is that kind's
      bridge (`mcp.discovery.api_kind_min_shared_tools`);
    - the in-use credential is offered to entries that name their host bare;
    - an existing registration that times out is retried with a longer
      deadline (`mcp.packages.slow_start_retries`,
      `slow_start_timeout_factor`), and never replaced for a timeout while
      `claude mcp list` reports it connected.
  - **build.opensuse.org: an account-less login counted as working.** Its
    login got an HTML 404 for every path while a made-up one got 401. Now:
    - calls that all fail alike are repeated with a stand-in, and a different
      answer means the service has no account behind the login: no API
      access, `remedies.no_account`
      (`mcp.verification.login_check.compare_with_standin`);
    - an identity tool's 404 means the same (`identity_tool_patterns`);
    - a web page is the gateway's answer, not the API's
      (`gateway_page_patterns`);
    - a server whose every call fails is not working
      (`failed_calls_unverify`, `failed_calls_min`).
  - **Agents are told.** The package, bridge-authoring and strategy skills and
    the agent brief say all of this.
  - **New tests:** `tests/test_mcp_account_and_tier_proof.py`.

- **Proof decides, not order: the documented bridge is always tried, and a
  working credential is not blamed for a broken bridge.** In agentic mode,
  sd.example.com's agent wrote its own customer-API bridge. It asked for a token
  *or* a user name and password, and read the latter without declaring them.
  It was refused with `401`, that ended the bridge stage, and the report
  blamed "the credential this machine already uses". The bridge generated from
  the documented operations (a bearer token, as the service documents) was
  never built. mcp-atlassian answered `[]` with a working token and a dead one
  alike, and a CLI copy of it would have outranked any bridge written to the
  file.
  - **Both modes try the generated bridge** before the address settles, unless
    something proved access (`mcp.discovery.generated_bridge_settles_first`).
    The agentic bridge stage merges it beside the agent's own.
  - **Candidates are ranked by proof** (accepted, unchecked, unproved,
    refused), not by which arrived first.
  - **Refusals are checked.** A refusal from a candidate whose credential
    method nobody checked is unconfirmed and doesn't end the search
    (`mcp.verification.login_check.unconfirmed_rejection_settles`). A
    credential accepted by one candidate and refused by another, compared by
    fingerprint, marks the refusing candidate broken. Bridges built from an
    API description, a 401 challenge or a kind's `security`, and published
    adapters, record where their method came from.
  - **Unproved existing entries are replaced in the CLI too** when a proven
    bridge takes over (`mcp.registration.replace_unproved_existing`).
  - **Undeclared credential reads are named as needed**
    (`mcp.registration.name_undeclared_credentials`).
  - **One variable per credential.** A candidate proved with another entry's
    credential reads that entry's variable
    (`mcp.verification.login_check.reuse_variable_names`).
  - **Agents are told.** The agent brief and the bridge-authoring,
    package-discovery and strategy skills say all of this.
  - **New tests:** `tests/test_mcp_proof_over_order.py`.

- **An echo of the request is not an API, and an unproven bridge does not
  displace a working entry.** smelt.example.net answered
  `GET /rest/servicedeskapi/info` with its HTML 404 page. The page repeats the
  path, the path was the Jira Service Management marker, and any status
  counted. So smelt "served" a customer API, a bridge was generated for it, and
  that bridge, waiting for `SMELT_EXAMPLE_NET_TOKEN`, was written over the
  `smelt-example-net` entry for `mcp-graphql`, which worked.
  - **Recognition is strict for every kind, in both modes.** Only a `2xx`
    counts. The answer must be in the kind's declared form (new `content_types`
    and dotted `required_keys`). A success marker the request itself carries is
    ignored. A control request to a sibling path that cannot exist must not be
    recognised too (`mcp.discovery.api_kind_control_name`).
  - **Agent records are checked.** A kind an agent records in
    `findings.api_kinds` is held to the same test before anything is built
    for it.
  - **The service-desk kind** is recognised by `version`, `platformVersion`
    and `_links.self`, and GraphQL by `data.__schema`.
  - **Bridges must reach their API.** An operation marked `public = true`
    (the service desk's `get_service_desk_info`) is called on every bridge
    generated from its kind, or written by an agent that names `"api_kind"`.
    It must return real content, or the bridge is discarded as broken rather
    than registered as waiting for a credential.
  - **Working entries stay.** An existing entry that answered during the run
    keeps its place against an unproven candidate, which is held back and
    reported (`mcp.registration.keep_answering_existing`).
  - **Agents are told.** The endpoint, package, bridge-authoring and strategy
    skills and the agent briefs say all of this.
  - **New tests:** `tests/test_mcp_echo_is_not_evidence.py`.

- **A credential is proved by an accepted call, not by a listing.**
  sd.example.com's `uvx mcp-atlassian` started, listed 49 tools and answered
  `ping`, and every tool call failed with `500 … /servicedesk/customer/portals`.
  With every credential variable holding a real value, both pipelines now call
  one read-only tool (a `readOnlyHint` tool, or one named by
  `mcp.verification.login_check.tool_patterns`, never one matching
  `never_patterns`) and judge the answer: accepted, rejected, or no API access
  (`failure_fields`, `rejected_markers`, `no_access_markers`). A refused
  credential makes the server *wait for a working credential*: written with its
  slot, never counted as working, the remedy named (`mode = "pending"`; `warn`,
  `off`; `INITIALIZE_CLAUDE_AI_MCP_LOGIN_CHECK`). The check runs in candidate
  trials, the entry launched as written, re-checks and reuse; a start-up failure
  that names a refused login is reported as one, and healing does not try to
  repair it. The verification table has an `authenticated` column; the
  verdict, briefing and outcome lines say "credential refused". Generated
  bridges mark their tools `readOnlyHint`, and their own login check speaks the
  same verdicts. (`mcp_auto_discovery/login_check.py`, `tests/test_mcp_login_check.py`.)
- **An empty answer is not access.** sd.example.com's customer account got `[]`
  from mcp-atlassian's `jira_get_all_projects`; read as "accepted", that would
  have settled the address and the customer-API bridge would never have been
  built. In both modes now: a structurally empty answer is "answered with
  nothing" (`login_check.empty_answers_prove_nothing`, `empty_total_fields`,
  `empty_list_fields`); a refusal from any call outranks acceptance from another
  (`login_check.summarise`); unproved access does not settle an address whose
  target serves another bridgeable API kind (`login_check.unproved_settles`),
  and a bridge for it with proven access takes the slot; with no credential at
  all the alternative API is named in the waiting line and briefing
  (`login_check.remedies.alternative`); a replacement entry's briefing lists
  its tools beside the ones it replaces
  (`mcp.instructions.replacement_tools_shown`). The randomness test for tokens
  now compares against the entropy a random string of that length is expected
  to have. (`tests/test_mcp_empty_is_not_access.py`.)
- **A credential is recognised by context and use, never written into the file,
  and checked against its slot.** build.example.net's entry carried its login token
  as the default of `API_HEADERS`, in a shape the server cannot use, while the
  crashing CLI copy kept loading. In both modes now: header-list and basic-auth
  names carry a credential by their shape (`mcp.secret_policy.carrier_patterns`);
  randomness is judged against the value's alphabet
  (`opaque_value_min_entropy_ratio`), with identifiers (commit, sha, digest …)
  exempt; a value equal to a credential an existing entry uses is a credential
  under any name (fingerprints only); the writer turns any literal credential
  left in an entry into a `${…}` reference; a known value is checked against
  `[mcp.discovery.auth.value_shapes]` and a mismatch is reported with the exact
  form for the service's login method (its API description, else its 401
  challenge); a credential to encode by hand ranks lower
  (`mcp.ranking.penalties.encoded_credential`); an existing registration proved
  not to start is replaced in the claude CLI, and a differing CLI copy is
  reported as shadowing the file; agent kinds such as `package` are mapped
  (`mcp.agent.kind_aliases`); a waiting line never ends in an empty "--".
  (`tests/test_mcp_credential_context.py`.)
- **A setting's form is proved by the tool that parses it.** build.opensuse.org's
  entry carried `OSC_MCP_API=https://api.opensuse.org`; osc-mcp's API tools
  accepted it and only `search_packages` rejected it ("unexpected api address
  format"), so nothing caught it. In both modes now: the check after the
  handshake calls up to `max_calls` read tools -- names beginning with a
  `read_verbs` verb are eligible, required arguments are filled from
  `login_check.arguments` or the tool's own schema (default, const, example,
  enum) -- and keeps every answer; "misconfigured"
  (`misconfiguration_markers`) is a verdict of its own; an address-valued
  variable is then tried in `[mcp.verification.address_forms]` until the very
  tool that rejected it accepts one, which is written; a registry variable's
  own description decides host or URL before its name does
  (`address_fill.description_*_patterns`); read tools nobody could call are
  reported as not exercised; the verification table gains a `configured`
  column; skills and the repair brief say a setting's form comes from the
  server, not its name. (`tests/test_mcp_setting_forms.py`.)
- **A server broken by the operator's own configuration is isolated, not lost.**
  build.opensuse.org finished with nothing: osc-mcp takes its API address from
  the operator's oscrc before `OSC_MCP_API` and refuses example.net by design, so
  every launch on a machine set up for build.example.net died with "failed to get
  credentials". In both modes now: a start-up failure that a stand-in
  credential leaves unchanged (`mcp.packages.credential_probe.volatile_patterns`)
  is not a credential wall; such a server is relaunched with a configuration
  scope of its own (`[mcp.healing.isolation]`) and keeps it when that helps,
  written relocatably; new healing repairs `isolate_user_config` and
  `restore_path_defaults`; existing entries naming `${ICA_MCP_*}` resolve as
  the program defines them, or are reported as copied from another machine;
  a candidate discarded by the start gate stays the best lead, ahead of a 404;
  a schema refusal names the fix (a newer or patched build); agents' copied
  placeholders are refused (`mcp.agent.placeholder_patterns`) and the skill
  examples no longer carry copyable paths; skills and the repair brief cover
  per-user configuration and building from current upstream.
  (`tests/test_mcp_user_config_interference.py`.)
- **No API access is not an answer: the API the account can use is found and
  bridged.** sd.example.com's token was valid (its Service Desk API answered it),
  but the customer account was sent to the portal by the core Jira API that
  mcp-atlassian speaks. Now, in both modes: a server whose account has no API
  access does not settle the tier descent
  (`mcp.verification.login_check.no_access_settles`), and gives up its
  provisional slot to anything found below it; an API kind may list its
  documented read-only `operations` and `security`, and the bridge stage
  generates a bridge from them -- `jira_service_management` does so for the
  customer API (`get_my_requests`, `get_request`, `get_request_comments`, …);
  the credential existing entries already use for the same host is tried on
  the new server, in memory, with the one read-only call
  (`mcp.verification.login_check.borrow_in_use`); a CLI registration proved
  unusable is replaced by its replacement rather than left to shadow the file.
  Tools named `get_my_*` / `list_my_*` are tried first by the login check. The
  agents of tiers 2 and below are shown the operations
  (`mcp.agent.api_kind_brief`). (`tests/test_mcp_api_access_by_account.py`.)
- **The credential an existing entry uses is checked.** On another machine the
  existing `sd-example-com` entry held its token as a literal; it is rightly never
  copied, so the exported variable was unset and the login check never ran --
  the token Claude Code sends went unjudged. That value (a literal, or the
  default of an unset `${NAME:-…}`) is now kept in memory as a redacted
  `Secret` for the one read-only call, in both modes, and the verdict names its
  source (`mcp.verification.login_check.check_in_use`). The agents' existing-
  registration brief and the skills say so; the value is still never written.
  (`tests/test_mcp_login_in_use.py`.)
- **One credential, one instance.** When an entry in the file, or one written
  earlier in the run, reads a credential variable for a different host, the new
  entry keeps the key the server reads and points it at
  `mcp.registration.shared_secret_template` (`{SLUG}_{NAME}`). Entries already
  sharing one across services are reported ("shared credential variables").
- **Every instruction names the variable the entry reads.** The waiting-for-a-
  credential lines, the briefing, the export line and the verdict named the
  server's key (`JIRA_PERSONAL_TOKEN`) where the entry read a namespaced
  variable.
- **A local entry's identity includes the hosts it names.** Two entries with the
  same launch command and different `JIRA_URL`s are no longer duplicates.
- **Credential descriptions are consistent.** `_PERSONAL_TOKEN`/`_PAT` names are
  described (`mcp.discovery.auth.name_shapes`); a shape learned for one target
  serves a sibling of the same package; a local server's hint names its host.
- **A section-relative read must name a packaged key.**
  `tests/test_config_fallbacks.py` now resolves single-word keys through their
  `section(...)` and refuses one missing from `data/*.toml`.

- **A generated bridge asked for "a header" and "a value".** build.example.net's
  bridge was registered with `MCP_BRIDGE_AUTH_HEADER` and
  `MCP_BRIDGE_AUTH_VALUE`, and nothing said that the server wants HTTP Basic
  or that the value must be `Basic` plus base64 of `user:password`. The value
  there could never have logged in. Now:
  - the method comes from the API description's security scheme, else the
    server's 401 challenge (`basic realm="API login"`) to a documented GET
    (`bridge_auth`, `[mcp.bridge.generated.auth]`);
  - each part is exported under a name built from `variable_template`
    (`BUILD_EXAMPLE_NET_USERNAME`, `BUILD_EXAMPLE_NET_PASSWORD`) and described in
    words. The bridge reads those same names, which the manifest gives it
    (`MANIFEST_SCHEMA` 2), and builds the header itself. The entry carries
    only the parts the method needs;
  - when the parts are exported, one authenticated GET checks the login and
    the report says whether it was accepted or rejected (`check_login`);
  - a credential in parts keeps every part when it is registered waiting for
    one (`prepare_provisional`), and a part described in words is reported as
    such rather than as "not a single opaque token".
  The bridge and package skills say the same.
  (`tests/test_mcp_bridge_auth.py`.)

- **One server's tool definition broke every request of the Claude Code
  session.** openSUSE's `osc-mcp` declares an input property named
  `path_repository jsonschema:` on `search_packages`. The Anthropic API
  accepts property keys matching `^[a-zA-Z0-9_.-]{1,64}$` only, so once the
  tool was loaded every message failed with "Property keys should match
  pattern". Discovery only asked whether a server answered. Now, in both
  modes:
  - every tool name and input property key, nested ones included, is checked
    against the client's rules (`mcp.verification.client_name_pattern`,
    `client_tool_name_pattern`) and recorded on the handshake
    (`schema_problems`);
  - such a server is refused by default, as a candidate, as an entry launched
    as written, and as an existing registration (reported as left in place).
    `mcp.verification.schema_problems = warn` registers it and names the
    problem, and `off` skips the check;
  - the verification table has a `schema` column, and the final verdict and
    `mcp_schema_problems` name the server, tool and key;
  - a generated bridge exposes a clean name for an API parameter the client
    would reject (`package[]` becomes `package_`) and still sends the API's
    own name.
  The skills say the same. (`tests/test_mcp_tool_schema_names.py`.)

- **build.example.net's bridge answered every call with HTTP 406, and its entry
  held a credential in plain text.**
  - The generated bridge sent `Accept: application/json` and nothing else.
    Open Build Service serves XML, so it refused every request. Each
    operation now records the media types its API description documents
    (OpenAPI `responses.*.content`, Swagger `produces`). The manifest records
    a fallback from `mcp.bridge.generated.accept`, and the bridge sends the
    operation's types, then `MCP_BRIDGE_ACCEPT`, then the manifest's, then
    `*/*`, so it never refuses a format.
  - The entry's auth variable was `${…_AUTH_VALUE:-<token>}`. That reads as a
    reference but is a literal in the file, and every check for bare literal
    secrets walked past it. The writer now strips a credential-shaped default
    from any `${NAME:-…}` reference, whatever produced it, leaves a path
    default alone, and notes why (`McpWriter._without_secret_defaults`).
  (`tests/test_mcp_bridge_accept_and_secret_defaults.py`.)

- **build.opensuse.org got no entry although its server was found four
  times.** The agents proposed openSUSE's own `osc-mcp` four times. Each time
  it was thrown away. In both modes:
  - **A search path read as a file.** `PATH=/usr/bin:/bin` was "a file that
    is not there", and the candidate was refused before its trial. A value of
    absolute directories joined by `os.pathsep` is now read as a search path
    (`portability.search_path`). Its directories are still checked for
    scratch space, they move with a configured root, and a missing one is
    never fatal.
  - **A credential in parts.** `osc-mcp` needs `OSC_MCP_USER` and
    `OSC_MCP_PASSWORD`. With a stand-in password its failure moved from
    "password not found" to "user not set", and the run concluded the
    credential was not the missing piece. A stand-in that moves the failure on
    to another credential-shaped complaint now leads to a search for the other
    part, by name. That covers variables the failure names and the secret's
    prefix plus `credential_probe.companion_suffixes`, bounded by
    `max_companion_trials`. The part found gets a slot, and the entry waits
    for all parts. A server that names its credential when refusing to start
    also gets the stand-in trial first. The startup-credential wordings
    include "failed to get credentials", "password not found" and "user not
    set".
  - **Agents** are told to declare every part of a credential under the exact
    names the server reads, and to propose a server as a candidate rather
    than describe it. The strategist had named the right server in prose,
    with the wrong variable names.
  - **The report** sorts every address into working, waiting for a
    credential, or nothing registered, in the summary box (a new per-phase
    `outcome_lines` hook), the summary line, the safety note, the final
    verdict and the JSON. An address with nothing says which entry the file
    lacks and names its best lead (`best_lead`), and the verdict lists it
    under `Not in ./.mcp.json:` (`mcp_not_registered`). An existing
    registration that did not start and was left in place is reported, with
    how to fix or remove it (`broken_existing`). The safety note no longer
    calls an address with an entry "missing", and no longer says every server
    was verified when some are still waiting for a credential.
  (`tests/test_mcp_credential_in_parts.py`, `tests/test_mcp_report_by_outcome.py`.)

- **smelt.example.net's server listed its tools and failed every request.**
  Its `NODE_EXTRA_CA_CERTS` named a bundle holding "Example CA 2023.1" and
  "Example CA Root" but not the "Example Trust Root" above them. This program
  verifies with partial chains on, as Python 3.13 and curl do, so every check
  passed. Node accepts only a chain that ends at a self-signed root, and
  `mcp-graphql` failed with `fetch failed`. A bundle an entry brought with it
  was never checked at all (`trust.env_for` keeps such values). Now, in both
  modes:
  - every trust-store file an entry names is checked against each https host
    the entry reaches, with partial chains off (`TrustStore.chain_complete`,
    `trust.check_named_bundles`, `mcp.trust.check_named_bundles`);
  - a variable that adds to its runtime's store is judged together with that
    store, which is asked of the runtime itself (`mcp.trust.runtime_stores`;
    Node prints its `tls.rootCertificates`). certifi was no stand-in, since on
    some distributions it is the host's store;
  - a file that falls short is replaced by the host bundle or the run's
    combined bundle, whichever carries the chain for every host. The note
    names the missing root, and the trial uses the same replacement;
  - when nothing on the machine carries the root, an entry whose runtime
    needs one (`mcp.trust.root_required_variables`) is refused in the stage
    and at publish, saying which root to pass with `--mcp-trust-ca`
    (`mcp.trust.refuse_incomplete_chain`).
  The package and bridge skills and the agent's trust brief say the same.
  (`tests/test_mcp_trust_chain_complete.py`, which runs a real TLS server
  with that chain shape.)

- **Agent repairs never ran, and what was written was not what counted.**
  - Every agent repair raised `TypeError` (`_note_turn_usage` was called
    without its stage) after the turn was paid for, so only the rules ever
    repaired. A repair the agent proves now also becomes the candidate's
    launch (`healing.adopt_launch`). Before, the writer rebuilt the entry
    from the unrepaired candidate and refused it.
  - An address whose every entry was refused still counted as working, and
    a run could end in SUCCESS with nothing written. After publish, each
    address's `verified` and `provisional` hold only what was written. Refused
    candidates move to `refused` in the report, and such an address stays
    PARTIAL, never SUCCESS.
  - `mcp.healing.enabled = false` also silenced the start check. It now
    turns off the repairs only. `mcp.registration.require_entry_starts =
    false` now keeps the candidate in the stage too, not only at publish.
  - At publish, an entry the stage already launched unchanged is not
    launched again.
  - A fallback no longer says an earlier entry "was left as it was" when it
    is about to be replaced, and it never picks a copy of an entry already
    chosen. It never picks a server waiting for a credential while a working
    one was found, outside `--exhaustive-mcp`. The refused entry's briefing
    line says what replaced it.
  - Reading a server's stderr while it was still writing could crash the
    stage. Relative arguments are resolved against the directory the trial
    ran in. A repair's note is added only once the repair is proven, and
    server errors put in stage findings are redacted.
  - Wording now follows what happened:
    - a local package is never called an endpoint that refused a client;
    - nothing says an entry "is written out" before the gates have run;
    - the bearer-token probe hint is offered for HTTP endpoints only;
    - "no working server" is used instead of "nothing was found";
    - the summary's "more waiting on a credential" counts only addresses
      that are not already working;
    - "no credential is needed" is said only when something was registered;
    - duplicate and retirement messages share one remove command, built
      from `remove_command`;
    - the `get_command` hint is filled in.
  - The skills, the TOML comments and README now say that a broken entry is
    repaired by the strategist, which has a section for it. They also say
    that a wrong registration is retired only under a name this program
    gives (otherwise it is reported with the command to remove it), and in
    the run that proves it. README documents `report_files`,
    `files_agree_within_seconds`, `client_env.strict` and
    `reuse.confidence`, explains the `…__KEY` shorthand, and its sample
    output matches the current messages. Two older CHANGELOG entries that
    later changes contradicted are annotated.

- **confluence.example.com had a server before the run and none after it.**
  Both of its candidates (the existing `mcp-atlassian` registration and
  `@atlassian-dc-mcp/confluence`) were waiting for a credential. The stage
  check that launches an entry as written looked only at verified candidates,
  so neither was launched that way until publish. There the chosen one died
  with `FileNotFoundError … | ) | ^`, the other was never tried, and the lower
  tiers were already closed. Now, in both modes:
  - a local server waiting for a credential is launched as written in the
    stage before it may settle the target, with the trial's stand-in in the
    credential slot (scrubbed from its output). The best candidate is tried
    first, and one that fails hands its place to the next
    (`mcp.healing.examine_pending`, `healing.EntryDoctor.standins`);
  - an entry refused at publish because it does not start is replaced by the
    next candidate the run proved (`mcp.registration.start_fallbacks`);
  - if nothing can be written, an existing registration that answered during
    the run is kept and reported (`<target> :: keep <name>`, `kept_existing`,
    `mcp_kept_existing`);
  - a failure names how the proven launch differs from the entry, by
    variable name and directory only (`mcp.healing.report_differences`), and
    the agent repairing it is told the same;
  - a server's error keeps the frame that raised it and drops layout-only
    lines (`probes._failure_lines`);
  - a local package waiting for a credential is no longer called "a real MCP
    endpoint that refused an anonymous client" or "the target's own MCP
    endpoint" (`types.waits_for_credential`, shared by both modes);
  - the final verdict no longer lists a refused entry as "registered with an
    EMPTY credential". It counts the entries it names, and no longer claims a
    local entry lacks its `${NAME}` slot;
  - the `mcp.registration.verify_as_written` and `require_entry_starts`
    comments and README rows now agree about which of them withholds an entry.
  The package and bridge skills and the agents' hard rules say an entry is
  launched from the client's directory, and that this includes one waiting for
  a credential. (`tests/test_mcp_pending_entry_starts.py`.)

- **Documentation, agent instructions and fallbacks that had drifted from the
  code.**
  - README defaults for `mcp.healing.repairs` (`withdraw_optional_reads`) and
    `console.palette` (`auto`) now match the TOML. `tls_weakening_patterns`
    and `github_token_ref` are documented.
  - The code fallbacks for `mcp.packages.query_templates` and
    `logging.max_bytes` / `logging.backups` now equal the TOML values.
  - The skills now say what the gate reads: a URL recorded in
    `due_diligence` closes the lower tiers, and one only in prose is never
    read. They also give the full list of selector suffixes, name a
    credential variable the same way in both credential sections, and say
    where `findings.*` and a bridge's `install` and files go. A valueless
    variable is dropped before the trial, which the minimal brief now says.
  - The duplicate warning says "Keep one and remove the other", so "stale"
    means only a registration proved wrong. The `allowed_tools` and
    `repair_brief` comments now describe what the code does.

- **One MCP.AI remote was registered for seven unrelated hosts.**
  `https://api.mcp.ai/p_examplep_administradores` (a Brazilian regulator's tool,
  published as `io.github.mcp-dir/examplep_administradores-mcp`) became the entry
  for gitlab, jira, confluence, openqa, sd and build on example.* and for
  github.com. Four causes, each fixed generally and in both modes:
  - the relevance gate matched product tokens as substrings ("example" in
    "examplep", "github" in "io.github.mcp-dir"). Words must now stand alone, and
    hosts match by whole labels (`grounding.text_names`, `mentions_host`);
  - grounding's "the vendor said so" read the URL path and the registry
    namespace as provenance. The path is never read; a hosting namespace
    (`mcp.packages.hosting_namespaces`) names the publisher's *account*, a
    reversed domain names its domain (`registry_owner`);
  - one endpoint accepted for unrelated addresses was only called a duplicate.
    A claim-based entry (`mcp.registration.shared_claim_bases`) accepted for
    an unrelated address is now refused, and the earlier one is withdrawn from
    `.mcp.json` and from `claude mcp` if this run added it
    (`mcp.registration.refuse_shared_claims`, `remove_command`). The phase
    re-judges the address that lost it. Each verdict records its
    `grounding_basis`;
  - reuse copied an opaque literal stored under a non-secret name
    (`SMELT_EXAMPLE_NET_HEADERS`). A single random-looking token is now a
    credential whatever it is called (`secret_policy.looks_opaque`,
    `mcp.discovery.reuse.opaque_value_min_chars` / `…_min_entropy`), and the
    replacement variable no longer repeats the entry's name.
  - a claim outweighed what the server said it was. A candidate on an
    unrelated host is now refused unless where it is, what it is configured
    for, or its own handshake names the target, and always if it describes
    itself as a marketplace or catalog (`grounding.identity_doubt`,
    `[mcp.registration.identity]`);
  - a registration proven wrong stayed in `.mcp.json` and the `claude` CLI and
    blocked its replacement. It is now retired from both, under names this
    program gives (`mcp.registration.retire_refused_existing`,
    `writer.retire`), reported as `stale_registrations`, and the agents are
    told it is NOT THIS ADDRESS'S. Each is judged again on the run's final
    evidence before it goes, a name another address in the run gives is never
    taken, a dry run changes nothing and says what a real run would do, and an
    entry left in place makes the verdict PARTIAL (`mcp_retired`,
    `mcp_wrong_registrations_kept`).
  - reuse matched an existing registration to an address by substring
    (`build.example.com` in `build.example.com.evil.net`); it now matches whole
    hostname labels (`grounding.mentions_host`).
  The endpoint and package agents and their skills are told the same rules.
  (`tests/test_mcp_substring_is_not_a_name.py`, corpus entry in
  `tests/test_mcp_regression_corpus.py`.)

- **A WebSocket upgrade no longer counts as an MCP endpoint on its own.** It
  needs an MCP subprotocol or route (`mcp.discovery.websocket_requires_mcp_evidence`,
  `websocket_mcp_subprotocols`, `websocket_mcp_path_patterns`). Other upgrades
  are hints, and a refusal status is never an upgrade.
- **A reply that proves nothing is no longer recorded as "does not speak MCP".**
  A non-JSON-RPC `2xx` is asked again (`mcp.discovery.inconclusive_retries`)
  and then recorded as inconclusive. Every rejection records the reply it saw,
  redacted (`mcp.discovery.reply_excerpt_chars`).
- **`build.example.net` missing from `.mcp.json` on a host whose store lacks the
  internal CA.** Discovery's HTTP client trusted certifi only, the preflight
  reported a certificate failure as "did not respond at all", the OpenAPI
  description (YAML, named only by Swagger UI) was never found, and each agent
  extracted the chain on its own and lost it. A certificate failure is now
  reported as one, with `--mcp-trust-ca` as the fix; the evidence GETs retry
  once without verification and without credentials (`tls_insecure_retry`),
  and the address note says "certificate not trusted" rather than "no
  response". Node servers still need the self-signed root, which the report
  says.

- **`--concurrency` now registers what a run one address at a time registers.**
  With the flag, a thirteen-address run finished with a few servers in
  `.mcp.json`; without it, all of them. One pipeline object serves every
  thread, and state that belonged to one address was shared. Fixed in both
  pipelines, keyed by what is shared rather than by server, so any address is
  covered:
  - The agent workspace is prepared once, under the pipeline's lock, and the
    skills tree is replaced by copy-then-rename, so no reader sees half of it.
    Seven endpoint stages had been lost to `FileExistsError`/`shutil.Error`
    from threads re-copying it underneath each other.
  - A stage whose agent machinery raises is now done by the deterministic
    stage (`findings["agent_crashed"]` says why) instead of being recorded
    FAILED, which had left the endpoint record empty and every lower tier
    refused with "the endpoint stage has not run". It is FAILED only when the
    deterministic stage raises too.
  - The runtime and retry model are per address (per thread). One address's
    refusal no longer changes the model of requests other addresses already
    have in flight, and an address never rotates back to a model that refused
    it. A new address starts where the last rotation landed, as before; a
    runtime that threw is still left by every address.
  - A retry model that the deployment does not serve is struck off for the run
    instead of latching the whole runtime as unusable; an unusable runtime
    learned by one address is honoured by the others at their next stage.
  - Each address writes its artefacts to its own directory
    (`agent-workspace/artifacts/<address>`), and the four skills say so.
  - The negative-control probe runs once per origin, with concurrent callers
    waiting for its answer instead of reading an empty one. Identical
    prerequisite installs are serialised (`mcp_auto_discovery/locks.py`), and
    the traditional pipeline's entry doctor is built under a lock.
  - `--concurrency 0` (or a negative or non-numeric value) is refused on the
    command line instead of being silently ignored. The bare form's count is
    no longer written into the parser: it is
    `mcp.discovery.target_concurrency_bare`
    (`INITIALIZE_CLAUDE_AI_MCP_CONCURRENCY_BARE`, 4 as shipped).
  Without the flag, nothing changes: one thread takes every guard uncontended.

- **An endpoint that is not the target's no longer shuts tiers 2–4.** The
  registry's name search for `confluence` returned "0DTE Confluence Pro"
  (`mcp.0dteconfluence.com`), which handshaked, was recorded as
  `confluence.example.com`'s native endpoint, blocked the package, bridge and
  strategy stages, and was then refused by the writer. Fixed in both pipelines:
  - The checklist records an endpoint only if it is registrable for the
    target; otherwise it is ruled out with the reason
    (`mcp.policy.native_must_be_registrable`). `DueDiligence` gained
    `ruled_out`, `note()` and `retract()`; a merge never re-adds a ruled-out
    URL, and candidates dropped as unregistrable or as not starting are
    retracted.
  - After every stage the checklist is vetted before the next gate: believed
    but unbacked endpoints are probed and retracted if they fail
    (`mcp.policy.vet_record_before_gate`).
  - Stages skipped because of a later-retracted endpoint are run after all
    (`mcp.policy.reopen_when_native_retracted`).
  - Reporting: `reconcile` absorbs only registrable candidates and says
    "recovered" only when it did; the due-diligence summary lists ruled-out
    endpoints; the remediation no longer claims the endpoint "is what was
    registered". The agent's retry correction names ruled-out endpoints.
  - A generic sole address variable filled by the program (`BASE_URL`) no
    longer counts as grounding evidence
    (`mcp.packages.address_fill.sole_fill_grounds = false`); product-named
    variables and handshake evidence still ground.
  - Skills (endpoint, package, strategy, bridge) say a name match or search hit
    is not evidence.
- **`confluence.example.com` no longer finishes with nothing in `.mcp.json`.**
  Five causes, each fixed in both the agentic and the traditional pipeline:
  - A server that lists no tools, or exits without saying why, when its
    credential is withheld was filed as a failure. This is what happened to
    `mcp-atlassian` and `@atlassian-dc-mcp/confluence`. Such a server is now
    started once more with a stand-in value
    (`mcp.packages.credential_probe`). If it then lists tools, it is
    registered as an endpoint waiting for that variable. If nothing changes,
    the verdict says the variable is not what it is missing. The stand-in is
    redacted everywhere and never written.
  - The official registry was asked phrases such as "confluence mcp server".
    Its `search=` matches server names, so every query returned nothing. It
    is now asked the bare product word (`registry_query_templates`), with its
    own timeout (`registry_timeout_seconds`,
    `INITIALIZE_CLAUDE_AI_MCP_REGISTRY_TIMEOUT`). A source that times out or
    fails is recorded under `search_problems` in the stage's findings.
  - `verify_all` stopped at the first candidate that started, even when the
    registration gate would refuse it. It now continues to one the gate
    accepts. A credential-walled candidate the gate would refuse no longer
    settles the stage either.
  - A registry entry's declared address variable (`CONFLUENCE_HOST` and
    similar) was left empty. It is now filled with the target's host or base
    URL (`mcp.packages.address_fill`).
  - The agent proposed the same launches for over an hour, and each one was
    started again. A target now remembers its launch verdicts
    (`mcp.verification.reuse_verdicts`), and the agent is told when an answer
    was reused and what the credential test showed. A rejected trial's error
    is quoted in the log on one redacted line (`failure_excerpt_chars`).
- **A sign-in without `--headless` no longer waits for a browser that never
  comes back.** With `--force-auth` or `--force-auth-with-reset` on a host with
  a display, the browser opened, sign-in succeeded, and nothing reached the
  terminal: there was no code to paste either.
  - Cause: `auth.browser_mode=auto` chose the loopback redirect wherever a
    display was detected. A display proves only that a browser can be drawn.
    Whether the page's last step reaches `http://localhost:<port>` on this
    machine can't be checked beforehand: a different machine, an
    organisation's sign-in page or browser policy, a port. When it fails, the
    sign-in waits out `auth.interactive_timeout_seconds`.
  - `auto` now takes a pasted code on every backend, display or not, and the
    URL is opened in a browser for you unless `--headless`. The redirect is
    only used when asked for (`--browser-loopback-redirect`,
    `auth.browser_mode=browser`). An unrecognised `browser_mode` is read as
    `auto` instead of falling back to detection.
  - `auth.signin.open_url` gets a new default, `auto`: open the URL unless
    `--headless`. `always` also opens it under `--headless`; `never` opens
    nothing.
  - The window opened now shows the same URL as the file, the clipboard and
    the screen, sign-out first included. Where the program keeps no browser
    profile of its own, the browser found for the run is used.
  - claude.ai runs `/login`, which opens the browser itself and takes a pasted
    code, without `--headless`, and `setup-token` under it
    (`auth.claude_oauth.interactive_command_opens_browser`).
  - The phase 2 banner, the sign-in status lines and `--help` describe the
    flow that will actually run.
- **The sign-out no longer hands a single sign-on account straight back.**
  After the previous fix, `--force-auth-with-reset --headless` signed the
  browser out of Google (the log showed the URL going through
  `accounts.google.com/Logout`), but the operator landed directly on their
  organisation's sign-in page, still signed in there. A sign-out reaches only
  its own site. Google still remembered the account in that browser and carried
  on with it, and the organisation's page kept its own session.
  - The sign-in behind the sign-out now asks for Google's account chooser:
    `auth.<backend>.reset_prompt` (`select_account` for Google, space-merged
    into any `prompt` already there, everything else in the URL unchanged).
  - New `auth.signin.idp_signout_url`: the organisation's sign-out page, with
    `{continue}`, made the first stop of the URL. No default. Without
    `{continue}` it is ignored, with a warning.
  - The advice before the sign-in now leads with a private window (with the
    command for the browser found here), because it is the one thing that
    clears every site at once. It also says what the sign-out cannot reach.
  - New env overrides: `INITIALIZE_CLAUDE_AI_GCP_RESET_PROMPT`,
    `INITIALIZE_CLAUDE_AI_CLAUDE_RESET_PROMPT`,
    `INITIALIZE_CLAUDE_AI_AUTH_SIGNIN_IDP_SIGNOUT_URL`.
- **The sign-out-first URL is now also the one shown on screen.** After the
  previous fix, `--force-auth-with-reset --headless` still let the operator
  straight back in. The sign-out-first URL went only to `sign-in-url.txt` and
  to the clipboard of the display the program found (`:1`), while gcloud's own
  URL was still printed on screen. From a terminal, that on-screen URL is the
  copy people use: they select it, Ctrl+click it, or copy it over ssh, where
  the `:1` clipboard is not even theirs. So the sign-out never ran.
  - `Runner.run_tty` accepts a `screen` rewriter (the `DisplayRewriter`
    protocol) that changes only what is painted. The relay still reads the
    child's own output, and nothing typed is touched.
  - The new `UrlOnScreen` paints each sign-in URL in the form it was handed
    over. It holds back a URL that is still arriving and releases held text
    when the child goes quiet or ends, so nothing is shown half-rewritten or
    swallowed.
  - The pty proxy now decodes UTF-8 incrementally, so a character split across
    two reads is no longer mangled.
  - The debug log records when a URL was handed over through the sign-out
    page.
  - The clipboard is chosen by the session, not just by what is installed. The
    log showed `wl-copy` announced, failing (no Wayland session), and `xclip`
    taking the URL instead. `[auth.signin.clipboard_needs]` names the variable
    each tool needs (`WAYLAND_DISPLAY`, `DISPLAY`); a tool without it is
    skipped, and the announcement names the display whose clipboard holds the
    URL. Installed-but-unreachable is no longer reported as "not installed".

- **`--force-auth-with-reset --headless` now asks for your email and password
  again.** After the reset the operator pasted the URL into their own browser
  and was signed straight back in. The reset had worked: there was nothing left
  to revoke on the machine. The provider recognised the browser's own session,
  which a local reset cannot reach, and no sign-in parameter refuses it (Google
  documents only `none`, `consent` and `select_account`). The advice to use a
  private window or sign out was printed, but it was easy to miss among the
  other bullets.
  - After a reset, the URL delivered to `sign-in-url.txt` and the clipboard now
    goes through the provider's sign-out page, which continues to the sign-in.
    Opening it in any browser, on any machine, ends that browser's session
    first.
  - New settings: `auth.signin.sign_out_first` (`reset` | `always` | `never`,
    `INITIALIZE_CLAUDE_AI_AUTH_SIGNIN_SIGN_OUT_FIRST`) and
    `auth.<backend>.signout_continue_param` (`continue` for Google; empty for
    claude.ai, which has no sign-out page).
  - Only the first sign-in URL of a run is chained, and only a URL on the
    sign-out page's own host.
  - The advice before the sign-in and the reset confirmation both say the
    browser will be signed out (every Google account in it), and that the
    on-screen URL skips the sign-out.
  - `AuthBackend.url_relay()` builds the relay in one place, so the advice and
    the delivered URL cannot disagree.

- **`--headless` no longer opens a browser, not even after
  `--force-auth-with-reset`.** Once the operator confirmed the reset, Firefox
  opened on the sign-in URL. `--headless` means the operator copies the URL and
  opens it themselves.
  - Cause: `auth.signin.open_url` defaulted to `reset`, which opened the
    captured URL in the emptied profile after a reset when a display was
    available. A pasted-code flow on a host with a display exists only under an
    explicit `--headless`. So the default fired exactly for the operator who
    had asked for no browser.
  - `open_url` now defaults to `never`. `always` is the only opt-in, and any
    other value, `reset` included, is read as `never`. (Superseded: the default
    is now `auto`, which opens nothing under `--headless`; see the first
    entry above.)
  - The `BROWSER` phase 2 exports for the redirect flow was also inherited by
    the pasted-code sign-in command. It is now overlaid by the new
    `[auth.signin.paste_env]`: by default `BROWSER` is set to a command that
    accepts the URL and does nothing
    (`INITIALIZE_CLAUDE_AI_AUTH_SIGNIN_PASTE_BROWSER`). An empty value removes
    a name, which `Runner.build_env` supports through `None`.
  - After a reset, the advice now also gives the command that opens the URL in
    the emptied profile, for the operator to run.

- **Phase 4 could hang indefinitely after "package hit(s) mention no product
  token".** jira.example.com's package stage fell back to trialling packages, and
  the log never moved again.
  - The cause was `_StdioSession.close`. It closed the server's stdout while
    its reader thread was blocked on it. That close waits for the read, and the
    read ends only when everything holding the pipe exits.
  - So a server that ignored stdin closing, or a launcher's grandchild holding
    the pipe, blocked forever. The terminate and kill after the close were
    never reached. No timeout covered it: the trial timeout bounds requests, the
    stage budget is checked between candidates, and the agent timeout covers
    agent runs only.
  - Stdio servers now start in their own process group. Ending a session closes
    stdin, waits `mcp.verification.stdio_shutdown_grace_seconds` (2), then sends
    the group the run's stop ladder (`run.stop_signals` /
    `run.stop_grace_seconds`). Pipes are closed only after their readers finish.
    A pipe held outside the group is abandoned with a warning, not waited on.
  - `mcp.verification.stdio_session_timeout_seconds` (240) caps one whole
    session.
  - Every trial is logged before it starts.
  - The same code serves agent and traditional mode and the check that each
    finished entry starts. The bridge and package skills now tell agents to
    bound their own hand launches and to make bridges exit at stdin EOF.
  - `Runner.stop_ladder()` is now public, so both kinds of child share one stop
    policy.

- **openqa-example-net was missing from `.mcp.json` on a host with an externally
  managed Python.** Phase 1 had installed into its own venv
  (`install.venv_path`), the program ran from it, and so the generated bridge
  named that venv's interpreter. `portability.assess` treated every virtualenv
  as one built to trial a package, so it refused the entry. It did so only in
  the writer, after the bridge stage had settled and closed the strategy stage,
  so nothing else was tried. Two changes fix it:
  - A venv at or under `mcp.registration.durable_venv_roots` is now host-bound.
    The shipped roots are phase 1's venv, pipx's, `uv tool`'s and
    `INITIALIZE_CLAUDE_AI_MCP_DURABLE_VENV_ROOT`. The venv the program runs from
    counts too; `mcp.registration.trust_running_venv` switches that off. The
    trial sandbox still wins.
  - The portability gate now runs before a stage settles, alongside grounding,
    in both traditional mode (`TraditionalPipeline.registrable`) and agent mode
    (`_drop_ungrounded` and `discard_unregistrable`). An entry the writer would
    refuse can no longer close the tiers beneath it.

- **`initialize-claude-ai --help` lists every option, and each one appears
  under the phase that reads it.** argparse prints only the parser it was asked
  about, so the program's help showed the shared options and the command
  names, and none of `run`'s. Top-level `--help` now appends every command's own
  groups, read off the parsers, and keeps the examples at the end. In
  `run --help`, `--agent-max-turns` and `--agent-max-attempts` appeared under
  the generic "options" heading, because `SETTING_FLAGS` rows were added to the
  parser rather than to a group. Rows are now filed by the key they write
  (`mcp.*` goes to phase 4, `run.*` to the new `run control` group), and phase
  groups are generated from the phase registry. `--phases`, `--dry-run`,
  `--auto-agree`, `--ignore-failure`, `--no-partial-success` and `--timeout`
  moved to `run control`. The README flag table now lists the two agent flags
  under phase 4.

- **`--help` is easier to read.**
  - Each phase heading names the phase and what `--phases` accepts, e.g.
    `phase 4 (mcp) - MCP auto-discovery`.
  - Each phase section opens with what the phase does.
  - Each command's section is headed with its one-line summary.
  - Help entries that ran to eight to ten lines are down to three or fewer;
    the reasoning stays in the README.
  - Credential flags read "…, read from REF" and "…, given as the value
    itself".
  - Mutually exclusive pairs say so.
  - `--no-agent` names the flags it disables, taken from `SETTING_FLAGS`.
  - New examples show `--force-auth-with-reset`, `--concurrency` and
    `--agent-max-attempts`.
  - Tests cap the length of each help entry.

- **`--force-auth-with-reset --headless` now leads to a sign-in that asks who
  you are.** The reset revoked both gcloud credentials and emptied the
  program's own browser profile. The pasted-code flow opens no browser, though,
  so the profile went unused. The URL was pasted into an everyday Firefox that
  still held the Google session, and Google skipped the email and password.
  After a reset, the captured URL is now also opened in the emptied profile
  when a display is available (`auth.signin.open_url`,
  `INITIALIZE_CLAUDE_AI_AUTH_SIGNIN_OPEN_URL`; superseded by the sign-out-first
  entry above: it now defaults to `auto`, and the URL itself signs the browser
  out first). It opens once, only
  when the URL is complete, and only in a profile of the program's own.
  Otherwise the sign-in says the reset did not sign the browser out. It also
  offers a private-window command from the new `[auth.browser.private_args]`
  and the backend's `signout_url`. New `Runner.launch` starts a detached child
  and does not wait for it.

- **Every variable a server reads is now in its `env` block, with a portable
  value.** `openqa-example-net` stayed disconnected after being copied to another
  machine. Its bridge read `MCP_BRIDGE_MANIFEST`, `_BASE_URL`, `_TIMEOUT`,
  `_CA_BUNDLE`, `_API_KEY` and `_API_SECRET`; the entry named only the base URL,
  because the writer only knew the variables a candidate *declared*. The
  manifest was found by a fallback beside the script, which on the other
  machine opened an older, unstamped manifest and exited on its schema. New
  `mcp_auto_discovery/env_contract.py` scans each launched text file for the
  variables it reads (`[mcp.env_contract]`: one regex per language, an ignore
  list for host variables, and fallback-file detection), and the writer names
  each one with the value the script already used -- a literal default, the
  file its fallback opens (made relocatable like any other path), or
  `${NAME:-}` -- under a server-namespaced export. Applies to both modes; the
  briefing lists every setting and the variable that changes it.
- **Credentials a server was proven to start without are written as
  `${NAME:-}` rather than left out**, so the entry names them while the launch
  stays the one that was proven. A bare `${NAME}` is still used for a
  credential the server needs -- Claude Code passes an unset one through
  literally, which is verified behaviour, not an assumption.
- **The generated bridge names its manifest and CA bundle in `env`.** It
  relied on its beside-the-script lookup to keep state-directory paths out of
  the entry; those paths are now rewritten through configured roots, so naming
  them costs no portability.
- **New repair `withdraw_optional_reads`** (`[mcp.healing] repairs`), for the
  rare server that treats an empty variable differently from an unset one: it
  removes exactly the `${NAME:-}` slots and proves the entry again.
- **The bridge-authoring skill told agents *not* to name companion files in
  `env`.** It now requires every variable the script reads -- files included --
  in `env`/`secret_env`, and the package, strategy and shared agent
  instructions say the same for code the scanner cannot read.

- **A server that refuses to *start* without its credential is no longer
  discarded as broken.** `gitlab.example.net` finished a run with no entry at all,
  and the reason was a rule this project had added itself. A trial launched
  without an unresolvable secret that then *crashes* was recorded as a plain
  failure — correct for `@ivotoby/openapi-mcp-server`, which died inside
  `ToolsManager.initialize` for reasons having nothing to do with a credential,
  and wrong for a server that reads its configuration, finds the variable
  absent and declines. Both exit non-zero, so "did it answer?" cannot separate
  them, and the bridge stage had already built a launcher for GitLab, pinned
  `GITLAB_URL` at the target, pointed TLS at a self-contained CA bundle and
  verified it end-to-end. Nothing downstream could recover a good server from
  the verdict "it failed".

  The two cases are now separated by the standard applied everywhere else here
  — evidence from the server, the same requirement that stops an intranet's
  blanket `401` from counting as an endpoint. Either the failure text **names a
  variable that was withheld**, which needs no configuration because the names
  come from the candidate, or it matches
  `mcp.packages.startup_credential.markers`. Markers are the weaker rule and
  are deliberately narrow: one that also appears in ordinary breakage would
  turn every crash back into a credential wall. Such a server is registered
  provisionally with the slot empty, exactly like one that answered `401`; it
  is never counted as working, `connected` stays `no` because it never
  handshaked, and the `${VAR}` slot is kept rather than dropped — the server
  has just said it needs it. `enabled = false` restores the old behaviour.
  Applies to both the agentic and the deterministic pipelines, which share the
  verifier.

- **A trialled server's stderr is kept from the end, and its failure leads with
  the exception.** Two bugs stacked, and between them they destroyed exactly
  the evidence the rule above needs. The stderr buffer was capped at 40 lines
  by refusing to append past it, which keeps the *head* — and a traceback puts
  the only line that names a cause at the very end, so a startup chain longer
  than forty lines had its exception thrown away before anything could read it.
  What remained was then summarised as the last three lines joined
  oldest-first, which takes the tail of the head: a frame from the middle. The
  GitLab bridge really died on `ValueError: GitLab token is required. Set one
  of: GITLAB_TOKEN, …` — a message naming the withheld variable outright — and
  reached the log as `user_lifespan_result = await
  stack.enter_async_context(self._…`.

  The buffer is now a bounded deque keeping the last 200 lines, and the failure
  text promotes the exception line to the front so that shortening it for a
  console cannot lose it. Neither was cosmetic: the credential-wall rule reads
  this string, so the summariser was silently setting a ceiling on what that
  rule could ever conclude.

- **An install step that is prose is dropped where steps are parsed.** A
  sentence survives `shlex.split` as a well-formed argv whose word zero is an
  article, so `build.example.net` reported `prerequisite needs the, which is not
  installed` and lost the candidate to it. The evidence is sentence punctuation
  — a command does not end a word with a comma or a full stop — rather than a
  list of English words, which would only ever be right about one language.
  `pip install .`, `python3.11` and `example.com` are unaffected. The
  "not installed" message also now quotes the whole step, not just word zero.

Three servers that `claude mcp list` reported as `failed` after a clean run --
`bugzilla-opensuse-org`, `build-example-net` and `confluence-example-com` -- each
having passed every check this program had.

- **The entry that is written is now the entry that was tested.** A stdio
  candidate is trialled with its unresolvable secrets left out; the entry was
  then written *with* them, as `${NAME}` placeholders, so the launch that
  shipped was one nobody had run. An unset variable is not a no-op:
  `mcp-bugzilla` and `@ivotoby/openapi-mcp-server` both exited during start-up,
  which is what `CONNECTION_CLOSED` means. A variable the proven launch did not
  carry is now left out of `env` and named in the briefing instead; exporting it
  and re-running phase 4 trials the server *with* it and writes the slot it
  proved (`Candidate.proven_without`).
- **Every finished entry is launched exactly as written, before the run claims
  it works.** The last gate, and the only one that tests the artefact rather
  than the candidate it came from. A failure is reported with its reason
  (`mcp.registration.verify_as_written`). Whether it also withholds the entry
  is `mcp.registration.require_entry_starts`, on by default since entries that
  could not start shipped anyway; the same check now runs in each stage, so a
  refusal reopens the lower tiers or hands over to the next proven candidate.
- **A variable that names a choice is no longer filed as a credential.**
  `BUGZILLA_AUTH_MODE` selects `apikey`/`basic`/`anonymous`. `auth` is a whole
  word in it, so the word-boundary rule that rescued
  `NODE_TLS_REJECT_UNAUTHORIZED` did not help, and no value was ever seen for it
  -- only a name. It became `"${BUGZILLA_AUTH_MODE}"` in the entry and the
  operator was told to export it "with your real token"
  (`secret_policy.names_a_selector`). Both pipelines enforce it: the agentic one
  was matching `SENSITIVE_NAME` by hand in two places rather than calling the
  shared predicate, so the guard would have reached only `--no-agent` runs — and
  the variable reached `secret_env` directly, where nothing classified it at
  all. A structural test now fails if any module outside `secret_policy` matches
  the pattern instead of asking the rule.
- **A credential's shape is per variable, and an unknown shape says so.** One
  sentence described every variable a server needed: "may call it an API token".
  `API_HEADERS` wants comma-separated `Name:value` pairs, so the operator
  exported a bare token into it, exactly as instructed, and the parser rejected
  it. Guidance is now rendered per variable, from what discovery found, then the
  variable's own name (`[mcp.discovery.auth.name_shapes]`), then the product
  profile -- and where none of them speaks, the placeholder admits it instead of
  implying "token".
- **A claim may no longer corroborate itself.** `endpoint_hosts` collects the
  tier-1 checklist *and the candidates*, so a URL candidate was normally in its
  own `served_by`: "X is served from X, which this run found answering". That
  circle registered `https://mcp.atlassian.com/v2/mcp` -- Atlassian Cloud, which
  serves `*.atlassian.net` tenants -- for a self-hosted Confluence Data Center.
  Provenance now has to say *whose* it is, too: "Atlassian's documentation"
  naming an Atlassian address is not the target speaking, while GitHub's
  documentation naming `api.githubcopilot.com` for `github.com` is.
- **An unproven endpoint on another organisation's domain no longer closes the
  lower tiers.** That single Atlassian URL cancelled tiers 2, 3 and 4, so
  `mcp-atlassian` -- which does talk to Data Center, given `CONFLUENCE_URL` and
  a PAT -- was never looked for. The one target with a real tier-2 answer got
  the only wrong tier-1. A *proven* endpoint still closes them wherever it lives
  (`mcp.policy.lower_tiers_when_wall_is_foreign`).

- **A generated bridge and its manifest must now prove they are a pair.** The
  two files are produced together and are only valid together, and nothing
  checked it. An openQA bridge was regenerated while its manifest was not; the
  older manifest had `path_params` as a list where the newer script called
  `.items()` on it, so the bridge died inside `tools/list` with
  `AttributeError: 'list' object has no attribute 'items'` — which the operator
  saw as `openqa-example-net · ✘ failed · Connection closed`, naming neither file,
  nor the field, nor the fact that two artefacts had drifted apart.

  The manifest now carries the generation that wrote it
  (`{"schema": 1, "script": "..."}`), a mismatch is refused by name, and the
  operation fields the script indexes are validated up front — reporting the
  operation and field rather than the line that touched them first. An unstamped
  manifest is still accepted, because every bridge already on disk is one.

  And the generated entry point is wrapped: anything that escapes now leaves one
  sentence on stderr naming the cause, because a traceback from a stdio server
  reaches the client as "the process exited" and nothing more. The
  bridge-authoring skill requires the same of agent-written bridges, which is
  where this one came from.
- **An agent runtime that cannot serve any request is diagnosed once, not
  retried everywhere.** `claude-opus-5-5` was not enabled on an operator's
  Vertex project, so every agent call returned `404` carrying its own fix —
  *"Try --model to switch to claude-opus-5"*. It matched no failure class
  (`_TRANSIENT` matches **unavailable**; the text said **not available**), so it
  was read as an ordinary failure: three attempts a stage, four stages a target,
  thirteen targets, each spending a real agent invocation to be told the same
  thing. Every address silently degraded to the deterministic pipeline —
  `developers.google.com` among them, which lost the native endpoint the agentic
  path finds and so had nothing to register — and no `.mcp.json` was written.

  A new `unusable` class covers failures about the *installation* rather than
  the request: model not enabled, credential refused, quota gone. It stops the
  retry loop, latches for the run so no later stage pays for the same answer,
  and is reported once in the final verdict as `THE AGENT NEVER RAN`, with the
  remedy. Extra wordings via `mcp.agent.unusable_markers`, since the phrasing
  belongs to whichever runtime is installed.
- **A failed agent run's message is parsed, not sliced.** The capture took the
  last 600 characters of `claude -p --output-format json` output, which for a
  JSON document begins mid-key — so the report showed
  `ackground":0,"foreground":0,...` twenty-nine times while the field that
  mattered held the sentence naming the fix. The classifier that should have
  caught the 404 was reading the same slice, so the unreadable message and the
  missing class were one bug twice. The message field is extracted first;
  slicing remains the fallback for unstructured text like a stack trace, where
  the tail is the informative part (`mcp.agent.error_chars`).
- **A server that starts and advertises nothing is no longer called a
  credential wall.** Trialling a package without its secret is worth doing
  *because* a stdio server reads its credential when a tool is called, so one
  launched without it still lists everything it can do. The branch recording
  that result tested only `handshake.ok and a secret was missing` — its own hint
  said "handshaked and listed its tools", and nothing checked that it had listed
  any.

  `mcp-atlassian` for `confluence.example.com` handshaked, listed zero tools, and
  became "a real MCP endpoint that refused an anonymous client": provisional,
  settling the target, closing tiers 3 and 4, and shipping an entry naming
  `PERSONAL_TOKEN` — a variable that package does not read. Supplying it changed
  nothing, and the server connected with no tools.

  An empty listing has several causes and a missing token is only one, so it is
  reported as *started, exposed nothing, cause unknown*, which lets `accepts()`
  say "server exposes no tools, resources or prompts" and lets the lower tiers
  run (`mcp.packages.credential_wall_needs_tools`).
- **Claude Code's own verdict is acted on instead of printed.** `claude mcp get
  <name>` is the one check performed by the real client, connecting exactly as
  the next session will. Its result was appended to a list, rendered as a
  column, and read by nothing — so a run whose own table said

  ```
  connected  tools  two-way  usable  starts  claude
  yes        yes    yes      -       yes     no
        - claude: Claude Code reports: failed to connect
  ```

  reported `PARTIAL`, wrote both entries, and left the operator to discover it
  in the next session. A negative now becomes a finding on that registration
  with remediation. The entry is still written — discarding a server the client
  is momentarily unhappy with is how confluence.example.com was lost once — but the
  run stops implying it worked.

  When it disagrees with `starts`, the report says so: an entry that runs when
  this program launches it and not when Claude Code does differs by environment
  and working directory, which is the most useful sentence available and was
  being left for the operator to infer.
- **A generated bridge no longer loads another bridge's manifest.** Resolving it
  as "the first `*.operations.json` beside the script" is right for a bridge that
  owns its directory and wrong as soon as two share one — an openQA bridge
  picked up the other openQA bridge's manifest, a real file for a different
  host, in silence. The manifest named for the script wins; a bare match is
  trusted only when unique; otherwise the bridge exits naming the candidates.
  Introduced by the self-location change in this same release.
- **A server is now asked what it serves, instead of only its entry being
  read.** Every grounding test read the entry — a command line, an environment
  value, a URL — which is what somebody *configured*. Meanwhile the server had
  handshaked and described itself, and all of it was discarded except the bare
  names: `instructions`, tool and prompt descriptions, **resource URIs** (lost
  whenever a resource also had a name), and the defaults in input schemas.

  `build.opensuse.org` is what that cost. Tier 2 returned `@ateam-ai/mcp`, an
  npm keyword-search hit naming no address; it was refused, correctly, and the
  address finished the run with nothing registered. The real Open Build Service
  server would have been refused by the same rule for the same reason, even
  though it lists resource URIs under `https://build.opensuse.org/…`. The entry
  cannot tell those two apart; the handshake can.

  `server_said_so` joins the grounding order above vendor provenance. It matches
  by host through `same_service`, so a lookalike in a description is not the
  target, and it reads only a handshake that completed
  (`[mcp.verification.advertised]` bounds and redacts what is kept).
- **A stage that falls back can no longer settle on a candidate registration
  will refuse.** The deterministic loop had guarded against this since the last
  release — its comment names `@ateam-ai/mcp` — but a stage that fell back calls
  `fallback.stage_*` directly, so its candidates met neither that guard nor the
  agentic one. One irrelevant package therefore closed the bridge and strategy
  stages beneath it and was then thrown away at publish, which is precisely the
  failure the guard was written for.
- **Every registered stdio server is told what this host trusts, not just the
  bridges this program writes itself.** `build.example.net` is signed by
  `Example CA 2023.1`: the host trusts it (`curl` fetches its OpenAPI spec with
  `HTTP 200`) but Node ships its own certificate store and does not, so the
  registered server could not load the spec, exited during start-up, and Claude
  Code reported `failed · Connection closed` — a message mentioning no
  certificate, which sent the diagnosis to `API_HEADERS`, a variable that turned
  out never to have been involved.

  The argument for naming the host's bundle already existed in
  `BridgeBuilder.ca_bundle` and was reachable by one caller. Detection moves to
  a shared `trust` module, and every stdio entry now carries the variable its
  runtime reads — `NODE_EXTRA_CA_CERTS`, `SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE`,
  keyed on the launcher and configurable in `[mcp.trust.runtimes]`, recognised
  through the `${VAR:-/path}` rewriting. Additive: it can add authorities and
  never remove one.

  Three behaviours were measured, not assumed: the bundle makes the fetch
  succeed; an empty value is inert; an unexpanded `${NODE_EXTRA_CA_CERTS}` costs
  one stderr warning and the server keeps running. The last is why the lever is
  written even where no bundle was found — and whatever is written is then
  launched exactly as written by the `starts` gate.

  This also completes an older rule rather than adding a new one: entries that
  switch verification off have long been stripped, on the grounds that "the
  remedy for a private CA is the trust store, not a flag". The trust store is
  now actually supplied.
- **Every absolute path in an entry is now relocatable, not just the kinds
  somebody remembered.** This rule had been fixed three times and each fix named
  one more kind of path: a configured root for the bridge bundle,
  `ICA_MCP_PYTHON` for the interpreter, and now `ICA_MCP_{LAUNCHER}` for `uvx`,
  `npx`, `node` and `docker` — the last being the first failure ever reported
  against this program (`/root/.local/bin/uvx`, `ENOENT` on the next machine,
  surfacing as `failed · Connection closed`) and still live in a real file.

  Each fix was correct and each was followed by a report of a path it did not
  cover, most tellingly a certificate bundle in a third-party package's
  `OPENQA_VERIFY` — under no root, matching no binary. Enumerating kinds of path
  is a losing game, so the default is now *every* absolute path, named after the
  configured root it sits under, the binary it is, the environment variable
  holding it, or its own basename
  (`mcp.registration.name_every_absolute_path`, `…launcher_variable_template`,
  `…path_variable_prefix`).

  Safe to apply broadly because an unset `${VAR:-default}` yields exactly the
  original string — nothing changes where it was written. The briefing now lists
  the variables by reading them back out of the entries rather than recomputing
  which roots might apply, so one named after an env key is not silently
  omitted from the instructions.
- **The verification table put marks under the wrong headings.** Rows were laid
  out positionally, but `audit` returns a different number of them depending on
  what happened — a server that handshaked without its credential gets an extra
  `usable` row, one that was never probed gets a single `connected` row. So for
  exactly the servers this release is about, every mark from `usable` onward sat
  one column right, and the column saying whether the thing worked was the one
  that slid. The table is now built by check name against `CHECKS`, with `-` for
  a check a given server has no row for.

## The first snapshot (2026-09-16)

First public release.

### Added

- **Phase 1 — install.** Verifies, installs and re-verifies the Claude Code
  CLI, the Python and Node SDKs, the agent SDKs, the desktop app, the Google
  Cloud CLI and a browser, across `apt`, `dnf`, `yum`, `zypper`, `pacman`,
  `apk`, `flatpak`, `snap` and Homebrew. Package-manager quirks are data, not
  code.
- **Phase 2 — authenticate.** Google Cloud, claude.ai and the Anthropic API,
  each as inspect → acquire → verify, with headless flows for machines with no
  browser and a live round trip proving every credential.
- **Phase 3 — provision.** Resolves backend, account, project, region and
  models — asking the backend which models it will actually serve rather than
  assuming — then writes `settings.json` and the environment files and prints
  the inventory.
- **Phase 4 — MCP auto-discovery.** Four Claude subagents (endpoint, package,
  bridge, strategy) under an enforced tier order, with every candidate proved
  by a real MCP `initialize` handshake before it is registered.
- Credential-walled endpoints (`401`/`403`) are recognised as findings and
  registered with the credential slot left empty, rather than discarded.
- Layered configuration with every value overridable from the environment, and
  secrets handled only as references.
- Every line of output carries the date and time, on screen and in the log file,
  so the transcript can be read as one timeline. Box frames, table rows and the
  `--json` report are deliberately left unstamped (`console.timestamps`).
- `--concurrency [N]` works several addresses at once (bare means 4, absent
  means one at a time, as before). Phase 4 is ~98% agent thinking and addresses
  are independent, so this is the one lever on a long run; it reduces waiting,
  not work. Output stays grouped by server, and at a concurrency of 1 no thread
  is started -- the sequential path is the same code with the strategy set to
  "inline", not a second implementation (`mcp.discovery.target_concurrency`).
- 1564 offline, hermetic tests, including a regression corpus of the real
  hosts that broke it (`tests/test_mcp_regression_corpus.py`).

### Fixed

Eleven defects found while verifying the tool against real infrastructure, all of
the same shape -- the recorded claim outran the evidence. See
[CONTRIBUTING.md](../CONTRIBUTING.md#the-mistake-this-codebase-keeps-making).

- A generated bridge was registered as `["python3", ...]` and relaunched later
  under a different interpreter with a different certificate store. The
  interpreter is now resolved to an absolute path at generation time, and the
  trust store is named explicitly (`mcp.bridge.generated.interpreter`,
  `ca_bundle`).
- A `401` from a host that refuses *every* URL -- a Jira, a Confluence, a
  service desk -- was registered as a credential-walled MCP endpoint. A refusal
  now has to carry something protocol-shaped, and a control probe of a path that
  cannot exist catches hosts that refuse everything identically
  (`mcp.discovery.auth.require_mcp_evidence`, `control_probe`).
- The scheme from `WWW-Authenticate` was copied into the entry verbatim, so
  Jira's `OAuth` (1.0a) was sent where a `Bearer` token was needed
  (`mcp.discovery.auth.header_schemes`).
- An endpoint that answered `401` was re-searched twice more before the stage
  gave up, at five to eight minutes an attempt
  (`mcp.agent.credential_wall_settles_stage`).
- A target was searched for by the **wrong word**. The product token was taken
  from the last label of the hostname, so `confluence.example.com` and
  `jira.example.com` were both asked about as `"example"` -- meaning
  `confluence-datacenter-mcp` and `jira-datacenter-mcp` could never be found --
  and `smelt.example.net`, with `.de` missing from the generic-label list, was
  searched for as the single word `"de"`. The product is now the leftmost
  meaningful label, the organisation is kept as a second query, and the label
  list is configuration (`mcp.packages.generic_host_labels`).
- A package had to have **something to do with the target**. A keyword match
  with no judgement behind it registered `@kubb/mcp`, a TypeScript code
  generator, as the MCP server for Confluence, Bugzilla and build.opensuse.org,
  and a Colombian compliance server for SMELT. Two ways to be relevant are
  accepted -- naming the product, or being a generic server configured for this
  host, which is what keeps `mcp-graphql` pointed at SMELT
  (`mcp.packages.require_relevance`).
- A package whose credential was unavailable was **not launched at all**, so it
  lost to any irrelevant package that needed none. It is now trialled without
  the credential -- `confluence-datacenter-mcp` handshakes and lists 115 tools
  with no token -- and registered provisionally with the slot empty, never as
  working (`mcp.packages.trial_without_secrets`).
- A **transient** failure -- a rate limit, a service briefly unavailable --
  consumed one of the agent's three attempts, although it said nothing about
  the stage. It is now retried without spending one
  (`mcp.agent.transient_free_retries`); otherwise running addresses
  concurrently would provoke rate limits and buy the deterministic fallback a
  stage the agent never got to work.
- An entry could name a path that **does not survive the machine that made
  it**. A bridge was registered as
  `…/work/agent-workspace/artifacts/openqa-mcp-bridge/venv/bin/python3` -- a
  virtualenv built to trial a package -- which handshaked when verified and was
  `ENOENT` on the next host. `.mcp.json` exists to be copied, so every entry is
  now graded: a launcher is portable and preferred in the ranking, a durable
  file is host-bound and reported as such in the briefing, and a path inside a
  virtualenv, inside the trial sandbox, or simply absent is refused with the
  launcher form to use instead (`mcp.registration.require_paths_exist`,
  `mcp.ranking.penalties.host_bound`).
- The credential variable was named after **one part of a multi-part value**.
  openQA's is `username:key:secret`; the agent named the variable
  `..._API_KEY`, an operator set just the key, and the server answered `403`
  with nothing to explain it. A variable is now named after the shape of its
  value -- `..._USERNAME_KEY_SECRET` -- and the shape is reported by the agent
  at discovery time, so it works for products with no table entry
  (`mcp.discovery.auth.shape_separators`).
- The credential slot was always named `..._TOKEN`, including for products that
  issue no token -- openQA wants `username:key:secret`. Products whose
  credential has a different shape are now described in
  `[mcp.discovery.auth.profiles]`, and an unknown product is admitted as
  unknown rather than guessed at.
