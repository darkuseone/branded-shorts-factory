"""Data chips — numeric callouts from narration."""

from __future__ import annotations

from shorts_factory.render.data_chips import extract_chips
from shorts_factory.render.timeline import build_timeline
from shorts_factory.spec import parse_spec

from .conftest import minimal_document


def test_extract_chips_finds_numbers_outside_hook():
    document = minimal_document(
        duration_target=20,
        hook={
            "id": "hook",
            "text": "Горячий вопрос про Венеру.",
            "start": 0,
            "duration": 4.0,
            "mode": "full_host",
        },
        script=[
            {
                "id": "s1",
                "text": "Давление у поверхности — девяносто земных атмосфер.",
                "start": 4.0,
                "duration": 5.0,
                "mode": "split",
            },
            {
                "id": "s2",
                "text": "Температура — четыреста семьдесят градусов.",
                "start": 9.0,
                "duration": 5.0,
                "mode": "full_footage",
            },
            {
                "id": "s3",
                "text": "И это только начало истории планеты.",
                "start": 14.0,
                "duration": 5.0,
                "mode": "split",
            },
        ],
        visuals=[
            {"id": "v1", "type": "footage", "query": "venus clouds", "start": 0, "duration": 2.0, "position": "auto"},
            {"id": "v2", "type": "footage", "query": "venus orbit", "start": 2.0, "duration": 2.0, "position": "auto"},
            {"id": "v3", "type": "footage", "query": "pressure gauge", "start": 4.0, "duration": 2.5, "position": "auto"},
            {"id": "v4", "type": "footage", "query": "deep ocean", "start": 6.5, "duration": 2.5, "position": "auto"},
            {"id": "v5", "type": "footage", "query": "heat haze", "start": 9.0, "duration": 2.5, "position": "fullscreen"},
            {"id": "v6", "type": "footage", "query": "molten lead", "start": 11.5, "duration": 2.5, "position": "fullscreen"},
            {"id": "v7", "type": "footage", "query": "venus surface", "start": 14.0, "duration": 3.0, "position": "auto"},
            {"id": "v8", "type": "footage", "query": "greenhouse effect", "start": 17.0, "duration": 3.0, "position": "auto"},
        ],
        cta={"text": "Подпишись", "start": 17, "duration": 2.5},
    )
    spec, _ = parse_spec(document)
    chips = extract_chips(spec)
    assert chips
    assert all(chip.start >= 4.0 for chip in chips)
    assert len(chips) <= 3


def test_timeline_includes_data_chip_elements(minimal_spec):
    # Force a number into a body segment.
    minimal_spec.script[0].text = "Там почти 90 атмосфер давления."
    timeline = build_timeline(minimal_spec, [])
    chips = [el for el in timeline.elements if el.props.get("role") == "data_chip"]
    assert chips
    assert chips[0].kind == "text"
