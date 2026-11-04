# Phase 1 — install

Components are declared as data. Each one carries its package names per manager,
a verification command and the environment variables to record:

```
✔ [   OK   ] claude-cli                /root/.local/bin/claude (2.1.268)
» [RUNNING ] gcloud                      installing Google Cloud CLI
✔ [   OK   ] gcloud                    /usr/bin/gcloud (579.0.0)
```

A component already present is verified and reported in one line. One that has
to be installed prints the `[RUNNING]` line first, because what follows is a
package manager holding the terminal for as long as the mirror takes.

## What the system has, and what an installer said

- **Package names come from the system.** A catalog name is one release's
  (`npm22`). When the package manager says it has no such package
  (`<mgr>.missing_markers`), the installed package providing the command
  (`<mgr>.provider`) or the newest package matching the component's
  `package_patterns` (`<mgr>.available`) is installed instead, once.
- **A failure is reported in the installer's own words.** That means its last
  lines, without progress bars, and "timed out after 600s while downloading
  (69.6%)" for a timeout. The advice to rerun it by hand stays advice.
- **A step that fails fails the strategy.** That includes a post-install step
  (nvm's `nvm install --lts`). Shell code (`post_shell`, `reinstall_shell`)
  gets only this program's `{token}`s; every `$VAR` is left to the shell.
- **A downloaded installer that is a web page is never run**
  (`install.script_web_page_markers`). A strategy may set its own
  `timeout_seconds`.

## What a step costs in disk, and a full disk

- **Space is checked before each step** (`install.check_space`).
  - A reinstall needs the folder holding the copy times its strategy's
    `space_factor`, or the installed size of its owning packages
    (`[install.commands] <mgr>.installed_size`), plus `install.space_margin_mb`.
  - One that doesn't fit is not run. It is reported OK with "kept --
    reinstalling it in place needs ~X MB, Y MB free".
  - `gcloud components reinstall` stages a whole copy of the SDK, declares
    `space_factor = 2`, and once filled a disk with just over 1 GB free.
  - A fresh install with less than the margin free is not attempted.
- **A failed reinstall is cleaned up after.** New entries it left next to the
  folder it owns, named after that folder (`google-cloud-sdk.staging`), are
  removed (`install.clean_failed_step_leftovers`). Nothing that was there
  before is touched.
- **A full disk is blamed only on the step whose own command hit it.** It
  stops the phase at once (`install.stop_on_full_disk`): the remaining
  components are "not attempted: the disk is full", and one verdict names it.
  Phase 4's on-demand installs follow the same rules.
- **Each owning package is reinstalled once per run.** A later component it
  owns is reported "reinstalled this run with <package>".

## Python packages and PEP 668

Two components — `claude-sdk-python` and `claude-agent-sdk-python` — are Python
packages, and most current distributions mark their system interpreter
*externally managed* under PEP 668. openSUSE, Debian, Fedora, Arch and
Homebrew's Python all do: `pip install` and `uv pip install` both refuse to
write there, and `--user` is no way out, because the marker blocks that too.

So `install.python_target=auto`, the default, asks the interpreter rather than
guessing, and builds a virtualenv when it finds the marker:

```
✔ [   OK   ] python-target   /usr/bin/python3 is externally managed (PEP 668); using a virtualenv instead
↻ [CHANGED ] python-venv     created /root/.local/state/initialize-claude-ai/venv
↻ [CHANGED ] claude-sdk-python   anthropic 1.5.0 in …/venv/bin/python
```

The virtualenv is built from the interpreter running `initialize-claude-ai`, not
from whichever `python3` is first on `PATH`, and phase 4 adds it to its own
`sys.path` before importing `claude_agent_sdk`. Both details matter for the same
reason: the SDK is imported *in process* by the agentic phase, and a wheel with a
compiled extension does not load across Python minor versions. A venv the agent
phase cannot import would be an install that only looked like it worked.

| `install.python_target` | Where packages go |
|------|--------|
| `auto` *(default)* | System interpreter, or a virtualenv if it is externally managed. |
| `system` | The interpreter itself — `--user` unless already inside a virtualenv. Needs `--break-dependencies` on a PEP 668 host. |
| `venv` | Always `install.venv_path` (default `$INITIALIZE_CLAUDE_AI_STATE_DIR/venv`). |
| `user` | Always `pip install --user`. `uv pip` has no `--user`, so this selects pip. |

`install.python_executable` (`INITIALIZE_CLAUDE_AI_INSTALL_PYTHON`, default
`python3`) is the interpreter the Python-module checks import *with*. The
detected one is normally used and this is never consulted; set it on a machine
where the interpreter that should own the SDKs is not the one on `PATH`.

Three modifiers change what installation means:

| Flag | Effect |
|------|--------|
| `--force-install` | Reinstall even when verification already passes, using each manager's reinstalling form (`apt --reinstall`, `dnf reinstall`, `pacman -S` without `--needed`, `pip --force-reinstall`, …). For repairing a half-installed toolchain. The copy in use is reinstalled **through whoever installed it** (`install.reinstall_owner_only`): the package that owns the file or provides the command (asked of the system, e.g. `rpm -q --whatprovides npm` → `npm26`), a strategy whose own folders hold it, or a kind recognised from the path (`[install.ownership]`). A strategy that would put a second copy beside it is not run. An installer with no in-place form keeps what is there: it is reported OK with "kept", never rerun over its own folder (`reinstall_argv` / `reinstall_shell` / `reinstalls_in_place` per strategy; gcloud uses `gcloud components reinstall`). CHANGED means the binary in use actually changed (`install.changed_needs_proof`). |
| `--break-dependencies` | Install into a PEP 668 externally managed interpreter anyway — adds `--break-system-packages` to pip *and* uv. Ignored inside a virtualenv, where nothing is being broken. |
| `--downgrade` | Resolve pip/npm packages through `[install.pins]` instead of taking the latest release, for hosts whose kernel or glibc cannot carry current builds. |

Both the forcing forms and the pinned versions are data in
`initialize_claude_ai/data/10-components.toml`, so a distro quirk or a new floor
is an edit there, not a code change:

```toml
[install.pins.pip]
anthropic = "1.4.0"
claude-agent-sdk = "0.1.81"
```

A package with no pin still resolves to its latest release, and a package you
version yourself is left alone — the table is a set of known-good floors, not a
lock file.

Verified paths and versions are written to `$INITIALIZE_CLAUDE_AI_STATE_DIR/env.json` and
a sourceable `env.sh`, as `INITIALIZE_CLAUDE_AI_BIN_<COMPONENT>` and
`INITIALIZE_CLAUDE_AI_VERSION_<COMPONENT>`, so later phases never have to guess where a
binary is:

```bash
source ~/.local/state/initialize-claude-ai/env.sh
```

A toolchain that installs programs of its own somewhere off the PATH (Go's
`$GOBIN`, else `$GOPATH/bin`) is asked where, through its catalog entry's
`bin_dir_queries`, and the answer is recorded as
`INITIALIZE_CLAUDE_AI_BINDIR_<COMPONENT>`. Every later run puts it on the search
path, so a server built with `go install` can still be launched tomorrow.

## Prefetching what later phases launch with

Phase 4 starts MCP servers with programs phase 1 has no other reason to install:
`uvx`, `npx`, `go`, `cargo`, `bunx`, `docker`. `--install-prefetch` (setting
`install.prefetch`) closes that gap ahead of time, from data only:

| Mode | Adds to the selection |
| --- | --- |
| `none` | nothing — the selection is what it was |
| `launchers` (default) | the component behind the first word of every command template in `install.demand_sources` (`mcp.packages.launchers`, `mcp.bridge.adapters`) |
| `all` | that, plus every component in `install.prefetch_groups` (`mcp-runtimes`: go, rust, deno, bun, docker) |

A command maps to a component through the binaries the component verifies and
the ones it `provides` (`npm` provides `npx`, `uv` verifies `uvx`, `bun`
provides `bunx`), so a toolchain added to `10-components.toml` is covered
without code. What prefetching adds is always optional: a failure is reported
as skipped and never fails the phase. `--install-skip` wins over it,
`--install-only` turns it off (it names exactly what to install), and a
component whose entry says `prefetch = false` (docker, whose install is a
system daemon) is only added by `all` or by name. On the shipped configuration
`launchers` adds nothing — `uv` and `npm` are already installed — and exists so
that a new launcher is covered the day it is configured.

<a id="installing-for-what-registered-servers-run"></a>
## Installing for what registered servers run

The launcher templates say what phase 4 *may* run. The MCP entries Claude Code
starts from this directory -- `./.mcp.json` and the local and user scopes of
Claude Code's own configuration -- say what it *will* run, and a copied
`.mcp.json` arrives on machines no discovery ran on. So phase 1 also reads them
(`install.demand_from_entries`, on by default):

- **A program an entry starts or calls, missing here,** is installed from the
  catalog, the way a prefetch is: optional, `--install-skip` wins,
  `--install-only` turns it off, and a component with `prefetch = false`
  (docker) is checked but never installed. What a client bridge runs for every
  call (its manifest's client, `osc`) is installed with the client's own
  `install` steps from `[mcp.discovery.clients.<name>]`. A program nothing here
  installs (`github-mcp-server`) is named with the run that finds or replaces
  its server: `initialize-claude-ai run --phases 4 --mcp-recheck <address>`.
- **A program variable an entry reads gets this machine's path**
  (`install.machine_paths`). Every absolute path is written
  `${ICA_MCP_UVX:-/root/.local/bin/uvx}`, and the default belongs to the machine
  that wrote it. On the next one Claude Code said `ENOENT: posix_spawn
  '${ICA_MCP_UVX}'` while `uvx` was in `/usr/bin`. A variable is set only when
  the path an entry would get here does not exist; only to a path found the way
  the program names them (a configured root, a launcher, a binary it recorded or
  finds on `PATH`) that serves every entry reading it; and never over a value
  already set where a new terminal's Claude Code reads it. It goes to Claude
  Code's `settings.json` env (`persist_to`), and each entry reading it is then
  asked of Claude Code itself (`claude mcp get`, `check_with_claude`). A file
  this machine does not have -- a bridge generated on the other one -- is left
  to phase 4, with the command that regenerates it.

<a id="present-is-not-usable"></a>
## Present is not usable

A check in `10-components.toml` can say `purpose = "usable"`: it runs once the
component is there and asks whether it works here. It is never a reason to
install. A failure makes the component PARTIAL, with the fix chosen by the
check's `remedies` (a phrase in its output → the sentence that fixes it) or its
`remedy`. Two ship:

- **docker** -- `docker info`: the daemon must be running and its socket open to
  this user. Phase 4 asks the same check before it trials a server launched with
  docker (`mcp.packages.check_launcher_usable`); one that cannot work is "not
  launched", never rejected as broken.
- **claude-agent-sdk** -- which Claude Code the SDK runs (`agent_cli`). The SDK
  ships its own copy, and claude-agent-sdk 0.2.152's 2.1.259 answered "does not
  support this model" while the installed 2.1.309 served it. `mcp.agent.sdk_cli`
  (`INITIALIZE_CLAUDE_AI_SDK_CLI`) decides: `auto` (default) runs the installed
  `claude` when it is at least as new as the bundled one, `installed` always,
  `bundled` never. Phase 1 reports the answer phase 4 will act on.

Go's download fallback fetches `INITIALIZE_CLAUDE_AI_GO_VERSION` from go.dev when
no package manager has it; each toolchain accepts any version that runs unless
`INITIALIZE_CLAUDE_AI_<GO|RUST|DENO|BUN|DOCKER>_MIN_VERSION` sets a floor.
