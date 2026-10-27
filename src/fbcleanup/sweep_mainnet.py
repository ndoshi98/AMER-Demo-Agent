"""Sweep every non-frozen MAINNET balance into one destination vault, then hide vaults that are worth little.

Like sweep_value.py, only vaults whose total USD value is at most the cap are touched (a bigger or unvaluable vault is
left alone entirely), but here only mainnet assets move. Testnet balances never move; they stay in the vault and count toward its USD value
(testnet is priced like mainnet). A vault is hidden when, after the sweep, everything left is fully priced and worth at
most the hide cap. Anything that cannot move cleanly (pending/frozen/locked/staked balance, deprecated asset, no gas
for a token, fee not covered) is skipped and reported by vault so a human can decide. Fund movement goes only through
sweep_value.sweep_item -> client.call("create_transaction").
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, List, Optional

from .sweep import _dec
from .sweep_value import AssetInfo, Item, VaultPlan, _clean, remaining_usd


@dataclass
class MainnetPlan(VaultPlan):
    # USD value predicted to remain after every planned transfer succeeds (None = cannot be valued)
    remaining: Optional[Decimal] = None
    remaining_why: str = ""
    notes: List[str] = field(default_factory=list)


def _gas_missing(vault: Dict[str, Any], info: AssetInfo) -> bool:
    """A token needs its chain's native asset in the same vault to pay the fee."""
    if info.native:
        return False
    row = next((a for a in vault.get("assets") or [] if a.get("id") == info.gas_asset), None)
    return row is None or _dec(row.get("available")) <= 0


def plan_mainnet(vaults: List[Dict[str, Any]], dest_id: str, infos: Dict[str, Optional[AssetInfo]],
                 prices: Dict[str, Optional[Decimal]], holds: Dict[str, str], max_usd: Decimal) -> List[MainnetPlan]:
    """Only vaults worth at most max_usd BEFORE the sweep (testnet + mainnet, priced alike) are touched; a bigger or
    unvaluable vault is left alone entirely."""
    plans: List[MainnetPlan] = []
    for v in sorted(vaults, key=lambda v: int(v["id"]) if str(v["id"]).isdigit() else 0):
        if str(v["id"]) == str(dest_id):
            continue
        p = MainnetPlan(v)
        if str(v["id"]) in holds:
            p.excluded = holds[str(v["id"])]
            plans.append(p)
            continue
        pre, pre_why = remaining_usd(v, infos, prices)
        if pre is None or pre > max_usd:
            p.excluded = f"cannot be valued: {pre_why}" if pre is None else f"over ${max_usd} (${pre:,.2f})"
            p.remaining, p.remaining_why = pre, pre_why
            plans.append(p)
            continue
        left = Decimal(0)
        why = ""
        for a in v.get("assets") or []:
            aid, total = a["id"], _dec(a.get("total"))
            if total.is_nan():
                why = why or f"unreadable balance for {aid}"
                continue
            if total == 0:
                continue
            info, price = infos.get(aid), prices.get(aid)
            usd = total * price if price is not None else Decimal(0)
            if info is None:
                p.notes.append(f"{aid}: cannot classify as mainnet/testnet; not moved")
                why = why or f"cannot classify {aid}"
                continue
            if info.testnet:
                if price is None and not info.deprecated:
                    why = why or f"no USD price for testnet asset {aid}"
                left += usd
                continue
            skip = _clean(a)
            if not skip and info.deprecated:
                skip = "deprecated asset"
            if not skip and _gas_missing(v, info):
                skip = f"no {info.gas_asset} in the vault to pay the fee"
            p.items.append(Item(aid, total, usd, skip or ""))
            if price is None:
                p.notes.append(f"{aid}: mainnet asset with no USD price")
            if skip:
                if price is None:
                    why = why or f"unswept mainnet {aid} has no USD price"
                left += usd
        p.items.sort(key=lambda i: infos[i.asset].native)   # tokens first, native last so gas is still there
        p.remaining, p.remaining_why = (None, why) if why else (left, "")
        p.usd = sum((i.usd for i in p.items), Decimal(0))
        if not p.items:
            continue
        plans.append(p)
    return plans


def stuck_items(plans: List[MainnetPlan]) -> List[tuple]:
    """(vault, item) for every mainnet balance that will not be swept, with the reason."""
    return [(p.vault, i) for p in plans if not p.excluded for i in p.items if i.skip]
