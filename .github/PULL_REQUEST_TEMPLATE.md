<!--
Thanks for contributing to OSimFlow!

This template mirrors docs/CONTRIBUTING.md §5 "Pull request process".
Sections below MUST be filled in. The "Before opening" checkboxes
are copied verbatim from CONTRIBUTING.md; tick them as you complete
each one. If a docs change is intentionally deferred, write
`// docs: skip — <reason>` so reviewers can confirm.
-->

## Summary

<!-- 1-3 sentences. What does this PR do, and why? -->

## Issue

<!-- `Closes #N` (one issue per PR; split if needed). -->

Closes #

## Test plan

<!-- What you ran, what you observed. Cite command + observed result,
e.g. `.venv/bin/pytest tests/unit/test_foo.py -v` → 12 passed. -->

## Risk

<!-- Any behavior changes, any backward-incompatible surface
(public API, executor contract, CLI flags, cache keys). If none, write
"None." -->

## Checklist

<!-- Verbatim from docs/CONTRIBUTING.md §5 "Before opening". Tick each
item as you complete it. The CI equivalents:
  - make precommit       → lint, format --check, typecheck, contract,
                           docs-sync, gitleaks (pre-commit)
  - make test            → full pytest suite (contract + unit + integration)
  - make test-cov        → the same suite + 82% coverage gate
  - AGENTS.md update     → enforced by tools/check_agents_contract.py
  - docs update          → enforced by tools/check_docs_sync.py
-->

- [ ] All checks green locally: `make precommit` (or `act` for the CI
      mirror if you have it installed).
- [ ] Full test suite green: `make test`.
- [ ] Coverage gate (82%) still passes: `make test-cov`.
- [ ] If you added a new public symbol to `osimflow/__init__.py`, a new
      `bin/*.py` script, a new file in `osimflow/executors/`, a new
      campaign step, or a new CLI flag, **AGENTS.md is updated in the
      same commit** (the contract check enforces this in CI).
- [ ] If you renamed, removed, or otherwise invalidated a docs
      reference, **the docs are updated in the same commit** (the docs
      sync check enforces this in CI).

<!-- Docs deferral convention (CONTRIBUTING.md §5 "PR template"):
If a docs change is intentionally deferred, write
  // docs: skip — <reason>
in the relevant section above so reviewers can confirm.
-->
