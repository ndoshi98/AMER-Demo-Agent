import json
from pathlib import Path

from fbcleanup import tap_draft as T

R1 = {"name": "a", "type": "TRANSFER", "verdict": {"action": "ALLOW"}, "policyEngineVersion": "v2"}
R2 = {"name": "b", "type": "TRANSFER", "verdict": {"action": "BLOCK"}, "policyEngineVersion": "v2"}


def draft(rules):
    return {"draftResponse": {"status": "x", "rules": rules, "draftId": "d", "metadata": {}}, "validation": {}}


def active(rules):
    return {"policy": {"rules": rules, "metadata": {}}, "validation": {}}


def test_underscore_keys_are_stripped_at_every_depth():
    r = {"name": "a", "_group": "g", "x": {"_note": 1, "y": [{"_z": 1, "k": 2}]}}
    assert T.strip_annotations(r) == {"name": "a", "x": {"y": [{"k": 2}]}}


def test_empty_draft_is_ok():
    assert T.precheck(draft([]), active([R1]))[0]


def test_draft_identical_to_active_is_ok_even_with_server_ids_and_nulls():
    a = [dict(R1, id="1", extra=None)]
    assert T.precheck(draft([dict(R1, id="1")]), active(a))[0]


def test_draft_that_differs_from_active_is_refused():
    ok, why = T.precheck(draft([R1, R2]), active([R1]))
    assert not ok and "editing" in why


def test_draft_already_holding_this_proposal_may_be_rewritten_but_not_a_different_one():
    assert T.precheck(draft([R1, R2]), active([R1]), [R1, R2])[0]
    assert not T.precheck(draft([R2, R1]), active([R1]), [R1, R2])[0]
    assert not T.precheck(draft([R1, R2]), active([R1]))[0]


def test_unreadable_draft_or_active_is_refused():
    assert not T.precheck({"error": 1}, active([R1]))[0]
    assert not T.precheck(draft([]), {"error": 1})[0]
    assert not T.precheck(None, None)[0]


def test_proposal_validation():
    assert T.problems_in_proposal([R1, R2]) == []
    assert T.problems_in_proposal([])
    assert T.problems_in_proposal([R1, R1])
    assert T.problems_in_proposal([{"name": "a"}])
    assert T.problems_in_proposal([dict(R1, type="MINT")])


def test_readback_compares_count_and_order():
    assert T.readback_problems([R1, R2], draft([R1, R2])) == []
    assert T.readback_problems([R1, R2], draft([R1]))
    assert T.readback_problems([R1, R2], draft([R2, R1]))
    assert T.readback_problems([R1], {})


def test_script_never_references_a_publish_op():
    src = Path(__file__).resolve().parent.parent / "scripts" / "write_tap_draft.py"
    assert "publish_draft" not in src.read_text().replace("NEVER PUBLISHES", "")
