import json

import yaml

from fbcleanup import actions as A
from fbcleanup import rules as R
from fbcleanup.client import DRY_RUN
from fbcleanup.config import parse_config
from fbcleanup.state import DAY, State

T0 = 1_800_000_000
T1 = T0 + 15 * DAY


def cfg(tmp_path):
    c = parse_config({})
    c.approvals_dir = str(tmp_path / "approvals")
    return c


def vault_decision(vid="1"):
    return R.Decision("vault", vid, "Empty", R.HIDE_VAULT, True, ["empty"])


def wallet_decision(wid="w1", action=R.DELETE_EXTERNAL_WALLET, etype="external_wallet"):
    return R.Decision(etype, wid, "Old CP", action, True, ["unused"])


def quarantined(state, decisions):
    keys = [d.key for d in decisions]
    state.observe("a", T0, keys, {d.entity_type for d in decisions})
    state.observe("b", T1, keys, {d.entity_type for d in decisions})


# --- Tier 1 ------------------------------------------------------------------------------------
def test_tier1_not_eligible_on_first_sighting(make_client, tmp_path):
    st = State(":memory:")
    d = vault_decision()
    st.observe("a", T0, [d.key], {"vault"})
    cl, sdk = make_client({("vaults", "get_vault_account"): {"id": "1", "assets": []}}, dry_run=False)
    assert A.execute_tier1(cl, st, [d], cfg(tmp_path), T0) == []
    assert sdk.calls("hide_vault_account") == []


def test_tier1_dry_run_then_execute_then_undo(make_client, tmp_path):
    st = State(":memory:")
    d = vault_decision()
    quarantined(st, [d])
    resp = {("vaults", "get_vault_account"): {"id": "1", "name": "Empty", "assets": [{"id": "BTC", "total": "0"}]},
            ("vaults", "hide_vault_account"): {}, ("vaults", "unhide_vault_account"): {}}

    cl, sdk = make_client(resp, dry_run=True)
    out = A.execute_tier1(cl, st, [d], cfg(tmp_path), T1)
    assert out[0].status == "would_hide" and sdk.calls("hide_vault_account") == [] and not st.is_acted(d.key)

    cl, sdk = make_client(resp, dry_run=False, run_id="live")
    out = A.execute_tier1(cl, st, [d], cfg(tmp_path), T1)
    assert out[0].status == "hidden" and st.is_acted(d.key)
    assert A.execute_tier1(cl, st, [d], cfg(tmp_path), T1 + DAY) == []  # acted: never re-queued

    undo = A.undo_hides(cl, str(tmp_path / "journal.jsonl"), "live")
    assert [o.status for o in undo] == ["executed"] and undo[0].name == "Empty" and sdk.calls("unhide_vault_account")[0][2] == {"vault_account_id": "1"}


def test_tier1_skips_when_balance_changed_since_classification(make_client, tmp_path):
    st = State(":memory:")
    d = vault_decision()
    quarantined(st, [d])
    cl, sdk = make_client({("vaults", "get_vault_account"): {"id": "1", "assets": [{"id": "BTC", "total": "0.5"}]},
                           ("vaults", "hide_vault_account"): {}}, dry_run=False)
    out = A.execute_tier1(cl, st, [d], cfg(tmp_path), T1)
    assert out[0].status == "skipped" and sdk.calls("hide_vault_account") == []


# --- Tier 2 approval round trip ----------------------------------------------------------------
def approve_all(tmp_path, value=True):
    for f in (tmp_path / "approvals").glob("approvals-*.yaml"):
        doc = yaml.safe_load(f.read_text())
        for it in doc["items"]:
            it["approved"] = value
        f.write_text(yaml.safe_dump(doc))


def test_tier2_never_proposed_before_quarantine_and_never_executed_in_proposing_run(make_client, tmp_path):
    c, st, d = cfg(tmp_path), State(":memory:"), wallet_decision()
    st.observe("a", T0, [d.key], {d.entity_type})
    assert A.propose_tier2(st, [d], c, "a", T0) is None          # first sighting: nothing proposed
    st.observe("b", T1, [d.key], {d.entity_type})
    path = A.propose_tier2(st, [d], c, "b", T1)
    assert path and yaml.safe_load(path.read_text())["items"][0]["approved"] is False

    cl, sdk = make_client({("external_wallets", "delete_external_wallet"): {}}, dry_run=False)
    approve_all(tmp_path)
    out = A.apply_approvals(cl, st, c, "b", T1, {d.key})          # SAME run id as the proposal
    assert out[0].status == "skipped" and sdk.calls("delete_external_wallet") == []


def test_unapproved_and_string_approved_items_never_execute(make_client, tmp_path):
    c, st, d = cfg(tmp_path), State(":memory:"), wallet_decision()
    quarantined(st, [d])
    A.propose_tier2(st, [d], c, "b", T1)
    cl, sdk = make_client({("external_wallets", "delete_external_wallet"): {}}, dry_run=False)
    assert A.apply_approvals(cl, st, c, "later", T1 + DAY, {d.key}) == []
    approve_all(tmp_path, value="true")                          # a string, not a YAML bool
    assert A.apply_approvals(cl, st, c, "later", T1 + DAY, {d.key}) == []
    assert sdk.calls("delete_external_wallet") == []


def test_approved_item_executes_in_a_later_run_once(make_client, tmp_path):
    c, st, d = cfg(tmp_path), State(":memory:"), wallet_decision()
    quarantined(st, [d])
    A.propose_tier2(st, [d], c, "b", T1)
    approve_all(tmp_path)
    cl, sdk = make_client({("external_wallets", "delete_external_wallet"): {}}, dry_run=False)
    out = A.apply_approvals(cl, st, c, "later", T1 + DAY, {d.key})
    assert [o.status for o in out] == ["executed"]
    assert sdk.calls("delete_external_wallet")[0][2] == {"wallet_id": "w1"}
    assert A.apply_approvals(cl, st, c, "later2", T1 + 2 * DAY, {d.key}) == []   # status moved on; no double delete
    assert A.propose_tier2(st, [d], c, "later2", T1 + 2 * DAY) is None            # acted: not re-proposed


def test_approved_item_skipped_if_fresh_classification_no_longer_flags_it(make_client, tmp_path):
    c, st, d = cfg(tmp_path), State(":memory:"), wallet_decision()
    quarantined(st, [d])
    A.propose_tier2(st, [d], c, "b", T1)
    approve_all(tmp_path)
    cl, sdk = make_client({("external_wallets", "delete_external_wallet"): {}}, dry_run=False)
    out = A.apply_approvals(cl, st, c, "later", T1 + DAY, set())   # e.g. wallet used in a tx since
    assert out[0].status == "skipped" and sdk.calls("delete_external_wallet") == []


def test_stale_approval_file_expires(make_client, tmp_path):
    c, st, d = cfg(tmp_path), State(":memory:"), wallet_decision()
    quarantined(st, [d])
    A.propose_tier2(st, [d], c, "b", T1)
    approve_all(tmp_path)
    cl, sdk = make_client({("external_wallets", "delete_external_wallet"): {}}, dry_run=False)
    out = A.apply_approvals(cl, st, c, "later", T1 + 40 * DAY, {d.key})
    assert out[0].status == "expired" and sdk.calls("delete_external_wallet") == []


def test_apply_dry_run_changes_nothing_and_keeps_item_pending(make_client, tmp_path):
    c, st, d = cfg(tmp_path), State(":memory:"), wallet_decision()
    quarantined(st, [d])
    A.propose_tier2(st, [d], c, "b", T1)
    approve_all(tmp_path)
    cl, sdk = make_client({("external_wallets", "delete_external_wallet"): {}}, dry_run=True)
    assert A.apply_approvals(cl, st, c, "later", T1 + DAY, {d.key})[0].status == "would_execute"
    assert not st.is_acted(d.key) and sdk.calls("delete_external_wallet") == []


# --- console-user deletion ---------------------------------------------------------------------
def test_user_deletion_force_is_opt_in_and_completion_needs_enabled_false(make_client, tmp_path):
    c, st = cfg(tmp_path), State(":memory:")
    d = R.Decision("user", "u9", "Old User", R.REQUEST_USER_DELETION, True, ["inactive"])
    quarantined(st, [d])
    A.propose_tier2(st, [d], c, "b", T1)
    approve_all(tmp_path)
    users = {"users": [{"id": "u9", "enabled": True}]}
    cl, sdk = make_client({("console_user", "delete_console_user"): {}, ("console_user", "get_console_users"): lambda **k: users},
                          dry_run=False)
    out = A.apply_approvals(cl, st, c, "later", T1 + DAY, {d.key})
    assert out[0].status == "requested"
    assert "force" not in sdk.calls("delete_console_user")[0][2]          # default: force never sent

    assert A.poll_user_deletions(cl, c, T1 + 2 * DAY)[0].status == "requested"   # still enabled -> not done
    users["users"][0]["enabled"] = False
    assert A.poll_user_deletions(cl, c, T1 + 3 * DAY)[0].status == "executed"


def test_force_flag_is_forwarded_only_when_explicitly_set(make_client, tmp_path):
    c, st = cfg(tmp_path), State(":memory:")
    d = R.Decision("user", "u9", "Old User", R.REQUEST_USER_DELETION, True, ["inactive"])
    quarantined(st, [d])
    A.propose_tier2(st, [d], c, "b", T1)
    approve_all(tmp_path)
    cl, sdk = make_client({("console_user", "delete_console_user"): {}}, dry_run=False)
    A.apply_approvals(cl, st, c, "later", T1 + DAY, {d.key}, force_user_delete=True)
    assert sdk.calls("delete_console_user")[0][2]["force"] is True


def test_api_failure_marks_item_failed_and_hint_never_mentions_force(make_client, tmp_path):
    c, st = cfg(tmp_path), State(":memory:")
    d = R.Decision("user", "u9", "Old User", R.REQUEST_USER_DELETION, True, ["inactive"])
    quarantined(st, [d])
    A.propose_tier2(st, [d], c, "b", T1)
    approve_all(tmp_path)

    class E(Exception):
        status = 409
        body = '{"code": "USER_REFERENCED_IN_TAP"}'

    def boom(**kw):
        raise E("conflict")

    cl, _ = make_client({("console_user", "delete_console_user"): boom}, dry_run=False)
    out = A.apply_approvals(cl, st, c, "later", T1 + DAY, {d.key})
    assert out[0].status == "failed" and "force" not in out[0].detail.lower()
    assert not st.is_acted(d.key)
    f = next((tmp_path / "approvals").glob("*.yaml"))
    assert yaml.safe_load(f.read_text())["items"][0]["status"] == "failed"
