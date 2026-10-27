"""Read-only collection. Returns plain data; never mutates. Only READ ops from client.OPS are used.

Every collector reports success/failure per entity type so a partial outage can never be mistaken for
"nothing is flagged" (rules.py + state.observe both honour `Inventory.ok`).
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

Key = Tuple[str, str]
TX_PAGE = 200
ACTIVITY_MARGIN_DAYS = 30   # walk past the longest threshold so "history covers the window" is provable
VAULT_PAGE = 500
PEER_TYPE_TO_ENTITY = {
    "VAULT_ACCOUNT": "vault",
    "INTERNAL_WALLET": "internal_wallet",
    "EXTERNAL_WALLET": "external_wallet",
    "NETWORK_CONNECTION": "network_connection",
}


@dataclass
class Inventory:
    now: int
    vaults: List[Dict[str, Any]] = field(default_factory=list)
    internal_wallets: List[Dict[str, Any]] = field(default_factory=list)
    external_wallets: List[Dict[str, Any]] = field(default_factory=list)
    network_connections: List[Dict[str, Any]] = field(default_factory=list)
    tags: List[Dict[str, Any]] = field(default_factory=list)
    console_users: List[Dict[str, Any]] = field(default_factory=list)
    api_users: List[Dict[str, Any]] = field(default_factory=list)
    activity: Dict[Key, int] = field(default_factory=dict)       # (entity_type, id) -> last tx epoch seconds
    history_start: Optional[int] = None                          # oldest tx the API showed us (epoch s); None if none
    audit_events: List[Dict[str, Any]] = field(default_factory=list)
    policy: Optional[Dict[str, Any]] = None
    policy_source: str = ""
    ok: Set[str] = field(default_factory=set)                    # entity types collected COMPLETELY
    errors: Dict[str, str] = field(default_factory=dict)


# -- helpers ------------------------------------------------------------------------------------
def _items(resp: Any, *keys: str) -> List[Dict[str, Any]]:
    if isinstance(resp, list):
        return resp
    if isinstance(resp, dict):
        for k in keys:
            if isinstance(resp.get(k), list):
                return resp[k]
    return []


def _ts_seconds(v: Any) -> Optional[int]:
    """Accept seconds or milliseconds, int/float/numeric-string."""
    try:
        n = float(v)
    except (TypeError, ValueError):
        return None
    return int(n / 1000) if n > 1e11 else int(n)


# -- collectors ---------------------------------------------------------------------------------
def collect_vaults(client, max_pages: int = 400) -> Tuple[List[Dict[str, Any]], bool]:
    out: List[Dict[str, Any]] = []
    after: Optional[str] = None
    for _ in range(max_pages):
        kw: Dict[str, Any] = {"limit": VAULT_PAGE}
        if after:
            kw["after"] = after
        resp = client.call("list_vaults", **kw)
        out.extend(_items(resp, "accounts"))
        after = ((resp or {}).get("paging") or {}).get("after") if isinstance(resp, dict) else None
        if not after:
            return out, True
    return out, False  # page cap hit: incomplete


def collect_activity(client, since_s: int, max_pages: int = 1000) -> Tuple[Dict[Key, int], bool, Optional[int]]:
    """Walk transaction history newest->oldest, recording the latest tx time per source/destination entity.

    Third element is the oldest transaction time seen: how far back the visible history actually reaches."""
    activity: Dict[Key, int] = {}
    earliest: Optional[int] = None
    before_ms: Optional[int] = None
    after_ms = since_s * 1000
    for _ in range(max_pages):
        kw: Dict[str, Any] = {"after": str(after_ms), "limit": TX_PAGE, "order_by": "createdAt", "sort": "DESC"}
        if before_ms is not None:
            kw["before"] = str(before_ms)
        page = _items(client.call("list_transactions", **kw))
        if not page:
            return activity, True, earliest
        oldest = None
        for tx in page:
            created = tx.get("createdAt")
            ts = _ts_seconds(created)
            if ts is None:
                continue
            earliest = ts if earliest is None else min(earliest, ts)
            oldest = created if oldest is None else min(oldest, created)
            peers = [tx.get("source"), tx.get("destination")]
            peers += [d.get("destination") for d in tx.get("destinations") or []]
            for peer in peers:
                if not peer or not peer.get("id"):
                    continue
                etype = PEER_TYPE_TO_ENTITY.get(str(peer.get("type")))
                if etype:
                    key = (etype, str(peer["id"]))
                    activity[key] = max(activity.get(key, 0), ts)
        if len(page) < TX_PAGE:
            return activity, True, earliest
        if oldest is None or (before_ms is not None and float(oldest) >= before_ms):
            return activity, False, earliest  # no forward progress: refuse to claim completeness
        before_ms = int(float(oldest))
    return activity, False, earliest


def normalize_audit_event(e: Dict[str, Any]) -> Dict[str, Any]:
    return {"ts": _ts_seconds(e.get("timestamp") or e.get("createdAt")), "user_id": e.get("userId") or "",
            "user": (e.get("user") or "").lower(), "event": e.get("event") or "", "category": e.get("category") or "",
            "subject": e.get("subject") or ""}


def collect_audit(client, since_s: int, until_s: int, max_pages: int = 2000) -> Tuple[List[Dict[str, Any]], bool]:
    """Callers pass epoch seconds; the audit API takes epoch MILLISECONDS (per the SDK docs)."""
    events: List[Dict[str, Any]] = []
    cursor: Optional[str] = None
    for _ in range(max_pages):
        kw: Dict[str, Any] = {"start_time": since_s * 1000, "end_time": until_s * 1000, "page_size": 500, "order": "ASC"}
        if cursor:
            kw["page_cursor"] = cursor
        resp = client.call("list_audit_logs", **kw)
        events.extend(normalize_audit_event(e) for e in _items(resp, "data"))
        cursor = (resp or {}).get("next") if isinstance(resp, dict) else None
        if not cursor:
            return events, True
    return events, False


def known_user_ids(inv: Inventory) -> Optional[Dict[str, bool]]:
    """id -> enabled across console AND API users, for TAP dead-reference checks.
    None unless both lists were collected: a partial map would report real signers as missing."""
    if not {"user", "api_user"} <= inv.ok:
        return None
    return {str(u.get("id")): bool(u.get("enabled")) for u in inv.console_users + inv.api_users}


def audit_event_names(events: List[Dict[str, Any]]) -> Counter:
    return Counter((e["category"], e["event"]) for e in events)


def collect_policy(client) -> Tuple[Optional[Dict[str, Any]], str]:
    """Try the v2 policy API first, then legacy. Read-only."""
    last_err = ""
    for op, kw, label in (("get_policy_v2", {"policy_type": "TRANSFER"}, "v2"), ("get_policy_legacy", {}, "legacy")):
        try:
            return client.call(op, **kw), label
        except Exception as exc:  # noqa: BLE001
            last_err = f"{label}: {exc}"
    raise RuntimeError(f"policy unavailable ({last_err})")


POLICY_TYPES = ("TRANSFER", "APPROVE", "CONTRACT_CALL", "DAPP_CONNECTION", "TYPED_MESSAGE", "MINT", "PROGRAM_CALL", "ORDER",
                "DEPLOYMENT", "UPGRADE", "BURN", "STAKE", "AML_CHAINALYSIS_V2_SCREENING",
                "AML_CHAINALYSIS_V2_POST_SCREENING")


def collect_vault_refs(client) -> Set[str]:
    """Every "vault:<id>" referenced by an ACTIVE rule of ANY policy type. Read-only.

    collect_policy() reads TRANSFER only, which misses vaults referenced by e.g. the AML, MINT/BURN and APPROVE rules.
    Raises RuntimeError if any type cannot be read: callers use this to decide what must not be touched, so a partial
    answer must never pass for a complete one."""
    from . import tap, tap_draft   # local import: keeps collect importable without the policy modules
    refs: Set[str] = set()
    for ptype in POLICY_TYPES:
        try:
            resp = client.call("get_policy_v2", policy_type=ptype)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"cannot read the {ptype} policy ({exc})") from exc
        rules = tap_draft.active_rules(resp)
        if rules is None:
            raise RuntimeError(f"unexpected {ptype} policy response; refusing to guess which vaults are referenced")
        for rule in rules:
            found: Set[str] = set()
            tap._collect_refs(rule, found)
            refs |= {r for r in found if r.startswith("vault:")}
    return refs


def collect_all(client, now: int, cfg) -> Inventory:
    """Run every collector; failures are recorded per type, never raised."""
    inv = Inventory(now=now)

    def attempt(etype: str, fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - recorded, run continues in degraded mode
            inv.errors[etype] = str(exc)
            return None

    res = attempt("vault", lambda: collect_vaults(client))
    if res:
        inv.vaults, complete = res
        if complete:
            inv.ok.add("vault")
        else:
            inv.errors["vault"] = "vault pagination cap hit; inventory incomplete"

    for etype, op, attr in (("internal_wallet", "list_internal_wallets", "internal_wallets"),
                            ("external_wallet", "list_external_wallets", "external_wallets"),
                            ("network_connection", "list_network_connections", "network_connections")):
        data = attempt(etype, lambda op=op: client.call(op))
        if data is not None:
            setattr(inv, attr, _items(data, "wallets", "connections"))
            inv.ok.add(etype)

    tags = attempt("tag", lambda: client.call("list_tags", page_size=100))
    if tags is not None:
        inv.tags = _items(tags, "data", "tags")
        # list_tags is paginated; a full page means we may have missed some
        if isinstance(tags, dict) and tags.get("next"):
            inv.errors["tag"] = "tags are paginated and more pages exist (pagination not yet implemented)"
        else:
            inv.ok.add("tag")

    users = attempt("user", lambda: client.call("list_console_users"))
    if users is not None:
        inv.console_users = _items(users, "users")
        inv.ok.add("user")

    api_users = attempt("api_user", lambda: client.call("list_api_users"))
    if api_users is not None:
        inv.api_users = _items(api_users, "users")
        inv.ok.add("api_user")

    longest = max(cfg.vault_no_tx_days, cfg.wallet_no_tx_days, cfg.connection_no_tx_days) + ACTIVITY_MARGIN_DAYS
    act = attempt("activity", lambda: collect_activity(client, now - longest * 86400))
    if act:
        inv.activity = act[0]
        inv.history_start = act[2]
        if act[1]:
            inv.ok.add("activity")
        else:
            inv.errors["activity"] = "transaction history walk incomplete"

    lookback = cfg.audit_lookback_days * 86400
    aud = attempt("audit", lambda: collect_audit(client, now - lookback, now))
    if aud:
        inv.audit_events = aud[0]
        if aud[1]:
            inv.ok.add("audit")
        else:
            inv.errors["audit"] = "audit log pagination incomplete"

    pol = attempt("policy", lambda: collect_policy(client))
    if pol:
        inv.policy, inv.policy_source = pol
        inv.ok.add("policy")
    return inv
