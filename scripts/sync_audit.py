#!/usr/bin/env python3
"""Sync run output with the private audit repo so approvals are reviewed in a PR. No Fireblocks credentials needed.

DRY RUN by default: shows what would happen and changes nothing. Add --execute to act.

  sync_audit.py                push: put new reports, journals and approval files on a branch of the audit repo and
                               open a PR. Never writes to main. New approval files must be all `approved: false`;
                               a reviewed approval file is never overwritten with different decisions.
  sync_audit.py --pull         pull: after the PR is merged, copy reviewer decisions (`approved: true` on origin/main)
                               into the local approval files. Never un-approves, never edits anything else.

`run_cleanup.py --apply-approvals` independently re-checks every item against origin/main, so a hand-edited local
file cannot execute.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from fbcleanup import actions, audit_repo  # noqa: E402
from fbcleanup.config import ConfigError, load_config, scope_paths, workspace_slug  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(ROOT / "config" / "cleanup.yaml"))
    ap.add_argument("--repo", default=None, help="local clone of the audit repo (default: audit_repo.path in the config)")
    ap.add_argument("--pull", action="store_true", help="bring merged reviewer decisions into the local approval files")
    ap.add_argument("--execute", action="store_true", help="actually push / write (default: dry run)")
    ap.add_argument("--no-pr", action="store_true", help="push the branch but do not open a PR")
    ap.add_argument("--git-name", default=None, help="commit author name (default: the repo's git config)")
    ap.add_argument("--git-email", default=None, help="commit author email (default: the repo's git config)")
    args = ap.parse_args()

    try:
        cfg = load_config(args.config)
        slug = workspace_slug(cfg.expected_name)
        scope_paths(cfg, ROOT)
        repo = str(Path(args.repo).expanduser()) if args.repo else cfg.audit_repo_path
        if not repo:
            raise ConfigError("no audit repo: set audit_repo.path in the config or pass --repo")
        audit_repo.fetch(repo)
        if not audit_repo.is_private(repo):
            raise audit_repo.AuditRepoError("could not confirm the audit repo is PRIVATE (needs `gh` logged in); "
                                            "it holds workspace data and must never be public")

        mode = "EXECUTE" if args.execute else "DRY RUN"
        if args.pull:
            notes = actions.merge_reviewed_approvals(cfg.approvals_dir, audit_repo.reviewed_approvals(repo, slug),
                                                     write=args.execute)
            print(f"{mode} | pull | workspace={cfg.expected_name!r}")
            print("\n".join(f"  {n}" for n in notes) or "  nothing new approved on origin/main")
            return 0

        files = audit_repo.local_files(slug, cfg.reports_dir, cfg.approvals_dir, cfg.state_dir)
        changes = audit_repo.plan_push(repo, files)
        print(f"{mode} | push | workspace={cfg.expected_name!r} | repo={repo}")
        for c in changes:
            print(f"  {c.kind:6} {c.relpath}" + (f"  ({c.reason})" if c.reason else ""))
        todo = [c for c in changes if c.kind != "skip"]
        if not todo:
            print("  nothing to push")
            return 0
        if not args.execute:
            print("dry run: nothing pushed. Re-run with --execute.")
            return 0

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        branch = f"sync/{slug}/{stamp}"
        ident = tuple(a for k, v in (("user.name", args.git_name), ("user.email", args.git_email)) if v
                      for a in ("-c", f"{k}={v}"))
        audit_repo.push(repo, branch, changes, f"Sync {slug} run output ({stamp})", ident)
        print(f"pushed branch {branch}")
        if not args.no_pr:
            new_approvals = [c.relpath for c in todo if c.relpath.startswith("approvals/") and c.kind == "add"]
            body = ("Run output from AMER-Demo-Agent.\n\n" +
                    ("Approval files to review (set `approved: true` only on items you accept, on this branch, "
                     "then merge):\n" + "\n".join(f"- {p}" for p in new_approvals) + "\n\n" if new_approvals else "") +
                    "Nothing executes until an operator pulls main and runs `--apply-approvals --execute`.")
            p = subprocess.run(["gh", "pr", "create", "--base", "main", "--head", branch, "--title",
                                f"Review {slug} run output ({stamp})", "--body", body],
                               cwd=repo, capture_output=True, text=True)
            print(p.stdout.strip() or p.stderr.strip())
            return 0 if p.returncode == 0 else 1
    except (ConfigError, audit_repo.AuditRepoError) as exc:
        print(f"REFUSING: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
