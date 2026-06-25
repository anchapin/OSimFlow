"""Skip-gated tests for the *real* OpenStudio example fixture (issue #938).

These tests exercise the real, simulation-capable ``example_package/`` fixture
that ``scripts/fetch_example_fixture.py`` materializes at dev/test time:

* a real ``.osm`` model containing ``OS:Version``, and
* a real ``.epw`` weather file containing ``LOCATION``.

The fetched ``.osm`` / ``.epw`` files are **gitignored** (``AGENTS.md`` §10),
so they are absent in CI. The whole module is therefore skipped unless a real
fixture is present on disk. To run these locally::

    python scripts/fetch_example_fixture.py
    .venv/bin/pytest tests/integration/test_real_example_fixture.py -v

In normal CI the module reports as skipped (``s``), never as an error.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_PACKAGE = REPO_ROOT / "example_package"
MODEL_OSM = EXAMPLE_PACKAGE / "model.osm"
WEATHER_EPW = EXAMPLE_PACKAGE / "USA_CO_Golden-NREL.724666_TMY3.epw"


def _file_contains(path: Path, marker: str) -> bool:
    if not path.is_file() or path.stat().st_size == 0:
        return False
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for chunk in iter(lambda: fh.read(65536), ""):
                if marker in chunk:
                    return True
    except OSError:
        return False
    return False


def _real_fixture_present() -> bool:
    """True iff a real (non-placeholder) model + weather file are on disk."""
    return _file_contains(MODEL_OSM, "OS:Version") and _file_contains(WEATHER_EPW, "LOCATION")


pytestmark = pytest.mark.skipif(
    not _real_fixture_present(),
    reason="real example fixture not fetched; run scripts/fetch_example_fixture.py",
)


def test_real_model_is_openstudio_osm() -> None:
    """The fetched model must be a real OpenStudio OSM (not the JSON stub)."""
    assert MODEL_OSM.is_file()
    text = MODEL_OSM.read_text(encoding="utf-8", errors="replace")
    assert "OS:Version" in text
    # The committed placeholder is a JSON object; the real model is not.
    assert not text.lstrip().startswith("{"), "model.osm is still the JSON placeholder"
    # A real SmallOffice seed must have thermal zones + a building.
    assert "OS:ThermalZone" in text
    assert "OS:Building" in text


def test_real_weather_is_valid_epw() -> None:
    """The fetched weather file must be a real EPW with a LOCATION header."""
    assert WEATHER_EPW.is_file()
    assert WEATHER_EPW.stat().st_size > 0
    first_line = WEATHER_EPW.read_text(encoding="utf-8", errors="replace").splitlines()[0]
    assert first_line.startswith("LOCATION,")


def test_workflow_references_seed_model() -> None:
    """``workflow.osw`` must still seed from ``model.osm`` for a real run."""
    import json

    osw_path = EXAMPLE_PACKAGE / "workflow.osw"
    osw = json.loads(osw_path.read_text(encoding="utf-8"))
    assert osw["seed_file"] == "model.osm"


def test_placeholder_is_preserved() -> None:
    """The JSON stub must remain restorable for stub-mode tests."""
    placeholder = EXAMPLE_PACKAGE / "model.osm.placeholder"
    assert placeholder.is_file(), (
        "model.osm.placeholder missing — stub-mode tests cannot be restored"
    )
    text = placeholder.read_text(encoding="utf-8", errors="replace")
    assert text.lstrip().startswith("{"), "placeholder should be the JSON stub"
