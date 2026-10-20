# AMER-Demo-Agent

Cleanup agent for a shared Fireblocks GTM demo workspace. It hides empty vaults, proposes TAP consolidation, and
requests deletion of dormant console users. **Dry run by default. Nothing changes unless you pass `--execute`.**

Status: built and unit-tested offline (78 tests). The read-only `probe.py --online` has been run against the testnet
workspace. No audit, `--execute` or approval run has been done yet.

## What it does

| Tier | Actions | Gate |
|---|---|---|
| 1 | Hide / unhide empty vaults | Automatic with `--execute`, after quarantine. Reversible (`--undo-run`). |
| 2 | Delete external/internal wallets, network connections, tags; request console-user deletion | YAML approval file, `approved: true`, applied in a **later** run with `--apply-approvals --execute` |
| 3 | TAP consolidation, vault renames | Propose only. A human publishes the policy. |

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
- **Prod gate.** `workspace.is_production: true` also requires `--i-understand-this-is-prod`. No config key bypasses it.
  `workspace.expected_name` refuses to run against a differently named workspace.

## Setup

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp config/.env.example config/.env      # fill in FIREBLOCKS_API_KEY and FIREBLOCKS_SECRET_PATH
chmod 600 /path/to/your/secret.key      # the loader refuses key files with looser permissions
```

`config/.env`, key files, `state/`, `reports/` and `approvals/` are gitignored. This repo is public, so never commit
them.

Edit `config/cleanup.yaml`. Unknown keys are rejected. Set `workspace.expected_name` to the exact workspace name.

## Usage

```bash
# 0. offline checks, no credentials needed
.venv/bin/python -m pytest -q
.venv/bin/python scripts/probe.py --offline        # resolves every OPS entry against the installed SDK

# 1. read-only probe of the real workspace (permissions, audit event names, shapes)
.venv/bin/python scripts/probe.py --online

# 2. audit: collect, classify, report. Dry run.
.venv/bin/python scripts/run_cleanup.py
.venv/bin/python scripts/run_cleanup.py --no-observe     # preview without advancing quarantine clocks

# 3. act (Tier 1 hides only)
.venv/bin/python scripts/run_cleanup.py --execute

# 4. Tier 2: edit approvals/approvals-*.yaml, then in a later run
.venv/bin/python scripts/run_cleanup.py --apply-approvals            # dry run
.venv/bin/python scripts/run_cleanup.py --apply-approvals --execute

# undo a run's hides
.venv/bin/python scripts/run_cleanup.py --undo-run <run-id> --execute
```

Outputs: `reports/` (markdown report and `tap-proposed-<run>.json`), `approvals/` (Tier 2 YAML), `state/` (SQLite
ledger and `journal.jsonl`).

## Testnet-first plan

1. `probe.py --offline`, then `probe.py --online` against the testnet workspace. Read the output before anything else.
2. Dry-run audits with `--no-observe` until the report looks right.
3. Normal dry-run audits across at least two runs 14+ days apart, to exercise quarantine.
4. `--execute` for Tier 1 only. Check the result in the console, then try `--undo-run`.
5. Tier 2 on a few throwaway objects.
6. Point at the real demo workspace. Set `expected_name` and `is_production` correctly.

## Known issues and risks (to fix or verify before the real workspace)

### Settled by `probe.py --online` against the testnet workspace (2026-10-20)
- **API key permissions: sufficient for every read.** Vaults, transactions, console users, audit logs, v2 TRANSFER
  policy, wallets, connections and tags all read fine. Write permission still can't be tested without mutating.
- **Audit log time window is epoch milliseconds, not seconds.** My original assumption was wrong and the call failed.
  Fixed in `collect_audit` and covered by `tests/test_collect.py`. A 365-day window works (2,730 events, 7 users).
- **Audit has a real login event** (`Administration` / `Signed in`), plus `Access denied`. `audit.activity_events` is
  left empty, so any audit event counts as activity. That is the conservative choice (fewer users flagged).
- **Hidden vaults are excluded from the vault listing.** 0 of 60 listed vaults are hidden even though the audit log
  shows 19 `Archived` events in 30 days. So "already hidden" detection can't work from the listing, and vault-tag
  usage counts ignore hidden vaults. Unhide by id still works.
- **v2 policy shape** (`{policy: {rules: [...]}, validation}`) parses cleanly: 3 rules, none opaque.
- **Legacy `/tap/active_policy` returns 500** on this workspace. v2 works, so nothing depends on legacy here.
- **SDK noise.** The v2 policy response triggers a pydantic "Failed to deserialize ... AssetTypesConfigInner" warning
  on the `'*'` wildcard. The SDK falls back to the raw JSON and the data is intact, so it's cosmetic.

### Still to verify
1. **Write permission**, which only the first `--execute` run reveals.
2. **Non-audit pagination.** Vault paging worked (60 vaults, complete). The transaction time-walk, network-connection
   and wallet list shapes are still assumed.
3. **Tags.** The `next` cursor isn't implemented, so a multi-page tag list is marked incomplete.
4. **Delete semantics** for internal wallets, external wallets and connections (side effects, dependent objects).

### Design gaps
5. **No vault creation time.** A fresh empty vault prepared for an upcoming demo can be hidden after 14 days. Use the
   `keep-for-demo` tag, `protect.name_patterns`, and `--undo-run`.
6. **Tier 1 tag add/remove is not implemented.** `create_tag` and vault renames beyond duplicate-name suggestions
   are also missing. The tag-based protection only works if people tag vaults by hand.
7. **Tag usage** is derived from vault tags only. Contact-tag usage is not checked.
8. **Policy analysis coverage.** Only the v2 `TRANSFER` type is collected. The v2 adapter parses the one live testnet
    policy (3 simple rules) but is otherwise untested on real policies, and the legacy adapter is unvalidated
    (the legacy endpoint 500s here). v2 dimensions are equality-only, so MERGEABLE and shadow findings are
    under-reported. Legacy amount semantics are not modelled. Union-of-rules shadowing is not detected. Rules the
    analyzer can't model are treated as opaque and never dropped.
9. **Dry-run audits advance the quarantine ledger.** Use `--no-observe` for previews. The 14-day span limits damage
    from rapid reruns. `--apply-approvals` relies on the ledger from the last audit plus a fresh re-classification.

### Console user deletion
10. Needs a quorum (default: Owner only), so expect `2760` until the quorum is adjusted in Settings > Quorums.
11. Users pending onboarding need their invite revoked in the console by hand.
12. Users referenced in TAP are protected, and the reference must be cleaned up first.
13. Completion is confirmed only when a later poll shows `enabled: false`.

### Environment
14. **Testnet workspaces use the production base path** (`us`/`eu`/`eu2`), so `is_production` is self-declared. Set
    `expected_name` to guard against hitting the wrong workspace.
15. Python 3.9 with LibreSSL prints a urllib3 `NotOpenSSLWarning`. It is harmless but noisy.
