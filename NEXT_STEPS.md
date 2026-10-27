# Where we left off

## State
- Everything is dry run by default; nothing executes without `--execute` (plus `--i-understand-this-is-prod` on a
  production config). Run `.venv/bin/python -m pytest -q` first (offline, no credentials).
- Built and verified against a testnet workspace, then used against a production demo workspace: TAP naming and cleanup
  across all 14 policy types (drafts written by `write_tap_draft.py`, published by a human), fund sweeps, hide runs, an
  idle-vault transaction scan, live pricing, and a read-only AML-screening investigation. See the README sections on live
  prices, the hide-only run, specialised sweeps, unfreeze, and API behaviours.
- Local-only, gitignored: `config/.env*`, `config/cleanup.*.yaml`, `reports/`, `state/`, `approvals/`.

## Open items
1. **Vault restructure.** A rename/group proposal (`Group - Role` names, tiers: core, optional, retire) exists as a local
   report. Vault renames are not built into the tooling yet; they would be propose-only with a journal and an undo, like
   the TAP renames.
2. **AML demo rules.** An outgoing pre-screening rule whose destination is a vault can never match (vault-to-vault is not
   screened); a post-screening rule's name should match its verdict. Both are draft-and-human-publish changes.
3. **Unfreeze.** Blocked in the demo workspace by the `disableUnfreeze` limit; needs the owner or a Support ticket.
4. **Canton sweeps.** `sweep_canton.py` failed with `INSUFFICIENT_FUNDS` on balances the vault reported as available;
   needs a Fireblocks answer before it is trusted.
5. **Unverified:** console valuation of deprecated testnet coins (the console uses its own, older rate), and whether the
   console's vault list header count (batched loading) ever matches the API count.
6. **Not built:** Tier 1 tag add/remove, `create_tag`, vault renames, non-TRANSFER policy analysis beyond naming.

## Next, in order
1. Decide the vault structure tier cut, then build the rename step (dry run, journal, undo).
2. Draft the AML rule fixes for human publish.
3. Retry the unfreeze once the workspace limit is lifted.
