"""Patch helper: the executor builds clients via ``boto3.Session(...).client``."""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import MagicMock, patch


@contextmanager
def patch_boto3_client(fake_client: Any) -> Iterator[MagicMock]:
    """Patch boto3 so every client resolves to ``fake_client``.

    Yields the mock that records ``client(service, **kwargs)`` calls.
    """
    with patch("boto3.Session") as session_cls, patch("boto3.client") as plain:
        session_cls.return_value.client = plain
        plain.return_value = fake_client
        yield plain
