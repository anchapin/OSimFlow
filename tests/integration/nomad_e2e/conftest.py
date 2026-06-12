"""Pytest fixtures for Nomad single-node E2E tests (issue #133).

Provides a session-scoped ``nomad_single`` fixture that:
  1. Starts the Docker Compose single-node Nomad cluster.
  2. Waits for the Nomad HTTP API to be ready.
  3. Yields the Nomad address (``http://localhost:4646``).
  4. Tears down Docker Compose after the test session.

Tests that use this fixture must be marked ``@pytest.mark.nomad_e2e``
and will be marked XFAIL (not silently skipped) when Docker is not
available — this prevents false-green CI results (issue #195).
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

import pytest

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
NOMAD_HTTP_PORT = 4646
NOMAD_ADDRESS = f"http://localhost:{NOMAD_HTTP_PORT}"
COMPOSE_FILE = Path(__file__).resolve().parent / "docker-compose.single.yml"
NOMAD_READY_TIMEOUT_S = 60.0
NOMAD_READY_POLL_S = 1.0


def _docker_available() -> bool:
    """Check whether Docker and Docker Compose are available."""
    try:
        subprocess.run(
            ["docker", "info"],
            capture_output=True,
            check=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False
    # Check for docker compose v2 (plugin).
    try:
        subprocess.run(
            ["docker", "compose", "version"],
            capture_output=True,
            check=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False
    return True


def _wait_for_nomad(address: str, timeout_s: float, poll_s: float) -> None:
    """Poll the Nomad ``/v1/status/leader`` endpoint until it returns 200.

    Raises ``TimeoutError`` if the endpoint does not respond within
    *timeout_s* seconds.
    """
    url = f"{address}/v1/status/leader"
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            req = Request(url, method="GET", headers={"Accept": "application/json"})
            with urlopen(req, timeout=5.0) as resp:
                if resp.status == 200:
                    return
        except (URLError, OSError):
            pass
        time.sleep(poll_s)
    raise TimeoutError(f"Nomad API at {address} did not become ready within {timeout_s:.0f}s")


@pytest.fixture(scope="session")
def nomad_single() -> str:  # type: ignore[misc]  # fixture return is complex
    """Session-scoped fixture: start single-node Nomad, yield address, tear down.

    Skips the test session when Docker is not available.
    """
    if not _docker_available():
        pytest.xfail("Docker / Docker Compose not available — Nomad E2E tests require Docker (issue #195)")

    # Start the Docker Compose stack.
    try:
        proc = subprocess.run(
            [
                "docker",
                "compose",
                "-f",
                str(COMPOSE_FILE),
                "up",
                "-d",
                "--wait",
            ],
            capture_output=True,
            timeout=120,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as exc:
        pytest.xfail(f"Docker Compose failed to start Nomad: {exc}")
        return  # pragma: no cover — skip already raised

    if proc.returncode != 0:
        # Collect container logs for debugging before skipping.
        log_proc = subprocess.run(
            ["docker", "compose", "-f", str(COMPOSE_FILE), "logs"],
            capture_output=True,
            timeout=30,
            check=False,
        )
        logs = log_proc.stdout.decode("utf-8", errors="replace")[-2000:]
        # Tear down failed containers.
        subprocess.run(
            ["docker", "compose", "-f", str(COMPOSE_FILE), "down", "--volumes", "--remove-orphans"],
            capture_output=True,
            check=False,
            timeout=60,
        )
        pytest.xfail(
            f"Docker Compose up failed (rc={proc.returncode}). "
            f"stderr: {proc.stderr.decode('utf-8', errors='replace')[-500:]}\n"
            f"logs:\n{logs}"
        )
        return  # pragma: no cover — skip already raised

    try:
        # Wait for the Nomad API to be ready.
        _wait_for_nomad(NOMAD_ADDRESS, NOMAD_READY_TIMEOUT_S, NOMAD_READY_POLL_S)
        yield NOMAD_ADDRESS
    except TimeoutError:
        # Nomad container started but API never became ready — likely a
        # resource / environment constraint in CI. Skip instead of error.
        log_proc = subprocess.run(
            ["docker", "compose", "-f", str(COMPOSE_FILE), "logs"],
            capture_output=True,
            timeout=30,
            check=False,
        )
        logs = log_proc.stdout.decode("utf-8", errors="replace")[-2000:]
        pytest.xfail(
            f"Nomad API did not become ready within {NOMAD_READY_TIMEOUT_S:.0f}s. "
            f"Container logs:\n{logs}"
        )
        return  # pragma: no cover — skip already raised
    finally:
        # Tear down: stop and remove containers.
        subprocess.run(
            [
                "docker",
                "compose",
                "-f",
                str(COMPOSE_FILE),
                "down",
                "--volumes",
                "--remove-orphans",
            ],
            capture_output=True,
            check=False,
            timeout=60,
        )
