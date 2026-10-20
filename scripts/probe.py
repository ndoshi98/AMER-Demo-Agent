#!/usr/bin/env python3
"""Verify the OPS table against the installed SDK, and (optionally) probe a real workspace READ-ONLY.

  probe.py --offline           No credentials, no network. Resolves every OPS entry on the installed SDK, checks the
                               kwargs we pass exist, and asserts no policy-write method is reachable. Safe anywhere.
  probe.py --online            Read-only calls against the workspace configured in config/cleanup.yaml:
                               key permission check, audit event-name census, response-shape spot checks.
                               Never calls a mutating op (the client is constructed dry_run=True regardless).
"""
from __future__ import annotations

import argparse
import inspect
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from fbcleanup import client as C  # noqa: E402
from fbcleanup.collect import audit_event_names, collect_audit  # noqa: E402
from fbcleanup.config import ConfigError, enforce_prod_gate, load_config, load_credentials, load_env_file  # noqa: E402

FORBIDDEN_FRAGMENTS = ("draft", "publish", "update_policy", "set_policy")


def offline() -> int:
    from fireblocks.base_path import BasePath
    from fireblocks.client import Fireblocks
    from fireblocks.client_configuration import ClientConfiguration

    sdk = Fireblocks(ClientConfiguration(api_key="offline", secret_key="offline", base_path=BasePath.US))
    cl = C.FireblocksClient(sdk)
    bad = 0
    print(f"{'op':28} {'sdk call':58} {'mut':3} status")
    for op, (group, cands) in C.OPS.items():
        try:
            fn = cl._resolve(op)
        except C.OpResolutionError as exc:
            print(f"{op:28} {'-':58} {'':3} UNRESOLVED\n    {exc}")
            bad += 1
            continue
        params = set(inspect.signature(fn).parameters)
        missing = [p for p in C.OP_PARAMS.get(op, ()) if p not in params]
        status = "ok" if not missing else f"MISSING PARAMS {missing}"
        if any(f in fn.__name__ for f in FORBIDDEN_FRAGMENTS):
            status, bad = "FORBIDDEN (policy write)", bad + 1
        bad += bool(missing)
        print(f"{op:28} {group + '.' + fn.__name__:58} {'M' if op in C.MUTATING_OPS else '':3} {status}")
    stray = C.MUTATING_OPS - set(C.OPS)
    if stray:
        print(f"MUTATING_OPS entries not in OPS: {sorted(stray)}")
        bad += 1
    print("\nRESULT:", "all OPS entries resolve on the installed SDK" if not bad else f"{bad} problem(s)")
    return 1 if bad else 0


def _try(label: str, fn):
    try:
        res = fn()
        print(f"  [ OK ] {label}")
        return res, None
    except Exception as exc:  # noqa: BLE001
        text = str(exc).splitlines()[0][:140]
        verdict = "FORBIDDEN (key lacks permission)" if (getattr(exc, "status", None) == 403 or "403" in text) else "ERROR"
        print(f"  [FAIL] {label}: {verdict}: {text}")
        return None, exc


def online(args) -> int:
    load_env_file(str(ROOT / "config" / ".env"))
    cfg = load_config(args.config)
    enforce_prod_gate(cfg, args.i_understand_this_is_prod)
    key, secret = load_credentials()
    cl = C.FireblocksClient.connect(key, secret, cfg.base_path, dry_run=True, run_id="probe",
                                    max_calls_per_second=cfg.max_calls_per_second,
                                    is_production=cfg.is_production, confirmed_prod=args.i_understand_this_is_prod)
    print(f"base_path={cfg.base_path} is_production={cfg.is_production} (read-only probe; mutating ops are suppressed)\n")

    ws, _ = _try("GET /workspace", lambda: cl.call("get_workspace"))
    if ws:
        print(f"      workspace name: {ws.get('name')!r}  (set workspace.expected_name to this to guard against wrong-workspace runs)")
    print("\nPermission check (Admin / Non-Signing Admin required for the management + policy reads):")
    results = {
        "vaults": _try("list vaults", lambda: cl.call("list_vaults", limit=5)),
        "transactions": _try("list transactions", lambda: cl.call("list_transactions", limit=5)),
        "users": _try("GET /users", lambda: cl.call("list_users")),
        "console_users": _try("GET /management/users", lambda: cl.call("list_console_users")),
        "audit": _try("GET /management/audit_logs", lambda: cl.call(
            "list_audit_logs", start_time=(int(time.time()) - 86400) * 1000, end_time=int(time.time()) * 1000, page_size=5, order="ASC")),
        "policy_v2": _try("GET /policy/active_policy (TRANSFER)", lambda: cl.call("get_policy_v2", policy_type="TRANSFER")),
        "policy_legacy": _try("GET /tap/active_policy", lambda: cl.call("get_policy_legacy")),
        "wallets": _try("list internal+external wallets", lambda: (cl.call("list_internal_wallets"), cl.call("list_external_wallets"))),
        "connections": _try("list network connections", lambda: cl.call("list_network_connections")),
        "tags": _try("list tags", lambda: cl.call("list_tags", page_size=5)),
    }
    must = ("console_users", "audit")
    blocked = [k for k in must if results[k][1] is not None]
    pol_ok = results["policy_v2"][1] is None or results["policy_legacy"][1] is None
    print("\nVERDICT:", "key can perform every read this tool needs." if not blocked and pol_ok else
          f"INSUFFICIENT: cannot read {blocked or ''}{'' if pol_ok else ' policy'}. Use an Admin or Non-Signing Admin API key.")
    print("Note: write permission (hide/delete) cannot be tested without mutating; the first --execute run will reveal it.")

    print("\nResponse-shape spot checks (keys only, no values):")
    v = results["vaults"][0]
    if v:
        accts = v.get("accounts") or []
        print(f"  vault page keys: {sorted(v)}; account keys: {sorted(accts[0]) if accts else '-'}")
        print(f"  hidden vaults visible in first page: {sum(1 for a in accts if a.get('hiddenOnUI'))} of {len(accts)} "
              "(if hidden vaults never appear, 'already hidden' detection and tag-usage counts have blind spots)")
    t = results["transactions"][0]
    if t:
        print(f"  tx keys (sample): {sorted(t[0])[:12] if t else '-'} createdAt sample: {t[0].get('createdAt') if t else '-'}")

    if results["audit"][1] is None:
        days = min(args.days, 365)
        print(f"\nAudit event census, last {days} day(s):")
        now = int(time.time())
        events, complete = collect_audit(cl, now - days * 86400, now)
        print(f"  {len(events)} event(s){'' if complete else ' (pagination incomplete)'}")
        if events:
            print(f"  normalized sample timestamp (epoch s): {events[0]['ts']}  <- confirm this is seconds & matches reality")
        print(f"  {'category':24} {'event':48} count")
        for (cat, ev), n in audit_event_names(events).most_common():
            print(f"  {cat:24} {ev:48} {n}")
        print(f"  distinct users in window: {len({e['user_id'] or e['user'] for e in events})}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--offline", action="store_true")
    mode.add_argument("--online", action="store_true")
    ap.add_argument("--config", default=str(ROOT / "config" / "cleanup.yaml"))
    ap.add_argument("--days", type=int, default=30, help="audit census window (max 365)")
    ap.add_argument("--i-understand-this-is-prod", action="store_true")
    args = ap.parse_args()
    try:
        return offline() if args.offline else online(args)
    except ConfigError as exc:
        print(f"config/credentials problem: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
