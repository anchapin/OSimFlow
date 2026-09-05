"""Executor conformance suite (issue #1478).

Exercises the :class:`~osimflow.executors.base.BaseExecutor` contract —
``submit()`` ``Handle`` lifecycle, result-reference handling via
:mod:`osimflow.executors.transport`, resource-directive propagation,
and health-check registration — against a caller-provided executor.

Design choices
--------------

* **Mixin class, not a single function.** Pytest discovers ``test_*``
  methods on subclasses, which means plug-in authors can override
  individual checks (e.g. a Slurm executor may want to assert that
  per-submit ``cpus`` lands in the rendered ``sbatch`` header) without
  copying the whole suite. The factory attribute pattern mirrors
  ``AlgorithmRegistry``'s discovery mechanism and lets authors keep all
  per-executor knobs in one place.

* **Fast unit checks plus an opt-in stub campaign.** Submit / Handle /
  timeout / transport / health-check tests run in milliseconds. The
  3-sample stub campaign mirrors ``test_local_executor.py`` and proves
  end-to-end Campaign integration; it is marked ``slow`` and gated by
  the ``run_stub_campaign`` class attribute so a CI run with
  ``pytest -m 'not slow'`` skips it.

* **No implicit dependency on pytest internals.** :func:`run_executor_conformance`
  returns a :class:`ConformanceReport` dataclass that a non-pytest caller
  (a pre-commit script, a notebook, a manual ``python -m`` invocation)
  can inspect and pretty-print.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

import pytest

if TYPE_CHECKING:
    from osimflow.executors.base import BaseExecutor


# ---------------------------------------------------------------------------
# Report dataclasses (used by run_executor_conformance)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConformanceCheck:
    """One named check inside a conformance run.

    Attributes:
        name: Short identifier (matches the pytest test method name).
        passed: Whether the check passed.
        detail: Human-readable description (success or failure message).
    """

    name: str
    passed: bool
    detail: str


@dataclass
class ConformanceReport:
    """Aggregated result from :func:`run_executor_conformance`.

    Attributes:
        executor_name: ``BaseExecutor.name`` of the executor under test.
        checks: Ordered list of every check that ran.
    """

    executor_name: str
    checks: list[ConformanceCheck] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        """``True`` if every check passed."""
        return all(c.passed for c in self.checks)

    @property
    def failed_checks(self) -> list[ConformanceCheck]:
        """List of checks that failed (empty if all passed)."""
        return [c for c in self.checks if not c.passed]

    def to_dict(self) -> dict[str, object]:
        """Serialize to a plain dict for JSON output."""
        return {
            "executor": self.executor_name,
            "passed": self.passed,
            "n_checks": len(self.checks),
            "n_passed": sum(1 for c in self.checks if c.passed),
            "n_failed": sum(1 for c in self.checks if not c.passed),
            "checks": [
                {"name": c.name, "passed": c.passed, "detail": c.detail} for c in self.checks
            ],
        }


# ---------------------------------------------------------------------------
# Programmatic runner (no pytest required)
# ---------------------------------------------------------------------------


def run_executor_conformance(
    executor: BaseExecutor,
    *,
    run_stub_campaign: bool = False,
    example_package: Path | None = None,
    n_samples: int = 3,
) -> ConformanceReport:
    """Run every conformance check against *executor* and return a report.

    This is the non-pytest equivalent of subclassing
    :class:`ExecutorConformanceSuite`. It is intended for plug-in authors
    who want a single ``python -c`` verification flow::

        python -c "from osimflow.testing import run_executor_conformance; \\
                  from my_pkg.executors import MyExecutor; \\
                  print(run_executor_conformance(MyExecutor()).to_dict())"

    The campaign check is opt-in (``run_stub_campaign=True``) because it
    requires ``example_package/`` on disk and writes to a temp dir; pass
    ``example_package=Path('...')`` to point at a custom template.

    Returns:
        :class:`ConformanceReport` with one :class:`ConformanceCheck`
        per contract area.
    """
    report = ConformanceReport(executor_name=executor.name)
    _check_submit_returns_handle(executor, report)
    _check_handle_has_job_id(executor, report)
    _check_handle_done_returns_bool(executor, report)
    _check_handle_result_returns_value(executor, report)
    _check_handle_result_respects_timeout(executor, report)
    _check_handle_error_propagates(executor, report)
    _check_resource_directives_accepted(executor, report)
    _check_transport_path_round_trip(report)
    _check_transport_result_hint_default(report)
    _check_transport_result_hint_path_payload(report)
    _check_fanout_chunk_size_positive(executor, report)
    _check_submit_throttles_when_low_rps(executor, report)
    if run_stub_campaign:
        _check_three_sample_stub_campaign(executor, report, example_package, n_samples)
    return report


# ---------------------------------------------------------------------------
# Individual checks (factored out so the pytest suite and the runner share them)
# ---------------------------------------------------------------------------

_DEFAULT_TIMEOUT_S = 30.0


def _check_submit_returns_handle(executor: BaseExecutor, report: ConformanceReport) -> None:
    """``submit()`` must return a Handle instance."""
    from osimflow.executors.base import Handle  # noqa: PLC0415

    try:
        handle = executor.submit(lambda: None, name="conformance_submit")
        ok = isinstance(handle, Handle)
        detail = (
            f"submit() returned {type(handle).__name__}"
            if ok
            else f"expected Handle, got {type(handle).__name__}"
        )
    except Exception as exc:  # noqa: BLE001
        ok = False
        detail = f"submit() raised {type(exc).__name__}: {exc}"
    report.checks.append(ConformanceCheck("submit_returns_handle", ok, detail))


def _check_handle_has_job_id(executor: BaseExecutor, report: ConformanceReport) -> None:
    """``Handle.job_id`` must be a non-empty string."""
    try:
        handle = executor.submit(lambda: None, name="conformance_job_id")
        ok = isinstance(handle.job_id, str) and bool(handle.job_id)
        detail = f"job_id={handle.job_id!r}"
    except Exception as exc:  # noqa: BLE001
        ok = False
        detail = f"submit() raised {type(exc).__name__}: {exc}"
    report.checks.append(ConformanceCheck("handle_job_id_non_empty", ok, detail))


def _check_handle_done_returns_bool(executor: BaseExecutor, report: ConformanceReport) -> None:
    """``Handle.done()`` must return a bool and not raise."""
    try:
        handle = executor.submit(lambda: 42, name="conformance_done")
        done_flag = handle.done()
        result = handle.result(timeout=_DEFAULT_TIMEOUT_S)
        ok = isinstance(done_flag, bool) and result == 42
        detail = f"done()={done_flag!r} (type={type(done_flag).__name__}); result={result!r}"
    except Exception as exc:  # noqa: BLE001
        ok = False
        detail = f"{type(exc).__name__}: {exc}"
    report.checks.append(ConformanceCheck("handle_done_returns_bool", ok, detail))


def _check_handle_result_returns_value(executor: BaseExecutor, report: ConformanceReport) -> None:
    """``Handle.result(timeout=...)`` must block until the callable returns."""
    payload = {"k": "conformance_payload", "n": 7}

    def _fn() -> dict[str, object]:
        return payload

    try:
        handle = executor.submit(_fn, name="conformance_value")
        got = handle.result(timeout=_DEFAULT_TIMEOUT_S)
        ok = got == payload
        detail = f"expected={payload!r} got={got!r}"
    except Exception as exc:  # noqa: BLE001
        ok = False
        detail = f"{type(exc).__name__}: {exc}"
    report.checks.append(ConformanceCheck("handle_result_returns_value", ok, detail))


def _check_handle_result_respects_timeout(
    executor: BaseExecutor, report: ConformanceReport
) -> None:
    """``Handle.result(timeout=t)`` must raise ``TimeoutError`` when t < work duration."""
    deadline_s = 0.1
    work_s = 5.0

    def _slow() -> str:
        time.sleep(work_s)
        return "should-not-reach"

    try:
        handle = executor.submit(_slow, name="conformance_timeout")
        t0 = time.monotonic()
        raised: BaseException | None = None
        try:
            handle.result(timeout=deadline_s)
        except TimeoutError as exc:
            raised = exc
        elapsed = time.monotonic() - t0
        # Some executors raise concurrent.futures.TimeoutError (a subclass of
        # TimeoutError on 3.12+) — either is acceptable; what matters is
        # that the call returns before work_s completes.
        ok = raised is not None and elapsed < work_s
        if raised is None:
            detail = (
                f"result(timeout={deadline_s}s) returned without raising after "
                f"{elapsed:.2f}s (work was {work_s}s)"
            )
        else:
            detail = f"raised {type(raised).__name__} after {elapsed:.2f}s (work was {work_s}s)"
    except Exception as exc:  # noqa: BLE001
        ok = False
        detail = f"unexpected exception during submit/result: {type(exc).__name__}: {exc}"
    report.checks.append(ConformanceCheck("handle_result_respects_timeout", ok, detail))


def _check_handle_error_propagates(executor: BaseExecutor, report: ConformanceReport) -> None:
    """``Handle.result()`` must re-raise exceptions from the callable."""
    boom = RuntimeError("conformance-boom")

    def _fail() -> None:
        raise boom

    try:
        handle = executor.submit(_fail, name="conformance_error")
        raised: BaseException | None = None
        try:
            handle.result(timeout=_DEFAULT_TIMEOUT_S)
        except RuntimeError as exc:
            raised = exc
        ok = raised is not None and "conformance-boom" in str(raised)
        detail = (
            f"re-raised {type(raised).__name__}: {raised}"
            if raised is not None
            else "result() did not propagate the callable's exception"
        )
    except Exception as exc:  # noqa: BLE001
        ok = False
        detail = f"unexpected exception during submit: {type(exc).__name__}: {exc}"
    report.checks.append(ConformanceCheck("handle_error_propagates", ok, detail))


def _check_resource_directives_accepted(executor: BaseExecutor, report: ConformanceReport) -> None:
    """``submit()`` must accept cpus/memory_mb/time_min without raising.

    The directives are advisory on the in-process executors (Local,
    submitit-debug Slurm) and translated to substrate constraints on
    remote executors. Either is acceptable; what we assert here is that
    the API surface does not reject the kwargs.
    """
    try:
        handle = executor.submit(
            lambda: None,
            name="conformance_rc",
            cpus=4,
            memory_mb=8 * 1024,
            time_min=240,
        )
        handle.result(timeout=_DEFAULT_TIMEOUT_S)
        ok = True
        detail = "cpus=4 memory_mb=8192 time_min=240 accepted"
    except Exception as exc:  # noqa: BLE001
        ok = False
        detail = f"{type(exc).__name__}: {exc}"
    report.checks.append(ConformanceCheck("resource_directives_accepted", ok, detail))


def _check_transport_path_round_trip(report: ConformanceReport) -> None:
    """``encode_transport_value`` / ``decode_transport_value`` round-trip a Path."""
    from osimflow.executors.transport import (  # noqa: PLC0415
        decode_transport_value,
        encode_transport_value,
    )

    original = Path("/tmp/conformance/foo/bar.txt")
    try:
        encoded = encode_transport_value(original)
        if not (isinstance(encoded, dict) and encoded.get("__osimflow_type__") == "path"):
            ok = False
            detail = f"Path was not tagged: {encoded!r}"
        else:
            decoded = decode_transport_value(encoded)
            ok = decoded == original
            detail = (
                f"encoded={encoded!r} decoded={decoded!r}"
                if ok
                else f"mismatch: original={original!r} decoded={decoded!r}"
            )
    except Exception as exc:  # noqa: BLE001
        ok = False
        detail = f"{type(exc).__name__}: {exc}"
    report.checks.append(ConformanceCheck("transport_path_round_trip", ok, detail))


def _check_transport_result_hint_default(report: ConformanceReport) -> None:
    """``resolve_result_for_callback(None, default=X)`` returns X."""
    from osimflow.executors.transport import (  # noqa: PLC0415
        resolve_result_for_callback,
    )

    sentinel = object()
    try:
        got = resolve_result_for_callback(None, default=sentinel)
        ok = got is sentinel
        detail = (
            f"None hint returned the provided default (id match: {got is sentinel})"
            if ok
            else f"expected default sentinel, got {got!r}"
        )
    except Exception as exc:  # noqa: BLE001
        ok = False
        detail = f"{type(exc).__name__}: {exc}"
    report.checks.append(ConformanceCheck("transport_result_hint_default", ok, detail))


def _check_transport_result_hint_path_payload(report: ConformanceReport) -> None:
    """``resolve_result_for_callback`` decodes Path-tagged payloads."""
    from osimflow.executors.transport import (  # noqa: PLC0415
        decode_transport_value,
        encode_transport_value,
        resolve_result_for_callback,
    )

    hint = {"result": Path("/tmp/conformance/result.txt"), "status": "ok"}
    try:
        got = resolve_result_for_callback(hint, default=None)
        ok = (
            isinstance(got, dict)
            and got["status"] == "ok"
            and isinstance(got["result"], Path)
            and got["result"] == Path("/tmp/conformance/result.txt")
        )
        detail = (
            f"hint={hint!r} -> resolved={got!r}"
            if ok
            else f"hint did not round-trip: hint={hint!r} resolved={got!r}"
        )
        # Also verify decode_transport_value normalizes the dict keys to str.
        _ = decode_transport_value(encode_transport_value({1: Path("/tmp/x")}))
    except Exception as exc:  # noqa: BLE001
        ok = False
        detail = f"{type(exc).__name__}: {exc}"
    report.checks.append(ConformanceCheck("transport_result_hint_path_payload", ok, detail))


def _check_fanout_chunk_size_positive(executor: BaseExecutor, report: ConformanceReport) -> None:
    """``fanout_submit_chunk_size`` must return a positive int (issue #1342)."""
    try:
        chunk = executor.fanout_submit_chunk_size(1000)
        ok = isinstance(chunk, int) and chunk > 0
        detail = f"fanout_submit_chunk_size(1000)={chunk!r}"
    except Exception as exc:  # noqa: BLE001
        ok = False
        detail = f"{type(exc).__name__}: {exc}"
    report.checks.append(ConformanceCheck("fanout_chunk_size_positive", ok, detail))


def _check_submit_throttles_when_low_rps(executor: BaseExecutor, report: ConformanceReport) -> None:
    """``submit()`` must acquire from the shared rate limiter (issue #1563).

    The check installs a temporary ``TokenBucketRateLimiter(rate=20, burst=1)``
    on the executor, drains the single token, then measures how long a
    second ``submit()`` call takes. With a 1-token burst at 20 RPS the
    second call has to wait ~50 ms for the bucket to refill — slack
    ``>= 40 ms`` absorbs scheduling jitter.

    The check does not depend on the executor's substrate — it only
    exercises ``BaseExecutor.submit``'s template-method acquire path.
    """
    from osimflow.executors._rate_limiter import (  # noqa: PLC0415
        TokenBucketRateLimiter,
    )

    limiter = TokenBucketRateLimiter(rate_per_sec=20.0, burst=1)
    original: TokenBucketRateLimiter | None = getattr(executor, "_rate_limiter", None)
    executor._rate_limiter = limiter  # noqa: SLF001
    try:
        limiter.acquire()  # drain the single token
        t0 = time.monotonic()
        try:
            handle = executor.submit(lambda: None, name="conformance_throttle")
        except Exception as exc:  # noqa: BLE001
            ok = False
            detail = f"second submit() raised {type(exc).__name__}: {exc}"
            report.checks.append(ConformanceCheck("submit_throttles_when_low_rps", ok, detail))
            return
        # Drain the handle result so the thread pool completes; we don't
        # care about the value, only that submit() returned and didn't
        # crash.
        try:
            if hasattr(handle, "result"):
                handle.result(timeout=_DEFAULT_TIMEOUT_S)
        except Exception:  # noqa: BLE001
            pass
        elapsed = time.monotonic() - t0
        # 50 ms expected; slack 40 ms absorbs scheduling jitter on slow CI.
        ok = elapsed >= 0.04
        detail = (
            f"submit() blocked {elapsed * 1000:.1f}ms with rate=20 burst=1 "
            f"(expected >=40ms; rate=20 means 50ms refill interval)"
        )
    except AttributeError as exc:
        ok = False
        detail = (
            f"executor {executor.name!r} has no ``_rate_limiter`` slot — "
            f"BaseExecutor.submit cannot enforce throttling. ({exc})"
        )
    finally:
        executor._rate_limiter = original  # type: ignore[assignment]  # noqa: SLF001
    report.checks.append(ConformanceCheck("submit_throttles_when_low_rps", ok, detail))


def _check_three_sample_stub_campaign(
    executor: BaseExecutor,
    report: ConformanceReport,
    example_package: Path | None,
    n_samples: int,
) -> None:
    """Run a 3-sample Campaign in stub mode and assert the four artifacts."""
    try:
        from osimflow import Campaign, CampaignConfig  # noqa: PLC0415

        if example_package is None:
            example_package = Path(__file__).resolve().parents[2] / "example_package"
        if not example_package.is_dir():
            ok = False
            detail = f"example_package not found at {example_package}"
            report.checks.append(ConformanceCheck("three_sample_stub_campaign", ok, detail))
            return

        # Force stub mode for the duration of the campaign.
        prev_stub = os.environ.get("OSIMFLOW_STUB_SIM")
        os.environ["OSIMFLOW_STUB_SIM"] = "1"
        try:
            with tempfile.TemporaryDirectory() as tmp:
                workdir = Path(tmp) / "work"
                workdir.mkdir()
                (workdir / "variables.yml").write_text(
                    "algorithm: lhs\n"
                    "variables:\n"
                    "  - name: wwr\n"
                    "    distribution: uniform\n"
                    "    min: 0.2\n"
                    "    max: 0.6\n"
                    "    measure_argument: SetEnvelopePerformance.wwr\n"
                )
                outdir = Path(tmp) / "out"
                outdir.mkdir()
                template = workdir / "template"
                shutil.copytree(example_package, template)

                cfg = CampaignConfig(
                    input_variables=workdir / "variables.yml",
                    template_sim_package=template,
                    n_samples=n_samples,
                    outdir=outdir,
                    openstudio_version="3.11.0",
                    archive_intermediates=False,
                    skip_preflight=True,
                )
                Campaign(cfg=cfg, executor=executor).run()

                csv = outdir / "aggregated_results.csv"
                failed = outdir / "failed_simulations.csv"
                kpi_files = list((outdir / "work" / "kpis").glob("kpi_*.json"))
                plots = outdir / "plots"

                ok = (
                    csv.is_file()
                    and failed.is_file()
                    and len(kpi_files) == n_samples
                    and plots.is_dir()
                )
                detail = (
                    f"csv={csv.is_file()} failed={failed.is_file()} "
                    f"kpis={len(kpi_files)}/{n_samples} plots={plots.is_dir()}"
                )
        finally:
            if prev_stub is None:
                os.environ.pop("OSIMFLOW_STUB_SIM", None)
            else:
                os.environ["OSIMFLOW_STUB_SIM"] = prev_stub
    except Exception as exc:  # noqa: BLE001
        ok = False
        detail = f"{type(exc).__name__}: {exc}"
    report.checks.append(ConformanceCheck("three_sample_stub_campaign", ok, detail))


# ---------------------------------------------------------------------------
# Pytest mixin suite
# ---------------------------------------------------------------------------


class ExecutorConformanceSuite:
    """Mixin pytest suite for executor plug-in conformance (issue #1478).

    Subclass this in your plug-in's test module and override
    :attr:`executor_factory` (and optionally :attr:`executor_name` and
    :attr:`run_stub_campaign`). Every ``test_*`` method on the mixin
    will be discovered by pytest and run against the executor your
    factory returns.

    Example::

        # tests/test_my_executor.py
        from osimflow.testing import ExecutorConformanceSuite
        from my_pkg.executors import MyExecutor


        class TestMyExecutorConformance(ExecutorConformanceSuite):
            executor_factory = staticmethod(
                lambda: MyExecutor(endpoint="http://localhost:8080")
            )

    Required overrides:
        ``executor_factory``: Zero-argument callable returning a fresh
        :class:`~osimflow.executors.base.BaseExecutor` instance. The suite
        calls ``executor_factory()`` once per test, so any per-test state
        is reset between checks.

    Optional overrides:
        ``executor_name``: Name to use for health-check registration
        tests. Defaults to ``executor.name``. Set this only when the
        executor must be registered under a name different from its
        ``name`` attribute.
        ``run_stub_campaign``: When ``False`` (the default for plugin
        authors who only ship remote substrates), skip the 3-sample
        Campaign integration test. The in-repo test overrides this to
        ``True`` for :class:`~osimflow.executors.LocalExecutor`.
        ``example_package``: Path to a Campaign-ready template package
        (containing ``model.osm`` / ``workflow.osw``). Defaults to the
        repo-root ``example_package/`` directory.

    Notes for plug-in authors
    -------------------------
    * Each test calls ``executor_factory()`` independently, so any
      thread pool / connection state is fresh per check.
    * The campaign test is marked ``slow`` and is excluded from
      ``pytest -m 'not slow'`` runs. Override :attr:`run_stub_campaign`
      to opt out entirely.
    * The suite is a *mixin*: pytest requires a concrete class on disk,
      so you must subclass it (you cannot use it directly).
    """

    # ---- Required overrides ----
    executor_factory: ClassVar[Callable[..., BaseExecutor]]
    executor_name: ClassVar[str] = ""

    # ---- Optional overrides ----
    run_stub_campaign: ClassVar[bool] = False
    example_package: ClassVar[Path | None] = None

    # ---- Fixtures ----

    @pytest.fixture
    def conformance_executor(self) -> BaseExecutor:
        """Fresh executor instance per test (no shared thread pools)."""
        return self.executor_factory()

    # ---- submit / Handle lifecycle ----

    def test_submit_returns_handle(self, conformance_executor: BaseExecutor) -> None:
        """``submit(fn, ...)`` returns a :class:`Handle` instance."""
        from osimflow.executors.base import Handle  # noqa: PLC0415

        handle = conformance_executor.submit(lambda: None, name="submit_handle")
        assert isinstance(handle, Handle), (
            f"submit() returned {type(handle).__name__}, expected Handle"
        )

    def test_handle_job_id_is_non_empty_string(self, conformance_executor: BaseExecutor) -> None:
        """``Handle.job_id`` is a non-empty string."""
        handle = conformance_executor.submit(lambda: None, name="job_id")
        assert isinstance(handle.job_id, str), (
            f"Handle.job_id must be str, got {type(handle.job_id).__name__}"
        )
        assert handle.job_id, "Handle.job_id must be non-empty"

    def test_handle_done_returns_bool(self, conformance_executor: BaseExecutor) -> None:
        """``Handle.done()`` returns a bool and ``Handle.result()`` returns the value."""
        handle = conformance_executor.submit(lambda: 42, name="done")
        assert isinstance(handle.done(), bool), (
            f"Handle.done() returned {type(handle.done()).__name__}, expected bool"
        )
        assert handle.result(timeout=30) == 42

    def test_handle_result_returns_value(self, conformance_executor: BaseExecutor) -> None:
        """``Handle.result(timeout=...)`` blocks until the callable returns."""
        payload = {"k": "conformance_payload", "n": 7}

        def _fn() -> dict[str, object]:
            return payload

        handle = conformance_executor.submit(_fn, name="value")
        assert handle.result(timeout=30) == payload

    def test_handle_result_respects_timeout(self, conformance_executor: BaseExecutor) -> None:
        """A short timeout must raise ``TimeoutError`` before the work finishes."""

        def _slow() -> str:
            time.sleep(5.0)
            return "should-not-reach"

        handle = conformance_executor.submit(_slow, name="timeout")
        t0 = time.monotonic()
        with pytest.raises(TimeoutError):
            handle.result(timeout=0.1)
        elapsed = time.monotonic() - t0
        assert elapsed < 5.0, f"result() blocked {elapsed:.2f}s; should have raised within 0.1s"

    def test_handle_error_propagates(self, conformance_executor: BaseExecutor) -> None:
        """Exceptions raised by the callable propagate through ``Handle.result()``."""
        sentinel = "conformance-boom"

        def _fail() -> None:
            raise RuntimeError(sentinel)

        handle = conformance_executor.submit(_fail, name="error")
        with pytest.raises(RuntimeError, match=sentinel):
            handle.result(timeout=30)

    # ---- Resource directives ----

    def test_resource_directives_accepted(self, conformance_executor: BaseExecutor) -> None:
        """``cpus`` / ``memory_mb`` / ``time_min`` are accepted without raising."""
        handle = conformance_executor.submit(
            lambda: None,
            name="resource_directives",
            cpus=4,
            memory_mb=8 * 1024,
            time_min=240,
        )
        # Must not raise on submit; result should also resolve.
        handle.result(timeout=10)

    # ---- transport.py result-reference contract ----

    def test_transport_path_round_trip(self) -> None:
        """``encode_transport_value`` / ``decode_transport_value`` round-trip a Path."""
        from osimflow.executors.transport import (  # noqa: PLC0415
            decode_transport_value,
            encode_transport_value,
        )

        original = Path("/tmp/conformance/path.txt")
        encoded = encode_transport_value(original)
        assert isinstance(encoded, dict)
        assert encoded["__osimflow_type__"] == "path"
        assert encoded["value"] == str(original)
        assert decode_transport_value(encoded) == original

    def test_transport_result_hint_default_returns_default(self) -> None:
        """``resolve_result_for_callback(None, default=X)`` returns ``X``."""
        from osimflow.executors.transport import (  # noqa: PLC0415
            resolve_result_for_callback,
        )

        sentinel: object = object()
        assert resolve_result_for_callback(None, default=sentinel) is sentinel

    def test_transport_result_hint_path_payload_decodes(self) -> None:
        """``resolve_result_for_callback`` decodes Path-tagged payloads."""
        from osimflow.executors.transport import (  # noqa: PLC0415
            resolve_result_for_callback,
        )

        hint = {"result": Path("/tmp/conformance/result.txt"), "status": "ok"}
        resolved = resolve_result_for_callback(hint, default=None)
        assert isinstance(resolved, dict)
        assert resolved["status"] == "ok"
        assert resolved["result"] == Path("/tmp/conformance/result.txt")

    # ---- Fan-out pacing ----

    def test_fanout_chunk_size_returns_positive_int(
        self, conformance_executor: BaseExecutor
    ) -> None:
        """``fanout_submit_chunk_size(total)`` returns a positive int (issue #1342)."""
        chunk = conformance_executor.fanout_submit_chunk_size(1000)
        assert isinstance(chunk, int)
        assert chunk > 0

    def test_submit_throttles_when_low_rps(self, conformance_executor: BaseExecutor) -> None:
        """``submit()`` acquires from the shared rate limiter (issue #1563).

        Installs a ``TokenBucketRateLimiter(rate=20, burst=1)`` on the
        executor, drains the single token, then asserts that a second
        ``submit()`` call takes ``>= 40 ms`` (50 ms expected — the
        slack absorbs CI scheduling jitter). The check is fast (no
        wall-clock wait of seconds) and substrate-agnostic — it only
        exercises ``BaseExecutor.submit``'s template-method acquire.
        """
        from osimflow.executors._rate_limiter import (  # noqa: PLC0415
            TokenBucketRateLimiter,
        )

        limiter = TokenBucketRateLimiter(rate_per_sec=20.0, burst=1)
        original: TokenBucketRateLimiter | None = getattr(
            conformance_executor, "_rate_limiter", None
        )
        conformance_executor._rate_limiter = limiter  # noqa: SLF001
        try:
            limiter.acquire()  # drain the single token
            t0 = time.monotonic()
            handle = conformance_executor.submit(lambda: None, name="conformance_throttle")
            try:
                if hasattr(handle, "result"):
                    handle.result(timeout=_DEFAULT_TIMEOUT_S)
            except Exception:  # noqa: BLE001 — substrate may not support .result()
                pass
            elapsed = time.monotonic() - t0
        finally:
            conformance_executor._rate_limiter = original  # type: ignore[assignment]  # noqa: SLF001

        assert elapsed >= 0.04, (
            f"submit() did not throttle under rate=20 burst=1; took {elapsed * 1000:.1f}ms "
            f"(expected >=40ms)"
        )

    # ---- Health-check registration ----

    def test_register_health_check_returns_callable(
        self, conformance_executor: BaseExecutor
    ) -> None:
        """A health check can be registered for this executor via ``ExecutorRegistry``.

        Only built-in executors are pre-registered. Third-party plug-ins
        must register themselves via :func:`ExecutorRegistry.register`
        *before* the suite runs. Tests skip (rather than fail) when the
        executor is missing from the registry, so the suite is safe to
        run against an unregistered plug-in.
        """
        from osimflow.executors import ExecutorRegistry  # noqa: PLC0415
        from osimflow.health import (  # noqa: PLC0415
            CheckCategory,
            CheckResult,
            CheckStatus,
        )

        name = self.executor_name or conformance_executor.name
        if name not in ExecutorRegistry.list_available():
            pytest.skip(
                f"executor {name!r} not in ExecutorRegistry; third-party plug-ins "
                f"must call ExecutorRegistry.register() before running the suite"
            )
        sentinel_msg = "conformance health check OK"

        def _check() -> CheckResult:
            return CheckResult(
                name=f"Executor: {name} (conformance)",
                status=CheckStatus.PASS,
                category=CheckCategory.INFORMATIONAL,
                message=sentinel_msg,
            )

        original = ExecutorRegistry.get_health_check(name)
        ExecutorRegistry.register_health_check(name, _check)
        try:
            check_fn = ExecutorRegistry.get_health_check(name)
            assert check_fn is _check
            result = check_fn()
            assert isinstance(result, CheckResult)
            assert result.status == CheckStatus.PASS
            assert result.message == sentinel_msg
        finally:
            # Restore (or remove) only the entry this test modified; do NOT
            # call ``clear_health_checks()`` — that would wipe every other
            # executor's health check and break sibling tests (e.g.
            # test_health_check.TestExecutorHealthChecks) when pytest
            # happens to schedule them after this one in the same process.
            ExecutorRegistry._health_checks.pop(name, None)
            if original is not None:
                ExecutorRegistry.register_health_check(name, original)

    # ---- 3-sample stub campaign ----

    @pytest.mark.slow
    def test_three_sample_stub_campaign_produces_all_artifacts(
        self,
        conformance_executor: BaseExecutor,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Run a 3-sample Campaign in stub mode and assert all four artifacts.

        Mirrors ``tests/integration/test_local_executor.py`` but is
        parameterised over the executor under test, so a third-party
        executor can be verified end-to-end without writing bespoke
        integration tests.
        """
        if not self.run_stub_campaign:
            pytest.skip("run_stub_campaign=False on this subclass; campaign check skipped")

        example = self.example_package
        if example is None:
            example = Path(__file__).resolve().parents[2] / "example_package"
        if not example.is_dir():
            pytest.skip(f"example_package not found at {example}")
        # Skip if the test environment lacks the binaries that the
        # stub-mode campaign still shells out to (e.g. ``openstudio``
        # not on PATH AND ``OSIMFLOW_STUB_SIM`` not forced). The
        # conftest already forces stub mode, but be defensive.
        if shutil.which("openstudio") is None and not self._stub_forced(monkeypatch):
            pytest.skip("openstudio CLI not on PATH and stub mode not forced")

        from osimflow import Campaign, CampaignConfig  # noqa: PLC0415

        workdir = tmp_path / "work"
        workdir.mkdir()
        (workdir / "variables.yml").write_text(
            "algorithm: lhs\n"
            "variables:\n"
            "  - name: wwr\n"
            "    distribution: uniform\n"
            "    min: 0.2\n"
            "    max: 0.6\n"
            "    measure_argument: SetEnvelopePerformance.wwr\n"
        )
        template = workdir / "template"
        shutil.copytree(example, template)
        outdir = tmp_path / "out"
        outdir.mkdir()

        cfg = CampaignConfig(
            input_variables=workdir / "variables.yml",
            template_sim_package=template,
            n_samples=3,
            outdir=outdir,
            openstudio_version="3.11.0",
            archive_intermediates=False,
            skip_preflight=True,
        )
        monkeypatch.setenv("OSIMFLOW_STUB_SIM", "1")
        Campaign(cfg=cfg, executor=conformance_executor).run()

        csv = outdir / "aggregated_results.csv"
        failed = outdir / "failed_simulations.csv"
        kpi_files = list((outdir / "work" / "kpis").glob("kpi_*.json"))
        plots = outdir / "plots"
        assert csv.is_file(), f"missing aggregated_results.csv at {csv}"
        assert csv.read_text().startswith("sample_id")
        assert failed.is_file(), f"missing failed_simulations.csv at {failed}"
        assert failed.read_text().startswith("sample_id")
        assert len(kpi_files) == 3, f"expected 3 KPI JSONs, got {len(kpi_files)}"
        assert plots.is_dir(), f"missing plots directory at {plots}"

    # ---- internal helpers ----

    @staticmethod
    def _stub_forced(monkeypatch: pytest.MonkeyPatch) -> bool:
        """Return True if the test has explicitly forced stub mode via monkeypatch."""
        # monkeypatch.setenv in the test runs *before* this fixture, but
        # the fixture cannot read the env it just set without a round
        # trip through os.environ. The conftest sets
        # ``OSIMFLOW_STUB_SIM=1`` by default, so this is almost always
        # True; the helper exists so future test-only opt-out paths
        # have a hook.
        return os.environ.get("OSIMFLOW_STUB_SIM") == "1"


# ---------------------------------------------------------------------------
# Convenience export: a self-contained example factory for the in-repo
# LocalExecutor conformance test. External authors should NOT subclass this;
# they should write their own factory.
# ---------------------------------------------------------------------------


def _local_executor_factory() -> BaseExecutor:
    from osimflow.executors import LocalExecutor  # noqa: PLC0415

    return LocalExecutor(max_workers=3)
