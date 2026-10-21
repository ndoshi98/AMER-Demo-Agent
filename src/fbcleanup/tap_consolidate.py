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
DIMS = ("initiator", "source", "destination", "address_type", "asset", "amount_usd", "amount_native", "operation")
BLOCK_DIMS = ("initiator", "source", "destination", "asset")
OP_KEYS = ("type", "subType", "account", "side", "method", "programCall", "screeningMetadata", "quoteAsset",
           "baseAsset", "quoteAmount", "baseAmount", "dAppAddress", "derivationPath")
KNOWN_KEYS = {"id", "name", "asset", "amount", "source", "destination", "verdict", "initiator", "externalDescriptor",
              "policyEngineVersion", "amountOverTime", "index", "v1RuleId", *OP_KEYS}
ENDPOINT_SELECTORS = ("type", "ids", "subType")
MAX_CLASSES = 10
MAX_CELLS = 3000
MAX_NODES = 400_000
MAX_GAPS = 15

U = ("U",)
Token = Tuple[str, str, str]   # (group, selector, element)


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
    parts: Dict[str, Tuple[str, str]] = field(default_factory=dict)   # dim -> (group, selector field)
    implies: Dict[str, Dict[Token, Tuple[Token, ...]]] = field(default_factory=dict)
    src: List[int] = field(default_factory=list)                       # original rule indexes folded into this one

    @property
    def non_universal(self) -> frozenset:
        return frozenset(d for d, v in self.dims.items() if v[0] != "U")


# ------------------------------------------------------------------------------------------------
# Parsing: raw v2 rule -> CRule
# ------------------------------------------------------------------------------------------------
def _strs(v: Any) -> bool:
    return isinstance(v, list) and bool(v) and all(isinstance(e, str) for e in v)


def _endpoint_tokens(group: str, sel: str, items: Any):
    """-> (tokens, implications) for a source/destination selector list."""
    if sel == "type":
        if not _strs(items):
            raise ValueError("endpoint 'type' is not a non-empty list of strings")
        return {(group, "type", e) for e in items}, {}
    if not isinstance(items, list) or not items or not all(isinstance(e, dict) for e in items):
        raise ValueError(f"endpoint '{sel}' is not a non-empty list of objects")
    toks, imp = set(), {}
    for e in items:
        typ, sub = e.get("type"), e.get("subType")
        allowed = {"type", "id", "subType"} if sel == "ids" else {"type", "subType"}
        if not isinstance(typ, str) or set(e) - allowed or (sel == "ids" and not isinstance(e.get("id"), str)) \
                or (sel == "subType" and not isinstance(sub, str)) or (sub is not None and not isinstance(sub, str)):
            raise ValueError(f"endpoint '{sel}' element has an unmodelled shape")
        tok = (group, sel, _canon(e))
        implied = [(group, "type", typ)]
        if sel == "ids" and sub:
            implied.append((group, "subType", _canon({"subType": sub, "type": typ})))
        toks.add(tok)
        imp[tok] = tuple(implied)
    return toks, imp


def _block(dim: str, blk: Any):
    """-> (value, (group, selector), implications). Raises ValueError for unmodelled shapes."""
    if not isinstance(blk, dict):
        raise ValueError(f"'{dim}' missing or not an object")
    if blk.get("operator", "INCLUDES") != "INCLUDES":
        raise ValueError(f"'{dim}' uses operator {blk.get('operator')!r} (complement semantics not modelled)")
    if blk.get("nftTransfer"):
        raise ValueError("nftTransfer: true is not modelled")
    sels = ("assetTypes",) if dim == "asset" else ENDPOINT_SELECTORS
    present = [k for k in sels if blk.get(k)]
    if len(present) != 1:
        raise ValueError(f"'{dim}' must use exactly one selector of {list(sels)}")
    sel = present[0]
    skip = {*sels, "operator", "nftTransfer"} | ({"addressType"} if dim == "destination" else set())
    rest = {k: v for k, v in blk.items() if k not in skip and not (k == "version" and v in (None, 1))}
    group = _canon(rest)
    if sel in ("type", "assetTypes"):
        if not _strs(blk[sel]):
            raise ValueError(f"'{dim}.{sel}' is not a non-empty list of strings")
        if STAR in blk[sel] and not rest:
            return U, (group, sel), {}
        return ("T", frozenset((group, sel, e) for e in blk[sel])), (group, sel), {}
    toks, imp = _endpoint_tokens(group, sel, blk[sel])
    return ("T", frozenset(toks)), (group, sel), imp


def _initiator(ini: Any):
    if ini is None:
        raise ValueError("initiator missing")
    if ini == STAR:
        return U, ("initiator", ""), {}
    if not isinstance(ini, dict) or ini.get("operator", "INCLUDES") != "INCLUDES":
        raise ValueError("initiator has a shape that is not modelled")
    present = [k for k in ("users", "groups") if ini.get(k)]
    if len(present) != 1 or set(ini) - {"operator", "users", "groups"} or not _strs(ini[present[0]]):
        raise ValueError("initiator must list exactly one of users/groups")
    sel = present[0]
    return ("T", frozenset(("initiator", sel, e) for e in ini[sel])), ("initiator", sel), {}


def _dec(v: Any) -> Optional[Decimal]:
    if v is None:
        return None
    try:
        return Decimal(str(v))
    except InvalidOperation:
        raise ValueError(f"amount bound {v!r} is not a number")


def _amount(raw: Dict[str, Any]) -> Dict[str, tuple]:
    if raw.get("amountOverTime") not in (None, {}):
        raise ValueError("amountOverTime is not modelled")
    amt = raw.get("amount")
    out = {"amount_usd": U, "amount_native": U}
    if amt is None:
        return out
    if not isinstance(amt, dict) or set(amt) - {"range", "currency"} or not isinstance(amt.get("range"), dict):
        raise ValueError("amount has a shape that is not modelled")
    dim = {"USD": "amount_usd", "NATIVE": "amount_native"}.get(amt.get("currency"))
    if dim is None:
        raise ValueError(f"amount currency {amt.get('currency')!r} is not modelled (USD and NATIVE only)")
    rng = amt["range"]
    if set(rng) - {"min", "max"}:
        raise ValueError("amount.range has unexpected keys")
    lo, hi = _dec(rng.get("min")), _dec(rng.get("max"))
    out[dim] = ("I", Decimal(0) if lo is None or lo < 0 else lo, hi)
    return out


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
        implies: Dict[str, Dict[Token, Tuple[Token, ...]]] = {}
        dims["initiator"], parts["initiator"], implies["initiator"] = _initiator(raw.get("initiator"))
        for d in ("source", "destination", "asset"):
            dims[d], parts[d], implies[d] = _block(d, raw.get(d))
        at = raw["destination"].get("addressType")
        if at is not None and not isinstance(at, str):
            raise ValueError("destination.addressType is not a string")
        dims["address_type"] = U if at in (None, STAR) else ("T", frozenset({("addressType", "value", at)}))
        dims.update(_amount(raw))
        op = _canon({k: raw[k] for k in OP_KEYS if raw.get(k) is not None})
        dims["operation"] = ("T", frozenset({("operation", "value", op)}))
        return CRule(index, name, raw, dims, verdict, False, "", parts, implies, [index])
    except ValueError as exc:
        return CRule(index, name, raw, {}, "<opaque>", True, str(exc), {}, {}, [index])


# ------------------------------------------------------------------------------------------------
# The cell space
# ------------------------------------------------------------------------------------------------
def _label(tok: Token) -> str:
    grp, sel, elem = tok
    if sel in ("ids", "subType"):
        e = json.loads(elem)
        return "/".join(str(e[k]) for k in ("type", "subType", "id") if e.get(k))
    return elem if grp in ("{}", "addressType", "initiator", "operation") else f"{elem} {grp}"


def _in_family(d: str, tok: Token) -> bool:
    """Family tokens are mutually exclusive: a transaction has one source, one destination, one initiator user,
    one address type and one operation."""
    return d != "asset" and not (d == "initiator" and tok[1] == "groups")


_OTHER = {"address_type": "any other address type", "source": "any other source", "destination": "any other destination",
          "initiator": "any other initiator"}


class Space:
    def __init__(self, rules: List[CRule]):
        live = [r for r in rules if not r.opaque]
        self.cells: Dict[str, List[Tuple[str, Any]]] = {}
        self.implies: Dict[str, Dict[Token, Tuple[Token, ...]]] = {d: {} for d in BLOCK_DIMS}
        for r in live:
            for d, m in r.implies.items():
                self.implies[d].update(m)
        for d in DIMS:
            if d.startswith("amount"):
                self._amount_cells(d, live)
            else:
                self._token_cells(d, live)
        self.col: Dict[str, List[int]] = {d: [0] * len(self.cells[d]) for d in DIMS}
        for r in rules:
            self.add(r)

    def _closure(self, d: str, t: Token) -> frozenset:
        out, todo = {t}, [t]
        while todo:
            for u in self.implies.get(d, {}).get(todo.pop(), ()):
                if u not in out:
                    out.add(u)
                    todo.append(u)
        return frozenset(out)

    def _token_cells(self, d: str, live: List[CRule]) -> None:
        mem: Dict[Token, set] = {}
        for r in live:
            if r.dims[d][0] == "T":
                for t in r.dims[d][1]:
                    mem.setdefault(t, set()).add(r.id)
        fam = sorted(t for t in mem if _in_family(d, t))
        free = [t for t in mem if not _in_family(d, t)]
        groups: Dict[frozenset, List[Token]] = {}
        for t in free:
            groups.setdefault(frozenset(mem[t]), []).append(t)
        classes = [sorted(ts) for _, ts in sorted(groups.items(), key=lambda kv: sorted(kv[0]))]
        if len(classes) > MAX_CLASSES:
            raise TooComplex(f"'{d}' distinguishes {len(classes)} groups of values (limit {MAX_CLASSES})")
        base = [(_label(t), self._closure(d, t)) for t in fam]
        if d != "operation" or not base:
            base.append((_OTHER.get(d, "-"), frozenset()))
        if len(base) * 2 ** len(classes) > MAX_CELLS:
            raise TooComplex(f"'{d}' needs too many cases to verify")
        cells = []
        for blabel, bpay in base:
            for bits in range(2 ** len(classes)):
                chosen = [classes[k] for k in range(len(classes)) if bits >> k & 1]
                pay = bpay.union(*[frozenset(c) for c in chosen]) if chosen else bpay
                lab = " + ".join([x for x in [blabel if blabel != "-" else ""] +
                                  [", ".join(_label(t) for t in c) for c in chosen] if x]) or "none of the named values"
                cells.append((lab, pay))
        self.cells[d] = cells

    def _amount_cells(self, d: str, live: List[CRule]) -> None:
        unit = "USD" if d == "amount_usd" else "native units"
        pts = {Decimal(0)}
        for r in live:
            v = r.dims[d]
            if v[0] == "I":
                pts.add(v[1])
                if v[2] is not None:
                    pts.add(v[2])
        b = sorted(pts)
        cells: List[Tuple[str, Any]] = []
        if len(pts) == 1 and not any(r.dims[d][0] == "I" for r in live):
            self.cells[d] = [("any amount", Decimal(0))]
            return
        for i, x in enumerate(b):
            cells.append((f"exactly {x:f} {unit}", x))
            if i + 1 < len(b):
                cells.append((f"between {x:f} and {b[i + 1]:f} {unit} (exclusive)", (x + b[i + 1]) / 2))
            else:
                cells.append((f"above {x:f} {unit}", x + 1))
        self.cells[d] = cells

    @staticmethod
    def _matches(r: CRule, d: str, payload: Any) -> bool:
        v = r.dims[d]
        if v[0] == "U":
            return True
        if v[0] == "I":
            return v[1] <= payload and (v[2] is None or payload <= v[2])
        return bool(v[1] & payload)

    def add(self, r: CRule) -> None:
        for d in DIMS:
            for c, (_, payload) in enumerate(self.cells[d]):
                if r.opaque or self._matches(r, d, payload):
                    self.col[d][c] |= 1 << r.id


def _default(deny: bool) -> str:
    return "BLOCK" if deny else "<NOMATCH>"


def _order(space: Space) -> List[str]:
    return sorted(DIMS, key=lambda d: len(space.cells[d]))


def _decide(items: List[CRule], G: int, rem: frozenset, default: str) -> Optional[tuple]:
    """Outcome of the list in this (partial) cell if it no longer depends on the dimensions in `rem`, else None."""
    trail: List[int] = []
    for r in items:
        if G >> r.id & 1:
            if r.opaque:
                trail.append(r.id)
            elif r.non_universal & rem:
                return None
            else:
                return (tuple(trail), r.verdict)
    return (tuple(trail), default)


def _compare(space: Space, a: List[CRule], b: List[CRule], deny: bool) -> bool:
    """True if a and b give the same outcome in every cell. Raises TooComplex past the node budget."""
    default = _default(deny)
    both = 0
    for r in a + b:
        both |= 1 << r.id
    order = _order(space)
    rems = [frozenset(order[k:]) for k in range(len(order) + 1)]
    nodes = [0]

    def rec(k: int, G: int) -> bool:
        if G & both == 0:
            return True
        oa, ob = _decide(a, G, rems[k], default), _decide(b, G, rems[k], default)
        if oa is not None and ob is not None:
            return oa == ob
        nodes[0] += 1
        if nodes[0] > MAX_NODES:
            raise TooComplex("too many cases to verify")
        return all(rec(k + 1, G & m) for m in space.col[order[k]])
    return rec(0, -1)


def _catch_all(r: CRule) -> bool:
    return not r.opaque and all(d == "operation" or v[0] == "U" or (v[0] == "I" and v[1] <= 0 and v[2] is None) for d, v in r.dims.items())


def _gaps(space: Space, rules: List[CRule]) -> Tuple[List[str], int]:
    """Cases that match no rule at all (only meaningful when there are no opaque rules)."""
    lmask = 0
    for r in rules:
        lmask |= 1 << r.id
    order, found, nodes = _order(space), [], [0]

    def rec(k: int, G: int, chosen: List[Tuple[str, int]]) -> None:
        if G & lmask == 0:
            found.append(list(chosen))
            return
        if k == len(order):
            return
        nodes[0] += 1
        if nodes[0] > MAX_NODES:
            raise TooComplex("too many cases to enumerate")
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
    for k in ("id", "externalDescriptor", "v1RuleId"):
        raw.pop(k, None)
    raw["name"] = f"{a.name} + {b.name}"
    dims = dict(a.dims)
    if d in BLOCK_DIMS and va[0] == "T" and vb[0] == "T" and a.parts[d] == b.parts[d]:
        fld = a.parts[d][1]
        la, lb = a.raw[d][fld], b.raw[d][fld]
        raw[d][fld] = list(la) + [e for e in lb if e not in la]
        dims[d] = ("T", va[1] | vb[1])
    elif d.startswith("amount") and va[0] == "I" and vb[0] == "I":
        if not (va[1] <= (vb[2] if vb[2] is not None else va[1]) and vb[1] <= (va[2] if va[2] is not None else vb[1])):
            return None
        lo = min(va[1], vb[1])
        hi = None if va[2] is None or vb[2] is None else max(va[2], vb[2])
        rng = {"min": _fmt(lo)}
        if hi is not None:
            rng["max"] = _fmt(hi)
        raw["amount"] = {"range": rng, "currency": a.raw["amount"]["currency"]}
        dims[d] = ("I", lo, hi)
    else:
        return None
    return CRule(new_id, raw["name"], raw, dims, a.verdict, False, "", a.parts, a.implies, a.src + b.src)


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
    except TooComplex as exc:
        plan.note = f"Consolidation skipped: {exc}. Only the pairwise findings above apply."
        return plan
    if not plan.opaque and rules and not any(_catch_all(r) for r in rules):
        try:
            plan.gaps, plan.gap_total = _gaps(space, rules)
            plan.gaps = plan.gaps[:MAX_GAPS]
        except TooComplex:
            plan.note = "Coverage gaps could not be enumerated (too many cases)."

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
    if plan.steps:
        plan.rules = [copy.deepcopy(r.raw) for r in cur]
        for i, r in enumerate(plan.rules):
            if "index" in r:
                r["index"] = i
    return plan


def plan_to_dict(plan: Plan) -> Dict[str, Any]:
    return {"assumes_default_deny": plan.default_deny, "original_rule_count": plan.original,
            "proposed_rule_count": len(plan.rules), "steps": [{"kind": s.kind, "original_rules": s.sources,
                                                               "detail": s.detail} for s in plan.steps],
            "coverage_gaps": plan.gaps, "opaque_rules": [{"index": i, "name": n, "reason": r} for i, n, r in plan.opaque],
            "note": plan.note, "rules": plan.rules}
