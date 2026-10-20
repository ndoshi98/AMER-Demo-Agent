---
name: console-cleanup
description: Operate the Fireblocks demo-console cleanup agent (audit, sweep, approve, TAP review). Use when asked to clean up the demo workspace, hide empty vaults, review TAP overlap, or process Tier 2 approvals.
---

# Console cleanup agent

Read `CLAUDE.md` first. Its constraints are binding.

## Rules for operating it

- Always start with a dry run (no `--execute`). Read the report before suggesting `--execute`.
- Never pass `--execute`, `--force-user-delete` or `--i-understand-this-is-prod` unless the user explicitly asks in
  this conversation.
- Never edit `approved:` in an approval file on the user's behalf. A human approves each item.
- Never add a policy write/publish operation. TAP output is a proposal JSON a human applies.
- Use `--no-observe` for previews so quarantine clocks don't advance.
- Say plainly that hiding a vault also hides its transaction history in the console.
- Never print credentials, key paths with real values, or user emails in anything that might be committed.

## Workflow

1. `scripts/probe.py --offline`, then `--online` if credentials are configured.
2. `scripts/run_cleanup.py` (audit) and open the newest file in `reports/`.
3. Summarise section 1 (needs your decision) first, then what changed automatically.
4. For Tier 2, point the user to `approvals/approvals-*.yaml`. Applying happens in a later run with
   `--apply-approvals`.
