from __future__ import annotations

import json
from pathlib import Path

import pytest

from shorts_factory.config import Budgets, Paths, Settings
from shorts_factory.spec import Spec, parse_spec

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "venus-hell.json"


def minimal_document(**overrides) -> dict:
    """The smallest scenario that validates, as a starting point for tests."""
    document = {
        "id": "test-short",
        "title": "Test short",
        "duration_target": 20,
        "hook": {"id": "hook", "text": "Telescopes found something impossible.", "start": 0, "duration": 2.5},
        "script": [
            {
                "id": "s1",
                "text": "The first spoken beat carries the whole setup for this short video.",
                "start": 2.5,
                "duration": 8.0,
            },
            {
                "id": "s2",
                "text": "The second beat closes it out and hands the viewer a reason to stay.",
                "start": 10.5,
                "duration": 9.0,
            },
        ],
        "visuals": [
            {
                "id": "v1",
                "type": "footage",
                "query": "space telescope",
                "keywords": ["observatory night sky"],
                "start": 0,
                "duration": 10.0,
                "segment_ref": "hook",
            },
            {
                "id": "v2",
                "type": "footage",
                "query": "galaxy",
                "start": 10.0,
                "duration": 10.0,
                "segment_ref": "s2",
            },
        ],
    }
    document.update(overrides)
    return document


@pytest.fixture
def minimal_spec() -> Spec:
    spec, _ = parse_spec(minimal_document())
    return spec


@pytest.fixture
def example_spec() -> Spec:
    spec, _ = parse_spec(json.loads(EXAMPLE.read_text(encoding="utf-8")), source=str(EXAMPLE))
    return spec


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Offline settings pointed at a temporary workspace."""
    return Settings(
        paths=Paths(root=REPO_ROOT, workdir=tmp_path / "build").ensure(),
        budgets=Budgets(magnific_tokens=6, magnific_reserve=2, magnific_max_per_visual=2),
        offline=True,
        dry_run=True,
    )
