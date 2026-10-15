"""measure.xml-typed OSW argument coercion (issue #2093)."""

import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from osimflow.apply_params import ArgumentCoercionError, apply_parameters

ARGS = {
    "dbl": "Double",
    "intg": "Integer",
    "flag": "Boolean",
    "text": "String",
    "choice": "Choice",
}


def _template(tmp_path: Path) -> Path:
    pkg = tmp_path / "pkg"
    mdir = pkg / "measures" / "Typed"
    mdir.mkdir(parents=True)
    arg_xml = "".join(
        f"<argument><name>{n}</name><type>{t}</type><required>false</required></argument>"
        for n, t in ARGS.items()
    )
    (mdir / "measure.xml").write_text(
        f"<measure><name>typed</name><arguments>{arg_xml}</arguments></measure>"
    )
    (mdir / "measure.rb").write_text("# measure\n")
    steps = [{"measure_dir_name": "Typed", "arguments": {n: None for n in ARGS}}]
    (pkg / "workflow.osw").write_text(json.dumps({"steps": steps}))
    return pkg


def _apply(tmp_path: Path, params: dict[str, Any]) -> dict[str, Any]:
    out = tmp_path / "out"
    apply_parameters(template=_template(tmp_path), parameters=params, sample_id="0001", out=out)
    args: dict[str, Any] = json.loads((out / "workflow.osw").read_text())["steps"][0]["arguments"]
    return args


@pytest.mark.parametrize(
    ("name", "value", "expected"),
    [
        ("dbl", "1.5", 1.5),
        ("dbl", 2, 2.0),
        ("intg", 3.0, 3),
        ("intg", "4", 4),
        ("intg", "5.0", 5),
        ("flag", "TRUE", True),
        ("flag", 0, False),
        ("flag", "false", False),
        ("text", 7, "7"),
        ("text", True, "true"),
        ("choice", 1, "1"),
        ("choice", False, "false"),
        ("choice", "keep", "keep"),
        ("flag", np.bool_(True), True),
        ("flag", np.int64(0), False),
        ("intg", np.float32(3.0), 3),
        ("intg", np.int64(8), 8),
        ("dbl", np.int64(2), 2.0),
        ("text", np.int64(5), "5"),
        ("choice", np.float64(1.5), "1.5"),
        ("text", Path("a/b"), "a/b"),
    ],
)
def test_coerces_by_measure_xml_type(tmp_path: Path, name: str, value: Any, expected: Any) -> None:
    got = _apply(tmp_path, {f"Typed.{name}": value})[name]
    assert got == expected
    assert type(got) is type(expected)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("dbl", "abc"),
        ("dbl", True),
        ("dbl", float("nan")),
        ("intg", 2.5),
        ("intg", "x"),
        ("intg", True),
        ("flag", "maybe"),
        ("flag", 2),
    ],
)
def test_invalid_coercion_raises_clear_error(tmp_path: Path, name: str, value: Any) -> None:
    with pytest.raises(ArgumentCoercionError, match=f"'{name}'"):
        _apply(tmp_path, {f"Typed.{name}": value})
