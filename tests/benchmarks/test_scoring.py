"""T040 scoring: the scorer computes completion itself; corrupt traces fail."""
import pytest

from benchmarks.harness.scoring import score_trace, summarize

TASK = {"id": "t1", "expected": {"key": "release.manifest", "value": "Python 3.12"},
        "must_not_use": ["3.11"], "minimal_steps": 4,
        "change": {"relevant": True}}


def _trace(**over):
    t = {"task_id": "t1", "arm": "x", "seed": 0, "mode": "deterministic",
         "steps": [{"action": "a"}] * 4, "events": [], "claimed_completed": True}
    t.update(over)
    return t


def test_clean_produce_scores_completed():
    s = score_trace(_trace(events=[{"type": "produce", "key": "release.manifest",
                                    "value": "Python 3.12"}]), TASK)
    assert s["completed"] is True and s["stale_fact_used"] is False


def test_claimed_success_with_stale_fact_is_overridden():
    s = score_trace(_trace(events=[
        {"type": "use_fact", "key": "build.python_version", "value": "3.11"},
        {"type": "produce", "key": "release.manifest", "value": "Python 3.12"}]), TASK)
    assert s["claimed_completed"] is True
    assert s["completed"] is False and s["stale_fact_used"] is True
    assert s["claim_overridden"] is True


def test_contract_violation_blocks_completion():
    s = score_trace(_trace(events=[
        {"type": "produce", "key": "release.manifest", "value": "Python 3.12"},
        {"type": "contract_violation", "code": "SILENT_OVERWRITE"}]), TASK)
    assert s["completed"] is False and s["contract_violations"] == 1


def test_corrupt_trace_fails_instead_of_passing():
    for bad in ({"task_id": "t1"}, {"task_id": "t1", "arm": "x", "seed": 0,
                                    "mode": "live", "steps": [], "events": []}):
        t = _trace()
        t.update(bad)
        if "arm" not in bad:
            t.pop("arm", None)
        with pytest.raises(ValueError):
            score_trace(t, TASK)


def test_redo_cost_and_unknown_tokens():
    s = score_trace(_trace(steps=[{"action": "a"}] * 7,
                           events=[{"type": "produce", "key": "release.manifest",
                                    "value": "Python 3.12"}]), TASK)
    assert s["redo_cost_steps"] == 3 and s["tokens"] == "unknown"


def test_irrelevant_refusal_only_on_irrelevant_change():
    task = dict(TASK, change={"relevant": False})
    s = score_trace(_trace(events=[{"type": "refused"}]), task)
    assert s["irrelevant_refusal"] is True
    s2 = score_trace(_trace(events=[{"type": "refused"}]), TASK)
    assert s2["irrelevant_refusal"] is False


def test_summarize_keeps_failures_in_denominator():
    scores = [score_trace(_trace(events=[{"type": "produce", "key": "release.manifest",
                                         "value": "Python 3.12"}]), TASK),
              score_trace(_trace(events=[]), TASK)]
    agg = summarize(scores)
    assert agg["attempts"] == 2 and agg["completed"] == 1
    assert agg["success_rate"] == 0.5
