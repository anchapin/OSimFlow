# OSimFlow — GitHub Copilot Instructions

Auto-discovered by GitHub Copilot. **AGENTS.md is the canonical source** for
project conventions, architecture, and task routing.

## Project

CLI + library wrapping the OpenStudio CLI for parametric building-energy
simulations. **NOT a web service** — no HTTP routes, no ORM, no auth layer.

## Conventions

- Python 3.12+. Type hints on public functions (mypy --strict).
- `pathlib.Path` over `os.path`. `logging` over `print`.
- Exceptions: catch, log with `exc_info=True`, re-raise.
- Never commit `.osm`, `.osw`, `.idf`, `.epw`, `eplusout.*` files.

## Key Paths

- `osimflow/campaign.py` — Campaign orchestrator (6-step DAG)
- `osimflow/executors/__init__.py` — Local, Slurm, AWS Batch, Nomad executors
- `osimflow/work.py` — Per-step work functions
- `bin/*.py` — CLI scripts called by the work layer
- `osimflow/__main__.py` — CLI entry point (`osimflow run ...`)

## Test Commands

Always via `.venv/bin/pytest` or `make test`, never bare `pytest`:
`make test` | `make test-fast` | `make lint` | `make typecheck`
