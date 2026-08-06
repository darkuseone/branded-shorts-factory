"""Data chips — numeric callouts from narration."""

from __future__ import annotations

from shorts_factory.render.data_chips import extract_chips
from shorts_factory.render.timeline import build_timeline
from shorts_factory.spec import parse_spec

from .conftest import minimal_document


def test_extract_chips_finds_numbers_outside_hook():
    document = minimal_document(
        duration_target=30,
        hook={"id": "hook", "text": "Горячий вопрос про Венеру.", "start": 0, "duration": 2.5},
        script=[
            {
                "id": "s1",
                "text": "Давление у поверхности — девяносто земных атмосфер.",
                "start": 2.5,
                "duration": 6,
            },
            {
                "id": "s2",
                "text": "Температура — четыреста семьдесят градусов.",
                "start": 8.5,
                "duration": 6,
            },
        ],
        visuals=[
            {"id": "v1", "type": "footage", "query": "venus clouds", "start": 0, "duration": 15},
            {"id": "v2", "type": "footage", "query": "venus surface", "start": 15, "duration": 15},
        ],
        cta={"text": "Подпишись", "start": 26, "duration": 3},
    )
    spec, _ = parse_spec(document)
    chips = extract_chips(spec)
    assert chips
    assert all(chip.start >= 2.5 for chip in chips)
    assert len(chips) <= 3


def test_timeline_includes_data_chip_elements(minimal_spec):
    # Force a number into a body segment.
    minimal_spec.script[0].text = "Там почти 90 атмосфер давления."
    timeline = build_timeline(minimal_spec, [])
    chips = [el for el in timeline.elements if el.props.get("role") == "data_chip"]
    assert chips
    assert chips[0].kind == "text"
