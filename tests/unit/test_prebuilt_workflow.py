"""Prebuilt (existing) OSW execution path and remote-hook rejection (issue #1812)."""

import sys
from pathlib import Path
from typing import Any

import pytest

from osimflow.config import load_config
from osimflow.executors import AWSBatchExecutor
from osimflow.remote_runner import StepFunctionRegistry, _register_builtin_steps
from osimflow.work import default_apply_parameters


def _pkg(tmp_path: Path) -> Path:
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "workflow.osw").write_text('{"seed_file": "model.osm"}\n')
    return pkg


def test_empty_parameters_leave_package_untouched_without_bindings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OSIMFLOW_STUB_SIM", raising=False)
    monkeypatch.setitem(sys.modules, "openstudio", None)  # import would fail
    pkg = _pkg(tmp_path)
    before = (pkg / "workflow.osw").read_bytes()
    assert default_apply_parameters(pkg, {}, "0001", pkg) == pkg
    assert (pkg / "workflow.osw").read_bytes() == before


def test_parametric_path_still_requires_bindings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OSIMFLOW_STUB_SIM", raising=False)
    monkeypatch.setitem(sys.modules, "openstudio", None)
    pkg = _pkg(tmp_path)
    (pkg / "model.osm").write_text("OS:Version,\n")
    with pytest.raises(RuntimeError, match="OpenStudio Python bindings"):
        default_apply_parameters(pkg, {"x": 1.0}, "0001", pkg)


def test_empty_package_is_rejected(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(FileNotFoundError):
        default_apply_parameters(empty, {}, "0001", empty)


def test_load_config_synthesises_variables_in_prebuilt_mode(tmp_path: Path) -> None:
    cfg = load_config(
        {
            "template_sim_package": str(_pkg(tmp_path)),
            "n_samples": 1,
            "outdir": str(tmp_path / "out"),
            "openstudio_version": "3.11.0",
            "prebuilt_workflow": True,
        }
    )
    assert cfg.prebuilt_workflow is True
    assert cfg.input_variables.is_file()


def test_load_config_requires_variables_otherwise(tmp_path: Path) -> None:
    from osimflow.validation import ValidationError  # noqa: PLC0415

    with pytest.raises(ValidationError):
        load_config(
            {
                "template_sim_package": str(_pkg(tmp_path)),
                "n_samples": 1,
                "outdir": str(tmp_path / "out"),
            }
        )


def test_aws_batch_accepts_builtin_hooks() -> None:
    _register_builtin_steps()
    AWSBatchExecutor.validate_work_fn("apply", StepFunctionRegistry.get("apply"))
    AWSBatchExecutor.validate_work_fn("extract", StepFunctionRegistry.get("extract"))


@pytest.mark.parametrize("step", ["apply", "extract"])
def test_aws_batch_rejects_custom_hook(step: str) -> None:
    def custom(*_a: Any, **_k: Any) -> None:
        return None

    with pytest.raises(NotImplementedError, match="custom"):
        AWSBatchExecutor.validate_work_fn(step, custom)
