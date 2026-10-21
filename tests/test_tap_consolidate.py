import copy
import itertools
import random
from decimal import Decimal

import pytest

from fbcleanup import tap_consolidate as C


def rule(name, action="ALLOW", assets=("*",), amount=None, addr="*", src=("*",), signer="A", extra=None):
    r = {"id": f"id-{name}", "name": name, "type": "TRANSFER", "initiator": "*", "policyEngineVersion": "v2",
         "externalDescriptor": "{}",
         "asset": {"operator": "INCLUDES", "assetTypes": list(assets), "nftTransfer": False},
         "source": {"type": list(src), "operator": "INCLUDES"},
         "destination": {"type": ["*"], "version": 1, "operator": "INCLUDES", "addressType": addr},
         "verdict": {"action": action}}
    if action == "ALLOW":
        r["verdict"]["designatedSigners"] = {"type": "SINGLE", "users": [signer]}
    if amount is not None:
        lo, hi = amount
        rng = {}
        if lo is not None:
            rng["min"] = str(lo)
        if hi is not None:
            rng["max"] = str(hi)
        r["amount"] = {"range": rng, "currency": "USD"}
    r.update(extra or {})
    return r


def names(plan):
    return [r["name"] for r in plan.rules]


def test_testnet_shaped_policy_block_catchall_only_removable_under_default_deny():
    rules = [rule("<250k", amount=(0, 249999)), rule(">250k", amount=(250000, None), signer="B"),
             rule("whitelisted", addr="WHITELISTED", signer="C"), rule("bad rule delete", action="BLOCK")]
    strict, deny = C.consolidate(rules), C.consolidate(rules, default_deny=True)
    assert names(strict) == ["<250k", ">250k", "whitelisted", "bad rule delete"] and strict.steps == []
    assert names(deny) == ["<250k", ">250k", "whitelisted"]
    assert deny.steps[0].kind == "REMOVE" and "blocked" in deny.steps[0].detail
    assert strict.gaps == []                       # the BLOCK catch-all covers everything


def test_coverage_gap_between_amount_ranges_is_reported():
    plan = C.consolidate([rule("lo", amount=(0, 249999)), rule("hi", amount=(250000, None), signer="B")])
    assert plan.gap_total == 1 and "between 249999 and 250000 USD" in plan.gaps[0]


def test_duplicate_and_shadowed_rules_are_removed_in_strict_mode():
    rules = [rule("a", assets=("BTC",)), rule("dup", assets=("BTC",)), rule("all"), rule("never", assets=("ETH",), action="BLOCK")]
    plan = C.consolidate(rules)
    assert names(plan) == ["all"]
    assert all(s.kind == "REMOVE" for s in plan.steps)


def test_merge_differing_only_in_asset_unions_the_asset_list():
    plan = C.consolidate([rule("btc", assets=("BTC",)), rule("eth", assets=("ETH",))])
    assert len(plan.rules) == 1 and plan.rules[0]["asset"]["assetTypes"] == ["BTC", "ETH"]
    assert plan.steps[0].kind == "MERGE" and "id" not in plan.rules[0] and plan.rules[0]["name"] == "btc + eth"


def test_merge_is_placed_where_it_preserves_precedence():
    rules = [rule("A", amount=(0, 10)), rule("B", action="BLOCK", amount=(15, 20)), rule("C", amount=(5, 20))]
    plan = C.consolidate(rules)
    assert names(plan) == ["B", "A + C"]


def test_overlapping_asset_values_block_a_merge_across_an_in_between_rule():
    rules = [rule("btc", assets=("BTC",)), rule("block-small-eth", action="BLOCK", assets=("ETH",), amount=(0, 10)),
             rule("eth", assets=("ETH",))]
    assert names(C.consolidate(rules)) == ["btc", "block-small-eth", "eth"]


def test_overlapping_amount_ranges_merge_but_gapped_ranges_do_not():
    merged = C.consolidate([rule("lo", amount=(0, 100)), rule("hi", amount=(50, None))])
    assert len(merged.rules) == 1 and merged.rules[0]["amount"] == {"range": {"min": "0"}, "currency": "USD"}
    gapped = C.consolidate([rule("lo", amount=(0, 100)), rule("hi", amount=(200, None))])
    assert len(gapped.rules) == 2


def test_different_signers_never_merge():
    assert len(C.consolidate([rule("x", assets=("BTC",), signer="A"), rule("y", assets=("ETH",), signer="B")]).rules) == 2


def test_opaque_rule_is_a_barrier_nothing_moves_across():
    opaque = rule("excl", assets=("BTC",))
    opaque["asset"]["operator"] = "EXCLUDES"
    plan = C.consolidate([rule("btc", assets=("BTC",)), opaque, rule("eth", assets=("ETH",))])
    assert names(plan) == ["btc", "excl", "eth"] and plan.opaque and plan.opaque[0][1] == "excl"


def test_unrecognised_field_and_legacy_shape_are_opaque():
    assert C.parse(0, rule("x", extra={"somethingNew": {"a": 1}})).opaque
    assert C.parse(0, {"name": "old", "operator": "x"}).opaque
    assert C.parse(0, rule("x", extra={"amountOverTime": {"x": 1}})).opaque


def test_too_many_distinct_groups_skips_consolidation_without_changing_anything():
    rules = [rule(f"r{i}", assets=(f"A{i}",), signer=str(i)) for i in range(12)]
    plan = C.consolidate(rules)
    assert plan.rules == rules and plan.note.startswith("Consolidation skipped")


def test_input_rules_are_never_mutated():
    rules = [rule("btc", assets=("BTC",)), rule("eth", assets=("ETH",))]
    before = copy.deepcopy(rules)
    C.consolidate(rules)
    assert rules == before


# --- brute-force ground truth ---------------------------------------------------------------------
ASSETS = ["A", "B", "C", "D"]
ADDRS = ["WHITELISTED", "OTHER"]
AMOUNTS = [Decimal(x) for x in ("0", "1", "5", "9.5", "10", "15", "20", "25", "30", "100")]


def truth(rules, tx, default):
    asset, addr, amt = tx
    for r in rules:
        if "*" not in r["asset"]["assetTypes"] and asset not in r["asset"]["assetTypes"]:
            continue
        if r["destination"]["addressType"] not in ("*", addr):
            continue
        rng = (r.get("amount") or {}).get("range")
        if rng:
            if "min" in rng and amt < Decimal(rng["min"]):
                continue
            if "max" in rng and amt > Decimal(rng["max"]):
                continue
        v = r["verdict"]
        return "BLOCK" if v["action"] == "BLOCK" else repr(v)
    return default


def random_rule(rng, i):
    assets = ("*",) if rng.random() < 0.3 else tuple(rng.sample(ASSETS, rng.randint(1, 3)))
    lo = rng.choice([None, 0, 5, 10, 20])
    hi = rng.choice([None, 10, 20, 30])
    if lo is not None and hi is not None and lo > hi:
        lo, hi = hi, lo
    amount = None if lo is None and hi is None else (lo, hi)
    return rule(f"r{i}", action=rng.choice(["ALLOW", "ALLOW", "BLOCK"]), assets=assets, amount=amount,
                addr=rng.choice(["*", "*", "WHITELISTED"]), signer=rng.choice(["A", "B"]))


@pytest.mark.parametrize("deny", [False, True])
def test_random_policies_consolidate_without_changing_behaviour(deny):
    default = "BLOCK" if deny else "<NOMATCH>"
    txs = list(itertools.product(ASSETS, ADDRS, AMOUNTS))
    shrunk = 0
    for seed in range(120):
        rng = random.Random(seed)
        rules = [random_rule(rng, i) for i in range(rng.randint(2, 7))]
        plan = C.consolidate(rules, default_deny=deny)
        for tx in txs:
            assert truth(plan.rules, tx, default) == truth(rules, tx, default), (seed, tx)
        shrunk += len(plan.rules) < len(rules)
    assert shrunk > 20


def test_catch_all_rule_shadows_everything_after_it_and_leaves_no_gaps():
    catch = rule("catch-all", amount=(0, None))
    plan = C.consolidate([rule("narrow", assets=("BTC",), signer="B"), catch, rule("later", assets=("ETH",), action="BLOCK"),
                          rule("later2", addr="WHITELISTED", signer="C")])
    assert names(plan) == ["narrow", "catch-all"] and plan.gaps == [] and plan.note == ""
