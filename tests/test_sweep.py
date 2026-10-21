import json
from decimal import Decimal

import pytest

from fbcleanup import sweep as S
from fbcleanup.client import DRY_RUN

AMT = Decimal("0.001")
FEE = {"low": {"networkFee": "0.000003"}, "medium": {"networkFee": "0.0000039"}, "high": {"networkFee": "0.000005"}}


def asset(aid, total, available=None, **extra):
    a = {"id": aid, "total": total, "available": total if available is None else available,
         "pending": "0", "frozen": "0", "lockedAmount": "0", "staked": "0"}
    a.update(extra)
    return a


def vault(vid, name, *assets):
    return {"id": str(vid), "name": name, "assets": list(assets)}


def test_eligibility_requires_exact_amount_sole_asset_and_all_available():
    assert S.is_eligible(vault(1, "a", asset("ETH", "0.001"), asset("USDC", "0")), "ETH", AMT)
    assert S.is_eligible(vault(1, "a", asset("ETH", "0.0010")), "ETH", AMT)
    assert not S.is_eligible(vault(1, "a", asset("ETH", "0.0001")), "ETH", AMT)
    assert not S.is_eligible(vault(1, "a", asset("ETH", "0.0011")), "ETH", AMT)
    assert not S.is_eligible(vault(1, "a", asset("ETH", "0.001"), asset("HEX", "5")), "ETH", AMT)
    assert not S.is_eligible(vault(1, "a", asset("ETH", "0.001", frozen="0.0000001", available="0.0009999")), "ETH", AMT)
    assert not S.is_eligible(vault(1, "a", asset("ETH", "0.001", staked="0.001")), "ETH", AMT)
    assert not S.is_eligible(vault(1, "a", asset("ETH", "0.0010001", available="0.001", frozen="0.0000001")), "ETH", AMT)
    assert not S.is_eligible(vault(1, "a", asset("ETH", "junk")), "ETH", AMT)
    assert not S.is_eligible(vault(1, "a"), "ETH", AMT)


def test_select_sources_excludes_destination_and_sorts_numerically():
    vs = [vault(10, "j", asset("ETH", "0.001")), vault(9, "i", asset("ETH", "0.001")),
          vault(386, "Treasury Demo", asset("ETH", "0.001")), vault(2, "x", asset("ETH", "0.5"))]
    assert [v["id"] for v in S.select_sources(vs, "ETH", AMT, "386")] == ["9", "10"]


def test_destination_must_match_exactly_one():
    vs = [vault(1, "Treasury Demo"), vault(2, "Treasury Demo"), vault(3, "Other")]
    with pytest.raises(S.SweepError):
        S.find_destination(vs, "Treasury Demo")
    with pytest.raises(S.SweepError):
        S.find_destination(vs, "treasury demo")
    assert S.find_destination(vs, "Other")["id"] == "3"


def test_request_is_gross_vault_to_vault():
    r = S.build_request("ETH", AMT, "5", "386", "MEDIUM", "n")
    assert r["treatAsGrossAmount"] is True and r["amount"] == "0.001"
    assert r["source"] == {"type": "VAULT_ACCOUNT", "id": "5"} and r["destination"] == {"type": "VAULT_ACCOUNT", "id": "386"}


def responses(src, tx_statuses=("COMPLETED",)):
    seq = iter(tx_statuses)
    return {("vaults", "get_vault_account"): src, ("transactions", "estimate_transaction_fee"): FEE,
            ("transactions", "create_transaction"): lambda **kw: {"id": "tx1", "status": "SUBMITTED"},
            ("transactions", "get_transaction"): lambda **kw: {"id": "tx1", "status": next(seq)}}


def run(make_client, src, live=None, dry_run=False, statuses=("COMPLETED",), **kw):
    cl, sdk = make_client(responses(live or src, statuses), dry_run=dry_run, run_id="sweep-1")
    return S.sweep_one(cl, src, "386", "ETH", AMT, "sweep-1", sleep=lambda s: None, **kw), sdk


def test_dry_run_sends_nothing_but_estimates_the_fee(make_client):
    v = vault(5, "v", asset("ETH", "0.001"))
    o, sdk = run(make_client, v, dry_run=True)
    assert o.status == "dry-run" and o.fee == Decimal("0.0000039")
    assert sdk.calls("create_transaction") == []


def test_live_sweep_sends_once_with_idempotency_key_and_waits_for_completion(make_client, tmp_path):
    v = vault(5, "v", asset("ETH", "0.001"))
    o, sdk = run(make_client, v, statuses=("CONFIRMING", "COMPLETED"))
    assert o.status == "completed" and o.tx_id == "tx1"
    sent = sdk.calls("create_transaction")
    assert len(sent) == 1 and sent[0][2]["idempotency_key"] == "sweep-sweep-1-5"
    body = sent[0][2]["transaction_request"].to_dict()
    assert body["assetId"] == "ETH" and body["treatAsGrossAmount"] is True
    journal = [json.loads(line) for line in (tmp_path / "journal.jsonl").read_text().splitlines()]
    assert journal[-1]["op"] == "create_transaction" and journal[-1]["outcome"] == "ok"
    assert journal[-1]["kwargs"]["transaction_request"]["destination"]["id"] == "386"


def test_balance_changed_since_listing_is_skipped_not_sent(make_client):
    listed = vault(5, "v", asset("ETH", "0.001"))
    live = vault(5, "v", asset("ETH", "0.0005"))
    o, sdk = run(make_client, listed, live=live)
    assert o.status == "skipped" and sdk.calls("create_transaction") == []


def test_fee_too_high_is_skipped(make_client):
    v = vault(5, "v", asset("ETH", "0.001"))
    o, sdk = run(make_client, v, max_fee_pct=Decimal("0.1"))
    assert o.status == "skipped" and "fee" in o.detail and sdk.calls("create_transaction") == []


def test_unreadable_fee_estimate_fails_closed(make_client):
    v = vault(5, "v", asset("ETH", "0.001"))
    r = responses(v)
    r[("transactions", "estimate_transaction_fee")] = {}
    cl, sdk = make_client(r, dry_run=False)
    o = S.sweep_one(cl, v, "386", "ETH", AMT, "r", sleep=lambda s: None)
    assert o.status == "skipped" and sdk.calls("create_transaction") == []


@pytest.mark.parametrize("final,expected", [("FAILED", "failed"), ("REJECTED", "failed"), ("BLOCKED", "failed")])
def test_terminal_failures(make_client, final, expected):
    o, _ = run(make_client, vault(5, "v", asset("ETH", "0.001")), statuses=(final,))
    assert o.status == expected and not o.ok


def test_pending_authorization_is_reported_as_needing_a_human(make_client):
    o, _ = run(make_client, vault(5, "v", asset("ETH", "0.001")), statuses=("PENDING_AUTHORIZATION",))
    assert o.status == "needs-approval" and not o.ok


def test_run_stops_at_first_failure(make_client):
    vs = [vault(i, f"v{i}", asset("ETH", "0.001")) for i in (1, 2, 3)]
    r = responses(vs[0], ("FAILED",))
    r[("vaults", "get_vault_account")] = lambda vault_account_id: next(v for v in vs if v["id"] == vault_account_id)
    cl, sdk = make_client(r, dry_run=False)
    outs = S.run_sweep(cl, vs, "386", "ETH", AMT, "r", sleep=lambda s: None)
    assert [o.status for o in outs] == ["failed"] and len(sdk.calls("create_transaction")) == 1


def test_create_transaction_is_a_gated_mutation():
    from fbcleanup import client as C
    assert "create_transaction" in C.MUTATING_OPS
    assert "estimate_fee" not in C.MUTATING_OPS
