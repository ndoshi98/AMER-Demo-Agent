"""Read/plan side of the private audit repo (AMER-Demo-Agent-auditlogs_and_approvals). Pure git + YAML, no SDK.

The repo is where a human reviews Tier 2 approval files in a PR. This module:
  * make_gate(): refuses to execute an item unless the same item is `approved: true` on origin/main of that repo;
  * plan_push() / push(): put new run output on a branch (never main) for a PR, without ever carrying an
    unreviewed approval;
  * reviewed_approvals(): the approval files as merged on origin/main, for pulling decisions back locally.
Nothing here touches the Fireblocks workspace. Every git call is an argument list (no shell).
"""
from __future__ import annotations

import fnmatch
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import yaml

MAIN_REF = "origin/main"
Gate = Callable[[str, Dict[str, Any], Dict[str, Any]], Optional[str]]


class AuditRepoError(RuntimeError):
    pass


def _git(repo: str, *args: str, check: bool = True, timeout: int = 120) -> str:
    try:
        p = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AuditRepoError(f"git failed: {exc}") from exc
    if check and p.returncode != 0:
        raise AuditRepoError(f"git {' '.join(a for a in args[:3] if not a.startswith('user.'))} failed: "
                             f"{(p.stderr or p.stdout).strip()}")
    return p.stdout if p.returncode == 0 else ""


def check_repo(repo: str) -> None:
    if not repo or not Path(repo).is_dir():
        raise AuditRepoError(f"audit repo path {repo!r} does not exist")
    if _git(repo, "rev-parse", "--is-inside-work-tree").strip() != "true":
        raise AuditRepoError(f"{repo} is not a git working tree")
    _git(repo, "remote", "get-url", "origin")


def fetch(repo: str) -> None:
    """Fail closed: if origin/main can't be refreshed, nothing may be treated as reviewed."""
    check_repo(repo)
    _git(repo, "fetch", "--quiet", "origin", "main", timeout=60)


def is_private(repo: str) -> bool:
    try:
        p = subprocess.run(["gh", "repo", "view", "--json", "isPrivate", "-q", ".isPrivate"], cwd=repo,
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return p.returncode == 0 and p.stdout.strip() == "true"


def read_main(repo: str, relpath: str, ref: str = MAIN_REF) -> Optional[str]:
    p = subprocess.run(["git", "-C", repo, "show", f"{ref}:{relpath}"], capture_output=True, text=True)
    return p.stdout if p.returncode == 0 else None


def list_main(repo: str, dirpath: str, pattern: str, ref: str = MAIN_REF) -> List[str]:
    out = _git(repo, "ls-tree", "--name-only", ref, f"{dirpath}/", check=False)
    return sorted(Path(n).name for n in out.splitlines() if fnmatch.fnmatch(Path(n).name, pattern))


def _load(text: Optional[str]) -> Optional[Dict[str, Any]]:
    if text is None:
        return None
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError:
        return None
    return doc if isinstance(doc, dict) else None


def identity(it: Dict[str, Any]) -> Tuple[Any, ...]:
    return (it.get("id"), it.get("entity_type"), str(it.get("entity_id")), it.get("action"))


def reviewed_approvals(repo: str, slug: str) -> Dict[str, Dict[str, Any]]:
    """filename -> parsed approval doc as it stands on origin/main."""
    out = {}
    for name in list_main(repo, f"approvals/{slug}", "approvals-*.yaml"):
        doc = _load(read_main(repo, f"approvals/{slug}/{name}"))
        if doc is not None:
            out[name] = doc
    return out


def make_gate(repo: str, slug: str) -> Gate:
    """gate(filename, local_doc, local_item) -> None if origin/main shows the same item approved, else the reason."""
    fetch(repo)
    cache: Dict[str, Optional[Dict[str, Any]]] = {}

    def gate(filename: str, doc: Dict[str, Any], item: Dict[str, Any]) -> Optional[str]:
        if filename not in cache:
            cache[filename] = _load(read_main(repo, f"approvals/{slug}/{filename}"))
        main = cache[filename]
        if main is None:
            return "approval file is not on origin/main of the audit repo (push it, get it reviewed and merged)"
        if main.get("run_id") != doc.get("run_id"):
            return "approval file on origin/main is from a different run"
        match = [m for m in main.get("items") or [] if identity(m) == identity(item)]
        if not match:
            return "item is not in the approval file on origin/main"
        if match[0].get("approved") is not True:
            return "item is not approved on origin/main (approved locally only)"
        return None
    return gate


# -- push side ----------------------------------------------------------------------------------
@dataclass
class Change:
    relpath: str
    kind: str          # add | update | skip
    reason: str = ""
    content: str = ""


def local_files(slug: str, reports_dir: str, approvals_dir: str, state_dir: str) -> Dict[str, Path]:
    files: Dict[str, Path] = {}
    for d, kind, pats in ((reports_dir, "reports", ("*.md", "*.json")), (approvals_dir, "approvals", ("approvals-*.yaml",))):
        for pat in pats:
            for p in sorted(Path(d).glob(pat)) if Path(d).exists() else []:
                files[f"{kind}/{slug}/{p.name}"] = p
    j = Path(state_dir) / "journal.jsonl"
    if j.exists():
        files[f"journals/{slug}/journal.jsonl"] = j
    return files


def _approval_shape(doc: Dict[str, Any]) -> Tuple[Any, ...]:
    return (doc.get("run_id"), doc.get("created_at"),
            tuple((identity(i), i.get("approved")) for i in doc.get("items") or []))


def plan_push(repo: str, files: Dict[str, Path]) -> List[Change]:
    """Changes needed to bring origin/main up to date. Approvals are never allowed to carry a new decision."""
    changes: List[Change] = []
    for rel, path in sorted(files.items()):
        data = path.read_text(encoding="utf-8")
        main = read_main(repo, rel)
        if main == data:
            continue
        if rel.startswith("approvals/"):
            doc = _load(data)
            if doc is None:
                changes.append(Change(rel, "skip", "not a readable approval file"))
            elif main is None:
                if any(i.get("approved") is not False for i in doc.get("items") or []):
                    changes.append(Change(rel, "skip", "new approval file has items not marked `approved: false`; "
                                                       "decisions must be made in a PR, not carried in locally"))
                else:
                    changes.append(Change(rel, "add", content=data))
            else:
                mdoc = _load(main)
                if mdoc is None or _approval_shape(mdoc) != _approval_shape(doc):
                    changes.append(Change(rel, "skip", "approved flags or items differ from origin/main; "
                                                       "refusing to overwrite a reviewed approval file"))
                else:
                    changes.append(Change(rel, "update", "status/outcome fields only", data))
        elif rel.startswith("journals/"):
            if main is None:
                changes.append(Change(rel, "add", content=data))
            elif data.startswith(main):
                changes.append(Change(rel, "update", "append-only", data))
            else:
                changes.append(Change(rel, "skip", "local journal is not an extension of origin/main's; not overwriting"))
        else:
            changes.append(Change(rel, "add" if main is None else "update", content=data))
    return changes


def push(repo: str, branch: str, changes: List[Change], message: str, identity_args: Tuple[str, ...] = ()) -> None:
    """Commit the add/update changes on `branch` (cut from origin/main) and push it. Never touches main."""
    todo = [c for c in changes if c.kind != "skip"]
    if not todo:
        return
    if branch in ("main", "master"):
        raise AuditRepoError("refusing to push to main directly; changes must go through a PR branch")
    if _git(repo, "status", "--porcelain").strip():
        raise AuditRepoError("audit repo working tree is not clean; commit or stash first")
    orig = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
    if orig == "HEAD":
        orig = _git(repo, "rev-parse", "HEAD").strip()
    _git(repo, "checkout", "-q", "-B", branch, MAIN_REF)
    try:
        for c in todo:
            dest = Path(repo) / c.relpath
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(c.content, encoding="utf-8")
            _git(repo, "add", "--", c.relpath)
        _git(repo, *identity_args, "commit", "-q", "-m", message)
        _git(repo, "push", "-q", "-u", "origin", branch)
    finally:
        _git(repo, "checkout", "-q", orig, check=False)
