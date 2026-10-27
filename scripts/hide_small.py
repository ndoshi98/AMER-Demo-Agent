#!/usr/bin/env python3
"""Hide every vault whose CURRENT total USD value is at or under a cap. Moves no funds. Reversible.

DRY RUN by default.

  hide_small.py --dest-name "Destination Vault"                          plan only
  hide_small.py ... --execute --limit 1                              hide ONE vault (the test)
  hide_small.py ... --execute --all                                  hide everything in the plan

Value is testnet + mainnet, priced alike; empty vaults qualify. A vault holding an asset with no trustworthy USD price
is left alone, as are the destination vault, Canton/DTCC, protected and TAP-referenced vaults. Each vault is re-read live
right before it is hidden. Hiding also hides the vault's transaction history in the console. Undo with
run_cleanup.py --undo-run <run-id>. Production needs --i-understand-this-is-prod.
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
from fbcleanup.config import (ConfigError, enforce_prod_gate, load_config, load_credentials, load_env_file,  # noqa: E402
                              scope_paths)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(ROOT / "config" / "cleanup.yaml"))
    ap.add_argument("--env-file", default=None, help="credentials file; its values override the environment")
    ap.add_argument("--dest-name", required=True, help="exact name of the sweep destination vault (never hidden)")
    ap.add_argument("--max-usd", default="10", help="hide vaults whose total USD value is at or under this")
    ap.add_argument("--live-prices", action="store_true",
                    help="value vaults at live CoinGecko prices instead of last-transaction prices (see prices.py); "
                         "an asset with no market price still leaves its vault alone")
    ap.add_argument("--execute", action="store_true", help="actually hide (default: dry run)")
    ap.add_argument("--limit", type=int, default=None, help="with --execute: hide at most N vaults")
    ap.add_argument("--all", action="store_true", help="with --execute: hide everything in the plan")
    ap.add_argument("--i-understand-this-is-prod", action="store_true")
    args = ap.parse_args()
    try:
        max_usd = Decimal(args.max_usd)
    except InvalidOperation:
        ap.error("--max-usd must be a number")
    if max_usd < 0:
        ap.error("--max-usd must not be negative")
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
            raise ConfigError("hiding requires workspace.expected_name in the config")
    except ConfigError as exc:
        print(f"config/credentials problem: {exc}", file=sys.stderr)
        return 2

    run_id = "hide-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
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
        dest_id = str(sweep.find_destination(vaults, args.dest_name)["id"])
        others = [v for v in vaults if str(v["id"]) != dest_id]
        for i, v in enumerate(others):   # the paged listing can omit balances (deprecated assets): plan visible vaults from a live read
            if not v.get("hiddenOnUI"):
                live = client.call("get_vault", vault_account_id=str(v["id"]))
                if isinstance(live, dict) and live.get("assets") is not None:
                    others[i] = live
        chains = sv.load_blockchains(client)
        every = sorted({a["id"] for v in others for a in v.get("assets") or []})   # zero-balance wallets count for Canton
        held = sorted({a["id"] for v in others for a in v.get("assets") or [] if sweep._dec(a.get("total")) != 0})
        infos = {a: sv.asset_info(client, chains, a) for a in every}
        if args.live_prices:
            live = pr.LivePricer(overrides=cfg.price_overrides).price(pr.describe_assets(client, chains, held))
            prices = live.decimals(held)
            print(f"live prices: {len(live.quotes)}/{len(held)} held assets priced")
        else:
            prices = {a: sv.usd_price(client, a) for a in held if infos[a] is not None}
        try:
            policy_refs = collect.collect_vault_refs(client)
        except Exception as exc:  # noqa: BLE001
            print(f"REFUSING: cannot read the TAP policies to find vaults they reference ({exc})", file=sys.stderr)
            return 1
        holds = {str(v["id"]): r for v in others if (r := sv.hold_reason(v, infos, cfg.vault_protect, policy_refs))}

        pool = [v for v in others if str(v["id"]) not in holds and not v.get("hiddenOnUI")]
        plan, over, unvalued = [], [], []
        for v in pool:
            usd, why = sv.remaining_usd(v, infos, prices)
            if usd is None:
                unvalued.append((v, why))
            elif usd <= max_usd:
                plan.append((v, usd))
            else:
                over.append((v, usd))
        empty = [t for t in plan if not any(sweep._dec(a.get("total")) != 0 for a in t[0].get("assets") or [])]
        print(f"{len(others)} other vaults | {len(holds)} protected | {sum(1 for v in others if v.get('hiddenOnUI'))} "
              f"already hidden | cap ${max_usd}")
        print(f"\nWOULD HIDE {len(plan)} ({len(empty)} empty, {len(plan) - len(empty)} holding up to ${max_usd}):")
        for v, usd in plan:
            if usd or v not in [e[0] for e in empty]:
                print(f"  vault {v['id']:>4} {v['name']}  ${usd:,.2f}")
        print(f"\nLEFT VISIBLE: {len(over)} over the cap, {len(unvalued)} unvalued (no USD price):")
        for v, why in unvalued:
            print(f"  vault {v['id']:>4} {v['name']}: {why}")
        if not args.execute:
            print("\ndry run: nothing hidden. Re-run with --execute --limit 1, then --execute --all.")
            return 0

        def show(o: sweep.Outcome) -> None:
            print(f"  {o.status:10} vault {o.vault_id:>4} {o.name} {o.detail}".rstrip(), flush=True)

        print(f"\nHIDING (up to {args.limit or len(plan)}):", flush=True)
        h = sv.run_hide(client, [v for v, _ in plan], infos, prices, max_usd, limit=args.limit, on_outcome=show)
        print(f"{sum(o.status == 'hidden' for o in h)} hidden, {sum(o.status == 'skipped' for o in h)} skipped, "
              f"{sum(o.status == 'failed' for o in h)} failed | undo: run_cleanup.py --undo-run {run_id}")
        return 1 if any(o.status == "failed" for o in h) else 0
    except (ApiError, sweep.SweepError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
