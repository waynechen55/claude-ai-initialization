# Development

How the code is laid out, how it is tested, and the checks that keep the
repository publishable. The contribution workflow — one commit per pull
request, with a title and a body — is in [CONTRIBUTING.md](../CONTRIBUTING.md).

## Layout

```
initialize_claude_ai/
  cli.py                   argument parsing, layering, dispatch
  core/                    config (the layering), context (what a phase is
                           handed), secrets, redact, http, proc, envfile,
                           fileio (owner-only atomic writes),
                           logging_setup (console + transcript), report,
                           deadline (the --timeout ceiling), platform_detect,
                           trust_store (one run-wide answer to "is this
                           certificate trusted"), locks, errors
  install/                 component specs, installers, verification   (phase 1)
  authenticate/            base (inspect/acquire/verify), google_cloud,
                           claude_oauth, anthropic_api, browser,
                           signin_url                                  (phase 2)
  provision/               models, settings_writer, inventory          (phase 3)
  mcp_auto_discovery/      address, pathgen, apispec (finding and reading an
                           API description), probes, verifier, policy (the
                           tier gate), portability (will the entry survive
                           this machine), grounding (does the entry name the
                           service), secret_policy (what counts as a
                           credential, for both pipelines), env_contract
                           (every variable a server reads is in its entry),
                           trust (the CA chain a server is told about),
                           healing (repairing an entry that fails to start),
                           types, agentic, traditional, registry_client,
                           bridge, ranking, runtime, writer            (phase 4)
  phases/                  base (the Phase contract and the grouped report),
                           phase1_install, phase2_authenticate,
                           phase3_provision, phase4_mcp_auto_discovery,
                           and the registry that makes --phases work
  data/*.toml              every default value in the program
  skills/*/SKILL.md        the skills the phase-4 agents load at run time
tests/                     pytest suite, offline and sandboxed
tools/                     the checks CI runs (commits, names, links, version,
                           generated docs, package data); stdlib only
docs/                      the guides; *-reference.md are generated
.github/                   CI and release workflows, templates
.env.example               every environment variable, with what it does
```

Two files carry the bulk of the reasoning rather than the code:
`mcp_auto_discovery/policy.py` is where the tier order stops being a preference,
and `core/config.py` is where every value in the program comes from. Reading
those two explains most of the rest.

The small modules beside them each hold one rule about what may be written, and
they are separate files on purpose: every one of them applies to **both**
pipelines, and a rule that lived in `agentic.py` would only ever be enforced
when an agent happened to be running. `grounding.py` (does the entry name its
service), `portability.py` (will it survive this machine), `secret_policy.py`
(what counts as a credential, whoever claimed otherwise).

## Tests

```bash
make test                         # python3 -m pytest -n auto
make cov                          # with coverage, failing under the floor in pyproject.toml
python3 -m pytest tests/test_cli.py -q
```

The suite is offline and hermetic: fixtures redirect `HOME`, the XDG
directories and the state directory into `tmp_path`, so a test can never read or
write your real `~/.claude`. They also unset every variable the packaged
defaults interpolate — `GOOGLE_CLOUD_PROJECT`, `CLOUD_ML_REGION` and the rest,
which a successful run of this very program exports — so the suite tests the
code rather than the machine it happens to be run on. That list is derived from
the defaults, not written out, so adding a `${VAR}` cannot quietly reintroduce
the dependency.

Two further properties are tested, because CI runners differ from a
developer's machine: the program's own variables (`ICA_MCP_*`, `MCP_BRIDGE_*`,
`MCP_CLIENT_*`) and every trust-store variable the configuration names are
removed for each test, and no test assumes one distribution's CA bundle path
(`/etc/ssl/ca-bundle.pem` on openSUSE, `/etc/ssl/certs/ca-certificates.crt` on
Debian and Ubuntu) — a test that needs one fixes `trust_store.system_bundle`.

## Repository checks

Each is a standalone script in `tools/` (standard library only), run by CI and
by `make`:

| Script | Checks | Make target |
| --- | --- | --- |
| `check_commits.py` | a pull request is one non-merge commit with a title and a body | `make commits` |
| `check_names.py` | no internal host names or personal paths (denied names stored as SHA-256 fingerprints) | `make names` |
| `check_links.py` | every relative Markdown link and anchor resolves | `make links` |
| `check_version.py` | one version in `__init__.py`, the changelog, the tag; prints release notes | `make version` |
| `gen_docs.py` | regenerates / checks `docs/cli-reference.md` and `docs/config-reference.md` | `make docs`, `make docs-check` |
| `check_package_data.py` | an installed wheel carries every TOML default and skill | CI `package` job |
| `dev_requirement.py` | prints the pinned version of one dev tool for CI | — |

