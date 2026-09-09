"""Reload-stable state for the algorithm registry.

This module intentionally contains only registry state.  Keeping the dict in a
cached leaf module prevents re-executing ``osimflow.algorithms`` from dropping
registrations made by built-ins, plug-ins, or callers.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from osimflow.algorithms import BaseAlgorithm

# Anchored outside the package ``__init__`` so ``importlib.reload`` of the
# package cannot recreate the dict and silently discard algorithm registrations.
_registry: dict[str, type["BaseAlgorithm"]] = {}
