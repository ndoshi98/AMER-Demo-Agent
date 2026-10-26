"""Pure helpers for writing a proposed TAP rule list (one policy type) into the policy DRAFT. Never publishes: a human does that."""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple


def strip_annotations(obj: Any) -> Any:
    """Drop proposal-only keys (those starting with '_', e.g. '_group') at every depth."""
    if isinstance(obj, dict):
        return {k: strip_annotations(v) for k, v in obj.items() if not str(k).startswith("_")}
    if isinstance(obj, list):
        return [strip_annotations(v) for v in obj]
    return obj


def _rules_of(resp: Any, *wrappers: str) -> Optional[List[Dict[str, Any]]]:
    cur = resp
    for w in wrappers:
        cur = cur.get(w) if isinstance(cur, dict) else None
    rules = cur.get("rules") if isinstance(cur, dict) else None
    return rules if isinstance(rules, list) else None


def active_rules(resp: Any) -> Optional[List[Dict[str, Any]]]:
    return _rules_of(resp, "policy")


def draft_rules(resp: Any) -> Optional[List[Dict[str, Any]]]:
    return _rules_of(resp, "draftResponse")


def _canon(rule: Dict[str, Any]) -> Any:
    def clean(o: Any) -> Any:
        if isinstance(o, dict):
            return {k: clean(v) for k, v in o.items() if v is not None}
        if isinstance(o, list):
            return [clean(v) for v in o]
        return o
    return clean(rule)


def precheck(draft_resp: Any, active_resp: Any, proposal: Optional[List[Dict[str, Any]]] = None) -> Tuple[bool, str]:
    """Writing the draft replaces it wholesale, so it may only overwrite an empty draft, one that holds nothing
    beyond the active policy, or one that already holds this same proposal (same rule names in the same order: a
    re-write of our own earlier write). Anything else may be a teammate's unpublished work."""
    drules, arules = draft_rules(draft_resp), active_rules(active_resp)
    if drules is None:
        return False, "cannot read the current draft, so cannot tell whether it holds someone's work"
    if arules is None:
        return False, "cannot read the active policy, so cannot compare it with the draft"
    if not drules:
        return True, "draft is empty"
    if [_canon(r) for r in drules] == [_canon(r) for r in arules]:
        return True, f"draft ({len(drules)} rules) is identical to the active policy"
    if proposal is not None and [r.get("name") for r in drules] == [r.get("name") for r in proposal]:
        return True, f"draft ({len(drules)} rules) already holds this proposal; re-writing it"
    return False, (f"draft holds {len(drules)} rule(s) that differ from the active policy ({len(arules)} rules): "
                   "someone may be editing it. Discard or publish that draft in the console first")


def problems_in_proposal(rules: Any, policy_type: str = "TRANSFER") -> List[str]:
    if not isinstance(rules, list) or not rules:
        return ["proposal has no rules"]
    out: List[str] = []
    names = set()
    for i, r in enumerate(rules, 1):
        if not isinstance(r, dict):
            out.append(f"rule {i} is not an object")
            continue
        for key in ("name", "type", "verdict", "policyEngineVersion"):
            if key not in r:
                out.append(f"rule {i} ({r.get('name')!r}) is missing '{key}'")
        if r.get("type") != policy_type:
            out.append(f"rule {i} ({r.get('name')!r}) is not a {policy_type} rule")
        if r.get("name") in names:
            out.append(f"rule {i}: duplicate name {r.get('name')!r}")
        names.add(r.get("name"))
    return out


def readback_problems(sent: List[Dict[str, Any]], draft_resp: Any) -> List[str]:
    got = draft_rules(draft_resp)
    if got is None:
        return ["could not read the draft back"]
    if len(got) != len(sent):
        return [f"draft has {len(got)} rules after the write, expected {len(sent)}"]
    out = []
    for i, (a, b) in enumerate(zip(sent, got), 1):
        if a.get("name") != b.get("name"):
            out.append(f"rule {i}: sent {a.get('name')!r}, draft has {b.get('name')!r}")
    return out
