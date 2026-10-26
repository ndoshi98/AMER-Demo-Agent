# Fireblocks demo-console cleanup agent — constraints for future sessions

Cleans a shared GTM demo workspace: empty vaults, overlapping TAP rules, dormant users.
Built and verified against a **testnet** workspace first. The report is the product.

## Hard API constraints (verified — do not design around them)

1. **Vault accounts cannot be deleted.** Only `hide` / `unhide`. Never write code, comments, config keys or
   report text implying vaults are deleted.
2. **Hiding a vault also hides its transaction history** in the console. Reports must say so.
3. **No last-login field exists.** Activity is derived from `GET /management/audit_logs` (1-year cap). `state.py`
   keeps a last-seen index so it outlives the API window. Absence of audit events is weak evidence — users who
   only view the console may emit none.
4. **Console user deletion is async and quorum-gated** (`DELETE /management/users/{id}`, beta). `OWNER_ONLY (2760)`
   → Settings > Quorums. `USER_REFERENCED_IN_TAP` → clean the TAP reference first. `USER_PENDING_ONBOARDING` →
   revoke the invite in the console. Completion = a later poll shows `enabled: false`. `force` is an explicit
   CLI flag (`--force-user-delete`), default off, **never suggested in error text**.
5. **Never publish policy.** `OPS` has no publish op; do not add one, and do not add a flag for it. A human publishes.
   The single exception, authorised by the owner for the AMER TAP rework: `update_policy_draft` (SDK `update_draft`)
   may overwrite the policy DRAFT, only from `scripts/write_tap_draft.py`, dry-run by default, behind the prod gate,
   and only when the current draft is empty, identical to the active policy, or already this proposal
   (`tap_draft.precheck`). A write clobbers a shared draft a teammate may have open, so never loosen that pre-check.

## Principles

- "Unused" is not "unneeded". When a call is close, keep the object and flag it.
- Tier 1 (hide/unhide vault): auto, reversible. Tier 2 (delete wallet / connection / tag, request user deletion):
  YAML approval file, `approved: true`, later run with `--apply-approvals`. **Tier 2 never executes in the run that
  proposes it.** Tier 3 (TAP consolidation, vault renames): propose only.
- Approval-file rule: every irreversible deletion goes through the file, whether or not a quorum or TAP also applies
  (so the file covers wallets, connections, tags and console users). Only reversible actions (hide/unhide) or
  propose-only output (TAP, renames) stay outside it. Do not exempt an op from the file because it looks ungated.
  The quorum on user deletion is a second gate, not a substitute: the file decides whether the agent asks, carries
  the evidence, and is the only human gate for non-quorum deletions (README: "Why Tier 2 needs an approval file").
- Quarantine: nothing acts on first sighting. Needs `min_consecutive_runs` (floor 2) AND `min_days_flagged`.
  Released entities restart from zero. Acted entities never re-enter.
- Everything is dry-run unless `--execute`. `is_production: true` additionally needs `--i-understand-this-is-prod`;
  no config key may bypass that.
- `base_path` is NOT a production signal: testnet workspaces use `us`/`eu`/`eu2` too.

- Fund movement exists only in `sweep.py` / `sweep_value.py` and `scripts/sweep.py` / `scripts/sweep_small.py`
  (`create_transaction`, plus `create_vault_asset` for destination wallets). Cleanup code (`run_cleanup.py`,
  `actions.py`) must never call them. Sweeps require `--limit`/`--all` to execute and stop at the first
  non-COMPLETED transfer.
- **Testnet assets are NOT worth $0.** The console reports `amountUSD` on testnet transactions (ETH_TEST5 ~ ETH). Value
  caps must price testnet and mainnet alike, and an unpriced asset leaves its vault alone. Treating testnet as $0
  once moved vaults worth thousands of dollars.

## Architecture (keep this shape)

`collect.py` read-only → `rules.py` pure classification → `actions.py` only mutator → `tap.py` + `tap_consolidate.py` pure policy analysis
→ `state.py` SQLite → `client.py` SDK wrapper (single `call()` choke point: dry-run, rate limit, JSONL journal)
→ `report.py` markdown. `config.py` loads/validates YAML. Never replace an SDK call with hand-rolled HTTP.
The `OPS` table in `client.py` is verified against the installed SDK by `scripts/probe.py --offline`.

## Tests

`pytest` — offline, no SDK calls, no credentials. `test_tap.py`, `test_tap_consolidate.py` and `test_quarantine.py` guard the places a bug
does real damage. Run them before touching `tap.py`, `tap_consolidate.py` or `state.py`.

## Credentials

`FIREBLOCKS_API_KEY`, `FIREBLOCKS_SECRET_PATH` (path to key file) via env or `config/.env` (gitignored).
Never commit keys, key paths with real values, user emails, or `state/ reports/ approvals/` contents — this repo is public.
