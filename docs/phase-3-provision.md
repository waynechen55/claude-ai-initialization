# Phase 3 — provision

Selects the backend (`vertex`, `anthropic-api`, `bedrock` or `auto`), resolves
project, region and account, queries the backend's live model catalogue, maps it
onto the opus/sonnet/haiku tiers and writes `settings.json`:

```
✔ [   OK   ] model discovery  12 model(s) offered by us
✔ [   OK   ] models  opus=claude-opus-5, sonnet=claude-sonnet-4-5@20250929, haiku=claude-haiku-4-5@20251001
• [  SKIP  ] model in settings  kept opus from settings.json
```

An inferred model never overwrites a choice you already made — `--default-model`
or `provision.overwrite_inferred_model = true` if you want it to. The phase
closes with a full inventory: backend, binaries, credentials, models, settings,
MCP servers and the exported environment.

**And it ends by telling you which models you can reach.** The catalogue was
already being fetched — to choose a model and to check the chosen one — and then
went into the JSON report and nowhere else, so the phase finished without ever
answering *"which models do I have?"* on screen:

```
✔ [   OK   ] available models  12 model(s) offered to this credential on vertex; Claude Code will use claude-opus-5
  · opus: claude-opus-5, claude-opus-4-8 (+1 more)
  · sonnet: claude-sonnet-5, claude-sonnet-4-5@20250929
  · haiku: claude-haiku-4-5@20251001
  · The full list for vertex: https://console.cloud.google.com/vertex-ai/model-garden?project=…
  · To print it yourself: gcloud ai model-garden models list --project=… --region=…
```

Grouped by tier and capped per tier (`provision.catalogue.per_tier`), because
the point is recognition rather than completeness — and capping is only fair if
the rest is one link away, so the documentation URL and a command you can run
yourself are printed every time. Both are per-backend TOML entries
(`[provision.catalogue.docs]`, `[provision.catalogue.commands]`), so supporting
another backend is configuration rather than code.

They are printed **even when the listing fails**, which is when they matter
most: Bedrock has no discovery route here, an offline host has none either, and
a permission the listing API wants is not one this program needs. *"I could not
list them"* is half an answer.

**"Offered" is not "entitled", and the step says so.** A publisher catalogue
lists what the provider publishes, not what your project may call. A real run
reported `12 model(s) offered by global` and picked `claude-opus-5-5` from that
list; every phase-4 agent call then returned *"The model claude-opus-5-5 is not
available on your vertex deployment"*. Only a call settles entitlement, and this
step does not make one — so it says `offered`, and warns that a listed model can
still be refused per project or per region.

**How each tier gets its model.** Three sources, first one wins:

1. **A pin** — `--default-opus-model`, `ANTHROPIC_DEFAULT_OPUS_MODEL`, or
   `provision.<backend>.model_aliases.opus`. Taken at its word, no check against
   the catalogue beyond the `model check` warning on the primary.
2. **The live catalogue** — the best model of that tier the selected backend
   offers in the selected region. This is the good answer and the reason
   discovery exists: it is the only source that knows what the backend will
   actually serve, spelled the way *that* backend spells it (Vertex qualifies
   ids with `@version`; the Anthropic API does not).
3. **`provision.model_fallbacks`** — `claude-opus-5`, `claude-sonnet-5`,
   `claude-haiku-4-5`. Used only when there was no catalogue to ask: offline,
   discovery timed out, or the region returned an empty publisher list. These
   are a *guess*, not a default — a literal id cannot know what this backend
   serves — so the step drops to `PARTIAL` and says so:

```
▲ [PARTIAL ] models  opus=claude-opus-5, sonnet=claude-sonnet-5, haiku=claude-haiku-4-5  (no catalogue; fell back for: opus, sonnet, haiku)
      · Unverified fallback ids from provision.model_fallbacks -- pin them with --default-opus-model if they are wrong for this backend.
```

**`settings.json` gets the tier, not the id.** `model` is written as `"opus"`,
`"sonnet"` or `"haiku"`, while the id each tier resolved to goes into
`ANTHROPIC_DEFAULT_<TIER>_MODEL`. A tier name is the only spelling that survives
both a model refresh and a change of backend — Vertex qualifies ids with
`@version`, the Anthropic API does not, Bedrock spells them differently again —
and it is what a tier switch inside Claude Code follows. `--default-model`
chooses the tier; give it a model id and phase 3 writes the tier that id belongs
to. Left unset, `provision.default_tier` decides; set it to `sonnet` for a
cheaper everyday default.

**The rest of `settings.json`.** Everything under `[provision.settings]` is
written as it stands, so adding a key there is all it takes — no code change.
The packaged set is the model above plus:

| Key | Value | Variable |
| --- | --- | --- |
| `theme` | `auto` — follow the terminal's own light/dark setting | `INITIALIZE_CLAUDE_AI_THEME` |
| `tui` | `fullscreen` | `INITIALIZE_CLAUDE_AI_TUI` |
| `permissions.defaultMode` | `default` — stop and ask about a tool call no rule covers | `INITIALIZE_CLAUDE_AI_PERMISSION_MODE` |

```
↻ [CHANGED ] settings.json  created ~/.claude/settings.json (10 key(s))
      · env.CLAUDE_CODE_USE_VERTEX = 1
      ...
      · model = opus
      · permissions.defaultMode = default
      · theme = auto
      · tui = fullscreen
```

These are *seeded, not enforced*: Claude Code writes this same file when you run
`/theme` or `/permissions`, so a value already there wins and a re-run reports
`already up to date`. Only the leaf is compared — a `permissions.allow` list you
wrote keeps its rules and still gets a `defaultMode` beside them. Set
`provision.overwrite_existing_settings = true` to make the configured values
authoritative on every run, and empty a setting
(`--set provision.settings.tui=`) to stop writing that key at all.

**Switching backend retracts the old one.** Merging into `settings.json` can add
and change keys but cannot delete them, so provisioning `anthropic-api` over a
machine that previously ran Vertex used to leave `CLAUDE_CODE_USE_VERTEX = "1"`
behind — and Claude Code went on routing to Vertex, from a file nobody remembers
editing. Phase 3 now takes those variables back out, in `settings.json` and in
the env files alike, and reports each one:

```
✔ [   OK   ] settings.json  would update ~/.claude/settings.json (6 key(s))
      · env.ANTHROPIC_BASE_URL = https://api.anthropic.com
      · env.CLAUDE_CODE_USE_VERTEX: 1 -> (removed)
      · env.CLOUD_ML_REGION: us-east5 -> (removed)
↻ [CHANGED ] environment  4 exported variable(s), 2 retired; source ~/.local/state/initialize-claude-ai/env.sh
```

Only variables phase 3 is itself capable of writing are ever removed: the union
of every backend's `[env]` keys and `[env_names]` values, derived from the
configuration rather than listed in code, so a backend added to the TOML is
covered automatically. Anything else in your `env` block — `HTTPS_PROXY`, your
own variables — is outside that set and is never touched, and neither is
anything outside `env`. Set `provision.retire_stale_env = false` to make the
phase purely additive again.

Bedrock carries the same `[provision.bedrock.env_names]` and
`model_aliases` tables as the other two backends, so `provision.bedrock.region`
reaches `AWS_REGION` and the three `--default-<tier>-model` pins apply there too.
