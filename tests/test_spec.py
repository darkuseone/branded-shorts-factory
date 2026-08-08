from __future__ import annotations

import json

import pytest

from shorts_factory.errors import SpecError
from shorts_factory.spec import load_spec, parse_spec

from .conftest import EXAMPLE, minimal_document


def test_example_scenario_is_valid_without_warnings():
    spec, issues = load_spec(EXAMPLE)
    errors = [issue for issue in issues if issue.level == "error"]
    warnings = [issue for issue in issues if issue.level == "warning"]
    assert errors == []
    assert warnings == [], f"example should be exemplary, got: {[str(w) for w in warnings]}"
    assert spec.id == "venus-hell"
    assert len(spec.visuals) >= 8
    assert spec.coverage() == pytest.approx(1.0, abs=0.01)


def test_missing_required_fields_are_reported_together():
    with pytest.raises(SpecError) as excinfo:
        parse_spec({"title": "no id and no duration"})
    paths = {issue.path for issue in excinfo.value.issues}
    assert "$.id" in paths
    assert "$.duration_target" in paths
    assert "script" in paths


def test_visual_past_duration_target_is_an_error():
    document = minimal_document()
    document["visuals"][1]["duration"] = 40.0
    with pytest.raises(SpecError) as excinfo:
        parse_spec(document)
    assert any("past duration_target" in issue.message for issue in excinfo.value.issues)


def test_unknown_segment_reference_is_an_error():
    document = minimal_document()
    document["visuals"][0]["segment_ref"] = "does-not-exist"
    with pytest.raises(SpecError) as excinfo:
        parse_spec(document)
    assert any(issue.path.endswith("segment_ref") for issue in excinfo.value.issues)


def test_duplicate_visual_ids_are_rejected():
    document = minimal_document()
    document["visuals"][1]["id"] = "v1"
    with pytest.raises(SpecError):
        parse_spec(document)


def test_stability_outside_the_shorts_window_warns_but_passes():
    document = minimal_document(voice_settings={"voice_id": "abc", "stability": 90})
    spec, issues = parse_spec(document)
    assert spec.voice.stability == 90
    assert any("stability" in issue.path for issue in issues if issue.level == "warning")


def test_stability_accepts_the_zero_to_one_convention():
    spec, _ = parse_spec(minimal_document(voice_settings={"voice_id": "abc", "stability": 0.47}))
    assert spec.voice.stability == pytest.approx(47.0)


def test_plain_string_script_is_split_and_timed():
    document = minimal_document()
    document["script"] = "First sentence here. Second sentence follows it."
    document.pop("hook")
    document["visuals"] = [{"id": "v1", "type": "footage", "query": "space", "start": 0, "duration": 6.0}]
    document["duration_target"] = 8
    spec, issues = parse_spec(document)
    assert len(spec.script) == 2
    assert spec.script[1].start > 0
    assert any(issue.level == "warning" and issue.path == "script" for issue in issues)


def test_hook_shifts_the_body_when_they_overlap():
    document = minimal_document()
    document["duration_target"] = 24
    document["script"][0]["start"] = 1.0
    document["script"][1]["start"] = 9.0
    document["visuals"][-1]["start"] = 20.0
    document["visuals"][-1]["duration"] = 3.5
    spec, issues = parse_spec(document)
    assert spec.hook is not None
    assert spec.script[0].start == pytest.approx(spec.hook.end)
    assert any("shifted" in issue.message for issue in issues)


def test_context_for_visual_prefers_the_referenced_segment(minimal_spec):
    visual = minimal_spec.visuals[0]
    assert visual.segment_ref == "hook"
    assert minimal_spec.context_for(visual) == minimal_spec.hook.text


def test_context_for_visual_falls_back_to_overlapping_narration(minimal_spec):
    visual = minimal_spec.visuals[0]
    visual.segment_ref = None
    context = minimal_spec.context_for(visual)
    # v1 spans 0-2s — hook only.
    assert minimal_spec.hook.text in context
    assert minimal_spec.script[1].text not in context


def test_coverage_and_gaps(minimal_spec):
    assert minimal_spec.coverage() == pytest.approx(1.0)
    assert minimal_spec.gaps() == []


def test_gaps_are_reported_for_partial_coverage():
    document = minimal_document()
    document["visuals"] = [{"id": "v1", "type": "footage", "query": "space", "start": 0, "duration": 5.0}]
    spec, issues = parse_spec(document)
    assert spec.gaps() == [(5.0, 20.0)]
    assert any("cover" in issue.message for issue in issues if issue.level == "warning")


def test_invalid_json_file_reports_position(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text("{ not json", encoding="utf-8")
    with pytest.raises(SpecError) as excinfo:
        load_spec(path)
    assert "invalid JSON" in str(excinfo.value)


def test_missing_file_is_a_spec_error(tmp_path):
    with pytest.raises(SpecError):
        load_spec(tmp_path / "nope.json")


def test_cta_past_the_end_is_rejected():
    document = minimal_document(cta={"text": "Subscribe", "start": 19.0, "duration": 5.0})
    with pytest.raises(SpecError):
        parse_spec(document)


def test_schema_file_stays_in_sync_with_the_example():
    schema = json.loads((EXAMPLE.parent.parent / "schema" / "short.schema.json").read_text("utf-8"))
    example = json.loads(EXAMPLE.read_text("utf-8"))
    for required in schema["required"]:
        assert required in example, f"schema requires {required} but the example omits it"
