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


class _PolicyClient:
    """Answers get_policy_v2 per policy type from a table; a missing type raises, like an unreadable policy."""
    def __init__(self, by_type):
        self.by_type = by_type

    def call(self, op, **kw):
        assert op == "get_policy_v2"
        if kw["policy_type"] not in self.by_type:
            raise RuntimeError("boom")
        return {"policy": {"rules": self.by_type[kw["policy_type"]]}}


def _every_type(extra):
    from fbcleanup.collect import POLICY_TYPES
    return {t: extra.get(t, []) for t in POLICY_TYPES}


def test_collect_vault_refs_reads_every_policy_type_not_just_transfer():
    from fbcleanup.collect import collect_vault_refs
    aml_rule = {"name": "r", "source": {"ids": [{"id": "123", "type": "VAULT"}]}}
    mint_rule = {"name": "m", "source": {"ids": [["VAULT", "456"]], "operator": "INCLUDES"}}
    wallet_rule = {"name": "w", "destination": {"ids": [{"id": "9", "type": "EXTERNAL_WALLET"}]}}
    refs = collect_vault_refs(_PolicyClient(_every_type({"AML_CHAINALYSIS_V2_SCREENING": [aml_rule], "MINT": [mint_rule],
                                                         "TRANSFER": [wallet_rule]})))
    assert refs == {"vault:123", "vault:456"}          # vaults only: wallet references are not vault references


def test_collect_vault_refs_refuses_when_any_policy_type_is_unreadable():
    import pytest
    from fbcleanup.collect import POLICY_TYPES, collect_vault_refs
    table = _every_type({})
    del table[POLICY_TYPES[5]]
    with pytest.raises(RuntimeError, match=POLICY_TYPES[5]):
        collect_vault_refs(_PolicyClient(table))
