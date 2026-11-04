# Credentials

**No credential value ever appears in code, in a config file, in a log line, in
an agent prompt or in the JSON report.** Credentials are named by *reference*
and fetched at the moment of use:

| Reference | Source |
|-----------|--------|
| `env:NAME` | environment variable |
| `file:/path` | file contents (warns when group/world readable) |
| `keyring:service/user` | OS keyring |
| `command:...` | stdout of a command |
| `gcloud-adc` | `gcloud auth application-default print-access-token` |
| `gcloud-token` | `gcloud auth print-access-token` |
| `prompt:Label` | interactive, echo off |
| `literal:VALUE` | inline — testing only |

Each credential the CLI accepts comes in two forms. `--x-ref` names where to
read the value; `--x` carries it inline and compiles to a `literal:` reference,
so both travel one resolution path:

```bash
initialize-claude-ai run --phases 2 --auth-anthropic-key-ref keyring:anthropic/prod
initialize-claude-ai run --phases 2 --auth-anthropic-key sk-ant-...      # visible in ps
```

**`--secure` removes the second form.** It refuses every value-taking flag on
the command line *and* sets `secrets.allow_literal = false`, so an inline
credential is rejected wherever it came from — a config file, an environment
variable or `--set`. Naming a credential keeps working; handing one over does
not:

```bash
initialize-claude-ai run --secure --auth-anthropic-key sk-ant-...
# error: --secure refuses --auth-anthropic-key, which puts the value in ps output
#        and the shell history; use --auth-anthropic-key-ref with env:NAME, ...
```

| Named (works under `--secure`) | Inline (refused by `--secure`) | Setting | Sent as |
|---|---|---------|---------|
| `--auth-anthropic-key-ref REF` | `--auth-anthropic-key VALUE` | `auth.anthropic_api.api_key_refs` | `x-api-key` |
| `--auth-anthropic-token-ref REF` | `--auth-anthropic-token VALUE` | `auth.anthropic_api.auth_token_refs` | `Authorization: Bearer` |
| `--auth-claude-token-ref REF` | `--auth-claude-token VALUE` | `auth.claude_oauth.token_ref` | Claude Code CLI |
| `--mcp-token-ref REF` | `--mcp-token VALUE` | `mcp.discovery.auth.bearer_token_ref` | MCP probe |

The pairs are declared in one table (`cli.SECRET_FLAGS`) that drives argparse,
the setting mapping and the `--secure` check together, so a credential flag
added later cannot quietly escape the policy — a test asserts the table and the
parser agree.

Four further guarantees:

- **Redaction at the sink.** Every resolved secret is registered and masked in
  the console, the log file and the report, alongside pattern matching for
  well-known key shapes and key-ish names in mappings and argv. A secret cannot
  escape even if a caller forgets to scrub it.
- **A credential typed into an address never survives the parse.**
  `--mcp-server https://admin:pw@box.corp/mcp` is how an appliance behind basic
  auth is routinely passed around. The value is stripped from the URL before it
  is probed (use `--mcp-token-ref` instead), and masked to `admin:***` in the
  echo of what was typed, so no report, transcript or log line carries it — and
  redaction catches the same shape anywhere else it appears.
- **Placeholders in generated files.** An MCP entry that needs a token is
  written as `"API_TOKEN": "${API_TOKEN}"`; Claude Code expands it at launch, so
  the file on disk holds a name, not a value.
- **A boundary around agent output.** Anything an agent proposes is untrusted
  input: a candidate carrying an inline credential in its URL, argv or
  environment is rejected outright, and `literal:` is not an accepted scheme
  across that boundary.
