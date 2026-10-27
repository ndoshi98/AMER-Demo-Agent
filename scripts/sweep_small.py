#!/usr/bin/env python3
"""Sweep EVERY asset out of every low-value vault into one destination vault. MOVES FUNDS.

DRY RUN by default: prints the plan (what would move, what is skipped and why); sends and creates nothing.

  sweep_small.py --max-usd 10 --dest-name "Treasury Demo"                  plan only
  sweep_small.py ... --execute --limit 1                                   send ONE transfer (the test)
  sweep_small.py ... --execute --all                                       send everything in the plan

A vault qualifies when it holds something and its USD total is <= --max-usd. Testnet assets are priced like mainnet
ones (the console reports USD on testnet transactions); an asset with no trustworthy USD price makes its vault unvaluable and it
is left alone. Missing destination wallets are created.
Per-asset problems are skipped and reported; the run stops on a failed, unconfirmed or approval-pending transfer.
Production needs --i-understand-this-is-prod. Transfers are irreversible.
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

from fbcleanup import collect, rules, sweep, sweep_value as sv  # noqa: E402
from fbcleanup.client import ApiError, FireblocksClient  # noqa: E402
from fbcleanup.config import (ConfigError, enforce_prod_gate, load_config, load_credentials, load_env_file,  # noqa: E402
                              scope_paths)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(ROOT / "config" / "cleanup.yaml"))
    ap.add_argument("--env-file", default=None, help="credentials file; its values override the environment")
    ap.add_argument("--max-usd", default="10", help="sweep vaults whose total USD value is at or under this")
    ap.add_argument("--dest-name", required=True, help="exact name of the destination vault account")
    ap.add_argument("--fee-level", default="MEDIUM", choices=["LOW", "MEDIUM", "HIGH"])
    ap.add_argument("--max-fee-pct", default="25", help="skip a native balance if the fee exceeds this %% of it")
    ap.add_argument("--poll-timeout", type=int, default=240, help="seconds to wait for each transfer to complete")
    ap.add_argument("--execute", action="store_true", help="actually send transfers (default: dry run)")
    ap.add_argument("--limit", type=int, default=None, help="with --execute: send at most N transfers")
    ap.add_argument("--all", action="store_true", help="with --execute: send everything in the plan")
    ap.add_argument("--hide", action="store_true",
                    help="after the sweep, hide (archive) each swept vault that is still worth at most --max-usd; "
                         "reversible with run_cleanup.py --undo-run <run-id>")
    ap.add_argument("--include-empty", action="store_true", help="with --hide: also hide vaults that are already empty")
    ap.add_argument("--i-understand-this-is-prod", action="store_true")
    args = ap.parse_args()
    try:
        max_usd, max_fee_pct = Decimal(args.max_usd), Decimal(args.max_fee_pct)
    except InvalidOperation:
        ap.error("--max-usd and --max-fee-pct must be numbers")
    if max_usd < 0:
        ap.error("--max-usd must not be negative")
    if args.execute and (args.all == (args.limit is not None)):
        ap.error("--execute needs exactly one of --limit N or --all")
    if args.limit is not None and args.limit < 1:
        ap.error("--limit must be at least 1")
    if args.include_empty and not args.hide:
        ap.error("--include-empty needs --hide")

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
        others = [v for v in vaults if str(v["id"]) != dest_id]
        every = sorted({a["id"] for v in others for a in v.get("assets") or []})   # zero-balance wallets count for Canton
        held = sorted({a["id"] for v in others for a in v.get("assets") or [] if sweep._dec(a.get("total")) != 0})
        infos = {a: sv.asset_info(client, chains, a) for a in every}
        prices = {a: sv.usd_price(client, a) for a in held if infos[a] is not None}

        try:
            policy_refs = collect.collect_vault_refs(client)
        except Exception as exc:  # noqa: BLE001
            print(f"REFUSING: cannot read the TAP policies to find vaults they reference ({exc})", file=sys.stderr)
            return 1
        holds = {str(v["id"]): r for v in others if (r := sv.hold_reason(v, infos, cfg.vault_protect, policy_refs))}
        plans = sv.plan_vaults(vaults, dest_id, infos, prices, max_usd, holds=holds)
        empties = [v for v in others if rules.vault_balances_zero(v)[0] and not v.get("hiddenOnUI")
                   and str(v["id"]) not in holds] if args.include_empty else []

        eligible = [p for p in plans if not p.excluded]
        print(f"destination: {dest['name']!r} (vault {dest_id}) | cap ${max_usd} | {len(vaults) - 1} other vaults, "
              f"{len(plans)} hold a balance, {len(eligible)} eligible")
        print("prices used (last completed tx): " +
              (", ".join(f"{a}=${p:,.4g}" for a, p in sorted(prices.items()) if p is not None) or "none"))
        for p in eligible:
            print(f"  vault {p.vault['id']:>4} {p.vault['name']}  (${p.usd:,.2f})")
            for i in p.items:
                print(f"      {i.asset:34} {i.amount}" + (f"  [${i.usd:,.2f}]" if i.usd else "") +
                      (f"  SKIP: {i.skip}" if i.skip else "") +
                      ("" if i.asset in dest_assets or i.skip else "  (needs destination wallet)"))
        left = [p for p in plans if p.excluded]
        print(f"left alone ({len(left)}):")
        for p in left:
            print(f"  vault {p.vault['id']:>4} {p.vault['name']}: {p.excluded}")

        held_back_empty = [v for v in others if rules.vault_balances_zero(v)[0] and str(v["id"]) in holds]
        if args.hide:
            print(f"\nHIDE: {len(eligible)} swept vault(s) after their sweep, {len(empties)} already-empty vault(s)"
                  f" ({len(held_back_empty)} empty vault(s) held back: Canton/protected/TAP)")
            for p in eligible:
                print(f"  after sweep  vault {p.vault['id']:>4} {p.vault['name']}")
            for v in empties:
                print(f"  empty        vault {v['id']:>4} {v['name']}")
            for v in held_back_empty:
                print(f"  HELD BACK    vault {v['id']:>4} {v['name']}: {holds[str(v['id'])]}")

        todo = sum(1 for p in eligible for i in p.items if not i.skip)
        if not todo and not args.hide:
            print("nothing to send")
            return 0
        print(f"{todo} transfer(s) planned")

        def show(o: sweep.Outcome) -> None:
            fee = f" fee~{o.fee}" if o.fee is not None else ""
            print(f"  {o.status:14} vault {o.vault_id:>4} {o.name}{fee} {o.tx_id} {o.detail}".rstrip(), flush=True)

        outcomes = sv.run_value_sweep(client, plans, infos, dest_id, dest_assets, run_id, limit=args.limit,
                                      fee_level=args.fee_level, max_fee_pct=max_fee_pct, poll_timeout=args.poll_timeout,
                                      on_outcome=show)
        done = sum(o.status == "completed" for o in outcomes)
        skipped = sum(o.status == "skipped" for o in outcomes)
        bad = [o for o in outcomes if not o.ok and o.status != "skipped"]
        if not args.execute:
            print("dry run: nothing sent or created. Re-run with --execute --limit 1, then --execute --all.")
        else:
            print(f"{done} completed, {skipped} skipped, {len(bad)} stopped the run")
        if bad:
            if args.hide:
                print("NOT hiding anything: the sweep stopped early.")
            return 1
        if args.hide:
            attempts = {}
            for o in outcomes:
                attempts[o.vault_id] = attempts.get(o.vault_id, 0) + 1
            finished = [p.vault for p in eligible if attempts.get(str(p.vault["id"]), 0) >= len(p.items)]
            print(f"\nhiding {len(finished)} swept + {len(empties)} empty vault(s)", flush=True)
            h = sv.run_hide(client, finished + empties, infos, prices, max_usd, limit=args.limit, on_outcome=show)
            print(f"{sum(o.status == 'hidden' for o in h)} hidden, {sum(o.status == 'skipped' for o in h)} skipped, "
                  f"{sum(o.status == 'failed' for o in h)} failed")
            if any(o.status == "failed" for o in h):
                return 1
        return 0
    except (ApiError, sweep.SweepError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
