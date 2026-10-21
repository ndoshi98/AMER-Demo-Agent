#!/usr/bin/env python3
"""Sweep a fixed amount of one asset from many vaults into one destination vault. MOVES FUNDS.

DRY RUN by default: lists the vaults that would be swept and the fee estimate; sends nothing.

  sweep.py --asset ETH --amount 0.001 --dest-name "Treasury Demo"                  plan only
  sweep.py ... --execute --limit 1                                                 send ONE (the test sweep)
  sweep.py ... --execute --all                                                     send every eligible vault

A vault is eligible only if ETH (or --asset) is its sole non-zero asset, its balance equals --amount exactly, and all
of it is available (nothing pending/frozen/locked/staked). Balances are re-read live before each send, the fee comes
out of the amount (source ends at zero), and the run stops at the first transfer that is not COMPLETED.
Production workspaces also need --i-understand-this-is-prod. Transfers are irreversible.
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from fbcleanup import collect, sweep  # noqa: E402
from fbcleanup.client import ApiError, FireblocksClient  # noqa: E402
from fbcleanup.config import (ConfigError, enforce_prod_gate, load_config, load_credentials, load_env_file,  # noqa: E402
                              scope_paths)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(ROOT / "config" / "cleanup.yaml"))
    ap.add_argument("--env-file", default=None, help="credentials file; its values override the environment")
    ap.add_argument("--asset", required=True, help="asset id to sweep, e.g. ETH")
    ap.add_argument("--amount", required=True, help="exact balance a vault must hold to be swept, e.g. 0.001")
    ap.add_argument("--dest-name", required=True, help="exact name of the destination vault account")
    ap.add_argument("--fee-level", default="MEDIUM", choices=["LOW", "MEDIUM", "HIGH"])
    ap.add_argument("--max-fee-pct", default="5", help="skip (and stop) if the fee exceeds this %% of the amount")
    ap.add_argument("--execute", action="store_true", help="actually send transfers (default: dry run)")
    ap.add_argument("--limit", type=int, default=None, help="with --execute: send at most N transfers")
    ap.add_argument("--all", action="store_true", help="with --execute: send every eligible vault")
    ap.add_argument("--i-understand-this-is-prod", action="store_true")
    args = ap.parse_args()
    try:
        amount, max_fee_pct = Decimal(args.amount), Decimal(args.max_fee_pct)
    except InvalidOperation:
        ap.error("--amount and --max-fee-pct must be numbers")
    if amount <= 0:
        ap.error("--amount must be positive")
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
        sources = sweep.select_sources(vaults, args.asset, amount, str(dest["id"]))
        print(f"destination: {dest['name']!r} (vault {dest['id']}) | {len(sources)} eligible vault(s) holding exactly "
              f"{amount} {args.asset} and nothing else")
        for v in sources:
            print(f"  vault {v['id']:>4}  {v['name']}")
        if args.execute and args.limit is not None:
            sources = sources[:args.limit]
            print(f"limited to the first {len(sources)}")
        if not sources:
            return 0

        def show(o: sweep.Outcome) -> None:
            fee = f" fee~{o.fee}" if o.fee is not None else ""
            print(f"  {o.status:14} vault {o.vault_id:>4} {o.name}{fee} {o.tx_id} {o.detail}".rstrip(), flush=True)

        outcomes = sweep.run_sweep(client, sources, str(dest["id"]), args.asset, amount, run_id, on_outcome=show,
                                   fee_level=args.fee_level, max_fee_pct=max_fee_pct)
        done = sum(o.status == "completed" for o in outcomes)
        bad = [o for o in outcomes if not o.ok]
        if not args.execute:
            print("dry run: nothing sent. Re-run with --execute --limit 1, then --execute --all.")
        else:
            print(f"{done} completed, {len(sources) - len(outcomes)} not attempted, {len(bad)} stopped the run")
        return 1 if bad else 0
    except (ApiError, sweep.SweepError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
