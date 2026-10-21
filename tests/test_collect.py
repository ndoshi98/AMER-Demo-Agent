from fbcleanup.collect import collect_audit


def test_audit_window_is_sent_in_milliseconds(make_client):
    cl, sdk = make_client({("audit_logs", "get_audit_logs"): {"data": [], "next": None}})
    events, complete = collect_audit(cl, 1_800_000_000, 1_800_086_400)
    kw = sdk.calls("get_audit_logs")[0][2]
    assert (kw["start_time"], kw["end_time"]) == (1_800_000_000_000, 1_800_086_400_000)
    assert events == [] and complete


def test_known_user_ids_merges_console_and_api_users_and_needs_both():
    from fbcleanup.collect import Inventory, known_user_ids
    inv = Inventory(now=1, console_users=[{"id": "c", "enabled": True}], api_users=[{"id": "a", "enabled": False}])
    inv.ok = {"user"}
    assert known_user_ids(inv) is None             # partial map would report real API-user signers as missing
    inv.ok = {"user", "api_user"}
    assert known_user_ids(inv) == {"c": True, "a": False}


def test_collect_activity_reports_how_far_back_history_reaches():
    from fbcleanup.collect import collect_activity

    class C:
        def call(self, op, **kw):
            return [{"createdAt": 1_700_000_100_000, "source": {"type": "VAULT_ACCOUNT", "id": "1"}},
                    {"createdAt": 1_700_000_000_000, "source": {"type": "NETWORK_CONNECTION", "id": "c"}}]

    act, complete, earliest = collect_activity(C(), 0)
    assert complete and earliest == 1_700_000_000 and act[("network_connection", "c")] == 1_700_000_000
