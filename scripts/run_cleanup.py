#!/usr/bin/env python3
"""Fireblocks demo-console cleanup agent — orchestrator.

DRY RUN by default: nothing in the workspace changes unless --execute is passed.

  run_cleanup.py                       audit: collect (read-only), classify, update quarantine, write report +
                                       approval file + proposed-policy JSON. Tier 1 hides only with --execute.
  run_cleanup.py --apply-approvals     execute Tier 2 items a human set to `approved: true` in an earlier run
                                       (re-verified against fresh data first). Needs --execute to act.
  run_cleanup.py --undo-run <run-id>   unhide every vault that run hid (per the action journal).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from fbcleanup import actions, collect, report, rules, tap  # noqa: E402
from fbcleanup.client import ApiError, FireblocksClient  # noqa: E402
from fbcleanup.config import ConfigError, enforce_prod_gate, load_config, load_credentials, load_env_file  # noqa: E402
from fbcleanup.state import State  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(ROOT / "config" / "cleanup.yaml"))
    ap.add_argument("--execute", action="store_true", help="actually mutate the workspace (default: dry run)")
    ap.add_argument("--apply-approvals", action="store_true")
    ap.add_argument("--undo-run", metavar="RUN_ID")
    ap.add_argument("--force-user-delete", action="store_true",
                    help="pass force=true to console-user deletion (overrides TAP-reference and quorum-integrity checks). "
                         "Only with --apply-approvals. Default off.")
    ap.add_argument("--no-observe", action="store_true", help="classify and report but do not advance quarantine clocks")
    ap.add_argument("--i-understand-this-is-prod", action="store_true")
    args = ap.parse_args()
    if args.force_user_delete and not args.apply_approvals:
        ap.error("--force-user-delete only applies with --apply-approvals")
    if args.apply_approvals and args.undo_run:
        ap.error("choose one of --apply-approvals / --undo-run")

    try:
        load_env_file(str(ROOT / "config" / ".env"))
        cfg = load_config(args.config)
        enforce_prod_gate(cfg, args.i_understand_this_is_prod)
        api_key, secret = load_credentials()
    except ConfigError as exc:
        print(f"config/credentials problem: {exc}", file=sys.stderr)
        return 2

    now = int(time.time())
    run_id = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    state_dir = ROOT / cfg.state_dir
    for attr in ("state_dir", "reports_dir", "approvals_dir"):
        setattr(cfg, attr, str(ROOT / getattr(cfg, attr)))
    client = FireblocksClient.connect(api_key, secret, cfg.base_path, dry_run=not args.execute, run_id=run_id,
                                      journal_path=str(state_dir / "journal.jsonl"),
                                      max_calls_per_second=cfg.max_calls_per_second,
                                      is_production=cfg.is_production, confirmed_prod=args.i_understand_this_is_prod)
    print(f"run {run_id} | {'EXECUTE' if args.execute else 'DRY RUN'} | base_path={cfg.base_path} "
          f"prod={cfg.is_production}")

    try:
        ws_name = (client.call("get_workspace") or {}).get("name", "")
        if cfg.expected_name and ws_name != cfg.expected_name:
            print(f"REFUSING: workspace is {ws_name!r} but workspace.expected_name is {cfg.expected_name!r}", file=sys.stderr)
            return 3

        if args.undo_run:
            for o in actions.undo_hides(client, str(state_dir / "journal.jsonl"), args.undo_run):
                print(f"  {o.status}: unhide vault {o.key[1]} {o.detail}")
            return 0

        state = State(str(state_dir / "state.db"))
        state.start_run(run_id, now, "apply" if args.apply_approvals else "audit", args.execute)

        inv = collect.collect_all(client, now, cfg)
        coverage = now
        if "audit" in inv.ok:
            coverage = state.update_audit_coverage(now, cfg.audit_lookback_days)
            state.ingest_audit(inv.audit_events, cfg.audit_activity_events)

        tap_rules, tap_findings, tap_path, refs = [], [], None, None
        if inv.policy is not None:
            tap_rules = tap.rules_from_policy(inv.policy)
            refs = set().union(*(r.refs for r in tap_rules)) if tap_rules else set()
            users = {str(u.get("id")): bool(u.get("enabled")) for u in inv.console_users} if "user" in inv.ok else None
            vaults = {str(v.get("id")) for v in inv.vaults} if "vault" in inv.ok else None
            tap_findings = tap.analyze(tap_rules, users, vaults)
            proposed = tap.propose(tap_rules, tap_findings)
            tap_path = Path(cfg.reports_dir) / f"tap-proposed-{run_id}.json"
            tap_path.parent.mkdir(parents=True, exist_ok=True)
            tap_path.write_text(json.dumps(proposed, indent=2), encoding="utf-8")

        decisions = rules.classify_all(inv, cfg, state.last_seen, coverage, refs)

        tier1, apply_out, proposal = [], [], None
        if args.apply_approvals:
            still = {d.key for d in decisions if d.flagged}
            apply_out = actions.apply_approvals(client, state, cfg, run_id, now, still, args.force_user_delete)
        else:
            if not args.no_observe:
                flagged = {d.key for d in decisions if d.flagged and d.tier in (1, 2)}
                state.observe(run_id, now, flagged, rules.evaluated_types(inv))
            tier1 = actions.execute_tier1(client, state, decisions, cfg, now)
            proposal = actions.propose_tier2(state, decisions, cfg, run_id, now)
        poll = actions.poll_user_deletions(client, cfg, now)

        text = report.build_report(run_id=run_id, now=now, mode="apply" if args.apply_approvals else "audit",
                                   executed=args.execute, cfg=cfg, workspace_name=ws_name, inv=inv, decisions=decisions,
                                   state=state, tier1=tier1, apply_outcomes=apply_out, poll_outcomes=poll,
                                   proposal_path=proposal, tap_rules=tap_rules, tap_findings=tap_findings,
                                   tap_proposal_path=tap_path)
        print(f"report: {report.write_report(text, cfg.reports_dir, run_id)}")
        if proposal:
            print(f"approval file: {proposal}")
        state.close()
    except ApiError as exc:
        print(f"API error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
