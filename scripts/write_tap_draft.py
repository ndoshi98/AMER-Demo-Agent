#!/usr/bin/env python3
"""Write a proposed TAP rule list for ONE policy type (default TRANSFER) into the policy DRAFT. NEVER PUBLISHES.

A human reviews the draft in the console (Policies > Transfer) and approves/publishes it there. The published policy
is untouched until then.

DRY RUN by default: reads the active policy and current draft, runs the pre-check, prints what would be written.

  write_tap_draft.py --proposal reports/.../option_C_simplified_demo.json            check only
  write_tap_draft.py --proposal ... --execute                                        write the draft

Refuses unless the current draft is empty or identical to the active policy (writing replaces the whole draft, so
anything else may be a teammate's unpublished work). Production needs --i-understand-this-is-prod.
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from fbcleanup import tap_draft  # noqa: E402
from fbcleanup.client import DRY_RUN, ApiError, FireblocksClient  # noqa: E402
from fbcleanup.config import (ConfigError, enforce_prod_gate, load_config, load_credentials, load_env_file,  # noqa: E402
                              scope_paths)

DEFAULT_PROPOSAL = ROOT / "reports" / "fireblocks-amer-demo" / "tap_proposal" / "option_C_simplified_demo.json"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(ROOT / "config" / "cleanup.yaml"))
    ap.add_argument("--env-file", default=None, help="credentials file; its values override the environment")
    ap.add_argument("--proposal", default=str(DEFAULT_PROPOSAL), help="JSON file: a rule list, or {'rules': [...]}")
    ap.add_argument("--policy-type", default="TRANSFER", help="policy type of the proposal (TRANSFER, APPROVE, ...)")
    ap.add_argument("--execute", action="store_true", help="write the draft (default: dry run)")
    ap.add_argument("--i-understand-this-is-prod", action="store_true")
    args = ap.parse_args()

    try:
        raw = json.loads(Path(args.proposal).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"cannot read proposal: {exc}", file=sys.stderr)
        return 2
    rules = tap_draft.strip_annotations(raw.get("rules") if isinstance(raw, dict) else raw)
    bad = tap_draft.problems_in_proposal(rules, args.policy_type)
    if bad:
        print("proposal is malformed:\n  " + "\n  ".join(bad), file=sys.stderr)
        return 2

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
            raise ConfigError("writing a draft requires workspace.expected_name in the config")
    except ConfigError as exc:
        print(f"config/credentials problem: {exc}", file=sys.stderr)
        return 2

    run_id = "tapdraft-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    client = FireblocksClient.connect(api_key, secret, cfg.base_path, dry_run=not args.execute, run_id=run_id,
                                      journal_path=str(state_dir / "journal.jsonl"),
                                      max_calls_per_second=cfg.max_calls_per_second,
                                      is_production=cfg.is_production, confirmed_prod=args.i_understand_this_is_prod)
    print(f"{run_id} | {'EXECUTE' if args.execute else 'DRY RUN'} | prod={cfg.is_production} | "
          f"workspace={cfg.expected_name!r} | {len(rules)} rules from {Path(args.proposal).name}")
    try:
        ws_name = (client.call("get_workspace") or {}).get("name", "")
        if ws_name != cfg.expected_name:
            print(f"REFUSING: workspace is {ws_name!r} but workspace.expected_name is {cfg.expected_name!r}", file=sys.stderr)
            return 3
        active = client.call("get_policy_v2", policy_type=args.policy_type)
        draft = client.call("get_policy_draft", policy_type=args.policy_type)
        ok, why = tap_draft.precheck(draft, active, rules)
        print(f"pre-check: {'OK' if ok else 'REFUSED'} - {why}")
        if not ok:
            return 1
        for i, r in enumerate(rules, 1):
            print(f"  {i:>2}. {r['name']}  [{(r.get('verdict') or {}).get('action')}]")

        result = client.call("update_policy_draft",
                             update_draft_request={"policyTypes": [args.policy_type], "rules": rules},
                             idempotency_key=str(uuid.uuid4()))
        if result is DRY_RUN:
            print("dry run: nothing written. Re-run with --execute to write the DRAFT (it is never published).")
            return 0
        back = client.call("get_policy_draft", policy_type=args.policy_type)
        probs = tap_draft.readback_problems(rules, back)
        validation = (result or {}).get("validation") if isinstance(result, dict) else None
        print("server validation:", json.dumps(validation, default=str)[:1500] if validation else "none returned")
        if probs:
            print("READ-BACK MISMATCH:\n  " + "\n  ".join(probs), file=sys.stderr)
            return 1
        print(f"draft written and read back: {len(rules)} rules. NOT published. "
              "Review in the console (Policies > " + args.policy_type.title() + ") and publish there.")
        return 0
    except ApiError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
