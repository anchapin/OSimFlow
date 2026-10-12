"""Unit tests for ``Campaign._inject_dp_overrides`` (issue #1909 coverage floor)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from osimflow.campaign import Campaign


class _StubManager:
    def __init__(self, points: dict[str, Any]) -> None:
        self._points = points

    def get(self, sample_id: str) -> Any:
        return self._points.get(sample_id)


def _campaign(manager: _StubManager | None) -> Campaign:
    camp = object.__new__(Campaign)
    camp._dp_manager = manager  # type: ignore[assignment]
    return camp


def test_no_manager_returns_samples_unchanged() -> None:
    samples: list[Any] = [{"sample_id": "s0"}]
    assert _campaign(None)._inject_dp_overrides(samples) is samples


def test_overrides_injected_per_sample() -> None:
    manager = _StubManager(
        {
            "s0": SimpleNamespace(seed_model="m.osm", weather_file="w.epw"),
            "s1": SimpleNamespace(seed_model=None, weather_file=None),
        }
    )
    samples: list[Any] = [{"sample_id": "s0"}, {"sample_id": "s1"}, {"sample_id": "s2"}]
    out = _campaign(manager)._inject_dp_overrides(samples)
    assert out[0]["seed_model"] == "m.osm"
    assert out[0]["weather_file"] == "w.epw"
    assert out[1] == {"sample_id": "s1"}
    assert out[2] == {"sample_id": "s2"}
    assert "seed_model" not in samples[0]
