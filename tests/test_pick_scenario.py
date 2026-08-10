"""Scenario picking (`scripts/pick_scenario.py`).

Every failure here shows up in Actions as a run that died before it installed
anything, which reads like a crash. That is why the rules are tested.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "pick_scenario.py"
_spec = importlib.util.spec_from_file_location("pick_scenario", MODULE_PATH)
pick = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(pick)


@pytest.fixture
def jobs(tmp_path, monkeypatch):
    """A jobs/ tree with two scenarios, one of them renderable."""
    root = tmp_path
    (root / "jobs").mkdir()

    def add(name: str, *, avatar: bool) -> None:
        (root / "jobs" / f"{name}.json").write_text(json.dumps({"id": name}), encoding="utf-8")
        if avatar:
            folder = root / "jobs" / name
            folder.mkdir(exist_ok=True)
            (folder / "avatar.mp4").write_bytes(b"\x00" * 8192)

    monkeypatch.setattr(pick, "ROOT", root)
    monkeypatch.setattr(pick, "JOBS", root / "jobs")
    return add


def test_an_explicit_spec_is_never_second_guessed(jobs, tmp_path, monkeypatch):
    jobs("alpha", avatar=True)
    jobs("beta", avatar=True)
    monkeypatch.setattr(pick, "changed_paths", lambda *a: ["jobs/alpha.json"])
    path, name = pick.resolve(spec="jobs/beta.json")
    assert name == "beta"
    assert path == tmp_path / "jobs" / "beta.json"


def test_a_missing_explicit_spec_fails_loudly(jobs):
    jobs("alpha", avatar=True)
    with pytest.raises(SystemExit, match="not found"):
        pick.resolve(spec="jobs/nope.json")


def test_the_scenario_touched_by_the_push_is_chosen(jobs, monkeypatch):
    jobs("alpha", avatar=True)
    jobs("beta", avatar=True)
    monkeypatch.setattr(pick, "changed_paths", lambda *a: ["jobs/beta.json", "README.md"])
    assert pick.resolve()[1] == "beta"


def test_a_touched_job_folder_counts_as_touching_its_scenario(jobs, monkeypatch):
    jobs("alpha", avatar=True)
    monkeypatch.setattr(pick, "changed_paths", lambda *a: ["jobs/alpha/broll/v1.mp4"])
    assert pick.resolve()[1] == "alpha"


def test_a_deletion_still_identifies_the_job(jobs, monkeypatch):
    """The old shell filtered deletions out, so this push found nothing."""
    jobs("alpha", avatar=True)
    monkeypatch.setattr(pick, "changed_paths", lambda *a: ["jobs/alpha/broll/gone.mp4"])
    assert pick.resolve()[1] == "alpha"


def test_a_push_touching_two_jobs_prefers_the_renderable_one(jobs, monkeypatch):
    jobs("alpha", avatar=False)
    jobs("beta", avatar=True)
    monkeypatch.setattr(pick, "changed_paths", lambda *a: ["jobs/alpha.json", "jobs/beta.json"])
    assert pick.resolve()[1] == "beta"


def test_a_lone_scenario_needs_no_disambiguation(jobs, monkeypatch):
    jobs("alpha", avatar=True)
    monkeypatch.setattr(pick, "changed_paths", lambda *a: ["src/whatever.py"])
    assert pick.resolve()[1] == "alpha"


def test_several_scenarios_and_no_signal_asks_for_an_explicit_choice(jobs, monkeypatch):
    jobs("alpha", avatar=True)
    jobs("beta", avatar=True)
    monkeypatch.setattr(pick, "changed_paths", lambda *a: ["src/whatever.py"])
    with pytest.raises(SystemExit, match="explicit spec"):
        pick.resolve()


def test_an_empty_jobs_tree_says_so(jobs, monkeypatch):
    monkeypatch.setattr(pick, "changed_paths", lambda *a: [])
    with pytest.raises(SystemExit, match="no scenarios"):
        pick.resolve()


def test_an_avatar_stub_does_not_count_as_an_avatar(jobs, tmp_path):
    """An LFS pointer is ~130 bytes and would otherwise pass as a clip."""
    jobs("alpha", avatar=True)
    (tmp_path / "jobs" / "alpha" / "avatar.mp4").write_bytes(b"version https://git-lfs...")
    assert pick.avatar_for("alpha") is None


def test_the_zero_sha_of_a_new_branch_is_not_a_commit():
    assert not pick._commit_exists("0" * 40)
    assert not pick._commit_exists("")
