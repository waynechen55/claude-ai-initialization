"""Phase 3 says which models the credential can reach, and where the rest are.

The catalogue was already being fetched -- to choose a model, and to check the
chosen one against the backend -- and then went into the JSON report and nowhere
else. So the phase ended without the operator ever being shown what they could
use, and "which models do I have?" had no answer on screen.

One thing this deliberately does *not* claim. A publisher catalogue lists what
the provider publishes, which is not the same as what a given project is
entitled to call. A real run reported::

    model discovery  12 model(s) offered by global
    primary model    opus (claude-opus-5-5)

and every phase-4 call to that model then returned *"The model claude-opus-5-5
is not available on your vertex deployment"*. The catalogue was right about
publication and wrong about entitlement, so the step says "offered" and warns
that only a call settles it.
"""

from __future__ import annotations

import pytest

from initialize_claude_ai.core.report import PhaseResult, Status
from initialize_claude_ai.phases.phase3_provision import ProvisionPhase
from initialize_claude_ai.provision.models import ModelCatalog, ModelInfo


def catalogue(*ids: str) -> ModelCatalog:
    catalog = ModelCatalog(backend="vertex", ok=bool(ids))
    catalog.models = [ModelInfo(id=i) for i in ids]
    return catalog


def report(ctx, catalog, backend="vertex", profile=None, primary="claude-opus-5"):
    phase = ProvisionPhase(ctx)
    result = PhaseResult(phase="provision", title="Provision")
    phase._report_available_models(
        result,
        backend,
        profile if profile is not None else {"project": "p1", "region": "us-east5"},
        catalog,
        {"primary": primary},
    )
    return result


def step(result, name="available models"):
    return next((s for s in result.steps if s.name == name), None)


# --------------------------------------------------------------------------- #
# the listing
# --------------------------------------------------------------------------- #


def test_the_models_are_named_on_screen(ctx) -> None:
    found = step(report(ctx, catalogue("claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5")))
    assert found is not None and found.status == Status.OK
    advice = " ".join(found.remediation)
    assert "claude-opus-5" in advice
    assert "claude-sonnet-5" in advice


def test_they_are_grouped_by_tier(ctx) -> None:
    """A flat list of twenty ids is a wall; the tier is how people choose."""
    found = step(report(ctx, catalogue("claude-opus-5", "claude-sonnet-5")))
    advice = " ".join(found.remediation)
    assert "opus:" in advice and "sonnet:" in advice


def test_a_long_tier_is_capped_and_says_how_many_more(ctx) -> None:
    ctx.settings.set("provision.catalogue.per_tier", 2)
    many = [f"claude-opus-{n}" for n in range(6)]
    advice = " ".join(step(report(ctx, catalogue(*many))).remediation)
    assert "+4 more" in advice


def test_the_count_and_the_chosen_model_are_in_the_headline(ctx) -> None:
    found = step(report(ctx, catalogue("claude-opus-5", "claude-sonnet-5")))
    assert "2 model(s) offered" in found.detail
    assert "claude-opus-5" in found.detail


# --------------------------------------------------------------------------- #
# where the rest are
# --------------------------------------------------------------------------- #


def test_the_full_list_is_pointed_at(ctx) -> None:
    """Capping the display is only acceptable if the rest is one link away."""
    advice = " ".join(step(report(ctx, catalogue("claude-opus-5"))).remediation)
    assert "The full list for vertex:" in advice
    assert "To print it yourself:" in advice


def test_the_pointer_is_filled_in_from_the_resolved_profile(ctx) -> None:
    advice = " ".join(step(report(ctx, catalogue("claude-opus-5"))).remediation)
    assert "p1" in advice, "the project the run actually used"
    assert "us-east5" in advice


@pytest.mark.parametrize("backend", ["vertex", "anthropic-api", "bedrock"])
def test_every_backend_has_somewhere_to_point(ctx, backend: str) -> None:
    """Including the one with no discovery route at all."""
    phase = ProvisionPhase(ctx)
    docs, command = phase._catalogue_pointer(backend, {"project": "p", "region": "r"})
    assert docs, f"{backend} has no documented list"
    assert command, f"{backend} has no command the operator can run"


def test_a_backend_nobody_configured_is_simply_quiet(ctx) -> None:
    """An unknown backend yields no pointer rather than an invented one."""
    phase = ProvisionPhase(ctx)
    assert phase._catalogue_pointer("some-new-cloud", {}) == ("", "")


# --------------------------------------------------------------------------- #
# when there is no catalogue -- which is when the pointer matters most
# --------------------------------------------------------------------------- #


def test_an_empty_catalogue_still_says_where_to_look(ctx) -> None:
    """Bedrock has no discovery route here, and an offline host has none either.

    "I could not list them" is half an answer; the other half is where to look,
    and it is the same half either way.
    """
    empty = ModelCatalog(backend="bedrock")
    empty.detail = "model discovery is not implemented for backend 'bedrock'"
    found = step(report(ctx, empty, backend="bedrock"))

    assert found.status == Status.PARTIAL, "not a failure of the operator's"
    advice = " ".join(found.remediation)
    assert "The full list for bedrock:" in advice
    assert "aws bedrock list-foundation-models" in advice


def test_it_does_not_claim_the_operator_has_no_models(ctx) -> None:
    found = step(report(ctx, ModelCatalog(backend="bedrock"), backend="bedrock"))
    assert "could not list" in found.detail or "not implemented" in found.detail


# --------------------------------------------------------------------------- #
# what it is careful not to claim
# --------------------------------------------------------------------------- #


def test_offered_is_not_promised(ctx) -> None:
    """The distinction a real run turned on.

    A catalogue of 12 named claude-opus-5-5; the deployment then refused every
    call to it. The word "offered" and the warning are the whole of the
    honesty available without making a billable call.
    """
    found = step(report(ctx, catalogue("claude-opus-5")))
    assert "offered" in found.detail
    assert "available to" not in found.detail
    assert any("not proof of entitlement" in line for line in found.remediation)


def test_the_machine_readable_report_carries_the_whole_list(ctx) -> None:
    """The screen is capped; the JSON report is not, and tools read that."""
    many = [f"claude-opus-{n}" for n in range(9)]
    found = step(report(ctx, catalogue(*many)))
    assert len(found.data["all"]) == 9
    assert found.data["docs_url"] and found.data["list_command"]


def test_it_can_be_switched_off(ctx) -> None:
    ctx.settings.set("provision.catalogue.show", False)
    assert step(report(ctx, catalogue("claude-opus-5"))) is None
