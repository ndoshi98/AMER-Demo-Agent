#!/usr/bin/env python3
"""Sweep non-frozen MAINNET balances out of low-value vaults into one destination vault, then hide them. MOVES FUNDS.

DRY RUN by default: prints the plan (transfers, mainnet balances that cannot move and why, vaults that would be hidden).

  sweep_mainnet.py --dest-name "Destination Vault"                         plan only
  sweep_mainnet.py ... --execute --limit 1                             send ONE transfer, hide at most ONE vault (the test)
  sweep_mainnet.py ... --execute --all                                 send everything, hide everything eligible

Only vaults worth at most --hide-max-usd BEFORE the sweep (testnet + mainnet) are touched; bigger or unvaluable vaults
are left alone. Testnet balances never move. After the sweep a vault is hidden if everything left in it (testnet + any mainnet that
could not move, all priced like mainnet) is worth at most --hide-max-usd. Empty vaults are hidden too. A vault with a
mainnet transfer still to come is never hidden. Protected vaults (x402, Canton/DTCC, config protect list, TAP-referenced)
are neither swept nor hidden. Hiding also hides the vault's transaction history; undo with run_cleanup.py --undo-run.
Production needs --i-understand-this-is-prod. Transfers are irreversible.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from fbcleanup import collect, sweep, sweep_mainnet as sm, sweep_value as sv  # noqa: E402
from fbcleanup.client import ApiError, FireblocksClient  # noqa: E402
from fbcleanup.config import (ConfigError, enforce_prod_gate, load_config, load_credentials, load_env_file,  # noqa: E402
                              scope_paths)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(ROOT / "config" / "cleanup.yaml"))
    ap.add_argument("--env-file", default=None, help="credentials file; its values override the environment")
    ap.add_argument("--dest-name", required=True, help="exact name of the destination vault account")
    ap.add_argument("--hide-max-usd", default="10",
                    help="only touch vaults worth at most this (before the sweep); hide them if still at most this after")
    ap.add_argument("--fee-level", default="MEDIUM", choices=["LOW", "MEDIUM", "HIGH"])
    ap.add_argument("--max-fee-pct", default="25", help="skip a native balance if the fee exceeds this %% of it")
    ap.add_argument("--poll-timeout", type=int, default=240, help="seconds to wait for each transfer to complete")
    ap.add_argument("--execute", action="store_true", help="actually send transfers and hide vaults (default: dry run)")
    ap.add_argument("--limit", type=int, default=None, help="with --execute: send at most N transfers, hide at most N vaults")
    ap.add_argument("--all", action="store_true", help="with --execute: send and hide everything in the plan")
    ap.add_argument("--i-understand-this-is-prod", action="store_true")
    args = ap.parse_args()
    try:
        hide_max, max_fee_pct = Decimal(args.hide_max_usd), Decimal(args.max_fee_pct)
    except InvalidOperation:
        ap.error("--hide-max-usd and --max-fee-pct must be numbers")
    if hide_max < 0:
        ap.error("--hide-max-usd must not be negative")
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
        plans = sm.plan_mainnet(vaults, dest_id, infos, prices, holds, hide_max)
        by_id = {str(p.vault["id"]): p for p in plans}
        live_sweep = [p for p in plans if not p.excluded and any(not i.skip for i in p.items)]
        transfers = sum(1 for p in live_sweep for i in p.items if not i.skip)

        print(f"destination: {dest['name']!r} (vault {dest_id}) | {len(others)} other vaults | "
              f"{len(holds)} protected | {len(live_sweep)} vault(s) with mainnet to sweep, {transfers} transfer(s)")
        print("prices used (last completed tx): " +
              (", ".join(f"{a}=${p:,.4g}" for a, p in sorted(prices.items()) if p is not None) or "none"))

        print("\nSWEEP (mainnet, not frozen):")
        total_usd = Decimal(0)
        for p in live_sweep:
            print(f"  vault {p.vault['id']:>4} {p.vault['name']}")
            for i in p.items:
                if not i.skip:
                    total_usd += i.usd
                    print(f"      {i.asset:34} {i.amount}  [${i.usd:,.2f}]" +
                          ("" if i.asset in dest_assets else "  (needs destination wallet)"))
        print(f"  total mainnet value moving: ${total_usd:,.2f}")

        stuck = sm.stuck_items(plans)
        print(f"\nMAINNET THAT CANNOT BE SWEPT ({len(stuck)}) - needs a human decision:")
        for v, i in stuck:
            print(f"  vault {v['id']:>4} {v['name']}: {i.asset} {i.amount} [${i.usd:,.2f}] - {i.skip}")
        notes = [(p.vault, n) for p in plans if not p.excluded for n in p.notes]
        if notes:
            print(f"\nNOTES ({len(notes)}):")
            for v, n in notes:
                print(f"  vault {v['id']:>4} {v['name']}: {n}")
        held_mainnet = [p for p in plans if str(p.vault['id']) in holds and any(
            (a.get('id') in infos and infos[a['id']] is not None and not infos[a['id']].testnet
             and sweep._dec(a.get('total')) != 0) for a in p.vault.get('assets') or [])]
        if held_mainnet:
            print(f"\nPROTECTED VAULTS HOLDING MAINNET (not touched) ({len(held_mainnet)}):")
            for p in held_mainnet:
                print(f"  vault {p.vault['id']:>4} {p.vault['name']}: {p.excluded}")

        # hide candidates: swept vaults first (so a --limit test exercises the post-sweep path), then everything else
        pending = {str(p.vault["id"]) for p in live_sweep}
        hide_pool = [v for v in others if str(v["id"]) not in holds and not v.get("hiddenOnUI")]
        would_hide, kept = [], []
        for v in hide_pool:
            vid = str(v["id"])
            pl = by_id.get(vid)
            usd, why = (pl.remaining, pl.remaining_why) if pl else sv.remaining_usd(v, infos, prices)
            (would_hide if usd is not None and usd <= hide_max else kept).append((v, usd, why))
        would_hide.sort(key=lambda t: (str(t[0]["id"]) not in pending, int(t[0]["id"]) if str(t[0]["id"]).isdigit() else 0))
        empty = [t for t in would_hide if not any(sweep._dec(a.get("total")) != 0 for a in t[0].get("assets") or [])]
        print(f"\nHIDE (remaining value <= ${hide_max}): {len(would_hide)} vault(s) "
              f"({len(empty)} already empty, {len(would_hide) - len(empty)} after sweep / testnet-only)")
        for v, usd, _ in would_hide:
            if v not in [e[0] for e in empty]:
                print(f"  vault {v['id']:>4} {v['name']}  ${usd:,.2f} stays inside")
        unvalued = [(v, why) for v, usd, why in kept if usd is None]
        print(f"KEPT VISIBLE: {len(kept) - len(unvalued)} over the cap; {len(unvalued)} could not be valued:")
        for v, why in unvalued:
            print(f"  vault {v['id']:>4} {v['name']}: {why}")
        print(f"  ({len(holds)} protected vaults are untouched)")

        if not args.execute:
            print("\ndry run: nothing sent, created or hidden. Review, then --execute --limit 1, then --execute --all.")
            return 0

        def show(o: sweep.Outcome) -> None:
            fee = f" fee~{o.fee}" if o.fee is not None else ""
            print(f"  {o.status:14} vault {o.vault_id:>4} {o.name}{fee} {o.tx_id} {o.detail}".rstrip(), flush=True)

        print("\nSENDING:")
        outcomes = sv.run_value_sweep(client, live_sweep, infos, dest_id, dest_assets, run_id, limit=args.limit,
                                      fee_level=args.fee_level, max_fee_pct=max_fee_pct, poll_timeout=args.poll_timeout,
                                      on_outcome=show)
        done = sum(o.status == "completed" for o in outcomes)
        skipped = [o for o in outcomes if o.status == "skipped"]
        bad = [o for o in outcomes if not o.ok and o.status != "skipped"]
        print(f"{done} completed, {len(skipped)} skipped, {len(bad)} stopped the run")
        if skipped:
            print("SKIPPED AT SEND TIME (needs a human decision):")
            for o in skipped:
                print(f"  vault {o.vault_id:>4} {o.name}: {o.detail}")
        if bad:
            print("NOT hiding anything: the sweep stopped early.")
            return 1

        attempts: dict = {}
        for o in outcomes:
            attempts[o.vault_id] = attempts.get(o.vault_id, 0) + 1
        # a vault with a transfer still to come (limit reached) must not be hidden yet
        todo = {str(p.vault["id"]) for p in live_sweep
                if attempts.get(str(p.vault["id"]), 0) < len(p.items)}
        candidates = [t[0] for t in would_hide if str(t[0]["id"]) not in todo]
        print(f"\nhiding up to {len(candidates)} vault(s)", flush=True)
        h = sv.run_hide(client, candidates, infos, prices, hide_max, limit=args.limit, on_outcome=show)
        print(f"{sum(o.status == 'hidden' for o in h)} hidden, {sum(o.status == 'skipped' for o in h)} skipped, "
              f"{sum(o.status == 'failed' for o in h)} failed")
        return 1 if any(o.status == "failed" for o in h) else 0
    except (ApiError, sweep.SweepError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
