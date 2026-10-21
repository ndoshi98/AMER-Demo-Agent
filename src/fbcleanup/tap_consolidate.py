"""Exact TAP consolidation. Pure, no API calls, no I/O. Output is a PROPOSAL; a human publishes.

Policy semantics (first match wins) are modelled over a finite set of "cells": for every dimension the space of
possible transaction values is cut into cells that no rule distinguishes between, and a cell of the whole space is
one cell per dimension. A candidate rewrite is accepted only if the original and the rewritten rule list give the
same outcome in every cell. So every proposed removal or merge is verified, not pattern-matched.

Soundness choices (all conservative):
  * Token dimensions (initiator, source, destination, asset) are modelled as "a transaction may match ANY subset of
    the named values", because overlap between named values is not known. address_type and operation are
    single-valued, so they are modelled exclusively.
  * A rule that uses a shape not modelled exactly is OPAQUE. It stays in place, and nothing may move across it:
    the evaluator records every opaque rule a cell passes through and requires the same trail after the rewrite.
  * Amount ranges are modelled as USD intervals with inclusive bounds (unverified).
  * Two modes: strict (a transaction matching no rule must keep matching no rule) and default-deny (a transaction
    matching no rule is blocked, so a BLOCK rule can be dropped). Default-deny is an assumption about the
    workspace, not something the API tells us.
"""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple

STAR = "*"
DIMS = ("initiator", "source", "destination", "address_type", "asset", "amount", "operation")
EXCLUSIVE = {"address_type", "operation"}
OP_KEYS = ("type", "subType", "account", "side", "method", "programCall", "screeningMetadata", "quoteAsset",
           "baseAsset", "quoteAmount", "baseAmount", "dAppAddress", "derivationPath")
KNOWN_KEYS = {"id", "name", "asset", "amount", "source", "destination", "verdict", "initiator", "externalDescriptor",
              "policyEngineVersion", "amountOverTime", *OP_KEYS}
PRIMARY = {"asset": "assetTypes", "source": "type", "destination": "type"}
MAX_CLASSES = 10
MAX_LEAVES = 300_000
MAX_GAPS = 15

U = ("U",)


class TooComplex(Exception):
    pass


def _canon(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


@dataclass
class CRule:
    id: int
    name: str
    raw: Dict[str, Any]
    dims: Dict[str, tuple] = field(default_factory=dict)
    verdict: str = ""
    opaque: bool = False
    reason: str = ""
    parts: Dict[str, Tuple[str, str]] = field(default_factory=dict)   # dim -> (group, primary field)
    src: List[int] = field(default_factory=list)                       # original rule indexes folded into this one


# ------------------------------------------------------------------------------------------------
# Parsing: raw v2 rule -> CRule
# ------------------------------------------------------------------------------------------------
def _block(dim: str, blk: Any) -> Tuple[tuple, Tuple[str, str]]:
    if not isinstance(blk, dict):
        raise ValueError(f"'{dim}' missing or not an object")
    if blk.get("operator", "INCLUDES") != "INCLUDES":
        raise ValueError(f"'{dim}' uses operator {blk.get('operator')!r} (complement semantics not modelled)")
    if blk.get("nftTransfer"):
        raise ValueError("nftTransfer: true is not modelled")
    primary = PRIMARY[dim]
    prim = blk.get(primary)
    if not isinstance(prim, list) or not prim or not all(isinstance(e, str) for e in prim):
        raise ValueError(f"'{dim}.{primary}' is not a non-empty list of strings")
    skip = {primary, "operator", "nftTransfer"} | ({"addressType"} if dim == "destination" else set())
    rest = {k: v for k, v in blk.items() if k not in skip and not (k == "version" and v in (None, 1))}
    if not rest and STAR in prim:
        return U, ("{}", primary)
    group = _canon(rest)
    return ("T", frozenset((group, e) for e in prim)), (group, primary)


def _dec(v: Any) -> Optional[Decimal]:
    if v is None:
        return None
    try:
        return Decimal(str(v))
    except InvalidOperation:
        raise ValueError(f"amount bound {v!r} is not a number")


def _amount(raw: Dict[str, Any]) -> tuple:
    if raw.get("amountOverTime") not in (None, {}):
        raise ValueError("amountOverTime is not modelled")
    amt = raw.get("amount")
    if amt is None:
        return U
    if not isinstance(amt, dict) or set(amt) - {"range", "currency"} or not isinstance(amt.get("range"), dict):
        raise ValueError("amount has a shape that is not modelled")
    if amt.get("currency") != "USD":
        raise ValueError(f"amount currency {amt.get('currency')!r} is not modelled (USD only)")
    rng = amt["range"]
    if set(rng) - {"min", "max"}:
        raise ValueError("amount.range has unexpected keys")
    lo, hi = _dec(rng.get("min")), _dec(rng.get("max"))
    lo = Decimal(0) if lo is None or lo < 0 else lo
    return ("I", lo, hi)


def parse(index: int, raw: Dict[str, Any]) -> CRule:
    name = str(raw.get("name") or raw.get("id") or f"rule-{index}")
    try:
        if "verdict" not in raw and "policyEngineVersion" not in raw:
            raise ValueError("not a v2 rule (legacy shape not modelled)")
        extra = sorted(k for k, v in raw.items() if k not in KNOWN_KEYS and v is not None)
        if extra:
            raise ValueError(f"unrecognised field(s) {extra}")
        if raw.get("policyEngineVersion") not in (None, "v2"):
            raise ValueError(f"policyEngineVersion {raw.get('policyEngineVersion')!r}")
        v = raw.get("verdict")
        if not isinstance(v, dict) or "action" not in v:
            raise ValueError("no verdict action")
        verdict = "BLOCK" if v["action"] == "BLOCK" else _canon(v)
        dims: Dict[str, tuple] = {}
        parts: Dict[str, Tuple[str, str]] = {}
        ini = raw.get("initiator")
        if ini is None:
            raise ValueError("initiator missing")
        dims["initiator"] = U if ini == STAR else ("T", frozenset({("initiator", _canon(ini))}))
        dims["source"], parts["source"] = _block("source", raw.get("source"))
        dims["destination"], parts["destination"] = _block("destination", raw.get("destination"))
        at = raw["destination"].get("addressType")
        if at is not None and not isinstance(at, str):
            raise ValueError("destination.addressType is not a string")
        dims["address_type"] = U if at in (None, STAR) else ("T", frozenset({("addressType", at)}))
        dims["asset"], parts["asset"] = _block("asset", raw.get("asset"))
        dims["amount"] = _amount(raw)
        op = _canon({k: raw[k] for k in OP_KEYS if raw.get(k) is not None})
        dims["operation"] = ("T", frozenset({("operation", op)}))
        return CRule(index, name, raw, dims, verdict, False, "", parts, [index])
    except ValueError as exc:
        return CRule(index, name, raw, {}, "<opaque>", True, str(exc), {}, [index])


# ------------------------------------------------------------------------------------------------
# The cell space
# ------------------------------------------------------------------------------------------------
def _label(tok: Tuple[str, str]) -> str:
    grp, elem = tok
    return elem if grp in ("{}", "addressType", "initiator", "operation") else f"{elem} {grp}"


class Space:
    def __init__(self, rules: List[CRule]):
        live = [r for r in rules if not r.opaque]
        self.cells: Dict[str, List[Tuple[str, Any]]] = {}
        self.cls: Dict[str, Dict[tuple, int]] = {}
        for d in DIMS:
            if d == "amount":
                self._amount_cells(live)
            else:
                self._token_cells(d, live)
        self.col: Dict[str, List[int]] = {d: [0] * len(self.cells[d]) for d in DIMS}
        for r in rules:
            self.add(r)

    def _token_cells(self, d: str, live: List[CRule]) -> None:
        sig: Dict[tuple, set] = {}
        for r in live:
            if r.dims[d][0] == "T":
                for t in r.dims[d][1]:
                    sig.setdefault(t, set()).add(r.id)
        groups: Dict[frozenset, List[tuple]] = {}
        for t, ids in sig.items():
            groups.setdefault(frozenset(ids), []).append(t)
        classes = [sorted(ts) for _, ts in sorted(groups.items(), key=lambda kv: sorted(kv[0]))]
        self.cls[d] = {t: k for k, ts in enumerate(classes) for t in ts}
        names = [", ".join(_label(t) for t in ts) for ts in classes]
        if d in EXCLUSIVE:
            cells = [(names[k], 1 << k) for k in range(len(classes))]
            if d == "address_type":
                cells.append(("any other address type", 0))
            self.cells[d] = cells or [("-", 0)]
        else:
            if len(classes) > MAX_CLASSES:
                raise TooComplex(f"'{d}' distinguishes {len(classes)} groups of values (limit {MAX_CLASSES})")
            self.cells[d] = [("none of the named values" if bits == 0 else
                              " + ".join(names[k] for k in range(len(classes)) if bits >> k & 1), bits)
                             for bits in range(2 ** len(classes))]

    def _amount_cells(self, live: List[CRule]) -> None:
        pts = {Decimal(0)}
        for r in live:
            v = r.dims["amount"]
            if v[0] == "I":
                pts.add(v[1])
                if v[2] is not None:
                    pts.add(v[2])
        b = sorted(pts)
        cells: List[Tuple[str, Any]] = []
        for i, x in enumerate(b):
            cells.append((f"exactly {x:f} USD", x))
            if i + 1 < len(b):
                cells.append((f"between {x:f} and {b[i + 1]:f} USD (exclusive)", (x + b[i + 1]) / 2))
            else:
                cells.append((f"above {x:f} USD", x + 1))
        self.cells["amount"] = cells

    def _matches(self, r: CRule, d: str, payload: Any) -> bool:
        v = r.dims[d]
        if v[0] == "U":
            return True
        if v[0] == "I":
            return v[1] <= payload and (v[2] is None or payload <= v[2])
        cls = self.cls[d]
        rb = 0
        for t in v[1]:
            rb |= 1 << cls[t]
        return bool(rb & payload)

    def add(self, r: CRule) -> None:
        for d in DIMS:
            for c, (_, payload) in enumerate(self.cells[d]):
                if r.opaque or self._matches(r, d, payload):
                    self.col[d][c] |= 1 << r.id


def _evaluator(items: List[CRule], default: str):
    lmask = 0
    for r in items:
        lmask |= 1 << r.id
    cache: Dict[int, tuple] = {}

    def f(G: int) -> tuple:
        g = G & lmask
        o = cache.get(g)
        if o is None:
            trail: List[int] = []
            for r in items:
                if g >> r.id & 1:
                    if r.opaque:
                        trail.append(r.id)
                    else:
                        o = (tuple(trail), r.verdict)
                        break
            if o is None:
                o = (tuple(trail), default)
            cache[g] = o
        return o
    return f, lmask


def _order(space: Space) -> List[str]:
    return sorted(DIMS, key=lambda d: len(space.cells[d]))


def _default(deny: bool) -> str:
    return "BLOCK" if deny else "<NOMATCH>"


def _compare(space: Space, a: List[CRule], b: List[CRule], deny: bool) -> bool:
    """True if a and b give the same outcome in every cell. Raises TooComplex past the leaf budget."""
    fa, ma = _evaluator(a, _default(deny))
    fb, mb = _evaluator(b, _default(deny))
    both, order, seen = ma | mb, _order(space), [0]

    def rec(k: int, G: int) -> bool:
        if G & both == 0:
            return True
        if k == len(order):
            seen[0] += 1
            if seen[0] > MAX_LEAVES:
                raise TooComplex("too many cells to verify")
            return fa(G) == fb(G)
        return all(rec(k + 1, G & m) for m in space.col[order[k]])
    return rec(0, -1)


def _gaps(space: Space, rules: List[CRule]) -> Tuple[List[str], int]:
    """Cells that match no rule at all (and pass no opaque rule)."""
    _, lmask = _evaluator(rules, "")
    order, found, seen = _order(space), [], [0]

    def rec(k: int, G: int, chosen: List[Tuple[str, int]]) -> None:
        if G & lmask == 0:
            found.append(list(chosen))
            return
        if k == len(order):
            return
        seen[0] += 1
        if seen[0] > MAX_LEAVES:
            raise TooComplex("too many cells to enumerate")
        d = order[k]
        for c, m in enumerate(space.col[d]):
            rec(k + 1, G & m, chosen + [(d, c)])
    rec(0, -1, [])
    out = []
    for ch in found:
        parts = [f"{d.replace('_', ' ')}: {space.cells[d][c][0]}" for d, c in sorted(ch, key=lambda x: DIMS.index(x[0]))
                 if len(space.cells[d]) > 1]
        out.append("; ".join(parts) or "everything")
    return out, len(out)


# ------------------------------------------------------------------------------------------------
# Merging
# ------------------------------------------------------------------------------------------------
def _fmt(x: Decimal) -> str:
    return format(x, "f")


def _try_merge(a: CRule, b: CRule, new_id: int) -> Optional[CRule]:
    if a.opaque or b.opaque or a.verdict != b.verdict:
        return None
    diff = [d for d in DIMS if a.dims[d] != b.dims[d]]
    if len(diff) != 1:
        return None
    d = diff[0]
    va, vb = a.dims[d], b.dims[d]
    raw = copy.deepcopy(a.raw)
    raw.pop("id", None)
    raw.pop("externalDescriptor", None)
    raw["name"] = f"{a.name} + {b.name}"
    dims = dict(a.dims)
    if d in PRIMARY and va[0] == "T" and vb[0] == "T" and a.parts[d] == b.parts[d]:
        fld = a.parts[d][1]
        la, lb = a.raw[d][fld], b.raw[d][fld]
        raw[d][fld] = list(la) + [e for e in lb if e not in la]
        dims[d] = ("T", va[1] | vb[1])
    elif d == "amount" and va[0] == "I" and vb[0] == "I":
        if not (va[1] <= (vb[2] if vb[2] is not None else va[1]) and vb[1] <= (va[2] if va[2] is not None else vb[1])):
            return None
        lo = min(va[1], vb[1])
        hi = None if va[2] is None or vb[2] is None else max(va[2], vb[2])
        rng = {"min": _fmt(lo)}
        if hi is not None:
            rng["max"] = _fmt(hi)
        raw["amount"] = {"range": rng, "currency": "USD"}
        dims[d] = ("I", lo, hi)
    else:
        return None
    return CRule(new_id, raw["name"], raw, dims, a.verdict, False, "", a.parts, a.src + b.src)


# ------------------------------------------------------------------------------------------------
# Public API
# ------------------------------------------------------------------------------------------------
@dataclass
class Step:
    kind: str            # REMOVE | MERGE
    sources: List[int]   # original rule indexes involved
    detail: str


@dataclass
class Plan:
    default_deny: bool
    original: int
    rules: List[Dict[str, Any]]
    steps: List[Step] = field(default_factory=list)
    opaque: List[Tuple[int, str, str]] = field(default_factory=list)
    gaps: List[str] = field(default_factory=list)
    gap_total: int = 0
    note: str = ""


def raw_rules(policy: Any) -> List[Dict[str, Any]]:
    body = policy.get("policy", policy) if isinstance(policy, dict) else {}
    return list(body.get("rules") or [])


def consolidate(raw: List[Dict[str, Any]], default_deny: bool = False) -> Plan:
    rules = [parse(i, r) for i, r in enumerate(raw)]
    plan = Plan(default_deny, len(rules), [r.raw for r in rules],
                opaque=[(r.id, r.name, r.reason) for r in rules if r.opaque])
    try:
        space = Space(rules)
        if not plan.opaque:
            plan.gaps, plan.gap_total = _gaps(space, rules)
            plan.gaps = plan.gaps[:MAX_GAPS]
    except TooComplex as exc:
        plan.note = f"Consolidation skipped: {exc}. Only the pairwise findings above apply."
        return plan

    def verified(cand: List[CRule]) -> bool:
        try:
            return _compare(space, rules, cand, default_deny)
        except TooComplex:
            return False

    cur = list(rules)
    next_id = len(rules)
    changed, guard = True, 0
    while changed and guard < 500:
        changed, guard = False, guard + 1
        for r in reversed(cur):
            if r.opaque:
                continue
            cand = [x for x in cur if x is not r]
            if not verified(cand):
                continue
            flipped = [replace(x, verdict="<FLIP>") if x is r else x for x in cur]
            try:
                never_fires = _compare(space, cur, flipped, default_deny)
            except TooComplex:
                never_fires = False
            why = ("can never fire: earlier rules already catch everything it matches" if never_fires else
                   "redundant: everything it catches gets the same outcome from the remaining rules" +
                   (" (counting unmatched transactions as blocked)" if default_deny else ""))
            plan.steps.append(Step("REMOVE", r.src, f"#{r.id} '{r.name}' — {why}"))
            cur, changed = cand, True
            break
        if changed:
            continue
        for i in range(len(cur)):
            for j in range(i + 1, len(cur)):
                m = _try_merge(cur[i], cur[j], next_id)
                if m is None:
                    continue
                next_id += 1
                space.add(m)
                for pos in (i, j):
                    cand = list(cur)
                    cand[pos] = m
                    del cand[j if pos == i else i]
                    if verified(cand):
                        plan.steps.append(Step("MERGE", m.src, f"#{cur[i].id} '{cur[i].name}' + #{cur[j].id} "
                                                               f"'{cur[j].name}' differ only in one dimension"))
                        cur, changed = cand, True
                        break
                if changed:
                    break
            if changed:
                break
    plan.rules = [r.raw for r in cur]
    return plan


def plan_to_dict(plan: Plan) -> Dict[str, Any]:
    return {"assumes_default_deny": plan.default_deny, "original_rule_count": plan.original,
            "proposed_rule_count": len(plan.rules), "steps": [{"kind": s.kind, "original_rules": s.sources,
                                                               "detail": s.detail} for s in plan.steps],
            "coverage_gaps": plan.gaps, "opaque_rules": [{"index": i, "name": n, "reason": r} for i, n, r in plan.opaque],
            "note": plan.note, "rules": plan.rules}
