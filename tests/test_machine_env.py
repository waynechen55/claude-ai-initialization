"""One writer brings a per-machine variable to the same end state in every place Claude Code reads.

openqa-example-net failed on another machine with UNABLE_TO_GET_ISSUER_CERT_LOCALLY:
the trust it needed reached nowhere Claude Code reads, and ~/.bashrc and
~/.profile both exported a NODE_EXTRA_CA_CERTS naming a deleted /tmp file.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from initialize_claude_ai.core import machine_env as m
from initialize_claude_ai.core.machine_env import MachineEnv

VAR = "NODE_EXTRA_CA_CERTS"


@pytest.fixture
def writer(ctx, tmp_path):
    ctx.dry_run = False
    ctx.settings.set("machine_env.enabled", True)
    ctx.settings.set(
        "machine_env.places",
        ["settings", str(tmp_path / "bashrc"), str(tmp_path / "profile"), str(tmp_path / "profile.d" / "ica.sh")],
    )
    ctx.settings.set("machine_env.places_if_present", [])
    ctx.settings.set("machine_env.scan", [])  # never the real startup files
    ctx.settings.set("machine_env.check_effective", False)
    return MachineEnv(ctx)


def _bundle(tmp_path: Path) -> str:
    bundle = tmp_path / "trust" / "claude-code-ca.pem"
    bundle.parent.mkdir(parents=True, exist_ok=True)
    bundle.write_text("-----BEGIN CERTIFICATE-----\nx\n-----END CERTIFICATE-----\n")
    return str(bundle)


def test_every_line_state_reaches_the_same_end(writer, ctx, tmp_path) -> None:
    value = _bundle(tmp_path)
    (tmp_path / "bashrc").write_text(
        '# Added by Some CLI installer\nexport NODE_EXTRA_CA_CERTS="/tmp/tmp3ynfawgm/ca-bundle.pem"\n'
    )
    (tmp_path / "profile").write_text(f"# export NODE_EXTRA_CA_CERTS={value}\n")
    missing = lambda v: "" if os.path.isfile(v) else "a file that does not exist"  # noqa: E731
    record = writer.apply(VAR, value, "lets Claude Code trust an internal root", valid=missing)
    by_place = {p.place: p for p in record.places}
    bashrc = by_place[str(tmp_path / "bashrc")]
    assert bashrc.outcome == m.CORRECTED and bashrc.was.startswith("/tmp/") and "does not exist" in bashrc.why_wrong
    assert "another tool" in bashrc.owner, "another tool's line is corrected in place, and said so"
    assert by_place[str(tmp_path / "profile")].outcome == m.UNCOMMENTED
    assert by_place[str(tmp_path / "profile.d" / "ica.sh")].outcome == m.ADDED
    assert next(p for p in record.places if p.place.endswith("(env)")).outcome == m.ADDED
    for name in ("bashrc", "profile", "profile.d/ica.sh"):
        assert f"export {VAR}={value}" in (tmp_path / name).read_text()
    assert json.loads((ctx.claude_home / "settings.json").read_text())["env"][VAR] == value
    assert list(tmp_path.glob("bashrc.*.bak")), "a file is backed up before it changes"


def test_a_second_run_recognises_every_place(writer, tmp_path) -> None:
    value = _bundle(tmp_path)
    writer.apply(VAR, value, "why")
    again = writer.apply(VAR, value, "why")
    assert {p.outcome for p in again.places} == {m.ALREADY_CORRECT}
    assert (tmp_path / "bashrc").read_text().count(f"export {VAR}=") == 1, "never appended twice"


def test_the_last_assignment_is_the_one_corrected(writer, tmp_path) -> None:
    value = _bundle(tmp_path)
    (tmp_path / "bashrc").write_text(f"export {VAR}={value}\nexport {VAR}=/tmp/gone.pem\n")
    record = writer.apply(VAR, value, "why")
    first = record.places[1]
    assert first.outcome == m.CORRECTED and first.line == 2


def test_a_place_it_may_not_write_is_a_step_with_the_line(writer, ctx, tmp_path, monkeypatch) -> None:
    locked = tmp_path / "locked"
    locked.mkdir()
    ctx.settings.set("machine_env.places", [str(locked / "ica.sh")])
    real = os.access
    monkeypatch.setattr(m.os, "access", lambda p, mode: False if str(p).startswith(str(locked)) else real(p, mode))
    record = writer.apply(VAR, "/x.pem", "why")
    assert record.places[0].outcome == m.NOT_PERMITTED and f"export {VAR}=/x.pem" in record.places[0].detail
    assert not (locked / "ica.sh").exists()


def test_the_effective_value_is_asked_of_fresh_shells(writer, ctx, tmp_path, monkeypatch) -> None:
    value = _bundle(tmp_path)
    ctx.settings.set("machine_env.check_effective", True)
    later = tmp_path / "later.sh"
    later.write_text(f"export {VAR}=/tmp/overridden.pem\n")
    ctx.settings.set(
        "machine_env.effective_shells",
        {
            "a shell reading the profile": ["bash", "-c", f". {tmp_path / 'profile'}; {{probe}}"],
            "a shell reading something later": ["bash", "-c", f". {tmp_path / 'profile'}; . {later}; {{probe}}"],
        },
    )
    record = writer.apply(VAR, value, "why")
    assert record.shells["a shell reading the profile"] == value
    assert record.shells_wrong == ["a shell reading something later"] and not record.ok
    assert any("NOT as intended" in line for line in record.lines())


def test_a_dry_run_writes_nothing(writer, ctx, tmp_path) -> None:
    ctx.dry_run = True
    record = writer.apply(VAR, "/x.pem", "why")
    assert {p.outcome for p in record.places} == {m.WOULD_CHANGE} and not (tmp_path / "bashrc").exists()


def test_switched_off_it_touches_nothing(writer, ctx, tmp_path) -> None:
    ctx.settings.set("machine_env.enabled", False)
    assert writer.apply(VAR, "/x.pem", "why").places == [] and not (tmp_path / "bashrc").exists()


def test_packaged_settings_equal_the_code(settings) -> None:
    s = settings.section("machine_env")
    assert list(s.str_list("places", [])) == list(m.PLACES)
    assert list(s.str_list("places_if_present", [])) == list(m.PLACES_IF_PRESENT)
    assert s.str_("block_begin") == m.BLOCK_BEGIN and s.str_("block_end") == m.BLOCK_END
    assert s.dict_("effective_shells", {}) == m.EFFECTIVE_SHELLS


def test_a_startup_file_that_already_sets_it_is_found_and_corrected(writer, ctx, tmp_path) -> None:
    """Another installer's /etc/profile.d/node_extra_ca_certs.sh exported a deleted /tmp bundle."""
    value = _bundle(tmp_path)
    profile_d = tmp_path / "profile.d.real"
    profile_d.mkdir()
    theirs = profile_d / "node_extra_ca_certs.sh"
    theirs.write_text(
        '#!/bin/sh\n# Added by Some CLI installer\nexport NODE_EXTRA_CA_CERTS="/tmp/tmp6b2_e87n/ca-bundle.pem"\n'
    )
    (profile_d / "unrelated.sh").write_text("export OTHER=1\n")
    ctx.settings.set("machine_env.scan", [str(profile_d / "*.sh")])
    places = writer.places(VAR)
    assert str(theirs) in places and str(profile_d / "unrelated.sh") not in places
    record = writer.apply(VAR, value, "why")
    assert next(p for p in record.places if p.place == str(theirs)).outcome == m.CORRECTED
    assert f"export {VAR}={value}" in theirs.read_text() and "/tmp/tmp6b2_e87n" not in theirs.read_text()


def test_a_file_is_backed_up_once_per_run_and_never_one_this_run_created(writer, tmp_path) -> None:
    value = _bundle(tmp_path)
    (tmp_path / "bashrc").write_text("# mine\n")
    for variable in ("NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"):
        writer.apply(variable, value, "why")
    assert len(list(tmp_path.glob("bashrc.*.bak"))) == 1
    assert not list((tmp_path / "profile.d").glob("*.bak")), "a file this run created has nothing to keep"
