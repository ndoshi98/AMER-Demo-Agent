from decimal import Decimal

import pytest

from fbcleanup import sweep_value as V

NOW = 1_800_000_000
D = Decimal


def asset(aid, total, **extra):
    a = {"id": aid, "total": total, "available": total, "pending": "0", "frozen": "0", "lockedAmount": "0", "staked": "0"}
    a.update(extra)
    return a


def vault(vid, name, *assets):
    return {"id": str(vid), "name": name, "assets": list(assets)}


def info(aid, testnet=False, native=True, gas="ETH"):
    return V.AssetInfo(aid, testnet, native, gas)


INFOS = {"ETH": info("ETH"), "ETH_TEST5": info("ETH_TEST5", True, True, "ETH_TEST5"),
         "USDC": info("USDC", False, False, "ETH"), "USDC_T": info("USDC_T", True, False, "ETH_TEST5"),
         "UNK": None}
PRICES = {"ETH": D(2500), "USDC": D(1), "ETH_TEST5": D(2500), "USDC_T": D(1)}


def plan(vaults, max_usd=D(10), margin=D("0.10")):
    return {p.vault["id"]: p for p in V.plan_vaults(vaults, "386", INFOS, PRICES, max_usd, margin)}


def test_testnet_and_mainnet_are_both_priced_and_tokens_go_first():
    p = plan([vault(1, "a", asset("ETH", "0.001"), asset("USDC", "2"), asset("ETH_TEST5", "0.001"))])["1"]
    assert not p.excluded and p.usd == D("7")
    assert p.items[0].asset == "USDC" and p.items[-1].asset in ("ETH", "ETH_TEST5")


def test_a_valuable_testnet_balance_counts_toward_the_cap():
    assert plan([vault(1, "a", asset("ETH_TEST5", "1"))])["1"].excluded.startswith("over cap")
    assert plan([vault(2, "b", asset("ETH", "0.001"), asset("ETH_TEST5", "0.004"))])["2"].excluded.startswith("over cap")


def test_empty_destination_and_unknown_assets():
    out = plan([vault(1, "empty", asset("ETH", "0")), vault(386, "dest", asset("ETH", "0.001")),
                vault(2, "x", asset("UNK", "1")), vault(3, "y", asset("ETH", "0.001"), asset("MISSING", "1"))])
    assert "1" not in out and "386" not in out
    assert "cannot classify" in out["2"].excluded and "cannot classify" in out["3"].excluded


@pytest.mark.parametrize("testnet", [False, True])
def test_unpriced_asset_leaves_the_vault_alone(testnet):
    infos = dict(INFOS, HEX=info("HEX", testnet))
    p = V.plan_vaults([vault(1, "a", asset("HEX", "5"))], "386", infos, {"HEX": None}, D(10))[0]
    assert "no USD price" in p.excluded and p.items == []


def test_cap_and_margin():
    assert plan([vault(1, "a", asset("USDC", "10.01"))])["1"].excluded.startswith("over cap")
    assert "within 10% of the cap" in plan([vault(1, "a", asset("USDC", "9.5"))])["1"].excluded
    assert not plan([vault(1, "a", asset("USDC", "9.0"))])["1"].excluded
    assert not plan([vault(1, "a", asset("USDC", "9.5"))], margin=D(0))["1"].excluded


def test_unavailable_balances_are_skipped_per_asset_not_per_vault():
    p = plan([vault(1, "a", asset("USDC", "2", frozen="1"), asset("ETH", "0.001"))])["1"]
    assert {i.asset: bool(i.skip) for i in p.items} == {"USDC": True, "ETH": False}


def price_client(make_client, txs):
    cl, _ = make_client({("transactions", "get_transactions"): txs})
    return cl


def tx(amount, usd, age_days=1):
    return {"amount": amount, "amountUSD": usd, "createdAt": int((NOW - age_days * 86400) * 1000)}


def test_price_uses_newest_valid_transaction(make_client):
    cl = price_client(make_client, [tx(0, 0), tx(2, 5000), tx(1, 2400)])
    assert V.usd_price(cl, "ETH", now=NOW) == D(2500)


@pytest.mark.parametrize("txs", [[], [tx(1, 5, age_days=90)], [tx("1000000000", "7e-50")],
                                 [tx(1, 100), tx(1, 10)]])
def test_implausible_stale_or_inconsistent_prices_are_untrusted(make_client, txs):
    assert V.usd_price(price_client(make_client, txs), "X", now=NOW) is None


BCS = {"b1": {"id": "b1", "legacyId": "ETH", "nativeAssetId": "n1", "onchain": {"test": False}},
       "b2": {"id": "b2", "legacyId": "ETH_TEST5", "nativeAssetId": "n2", "onchain": {"test": True}},
       "b3": {"id": "b3", "legacyId": "WEIRD", "nativeAssetId": "n3", "onchain": {}}}


def test_asset_info_testnet_native_and_fail_closed(make_client):
    assets = {"ETH": {"id": "n1", "blockchainId": "b1"}, "USDC": {"id": "t1", "blockchainId": "b1"},
              "ETH_TEST5": {"id": "n2", "blockchainId": "b2"}, "W": {"id": "w", "blockchainId": "b3"},
              "N": {"id": "x", "blockchainId": "nope"}}
    cl, _ = make_client({("blockchains_assets", "get_asset"): lambda id, **kw: assets[id]})

    def g(a):
        return V.asset_info(cl, BCS, a)

    assert (g("ETH").testnet, g("ETH").native) == (False, True)
    assert (g("USDC").native, g("USDC").gas_asset) == (False, "ETH")
    assert g("ETH_TEST5").testnet is True
    assert g("W") is None and g("N") is None   # unknown chain / no test flag is never assumed to be testnet


# --- execution ------------------------------------------------------------------------------------
FEE = {"medium": {"networkFee": "0.00001"}}


def exec_responses(live):
    return {("vaults", "get_vault_account"): live, ("transactions", "estimate_transaction_fee"): FEE,
            ("vaults", "create_vault_account_asset"): {"id": "386"},
            ("transactions", "create_transaction"): lambda **kw: {"id": "tx1", "status": "SUBMITTED"},
            ("transactions", "get_transaction"): {"id": "tx1", "status": "COMPLETED"}}


def by_id(vs):
    return lambda vault_account_id: next(v for v in vs if v["id"] == vault_account_id)


def run(make_client, v, dest_assets=("ETH",), dry_run=False, limit=None, live=None):
    plans = V.plan_vaults([v], "386", INFOS, PRICES, D(10))
    cl, sdk = make_client(exec_responses(live or v), dry_run=dry_run)
    outs = V.run_value_sweep(cl, plans, INFOS, "386", set(dest_assets), "r", limit=limit, sleep=lambda s: None)
    return outs, sdk


def test_creates_missing_destination_wallet_then_sends(make_client):
    outs, sdk = run(make_client, vault(1, "a", asset("ETH", "0.001")), dest_assets=())
    assert [o.status for o in outs] == ["completed"] and "created destination wallet" in outs[0].detail
    assert sdk.calls("create_vault_account_asset")[0][2] == {"vault_account_id": "386", "asset_id": "ETH"}
    assert len(sdk.calls("create_transaction")) == 1


def test_existing_destination_wallet_is_not_recreated(make_client):
    _, sdk = run(make_client, vault(1, "a", asset("ETH", "0.001")))
    assert sdk.calls("create_vault_account_asset") == []


def test_dry_run_creates_and_sends_nothing(make_client):
    outs, sdk = run(make_client, vault(1, "a", asset("ETH", "0.001"), asset("USDC", "2")), dest_assets=("ETH",),
                    dry_run=True)
    assert all(o.status == "dry-run" for o in outs) and outs
    assert sdk.calls("create_transaction") == []
    assert [c for c in sdk.calls("create_vault_account_asset")] == []   # USDC wallet creation is suppressed too


def test_token_needs_gas_in_the_source_vault(make_client):
    outs, sdk = run(make_client, vault(1, "a", asset("USDC", "2")), dest_assets=("USDC",))
    assert outs[0].status == "skipped" and "ETH" in outs[0].detail and sdk.calls("create_transaction") == []
    outs, sdk = run(make_client, vault(1, "a", asset("USDC", "2"), asset("ETH", "0.001")), dest_assets=("USDC", "ETH"))
    assert [o.status for o in outs] == ["completed", "completed"]
    body = sdk.calls("create_transaction")[0][2]["transaction_request"].to_dict()
    assert body["assetId"] == "USDC" and body["treatAsGrossAmount"] is False   # token first, fee paid in ETH


def test_native_balance_smaller_than_fee_is_skipped(make_client):
    outs, sdk = run(make_client, vault(1, "a", asset("ETH", "0.000005")))
    assert outs[0].status == "skipped" and sdk.calls("create_transaction") == []


def test_stale_plan_is_not_sent(make_client):
    v = vault(1, "a", asset("ETH", "0.001"))
    outs, sdk = run(make_client, v, live=vault(1, "a", asset("ETH", "0.0005")))
    assert outs[0].status == "skipped" and sdk.calls("create_transaction") == []


def test_wallet_creation_failure_is_a_skip_not_a_stop(make_client):
    vs = [vault(1, "a", asset("ETH_T9", "1"), asset("ETH", "0.001")), vault(2, "b", asset("ETH", "0.001"))]
    infos = dict(INFOS, ETH_T9=info("ETH_T9", True, False, "ETH_TEST5"))
    plans = V.plan_vaults(vs, "386", infos, dict(PRICES, ETH_T9=D(1)), D(10))
    r = exec_responses(by_id(vs))

    def boom(**kw):
        raise RuntimeError("no such asset")
    r[("vaults", "create_vault_account_asset")] = boom
    cl, sdk = make_client(r, dry_run=False)
    outs = V.run_value_sweep(cl, plans, infos, "386", {"ETH"}, "r", sleep=lambda s: None)
    assert [o.status for o in outs] == ["skipped", "completed", "completed"]


def test_failed_transfer_stops_the_run(make_client):
    vs = [vault(1, "a", asset("ETH", "0.001")), vault(2, "b", asset("ETH", "0.001"))]
    plans = V.plan_vaults(vs, "386", INFOS, PRICES, D(10))
    r = exec_responses(by_id(vs))
    r[("transactions", "get_transaction")] = {"id": "tx1", "status": "FAILED"}
    cl, sdk = make_client(r, dry_run=False)
    outs = V.run_value_sweep(cl, plans, INFOS, "386", {"ETH"}, "r", sleep=lambda s: None)
    assert [o.status for o in outs] == ["failed"] and len(sdk.calls("create_transaction")) == 1


def test_limit_counts_transfers_sent(make_client):
    vs = [vault(i, f"v{i}", asset("ETH", "0.001")) for i in (1, 2, 3)]
    plans = V.plan_vaults(vs, "386", INFOS, PRICES, D(10))
    cl, sdk = make_client(exec_responses(by_id(vs)), dry_run=False)
    outs = V.run_value_sweep(cl, plans, INFOS, "386", {"ETH"}, "r", limit=2, sleep=lambda s: None)
    assert len(sdk.calls("create_transaction")) == 2 and len(outs) == 2


def test_api_rejection_of_a_transfer_is_a_skip_but_other_errors_stop(make_client):
    from fbcleanup.client import ApiError
    vs = [vault(1, "a", asset("ETH", "0.001")), vault(2, "b", asset("ETH", "0.001"))]
    plans = V.plan_vaults(vs, "386", INFOS, PRICES, D(10))

    class Rejected(Exception):
        status = 400

    def reject(**kw):
        raise Rejected("Source balance error")
    r = exec_responses(by_id(vs))
    r[("transactions", "create_transaction")] = reject
    cl, _ = make_client(r, dry_run=False)
    outs = V.run_value_sweep(cl, plans, INFOS, "386", {"ETH"}, "r", sleep=lambda s: None)
    assert [o.status for o in outs] == ["skipped", "skipped"] and "rejected" in outs[0].detail

    class Boom(Exception):
        status = 500

    def boom(**kw):
        raise Boom("server")
    r[("transactions", "create_transaction")] = boom
    cl, _ = make_client(r, dry_run=False)
    outs = V.run_value_sweep(cl, plans, INFOS, "386", {"ETH"}, "r", sleep=lambda s: None)
    assert [o.status for o in outs] == ["failed"]


def test_idempotency_key_is_deterministic_and_within_the_api_limit():
    k = V._idem_key("sweep-20261021T193902Z", "123456", "AMOY_POLYGON_TEST")
    assert len(k) <= 40 and k == V._idem_key("sweep-20261021T193902Z", "123456", "AMOY_POLYGON_TEST")
    assert k != V._idem_key("sweep-20261021T193902Z", "123456", "ETH_TEST5")


# --- holds and hiding -----------------------------------------------------------------------------
from fbcleanup.config import Protect  # noqa: E402


def _protect(**kw):
    return Protect(**{"ids": [], "name_patterns": [], "customer_ref_id_prefixes": [], "tag_labels": ["keep-for-demo"], **kw})


CINFOS = dict(INFOS, CANTON=info("CANTON", False, True, "CANTON"), CT=info("CT", True, False, "CANTON_TEST"))


@pytest.mark.parametrize("v,expect", [
    (vault(1, "DTCC Demo", asset("ETH", "0")), "Canton/DTCC"),
    (vault(2, "plain", asset("CANTON", "0")), "Canton asset"),
    (vault(3, "plain", asset("CT", "0")), "Canton asset"),
    (dict(vault(4, "plain", asset("ETH", "1")), tags=[{"label": "Keep-For-Demo"}]), "protected"),
    (vault(5, "in policy", asset("ETH", "1")), "TAP"),
    (vault(6, "plain", asset("ETH", "1")), ""),
])
def test_hold_reasons(v, expect):
    why = V.hold_reason(v, CINFOS, _protect(), {"vault:5"})
    assert (expect in why) if expect else why == ""


def test_held_vault_is_excluded_from_the_sweep_plan():
    vs = [vault(1, "DTCC Demo", asset("ETH", "0.001")), vault(2, "ok", asset("ETH", "0.001"))]
    out = {p.vault["id"]: p for p in V.plan_vaults(vs, "386", INFOS, PRICES, D(10), holds={"1": "Canton/DTCC vault"})}
    assert out["1"].excluded == "Canton/DTCC vault" and out["1"].items == [] and not out["2"].excluded


def hide_run(make_client, vs, live=None, **kw):
    r = {("vaults", "get_vault_account"): lambda vault_account_id: next(v for v in (live or vs) if v["id"] == vault_account_id),
         ("vaults", "hide_vault_account"): lambda **k: {}}
    cl, sdk = make_client(r, dry_run=kw.pop("dry_run", False))
    return V.run_hide(cl, vs, INFOS, PRICES, D(25), **kw), sdk


def test_hide_empty_and_dusty_vaults_but_not_over_cap_unpriced_or_already_hidden(make_client):
    vs = [vault(1, "empty", asset("ETH", "0")), vault(2, "dust", asset("ETH", "0.000001")),
          vault(3, "big", asset("ETH", "0.1")), vault(4, "unpriced", asset("UNK", "1")),
          dict(vault(5, "gone", asset("ETH", "0")), hiddenOnUI=True)]
    outs, sdk = hide_run(make_client, vs)
    assert [o.status for o in outs] == ["hidden", "hidden", "skipped", "skipped", "skipped"]
    assert [c[2]["vault_account_id"] for c in sdk.calls("hide_vault_account")] == ["1", "2"]


def test_hide_is_a_dry_run_by_default_and_respects_limit(make_client):
    vs = [vault(i, f"v{i}", asset("ETH", "0")) for i in (1, 2, 3)]
    outs, sdk = hide_run(make_client, vs, dry_run=True)
    assert [o.status for o in outs] == ["dry-run"] * 3 and sdk.calls("hide_vault_account") == []
    outs, sdk = hide_run(make_client, vs, limit=2)
    assert len(sdk.calls("hide_vault_account")) == 2


DEP = dict(INFOS, DEPR=V.AssetInfo("DEPR", True, True, "DEPR", deprecated=True),
           DEPRMAIN=V.AssetInfo("DEPRMAIN", False, True, "DEPRMAIN", deprecated=True),
           OLDT=V.AssetInfo("OLDT", True, True, "OLDT"))
DEP_PRICES = dict(PRICES, DEPR=None, DEPRMAIN=None, OLDT=None)


def test_unpriced_deprecated_testnet_asset_counts_as_zero_and_stays_in_the_vault():
    ps = {p.vault["id"]: p for p in V.plan_vaults(
        [vault(1, "a", asset("DEPR", "5"), asset("USDC", "2")), vault(2, "b", asset("DEPR", "5"), asset("USDC", "30")),
         vault(3, "c", asset("DEPRMAIN", "5")), vault(4, "d", asset("OLDT", "5"))], "386", DEP, DEP_PRICES, D(25))}
    assert not ps["1"].excluded and ps["1"].usd == D(2)
    assert {i.asset: bool(i.skip) for i in ps["1"].items} == {"DEPR": True, "USDC": False}
    assert ps["2"].excluded.startswith("over cap")
    assert "no USD price" in ps["3"].excluded and "no USD price" in ps["4"].excluded


def test_hide_values_deprecated_testnet_as_zero_but_not_other_unpriced(make_client):
    r = {("vaults", "hide_vault_account"): {}}
    vs = [vault(1, "a", asset("DEPR", "5")), vault(2, "b", asset("DEPRMAIN", "5")), vault(3, "c", asset("OLDT", "5"))]
    r[("vaults", "get_vault_account")] = lambda **kw: next(v for v in vs if v["id"] == kw["vault_account_id"])
    cl, sdk = make_client(r, dry_run=False)
    outs = V.run_hide(cl, vs, DEP, DEP_PRICES, D(25))
    assert [o.status for o in outs] == ["hidden", "skipped", "skipped"]
