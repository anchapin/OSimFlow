# OSimFlow — Claude Code Instructions

**AGENTS.md is the canonical source** for project conventions, architecture,
commands, and task routing. Read it before proposing or writing code.
This file is intentionally a thin pointer (issue #1893).

OSimFlow is a CLI + library hybrid, NOT a web service. Never run bare
`pytest`; use `make test` / `.venv/bin/pytest`.
