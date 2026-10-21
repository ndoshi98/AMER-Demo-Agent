import subprocess

import pytest
import yaml

from fbcleanup import actions as A
from fbcleanup import audit_repo as AR
from fbcleanup.config import ConfigError, parse_config
from fbcleanup.state import DAY, State

from test_actions import T0, T1, approve_all, cfg, quarantined, wallet_decision

SLUG = "ws"
ITEM = {"id": "external_wallet:w1", "entity_type": "external_wallet", "entity_id": "w1", "name": "Old CP",
        "action": "delete_external_wallet", "approved": False, "status": "pending"}


def doc(approved=False, run_id="b", extra_items=()):
    return {"run_id": run_id, "created_at": "2026-10-01T00:00:00Z", "items": [dict(ITEM, approved=approved), *extra_items]}


def sh(*args, cwd):
    subprocess.run(args, cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repos(tmp_path, monkeypatch):
    for k in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{k}_NAME", "t")
        monkeypatch.setenv(f"GIT_{k}_EMAIL", "t@example.invalid")
    origin, seed, work = tmp_path / "origin.git", tmp_path / "seed", tmp_path / "work"
    sh("git", "init", "-q", "--bare", "-b", "main", str(origin), cwd=tmp_path)
    sh("git", "clone", "-q", str(origin), str(seed), cwd=tmp_path)
    (seed / "README.md").write_text("x")
    sh("git", "add", ".", cwd=seed)
    sh("git", "commit", "-q", "-m", "init", cwd=seed)
    sh("git", "push", "-q", "origin", "HEAD:main", cwd=seed)
    sh("git", "clone", "-q", str(origin), str(work), cwd=tmp_path)

    class R:
        path = str(work)

        @staticmethod
        def merge_to_main(rel, text):
            f = seed / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(text)
            sh("git", "add", ".", cwd=seed)
            sh("git", "commit", "-q", "-m", "m", cwd=seed)
            sh("git", "push", "-q", "origin", "HEAD:main", cwd=seed)

        @staticmethod
        def origin_branches():
            out = subprocess.run(["git", "branch", "--list"], cwd=origin, capture_output=True, text=True).stdout
            return sorted(b.strip("* ").strip() for b in out.splitlines())
    return R


def yml(d):
    return yaml.safe_dump(d, sort_keys=False)


# --- gate -----------------------------------------------------------------------------------------
def test_gate_requires_the_same_item_approved_on_origin_main(repos):
    rel = f"approvals/{SLUG}/approvals-b.yaml"
    gate = AR.make_gate(repos.path, SLUG)
    assert "not on origin/main" in gate("approvals-b.yaml", doc(True), ITEM)

    repos.merge_to_main(rel, yml(doc(False)))
    gate = AR.make_gate(repos.path, SLUG)
    assert "approved locally only" in gate("approvals-b.yaml", doc(True), ITEM)

    repos.merge_to_main(rel, yml(doc(True)))
    gate = AR.make_gate(repos.path, SLUG)
    assert gate("approvals-b.yaml", doc(True), ITEM) is None
    assert "different run" in gate("approvals-b.yaml", doc(True, run_id="other"), ITEM)
    assert "not in the approval file" in gate("approvals-b.yaml", doc(True), dict(ITEM, id="external_wallet:w2"))
    assert "not in the approval file" in gate("approvals-b.yaml", doc(True), dict(ITEM, action="delete_internal_wallet"))


def test_gate_fails_closed_when_origin_is_unreachable(repos, tmp_path):
    sh("git", "remote", "set-url", "origin", str(tmp_path / "gone.git"), cwd=repos.path)
    with pytest.raises(AR.AuditRepoError):
        AR.make_gate(repos.path, SLUG)
    with pytest.raises(AR.AuditRepoError):
        AR.make_gate(str(tmp_path / "missing"), SLUG)


def test_apply_approvals_honours_the_gate(make_client, tmp_path):
    c, st, d = cfg(tmp_path), State(":memory:"), wallet_decision()
    quarantined(st, [d])
    A.propose_tier2(st, [d], c, "b", T1)
    approve_all(tmp_path)
    cl, sdk = make_client({("external_wallets", "delete_external_wallet"): {}}, dry_run=False)
    out = A.apply_approvals(cl, st, c, "later", T1 + DAY, {d.key}, gate=lambda f, doc_, it: "nope")
    assert out[0].status == "skipped" and "audit repo" in out[0].detail and sdk.calls("delete_external_wallet") == []
    assert yaml.safe_load(next((tmp_path / "approvals").glob("*.yaml")).read_text())["items"][0]["status"] == "pending"
    out = A.apply_approvals(cl, st, c, "later", T1 + DAY, {d.key}, gate=lambda f, doc_, it: None)
    assert out[0].status == "executed" and len(sdk.calls("delete_external_wallet")) == 1


# --- push -----------------------------------------------------------------------------------------
def files(tmp_path, **named):
    out = {}
    for rel, text in named.items():
        p = tmp_path / "local" / rel.replace("/", "_")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        out[rel] = p
    return out


def kinds(changes):
    return {c.relpath: c.kind for c in changes}


def test_plan_push_never_carries_an_unreviewed_approval(repos, tmp_path):
    rel = f"approvals/{SLUG}/approvals-b.yaml"
    new_ok = kinds(AR.plan_push(repos.path, files(tmp_path, **{rel: yml(doc(False))})))
    assert new_ok == {rel: "add"}
    new_bad = AR.plan_push(repos.path, files(tmp_path, **{rel: yml(doc(True))}))
    assert kinds(new_bad) == {rel: "skip"} and "PR" in new_bad[0].reason


def test_plan_push_updates_outcomes_but_never_overwrites_decisions(repos, tmp_path):
    rel = f"approvals/{SLUG}/approvals-b.yaml"
    repos.merge_to_main(rel, yml(doc(True)))
    AR.fetch(repos.path)
    executed = doc(True)
    executed["items"][0].update(status="executed", detail="deleted")
    assert kinds(AR.plan_push(repos.path, files(tmp_path, **{rel: yml(executed)}))) == {rel: "update"}
    tampered = doc(False)
    assert kinds(AR.plan_push(repos.path, files(tmp_path, **{rel: yml(tampered)}))) == {rel: "skip"}
    extra = doc(True, extra_items=[dict(ITEM, id="external_wallet:w9", entity_id="w9")])
    assert kinds(AR.plan_push(repos.path, files(tmp_path, **{rel: yml(extra)}))) == {rel: "skip"}


def test_plan_push_journal_is_append_only(repos, tmp_path):
    rel = f"journals/{SLUG}/journal.jsonl"
    repos.merge_to_main(rel, "a\nb\n")
    AR.fetch(repos.path)
    assert kinds(AR.plan_push(repos.path, files(tmp_path, **{rel: "a\nb\nc\n"}))) == {rel: "update"}
    assert kinds(AR.plan_push(repos.path, files(tmp_path, **{rel: "x\n"}))) == {rel: "skip"}
    assert AR.plan_push(repos.path, files(tmp_path, **{rel: "a\nb\n"})) == []


def test_push_goes_to_a_branch_not_main_and_restores_the_checkout(repos, tmp_path):
    rel = f"reports/{SLUG}/cleanup-1.md"
    ch = AR.plan_push(repos.path, files(tmp_path, **{rel: "report"}))
    AR.push(repos.path, f"sync/{SLUG}/1", ch, "msg")
    assert "sync/ws/1" in repos.origin_branches()
    assert subprocess.run(["git", "show", f"origin/main:{rel}"], cwd=repos.path, capture_output=True).returncode != 0
    assert subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repos.path, capture_output=True,
                          text=True).stdout.strip() == "main"


def test_push_refuses_main_and_dirty_trees(repos, tmp_path):
    ch = AR.plan_push(repos.path, files(tmp_path, **{f"reports/{SLUG}/x.md": "r"}))
    with pytest.raises(AR.AuditRepoError):
        AR.push(repos.path, "main", ch, "msg")
    (tmp_path / "work" / "dirty.txt").write_text("x")
    with pytest.raises(AR.AuditRepoError):
        AR.push(repos.path, "sync/x", ch, "msg")
    assert "sync/x" not in repos.origin_branches()


# --- pull -----------------------------------------------------------------------------------------
def write_local(tmp_path, d, name="approvals-b.yaml"):
    p = tmp_path / "approvals" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yml(d))
    return p


def test_pull_sets_approved_only_on_matching_pending_items(tmp_path):
    p = write_local(tmp_path, doc(False))
    reviewed = {"approvals-b.yaml": doc(True)}
    assert A.merge_reviewed_approvals(str(tmp_path / "approvals"), reviewed, write=False)
    assert yaml.safe_load(p.read_text())["items"][0]["approved"] is False         # dry run
    notes = A.merge_reviewed_approvals(str(tmp_path / "approvals"), reviewed, write=True)
    assert len(notes) == 1 and yaml.safe_load(p.read_text())["items"][0]["approved"] is True


def test_pull_never_unapproves_or_touches_other_runs_or_executed_items(tmp_path):
    d = doc(True)
    p = write_local(tmp_path, d)
    assert A.merge_reviewed_approvals(str(tmp_path / "approvals"), {"approvals-b.yaml": doc(False)}, True) == []
    assert yaml.safe_load(p.read_text())["items"][0]["approved"] is True
    executed = doc(False)
    executed["items"][0]["status"] = "executed"
    p = write_local(tmp_path, executed)
    assert A.merge_reviewed_approvals(str(tmp_path / "approvals"), {"approvals-b.yaml": doc(True)}, True) == []
    other = doc(True, run_id="z")
    other["created_at"] = "2026-09-01T00:00:00Z"
    write_local(tmp_path, doc(False))
    notes = A.merge_reviewed_approvals(str(tmp_path / "approvals"), {"approvals-b.yaml": other}, True)
    assert "different run" in notes[0]


def test_pull_copies_files_that_do_not_exist_locally(tmp_path):
    notes = A.merge_reviewed_approvals(str(tmp_path / "approvals"), {"approvals-q.yaml": doc(True, run_id="q")}, True)
    assert "new file" in notes[0] and (tmp_path / "approvals" / "approvals-q.yaml").exists()


# --- config ---------------------------------------------------------------------------------------
def test_audit_repo_config_key(tmp_path):
    assert parse_config({}).audit_repo_path == ""
    assert parse_config({"audit_repo": {"path": str(tmp_path)}}).audit_repo_path == str(tmp_path)
    with pytest.raises(ConfigError):
        parse_config({"audit_repo": {"url": "x"}})
