import pytest


@pytest.fixture(autouse=True)
def _task_payload_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    # AWSBatchExecutor requires a locally held signing secret (issues #1633/#2025).
    monkeypatch.setenv("OSIMFLOW_TASK_PAYLOAD_SECRET", "test-hmac-secret")
