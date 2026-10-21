"""Markdown report writer. Order: decisions needed -> automatic changes -> policy -> quarantine -> inventory."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import tap as T
from . import tap_consolidate as TC
from .actions import Outcome
from .collect import Inventory
from .config import Config
from .rules import Decision
from .state import DAY, State


def _d(ts: Optional[int]) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d") if ts else "-"


def _row(*cells: Any) -> str:
    return "| " + " | ".join(str(c).replace("|", "\\|").replace("\n", " ") for c in cells) + " |"


def _findings_block(title: str, findings: List[T.Finding], rules: List[T.Rule]) -> List[str]:
    out = [f"#### {title}", ""]
    if not findings:
        return out + ["_None._", ""]
    for f in findings:
        out.append(f"- **{f.kind}** — {f.summary}")
    return out + [""]


def _consolidation_block(plans: Dict[str, TC.Plan]) -> List[str]:
    strict, deny = plans["strict"], plans["default_deny"]
    out = ["#### C. Consolidation (verified against every case the policy can distinguish)", "",
           f"{strict.original} rule(s) today. **{len(strict.rules)}** after behaviour-identical consolidation; "
           f"**{len(deny.rules)}** if transactions matching no rule are blocked (assumption, see below). "
           "The full proposed rule lists are in the `consolidation` section of the proposal JSON.", ""]
    for title, plan in (("Behaviour-identical (always safe)", strict),
                        ("Additional only if a transaction matching no rule is blocked", deny)):
        steps = plan.steps if plan is strict else [s for s in deny.steps if s.detail not in {x.detail for x in strict.steps}]
        out += [f"**{title}:**", ""]
        out += [f"- {s.kind}: {s.detail}" for s in steps] or ["- none"]
        out += [""]
    if strict.note:
        out += [f"> {strict.note}", ""]
    if strict.gaps:
        out += [f"**Coverage gaps** — {strict.gap_total} case(s) match NO rule (assumes amount bounds are inclusive):", ""]
        out += [f"- {g}" for g in strict.gaps] + [""]
    if strict.opaque:
        out += ["Opaque rules were left in place and nothing was moved across them.", ""]
    out += ["> The default-deny column assumes the workspace blocks transactions that match no rule. This has not been "
            "verified from the API; confirm it in the console before relying on it.", ""]
    return out


def build_report(*, run_id: str, now: int, mode: str, executed: bool, cfg: Config, workspace_name: str,
                 inv: Inventory, decisions: List[Decision], state: State, tier1: List[Outcome],
                 apply_outcomes: List[Outcome], poll_outcomes: List[Outcome], proposal_path: Optional[Path],
                 tap_rules: List[T.Rule], tap_findings: List[T.Finding], tap_proposal_path: Optional[Path],
                 observed: bool = True, consolidation: Optional[Dict[str, TC.Plan]] = None) -> str:
    L: List[str] = []
    env = "PRODUCTION" if cfg.is_production else "non-production (declared)"
    L += [f"# Cleanup report `{run_id}`", "",
          f"- Generated {_d(now)} · mode `{mode}` · **{'EXECUTED' if executed else 'DRY RUN — nothing was changed'}**",
          f"- Workspace: `{workspace_name or 'unknown'}` · base path `{cfg.base_path}` · {env}", ""]

    # ---- 1. needs your decision
    L += ["## 1. Needs your decision", ""]
    tier2 = [d for d in decisions if d.tier == 2 and d.flagged]
    eligible2 = [d for d in tier2 if state.eligible(d.key, now, cfg.min_consecutive_runs, cfg.min_days_flagged)]
    if proposal_path:
        L += [f"**Approval file written:** `{proposal_path}` — {len(eligible2)} item(s) past quarantine. "
              "Flip `approved: true` per item, then run with `--apply-approvals` in a later run.", ""]
        L += [_row("Type", "Name", "Why"), _row("---", "---", "---")]
        L += [_row(d.entity_type, d.name or d.entity_id, "; ".join(d.reasons)) for d in eligible2]
        L += [""]
    else:
        L += ["No Tier 2 items are past quarantine (or all are already in a pending approval file).", ""]
    manual = [d for d in decisions if d.manual]
    if manual:
        L += ["**Manual console actions (this tool cannot do these):**", ""]
        L += list(dict.fromkeys(f"- {d.name or d.entity_id}: {d.manual}" for d in manual))
        L += [""]
    if tap_findings:
        judg = [f for f in tap_findings if not (f.safe)]
        L += [f"**Policy:** {len(judg)} judgment call(s) need a human — see section 3.", ""]
    if inv.errors:
        L += ["**Data gaps this run** (affected entity types were NOT judged, and their quarantine clocks were "
              "neither advanced nor reset):", ""]
        L += [f"- `{k}`: {v}" for k, v in sorted(inv.errors.items())]
        L += [""]
    if apply_outcomes:
        L += ["**Approval results:**", ""] + [f"- {o.status.upper()} {o.key[0]} {o.name or o.key[1]} — {o.detail}"
                                              for o in apply_outcomes] + [""]
    if poll_outcomes:
        L += ["**User-deletion polling:**", ""] + [f"- {o.status.upper()} {o.name or o.key[1]} — {o.detail}"
                                                    for o in poll_outcomes] + [""]

    # ---- 2. changed automatically
    L += ["## 2. Changed automatically (Tier 1)", ""]
    if tier1:
        L += ["> Hiding a vault also hides its transaction history in the console. Vaults cannot be deleted; "
              "every hide is reversible with `--undo-run <run-id>` or Unhide in the console.", ""]
        L += [_row("Vault", "Result", "Detail"), _row("---", "---", "---")]
        L += [_row(o.name or o.key[1], o.status, o.detail) for o in tier1]
    else:
        L += ["Nothing was eligible."]
    L += [""]

    # ---- 3. policy
    L += ["## 3. Policy (TAP) findings — propose only, never executed", ""]
    if not tap_rules:
        L += ["_Policy not analysed (unavailable this run)._", ""]
    else:
        opaque = [r for r in tap_rules if r.opaque]
        L += [f"{len(tap_rules)} rule(s) parsed, {len(opaque)} opaque (shape not modelled; never touched).", ""]
        if opaque:
            L += [f"- opaque rule #{r.index} `{r.name}`: {r.opaque_reason}" for r in opaque] + [""]
        safe = [f for f in tap_findings if f.safe]
        L += _findings_block("A. Behaviour-preserving (safe to remove by hand)", safe, tap_rules)
        L += _findings_block("B. Judgment calls (never automated)", [f for f in tap_findings if not f.safe], tap_rules)
        if tap_proposal_path:
            L += [f"Proposed rule set (safe removals only): `{tap_proposal_path}`. A human publishes — this tool never does.", ""]
        if consolidation:
            L += _consolidation_block(consolidation)

    # ---- 4. quarantine
    if observed:
        L += ["## 4. Still in quarantine", ""]
    else:
        L += ["## 4. Currently flagged (NOT observed this run)", "",
              "`--no-observe`: quarantine clocks were not advanced. These are flagged by today's data; "
              "Runs/First flagged reflect earlier observing runs only.", ""]
    pend = [d for d in decisions if d.flagged and d.tier in (1, 2)
            and not state.eligible(d.key, now, cfg.min_consecutive_runs, cfg.min_days_flagged)]
    if pend:
        L += [_row("Type", "Name", "Runs", "First flagged", "Days left"), _row("---", "---", "---", "---", "---")]
        for d in pend:
            p = state.progress(d.key) or {}
            if p.get("status") == "acted":
                continue
            left = max(0, cfg.min_days_flagged - (now - p.get("first_flagged_at", now)) // DAY)
            L += [_row(d.entity_type, d.name or d.entity_id, f"{p.get('runs', 0)}/{cfg.min_consecutive_runs}",
                       _d(p.get("first_flagged_at")), left)]
    else:
        L += ["Nothing in quarantine."]
    L += [""]

    # ---- 5. inventory
    L += ["## 5. Inventory", ""]
    c = Counter((d.entity_type, d.action) for d in decisions if d.action != "suggest_rename")
    L += [_row("Type", "Total", "Flagged", "Kept"), _row("---", "---", "---", "---")]
    for etype in ("vault", "internal_wallet", "external_wallet", "network_connection", "tag", "user"):
        tot = sum(v for (t, _), v in c.items() if t == etype)
        fl = sum(1 for d in decisions if d.entity_type == etype and d.flagged and d.action != "suggest_rename")
        L += [_row(etype, tot if tot or etype in {t for t, _ in c} else "not judged", fl, tot - fl)]
    L += [""]
    renames = [d for d in decisions if d.action == "suggest_rename"]
    if renames:
        L += ["**Rename suggestions (Tier 3, never applied):**", ""] + \
             [f"- “{d.name}” (vault id {d.entity_id}, shown only because the names are identical): {d.manual}"
              for d in renames] + [""]
    near = [d for d in decisions if d.action == "hide_vault" and not d.flagged and d.keep_reasons
            and not any(k.startswith(("already hidden", "non-zero")) for k in d.keep_reasons)]
    if near:
        L += ["**Zero-balance vaults kept on purpose:**", ""] + \
             [f"- {d.name or d.entity_id}: {'; '.join(d.keep_reasons)}" for d in near] + [""]
    return "\n".join(L) + "\n"


def write_report(text: str, reports_dir: str, run_id: str) -> Path:
    p = Path(reports_dir) / f"cleanup-{run_id}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p
