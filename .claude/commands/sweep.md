---
description: Normal audit run that advances quarantine clocks (dry run unless user says execute)
---
Run `.venv/bin/python scripts/run_cleanup.py` as a dry run and summarise the report. Only add `--execute` if the
user has explicitly asked for it in this conversation, and confirm the workspace name from the run header first.
