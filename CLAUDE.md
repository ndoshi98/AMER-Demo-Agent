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
5. **Never publish policy.** No policy write op exists in `OPS`; do not add one, and do not add a flag for it.
   `PUT/POST /policy/draft` clobbers a shared draft a teammate may have open. A human publishes.

## Principles

- "Unused" is not "unneeded". When a call is close, keep the object and flag it.
- Tier 1 (hide/unhide vault): auto, reversible. Tier 2 (delete wallet / connection / tag, request user deletion):
  YAML approval file, `approved: true`, later run with `--apply-approvals`. **Tier 2 never executes in the run that
  proposes it.** Tier 3 (TAP consolidation, vault renames): propose only.
- Approval-file rule: every irreversible deletion goes through the file, whether or not a quorum or TAP also applies
  (so the file covers wallets, connections, tags and console users). Only reversible actions (hide/unhide) or
  propose-only output (TAP, renames) stay outside it. Do not exempt an op from the file because it looks ungated.
- Quarantine: nothing acts on first sighting. Needs `min_consecutive_runs` (floor 2) AND `min_days_flagged`.
  Released entities restart from zero. Acted entities never re-enter.
- Everything is dry-run unless `--execute`. `is_production: true` additionally needs `--i-understand-this-is-prod`;
  no config key may bypass that.
- `base_path` is NOT a production signal: testnet workspaces use `us`/`eu`/`eu2` too.

## Architecture (keep this shape)

`collect.py` read-only → `rules.py` pure classification → `actions.py` only mutator → `tap.py` pure policy analysis
→ `state.py` SQLite → `client.py` SDK wrapper (single `call()` choke point: dry-run, rate limit, JSONL journal)
→ `report.py` markdown. `config.py` loads/validates YAML. Never replace an SDK call with hand-rolled HTTP.
The `OPS` table in `client.py` is verified against the installed SDK by `scripts/probe.py --offline`.

## Tests

`pytest` — offline, no SDK calls, no credentials. `test_tap.py` and `test_quarantine.py` guard the two places a bug
does real damage. Run them before touching `tap.py` or `state.py`.

## Credentials

`FIREBLOCKS_API_KEY`, `FIREBLOCKS_SECRET_PATH` (path to key file) via env or `config/.env` (gitignored).
Never commit keys, key paths with real values, user emails, or `state/ reports/ approvals/` contents — this repo is public.
