import json

import pytest

from fbcleanup import client as C
from fbcleanup.config import ConfigError, enforce_prod_gate, parse_config


def test_dry_run_suppresses_mutations_but_not_reads(make_client, tmp_path):
    cl, sdk = make_client({("vaults", "hide_vault_account"): {}, ("vaults", "get_vault_account"): {"id": "1"}})
    assert cl.call("hide_vault", vault_account_id="1") is C.DRY_RUN
    assert sdk.calls("hide_vault_account") == []
    assert cl.call("get_vault", vault_account_id="1") == {"id": "1"}
    entry = json.loads((tmp_path / "journal.jsonl").read_text().splitlines()[0])
    assert entry["outcome"] == "dry_run" and entry["op"] == "hide_vault"


def test_live_mutation_is_executed_and_journaled(make_client, tmp_path):
    cl, sdk = make_client({("vaults", "hide_vault_account"): {}}, dry_run=False)
    cl.call("hide_vault", vault_account_id="7")
    assert sdk.calls("hide_vault_account")[0][2] == {"vault_account_id": "7"}
    assert json.loads((tmp_path / "journal.jsonl").read_text())["outcome"] == "ok"


def test_unknown_op_is_refused(make_client):
    cl, _ = make_client()
    with pytest.raises(KeyError):
        cl.call("publish_policy_draft")


def test_no_policy_write_op_exists():
    for op, (_, cands) in C.OPS.items():
        assert not any(frag in name for name in cands for frag in ("draft", "publish", "update_policy")), op
        if "policy" in op:
            assert op not in C.MUTATING_OPS


def test_every_mutating_op_is_in_ops_and_vice_versa_reads_are_not_mutating():
    assert C.MUTATING_OPS <= set(C.OPS)
    assert all(not op.startswith(("list_", "get_")) for op in C.MUTATING_OPS)


def test_resolution_failure_lists_what_sdk_exposes(make_client):
    cl, _ = make_client({("vaults", "something_else"): {}})
    with pytest.raises(C.OpResolutionError) as e:
        cl.call("list_vaults", limit=1)
    assert "something_else" in str(e.value)


def test_prod_requires_confirmation_in_client():
    with pytest.raises(PermissionError):
        C.FireblocksClient(object(), is_production=True, confirmed_prod=False)


def test_reads_retry_on_429_but_mutations_never_retry(make_client):
    class Boom(Exception):
        status = 429
        body = ""

    attempts = {"n": 0}

    def flaky(**kw):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise Boom()
        return {"ok": True}

    cl, sdk = make_client({("workspace", "get_workspace"): flaky, ("vaults", "hide_vault_account"): flaky}, dry_run=False)
    cl._sleep = lambda s: None
    assert cl.call("get_workspace") == {"ok": True} and attempts["n"] == 3
    attempts["n"] = 0
    with pytest.raises(C.ApiError):
        cl.call("hide_vault", vault_account_id="1")
    assert attempts["n"] == 1


@pytest.mark.parametrize("body,needle", [
    ('{"code": 2760, "message": "x"}', "Settings > Quorums"),
    ('{"code": "USER_REFERENCED_IN_TAP", "message": "x"}', "TAP policy"),
    ('{"code": "USER_PENDING_ONBOARDING", "message": "x"}', "Revoke the invite"),
])
def test_known_errors_are_translated_and_never_suggest_force(body, needle):
    class E(Exception):
        status = 409

    e = E("boom")
    e.body = body
    err = C.translate_error(e)
    assert needle in str(err)
    assert "force" not in str(err).lower()


# --- config ------------------------------------------------------------------------------------
def test_unknown_config_keys_rejected():
    with pytest.raises(ConfigError):
        parse_config({"workspace": {"is_prodution": False}})
    with pytest.raises(ConfigError):
        parse_config({"workspace": {"skip_prod_check": True}})


def test_is_production_must_be_real_boolean():
    with pytest.raises(ConfigError):
        parse_config({"workspace": {"is_production": "false"}})


def test_base_path_is_not_production_signal_and_sandbox_is_valid():
    assert parse_config({"workspace": {"base_path": "us", "is_production": False}}).is_production is False
    assert parse_config({"workspace": {"base_path": "sandbox"}}).base_path == "sandbox"
    with pytest.raises(ConfigError):
        parse_config({"workspace": {"base_path": "mainnet"}})


def test_min_runs_floor_is_two():
    assert parse_config({"quarantine": {"min_consecutive_runs": 1}}).min_consecutive_runs == 2


def test_prod_gate():
    cfg = parse_config({"workspace": {"is_production": True}})
    with pytest.raises(SystemExit):
        enforce_prod_gate(cfg, False)
    enforce_prod_gate(cfg, True)
    enforce_prod_gate(parse_config({}), False)
