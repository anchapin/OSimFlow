"""Tests for osimflow/api/timeseries.py (issue #1773).

The ``timeseries_router`` (mounted on the FastAPI app via
``app.include_router(timeseries_router)`` in ``osimflow/api/app.py:1867``)
was previously exercised only at module import / route registration
time — the two production user-facing endpoints
(``GET .../samples/{sid}/timeseries`` and
``GET .../samples/{sid}/timeseries/variables``) had no direct coverage.
These tests build a synthetic ``eplusout.sql`` per scenario and assert
the route's behaviour end-to-end:

- (a) ``GET .../timeseries?variable=...&freq=hourly|daily|monthly``
  returns the right shape on a synthetic SQL file
- (b) the variables listing endpoint
- (c) the ``_sim_dir_from_sample`` path-traversal guard rejects
  ``sample_id`` containing ``/``, ``\\``, or ``..``
- (d) the 404 path when ``eplusout.sql`` is missing
- (e) the ``_open_sql`` 500/connection-failure surface
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="osimflow[api] extra required")
pytest.importorskip("slowapi", reason="osimflow[api] extra required")
from fastapi.testclient import TestClient

from osimflow.api import create_app

# ---------------------------------------------------------------------------
# Synthetic eplusout.sql builder
# ---------------------------------------------------------------------------


def _make_eplusout_sql(path: Path, *, n_hours: int = 6) -> None:
    """Write a synthetic EnergyPlus-style ``eplusout.sql`` to *path*.

    Builds the minimal ``reportmetadata`` / ``reportdata`` / ``timedata``
    schema ``_query_timeseries`` joins against — *not* a real
    EnergyPlus file, just enough to exercise the GROUP BY strftime
    aggregation paths.  Each of *n_hours* ticks reports
    ``"Zone Air Temperature"`` for two keys (``Zone 1``, ``Zone 2``) at
    hourly intervals starting on 2024-01-01 00:00.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        cur = conn.cursor()
        cur.executescript(
            """
            CREATE TABLE reportmetadata (
                VariableName TEXT,
                KeyName TEXT,
                Units TEXT,
                ReportingFrequency TEXT,
                ScheduleName TEXT,
                ConformReportFreq TEXT,
                MinValue REAL,
                MaxValue REAL,
                AvgValue REAL
            );
            CREATE TABLE reportdata (
                TimeIndex INTEGER,
                ReportDataRunIndex INTEGER,
                VariableName TEXT,
                KeyName TEXT,
                Units TEXT,
                Value REAL,
                ReportingFrequency TEXT
            );
            CREATE TABLE timedata (
                TimeIndex INTEGER PRIMARY KEY,
                Month INTEGER,
                Day INTEGER,
                Hour INTEGER,
                Minute INTEGER,
                DST INTEGER
            );
            """
        )
        # Insert metadata (one row per (Variable, Key) — matches the
        # production join in _query_timeseries).
        for key in ("Zone 1", "Zone 2"):
            cur.execute(
                """
                INSERT INTO reportmetadata
                    (VariableName, KeyName, Units, ReportingFrequency,
                     ScheduleName, ConformReportFreq, MinValue, MaxValue, AvgValue)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("Zone Air Temperature", key, "C", "Hourly", "", "", 18.0, 22.0, 20.0),
            )

        # Insert n_hours hourly rows starting at 2024-01-01 00:00.
        # EnergyPlus stores Hour as 1..24 with Hour=1 representing
        # 00:00-01:00 — the query in _query_timeseries already handles
        # this, so we mirror it here.
        for hour in range(n_hours):
            cur.execute(
                "INSERT INTO timedata (TimeIndex, Month, Day, Hour, Minute, DST) "
                "VALUES (?, 1, 1, ?, 0, 0)",
                (hour + 1, hour + 1),  # Hour=1 → 00:00–01:00
            )
            for key_idx, key in enumerate(("Zone 1", "Zone 2")):
                cur.execute(
                    """
                    INSERT INTO reportdata
                        (TimeIndex, ReportDataRunIndex, VariableName, KeyName,
                         Units, Value, ReportingFrequency)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        hour + 1,
                        1,
                        "Zone Air Temperature",
                        key,
                        "C",
                        20.0 + hour * 0.5 + key_idx * 0.1,
                        "Hourly",
                    ),
                )
        conn.commit()
    finally:
        conn.close()


def _setup_campaign(
    base: Path,
    campaign_id: str,
    *,
    sample_id: str = "sample_000",
    with_sql: bool = True,
    n_hours: int = 6,
) -> Path:
    """Create a campaign + sample directory with a synthetic eplusout.sql.

    Returns the campaign directory. When *with_sql* is False the
    ``work/sim/{sample_id}/eplusout.sql`` file is intentionally
    omitted so the route's 404 path can be exercised.
    """
    cdir = base / campaign_id
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / "run.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "campaign_id": campaign_id,
                "started_at": 1000.0,
                "finished_at": 2000.0,
                "config_summary": {"executor": "local", "n_samples": 1},
            }
        )
    )
    sim_dir = cdir / "work" / "sim" / sample_id
    if with_sql:
        _make_eplusout_sql(sim_dir / "eplusout.sql", n_hours=n_hours)
    else:
        sim_dir.mkdir(parents=True, exist_ok=True)
    return cdir


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def campaigns_base(tmp_path: Path) -> Path:
    base = tmp_path / "campaigns"
    base.mkdir()
    _setup_campaign(base, "campaign-001", sample_id="sample_000", n_hours=6)
    _setup_campaign(base, "campaign-002", sample_id="sample_000", n_hours=6)
    return base


@pytest.fixture
def client_ro(campaigns_base: Path) -> TestClient:
    return TestClient(create_app(campaigns_base_dir=campaigns_base))


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------


class TestRouteRegistration:
    """The timeseries routes must be mounted on the FastAPI app."""

    def test_timeseries_route_present(self, client_ro: TestClient) -> None:
        # The timeseries router is mounted via ``app.include_router``,
        # so its routes live on a nested ``APIRouter`` exposed through
        # the ``Mount`` wrapper.  Walk every route recursively to
        # collect the leaf path strings.
        from fastapi.routing import APIRoute

        def _paths(routes: object) -> set[str]:
            paths: set[str] = set()
            for r in getattr(routes, "routes", []) or []:
                if isinstance(r, APIRoute):
                    paths.add(r.path)
                inner = getattr(r, "app", None) or getattr(r, "original_router", None)
                if inner is not None:
                    paths |= _paths(inner)
            return paths

        paths = _paths(client_ro.app)
        assert "/api/v1/campaigns/{campaign_id}/samples/{sample_id}/timeseries" in paths
        assert "/api/v1/campaigns/{campaign_id}/samples/{sample_id}/timeseries/variables" in paths


# ---------------------------------------------------------------------------
# GET .../timeseries — variable + freq
# ---------------------------------------------------------------------------


class TestGetTimeseries:
    """Coverage for ``get_timeseries`` (issue #1773 acceptance (a))."""

    def test_hourly_returns_aggregated_rows(self, client_ro: TestClient) -> None:
        resp = client_ro.get(
            "/api/v1/campaigns/campaign-001/samples/sample_000/timeseries",
            params={"variable": "Zone Air Temperature", "freq": "hourly"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["variable"] == "Zone Air Temperature"
        assert body["frequency"] == "hourly"
        assert body["units"] == "C"
        assert body["n_points"] > 0
        assert isinstance(body["data"], list)
        assert all({"timestamp", "value", "units", "key"} <= set(row) for row in body["data"])

    def test_daily_returns_aggregated_rows(self, client_ro: TestClient) -> None:
        resp = client_ro.get(
            "/api/v1/campaigns/campaign-001/samples/sample_000/timeseries",
            params={"variable": "Zone Air Temperature", "freq": "daily"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["frequency"] == "daily"
        assert body["n_points"] >= 1

    def test_monthly_returns_aggregated_rows(self, client_ro: TestClient) -> None:
        resp = client_ro.get(
            "/api/v1/campaigns/campaign-001/samples/sample_000/timeseries",
            params={"variable": "Zone Air Temperature", "freq": "monthly"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["frequency"] == "monthly"
        assert body["n_points"] >= 1

    def test_invalid_freq_returns_400(self, client_ro: TestClient) -> None:
        resp = client_ro.get(
            "/api/v1/campaigns/campaign-001/samples/sample_000/timeseries",
            params={"variable": "Zone Air Temperature", "freq": "weekly"},
        )
        assert resp.status_code == 400

    def test_unknown_variable_returns_404(self, client_ro: TestClient) -> None:
        resp = client_ro.get(
            "/api/v1/campaigns/campaign-001/samples/sample_000/timeseries",
            params={"variable": "No Such Variable", "freq": "hourly"},
        )
        assert resp.status_code == 404

    def test_missing_eplusout_sql_returns_404(self, client_ro: TestClient, tmp_path: Path) -> None:
        # Build a separate client whose campaign has no eplusout.sql.
        base = tmp_path / "no_sql"
        base.mkdir()
        _setup_campaign(base, "campaign-nosql", with_sql=False)
        client = TestClient(create_app(campaigns_base_dir=base))
        resp = client.get(
            "/api/v1/campaigns/campaign-nosql/samples/sample_000/timeseries",
            params={"variable": "Zone Air Temperature", "freq": "hourly"},
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET .../timeseries/variables — listing
# ---------------------------------------------------------------------------


class TestListVariables:
    """Coverage for ``list_timeseries_variables`` (issue #1773 acceptance (b))."""

    def test_listing_returns_distinct_variable_keys(self, client_ro: TestClient) -> None:
        resp = client_ro.get(
            "/api/v1/campaigns/campaign-001/samples/sample_000/timeseries/variables"
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 2
        keys = {(v["VariableName"], v["KeyName"]) for v in body["variables"]}
        assert ("Zone Air Temperature", "Zone 1") in keys
        assert ("Zone Air Temperature", "Zone 2") in keys

    def test_listing_returns_404_when_sql_missing(
        self, client_ro: TestClient, tmp_path: Path
    ) -> None:
        base = tmp_path / "no_sql_listing"
        base.mkdir()
        _setup_campaign(base, "campaign-nosql-list", with_sql=False)
        client = TestClient(create_app(campaigns_base_dir=base))
        resp = client.get(
            "/api/v1/campaigns/campaign-nosql-list/samples/sample_000/timeseries/variables"
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Path-traversal guard
# ---------------------------------------------------------------------------


class TestSampleIdPathTraversalGuard:
    """``_sim_dir_from_sample`` must reject traversal attempts (issue #1773 (c)).

    Two layers of defence are exercised:

    * The URL itself is normalised by Starlette / TestClient before the
      route resolves — slashes inside the segment (``../etc/passwd``)
      and bare ``..`` collapse to an unrelated path and the route
      simply doesn't match (404).  Direct coverage of the guard itself
      for those inputs lives in :class:`TestSimDirFromSampleHelper`.
    * The ``\\`` percent-encoded backslash (``%5C``) does survive URL
      parsing and reaches the guard, which fires 400.
    """

    @pytest.mark.parametrize(
        "bad_sample_id",
        ["..", "../etc/passwd", "a/b"],
    )
    def test_traversal_in_sample_id_does_not_reach_filesystem(
        self, client_ro: TestClient, bad_sample_id: str
    ) -> None:
        """Slashes in the segment collapse the URL — 404 is the safe answer.

        The route never executes, the request never touches the
        filesystem, and ``_sim_dir_from_sample`` never builds a path
        with traversal semantics.  Direct coverage of the in-handler
        guard for the same inputs is in
        :class:`TestSimDirFromSampleHelper` below.
        """
        resp = client_ro.get(
            f"/api/v1/campaigns/campaign-001/samples/{bad_sample_id}/timeseries",
            params={"variable": "Zone Air Temperature", "freq": "hourly"},
        )
        assert resp.status_code == 404

    def test_rejects_backslash_in_sample_id(self, client_ro: TestClient) -> None:
        resp = client_ro.get(
            "/api/v1/campaigns/campaign-001/samples/foo%5Cbar/timeseries",
            params={"variable": "Zone Air Temperature", "freq": "hourly"},
        )
        # FastAPI decodes %5C → '\' so the guard fires with 400.
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Connection-failure path (issue #1773 (e))
# ---------------------------------------------------------------------------


class TestOpenSqlConnectionFailure:
    """``_open_sql`` propagates the connection-failure surface (issue #1773 (e)).

    The current behaviour: ``_open_sql`` lets ``sqlite3.connect``
    errors propagate unhandled so FastAPI surfaces them as a 500 to
    the client (verified through the TestClient by checking the
    exception is the documented ``OSError`` with the expected
    message — the same shape the route's callers would see via the
    500 surface in production).
    """

    def test_connection_failure_propagates_oserror(
        self,
        client_ro: TestClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Replace ``sqlite3.connect`` with a raising stub.

        We monkeypatch via the module's own ``sqlite3`` import rather
        than the global so we only affect the timeseries route's view
        of the database.  The real ``eplusout.sql`` exists, but the
        replaced connect always raises so the failure surface fires
        before the 404 check.
        """
        import osimflow.api.timeseries as ts_mod

        def _broken_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
            raise OSError("simulated sqlite open failure")

        monkeypatch.setattr(ts_mod.sqlite3, "connect", _broken_connect)
        # Starlette's TestClient raises the underlying exception (it
        # does not convert server-side OSError to 500 by default).
        # We assert on the exception type + message to lock the
        # connection-failure surface contract — a future refactor
        # that wraps it as HTTPException(500) would need a parallel
        # test update.
        with pytest.raises(OSError, match="simulated sqlite open failure"):
            client_ro.get(
                "/api/v1/campaigns/campaign-001/samples/sample_000/timeseries",
                params={"variable": "Zone Air Temperature", "freq": "hourly"},
            )


# ---------------------------------------------------------------------------
# Helpers — direct unit coverage of _sim_dir_from_sample + _query_timeseries
# ---------------------------------------------------------------------------


class TestSimDirFromSampleHelper:
    """Direct unit coverage for the helper (path-traversal guard)."""

    def test_rejects_slash(self, tmp_path: Path) -> None:
        from fastapi import HTTPException

        from osimflow.api.timeseries import _sim_dir_from_sample

        with pytest.raises(HTTPException) as exc_info:
            _sim_dir_from_sample(tmp_path, "a/b")
        assert exc_info.value.status_code == 400

    def test_rejects_double_dot(self, tmp_path: Path) -> None:
        from fastapi import HTTPException

        from osimflow.api.timeseries import _sim_dir_from_sample

        with pytest.raises(HTTPException) as exc_info:
            _sim_dir_from_sample(tmp_path, "..")
        assert exc_info.value.status_code == 400

    def test_rejects_backslash(self, tmp_path: Path) -> None:
        from fastapi import HTTPException

        from osimflow.api.timeseries import _sim_dir_from_sample

        with pytest.raises(HTTPException) as exc_info:
            _sim_dir_from_sample(tmp_path, "a\\b")
        assert exc_info.value.status_code == 400

    def test_accepts_safe_sample_id(self, tmp_path: Path) -> None:
        from osimflow.api.timeseries import _sim_dir_from_sample

        result = _sim_dir_from_sample(tmp_path, "sample_001")
        assert result == tmp_path / "work" / "sim" / "sample_001"
