# Where we left off (2026-10-20)

## State
- Shell, README, skill/commands and 78 offline tests are done and pushed. Run `.venv/bin/python -m pytest -q` first.
- `scripts/probe.py --online` has been run against the testnet workspace (read-only). Results are in the README under
  "Settled by probe.py --online". Headline: the key can do every read; the audit API takes epoch **milliseconds**
  (fixed); hidden vaults are **not** returned by the vault listing.
- **Nothing has been executed.** No audit run, no `--execute`, no approval file, no state DB exists yet.
- Local-only setup that is not in the repo: `config/.env` (gitignored; needs `FIREBLOCKS_API_KEY` and
  `FIREBLOCKS_SECRET_PATH`) and the key file at mode 600. On a new machine, recreate from `config/.env.example`.

## Decisions made
- Every irreversible deletion (wallets, connections, tags, console users) goes through the approval file, whether or
  not a quorum also applies. Only hide/unhide (Tier 1) and propose-only output (TAP, renames) stay outside it.
- `audit.activity_events` stays empty (any audit event counts as activity) because that flags fewer users.
- `workspace.expected_name` is set to the testnet workspace name. Change it when pointing at another workspace.

## Next, in order
1. **Start the quarantine clock early.** Quarantine needs 2 runs AND 14 days, so nothing can be hidden until two
   observing audits are 14+ days apart. Do a first dry-run audit (`scripts/run_cleanup.py`, no `--execute`) soon.
   Use `--no-observe` first if you want to read the report without advancing any clocks.
2. Read the first report. About 31 of 60 vaults have all-zero totals (5 have no assets), so check the protect lists
   (`keep-for-demo` tag, `name_patterns`) before the clock starts counting them.
3. **Hidden vaults gap:** find out whether the SDK's vault listing has a parameter to include hidden vaults. If not,
   tag usage and "already hidden" detection stay blind to them (details in the README).
4. Still unverified: write permission (first `--execute`), the transaction time-walk, network-connection and wallet
   list shapes, tag pagination (`next` cursor not implemented), and what deleting wallets or connections does.
5. Try Tier 2 on a few throwaway objects in testnet. This also shows which deletes are quorum-gated.
6. Only then consider the real demo workspace: new key, `expected_name`, `is_production`, and a fresh read-only probe.

## Not built
Tier 1 tag add/remove, `create_tag`, vault renames beyond duplicate-name suggestions, and non-TRANSFER policy types.
