from fbcleanup.collect import Inventory
from fbcleanup.config import parse_config
from fbcleanup.rules import (classify_tags, classify_users, classify_vaults, evaluated_types, suggest_renames,
                             vault_balances_zero)

NOW = 1_800_000_000
DAY = 86400
CFG = parse_config({"vaults": {"protect": {"name_patterns": ["^Treasury"], "tag_labels": ["keep-for-demo"]}}})


def asset(total="0", **kw):
    return {"id": "BTC_TEST", "total": total, **kw}


def vault(vid="1", name="Empty", assets=None, **kw):
    return {"id": vid, "name": name, "assets": [asset()] if assets is None else assets, **kw}


def inv(**kw):
    i = Inventory(now=NOW, ok={"vault", "activity", "policy", "tag", "user", "audit"})
    for k, v in kw.items():
        setattr(i, k, v)
    return i


def one(decs):
    assert len(decs) == 1
    return decs[0]


# --- zero-balance test -------------------------------------------------------------------------
def test_exactly_zero_everywhere_is_zero():
    assert vault_balances_zero(vault(assets=[asset(), asset(pending="0", staked="0.000")]))[0]
    assert vault_balances_zero(vault(assets=[]))[0]


def test_any_nonzero_field_blocks():
    for field in ("total", "pending", "staked", "available", "frozen", "lockedAmount"):
        a = asset(**({} if field == "total" else {field: "0.00000001"}))
        if field == "total":
            a["total"] = "0.00000001"
        assert not vault_balances_zero(vault(assets=[a]))[0], field


def test_missing_or_garbage_total_is_not_zero():
    assert not vault_balances_zero(vault(assets=[{"id": "X"}]))[0]
    assert not vault_balances_zero(vault(assets=[asset(total="abc")]))[0]


# --- vault classification ----------------------------------------------------------------------
def test_empty_untouched_vault_is_flagged():
    d = one(classify_vaults(inv(vaults=[vault()]), CFG, set()))
    assert d.flagged and d.action == "hide_vault" and d.tier == 1


def test_recent_tx_keeps_vault_old_tx_does_not():
    recent = inv(vaults=[vault()], activity={("vault", "1"): NOW - 10 * DAY})
    old = inv(vaults=[vault()], activity={("vault", "1"): NOW - 200 * DAY})
    assert not one(classify_vaults(recent, CFG, set())).flagged
    assert one(classify_vaults(old, CFG, set())).flagged


def test_protections_keep_vault():
    assert not one(classify_vaults(inv(vaults=[vault(name="Treasury ops")]), CFG, set())).flagged
    tagged = vault(tags=[{"id": "t", "label": "Keep-For-Demo"}])
    assert not one(classify_vaults(inv(vaults=[tagged]), CFG, set())).flagged
    assert not one(classify_vaults(inv(vaults=[vault()]), CFG, {"vault:1"})).flagged
    assert not one(classify_vaults(inv(vaults=[vault(hiddenOnUI=True)]), CFG, set())).flagged


def test_vault_not_evaluated_without_complete_tx_history():
    i = inv(vaults=[vault()])
    i.ok.discard("activity")
    assert "vault" not in evaluated_types(i)


def test_duplicate_names_become_rename_suggestions_only():
    ds = suggest_renames(inv(vaults=[vault("1", "Demo"), vault("2", "demo"), vault("3", "Other")]))
    assert {d.entity_id for d in ds} == {"1", "2"} and all(d.tier == 3 and not d.flagged for d in ds)


# --- tags --------------------------------------------------------------------------------------
def test_tag_in_use_or_protected_is_kept():
    tags = [{"id": "a", "label": "used"}, {"id": "b", "label": "orphan"}, {"id": "c", "label": "p", "isProtected": True},
            {"id": "d", "label": "contacts", "allowedEntityTypes": ["contact"]}]
    ds = {d.entity_id: d for d in classify_tags(inv(tags=tags, vaults=[vault(tags=[{"id": "a"}])]), CFG)}
    assert [k for k, d in ds.items() if d.flagged] == ["b"]


# --- users -------------------------------------------------------------------------------------
def user(uid="u1", role="EDITOR", **kw):
    return {"id": uid, "email": f"{uid}@x.test", "role": role, "enabled": True, "status": "READY",
            "userType": "CONSOLE", **kw}


def users(i, last=None, cov=0, refs=frozenset()):
    return classify_users(i, CFG, set(refs), lambda a: (last or {}).get(a), cov)


def test_inactive_user_flagged_when_coverage_is_long_enough():
    assert one(users(inv(console_users=[user()]), cov=NOW - 365 * DAY)).flagged


def test_user_kept_when_audit_coverage_shorter_than_window():
    assert not one(users(inv(console_users=[user()]), cov=NOW - 30 * DAY)).flagged


def test_recent_activity_by_id_or_email_keeps_user():
    i = inv(console_users=[user()])
    assert not one(users(i, last={"u1": NOW - DAY}, cov=0)).flagged
    assert not one(users(i, last={"u1@x.test": NOW - DAY}, cov=0)).flagged
    assert one(users(i, last={"u1": NOW - 400 * DAY}, cov=0)).flagged


def test_protected_roles_tap_refs_disabled_and_pending_are_kept():
    for u in (user(role="OWNER"), user(role="ADMIN"), user(enabled=False), user(status="PENDING_ACTIVATION")):
        assert not one(users(inv(console_users=[u]), cov=0)).flagged
    assert not one(users(inv(console_users=[user()]), cov=0, refs={"user:u1"})).flagged
    pending = one(users(inv(console_users=[user(status="PENDING_ACTIVATION")]), cov=0))
    assert "revoke the invite" in pending.manual.lower()


def test_connection_name_is_readable_not_a_dict_repr():
    from fbcleanup.rules import classify_connections
    conn = {"id": "c1", "localNetworkId": {"id": "1", "name": "Mine"}, "remoteNetworkId": {"id": "2", "name": "LP"}}
    assert one(classify_connections(inv(network_connections=[conn]), CFG, set())).name == "Mine <-> LP"


# --- connections: flag only on positive evidence of disuse --------------------------------------
def _conn(i):
    from fbcleanup.rules import classify_connections
    return one(classify_connections(i, CFG, set()))


def test_connection_with_no_tx_and_short_history_is_kept_and_listed_for_manual_decision():
    d = _conn(inv(network_connections=[{"id": "c1"}], history_start=NOW - 203 * DAY))
    assert not d.flagged and "decide by hand" in d.manual and "unproven" in d.keep_reasons[0]


def test_connection_with_no_tx_and_no_visible_history_at_all_is_not_flagged():
    d = _conn(inv(network_connections=[{"id": "c1"}], history_start=None))
    assert not d.flagged and d.manual


def test_connection_flagged_when_history_covers_full_window_and_no_tx():
    d = _conn(inv(network_connections=[{"id": "c1"}], history_start=NOW - 380 * DAY,
                  activity={("network_connection", "c1"): NOW - 370 * DAY}))
    assert d.flagged and not d.manual


def test_connection_with_tx_inside_window_is_kept_even_with_full_history():
    d = _conn(inv(network_connections=[{"id": "c1"}], history_start=NOW - 380 * DAY,
                  activity={("network_connection", "c1"): NOW - 154 * DAY}))
    assert not d.flagged and not d.manual
