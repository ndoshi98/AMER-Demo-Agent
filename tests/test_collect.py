from fbcleanup.collect import collect_audit


def test_audit_window_is_sent_in_milliseconds(make_client):
    cl, sdk = make_client({("audit_logs", "get_audit_logs"): {"data": [], "next": None}})
    events, complete = collect_audit(cl, 1_800_000_000, 1_800_086_400)
    kw = sdk.calls("get_audit_logs")[0][2]
    assert (kw["start_time"], kw["end_time"]) == (1_800_000_000_000, 1_800_086_400_000)
    assert events == [] and complete
