"""``osimflow status --from-s3`` reports succeeded/failed counts and terminal state (issue #2081)."""

import argparse
import json
from pathlib import Path
from typing import Any

import pytest

from osimflow.cli.status import _cmd_status


class _FakeStore:
    def __init__(self, status: dict[str, Any]) -> None:
        self._status = status

    def status(self, cid: str) -> dict[str, Any]:
        return {"campaign_id": cid, **self._status}


@pytest.mark.parametrize(
    ("succeeded", "failed", "state"),
    [
        (2, [], "completed"),
        (1, ["0001"], "completed_with_failures"),
        (0, ["0000", "0001"], "failed"),
    ],
)
def test_status_from_s3_outputs_counts_and_state(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    succeeded: int,
    failed: list[str],
    state: str,
) -> None:
    payload = {
        "total": 2,
        "completed": succeeded + len(failed),
        "succeeded": succeeded,
        "failed": failed,
        "failed_count": len(failed),
        "pending": [],
        "state": state,
    }
    monkeypatch.setattr("osimflow.cli.s3._s3_store", lambda args, scratch: _FakeStore(payload))
    args = argparse.Namespace(from_s3=True, outdir=tmp_path / "camp1")
    assert _cmd_status(args) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["state"] == state
    assert out["succeeded"] == succeeded
    assert out["failed_count"] == len(failed)
    assert out["completed"] == 2
