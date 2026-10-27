#!/usr/bin/env python3
"""Sweep every non-zero Canton-chain asset (CANTON, CANTON_TEST, CANTON_USDC, ...) into one destination vault. MOVES FUNDS.

The other sweep scripts deliberately skip Canton/DTCC vaults; this one exists only because the owner asked for it.
DRY RUN by default.

  sweep_canton.py --dest-name "Destination Vault"                      plan only
  sweep_canton.py ... --execute --limit 1                      send ONE transfer (the test)
  sweep_canton.py ... --execute --all                          send everything in the plan

A balance with pending/frozen/locked/staked amounts is skipped and reported. Missing destination wallets are created.
Stops at the first failed, unconfirmed or approval-pending transfer. Production needs --i-understand-this-is-prod.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from fbcleanup import collect, sweep, sweep_value as sv  # noqa: E402
from fbcleanup.client import ApiError, FireblocksClient  # noqa: E402
from fbcleanup.config import (ConfigError, enforce_prod_gate, load_config, load_credentials, load_env_file,  # noqa: E402
                              scope_paths)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(ROOT / "config" / "cleanup.yaml"))
    ap.add_argument("--env-file", default=None, help="credentials file; its values override the environment")
    ap.add_argument("--dest-name", required=True, help="exact name of the destination vault account")
    ap.add_argument("--fee-level", default="MEDIUM", choices=["LOW", "MEDIUM", "HIGH"])
    ap.add_argument("--poll-timeout", type=int, default=240, help="seconds to wait for each transfer to complete")
    ap.add_argument("--execute", action="store_true", help="actually send transfers (default: dry run)")
    ap.add_argument("--limit", type=int, default=None, help="with --execute: send at most N transfers")
    ap.add_argument("--all", action="store_true", help="with --execute: send everything in the plan")
    ap.add_argument("--i-understand-this-is-prod", action="store_true")
    args = ap.parse_args()
    if args.execute and (args.all == (args.limit is not None)):
        ap.error("--execute needs exactly one of --limit N or --all")
    if args.limit is not None and args.limit < 1:
        ap.error("--limit must be at least 1")
    try:
        if args.env_file:
            load_env_file(args.env_file, override=True)
        else:
            load_env_file(str(ROOT / "config" / ".env"))
        cfg = load_config(args.config)
        enforce_prod_gate(cfg, args.i_understand_this_is_prod)
        state_dir = scope_paths(cfg, ROOT)
        api_key, secret = load_credentials()
        if not cfg.expected_name:
            raise ConfigError("sweeping requires workspace.expected_name in the config")
    except ConfigError as exc:
        print(f"config/credentials problem: {exc}", file=sys.stderr)
        return 2

    run_id = "sweep-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    client = FireblocksClient.connect(api_key, secret, cfg.base_path, dry_run=not args.execute, run_id=run_id,
                                      journal_path=str(state_dir / "journal.jsonl"),
                                      max_calls_per_second=cfg.max_calls_per_second,
                                      is_production=cfg.is_production, confirmed_prod=args.i_understand_this_is_prod)
    print(f"{run_id} | {'EXECUTE' if args.execute else 'DRY RUN'} | prod={cfg.is_production} | "
          f"workspace={cfg.expected_name!r}")
    try:
        ws_name = (client.call("get_workspace") or {}).get("name", "")
        if ws_name != cfg.expected_name:
            print(f"REFUSING: workspace is {ws_name!r} but workspace.expected_name is {cfg.expected_name!r}", file=sys.stderr)
            return 3
        vaults, complete = collect.collect_vaults(client)
        if not complete:
            print("REFUSING: vault list incomplete (page cap hit)", file=sys.stderr)
            return 1
        dest = sweep.find_destination(vaults, args.dest_name)
        dest_id = str(dest["id"])
        dest_assets = {a["id"] for a in dest.get("assets") or []}
        chains = sv.load_blockchains(client)

        infos, plans = {}, []
        for v in sorted(vaults, key=lambda v: int(v["id"]) if str(v["id"]).isdigit() else 0):
            if str(v["id"]) == dest_id:
                continue
            plan = sv.VaultPlan(v)
            for a in v.get("assets") or []:
                total = sweep._dec(a.get("total"))
                if "CANTON" not in a["id"].upper() or total.is_nan() or total == 0:
                    continue
                if a["id"] not in infos:
                    infos[a["id"]] = sv.asset_info(client, chains, a["id"])
                if infos[a["id"]] is None:
                    print(f"  vault {v['id']:>4} {v['name']}: cannot classify {a['id']}; left alone")
                    continue
                plan.items.append(sv.Item(a["id"], total, Decimal(0), sv._clean(a) or ""))
            if plan.items:
                plan.items.sort(key=lambda i: infos[i.asset].native)
                plans.append(plan)

        todo = sum(1 for p in plans for i in p.items if not i.skip)
        print(f"destination: {dest['name']!r} (vault {dest_id}) | {len(plans)} source vault(s), {todo} transfer(s)")
        for p in plans:
            print(f"  vault {p.vault['id']:>4} {p.vault['name']}")
            for i in p.items:
                print(f"      {i.asset:24} {i.amount}" + (f"  SKIP: {i.skip}" if i.skip else "") +
                      ("" if i.asset in dest_assets or i.skip else "  (needs destination wallet)"))
        if not todo:
            print("nothing to send")
            return 0

        def show(o: sweep.Outcome) -> None:
            fee = f" fee~{o.fee}" if o.fee is not None else ""
            print(f"  {o.status:14} vault {o.vault_id:>4} {o.name}{fee} {o.tx_id} {o.detail}".rstrip(), flush=True)

        outcomes = sv.run_value_sweep(client, plans, infos, dest_id, dest_assets, run_id, limit=args.limit,
                                      fee_level=args.fee_level, max_fee_pct=Decimal("25"),
                                      poll_timeout=args.poll_timeout, on_outcome=show)
        done = sum(o.status == "completed" for o in outcomes)
        skipped = sum(o.status == "skipped" for o in outcomes)
        bad = [o for o in outcomes if not o.ok and o.status != "skipped"]
        if not args.execute:
            print("dry run: nothing sent or created. Re-run with --execute --limit 1, then --execute --all.")
        else:
            print(f"{done} completed, {skipped} skipped, {len(bad)} stopped the run")
        return 1 if bad else 0
    except (ApiError, sweep.SweepError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
