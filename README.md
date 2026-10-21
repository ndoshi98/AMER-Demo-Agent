# AMER-Demo-Agent

Cleanup agent for the shared AMER (demo) Fireblocks workspace. It hides empty vaults, proposes TAP consolidation, and
requests deletion of dormant console users. **Dry run by default. Nothing changes unless you pass `--execute`.**

Status: built and unit-tested offline (120 tests). On the Testnet workspace, `probe.py --online`, dry-run audits, a Tier 1
`--execute` (29 vaults hidden) and `--undo-run` (all 29 restored) have been run. A Tier 2
`--apply-approvals --execute` has deleted one internal wallet on Testnet. Connection, tag and user deletions have not been run. A read-only pull of the AMER (demo) TAP policy has been analyzed (see item 9); nothing in AMER has been changed.

## Workspaces

Exactly two workspaces are in scope. The agent is never pointed at any other.

| | Testnet | AMER (demo) |
|---|---|---|
| Role | Rehearsal. Every action is tried here first, including destructive ones. | The only mainnet / production workspace. The real shared demo console. |
| Name (`workspace.expected_name`) | `Neil Doshi - Testnet [ApI Test]` | The exact AMER demo workspace name. Not yet configured. |
| `workspace.is_production` | `false` | `true`, which also requires `--i-understand-this-is-prod` |
| Config | `config/cleanup.yaml` | A separate file, passed with `--config` |
| State, reports, approvals | `state/`, `reports/`, `approvals/` under `neil-doshi-testnet-api-test/` | Same folders under the AMER slug, e.g. `amer-demo/`. Never shared. |
| API key | Testnet key in `config/.env` | A separate AMER key. Never use one key for both. |

- Testnet uses `base_path: us`, the same production API host a mainnet workspace would use, so the base path says
  nothing about which workspace you are on. `expected_name` is the guard, and the run aborts if the name differs.
- **State is scoped per workspace.** `state/`, `reports/` and `approvals/` each get a subfolder named after
  `workspace.expected_name` (lowercased, punctuation replaced by `-`). That folder holds the quarantine ledger,
  action journal, reports and approval files, so ids like vault `1` can never collide across workspaces. The run
  refuses to start without `expected_name`, and aborts if the real workspace name differs.
- Nothing is aimed at AMER until the Testnet plan below is done.

## What it does

| Tier | Actions | Gate |
|---|---|---|
| 1 | Hide / unhide empty vaults | Automatic with `--execute`, after quarantine. Reversible (`--undo-run`). |
| 2 | Delete external/internal wallets, network connections, tags; request console-user deletion | YAML approval file, `approved: true`, applied in a **later** run with `--apply-approvals --execute` |
| 3 | TAP consolidation, vault renames | Propose only. A human publishes the policy. |

TAP consolidation (`tap_consolidate.py`) removes duplicate and unreachable rules and merges rules that differ in one
dimension (asset, source, destination or amount range). Every step is verified: the original and proposed rule lists
must give the same outcome in every case the policy can distinguish. It reports two proposals: behaviour-identical, and
one that also drops a BLOCK rule if transactions matching no rule are blocked (an unverified assumption). It also lists
transactions that match no rule. Rules it can't model exactly are left in place and nothing moves across them.

Hard constraints (details in `CLAUDE.md`):

- Vaults cannot be deleted, only hidden. Hiding a vault also hides its transaction history in the console.
- There is no last-login field. User activity is inferred from audit logs, and the absence of events is weak evidence.
- No policy-write operation exists in the client, and none should be added.
- Console-user deletion is async and quorum-gated. `force` is sent only with `--force-user-delete`.

## Safety model

- **Single choke point.** Every SDK call goes through `FireblocksClient.call()`: dry-run suppression of mutations,
  rate limiting, a JSONL journal, retries for reads only, and translated errors.
- **Quarantine.** Nothing acts on first sighting. An entity needs at least 2 consecutive flagged runs (a floor that
  config can't lower) and at least 14 days flagged. A released entity restarts from zero. An acted entity never
  re-enters.
- **Fail closed.** If a collection is incomplete, that entity type is not evaluated. The quarantine clock neither
  advances nor resets.
- **Re-verification.** Tier 1 re-fetches the vault and re-checks zero balance immediately before hiding. Tier 2
  re-classifies from fresh data and skips anything no longer flagged.
- **Tier 2 never executes in the run that proposes it.** It requires `approved: true` (a real boolean), an approval
  file under 30 days old, and a different run id.
- **Prod gate.** AMER is the only workspace with `workspace.is_production: true`, and that also requires
  `--i-understand-this-is-prod`. No config key bypasses it. `workspace.expected_name` refuses to run against a
  differently named workspace.

### Why Tier 2 needs an approval file when some deletions have a quorum

Console-user deletion is quorum-gated: the API call only *requests* it, and it completes after the workspace quorum
approves (default: Owner only). Wallet, connection and tag deletion have no known quorum (the Testnet internal-wallet
delete ran with only the approval file; delete semantics are still unverified, known issue 4). The file stays for every
Tier 2 action because:

- **It decides whether the agent asks at all.** The quorum approves a request that already exists. Without the file,
  every dormant-user guess would become a pending quorum request.
- **It is the only human gate for wallets, connections and tags.**
- **It carries the evidence.** A quorum approver sees "delete user X"; the file records why (quarantine history, days
  flagged, the weak audit-log evidence), which makes rubber-stamping less likely.
- **The quorum can be loosened.** Anyone with the right role can change it in Settings > Quorums, so it is not a
  guarantee the agent can rely on.

Fireblocks' own audit log records the request and the quorum approval, so outcomes for user deletions can be
reconciled from it (the agent already confirms completion by polling for `enabled: false`). That is an outcome record,
not a replacement for the decision record. Decision made 2026-10-21: keep the file for all Tier 2 actions, including
user deletions. Shared review of approval files happens through the private audit repo
(`AMER-Demo-Agent-auditlogs_and_approvals`), never this public one.

## Setup

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp config/.env.example config/.env      # fill in FIREBLOCKS_API_KEY and FIREBLOCKS_SECRET_PATH
chmod 600 /path/to/your/secret.key      # the loader refuses key files with looser permissions
```

`config/.env`, key files, `state/`, `reports/` and `approvals/` are gitignored. This repo is public, so never commit
them.

Edit `config/cleanup.yaml` for Testnet. Unknown keys are rejected. For AMER, copy it to a separate config file, set
`expected_name` and `is_production: true`, and pass it with `--config`. `expected_name` is required. See Workspaces.
Keep AMER credentials in a separate file (e.g. `config/.env.amer`, gitignored, mode 600) and pass `--env-file`. Unlike the
default `config/.env`, `--env-file` overrides `FIREBLOCKS_API_KEY` / `FIREBLOCKS_SECRET_PATH` already in your shell, so the
Testnet key can't be used by accident. Local AMER/test configs are gitignored.

## Usage

```bash
# 0. offline checks, no credentials needed
.venv/bin/python -m pytest -q
.venv/bin/python scripts/probe.py --offline        # resolves every OPS entry against the installed SDK

# 1. read-only probe of the workspace (permissions, audit event names, shapes)
.venv/bin/python scripts/probe.py --online

# 2. audit: collect, classify, report. Dry run.
.venv/bin/python scripts/run_cleanup.py
.venv/bin/python scripts/run_cleanup.py --no-observe     # preview without advancing quarantine clocks

# 3. act (Tier 1 hides only)
.venv/bin/python scripts/run_cleanup.py --execute

# 4. Tier 2: edit approvals/approvals-*.yaml, then in a later run
.venv/bin/python scripts/run_cleanup.py --apply-approvals            # dry run
.venv/bin/python scripts/run_cleanup.py --apply-approvals --execute

# 5. shared review of Tier 2 approvals through the private audit repo (see "Shared review" below)
.venv/bin/python scripts/sync_audit.py                     # dry run: what would go to a PR branch
.venv/bin/python scripts/sync_audit.py --execute           # push a branch + open a PR (never writes to main)
.venv/bin/python scripts/sync_audit.py --pull --execute    # after merge: copy reviewer approvals into local files

# undo a run's hides (also releases them from the ledger, so quarantine restarts from zero)
.venv/bin/python scripts/run_cleanup.py --undo-run <run-id> --execute
```

Outputs: `reports/` (markdown report and `tap-proposed-<run>.json`), `approvals/` (Tier 2 YAML), `state/` (SQLite
ledger and `journal.jsonl`).

### Shared review of Tier 2 approvals

Reports, journals and approval files can be pushed to a private audit repo (`AMER-Demo-Agent-auditlogs_and_approvals`)
so several people review approvals in a PR. Set `audit_repo.path` (a local clone) in the workspace config.

1. Audit run proposes `approvals-<run>.yaml` (all `approved: false`).
2. `sync_audit.py --execute` pushes a branch and opens a PR. It refuses unless `gh` confirms the repo is private, it
   never writes to `main`, it will not carry a new approval file that has any item approved, and it will not overwrite a
   reviewed approval file with different decisions. Journals are only appended to.
3. Reviewers set `approved: true` on the items they accept (on the PR branch) and merge.
4. The operator runs `sync_audit.py --pull --execute`, which only sets `approved: true` on matching still-pending local
   items.
5. `run_cleanup.py --apply-approvals` additionally fetches `origin/main` of the audit repo and executes an item only if
   the same item (id, entity, action, run) is `approved: true` there. A hand-edited local file cannot execute. If the
   fetch fails, the run refuses. **A production config without `audit_repo.path` refuses `--apply-approvals`.**

Limits: the check proves the decision exists on `origin/main`, not that a second person made it. Enforce that with
GitHub branch protection (required reviews, no direct pushes to `main`), which needs a plan that supports it for private
repos. The operator machine still needs the API key, and `--execute` is still a human choice.

## Testnet-first plan

Steps 1 to 5 run only against the Testnet workspace. Step 6 is the only step that touches AMER.

1. `probe.py --offline`, then `probe.py --online` against Testnet. Read the output before anything else. (done)
2. Dry-run audits with `--no-observe` until the report looks right. (done)
3. Normal dry-run audits across at least two runs 14+ days apart, to exercise quarantine. (the Testnet round used
   `min_days_flagged: 0` to shortcut this; AMER uses 14)
4. `--execute` for Tier 1 only. Check the result in the console, then try `--undo-run`. (done)
5. Tier 2 on a few throwaway objects, created by hand in the Testnet console.
6. Point at AMER with its own config, key and `state_dir`. Start with a read-only probe and `--no-observe` audits.

## Known issues and risks (to fix or verify before AMER)

### Settled by `probe.py --online` against the testnet workspace (2026-10-20)
- **API key permissions: sufficient for every read.** Vaults, transactions, console users, audit logs, v2 TRANSFER
  policy, wallets, connections and tags all read fine. Write permission still can't be tested without mutating.
- **Audit log time window is epoch milliseconds, not seconds.** My original assumption was wrong and the call failed.
  Fixed in `collect_audit` and covered by `tests/test_collect.py`. A 365-day window works (2,730 events, 7 users).
- **Audit has a real login event** (`Administration` / `Signed in`), plus `Access denied`. `audit.activity_events` is
  left empty, so any audit event counts as activity. That is the conservative choice (fewer users flagged).
- **Hidden vaults are included in the vault listing, flagged `hiddenOnUI: true`.** Confirmed on 2026-10-21: after
  `--execute` hid 29 vaults, a fresh listing returned all of them with `hiddenOnUI: true`, and `--undo-run` took the
  count back to 0. "Already hidden" detection therefore works, and hidden vaults still count toward tag usage. The first
  probe saw 0 hidden vaults of 60. The 19 `Archived` audit events in that window are unexplained.
- **v2 policy shape** (`{policy: {rules: [...]}, validation}`) parses cleanly: 3 rules, none opaque.
- **TAP signers can be API users**, not only console users. `list_api_users` is now collected and merged with console
  users for the dead-reference check (skipped entirely if either list fails to load), which removed a false
  DEAD_REF on the testnet policy.
- **Legacy `/tap/active_policy` returns 500** on this workspace. v2 works, so nothing depends on legacy here.
- **SDK noise.** The v2 policy response triggers a pydantic "Failed to deserialize ... AssetTypesConfigInner" warning
  on the `'*'` wildcard. The SDK falls back to the raw JSON and the data is intact, so it's cosmetic.

### Still to verify
1. **Write permission** is settled for Testnet (hide and unhide worked). Tier 2 delete permission is still untested, and
   AMER's key needs its own check.
2. **Non-audit pagination.** Vault paging worked (60 vaults, complete). The transaction time-walk, network-connection
   and wallet list shapes are still assumed.
3. **Tags.** The `next` cursor isn't implemented, so a multi-page tag list is marked incomplete.
4. **Delete semantics** for internal wallets, external wallets and connections (side effects, dependent objects).

### Design gaps
5. **No vault creation time.** A fresh empty vault prepared for an upcoming demo can be hidden after 14 days. Use the
   `keep-for-demo` tag, `protect.name_patterns`, and `--undo-run`.
6. **Connection threshold is 365 days, but the API shows only ~200 days of transaction history** (testnet, as of
   2026-10-21). A connection is flagged only when visible history reaches back the full window. Until then,
   connections with no visible transaction are kept and listed for a manual decision, because unused cannot be
   told from newly created. Expect flagging to start around March 2027.
7. **Tier 1 tag add/remove is not implemented.** `create_tag` and vault renames beyond duplicate-name suggestions
   are also missing. The tag-based protection only works if people tag vaults by hand.
8. **Tag usage** is derived from vault tags only. Contact-tag usage is not checked.
9. **Policy analysis coverage.** Consolidation models v2 `TRANSFER` rules: `INCLUDES` blocks, source/destination
    `type`/`subType`/`ids`, initiator users and groups, asset lists, USD and NATIVE amount ranges, `index` and
    `v1RuleId`. Anything else (EXCLUDES, `amountOverTime`, unknown keys, legacy shape) is opaque and acts as a barrier.
    Assumptions: amount bounds are inclusive; named asset and initiator-group values may overlap (this blocks some
    merges); unmatched transactions are blocked (only the default-deny proposal uses this, and it is unverified); a USD
    range is assumed to match every asset, though the engine may not be able to price testnet assets. It gives up beyond
    10 distinct value groups in one dimension or 400k verification nodes. Only the v2 `TRANSFER` type is collected, and
    rule order from the API is assumed to be evaluation order. Fuzz-tested against a brute-force check. On the AMER
    policy (2026-10-21) all 26 rules parse and a catch-all at position 7 makes 20 later rules unreachable (see
    `--env-file` below). Union-of-rules shadowing in `tap.py` is not detected.
10. **Dry-run audits advance the quarantine ledger.** Use `--no-observe` for previews. The 14-day span limits damage
    from rapid reruns. `--apply-approvals` relies on the ledger from the last audit plus a fresh re-classification.

### Console user deletion
11. Needs a quorum (default: Owner only), so expect `2760` until the quorum is adjusted in Settings > Quorums.
12. Users pending onboarding need their invite revoked in the console by hand.
13. Users referenced in TAP are protected, and the reference must be cleaned up first.
14. Completion is confirmed only when a later poll shows `enabled: false`.

### Environment
15. **Testnet workspaces use the production base path** (`us`/`eu`/`eu2`), so `is_production` is self-declared. Set
    `expected_name` to guard against hitting the wrong workspace. It is required, and it also names the per-workspace
    state folder.
16. Python 3.9 with LibreSSL prints a urllib3 `NotOpenSSLWarning`. It is harmless but noisy.
