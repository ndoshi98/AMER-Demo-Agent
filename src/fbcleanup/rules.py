"""Pure classification. No API calls, no I/O. Inventory + config in, Decision objects out.

A Decision with flagged=False is a KEEP (with reasons) — the report shows near-calls so a human can see
what was protected and why. Quarantine (state.py) decides when a flagged Decision becomes actionable.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

from .collect import Inventory
from .config import Config, Protect

DAY = 86400

# action names (actions.py maps these to client ops)
HIDE_VAULT = "hide_vault"
DELETE_INTERNAL_WALLET = "delete_internal_wallet"
DELETE_EXTERNAL_WALLET = "delete_external_wallet"
DELETE_NETWORK_CONNECTION = "delete_network_connection"
DELETE_TAG = "delete_tag"
REQUEST_USER_DELETION = "request_user_deletion"
SUGGEST_RENAME = "suggest_rename"

TIER = {HIDE_VAULT: 1, DELETE_INTERNAL_WALLET: 2, DELETE_EXTERNAL_WALLET: 2, DELETE_NETWORK_CONNECTION: 2,
        DELETE_TAG: 2, REQUEST_USER_DELETION: 2, SUGGEST_RENAME: 3}

_BALANCE_FIELDS_REQUIRED = ("total",)
_BALANCE_FIELDS_ZERO_IF_PRESENT = ("pending", "staked", "available", "frozen", "lockedAmount")


@dataclass
class Decision:
    entity_type: str
    entity_id: str
    name: str
    action: str
    flagged: bool
    reasons: List[str] = field(default_factory=list)     # why flagged
    keep_reasons: List[str] = field(default_factory=list)  # why NOT flagged / protected
    manual: str = ""                                       # human-only follow-up, never executed

    @property
    def key(self) -> Tuple[str, str]:
        return (self.entity_type, self.entity_id)

    @property
    def tier(self) -> int:
        return TIER[self.action]


# -- helpers ------------------------------------------------------------------------------------
def _dec(v: Any) -> Optional[Decimal]:
    try:
        d = Decimal(str(v))
        return d if d.is_finite() else None
    except (InvalidOperation, ValueError):
        return None


def vault_balances_zero(vault: Dict[str, Any]) -> Tuple[bool, str]:
    """Exactly zero on EVERY asset: total, pending, staked (plus available/frozen/locked when present).

    No USD conversion on purpose. Unparseable or missing `total` => not zero (conservative: keep).
    """
    for asset in vault.get("assets") or []:
        aid = asset.get("id", "?")
        for f in _BALANCE_FIELDS_REQUIRED:
            d = _dec(asset.get(f))
            if d is None:
                return False, f"asset {aid}: '{f}' missing or unparseable"
            if d != 0:
                return False, f"asset {aid}: {f}={asset.get(f)}"
        for f in _BALANCE_FIELDS_ZERO_IF_PRESENT:
            if asset.get(f) in (None, ""):
                continue
            d = _dec(asset.get(f))
            if d is None or d != 0:
                return False, f"asset {aid}: {f}={asset.get(f)}"
    return True, ""


def _protected(name: str, ident: str, p: Protect, extra_ref: str = "", tag_labels: Iterable[str] = ()) -> List[str]:
    why = []
    if ident in p.ids:
        why.append("explicitly protected id")
    for pat in p.name_patterns:
        if re.search(pat, name or "", re.IGNORECASE):
            why.append(f"name matches protect pattern '{pat}'")
    if extra_ref and any(extra_ref.startswith(pre) for pre in p.customer_ref_id_prefixes):
        why.append("customerRefId matches protected prefix")
    wanted = {t.lower() for t in p.tag_labels}
    hit = sorted({t.lower() for t in tag_labels} & wanted)
    if hit:
        why.append(f"carries protected tag(s) {hit}")
    return why


def _recent(inv: Inventory, key: Tuple[str, str], days: int) -> Optional[int]:
    last = inv.activity.get(key)
    return last if last is not None and inv.now - last < days * DAY else None


def _fmt(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def evaluated_types(inv: Inventory) -> Set[str]:
    """Entity types we had enough complete data to judge this run."""
    ev: Set[str] = set()
    act = "activity" in inv.ok
    pol = "policy" in inv.ok
    if "vault" in inv.ok and act:
        ev.add("vault")
    for t in ("internal_wallet", "external_wallet", "network_connection"):
        if t in inv.ok and act and pol:
            ev.add(t)
    if "tag" in inv.ok and "vault" in inv.ok and pol:
        ev.add("tag")
    if "user" in inv.ok and "audit" in inv.ok and pol:
        ev.add("user")
    return ev


# -- classifiers --------------------------------------------------------------------------------
def classify_vaults(inv: Inventory, cfg: Config, policy_refs: Optional[Set[str]]) -> List[Decision]:
    out = []
    for v in inv.vaults:
        vid, name = str(v.get("id")), v.get("name") or ""
        keep: List[str] = []
        if v.get("hiddenOnUI"):
            keep.append("already hidden")
        zero, why = vault_balances_zero(v)
        if not zero:
            keep.append(f"non-zero or unverifiable balance ({why})")
        last = _recent(inv, ("vault", vid), cfg.vault_no_tx_days)
        if last:
            keep.append(f"transaction on {_fmt(last)} (within {cfg.vault_no_tx_days}d)")
        labels = [t.get("label", "") for t in v.get("tags") or []]
        keep += _protected(name, vid, cfg.vault_protect, v.get("customerRefId") or "", labels)
        if policy_refs and f"vault:{vid}" in policy_refs:
            keep.append("referenced by a TAP rule")
        flagged = not keep
        out.append(Decision("vault", vid, name, HIDE_VAULT, flagged,
                            [f"all assets exactly zero, no transactions in {cfg.vault_no_tx_days}d"] if flagged else [],
                            keep))
    return out


def _classify_wallets(inv, cfg, policy_refs, etype, wallets, action) -> List[Decision]:
    out = []
    for w in wallets:
        wid, name = str(w.get("id")), w.get("name") or ""
        keep = _protected(name, wid, cfg.wallet_protect)
        last = _recent(inv, (etype, wid), cfg.wallet_no_tx_days)
        if last:
            keep.append(f"transaction on {_fmt(last)} (within {cfg.wallet_no_tx_days}d)")
        if policy_refs is not None and f"wallet:{wid}" in policy_refs:
            keep.append("referenced by a TAP rule")
        flagged = not keep
        out.append(Decision(etype, wid, name, action, flagged,
                            [f"no transactions in {cfg.wallet_no_tx_days}d and not referenced by policy"] if flagged else [],
                            keep))
    return out


def _net_label(side: Any) -> str:
    if isinstance(side, dict):
        return str(side.get("name") or side.get("id") or "?")
    return str(side or "?")


def classify_connections(inv: Inventory, cfg: Config, policy_refs: Optional[Set[str]]) -> List[Decision]:
    """Flag only on positive evidence of disuse: no tx in the window AND visible history reaches back that far.

    Otherwise "never used" is indistinguishable from "created recently" (no creation date is exposed), so the
    connection is kept and listed for a manual decision."""
    days = cfg.connection_no_tx_days
    covered = inv.history_start is not None and inv.history_start <= inv.now - days * DAY
    out = []
    for c in inv.network_connections:
        cid = str(c.get("id"))
        name = f"{_net_label(c.get('localNetworkId'))} <-> {_net_label(c.get('remoteNetworkId'))}"
        keep, manual = [], ""
        if cid in cfg.connection_protect.ids:
            keep.append("explicitly protected id")
        last = _recent(inv, ("network_connection", cid), days)
        if last:
            keep.append(f"transaction on {_fmt(last)} (within {days}d)")
        elif not covered:
            seen = f"history visible only from {_fmt(inv.history_start)}" if inv.history_start else "no transactions visible"
            keep.append(f"unproven: no transaction visible, but {seen}, less than {days}d")
            manual = (f"no transaction seen, but the API shows less than {days} days of history, so unused cannot be "
                      "told from newly created; decide by hand")
        if policy_refs is not None and f"connection:{cid}" in policy_refs:
            keep.append("referenced by a TAP rule")
        flagged = not keep
        out.append(Decision("network_connection", cid, name, DELETE_NETWORK_CONNECTION, flagged,
                            [f"no transactions in {days}d (visible history covers the full window)"] if flagged else [],
                            keep, manual))
    return out


def classify_tags(inv: Inventory, cfg: Config) -> List[Decision]:
    in_use = {str(t.get("id")) for v in inv.vaults for t in v.get("tags") or []}
    protect = {l.lower() for l in cfg.tag_protect_labels}
    out = []
    for t in inv.tags:
        tid, label = str(t.get("id")), t.get("label") or ""
        keep = []
        if t.get("isProtected"):
            keep.append("tag is protected")
        if label.lower() in protect:
            keep.append("label in protect list")
        if t.get("pendingApprovalRequest"):
            keep.append("has a pending approval request")
        allowed = t.get("allowedEntityTypes") or ["vault_account"]
        if set(allowed) != {"vault_account"}:
            keep.append(f"can attach to {sorted(set(allowed) - {'vault_account'})}; usage there is not checked")
        if tid in in_use:
            keep.append("attached to at least one vault")
        flagged = not keep
        out.append(Decision("tag", tid, label, DELETE_TAG, flagged,
                            ["not attached to any vault"] if flagged else [], keep))
    return out


def classify_users(inv: Inventory, cfg: Config, policy_refs: Optional[Set[str]],
                   last_seen: Callable[[str], Optional[int]], coverage_start: int) -> List[Decision]:
    out = []
    cutoff = inv.now - cfg.user_inactive_days * DAY
    for u in inv.console_users:
        uid, email = str(u.get("id")), (u.get("email") or "").lower()
        name = f"{u.get('firstName', '')} {u.get('lastName', '')}".strip() or email
        label = f"{name} <{email}> [{u.get('role')}]"
        keep: List[str] = []
        manual = ""
        if str(u.get("userType") or "CONSOLE").upper() != "CONSOLE":
            keep.append("not a console user")
        if u.get("enabled") is False:
            keep.append("already disabled")
        status = str(u.get("status") or "READY").upper()
        if status != "READY":
            keep.append(f"status {status}")
            manual = "Never finished onboarding: revoke the invite in the console (there is nothing to delete)."
        if str(u.get("role") or "").upper() in cfg.user_protect_roles:
            keep.append(f"role {u.get('role')} is protected")
        if email in cfg.user_protect_emails:
            keep.append("email in protect list")
        if policy_refs is not None and f"user:{uid}" in policy_refs:
            keep.append("referenced in a TAP rule")
        seen = max([s for s in (last_seen(uid), last_seen(email)) if s is not None], default=None)
        if seen is not None and seen >= cutoff:
            keep.append(f"audit activity on {_fmt(seen)} (within {cfg.user_inactive_days}d)")
        elif seen is None and coverage_start > cutoff:
            keep.append("no audit activity seen, but our audit coverage is shorter than the inactivity window")
        flagged = not keep
        reason = (f"last audit activity {_fmt(seen)}" if seen else
                  f"no audit activity since at least {_fmt(coverage_start)}") + \
                 f"; inactivity window {cfg.user_inactive_days}d (audit events are weak evidence of console use)"
        out.append(Decision("user", uid, label, REQUEST_USER_DELETION, flagged, [reason] if flagged else [],
                            keep, manual))
    return out


def suggest_renames(inv: Inventory) -> List[Decision]:
    """Tier 3: list only. A vault's name is how an SE finds it mid-demo."""
    by_name: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for v in inv.vaults:
        by_name[(v.get("name") or "").strip().lower()].append(v)
    out = []
    for norm, vs in by_name.items():
        if len(vs) > 1 and norm:
            for v in vs:
                out.append(Decision("vault", str(v.get("id")), v.get("name") or "", SUGGEST_RENAME, False, [], [],
                                    f"name is shared by {len(vs)} vaults; consider disambiguating by hand"))
    return out


def classify_all(inv: Inventory, cfg: Config, last_seen: Callable[[str], Optional[int]], coverage_start: int,
                 policy_refs: Optional[Set[str]]) -> List[Decision]:
    ev = evaluated_types(inv)
    out: List[Decision] = []
    if "vault" in ev:
        out += classify_vaults(inv, cfg, policy_refs)
        out += suggest_renames(inv)
    if "internal_wallet" in ev:
        out += _classify_wallets(inv, cfg, policy_refs, "internal_wallet", inv.internal_wallets, DELETE_INTERNAL_WALLET)
    if "external_wallet" in ev:
        out += _classify_wallets(inv, cfg, policy_refs, "external_wallet", inv.external_wallets, DELETE_EXTERNAL_WALLET)
    if "network_connection" in ev:
        out += classify_connections(inv, cfg, policy_refs)
    if "tag" in ev:
        out += classify_tags(inv, cfg)
    if "user" in ev:
        out += classify_users(inv, cfg, policy_refs, last_seen, coverage_start)
    return out
