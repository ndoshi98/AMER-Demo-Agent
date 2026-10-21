"""Sweep a fixed amount of one asset from many vault accounts into one destination vault.

This is the only code in the repo that moves funds, and it does so only through client.call("create_transaction").
Selection is by EXACT balance (not USD value, which drifts) and only vaults whose sole non-zero asset is the swept
one. Every source is re-read live right before it is sent. Nothing here is reachable from run_cleanup.py.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Dict, List, Optional

from .client import DRY_RUN

TERMINAL_FAIL = {"FAILED", "REJECTED", "CANCELLED", "BLOCKED"}
NEEDS_HUMAN = {"PENDING_AUTHORIZATION", "PENDING_3RD_PARTY_MANUAL_APPROVAL"}
FEE_FIELDS = {"LOW": "low", "MEDIUM": "medium", "HIGH": "high"}


class SweepError(RuntimeError):
    pass


@dataclass
class Outcome:
    vault_id: str
    name: str
    status: str            # dry-run | completed | skipped | needs-approval | failed | unconfirmed
    detail: str = ""
    tx_id: str = ""
    fee: Optional[Decimal] = None

    @property
    def ok(self) -> bool:
        return self.status in ("dry-run", "completed")


def _dec(v: Any) -> Decimal:
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError):
        return Decimal("NaN")


def find_destination(vaults: List[Dict[str, Any]], name: str) -> Dict[str, Any]:
    hits = [v for v in vaults if v.get("name") == name]
    if len(hits) != 1:
        raise SweepError(f"destination {name!r} must match exactly one vault, found {len(hits)}")
    return hits[0]


def is_eligible(vault: Dict[str, Any], asset: str, amount: Decimal) -> bool:
    """Only `asset` is held, the whole balance equals `amount`, and all of it is spendable."""
    held = {}
    for a in vault.get("assets") or []:
        total = _dec(a.get("total"))
        if total.is_nan():
            return False
        if total != 0:
            held[a.get("id")] = a
    if set(held) != {asset}:
        return False
    a = held[asset]
    return (_dec(a.get("total")) == amount and _dec(a.get("available")) == amount
            and all(_dec(a.get(k) or 0) == 0 for k in ("pending", "frozen", "lockedAmount", "staked")))


def select_sources(vaults: List[Dict[str, Any]], asset: str, amount: Decimal, dest_id: str) -> List[Dict[str, Any]]:
    out = [v for v in vaults if str(v.get("id")) != str(dest_id) and is_eligible(v, asset, amount)]
    return sorted(out, key=lambda v: int(v["id"]) if str(v["id"]).isdigit() else 0)


def build_request(asset: str, amount: Decimal, source_id: str, dest_id: str, fee_level: str, note: str,
                  gross: bool = True) -> Dict[str, Any]:
    return {"assetId": asset, "amount": format(amount, "f"), "operation": "TRANSFER",
            "source": {"type": "VAULT_ACCOUNT", "id": str(source_id)},
            "destination": {"type": "VAULT_ACCOUNT", "id": str(dest_id)},
            "treatAsGrossAmount": gross,   # native: fee comes out of the amount, so the source ends near zero
            "feeLevel": fee_level, "note": note}


def estimated_fee(est: Any, fee_level: str) -> Optional[Decimal]:
    tier = (est or {}).get(FEE_FIELDS[fee_level]) if isinstance(est, dict) else None
    fee = _dec((tier or {}).get("networkFee")) if isinstance(tier, dict) else Decimal("NaN")
    return None if fee.is_nan() else fee


def sweep_one(client, vault: Dict[str, Any], dest_id: str, asset: str, amount: Decimal, run_id: str, *,
              fee_level: str = "MEDIUM", max_fee_pct: Decimal = Decimal("5"), poll_timeout: int = 240,
              sleep: Callable[[float], None] = time.sleep) -> Outcome:
    vid, name = str(vault["id"]), vault.get("name", "")
    live = client.call("get_vault", vault_account_id=vid)
    if not isinstance(live, dict) or not is_eligible(live, asset, amount):
        return Outcome(vid, name, "skipped", "live balance no longer matches the sweep criteria")
    req = build_request(asset, amount, vid, dest_id, fee_level, f"sweep {asset} to destination ({run_id})")
    fee = estimated_fee(client.call("estimate_fee", transaction_request=req), fee_level)
    if fee is None:
        return Outcome(vid, name, "skipped", "could not read a network fee estimate")
    if fee > amount * max_fee_pct / 100:
        return Outcome(vid, name, "skipped", f"fee {fee} exceeds {max_fee_pct}% of the amount", fee=fee)
    return send_and_wait(client, req, f"sweep-{run_id}-{vid}", vid, name, fee, poll_timeout, sleep)


def send_and_wait(client, req: Dict[str, Any], idem_key: str, vid: str, name: str, fee: Optional[Decimal],
                  poll_timeout: int = 240, sleep: Callable[[float], None] = time.sleep) -> Outcome:
    resp = client.call("create_transaction", idempotency_key=idem_key, transaction_request=req)
    if resp is DRY_RUN:
        return Outcome(vid, name, "dry-run", "would send", fee=fee)
    tx_id = (resp or {}).get("id", "")
    if not tx_id:
        return Outcome(vid, name, "failed", f"no transaction id in response: {resp!r}", fee=fee)
    status, deadline = str((resp or {}).get("status", "")), time.monotonic() + poll_timeout
    while True:
        if status == "COMPLETED":
            return Outcome(vid, name, "completed", tx_id=tx_id, fee=fee)
        if status in TERMINAL_FAIL:
            return Outcome(vid, name, "failed", f"transaction {status}", tx_id, fee)
        if status in NEEDS_HUMAN:
            return Outcome(vid, name, "needs-approval", f"transaction is {status}: approve it in the console", tx_id, fee)
        if time.monotonic() > deadline:
            return Outcome(vid, name, "unconfirmed", f"still {status} after {poll_timeout}s", tx_id, fee)
        sleep(3)
        status = str((client.call("get_transaction", tx_id=tx_id) or {}).get("status", ""))


def run_sweep(client, sources: List[Dict[str, Any]], dest_id: str, asset: str, amount: Decimal, run_id: str,
              on_outcome: Callable[[Outcome], None] = lambda o: None, **kw: Any) -> List[Outcome]:
    """Sweep in order and STOP at the first outcome that is not ok."""
    outcomes: List[Outcome] = []
    for v in sources:
        o = sweep_one(client, v, dest_id, asset, amount, run_id, **kw)
        outcomes.append(o)
        on_outcome(o)
        if not o.ok:
            break
    return outcomes
