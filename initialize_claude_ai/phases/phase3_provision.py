"""Phase 3 -- provision and configure the Claude environment.

Picks the backend Claude Code should talk to, resolves the account, project and
region behind it, asks that backend which models it will actually serve, writes
the result into ``settings.json`` and the initialize-claude-ai environment files, and
then lists everything out.

Backend selection is ``auto`` by default: the first entry of
``provision.backend_priority`` that is both enabled and backed by a credential
phase 2 verified.  ``--provision-backend`` (``provision.backend``) overrides it.

Like every phase this is idempotent -- re-running it re-reads the live state and
writes only what actually differs.
"""

from __future__ import annotations

from typing import Any, Mapping

from ..core import diskspace
from ..core.config import fill
from ..core.report import BAD, PhaseResult, Safety, Status
from ..provision import AUTH_FOR_BACKEND
from ..provision import inventory as inventory_mod
from ..provision import models as models_mod
from ..provision.settings_writer import SettingsWriter, settings_path
from .base import Phase, register


@register
class ProvisionPhase(Phase):
    """Phase 3 -- point Claude Code at the backend phase 2 authenticated.

    Chooses the backend, resolves the coordinates it cannot work without
    (``provision.required.<backend>`` says which), asks that backend which
    models it will actually serve rather than assuming a list, and writes the
    answer into ``settings.json`` and the environment files. Everything it
    writes is merged into what is already there, never overwritten wholesale.
    """

    slug = "provision"
    number = 3
    title = "Provision & configure"
    description = (
        "Choose the backend Claude Code talks to, resolve the account, project and "
        "region behind it, ask that backend which models it will actually serve, and "
        "write the result into settings.json and the environment files."
    )
    enable_key = "provision"

    def banner_facts(self) -> list[tuple[str, str]]:
        config = self.settings.section("provision")
        requested = (self.ctx.state.get("provision_backend") or config.str_("backend", "auto")).strip().lower()
        verified = self.ctx.state.get("auth_verified")
        return [
            (
                "Backend",
                requested
                if requested != "auto"
                else "auto -- "
                + ", ".join(
                    config.str_list(
                        "backend_priority",
                        [
                            "vertex",
                            "anthropic-api",
                            "bedrock",
                        ],
                    )
                ),
            ),
            ("Credentials", ", ".join(sorted(verified)) if verified else "not checked in this run"),
            ("Tiers", ", ".join(config.str_list("tiers", list(models_mod.DEFAULT_TIERS)))),
            ("Settings file", str(settings_path(self.ctx))),
            *super().banner_facts(),
        ]

    def safety(self, result: PhaseResult) -> tuple[str, str]:
        """The only question that matters here: is Claude Code configured or not?

        Two things have to be true -- a backend was chosen, and ``settings.json``
        was written. Everything else this phase reports (an unlisted model, a
        catalogue that would not answer) leaves a working configuration behind
        and is a caveat rather than a stop.
        """
        if not result.steps:
            # The phase never executed; the base class has the right words for that.
            return super().safety(result)
        steps = {step.name: step for step in result.steps}
        backend = self.ctx.state.get("provision_backend_active", "")
        written = steps.get("settings.json")
        path = settings_path(self.ctx)

        if result.fatal:
            missing = ", ".join(s.name for s in result.steps if s.error == "missing_setting")
            return Safety.BLOCKED, (
                f"Provisioning stopped: {missing or 'a required value'} has no value, here or in "
                "any file or variable this run could read, and there is no honest default for "
                f"it. Nothing was written to {path}, deliberately -- a settings.json missing "
                "this would look finished and would not work. Supply it as listed above and "
                "re-run phase 3."
            )
        if not backend:
            return Safety.BLOCKED, (
                "No backend was selected, so nothing was configured. Claude Code is left on "
                f"whatever {path} already said, which may be nothing at all. Authenticate a "
                "backend (phase 2) or force one with --provision-backend, then re-run phase 3."
            )
        if written is None or written.status == Status.FAILED:
            return Safety.BLOCKED, (
                f"{backend} was selected but {path} could not be written, so Claude Code has "
                "not been pointed at it. The rest of the run will not fix that; resolve the "
                "file error above and re-run phase 3."
            )
        if result.status in BAD:
            primary = steps.get("primary model")
            model = f" on {primary.detail}" if primary and primary.detail else ""
            return Safety.CAUTION, (
                f"Claude Code is configured for {backend}{model} and {path} was written, so it "
                "is usable. The items above are unresolved details -- check them before "
                "trusting model selection, but they do not stop phase 4 or normal use."
            )
        return Safety.SAFE, (
            f"Claude Code is configured for {backend}, the models were confirmed against the "
            f"backend's own catalogue, and {path} reflects it."
        )

    def execute(self, result: PhaseResult) -> None:
        """Select the backend, resolve its coordinates, discover models, write.

        The active backend is recorded into the run's state before any of the
        work, so a phase that stops half way still reports which backend it
        stopped on.
        """
        config = self.settings.section("provision")
        tiers = tuple(config.str_list("tiers", list(models_mod.DEFAULT_TIERS)))

        backend = self._select_backend(result, config)
        if not backend:
            return
        # Recorded before the work rather than after it, so that a run which
        # stops short still says which backend it stopped on.
        self.ctx.state["provision_backend_active"] = backend

        backend_cfg = self.settings.section(f"provision.{backend.replace('-', '_')}")
        profile = self._profile(result, backend, backend_cfg)
        self._require_profile(result, config, backend, profile)

        catalog = self._discover(result, config, backend, profile, tiers)
        selection = self._select_models(result, config, backend_cfg, catalog, tiers)

        env_block = self._env_block(backend_cfg, profile, selection)
        env_block.update(self._login_env(backend_cfg))
        # What this phase wrote before and no longer stands behind. Computed
        # once so settings.json and the env files retract exactly the same set.
        stale = self._stale_env_names(config, env_block, tiers)
        self._write_settings(result, config, env_block, selection, stale, backend=backend)
        self._export_env(result, config, backend, profile, selection, env_block, stale)
        self._fresh_terminal_check(result, backend)
        self._inventory(result, backend, profile, catalog, selection)
        # Last, so it is the thing still on screen when the phase ends: what
        # this credential can actually reach, and where to see the rest.
        self._report_available_models(result, backend, profile, catalog, selection)

    # -- backend ---------------------------------------------------------------

    def _select_backend(self, result: PhaseResult, config) -> str:
        requested = (self.ctx.state.get("provision_backend") or config.str_("backend", "auto")).strip().lower()
        priority = config.str_list("backend_priority", ["vertex", "anthropic-api", "bedrock"])
        verified = self.ctx.state.get("auth_verified")
        # A standalone `--phases 3` run has no phase-2 state: do not filter on
        # credentials we never looked at, just report the weaker guarantee.
        auth_known = verified is not None
        verified = set(verified or [])

        if requested and requested != "auto":
            if not self._backend_enabled(requested):
                section = f"provision.{requested.replace('-', '_')}"
                # A backend with a switch of its own declares it in
                # configuration, so this can name the flag the operator would
                # actually type rather than the key it happens to compile to.
                flag = self.settings.str_(f"{section}.enable_flag").strip()
                self.report_step(
                    result,
                    "backend",
                    Status.FAILED,
                    f"{requested} is disabled ({section}.enabled = false)",
                    remediation=[
                        *([f"Turn it back on with {flag}"] if flag else []),
                        f"Enable it, or choose another with --provision-backend {'|'.join(priority)}",
                    ],
                )
                return ""
            self.report_step(result, "backend", Status.OK, f"{requested} (explicitly selected)")
            return requested

        skipped: list[str] = []
        for candidate in priority:
            if not self._backend_enabled(candidate):
                skipped.append(f"{candidate}: disabled")
                continue
            needed = AUTH_FOR_BACKEND.get(candidate, "")
            if auth_known and needed and needed not in verified:
                skipped.append(f"{candidate}: {needed} not verified")
                continue
            detail = f"{candidate} (auto-selected)"
            if not auth_known:
                detail += " - credentials not checked in this run"
            self.report_step(result, "backend", Status.OK, detail, data={"skipped": skipped})
            return candidate

        self.report_step(
            result,
            "backend",
            Status.FAILED,
            "no backend is both enabled and authenticated",
            data={"considered": priority, "skipped": skipped},
            remediation=[
                "Authenticate one first: initialize-claude-ai run --phases 2",
                f"Or force one: initialize-claude-ai run --phases 3 --provision-backend "
                f"{priority[0] if priority else 'vertex'}",
            ],
        )
        return ""

    def _backend_enabled(self, backend: str) -> bool:
        return self.settings.bool_(f"provision.{backend.replace('-', '_')}.enabled", False)

    # -- account / project / region -------------------------------------------

    def _profile(self, result: PhaseResult, backend: str, cfg) -> dict[str, str]:
        """Resolve the identity coordinates for the chosen backend."""
        if backend == "vertex":
            return self._vertex_profile(result, cfg)
        if backend == "anthropic-api":
            base_url = cfg.str_("base_url", "https://api.anthropic.com")
            self.report_step(result, "endpoint", Status.OK, base_url)
            return {"endpoint": base_url}
        region = cfg.str_("region")
        # Unset is left to _require_profile: Bedrock has no default region and
        # every call has to name one, so this is a stop rather than a caveat.
        if region:
            self.report_step(result, "region", Status.OK, region)
        return {"region": region}

    def _require_profile(self, result: PhaseResult, config, backend: str, profile) -> None:
        """Refuse to provision a backend whose coordinates are incomplete.

        ``provision.required.<backend>`` names the fields that backend cannot
        be addressed without -- the project Vertex bills, the region Bedrock
        lives in. Each is both a key of the resolved profile and a setting
        under the backend's own section, so one name answers both "what is
        missing" and "what would you set to fix it".

        Every layer has already been consulted by this point: the flag, the
        config files, the environment and whatever ``--env-file`` put there.
        An empty value here means there is nothing to use anywhere, and no
        default worth inventing -- so the run stops instead of writing a
        configuration that is missing the one field that makes it work.
        """
        key = backend.replace("-", "_")
        for field_name in config.str_list(f"required.{key}"):
            self.require(
                result,
                field_name,
                profile.get(field_name, ""),
                setting=f"provision.{key}.{field_name}",
                why=f"{backend} needs a {field_name} and none was found",
            )

    def _vertex_profile(self, result: PhaseResult, cfg) -> dict[str, str]:
        gcloud = self.ctx.tool("gcloud", config_key="paths.gcloud")
        project, source = self._vertex_project(cfg, gcloud)
        account = self._gcloud_value(gcloud, "active_account") if gcloud else ""

        region = cfg.str_("region", "global").strip()
        known = cfg.str_list("known_regions")

        # A missing project is not reported here: it is a required coordinate,
        # and _require_profile fails it with the full list of places it could
        # have come from rather than a message this function would have to keep
        # in step with the [sources] table by hand.
        if project:
            self.report_step(result, "project", Status.OK, f"{project} ({source})")

        if known and region not in known:
            self.report_step(
                result,
                "region",
                Status.PARTIAL,
                f"{region} is not in provision.vertex.known_regions - continuing anyway",
            )
        else:
            # Same courtesy as the project line: say which of the several places
            # a region can come from this one did, so 'why that one' does not
            # need an archaeology session.
            origin = self.setting_origin("provision.vertex.region", region)
            self.report_step(result, "region", Status.OK, f"{region} ({origin})" if origin else region)

        if account:
            self.report_step(result, "account", Status.OK, account)

        return {"project": project, "region": region, "account": account}

    def _vertex_project(self, cfg, gcloud: str) -> tuple[str, str]:
        """The billing project, and which of the three sources supplied it.

        In order of how deliberate each source is, and stopping at the first:
        what was configured (``--gcloud-project``, an env file, ``ANTHROPIC_
        VERTEX_PROJECT_ID``), then the project phase 2 signed in against, then
        gcloud's active project, and last what the settings file being rewritten
        already said.  The phase-2 answer is preferred over asking gcloud a
        second time because it cannot disagree with the credential that was
        actually verified -- the sign-in may itself have changed which project
        is active, and an impersonated service account belongs to one that
        ``gcloud config`` never mentions.

        The settings file comes last because it is this program's own previous
        answer (see :func:`~initialize_claude_ai.core.config.demote_echo`); it
        is consulted at all because, unlike the region, a project id has no
        default worth inventing, and keeping the one already in the file beats
        failing a host that has nothing else left to say.

        The source is reported alongside the id.  "which project is this going
        to bill" is not a question worth making somebody reverse-engineer from
        an empty flag and a shell they did not set up.
        """
        project = cfg.str_("project").strip()
        if project:
            return project, "configured"

        outcome = (self.ctx.state.get("auth_outcomes") or {}).get("google-cloud")
        signed_in = str(getattr(outcome, "data", {}).get("project", "") or "").strip()
        if signed_in:
            return signed_in, "from the phase 2 sign-in"

        if gcloud:
            value = self._gcloud_value(gcloud, "get_project")
            if value:
                return value, "gcloud active project"

        name = str(cfg.dict_("env_names").get("project", "") or "")
        previous = str(self.settings.demoted.get(name, "") or "").strip()
        if previous:
            path = settings_path(self.ctx)
            return previous, f"already in {path.name}"
        return "", ""

    def _gcloud_value(self, gcloud: str, command_key: str) -> str:
        """Run a read-only `gcloud config get-value` style command."""
        google = self.settings.section("auth.google_cloud")
        template = google.list_(f"commands.{command_key}")
        if not template:
            return ""
        argv = [fill(p, {"gcloud": gcloud}) for p in template]
        probe = self.runner.probe(
            argv, timeout=google.float_(f"command_timeout_seconds.{command_key}", self.runner.default_timeout)
        )
        if not probe.ok:
            return ""
        value = probe.first_line().strip()
        return "" if value in google.str_list("unset_values", ["(unset)", "None"]) else value

    # -- models ----------------------------------------------------------------

    def _discover(self, result: PhaseResult, config, backend, profile, tiers):
        if not config.bool_("discover_models", True):
            self.report_step(result, "model discovery", Status.SKIPPED, "disabled by configuration")
            return models_mod.ModelCatalog(backend=backend, tiers=tiers)

        catalog = models_mod.discover(
            self.ctx,
            backend,
            project=profile.get("project", ""),
            region=profile.get("region", ""),
            tiers=tiers,
        )
        self.report_step(
            result,
            "model discovery",
            Status.OK if catalog.ok else Status.PARTIAL,
            catalog.detail or "no catalogue returned",
            data=catalog.to_dict(),
            remediation=catalog.remediation,
        )
        return catalog

    def _catalogue_pointer(self, backend: str, profile) -> tuple[str, str]:
        """``(where the full list is, how to print it yourself)`` for a backend.

        Both come from ``[provision.catalogue.docs]`` and
        ``[provision.catalogue.commands]``, keyed by backend name, so supporting
        another backend is a TOML entry rather than a change here. An entry that
        is missing simply yields ``""`` and is left out of the report.
        """
        tokens = {
            "project": str(profile.get("project", "") or ""),
            "region": str(profile.get("region", "") or ""),
        }

        def rendered(table: str) -> str:
            value = str(self.settings.dict_(f"provision.catalogue.{table}", {}).get(backend, ""))
            return fill(value, tokens).strip()

        return rendered("docs"), rendered("commands")

    def _report_available_models(self, result, backend, profile, catalog, selection) -> None:
        """Say which models this credential can reach, and where the rest are.

        The catalogue was already being fetched -- to choose a model and to check
        the chosen one -- and then went into the JSON report and nowhere else. So
        phase 3 ended without the operator ever being told what their credential
        could serve, and the answer arrived in phase 4 as
        ``The model claude-opus-5-5 is not available on your vertex deployment``,
        twenty-nine times across thirteen addresses.

        Printed last in the phase for the same reason: it is what should still be
        on screen when provisioning finishes.

        The pointer is shown whether or not the listing worked. A catalogue can
        be empty for reasons that say nothing about the operator's access -- an
        offline host, a backend with no discovery route (Bedrock has none here),
        a permission the listing API wants and this program does not -- and "I
        could not list them" is only half an answer.
        """
        section = self.settings.section("provision.catalogue")
        if not section.bool_("show", True):
            return

        docs, command = self._catalogue_pointer(backend, profile)
        remediation = [f"The full list for {backend}: {docs}" if docs else ""]
        remediation.append(f"To print it yourself: {command}" if command else "")
        remediation = [line for line in remediation if line]

        if not catalog.models:
            # Not a failure of the operator's, and not a claim that they have no
            # models -- only that this program could not enumerate them here.
            self.report_step(
                result,
                "available models",
                Status.PARTIAL,
                catalog.detail or f"this run could not list the models available for {backend}",
                data={"backend": backend, "docs_url": docs, "list_command": command},
                remediation=remediation,
            )
            return

        grouped = catalog.by_tier()
        per_tier = max(1, section.int_("per_tier", 4))
        lines: list[str] = []
        if grouped:
            for tier in catalog.tiers:
                found = grouped.get(tier, [])
                if not found:
                    continue
                shown = [m.qualified for m in found[:per_tier]]
                more = len(found) - len(shown)
                lines.append(f"{tier}: " + ", ".join(shown) + (f" (+{more} more)" if more > 0 else ""))
        else:
            # A backend whose ids carry no tier vocabulary. Still worth listing;
            # the grouping is a convenience, not the content.
            limit = max(1, section.int_("untiered", 8))
            shown = catalog.qualified_ids()[:limit]
            more = len(catalog.models) - len(shown)
            lines.append(", ".join(shown) + (f" (+{more} more)" if more > 0 else ""))

        primary = str(selection.get("primary", "") or "")
        # "offered", not "available to you". The distinction is not pedantry: a
        # publisher catalogue lists what the provider publishes, which is not the
        # same as what this project is entitled to call. A real run picked
        # claude-opus-5-5 out of a catalogue of 12 that named it, and every
        # phase-4 call then returned "not available on your vertex deployment".
        # Only a call proves entitlement, and this step does not make one.
        detail = f"{len(catalog.models)} model(s) offered to this credential on {backend}"
        if primary:
            detail += f"; Claude Code will use {primary}"
        remediation.append(
            "This is what the provider publishes, which is not proof of entitlement -- a model "
            "can be listed here and still be refused at call time, per project or per region. "
            "If a call later reports the model is unavailable, the list above is where to pick "
            "another and --default-model is how to set it."
        )
        self.report_step(
            result,
            "available models",
            Status.OK,
            detail,
            data={
                "backend": backend,
                "count": len(catalog.models),
                "by_tier": {t: [m.qualified for m in ms] for t, ms in grouped.items()},
                "all": catalog.qualified_ids(),
                "docs_url": docs,
                "list_command": command,
            },
            remediation=[*lines, *remediation],
        )

    def _select_models(self, result, config, backend_cfg, catalog, tiers) -> dict[str, Any]:
        """Fill each tier, in descending order of how much the answer is trusted.

        1. A pin: ``--default-<tier>-model``, ``ANTHROPIC_DEFAULT_<TIER>_MODEL``
           or ``provision.<backend>.model_aliases.<tier>``. Taken at its word.
        2. The best model of that tier in the backend's live catalogue. This is
           the good answer, and the reason discovery exists -- it is the only
           source that knows what this backend, in this region, will actually
           serve, spelled the way that backend spells it.
        3. ``provision.model_fallbacks.<tier>``, which is a guess. It is used
           only when there was no catalogue to ask: offline, timed out, or a
           region whose publisher list came back empty. Writing a guess beats
           leaving the tier unset, which leaves Claude Code with no model to
           switch to at all -- but it is reported as a guess, because a literal
           id can be wrong here in a way the other two cannot.
        """
        aliases = backend_cfg.dict_("model_aliases")
        fallbacks = config.dict_("model_fallbacks")
        resolved: dict[str, str] = {}
        inferred: list[str] = []
        guessed: list[str] = []

        for tier in tiers:
            configured = str(aliases.get(tier, "") or "").strip()
            if configured:
                resolved[tier] = configured
                continue
            best = catalog.best_for_tier(tier)
            if best:
                resolved[tier] = best.qualified
                inferred.append(tier)
                continue
            fallback = str(fallbacks.get(tier, "") or "").strip()
            if fallback:
                resolved[tier] = fallback
                guessed.append(tier)

        if not resolved and config.bool_("require_model", True):
            # Every source came back empty, including the fallbacks -- which
            # only happens if they were deliberately blanked. Writing a
            # settings.json with no model in it would leave Claude Code with
            # nothing to call, so this is a stop rather than a caveat.
            self.require(
                result,
                "models",
                "",
                setting="provision.settings.model",
                why=f"no model resolved for any of the tiers: {', '.join(tiers)}",
                remediation=["Or pin the tiers with provision.model_fallbacks."],
            )

        requested = self.settings.str_("provision.settings.model") or backend_cfg.str_("default_model")
        # Two answers, because the two places that get told differ. settings.json
        # "model" names a *tier* -- "opus" -- which stays true as the line-up
        # moves and is spelled the same for all three backends; the id it
        # resolved to is what the catalogue check and the report need, and it
        # reaches Claude Code through ANTHROPIC_DEFAULT_<TIER>_MODEL.
        default_tier = config.str_("default_tier", tiers[0] if tiers else "")
        by_name = {tier.lower(): tier for tier in tiers}
        asked_tier = by_name.get(requested.strip().lower(), "")
        if asked_tier:  # --default-model opus
            primary_tier = asked_tier
            primary = resolved.get(asked_tier, "")
        elif requested:  # --default-model <an id>: write the tier it belongs to
            primary = requested
            primary_tier = models_mod.ModelInfo(id=requested).tier(tiers)
        else:
            primary_tier = default_tier if default_tier in resolved else (tiers[0] if tiers else "")
            primary = resolved.get(primary_tier, "")
            # That tier resolved to nothing, so there is no tier to name either:
            # _write_settings leaves "model" alone rather than inventing one.
            primary_tier = primary_tier if primary else ""
        # An id that matches no tier at all is somebody's deliberate choice --
        # a fine-tune, a preview name -- so it is written as given rather than
        # dropped for not fitting the vocabulary.
        primary_setting = primary_tier or requested

        status = Status.OK if resolved else Status.PARTIAL
        detail = ", ".join(f"{tier}={model}" for tier, model in resolved.items()) or "none resolved"
        if inferred:
            detail += f"  (inferred: {', '.join(inferred)})"
        if guessed:
            status = Status.PARTIAL
            detail += f"  (no catalogue; fell back for: {', '.join(guessed)})"
        self.report_step(
            result,
            "models",
            status,
            detail,
            remediation=[
                f"Unverified fallback ids from provision.model_fallbacks -- pin them with "
                f"--default-{guessed[0]}-model if they are wrong for this backend."
            ]
            if guessed
            else [],
        )

        if primary and catalog.ok and catalog.models and not catalog.find(primary):
            strict = config.bool_("strict_model_check", False)
            self.report_step(
                result,
                "model check",
                Status.FAILED if strict else Status.PARTIAL,
                f"{primary} is not in the backend's catalogue",
                remediation=[
                    "Pick one of: " + ", ".join(catalog.qualified_ids()[:8]),
                    "Or set provision.strict_model_check = false to continue anyway.",
                ],
            )
        elif primary:
            # Both halves, because settings.json will say one and the models
            # step above says the other: "opus (claude-opus-5@20251101)".
            shown = f"{primary_setting} ({primary})" if primary_setting != primary else primary
            self.report_step(result, "primary model", Status.OK, shown)

        return {
            "primary": primary,
            "primary_setting": primary_setting,
            "primary_requested": bool(requested),
            "tiers": resolved,
            "inferred": inferred,
            "guessed": guessed,
        }

    # -- settings & environment -----------------------------------------------

    def _env_block(self, backend_cfg, profile, selection) -> dict[str, str]:
        """Assemble the settings.json ``env`` block from configured names only."""
        block: dict[str, str] = {str(k): str(v) for k, v in backend_cfg.dict_("env").items() if v not in (None, "")}
        names = backend_cfg.dict_("env_names")

        for field_name in ("region", "project", "base_url"):
            var = str(names.get(field_name, "") or "")
            value = profile.get(field_name) or profile.get("endpoint" if field_name == "base_url" else "")
            if var and value:
                block[var] = str(value)

        template = str(names.get("model_tier", "") or "")
        if template:
            for tier, model in selection["tiers"].items():
                block[template.replace("{TIER}", tier.upper())] = model

        return block

    def _login_env(self, backend_cfg) -> dict[str, str]:
        """The variables Claude Code reads to log in that phase 2 set, for the ``env`` block (``login_env``).

        Phase 2 kept a service-account key's path only in this program's own
        env.sh, which nothing sources: a ``claude`` started in a new terminal
        never saw ``GOOGLE_APPLICATION_CREDENTIALS`` and could not log in.
        Claude Code reads settings.json on its own, so what it needs to log in
        goes there -- a path or a setting, never a credential: a value shaped
        like one is left out and said so by the new-terminal check.
        """
        import os

        from ..mcp_auto_discovery.secret_policy import holds_a_credential, looks_opaque

        block: dict[str, str] = {}
        for name in backend_cfg.str_list("login_env", []):
            value = self.ctx.env.get(name) or os.environ.get(name, "")
            # By the value as well as the name: GOOGLE_APPLICATION_CREDENTIALS
            # names a location, and a token pasted into it is still a token.
            if value and not holds_a_credential(name, value) and not looks_opaque(value):
                block[name] = value
        return block

    def _login_settings(self, backend: str, document: Mapping[str, Any]) -> dict[str, Any]:
        """Settings Claude Code reads its credential through, for ``backend``: an ``apiKeyHelper``.

        An API key phase 2 resolved from a file, a keyring or a command was
        invisible to a ``claude`` started later: nothing told Claude Code where
        it lives. ``apiKeyHelper`` is a command Claude Code runs to get it -- it
        is written from the reference (``[provision.<backend>.key_helpers]``, by
        scheme), never from the value. A key in an exported variable needs
        none: Claude Code reads the variable itself. An ``apiKeyHelper`` the
        file already has is the operator's and is kept, unless
        ``provision.overwrite_existing_settings`` says otherwise.
        """
        import shlex
        import sys

        backend_section = self.settings.section(f"provision.{backend.replace('-', '_')}")
        templates = backend_section.dict_("key_helpers")
        if not templates:
            return {}
        if document.get("apiKeyHelper") and not self.settings.bool_("provision.overwrite_existing_settings", False):
            return {}
        helper = self.settings.str_("auth.anthropic_api.api_key_helper", "").strip()
        if not helper:
            secret = self.ctx.state.get("anthropic_api_key")
            source = str(getattr(secret, "source", "") or "")
            scheme, _, rest = source.partition(":")
            template = str(templates.get(scheme) or "")
            if not template or not rest:
                return {}
            service, _, user = rest.partition("/")
            helper = fill(
                template,
                {
                    "path": shlex.quote(rest),
                    "command": rest,
                    "service": shlex.quote(service),
                    "user": shlex.quote(user),
                    "python": shlex.quote(sys.executable),
                },
            )
        return {"apiKeyHelper": helper} if helper else {}

    def _managed_env_names(self, config, tiers) -> set[str]:
        """Every variable phase 3 is capable of writing, for any backend.

        Which is exactly the set it is entitled to take away again. A name in
        here that the *selected* backend does not want is not something the
        operator put there -- it is this phase's own output from an earlier run
        against a different backend, and leaving it is what makes
        ``--provision-backend anthropic-api`` produce a settings.json that
        still says ``CLAUDE_CODE_USE_VERTEX=1`` and routes to Vertex anyway.

        Derived from configuration rather than listed here, so a backend added
        to the TOML is covered without touching this file. Anything outside the
        set is never removed, whatever it looks like.
        """
        names: set[str] = set()
        for backend in config.str_list("backend_priority", ["vertex", "anthropic-api", "bedrock"]):
            cfg = self.settings.section(f"provision.{backend.replace('-', '_')}")
            names.update(str(key) for key in cfg.dict_("env"))
            names.update(str(name) for name in cfg.str_list("login_env", []))
            for var in cfg.dict_("env_names").values():
                var = str(var or "")
                if not var:
                    continue
                if "{TIER}" in var:
                    names.update(var.replace("{TIER}", tier.upper()) for tier in tiers)
                else:
                    names.add(var)
        return names

    def _stale_env_names(self, config, env_block, tiers) -> list[str]:
        """Managed variables the selected backend has no use for."""
        if not config.bool_("retire_stale_env", True):
            return []
        return sorted(self._managed_env_names(config, tiers) - set(env_block))

    def _plain_settings(self, config, document) -> dict[str, Any]:
        """The non-env keys from ``[provision.settings]``: theme, tui, permissions.

        Whatever is configured there is written as it stands, so a key added to
        the TOML reaches settings.json without a code change. ``model`` is
        handled by the caller, which has its own precedence rule for it, and
        ``extra`` is merged after this so it can override these.

        They are seeded rather than enforced. Claude Code writes this file too
        -- ``/theme`` and ``/permissions`` put the operator's own answer in it
        -- and a provisioning run that resets those on every invocation would
        be fighting the person using the machine. Only the leaf is compared, so
        a settings.json that already has a ``permissions.allow`` list still
        gets ``permissions.defaultMode``.
        """
        configured = self.settings.dict_("provision.settings")
        for handled in ("model", "extra"):
            configured.pop(handled, None)
        if config.bool_("overwrite_existing_settings", False):
            return configured
        return _unanswered(configured, document)

    def _write_settings(self, result, config, env_block, selection, stale, backend: str = "") -> None:
        path = settings_path(self.ctx)
        writer = SettingsWriter(self.ctx, path)
        try:
            document = writer.read()
        except Exception:  # noqa: BLE001 - apply() reports the same fault properly
            document = {}
        updates: dict[str, Any] = {"env": env_block}

        model = selection.get("primary_setting") or ""
        if model:
            # An inferred model must not silently replace a choice the user
            # already made in settings.json -- "opus" is a deliberate answer.
            current = str(document.get("model", "") or "")
            keep = (
                current and not selection["primary_requested"] and not config.bool_("overwrite_inferred_model", False)
            )
            if keep:
                self.report_step(
                    result,
                    "model in settings",
                    Status.SKIPPED,
                    f"kept {current} from {path.name}",
                    remediation=[
                        "Pass --default-model to change it, or set provision.overwrite_inferred_model = true."
                    ],
                )
            else:
                updates["model"] = model

        updates.update(self._plain_settings(config, document))
        if backend:
            updates.update(self._login_settings(backend, document))
        extra = self.settings.dict_("provision.settings.extra")
        if extra:
            updates.update(extra)

        outcome = writer.apply(updates, remove=[f"env.{name}" for name in stale])
        self.ctx.state["provision_settings"] = outcome

        if outcome.error:
            self.report_step(
                result,
                "settings.json",
                Status.FAILED,
                outcome.error,
                remediation=[f"Fix or move {path} and re-run phase 3."],
            )
            return
        if not outcome.changed:
            self.report_step(result, "settings.json", Status.OK, f"{path} already up to date")
            return

        verb = "would update" if outcome.dry_run else ("updated" if outcome.existed else "created")
        self.report_step(
            result,
            "settings.json",
            Status.OK if outcome.dry_run else Status.CHANGED,
            f"{verb} {path} ({len(outcome.changes)} key(s))",
            data=outcome.to_dict(),
        )
        for change in outcome.changes:
            self.console.bullet(change.describe(), indent=6)
        if outcome.backup:
            self.console.bullet(f"backup: {outcome.backup}", indent=6)

    def _fresh_terminal_check(self, result: PhaseResult, backend: str) -> None:
        """Start ``claude`` the way a new terminal would, and say whether it logs in on its own.

        Phase 2 proved each login by running ``claude`` inside this process --
        which holds every variable the run resolved, every value from env.sh
        and everything the operator's shell exported. A new terminal has none
        of that: a service-account key kept only in env.sh, an API key read
        from a file, a headless token that was printed and never stored all
        passed phase 2 and left the next ``claude`` asking to log in. So the
        same one-turn round trip runs once more with only
        ``provision.fresh_terminal.keep`` (HOME, PATH, ...) in its environment,
        from an empty directory, and what it lacks is named.
        """
        import os

        terminal = self.settings.section("provision.fresh_terminal")
        if not terminal.bool_("enabled", True):
            return
        if self.ctx.dry_run:
            self.report_step(result, "a new terminal", Status.SKIPPED, "dry run: claude is not started")
            return
        claude = self.ctx.tool("claude", config_key="paths.claude")
        if not claude:
            self.report_step(result, "a new terminal", Status.SKIPPED, "claude is not installed")
            return
        prompt = terminal.str_("prompt", "Reply with the single word: ok")
        argv = [
            fill(t, {"claude": claude, "prompt": prompt})
            for t in terminal.str_list(
                "command", ["{claude}", "-p", "--output-format", "json", "--max-turns", "1", "{prompt}"]
            )
        ]
        keep = set(terminal.str_list("keep", ["HOME", "USER", "LOGNAME", "PATH", "SHELL", "TERM", "LANG"]))
        env: dict[str, str | None] = {
            name: None for name in {*os.environ, *getattr(self.ctx.runner, "base_env", {})} if name not in keep
        }
        env.update({name: os.environ[name] for name in keep if name in os.environ})
        with diskspace.temporary_directory("ica-new-terminal-") as cwd:
            run = self.ctx.runner.run(
                argv,
                timeout=terminal.float_("timeout_seconds", 120),
                cwd=cwd,
                env=env,
                stdin_text="",
                mutating=False,
                check=False,
                label="claude in a new terminal",
            )
        payload = _cli_json(run.stdout)
        ok = run.ok and not (payload or {}).get("is_error", False)
        notes = self._first_start_notes()
        if ok:
            self.report_step(
                result,
                "a new terminal",
                Status.OK,
                f"claude started with only {', '.join(sorted(keep))} logs in on its own ({backend})"
                + (f"; {notes[0]}" if notes else ""),
                remediation=notes,
            )
            return
        said = (run.stderr or run.stdout or "").strip()[-300:]
        if any(
            m in said.lower()
            for m in terminal.str_list("temporary_markers", ["429", "rate limit", "quota", "overloaded"])
        ):
            self.report_step(
                result,
                "a new terminal",
                Status.PARTIAL,
                "inconclusive: the backend is rate limited or out of quota -- the login itself was accepted",
            )
            return
        lacking = sorted(
            name
            for name in terminal.str_list("login_names", [])
            if (os.environ.get(name) or self.ctx.env.get(name)) and name not in self._settings_env()
        )
        self.report_step(
            result,
            "a new terminal",
            Status.FAILED,
            "claude started in a new terminal does not log in on its own"
            + (f": it lacks {', '.join(lacking)}, which only this run had" if lacking else "")
            + (f" ({said})" if said else ""),
            remediation=[
                *(
                    [
                        f"Export {', '.join(lacking)} in your shell profile (e.g. ~/.bashrc), or keep each where "
                        "Claude Code reads it itself: its settings.json env block, an apiKeyHelper, its own login."
                    ]
                    if lacking
                    else []
                ),
                "Run `claude` in a new terminal to see what it asks for, then re-run phases 2 and 3.",
                *notes,
            ],
        )

    def _settings_env(self) -> dict[str, Any]:
        """The ``env`` block of the settings.json Claude Code reads, as written now."""
        try:
            return dict(SettingsWriter(self.ctx, settings_path(self.ctx)).read().get("env") or {})
        except Exception:  # noqa: BLE001 - unreadable reads as empty; the write step reported it
            return {}

    def _first_start_notes(self) -> list[str]:
        """Prompts the first interactive ``claude`` shows that are not a login -- said, so they are no surprise."""
        import json
        import os
        from pathlib import Path

        if not self.settings.bool_("provision.fresh_terminal.first_start_notes", True):
            return []
        base = os.environ.get("CLAUDE_CONFIG_DIR") or str(Path.home())
        try:
            state = json.loads((Path(base) / ".claude.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = {}
        if state.get("hasCompletedOnboarding"):
            return []
        return [
            "The first interactive `claude` asks for a theme, and whether to trust the folder it starts in. "
            "Neither is a login: answer once and they are not asked again."
        ]

    def _export_env(self, result, config, backend, profile, selection, env_block, stale) -> None:
        if not config.bool_("export_env", True):
            self.report_step(result, "environment", Status.SKIPPED, "export_env = false")
            return

        # Names the downstream tools read must be exported verbatim.
        for name, value in env_block.items():
            self.ctx.env.set(name, value, passthrough=True)
        # The same retraction settings.json gets: a shell that sourced env.sh
        # after an earlier run would otherwise keep the old backend alive in
        # every terminal it is still open in.
        dropped = [name for name in stale if self.ctx.env.unset(name)]
        # initialize-claude-ai's own bookkeeping stays under its prefix.
        self.ctx.env.set("BACKEND", backend)
        for key in ("project", "region", "account", "endpoint"):
            if profile.get(key):
                self.ctx.env.set(key.upper(), profile[key])
        # The tier, not the id: this is read back as provision.settings.model on
        # the next run, and what that writes into settings.json is a tier.
        if selection.get("primary_setting") or selection["primary"]:
            self.ctx.env.set("MODEL", selection.get("primary_setting") or selection["primary"])

        written = self.ctx.env.save()
        count = f"{len(env_block)} exported variable(s)"
        if dropped:
            count += f", {len(dropped)} retired"
        detail = (
            f"{count}; {self.ctx.env.shell_hint()}" if written or self.ctx.dry_run else f"{count} (unchanged on disk)"
        )
        self.report_step(
            result,
            "environment",
            Status.CHANGED if written else Status.OK,
            detail,
            data={"variables": sorted(env_block), "retired": dropped},
        )

    # -- inventory -------------------------------------------------------------

    def _inventory(self, result, backend, profile, catalog, selection) -> None:
        settings_outcome = self.ctx.state.get("provision_settings")
        settings_summary: dict[str, Any] = {}
        if settings_outcome is not None:
            settings_summary = {
                "path": str(settings_outcome.path),
                "backup": str(settings_outcome.backup) if settings_outcome.backup else "",
                "changes": [c.describe() for c in settings_outcome.changes],
            }

        collector = inventory_mod.InventoryCollector(self.ctx)
        inv = collector.collect(
            backend={
                "backend": backend,
                "model": selection["primary"],
                **{k: v for k, v in profile.items() if v},
            },
            models={
                "tiers": selection["tiers"],
                "available": catalog.qualified_ids(),
                "endpoint": catalog.endpoint,
                "detail": catalog.detail,
            },
            settings=settings_summary,
        )
        if self.ctx.dry_run:
            inv.notes.append("dry run: nothing on this machine was modified")

        inventory_mod.render(self.console, inv)

        # Phase 4 and the JSON report both consume this.
        self.ctx.state["inventory"] = inv
        self.ctx.state["provision_backend_active"] = backend
        result.data["inventory"] = inv.to_dict()
        result.add("inventory", Status.OK, "environment listed above")


def _unanswered(wanted: Mapping[str, Any], present: Mapping[str, Any]) -> dict[str, Any]:
    """``wanted`` reduced to the leaves ``present`` has no answer for yet.

    Nested tables are walked rather than compared whole: the question
    ``permissions.defaultMode`` asks is not answered by a ``permissions`` table
    that only lists allowed tools.
    """
    out: dict[str, Any] = {}
    for key, value in wanted.items():
        existing = present.get(key)
        if isinstance(value, Mapping):
            nested = _unanswered(value, existing if isinstance(existing, Mapping) else {})
            if nested:
                out[key] = nested
        elif existing is None or existing == "":
            out[key] = value
    return out


def _cli_json(text: str) -> dict | None:
    """The JSON object ``claude -p --output-format json`` printed, tolerating other lines; None without one."""
    import json

    for line in reversed((text or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                payload = json.loads(line)
            except ValueError:
                continue
            if isinstance(payload, dict):
                return payload
    return None
