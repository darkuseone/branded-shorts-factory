"""The wall-clock budget (§19).

Every other budget here counts calls or tokens. This one counts the thing that
actually kills a run: a single Magnific generation polls for up to seven
minutes, and enough of those queued together sail past the workflow's own
timeout — at which point the job is killed from outside with no artifact, no
report and a log that just stops.
"""

from __future__ import annotations

from shorts_factory.deadline import SLOW_STEP_S, Deadline


def test_a_fresh_run_allows_a_slow_step():
    assert Deadline.from_minutes(35).allows_slow_step()


def test_a_nearly_spent_clock_refuses_a_slow_step():
    deadline = Deadline.from_minutes(35)
    deadline.started -= 34 * 60  # 60s left, a generation needs 420
    assert not deadline.allows_slow_step()


def test_refusing_is_recorded_once_so_the_report_can_say_it_degraded():
    deadline = Deadline.from_minutes(1)
    deadline.started -= 59
    assert not deadline.allows_slow_step()
    first = deadline.exceeded_at
    assert not deadline.allows_slow_step()
    assert deadline.exceeded_at == first, "the moment of degradation is recorded once"


def test_a_short_step_still_runs_when_a_slow_one_would_not():
    deadline = Deadline.from_minutes(10)
    deadline.started -= 9 * 60 + 30  # 30s left
    assert not deadline.allows_slow_step()
    assert deadline.allows(5.0), "cheap work continues; only the slow tiers are dropped"


def test_zero_disables_the_guard_entirely():
    deadline = Deadline.from_minutes(0)
    deadline.started -= 10_000
    assert deadline.remaining == float("inf")
    assert deadline.allows_slow_step()
    assert not deadline.expired


def test_a_call_timeout_is_capped_to_what_is_left():
    deadline = Deadline.from_minutes(10)
    deadline.started -= 9 * 60  # 60s left
    assert 59.0 <= deadline.cap(600.0) <= 60.0
    assert deadline.cap(10.0) == 10.0, "a shorter timeout is left alone"


def test_a_cap_never_returns_zero_or_negative():
    deadline = Deadline.from_minutes(1)
    deadline.started -= 300  # long past
    assert deadline.expired
    assert deadline.cap(600.0) >= 1.0


def test_the_report_shows_whether_the_run_degraded():
    deadline = Deadline.from_minutes(35)
    assert deadline.to_dict()["degraded_at_s"] is None
    deadline.started -= 35 * 60
    deadline.allows_slow_step()
    assert deadline.to_dict()["degraded_at_s"] is not None


def test_the_slow_step_threshold_matches_a_real_generation():
    # Magnific polls a generation for 420s; a guard shorter than that would
    # green-light a call that cannot finish.
    from shorts_factory.generative.magnific import POLL_TIMEOUT

    assert SLOW_STEP_S >= POLL_TIMEOUT
