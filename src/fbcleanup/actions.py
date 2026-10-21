"""The ONLY module that mutates the workspace (through client.call) and the approvals directory.

Tier 1: hide/unhide vault — automatic once past quarantine, reversible from the console.
Tier 2: approval gate. propose_tier2() writes YAML; a LATER run with --apply-approvals executes items a human
        flipped to `approved: true`. apply_approvals() refuses anything proposed in the current run.
Nothing here can publish policy: there is no such op in client.OPS.
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from . import rules as R
from .client import DRY_RUN, ApiError
from .config import Config
from .state import DAY, State

# action -> (client op, kwarg name carrying the entity id)
TIER2_OPS = {
    R.DELETE_INTERNAL_WALLET: ("delete_internal_wallet", "wallet_id"),
    R.DELETE_EXTERNAL_WALLET: ("delete_external_wallet", "wallet_id"),
    R.DELETE_NETWORK_CONNECTION: ("delete_network_connection", "connection_id"),
    R.DELETE_TAG: ("delete_tag", "tag_id"),
    R.REQUEST_USER_DELETION: ("delete_console_user", "id"),
}
_HEADER = ("Review each item. To approve, change `approved: false` to `approved: true` for THAT item only, then run "
           "`scripts/run_cleanup.py --apply-approvals` in a LATER run. Items proposed in a run are never executed in "
           "that same run. Delete anything you want to keep, or leave it unapproved.")


@dataclass
class Outcome:
    key: tuple
    name: str
    status: str   # hidden | would_hide | executed | requested | would_execute | skipped | failed | expired
    detail: str = ""


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(s: str) -> int:
    return int(datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp())


# -- Tier 1 -------------------------------------------------------------------------------------
def execute_tier1(client, state: State, decisions: List[R.Decision], cfg: Config, now: int) -> List[Outcome]:
    outcomes: List[Outcome] = []
    for d in decisions:
        if d.action != R.HIDE_VAULT or not d.flagged:
            continue
        if not state.eligible(d.key, now, cfg.min_consecutive_runs, cfg.min_days_flagged):
            continue
        try:
            fresh = client.call("get_vault", vault_account_id=d.entity_id)  # re-verify right before acting
            zero, why = R.vault_balances_zero(fresh)
            if fresh.get("hiddenOnUI"):
                outcomes.append(Outcome(d.key, d.name, "skipped", "already hidden"))
                continue
            if not zero:
                outcomes.append(Outcome(d.key, d.name, "skipped", f"balance changed since classification ({why})"))
                continue
            res = client.call("hide_vault", vault_account_id=d.entity_id)
        except ApiError as exc:
            outcomes.append(Outcome(d.key, d.name, "failed", str(exc)))
            continue
        if res is DRY_RUN:
            outcomes.append(Outcome(d.key, d.name, "would_hide", "dry-run: nothing changed"))
        else:
            state.mark_acted(d.key, now)
            outcomes.append(Outcome(d.key, d.name, "hidden", "hidden (also hides its tx history in the console)"))
    return outcomes


def _vault_name(client, vid: str) -> str:
    try:
        return (client.call("get_vault", vault_account_id=vid) or {}).get("name") or vid
    except Exception:  # noqa: BLE001 - a name is cosmetic; never block an unhide on it
        return vid


def undo_hides(client, journal_path: str, run_id: str, state=None) -> List[Outcome]:
    """Unhide every vault this run hid, per the action journal. Released from the ledger so quarantine restarts."""
    outcomes: List[Outcome] = []
    p = Path(journal_path)
    if not p.exists():
        return outcomes
    for line in p.read_text(encoding="utf-8").splitlines():
        e = json.loads(line)
        if e["run_id"] == run_id and e["op"] == "hide_vault" and e["outcome"] == "ok":
            vid = e["kwargs"]["vault_account_id"]
            name = _vault_name(client, vid)
            try:
                res = client.call("unhide_vault", vault_account_id=vid)
                outcomes.append(Outcome(("vault", vid), name, "would_execute" if res is DRY_RUN else "executed", "undone"))
                if state is not None and res is not DRY_RUN:
                    state.release(("vault", str(vid)))
            except ApiError as exc:
                outcomes.append(Outcome(("vault", vid), name, "failed", str(exc)))
    return outcomes


# -- Tier 2: approval files ---------------------------------------------------------------------
def _write_yaml_atomic(path: Path, doc: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        yaml.safe_dump(doc, fh, sort_keys=False, default_flow_style=False)
    os.replace(tmp, path)


def _approval_files(approvals_dir: str) -> List[Path]:
    d = Path(approvals_dir)
    return sorted(d.glob("approvals-*.yaml")) if d.exists() else []


def _pending_ids(approvals_dir: str) -> set:
    ids = set()
    for f in _approval_files(approvals_dir):
        for it in (yaml.safe_load(f.read_text(encoding="utf-8")) or {}).get("items", []):
            if it.get("status") in ("pending", "requested"):
                ids.add(it["id"])
    return ids


def propose_tier2(state: State, decisions: List[R.Decision], cfg: Config, run_id: str, now: int) -> Optional[Path]:
    already = _pending_ids(cfg.approvals_dir)
    items = []
    for d in decisions:
        if d.tier != 2 or not d.flagged:
            continue
        ident = f"{d.entity_type}:{d.entity_id}"
        if ident in already or not state.eligible(d.key, now, cfg.min_consecutive_runs, cfg.min_days_flagged):
            continue
        prog = state.progress(d.key) or {}
        items.append({"id": ident, "entity_type": d.entity_type, "entity_id": d.entity_id, "name": d.name,
                      "action": d.action, "reasons": d.reasons,
                      "quarantine": {"runs_flagged": prog.get("runs"),
                                     "first_flagged": _iso(prog["first_flagged_at"]) if prog else None},
                      "approved": False, "status": "pending"})
    if not items:
        return None
    path = Path(cfg.approvals_dir) / f"approvals-{run_id}.yaml"
    _write_yaml_atomic(path, {"run_id": run_id, "created_at": _iso(now), "instructions": _HEADER, "items": items})
    return path


def apply_approvals(client, state: State, cfg: Config, run_id: str, now: int, still_flagged: set,
                    force_user_delete: bool = False) -> List[Outcome]:
    """`still_flagged`: keys flagged by a FRESH classification made at the start of this apply run.
    Fail-closed: an approved item whose entity is not in that set (used since, collection failed, ...) is skipped."""
    outcomes: List[Outcome] = []
    for f in _approval_files(cfg.approvals_dir):
        doc = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        changed = False
        for it in doc.get("items", []):
            if it.get("status") != "pending" or it.get("approved") is not True:
                continue
            key = (it["entity_type"], str(it["entity_id"]))
            name = it.get("name", "")
            if doc.get("run_id") == run_id:
                outcomes.append(Outcome(key, name, "skipped", "proposed in this same run — refusing"))
                continue
            if now - _parse_iso(doc["created_at"]) > cfg.approval_max_age_days * DAY:
                it["status"], it["detail"] = "expired", f"older than {cfg.approval_max_age_days}d; re-run to re-propose"
                outcomes.append(Outcome(key, name, "expired", it["detail"]))
                changed = True
                continue
            if it.get("action") not in TIER2_OPS:
                outcomes.append(Outcome(key, name, "skipped", f"unknown action {it.get('action')!r}"))
                continue
            if key not in still_flagged:
                outcomes.append(Outcome(key, name, "skipped",
                                        "not flagged by a fresh classification just now (activity since, or data unavailable); "
                                        "left pending"))
                continue
            if not state.eligible(key, now, cfg.min_consecutive_runs, cfg.min_days_flagged):
                it["status"], it["detail"] = "skipped", "no longer flagged/eligible at apply time (released or acted)"
                outcomes.append(Outcome(key, name, "skipped", it["detail"]))
                changed = True
                continue
            op, idkw = TIER2_OPS[it["action"]]
            kwargs: Dict[str, Any] = {idkw: it["entity_id"]}
            if op == "delete_console_user" and force_user_delete:
                kwargs["force"] = True
            try:
                res = client.call(op, **kwargs)
            except ApiError as exc:
                it["status"], it["detail"] = "failed", str(exc)
                outcomes.append(Outcome(key, name, "failed", str(exc)))
                changed = True
                continue
            if res is DRY_RUN:
                outcomes.append(Outcome(key, name, "would_execute", "dry-run: nothing changed"))
                continue
            state.mark_acted(key, now)
            it["status"] = "requested" if op == "delete_console_user" else "executed"
            it["detail"] = ("deletion requested; completes only when a later poll shows enabled=false"
                            if it["status"] == "requested" else "deleted")
            it["executed_at"], it["executed_in_run"] = _iso(now), run_id
            outcomes.append(Outcome(key, name, it["status"], it["detail"]))
            changed = True
        if changed:
            _write_yaml_atomic(f, doc)
    return outcomes


def poll_user_deletions(client, cfg: Config, now: int) -> List[Outcome]:
    """Close out `requested` user deletions: complete only when the user now reads enabled=false."""
    outcomes: List[Outcome] = []
    pending = []
    for f in _approval_files(cfg.approvals_dir):
        doc = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        pending.append((f, doc, [i for i in doc.get("items", []) if i.get("status") == "requested"]))
    if not any(p[2] for p in pending):
        return outcomes
    users = {str(u.get("id")): u for u in (client.call("list_console_users") or {}).get("users", [])}
    for f, doc, items in pending:
        changed = False
        for it in items:
            u = users.get(str(it["entity_id"]))
            key = ("user", str(it["entity_id"]))
            if u is not None and u.get("enabled") is False:
                it["status"], it["detail"], it["completed_at"] = "executed", "poll shows enabled=false", _iso(now)
                outcomes.append(Outcome(key, it.get("name", ""), "executed", it["detail"]))
                changed = True
            else:
                outcomes.append(Outcome(key, it.get("name", ""), "requested",
                                        "still enabled — awaiting quorum approval in the console"))
        if changed:
            _write_yaml_atomic(f, doc)
    return outcomes
