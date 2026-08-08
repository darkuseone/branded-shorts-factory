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
        "hook": {
            "id": "hook",
            "text": "Telescopes found something impossible.",
            "start": 0,
            "duration": 4.0,
            "mode": "full_host",
        },
        "script": [
            {
                "id": "s1",
                "text": "The first spoken beat carries the whole setup for this short video.",
                "start": 4.0,
                "duration": 5.0,
                "mode": "split",
            },
            {
                "id": "s2",
                "text": "Only footage now while the claim lands hard on screen.",
                "start": 9.0,
                "duration": 5.0,
                "mode": "full_footage",
            },
            {
                "id": "s3",
                "text": "Back with the host for the closing beat and the takeaway.",
                "start": 14.0,
                "duration": 5.0,
                "mode": "split",
            },
        ],
        "visuals": [
            {
                "id": "v1",
                "type": "footage",
                "query": "space telescope",
                "keywords": ["observatory night sky"],
                "start": 0,
                "duration": 2.0,
                "segment_ref": "hook",
                "position": "fullscreen",
            },
            {
                "id": "v2",
                "type": "footage",
                "query": "telescope dish",
                "start": 2.0,
                "duration": 2.0,
                "segment_ref": "hook",
                "position": "fullscreen",
            },
            {
                "id": "v3",
                "type": "footage",
                "query": "galaxy",
                "start": 4.0,
                "duration": 2.5,
                "segment_ref": "s1",
                "position": "auto",
            },
            {
                "id": "v4",
                "type": "footage",
                "query": "spiral arms",
                "start": 6.5,
                "duration": 2.5,
                "segment_ref": "s1",
                "position": "auto",
            },
            {
                "id": "v5",
                "type": "footage",
                "query": "nebula",
                "start": 9.0,
                "duration": 2.5,
                "segment_ref": "s2",
                "position": "fullscreen",
            },
            {
                "id": "v6",
                "type": "footage",
                "query": "deep space",
                "start": 11.5,
                "duration": 2.5,
                "segment_ref": "s2",
                "position": "fullscreen",
            },
            {
                "id": "v7",
                "type": "footage",
                "query": "stars",
                "start": 14.0,
                "duration": 3.0,
                "segment_ref": "s3",
                "position": "auto",
            },
            {
                "id": "v8",
                "type": "footage",
                "query": "milky way",
                "start": 17.0,
                "duration": 3.0,
                "segment_ref": "s3",
                "position": "auto",
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
