from fbcleanup.tap import (DEAD_REF, DUPLICATE, MERGEABLE, SHADOWED, Match, Rule, analyze, propose,
                           rules_from_policy)

STAR = Match.star()


def mk(index, verdict="ALLOW", refs=(), opaque=False, **dims):
    base = {"initiator": STAR, "source": STAR, "destination": STAR, "address_type": STAR, "asset": STAR,
            "amount": Match.of(["any"], atomic=True), "operation": Match.of(["TRANSFER"], atomic=True)}
    base.update({k: (v if isinstance(v, Match) else Match.of(v)) for k, v in dims.items()})
    return Rule(index, f"r{index}", base, verdict, frozenset(refs), {"id": f"raw{index}"}, opaque)


def kinds(findings):
    return [(f.kind, f.rule, f.safe) for f in findings]


# --- Match primitives --------------------------------------------------------------------------
def test_match_covers_and_union():
    a, b = Match.of(["x", "y"]), Match.of(["x"])
    assert a.covers(b) and not b.covers(a)
    assert STAR.covers(a) and not a.covers(STAR)
    assert a.union(Match.of(["z"])).tokens == frozenset({"x", "y", "z"})
    assert a.union(STAR).universal


def test_atomic_never_unions_or_partially_covers():
    a = Match.of(["p"], atomic=True)
    assert a.covers(Match.of(["p"], atomic=True)) and not a.covers(Match.of(["q"], atomic=True))


# --- each finding type -------------------------------------------------------------------------
def test_duplicate_detected_and_safe():
    f = analyze([mk(0, asset=["BTC"]), mk(1, asset=["BTC"])])
    assert kinds(f) == [(DUPLICATE, 1, True)]


def test_shadowed_same_verdict_is_safe():
    f = analyze([mk(0), mk(1, asset=["BTC"])])  # rule 0 is all-assets; rule 1 can never fire
    assert kinds(f) == [(SHADOWED, 1, True)]


def test_shadowed_with_differing_verdict_marked_unsafe_and_loud():
    f = analyze([mk(0, verdict="ALLOW"), mk(1, verdict="BLOCK", asset=["BTC"])])
    assert kinds(f) == [(SHADOWED, 1, False)]
    assert "DIFFERS" in f[0].summary


def test_identical_dims_but_different_verdict_is_unsafe_shadow_not_duplicate():
    f = analyze([mk(0, verdict="ALLOW", asset=["BTC"]), mk(1, verdict="BLOCK", asset=["BTC"])])
    assert kinds(f) == [(SHADOWED, 1, False)]


def test_dead_ref_detected_for_missing_and_disabled_users_and_missing_vault():
    rules = [mk(0, refs=["user:gone"], asset=["BTC"]), mk(1, refs=["user:off"], asset=["ETH"]),
             mk(2, refs=["vault:9"], asset=["SOL"]), mk(3, refs=["user:ok", "vault:1"], asset=["XRP"])]
    f = [x for x in analyze(rules, {"ok": True, "off": False}, {"1"}) if x.kind == DEAD_REF]
    assert sorted((x.rule, x.safe) for x in f) == [(0, False), (1, False), (2, False)]


def test_dead_ref_check_skipped_when_inventory_unknown():
    assert analyze([mk(0, refs=["user:gone"])], None, None) == []


def test_mergeable_same_verdict_one_dimension():
    f = analyze([mk(0, asset=["BTC"]), mk(1, asset=["ETH"])])
    assert kinds(f) == [(MERGEABLE, 1, False)] and f[0].dimension == "asset"


def test_not_mergeable_when_verdicts_differ_or_two_dimensions_differ():
    assert analyze([mk(0, asset=["BTC"]), mk(1, verdict="BLOCK", asset=["ETH"])]) == []
    assert analyze([mk(0, asset=["BTC"], source=["a"]), mk(1, asset=["ETH"], source=["b"])]) == []


def test_mergeable_flags_in_between_precedence_hazard():
    rules = [mk(0, asset=["BTC"]), mk(1, verdict="BLOCK", asset=["ETH"]), mk(2, asset=["ETH", "SOL"])]
    m = [x for x in analyze(rules) if x.kind == MERGEABLE]
    assert m and m[0].hazards == [1]


def test_opaque_in_between_is_always_a_hazard_and_never_a_shadower():
    rules = [mk(0, asset=["BTC"]), mk(1, opaque=True), mk(2, asset=["ETH"])]
    m = [x for x in analyze(rules) if x.kind == MERGEABLE]
    assert m and m[0].hazards == [1]
    assert analyze([mk(0, opaque=True), mk(1, asset=["BTC"])]) == []  # opaque cannot shadow


def test_findings_sorted_safe_first():
    rules = [mk(0, asset=["BTC"]), mk(1, asset=["ETH"]), mk(2, asset=["ETH"], source=["a"]),
             mk(3, asset=["BTC"], refs=["user:gone"])]
    f = analyze(rules, {}, None)
    safe_flags = [x.safe for x in f]
    assert safe_flags == sorted(safe_flags, reverse=True)


# --- proposed rule set -------------------------------------------------------------------------
def test_proposal_keeps_first_of_duplicate_pair():
    rules = [mk(0, asset=["BTC"]), mk(1, asset=["BTC"])]
    p = propose(rules, analyze(rules))
    assert p["rules"] == [{"id": "raw0"}] and p["dropped"][0]["index"] == 1


def test_proposal_drops_same_verdict_shadow_but_never_differing_verdict():
    rules = [mk(0), mk(1, asset=["BTC"]), mk(2, verdict="BLOCK", asset=["ETH"])]
    p = propose(rules, analyze(rules))
    assert [r["id"] for r in p["rules"]] == ["raw0", "raw2"]
    assert [d["index"] for d in p["dropped"]] == [1]


def test_proposal_never_drops_mergeable_dead_ref_or_opaque():
    rules = [mk(0, asset=["BTC"]), mk(1, asset=["ETH"], refs=["user:gone"]), mk(2, opaque=True)]
    p = propose(rules, analyze(rules, {}, None))
    assert len(p["rules"]) == 3 and p["dropped"] == []


# --- adapters ----------------------------------------------------------------------------------
def legacy(action="ALLOW", asset="BTC", users=("u1",), dst=None, **extra):
    r = {"type": "TRANSFER", "action": action, "asset": asset, "operators": {"users": list(users)},
         "src": {"ids": [["*"]]}, "dst": {"ids": dst or [["*"]]}, "amountCurrency": "USD", "amountScope": "SINGLE_TX",
         "amount": "0", "periodSec": 0, "dstAddressType": "*", "authorizationGroups": None}
    r.update(extra)
    return r


def test_legacy_adapter_end_to_end_duplicate_and_shadow():
    rules = rules_from_policy({"rules": [legacy(), legacy(), legacy(asset="*"), legacy(asset="ETH")]})
    f = analyze(rules)
    assert (DUPLICATE, 1, True) in kinds(f)   # rule 1 repeats rule 0
    assert (SHADOWED, 3, True) in kinds(f)    # rule 3 (ETH) is covered by earlier rule 2 (all assets)


def test_legacy_address_type_prevents_false_duplicate():
    rules = rules_from_policy({"rules": [legacy(dstAddressType="WHITELISTED"), legacy(dstAddressType="ONE_TIME")]})
    f = analyze(rules)
    assert not any(x.safe for x in f)                         # never a behaviour-preserving removal
    assert [x.kind for x in f] == [MERGEABLE] and f[0].dimension == "address_type"


def test_legacy_different_approvers_are_different_verdicts():
    g1 = {"logic": "AND", "groups": [{"users": ["a"], "th": 1}]}
    g2 = {"logic": "AND", "groups": [{"users": ["b"], "th": 1}]}
    rules = rules_from_policy({"rules": [legacy(authorizationGroups=g1), legacy(authorizationGroups=g2)]})
    assert all(not f.safe for f in analyze(rules))


def test_legacy_refs_extracted_including_approvers_and_vaults():
    g = {"logic": "AND", "groups": [{"users": ["approver"], "th": 1}]}
    r = rules_from_policy({"rules": [legacy(authorizationGroups=g, dst=[["VAULT", "7"]])]})[0]
    assert {"user:u1", "user:approver", "vault:7"} <= r.refs


def test_legacy_without_operators_block_is_opaque():
    raw = legacy()
    del raw["operators"]
    assert rules_from_policy({"rules": [raw]})[0].opaque


def test_v2_excludes_operator_is_opaque_and_equal_rules_are_duplicates():
    base = {"name": "a", "id": "1", "policyEngineVersion": "v2", "type": "TRANSFER", "initiator": "*",
            "source": {"ids": [{"type": "VAULT", "id": "3"}], "operator": "INCLUDES"},
            "destination": {"addressType": "*", "operator": "INCLUDES"},
            "verdict": {"action": "ALLOW"}}
    excl = dict(base, name="b", id="2", source={"ids": [], "operator": "EXCLUDES"})
    rules = rules_from_policy({"policy": {"rules": [base, dict(base, name="c", id="3"), excl]}})
    assert rules[2].opaque and not rules[0].opaque
    assert (DUPLICATE, 1, True) in kinds(analyze(rules))
    assert "vault:3" in rules[0].refs
