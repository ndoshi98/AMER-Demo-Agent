"""Pure TAP/policy analysis. No API calls, no I/O.

Rules are evaluated in order, first match wins. Each rule is modelled as one Match per dimension.
Anything the adapter cannot model exactly is marked `opaque`: it is never reported as safe, never dropped,
and never used as the "shadowing" rule. That is the safety valve for unverified policy shapes.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Set

STAR = "*"
DIMENSIONS = ("initiator", "source", "destination", "address_type", "asset", "amount", "operation")


# ------------------------------------------------------------------------------------------------
# Match: one dimension of a rule
# ------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Match:
    universal: bool = False
    tokens: FrozenSet[str] = frozenset()
    # atomic: tokens are opaque labels, not set members. covers() degrades to equality-or-universal and
    # the dimension can never be unioned (no MERGEABLE finding).
    atomic: bool = False

    @staticmethod
    def star() -> "Match":
        return Match(universal=True)

    @staticmethod
    def of(tokens: Iterable[str], atomic: bool = False) -> "Match":
        toks = frozenset(tokens)
        return Match.star() if STAR in toks and not atomic else Match(False, toks, atomic)

    def covers(self, other: "Match") -> bool:
        if self.universal:
            return True
        if other.universal:
            return False
        if self.atomic or other.atomic:
            return self.tokens == other.tokens
        return other.tokens <= self.tokens

    def intersects(self, other: "Match") -> bool:
        if self.universal or other.universal:
            return True
        if self.atomic or other.atomic:
            return self.tokens == other.tokens
        return bool(self.tokens & other.tokens)

    def union(self, other: "Match") -> "Match":
        if self.universal or other.universal:
            return Match.star()
        if self.atomic or other.atomic:
            raise ValueError("atomic dimensions cannot be unioned")
        return Match(False, self.tokens | other.tokens)

    def label(self) -> str:
        return "*" if self.universal else ", ".join(sorted(self.tokens))


# ------------------------------------------------------------------------------------------------
# Rule + findings
# ------------------------------------------------------------------------------------------------
@dataclass
class Rule:
    index: int
    name: str
    dims: Dict[str, Match]
    verdict: str                      # canonical string; equal verdict <=> identical behaviour on match
    refs: FrozenSet[str] = frozenset()  # "user:<id>" / "vault:<id>" referenced anywhere in the rule
    raw: Dict[str, Any] = field(default_factory=dict)
    opaque: bool = False
    opaque_reason: str = ""


DUPLICATE, SHADOWED, DEAD_REF, MERGEABLE = "DUPLICATE", "SHADOWED", "DEAD_REF", "MERGEABLE"
_KIND_RANK = {DUPLICATE: 0, SHADOWED: 1, DEAD_REF: 2, MERGEABLE: 3}


@dataclass
class Finding:
    kind: str
    rule: int                         # affected rule (the later one for DUPLICATE/SHADOWED/MERGEABLE)
    other: Optional[int]              # the earlier / partner rule
    safe: bool                        # behaviour-preserving to act on
    summary: str
    dead_refs: List[str] = field(default_factory=list)
    dimension: str = ""
    hazards: List[int] = field(default_factory=list)  # MERGEABLE: in-between rules whose precedence would change


# ------------------------------------------------------------------------------------------------
# Analysis
# ------------------------------------------------------------------------------------------------
def _covers_all(a: Rule, b: Rule) -> bool:
    return all(a.dims[d].covers(b.dims[d]) for d in DIMENSIONS)


def _identical(a: Rule, b: Rule) -> bool:
    return all(a.dims[d] == b.dims[d] for d in DIMENSIONS)


def _intersects_all(a: Rule, b: Rule) -> bool:
    return all(a.dims[d].intersects(b.dims[d]) for d in DIMENSIONS)


def _label(r: Rule) -> str:
    return f"#{r.index} '{r.name}'"


def analyze(rules: List[Rule], known_users: Optional[Dict[str, bool]] = None,
            known_vaults: Optional[Set[str]] = None) -> List[Finding]:
    """known_users: id -> enabled. known_vaults: ids. Pass None to skip that DEAD_REF check."""
    findings: List[Finding] = []
    flagged_for_removal: Set[int] = set()
    live = [r for r in rules if not r.opaque]

    for j, later in enumerate(live):
        earlier = live[:j]
        dup = next((e for e in earlier if _identical(e, later) and e.verdict == later.verdict), None)
        if dup is not None:
            findings.append(Finding(DUPLICATE, later.index, dup.index, True,
                                    f"{_label(later)} is identical to earlier {_label(dup)} with the same verdict; "
                                    "removing the later one cannot change behaviour."))
            flagged_for_removal.add(later.index)
            continue
        shadow = next((e for e in earlier if _covers_all(e, later)), None)
        if shadow is not None:
            if shadow.verdict == later.verdict:
                findings.append(Finding(SHADOWED, later.index, shadow.index, True,
                                        f"{_label(later)} can never fire: earlier {_label(shadow)} matches a superset "
                                        "and has the same verdict."))
                flagged_for_removal.add(later.index)
            else:
                findings.append(Finding(SHADOWED, later.index, shadow.index, False,
                                        f"!! {_label(later)} can never fire AND its verdict DIFFERS from earlier "
                                        f"{_label(shadow)}. This usually means the rules are in the wrong order — "
                                        "review by hand."))

    for r in rules:
        dead = _dead_refs(r, known_users, known_vaults)
        if dead:
            findings.append(Finding(DEAD_REF, r.index, None, False,
                                    f"{_label(r)} references missing/disabled {', '.join(dead)}; it still fires but "
                                    "its scope is narrower than it reads. Usually repoint, not delete.", dead_refs=dead))

    for jj, later in enumerate(live):
        if later.index in flagged_for_removal:
            continue
        for ii in range(jj):
            earlier = live[ii]
            if earlier.index in flagged_for_removal or earlier.verdict != later.verdict:
                continue
            differing = [d for d in DIMENSIONS if earlier.dims[d] != later.dims[d]]
            if len(differing) != 1 or _covers_all(earlier, later) or _covers_all(later, earlier):
                continue
            dim = differing[0]
            if earlier.dims[dim].atomic or later.dims[dim].atomic:
                continue
            between = [b for b in rules if earlier.index < b.index < later.index]
            hazards = [b.index for b in between
                       if b.opaque or (b.verdict != later.verdict and _intersects_all(b, later))]
            findings.append(Finding(MERGEABLE, later.index, earlier.index, False,
                                    f"{_label(earlier)} and {_label(later)} differ only in '{dim}'; union of "
                                    f"'{dim}' would collapse them into one rule." +
                                    (f" CAUTION: rule(s) {hazards} sit between them and would lose precedence."
                                     if hazards else ""), dimension=dim, hazards=hazards))
            break

    findings.sort(key=lambda f: (not f.safe, _KIND_RANK[f.kind], f.rule))
    return findings


def _dead_refs(r: Rule, known_users: Optional[Dict[str, bool]], known_vaults: Optional[Set[str]]) -> List[str]:
    dead = []
    for ref in sorted(r.refs):
        kind, _, ident = ref.partition(":")
        if kind == "user" and known_users is not None and not known_users.get(ident, False):
            dead.append(ref)
        elif kind == "vault" and known_vaults is not None and ident not in known_vaults:
            dead.append(ref)
    return dead


def propose(rules: List[Rule], findings: List[Finding]) -> Dict[str, Any]:
    """Proposed rule set: drops ONLY safe DUPLICATE and same-verdict SHADOWED rules. Never executed."""
    drop = {f.rule: f for f in findings if f.safe and f.kind in (DUPLICATE, SHADOWED)}
    kept = [r.raw for r in rules if r.index not in drop]
    dropped = [{"index": i, "name": next(r.name for r in rules if r.index == i), "reason": f.kind,
                "because_of": f.other} for i, f in sorted(drop.items())]
    return {"note": "PROPOSAL ONLY — review in the console and publish by hand. Behaviour-preserving removals only.",
            "original_rule_count": len(rules), "proposed_rule_count": len(kept),
            "dropped": dropped, "rules": kept}


# ------------------------------------------------------------------------------------------------
# Adapters: raw policy JSON -> Rule objects (best-effort; verified against a live policy at audit time)
# ------------------------------------------------------------------------------------------------
def _canon(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def _collect_refs(obj: Any, out: Set[str]) -> None:
    if isinstance(obj, dict):
        for key, val in obj.items():
            if key == "users" and isinstance(val, list):
                out.update(f"user:{u}" for u in val if isinstance(u, str) and u != STAR)
            elif key == "ids" and isinstance(val, list):
                for item in val:
                    seq = item if isinstance(item, list) else [item.get("type"), item.get("id")] if isinstance(item, dict) else []
                    if len(seq) >= 2 and isinstance(seq[0], str) and isinstance(seq[1], str) and seq[1] != STAR:
                        kind = seq[0].upper()
                        if kind.startswith("VAULT"):
                            out.add(f"vault:{seq[1]}")
                        elif "WALLET" in kind:
                            out.add(f"wallet:{seq[1]}")
                        elif "NETWORK" in kind:
                            out.add(f"connection:{seq[1]}")
            else:
                _collect_refs(val, out)
    elif isinstance(obj, list):
        for item in obj:
            _collect_refs(item, out)


def _legacy_ids(block: Optional[Dict[str, Any]], fallback_type: Any, fallback_id: Any) -> Match:
    ids = (block or {}).get("ids")
    if ids is None:
        if fallback_type in (None, STAR):
            return Match.star()
        return Match.of([f"{fallback_type}:{fallback_id}"])
    toks = set()
    for inner in ids:
        seq = inner if isinstance(inner, list) else [inner]
        toks.add(STAR if seq and seq[0] == STAR else ":".join(str(s) for s in seq))
    return Match.of(toks)


def _opaque(index: int, raw: Dict[str, Any], reason: str) -> Rule:
    blank = Match.of(["<opaque>"], atomic=True)
    return Rule(index, str(raw.get("name") or raw.get("externalDescriptor") or raw.get("id") or f"rule-{index}"),
                {d: blank for d in DIMENSIONS}, "<opaque>", frozenset(), raw, True, reason)


def _legacy_rule(index: int, raw: Dict[str, Any]) -> Rule:
    ops = raw.get("operators")
    if ops is None:
        return _opaque(index, raw, "no 'operators' block (deprecated single 'operator' only)")
    if ops.get("wildcard") == STAR:
        initiator = Match.star()
    else:
        toks = {f"user:{u}" for u in ops.get("users") or []} | {f"group:{g}" for g in ops.get("usersGroups") or []} \
            | {f"service:{s}" for s in ops.get("services") or []}
        if not toks:
            return _opaque(index, raw, "empty initiator set")
        initiator = Match.of(toks)
    amount = Match.of([_canon([raw.get("amountCurrency"), raw.get("amountScope"), raw.get("amount"),
                               raw.get("periodSec"), raw.get("amountAggregation")])], atomic=True)
    operation = Match.of([_canon([raw.get("type"), raw.get("transactionType"), raw.get("rawMessageSigning"),
                                  raw.get("applyForApprove"), raw.get("applyForTypedMessage")])], atomic=True)
    asset = Match.of([str(raw.get("asset", STAR))])
    addr = raw.get("dstAddressType")
    address_type = Match.star() if addr in (None, STAR, "ANY") else Match.of([str(addr)])
    verdict = _canon([raw.get("action"), raw.get("authorizationGroups"), raw.get("designatedSigners"),
                      raw.get("authorizers"), raw.get("authorizersCount")])
    refs: Set[str] = set()
    _collect_refs({k: raw.get(k) for k in ("operators", "src", "dst", "authorizationGroups", "designatedSigners")}, refs)
    return Rule(index, str(raw.get("externalDescriptor") or f"rule-{index}"),
                {"initiator": initiator, "source": _legacy_ids(raw.get("src"), raw.get("srcType"), raw.get("srcId")),
                 "destination": _legacy_ids(raw.get("dst"), raw.get("dstType"), raw.get("dstId")),
                 "address_type": address_type, "asset": asset, "amount": amount, "operation": operation},
                verdict, frozenset(refs), raw)


def _has_excludes(obj: Any) -> bool:
    if isinstance(obj, dict):
        return obj.get("operator") == "EXCLUDES" or any(_has_excludes(v) for v in obj.values())
    if isinstance(obj, list):
        return any(_has_excludes(v) for v in obj)
    return False


def _v2_rule(index: int, raw: Dict[str, Any]) -> Rule:
    if any(_has_excludes(raw.get(k)) for k in ("initiator", "source", "destination", "asset")):
        return _opaque(index, raw, "uses an EXCLUDES operator (complement semantics not modelled)")

    def atom(value: Any) -> Match:
        return Match.of(["<absent>" if value is None else _canon(value)], atomic=True)

    initiator = Match.star() if raw.get("initiator") == STAR else atom(raw.get("initiator"))
    dest = raw.get("destination") or {}
    dest_wo_addr = {k: v for k, v in dest.items() if k != "addressType"} if dest else None
    operation_keys = ("type", "subType", "account", "side", "method", "programCall", "screeningMetadata", "quoteAsset",
                      "baseAsset", "quoteAmount", "baseAmount", "dAppAddress", "derivationPath")
    refs: Set[str] = set()
    _collect_refs({k: raw.get(k) for k in ("initiator", "source", "destination", "verdict")}, refs)
    return Rule(index, str(raw.get("name") or raw.get("id") or f"rule-{index}"),
                {"initiator": initiator, "source": atom(raw.get("source")), "destination": atom(dest_wo_addr),
                 "address_type": atom(dest.get("addressType") if dest else None), "asset": atom(raw.get("asset")),
                 "amount": atom([raw.get("amount"), raw.get("amountOverTime")]),
                 "operation": atom({k: raw.get(k) for k in operation_keys})},
                _canon(raw.get("verdict")), frozenset(refs), raw)


def rules_from_policy(policy: Dict[str, Any]) -> List[Rule]:
    """Accepts a legacy ({rules:[...]}) or v2 ({policy:{rules:[...]}} / {rules:[...]}) payload."""
    body = policy.get("policy", policy) if isinstance(policy, dict) else {}
    raw_rules = body.get("rules") or []
    out = []
    for i, raw in enumerate(raw_rules):
        is_v2 = "verdict" in raw or "policyEngineVersion" in raw
        out.append(_v2_rule(i, raw) if is_v2 else _legacy_rule(i, raw))
    return out
