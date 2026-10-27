#!/usr/bin/env python3
"""Value every visible vault at LIVE market prices (CoinGecko), per asset, with the price source. READ-ONLY.

  price_vaults.py                              every visible vault, smallest first
  price_vaults.py --max-usd 10                 only vaults worth at most $10 (or containing an unpriced asset)

An asset with no market price (custom testnet tokens, deprecated chains, unlisted tokens) is shown UNPRICED with the reason;
it is never counted as $0. A vault with an unpriced asset is marked so a human can decide. Nothing is written anywhere.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from fbcleanup import collect, prices as pr, sweep, sweep_value as sv  # noqa: E402
from fbcleanup.client import ApiError, FireblocksClient  # noqa: E402
from fbcleanup.config import ConfigError, load_config, load_credentials, load_env_file, scope_paths  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(ROOT / "config" / "cleanup.yaml"))
    ap.add_argument("--env-file", default=None, help="credentials file; its values override the environment")
    ap.add_argument("--max-usd", default=None, help="only show vaults worth at most this (and any with an unpriced asset)")
    ap.add_argument("--include-hidden", action="store_true", help="also value hidden vaults")
    args = ap.parse_args()
    try:
        cap = Decimal(args.max_usd) if args.max_usd is not None else None
    except InvalidOperation:
        ap.error("--max-usd must be a number")
    try:
        if args.env_file:
            load_env_file(args.env_file, override=True)
        else:
            load_env_file(str(ROOT / "config" / ".env"))
        cfg = load_config(args.config)
        state_dir = scope_paths(cfg, ROOT)
        api_key, secret = load_credentials()
        if not cfg.expected_name:
            raise ConfigError("this script requires workspace.expected_name in the config")
    except ConfigError as exc:
        print(f"config/credentials problem: {exc}", file=sys.stderr)
        return 2

    # reads only: dry_run=True suppresses any mutation, and none is called here
    client = FireblocksClient.connect(api_key, secret, cfg.base_path, dry_run=True,
                                      run_id="price-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
                                      journal_path=str(state_dir / "journal.jsonl"),
                                      max_calls_per_second=cfg.max_calls_per_second,
                                      is_production=cfg.is_production, confirmed_prod=True)
    try:
        ws_name = (client.call("get_workspace") or {}).get("name", "")
        if ws_name != cfg.expected_name:
            print(f"REFUSING: workspace is {ws_name!r} but workspace.expected_name is {cfg.expected_name!r}", file=sys.stderr)
            return 3
        vaults, complete = collect.collect_vaults(client)
        if not complete:
            print("REFUSING: vault list incomplete (page cap hit)", file=sys.stderr)
            return 1
        pool = [v for v in vaults if args.include_hidden or not v.get("hiddenOnUI")]
        # the paged listing can omit balances (e.g. deprecated assets), so value each vault from a live read
        stale = []
        for i, v in enumerate(pool):
            live = client.call("get_vault", vault_account_id=str(v["id"]))
            if isinstance(live, dict) and live.get("assets") is not None:
                pool[i] = live
            else:
                stale.append(v["id"])
        if stale:
            print(f"WARNING: could not re-read {len(stale)} vault(s) live; their listing balances may be incomplete: {stale}")
        held = sorted({a["id"] for v in pool for a in v.get("assets") or [] if sweep._dec(a.get("total")) != 0})
        chains = sv.load_blockchains(client)
        res = pr.LivePricer(overrides=cfg.price_overrides).price(pr.describe_assets(client, chains, held))
    except (ApiError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(f"{ws_name} | {len(pool)} vault(s) | {len(res.quotes)}/{len(held)} held assets priced live\n")
    rows = []
    for v in pool:
        total, unpriced, lines = Decimal(0), [], []
        for a in v.get("assets") or []:
            amt = sweep._dec(a.get("total"))
            if amt.is_nan() or amt == 0:
                continue
            q = res.quotes.get(a["id"])
            if q:
                total += amt * q.usd
                lines.append(f"{a['id']} {amt} @ ${q.usd:,.6g} = ${amt * q.usd:,.2f}  [{q.source.split(':')[0]}]")
            else:
                unpriced.append(a["id"])
                lines.append(f"{a['id']} {amt}  UNPRICED: {res.unpriced.get(a['id'], 'unknown')}")
        rows.append((total, bool(unpriced), v, lines))
    for total, has_unpriced, v, lines in sorted(rows, key=lambda r: (r[0], r[2]["id"])):
        if cap is not None and total > cap and not has_unpriced:
            continue
        flag = "  ** HAS UNPRICED ASSET **" if has_unpriced else ""
        print(f"vault {v['id']:>4} {v['name']}  ${total:,.2f}{flag}")
        for ln in lines:
            print(f"      {ln}")
    if res.unpriced:
        print("\nUNPRICED ASSETS (no market price; not counted as $0):")
        for aid, why in sorted(res.unpriced.items()):
            print(f"  {aid:34} {why}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
