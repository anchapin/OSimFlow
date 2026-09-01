"""Project-root exception hierarchy for OSimFlow (issue #1484).

The package previously defined ~40 exception classes with inconsistent
bases chosen ad hoc: some inherited :class:`RuntimeError`, some
:class:`ValueError`, some bare :class:`Exception`, and a few inherited
nothing related to the domain at all. Without a single root, callers
and the REST surface had to enumerate dozens of unrelated classes (or
over-broadly catch ``Exception``), and there was no way to distinguish
"an OSimFlow domain failure" from a programming error anywhere in the
stack.

This module introduces:

* :class:`OSimFlowError` — the single public root. Every module-level
  exception class in ``osimflow/`` (outside ``client.py`` and the
  ``api/`` package, which carry their own transport-shaped hierarchy)
  is a subclass of this root. The contract is pinned by
  ``tests/unit/test_exception_hierarchy.py``.
* :class:`OSimFlowRuntimeError` — intermediate layer that keeps every
  exception that was historically a ``RuntimeError`` catchable by
  ``except RuntimeError``. Preserves the existing contract surface so
  callers do not have to be rewritten in lockstep.
* :class:`OSimFlowValueError` — the ``ValueError`` analogue. Same idea:
  pre-existing ``except ValueError:`` clauses keep working.

The two intermediate classes use multiple inheritance. Because
``OSimFlowError`` only inherits from :class:`Exception` and
``RuntimeError`` / ``ValueError`` are siblings of ``Exception``, the
C3 linearization is unambiguous and the MRO is::

    OSimFlowRuntimeError.__mro__ == (
        OSimFlowRuntimeError, OSimFlowError, RuntimeError,
        Exception, BaseException, object,
    )

so ``isinstance(e, OSimFlowError)``, ``isinstance(e, RuntimeError)``,
and ``isinstance(e, Exception)`` all hold for any subclass.
"""

from __future__ import annotations

__all__ = [
    "OSimFlowError",
    "OSimFlowRuntimeError",
    "OSimFlowValueError",
]


class OSimFlowError(Exception):
    """Root of the OSimFlow package exception hierarchy.

    Catch this to handle any domain failure raised by OSimFlow,
    regardless of whether it semantically classifies as a runtime
    failure (e.g. :class:`~osimflow.campaign.CampaignError`) or a
    validation failure (e.g. :class:`~osimflow.measures.MeasureRegistryError`).

    Do **not** use this as a base for new ``Exception`` subclasses
    outside of OSimFlow domain logic; programming errors and stdlib
    failures should continue to raise the appropriate builtin
    exception.
    """


class OSimFlowRuntimeError(OSimFlowError, RuntimeError):
    """Intermediate base for OSimFlow exceptions that were historically ``RuntimeError``.

    The multiple-inheritance keeps ``except RuntimeError:`` working for
    every subclass (so existing call sites that match on the legacy
    base keep matching), while also making every subclass catchable as
    ``OSimFlowError``.
    """


class OSimFlowValueError(OSimFlowError, ValueError):
    """Intermediate base for OSimFlow exceptions that were historically ``ValueError``.

    The multiple-inheritance keeps ``except ValueError:`` working for
    every subclass (so existing call sites that match on the legacy
    base keep matching), while also making every subclass catchable as
    ``OSimFlowError``.
    """
