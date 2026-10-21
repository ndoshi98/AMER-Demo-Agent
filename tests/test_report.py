from fbcleanup.collect import Inventory
from fbcleanup.config import parse_config
from fbcleanup.report import build_report
from fbcleanup.rules import SUGGEST_RENAME, Decision
from fbcleanup.state import State

NOW = 1_800_000_000


def report(observed=True):
    decs = [Decision("vault", vid, "CFXN-USER-bob", SUGGEST_RENAME, False, [], [], "name is shared by 2 vaults")
            for vid in ("1", "2")]
    return build_report(run_id="r", now=NOW, mode="audit", executed=False, cfg=parse_config({}), workspace_name="ws",
                        inv=Inventory(now=NOW), decisions=decs, state=State(":memory:"), tier1=[], apply_outcomes=[],
                        poll_outcomes=[], proposal_path=None, tap_rules=[], tap_findings=[], tap_proposal_path=None,
                        observed=observed)


def test_identical_manual_actions_are_listed_once():
    assert report().count("- CFXN-USER-bob: name is shared by 2 vaults") == 1


def test_no_observe_report_does_not_claim_quarantine():
    assert "NOT observed this run" in report(observed=False) and "Still in quarantine" not in report(observed=False)
    assert "Still in quarantine" in report(observed=True)


def test_vault_ids_appear_only_in_the_rename_list_where_names_collide():
    text = report()
    assert "(vault id 1," in text and "(vault id 2," in text
    assert "ids 1, 2" not in text
