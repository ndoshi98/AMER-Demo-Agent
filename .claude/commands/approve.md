---
description: Show pending Tier 2 approval items and dry-run applying approved ones
---
List the items in `approvals/approvals-*.yaml` with their status and age. Do NOT edit `approved:` flags. Then run
`.venv/bin/python scripts/run_cleanup.py --apply-approvals` (dry run) and report what would execute and what would be
skipped or expired. Only add `--execute` on explicit user instruction.
