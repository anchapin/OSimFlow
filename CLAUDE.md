# OSimFlow — Claude Code Instructions

Auto-discovered by Claude Code. **AGENTS.md is the canonical source** for
project conventions, architecture, and task routing. Read it before proposing
or writing code.

## Project Type

CLI + library hybrid wrapping the OpenStudio CLI for parametric building-energy
simulation campaigns. Runs locally or on HPC/cloud. **NOT a web service** — no
HTTP routes, no ORM models, no authentication layer.

Architecture: Orchestrator (`osimflow/campaign.py`) → Executor (`osimflow/executors/`) → Work function (`osimflow/work.py` + `bin/*.py`).

## Key Docs

- `AGENTS.md` — canonical conventions and task routing (§9)
- `docs/OSimFlow.md` — Product Requirements Document
- `.agents/results/decision-verdict.md` — architecture decision rationale

## Commands

```bash
make install    # pip install -e ".[dev,aws,slurm]"
make test       # full pytest suite (via .venv)
make lint       # ruff check
make typecheck  # mypy --strict osimflow/
make contract   # AGENTS.md + docs sync checks
make precommit  # the pre-push safety net
```
