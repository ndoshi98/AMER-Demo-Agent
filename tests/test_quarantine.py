import pytest

from fbcleanup.state import DAY, State

V = ("vault", "42")
EVAL = ["vault"]


@pytest.fixture
def st():
    s = State(":memory:")
    yield s
    s.close()


def elig(st, now, runs=2, days=14, key=V):
    return st.eligible(key, now, runs, days)


def test_first_sighting_never_eligible(st):
    st.observe("r1", 0, [V], EVAL)
    assert not elig(st, 100 * DAY)  # plenty of days, but only one run


def test_first_sighting_never_eligible_even_if_config_says_one_run(st):
    st.observe("r1", 0, [V], EVAL)
    assert not elig(st, 100 * DAY, runs=1, days=0)


def test_run_count_alone_is_insufficient(st):
    for i in range(5):
        st.observe(f"r{i}", i * 60, [V], EVAL)  # five runs within five minutes
    assert not elig(st, 5 * 60)


def test_days_alone_is_insufficient(st):
    st.observe("r1", 0, [V], EVAL)
    assert not elig(st, 30 * DAY)  # 30 days elapsed but never re-observed


def test_both_thresholds_required(st):
    st.observe("r1", 0, [V], EVAL)
    st.observe("r2", 14 * DAY, [V], EVAL)
    assert elig(st, 14 * DAY)
    assert not st.eligible(V, 14 * DAY - 1, 2, 14)


def test_same_run_id_counts_once(st):
    st.observe("r1", 0, [V], EVAL)
    st.observe("r1", 20 * DAY, [V], EVAL)
    assert st.progress(V)["runs"] == 1


def test_release_resets_rather_than_resumes(st):
    st.observe("r1", 0, [V], EVAL)
    st.observe("r2", 10 * DAY, [V], EVAL)
    st.observe("r3", 11 * DAY, [], EVAL)  # funded again -> released
    assert st.progress(V) is None
    st.observe("r4", 20 * DAY, [V], EVAL)  # qualifies again: clock restarts from zero
    p = st.progress(V)
    assert p["runs"] == 1 and p["first_flagged_at"] == 20 * DAY
    assert not elig(st, 40 * DAY)


def test_unevaluated_type_is_neither_advanced_nor_released(st):
    st.observe("r1", 0, [V], EVAL)
    st.observe("r2", 5 * DAY, [], ["tag"])  # vault collection failed this run
    assert st.progress(V)["runs"] == 1
    st.observe("r3", 6 * DAY, [V], ["tag"])  # still not evaluated -> no increment
    assert st.progress(V)["runs"] == 1


def test_acted_items_do_not_requeue(st):
    st.observe("r1", 0, [V], EVAL)
    st.observe("r2", 14 * DAY, [V], EVAL)
    st.mark_acted(V, 14 * DAY)
    assert st.is_acted(V)
    assert not elig(st, 30 * DAY)
    st.observe("r3", 30 * DAY, [V], EVAL)  # still flagged -> must stay acted, not restart
    st.observe("r4", 31 * DAY, [], EVAL)  # no longer flagged -> must not be released/deleted
    assert st.is_acted(V)
    st.observe("r5", 60 * DAY, [V], EVAL)
    assert st.is_acted(V) and not elig(st, 90 * DAY)


def test_last_seen_keeps_max_across_out_of_order_events(st):
    for ts in (500, 900, 100, 700):
        st.record_activity("User@Example.com", ts)
    assert st.last_seen("user@example.com") == 900


def test_last_seen_unknown_actor(st):
    assert st.last_seen("nobody") is None


def test_audit_coverage_first_run_is_lookback_window(st):
    assert st.update_audit_coverage(1000 * DAY, 365) == 635 * DAY


def test_audit_coverage_extends_while_runs_are_continuous(st):
    st.update_audit_coverage(1000 * DAY, 365)
    assert st.update_audit_coverage(1100 * DAY, 365) == 635 * DAY  # unchanged: coverage is continuous


def test_audit_coverage_resets_after_gap_longer_than_api_window(st):
    st.update_audit_coverage(1000 * DAY, 365)
    assert st.update_audit_coverage(1400 * DAY, 365) == 1035 * DAY
