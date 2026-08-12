"""Core: cache, budget, retries, state, schemas (ЗАДАЧА 1 DoD)."""

from __future__ import annotations

import json

import pytest

from redshift.core.budget import Budget, CostRecord, cost
from redshift.core.cache import Cache, ObjectMeta, llm_key, query_key
from redshift.core.config import Config, load_config
from redshift.core.errors import BudgetExceeded, ConfigError, ProviderError, SchemaError
from redshift.core.media import average_hash, hamming, laplacian_variance
from redshift.core.retry import CircuitBreaker, backoff_delay, parse_json_strict, with_retries
from redshift.core.schemas import Script, Shot, Shotlist, Timeline, TimelineMeta
from redshift.core.state import RunState, new_run_id, stage_hash

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #


def test_every_config_file_loads():
    config = load_config()
    assert config.rhythm("shot_duration_s.MODE_B.max") == 2.9
    assert config.brand("colors.accent") == "#E11D48"
    assert config.critic("weights.relevance") == 0.30


def test_missing_key_raises_instead_of_defaulting_silently():
    """A typo in a config path must not quietly become a magic number."""
    with pytest.raises(ConfigError):
        load_config().rhythm("shot_duration_s.MODE_Z.max")


def test_explicit_default_is_allowed():
    assert load_config().rhythm("does.not.exist", 7) == 7


def test_config_directory_must_exist(tmp_path):
    with pytest.raises(ConfigError):
        Config.load(tmp_path)


# --------------------------------------------------------------------------- #
# Cache (§4.2)
# --------------------------------------------------------------------------- #


@pytest.fixture
def cache(tmp_path) -> Cache:
    return Cache(tmp_path / "cache")


def test_object_store_is_content_addressed(cache):
    digest, path = cache.put_object(b"hello world", suffix=".mp4")
    again, same_path = cache.put_object(b"hello world", suffix=".mp4")
    assert digest == again
    assert path == same_path
    assert cache.has_object(digest)


def test_object_meta_round_trips(cache):
    digest, _ = cache.put_object(
        b"x" * 128, suffix=".jpg", meta=ObjectMeta(sha256="", source="nasa", phash="abcd")
    )
    meta = cache.get_meta(digest)
    assert meta is not None
    assert meta.source == "nasa"
    assert meta.first_seen > 0


def test_query_cache_expires(tmp_path):
    cache = Cache(tmp_path / "c", queries_ttl_days=0)
    key = query_key("pexels", "nebula", {"per_page": 10})
    cache.put_query(key, [{"id": 1}])
    assert cache.get_query(key) is None, "a zero TTL means every entry is stale"


def test_query_cache_returns_a_fresh_hit(cache):
    key = query_key("pexels", "nebula", None)
    cache.put_query(key, [{"id": 1}])
    assert cache.get_query(key) == [{"id": 1}]
    assert cache.hit_rate > 0


def test_llm_cache_keys_depend_on_the_prompt(cache):
    first = llm_key("grok", "prompt A", {"t": 0})
    second = llm_key("grok", "prompt B", {"t": 0})
    assert first != second
    cache.put_llm(first, {"ok": True})
    assert cache.get_llm(first) == {"ok": True}
    assert cache.get_llm(second) is None


def test_purge_never_touches_objects(cache):
    digest, path = cache.put_object(b"y" * 64)
    cache.queries_ttl = 0
    cache.put_query("stale", {"a": 1})
    cache.purge_expired()
    assert path.exists(), "the object store is the main saving; GC must not eat it"


# --------------------------------------------------------------------------- #
# Budget (§4.3) — DoD: exceeding a limit degrades, it does not crash the run
# --------------------------------------------------------------------------- #


@pytest.fixture
def budget(tmp_path) -> Budget:
    return Budget(limits={"grok_vision": 2, "magnific_image": 1}, costs_file=tmp_path / "costs.jsonl")


def test_budget_allows_up_to_the_limit(budget):
    budget.check("grok_vision")
    budget.charge("grok_vision")
    budget.check("grok_vision")
    budget.charge("grok_vision")
    assert budget.remaining("grok_vision") == 0


def test_budget_raises_past_the_limit(budget):
    budget.charge("grok_vision", 2)
    with pytest.raises(BudgetExceeded) as excinfo:
        budget.check("grok_vision")
    assert excinfo.value.kind == "grok_vision"


def test_unknown_kinds_are_not_metered(budget):
    budget.check("something_else", 1000)  # must not raise


def test_costs_are_appended_to_the_ledger(budget, tmp_path):
    budget.record(CostRecord(stage="s05", provider="grok", kind="grok_vision", tokens_in=100))
    lines = (tmp_path / "costs.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert json.loads(lines[0])["kind"] == "grok_vision"


class _Client:
    """Minimal stand-in for a client the @cost decorator wraps."""

    name = "grok"
    stage = "s05"

    def __init__(self, budget: Budget, cached: bool = False):
        self.budget = budget
        self._last_call_cached = cached
        self._last_tokens_in = 10
        self._last_tokens_out = 5
        self._last_credits = 0.0

    @cost(kind="grok_vision")
    def evaluate(self) -> str:
        return "ok"


def test_decorator_charges_a_real_call(budget):
    client = _Client(budget)
    assert client.evaluate() == "ok"
    assert budget.spent("grok_vision") == 1
    assert budget.records[0].tokens_in == 10


def test_decorator_does_not_charge_a_cache_hit(budget):
    """A cached answer costs nothing — that is what makes the ledger honest."""
    client = _Client(budget, cached=True)
    client.evaluate()
    assert budget.spent("grok_vision") == 0
    assert budget.records[0].cache_hit is True


def test_decorator_refuses_once_the_budget_is_gone(budget):
    budget.charge("grok_vision", 2)
    with pytest.raises(BudgetExceeded):
        _Client(budget).evaluate()


def test_summary_reports_the_hit_rate(budget):
    _Client(budget).evaluate()
    _Client(budget, cached=True).evaluate()
    summary = budget.summary()
    assert summary["calls"] == 2
    assert summary["cache_hits"] == 1


# --------------------------------------------------------------------------- #
# Retries and the circuit breaker (§4.4)
# --------------------------------------------------------------------------- #


def test_transient_failure_is_retried():
    attempts = {"n": 0}

    def flaky():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise ProviderError("pexels", "boom", status=503)
        return "ok"

    assert with_retries(flaky, provider="pexels", sleep=lambda _: None) == "ok"
    assert attempts["n"] == 3


def test_permanent_failure_is_not_retried():
    attempts = {"n": 0}

    def forbidden():
        attempts["n"] += 1
        raise ProviderError("pexels", "nope", status=401)

    with pytest.raises(ProviderError):
        with_retries(forbidden, provider="pexels", sleep=lambda _: None)
    assert attempts["n"] == 1, "a 401 will not fix itself"


def test_breaker_parks_a_provider_after_five_failures():
    breaker = CircuitBreaker(threshold=2, cooldown_s=60)
    for _ in range(2):
        breaker.record_failure("magnific")
    assert breaker.is_open("magnific")
    assert not breaker.is_open("pexels")


def test_backoff_honours_retry_after():
    assert backoff_delay(0, retry_after=5.0) == 5.0
    assert backoff_delay(0, retry_after=999.0) == 30.0, "capped, or a run stalls"


@pytest.mark.parametrize(
    "reply",
    [
        '{"a": 1}',
        '```json\n{"a": 1}\n```',
        'Sure! Here you go: {"a": 1} — hope that helps',
    ],
)
def test_json_is_recovered_from_a_chatty_model(reply):
    assert parse_json_strict(reply) == {"a": 1}


def test_unparsable_reply_raises():
    with pytest.raises(ValueError):
        parse_json_strict("no json here at all")


# --------------------------------------------------------------------------- #
# Run state (§4.1, §20)
# --------------------------------------------------------------------------- #


def test_state_survives_a_round_trip(tmp_path):
    state = RunState(run_id="r1", topic_id="t1")
    state.mark_running("s03_shotlist", "hash1")
    state.mark_done("s03_shotlist", artifact="shotlist.json")
    state.save(tmp_path)

    reloaded = RunState.load(tmp_path, "r1")
    assert reloaded.stages["s03_shotlist"].status == "done"
    assert reloaded.topic_id == "t1"


def test_done_stage_is_skipped_only_when_the_artifact_is_intact(tmp_path):
    state = RunState(run_id="r1")
    run_dir = tmp_path / "r1"
    run_dir.mkdir(parents=True)
    (run_dir / "shotlist.json").write_text('{"ok": true}', encoding="utf-8")

    state.mark_running("s03_shotlist", "h")
    state.mark_done("s03_shotlist", artifact="shotlist.json")
    assert state.is_done("s03_shotlist", "h", tmp_path)
    assert not state.is_done("s03_shotlist", "different-hash", tmp_path)

    (run_dir / "shotlist.json").write_text("{ broken", encoding="utf-8")
    assert not state.is_done("s03_shotlist", "h", tmp_path), "corrupt artifact must re-run"


def test_failure_records_a_traceback_and_a_partial(tmp_path):
    state = RunState(run_id="r1")
    state.mark_running("s04_asset_hunter", "h")
    try:
        raise RuntimeError("MagnificTimeout")
    except RuntimeError as exc:
        state.mark_failed("s04_asset_hunter", exc, partial="candidates.partial.json")
    stage = state.stages["s04_asset_hunter"]
    assert stage.status == "failed"
    assert "MagnificTimeout" in stage.error
    assert stage.partial == "candidates.partial.json"


def test_resume_starts_at_the_first_unfinished_stage(tmp_path):
    state = RunState(run_id="r1")
    for name in ("s00_topic_scout", "s01_research"):
        state.mark_running(name, "h")
        state.mark_done(name)
    assert state.next_stage() == "s02_script"


def test_attempts_are_capped(tmp_path):
    state = RunState(run_id="r1")
    for _ in range(3):
        state.mark_running("s05_critic", "h")
    assert state.exhausted("s05_critic")


def test_degradation_only_moves_forward():
    state = RunState(run_id="r1")
    state.degrade_to(2, "no gemini budget")
    state.degrade_to(1, "should not lower the level")
    assert state.degradation_level == 2
    state.degrade_to(3, "no vision at all")
    assert state.needs_human_review is True


def test_stage_hash_changes_with_its_inputs():
    assert stage_hash("a", {"x": 1}) == stage_hash("a", {"x": 1})
    assert stage_hash("a", {"x": 1}) != stage_hash("a", {"x": 2})


def test_run_id_is_sortable_and_readable():
    assert new_run_id("jwst_k218b").endswith("_jwst_k218b_01")


# --------------------------------------------------------------------------- #
# Schemas (§21)
# --------------------------------------------------------------------------- #


def test_wrong_schema_version_fails_loudly(tmp_path):
    path = tmp_path / "shotlist.json"
    path.write_text(json.dumps({"schema_version": "0.9", "shots": []}), encoding="utf-8")
    with pytest.raises(SchemaError):
        Shotlist.load(path)


def test_timeline_rejects_an_external_url():
    with pytest.raises(ValueError):
        Timeline(
            meta=TimelineMeta(duration=45.0),
            tracks={"L1_footage": [{"t": 0, "d": 1, "src": "https://example.com/a.mp4"}]},
        )


def test_timeline_rejects_an_unknown_layer():
    with pytest.raises(ValueError):
        Timeline(tracks={"L9_mystery": []})


def test_script_needs_a_closer(tmp_path):
    with pytest.raises(ValueError):
        Script(
            topic_id="t",
            closer_type="CLOSER_LOOP",
            beats=[{"id": "b1", "section": "HOOK", "text": "раз"}],
        )


def test_shot_rejects_a_negative_duration():
    with pytest.raises(ValueError):
        Shot(id="s1", beat_id="b1", t_start=5.0, t_end=4.0, mode="MODE_B", visual_intent="COSMIC_HERO")


def test_artifact_write_is_atomic(tmp_path):
    shotlist = Shotlist(total_duration_s=1.0)
    path = shotlist.save(tmp_path / "out" / "shotlist.json")
    assert path.exists()
    assert not list(path.parent.glob("*.tmp"))


# --------------------------------------------------------------------------- #
# Media measurements (§9.5) — pure functions, no ffmpeg needed
# --------------------------------------------------------------------------- #


def test_identical_frames_hash_the_same():
    frame = bytes(range(64))
    assert hamming(average_hash(frame), average_hash(frame)) == 0


def test_different_frames_hash_apart():
    dark = bytes([10] * 32 + [200] * 32)
    inverted = bytes([200] * 32 + [10] * 32)
    assert hamming(average_hash(dark), average_hash(inverted)) > 20


def test_missing_hash_is_maximally_distant():
    assert hamming("", "abcd") == 64


def test_flat_frame_is_not_sharp():
    flat = bytes([128] * (32 * 32))
    noisy = bytes([0 if (i // 3) % 2 else 255 for i in range(32 * 32)])
    assert laplacian_variance(flat, 32, 32) < laplacian_variance(noisy, 32, 32)
