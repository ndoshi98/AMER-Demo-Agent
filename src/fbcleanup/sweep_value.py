"""Sweep every asset out of every low-value vault into one destination vault.

A vault is eligible when it holds something and its USD total is at or under a cap. Testnet assets are priced like
mainnet ones (the console reports a USD value on testnet transactions); any asset with no trustworthy price makes its
vault unvaluable, so the vault is left alone. Per asset,
anything that cannot move cleanly (pending/frozen/locked/staked balance, fee not covered, no gas for a token, no
wallet creatable in the destination) is skipped and reported. Tokens go before the native asset so gas is still there.
Fund movement goes only through client.call("create_transaction"); see sweep.py for the shared send/poll logic.
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional

from .client import DRY_RUN, ApiError
from .sweep import Outcome, _dec, build_request, estimated_fee, send_and_wait


@dataclass
class AssetInfo:
    asset_id: str
    testnet: bool
    native: bool
    gas_asset: str


@dataclass
class Item:
    asset: str
    amount: Decimal
    usd: Decimal
    skip: str = ""


@dataclass
class VaultPlan:
    vault: Dict[str, Any]
    items: List[Item] = field(default_factory=list)
    usd: Decimal = Decimal(0)
    excluded: str = ""


def load_blockchains(client) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    cursor = None
    for _ in range(50):
        kw: Dict[str, Any] = {"page_size": 500}
        if cursor:
            kw["page_cursor"] = cursor
        r = client.call("list_blockchains", **kw) or {}
        for b in r.get("data") or []:
            out[b["id"]] = b
        cursor = r.get("next")
        if not cursor:
            return out
    raise RuntimeError("blockchain list did not terminate")


def asset_info(client, blockchains: Dict[str, Dict[str, Any]], asset_id: str) -> Optional[AssetInfo]:
    """None if the asset or its chain can't be resolved: callers must treat that as unknown, never as testnet."""
    try:
        a = client.call("get_asset", id=asset_id) or {}
    except ApiError:
        return None
    b = blockchains.get(a.get("blockchainId"))
    test = ((b or {}).get("onchain") or {}).get("test")
    if b is None or not isinstance(test, bool):
        return None
    return AssetInfo(asset_id, test, a.get("id") == b.get("nativeAssetId"), b.get("legacyId") or "")


PRICE_FLOOR = Decimal("1e-9")
PRICE_CEIL = Decimal("1e7")
MAX_PRICE_AGE_DAYS = 60
MAX_PRICE_SPREAD = Decimal(3)


def usd_price(client, asset_id: str, now: Optional[float] = None) -> Optional[Decimal]:
    """USD per unit from recent completed transactions in this asset that carry a USD value (newest price used).

    None (= unvalued, vault left alone) if there is none within MAX_PRICE_AGE_DAYS, the price is outside a plausible
    range, or the recent prices disagree by more than MAX_PRICE_SPREAD."""
    now = time.time() if now is None else now
    txs = client.call("list_transactions", assets=asset_id, status="COMPLETED", limit=10,
                      order_by="createdAt", sort="DESC")
    prices: List[Decimal] = []
    for t in txs if isinstance(txs, list) else []:
        amt, usd = _dec(t.get("amount")), _dec(t.get("amountUSD"))
        created = t.get("createdAt")
        if (amt.is_nan() or usd.is_nan() or amt <= 0 or usd <= 0 or not isinstance(created, (int, float))
                or now - created / 1000 > MAX_PRICE_AGE_DAYS * 86400):
            continue
        prices.append(usd / amt)
    if not prices or not (PRICE_FLOOR <= prices[0] <= PRICE_CEIL) or max(prices) > min(prices) * MAX_PRICE_SPREAD:
        return None
    return prices[0]


def _clean(a: Dict[str, Any]) -> Optional[str]:
    """Reason this balance can't move as a whole, or None."""
    total, avail = _dec(a.get("total")), _dec(a.get("available"))
    if total.is_nan() or avail.is_nan():
        return "unreadable balance"
    others = [k for k in ("pending", "frozen", "lockedAmount", "staked") if _dec(a.get(k) or 0) != 0]
    if others:
        return "has " + "/".join(others) + " balance"
    return None if avail == total else "not all of the balance is available"


def plan_vaults(vaults: List[Dict[str, Any]], dest_id: str, infos: Dict[str, Optional[AssetInfo]],
                prices: Dict[str, Optional[Decimal]], max_usd: Decimal,
                margin: Decimal = Decimal("0.10")) -> List[VaultPlan]:
    plans: List[VaultPlan] = []
    for v in sorted(vaults, key=lambda v: int(v["id"]) if str(v["id"]).isdigit() else 0):
        if str(v["id"]) == str(dest_id):
            continue
        held = [a for a in v.get("assets") or [] if _dec(a.get("total")) != 0]
        if not held:
            continue
        p = VaultPlan(v)
        for a in held:
            aid, total = a["id"], _dec(a.get("total"))
            info = infos.get(aid)
            if total.is_nan() or info is None:
                p.excluded = f"cannot classify {aid}"
                break
            if prices.get(aid) is None:
                p.excluded = f"no USD price for {'testnet' if info.testnet else 'mainnet'} asset {aid}"
                break
            usd = total * prices[aid]
            p.items.append(Item(aid, total, usd, _clean(a) or ""))
            p.usd += usd
        if not p.excluded and p.usd > max_usd:
            p.excluded = f"over cap (${p.usd:,.2f})"
        elif not p.excluded and p.usd * (1 + margin) > max_usd:
            p.excluded = f"${p.usd:,.2f} is within {margin:.0%} of the cap and prices may be stale"
        if p.excluded:
            p.items = []
        else:
            p.items.sort(key=lambda i: infos[i.asset].native)   # tokens first (False < True), native last
        plans.append(p)
    return plans


def _idem_key(run_id: str, vid: str, asset: str) -> str:
    """Deterministic per run/vault/asset; the API caps idempotency keys at 40 characters."""
    return "sv-" + hashlib.sha256(f"{run_id}|{vid}|{asset}".encode()).hexdigest()[:30]


def _asset_row(vault: Dict[str, Any], asset: str) -> Optional[Dict[str, Any]]:
    return next((a for a in vault.get("assets") or [] if a.get("id") == asset), None)


def sweep_item(client, plan: VaultPlan, item: Item, info: AssetInfo, dest_id: str, dest_assets: set, planned: set, run_id: str, *,
               fee_level: str, max_fee_pct: Decimal, poll_timeout: int, sleep: Callable[[float], None]) -> Outcome:
    v = plan.vault
    vid, label = str(v["id"]), f"{v.get('name', '')} [{item.asset}]"
    if item.skip:
        return Outcome(vid, label, "skipped", item.skip)
    live = client.call("get_vault", vault_account_id=vid)
    row = _asset_row(live, item.asset) if isinstance(live, dict) else None
    if row is None or _clean(row) or _dec(row.get("total")) != item.amount:
        return Outcome(vid, label, "skipped", "live balance changed since the plan")
    amount = item.amount

    created = ""
    if item.asset in planned:
        return Outcome(vid, label, "dry-run", "would send (destination wallet is created earlier in this plan)")
    if item.asset not in dest_assets:
        try:
            r = client.call("create_vault_asset", vault_account_id=str(dest_id), asset_id=item.asset)
        except ApiError as exc:
            return Outcome(vid, label, "skipped", f"could not create the wallet in the destination: {exc}".splitlines()[0])
        if r is DRY_RUN:
            planned.add(item.asset)
            return Outcome(vid, label, "dry-run", "would create destination wallet, then send")
        dest_assets.add(item.asset)
        created = "created destination wallet; "

    req = build_request(item.asset, amount, vid, dest_id, fee_level, f"sweep {item.asset} to destination ({run_id})",
                        gross=info.native)
    try:
        fee = estimated_fee(client.call("estimate_fee", transaction_request=req), fee_level)
    except ApiError as exc:
        return Outcome(vid, label, "skipped", f"fee estimate failed: {exc}".splitlines()[0])
    if fee is None:
        return Outcome(vid, label, "skipped", "could not read a network fee estimate")
    if info.native:
        if fee >= amount or fee > amount * max_fee_pct / 100:
            return Outcome(vid, label, "skipped", f"fee {fee} too large for a balance of {amount}", fee=fee)
    else:
        gas = _asset_row(live, info.gas_asset)
        if gas is None or _dec(gas.get("available")) < fee * Decimal("1.5"):
            return Outcome(vid, label, "skipped", f"not enough {info.gas_asset} in the vault to pay the fee ({fee})", fee=fee)
    try:
        o = send_and_wait(client, req, _idem_key(run_id, vid, item.asset), vid, label, fee, poll_timeout, sleep)
    except ApiError as exc:
        text = str(exc).splitlines()[0]
        if exc.status in (400, 422) or "BadRequest" in text:   # rejected before anything was created or sent
            return Outcome(vid, label, "skipped", f"API rejected the transfer: {text}", fee=fee)
        return Outcome(vid, label, "failed", f"API error: {text}", fee=fee)
    o.detail = (created + o.detail).strip("; ")
    return o


def run_value_sweep(client, plans: List[VaultPlan], infos: Dict[str, Optional[AssetInfo]], dest_id: str,
                    dest_assets: set, run_id: str, *, limit: Optional[int] = None, fee_level: str = "MEDIUM",
                    max_fee_pct: Decimal = Decimal("25"), poll_timeout: int = 240,
                    on_outcome: Callable[[Outcome], None] = lambda o: None,
                    sleep: Callable[[float], None] = time.sleep) -> List[Outcome]:
    """Stops at the first failed / unconfirmed / needs-approval transfer; plain skips do not stop the run.
    `limit` caps transfers actually sent (completed)."""
    outcomes: List[Outcome] = []
    sent, planned = 0, set()
    for p in plans:
        if p.excluded:
            continue
        for item in p.items:
            if limit is not None and sent >= limit:
                return outcomes
            o = sweep_item(client, p, item, infos[item.asset], dest_id, dest_assets, planned, run_id, fee_level=fee_level,
                           max_fee_pct=max_fee_pct, poll_timeout=poll_timeout, sleep=sleep)
            outcomes.append(o)
            on_outcome(o)
            sent += o.status == "completed"
            if not o.ok and o.status != "skipped":
                return outcomes
    return outcomes
