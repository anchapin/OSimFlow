"""Contract: every module-level exception class in osimflow/ is an OSimFlowError.

Issue #1484 mandates a single project root for the package exception
hierarchy so callers and the REST surface can match "an OSimFlow domain
failure" without enumerating dozens of unrelated classes (or
over-broadly catching :class:`Exception`).

This test walks the public :mod:`osimflow` namespace and asserts the
invariant: every module-level ``Exception`` subclass outside the
``api/`` package and the typed ``client`` surface (which carry their
own transport-shaped hierarchy) must be a subclass of
:class:`osimflow.errors.OSimFlowError`. Subclasses inherit the root via
the intermediate :class:`OSimFlowRuntimeError` /
:class:`OSimFlowValueError` mixins so existing ``except RuntimeError``
/ ``except ValueError`` clauses keep working unchanged.

Run via::

    .venv/bin/pytest tests/unit/test_exception_hierarchy.py -v
"""

from __future__ import annotations

import importlib
import pkgutil

import pytest

import osimflow
from osimflow import OSimFlowError

# Modules whose exception hierarchy is intentionally outside the
# ``OSimFlowError`` root:
#
# * ``api`` — REST-surface exceptions that are mapped to HTTP responses
#   and intentionally mirror transport-shape classes (``HTTPException``,
#   ``PermissionError``, etc.). They live under ``osimflow.api.*`` and are
#   re-exported by ``osimflow.client`` to callers of the REST API.
# * ``client`` — typed Python client for the REST surface; carries its
#   own ``OSimFlowAPIError`` root.
# * ``_byos_runner_generated`` — generated inline-subprocess runner
#   (issue #1061). The script runs in a sanitised child Python with no
#   ``osimflow`` import available, so it must use stdlib-only
#   exceptions. Regenerated via ``make contract`` / pre-commit.
SKIP_MODULE_SEGMENTS = frozenset({"client", "api", "_byos_runner_generated"})


def _walked_exception_classes() -> list[tuple[str, type[BaseException]]]:
    """Return every module-level exception class under :mod:`osimflow`."""
    found: list[tuple[str, type[BaseException]]] = []
    for module_info in pkgutil.walk_packages(
        osimflow.__path__,
        prefix="osimflow.",
    ):
        parts = module_info.name.split(".")
        if any(part in SKIP_MODULE_SEGMENTS or part.startswith("test_") for part in parts):
            continue
        module = importlib.import_module(module_info.name)
        for attr_name in dir(module):
            attr = getattr(module, attr_name, None)
            # ``BaseException`` covers both ``Exception`` and its
            # non-``Exception`` cousins (``SystemExit``, ``KeyboardInterrupt``
            # etc.) so we catch any subclass that has been re-based — but
            # OSimFlow domain code uses ``Exception`` subclasses, and the
            # filter below excludes the stdlib ``BaseException`` itself.
            if (
                isinstance(attr, type)
                and issubclass(attr, BaseException)
                and attr is not OSimFlowError
                and attr is not BaseException
                # Only enforce the invariant for classes that are
                # actually defined in osimflow/. A module-level binding
                # of a third-party exception (e.g. ``pandas.errors.DatabaseError``
                # aliased for catch-tuple construction in
                # ``aggregate_results.py``) is not "an OSimFlow exception
                # class" — it's a re-exported stdlib/3rd-party class and
                # we have no business re-basing it.
                and (getattr(attr, "__module__", "") or "").startswith("osimflow.")
            ):
                found.append((f"{module_info.name}.{attr_name}", attr))
    return found


@pytest.fixture(scope="module")
def exception_classes() -> list[tuple[str, type[BaseException]]]:
    """Resolve the exception classes once per test module for speed."""
    return _walked_exception_classes()


def test_root_is_public() -> None:
    """``OSimFlowError`` is exported from the package root."""
    assert hasattr(osimflow, "OSimFlowError"), (
        "OSimFlowError must be exported from osimflow.__init__ for callers "
        "to import it via ``from osimflow import OSimFlowError``."
    )
    assert "OSimFlowError" in osimflow.__all__, (
        "OSimFlowError must be listed in osimflow.__all__ so the agents-md "
        "contract checker treats it as a public symbol."
    )


def test_root_subclasses_exception() -> None:
    """``OSimFlowError`` extends :class:`Exception`, not :class:`BaseException`.

    Catching ``BaseException`` would swallow ``SystemExit`` /
    ``KeyboardInterrupt``; the root must stay anchored on ``Exception``
    so the existing ``except Exception:``-style guards keep working.
    """
    assert issubclass(OSimFlowError, Exception)
    assert not issubclass(OSimFlowError, type(BaseException)) or issubclass(
        OSimFlowError, Exception
    )


def test_every_module_exception_is_osimflow_error(
    exception_classes: list[tuple[str, type[BaseException]]],
) -> None:
    """Every module-level exception under osimflow/ descends from OSimFlowError.

    This is the binding invariant from issue #1484's acceptance
    criterion. If this test fails, a new module-level exception was
    added without rebasing onto :class:`OSimFlowError` (or onto one of
    the intermediate mixins ``OSimFlowRuntimeError`` /
    ``OSimFlowValueError``).
    """
    # Sanity: the namespace must contain exceptions; otherwise the test
    # would silently pass on an empty tree and miss new violations.
    assert exception_classes, (
        "no module-level exceptions discovered under osimflow/ — the test "
        "fixture is broken (check SKIP_MODULE_SEGMENTS / walk_packages)."
    )

    violations = [
        (qualified_name, exc_cls)
        for qualified_name, exc_cls in exception_classes
        if not issubclass(exc_cls, OSimFlowError)
    ]
    assert not violations, (
        "the following exception classes are not OSimFlowError subclasses "
        "(issue #1484 invariant violated):\n"
        + "\n".join(f"  - {qn}: {cls.__mro__!r}" for qn, cls in violations)
    )


def test_runtime_mixin_keeps_runtime_error_compatible() -> None:
    """OSimFlowRuntimeError subclasses remain catchable as ``RuntimeError``.

    Migration must not break existing ``except RuntimeError:`` clauses
    that match OSimFlow domain failures.
    """
    from osimflow.errors import OSimFlowRuntimeError

    assert issubclass(OSimFlowRuntimeError, RuntimeError)
    assert issubclass(OSimFlowRuntimeError, OSimFlowError)

    class _Sample(OSimFlowRuntimeError):
        pass

    with pytest.raises(RuntimeError):
        raise _Sample("boom")


def test_value_mixin_keeps_value_error_compatible() -> None:
    """OSimFlowValueError subclasses remain catchable as ``ValueError``.

    Migration must not break existing ``except ValueError:`` clauses
    that match OSimFlow validation failures.
    """
    from osimflow.errors import OSimFlowValueError

    assert issubclass(OSimFlowValueError, ValueError)
    assert issubclass(OSimFlowValueError, OSimFlowError)

    class _Sample(OSimFlowValueError):
        pass

    with pytest.raises(ValueError):
        raise _Sample("boom")
