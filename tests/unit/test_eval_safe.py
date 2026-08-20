"""Unit tests for osimflow/_eval_safe.py.

Covers:
- Literals through ast.Constant (int / float / str / bytes / True / False / None)
- Arithmetic / boolean / comparison operators
- Disallowed syntax raises ExpressionError (no bypass via deprecated AST classes)
- Python 3.14 forward-compat: deprecated ast.Num/Str/Bytes/NameConstant
  are no longer referenced in the allowed set (issue #1046)
"""

from __future__ import annotations

import warnings

import pytest

from osimflow._eval_safe import ExpressionError, safe_eval


@pytest.mark.parametrize(
    ("expr", "expected"),
    [
        ("1", 1),
        ("-1", -1),
        ("3.14", 3.14),
        ("'hello'", "hello"),
        ('"world"', "world"),
        ("b'bytes'", b"bytes"),
        ("True", True),
        ("False", False),
        ("None", None),
        ("(1, 2, 3)", (1, 2, 3)),
        ("[1, 2]", [1, 2]),
        ("{1, 2}", {1, 2}),
        ("{'k': 1}", {"k": 1}),
    ],
)
def test_safe_eval_literals(expr: str, expected: object) -> None:
    assert safe_eval(expr, {}) == expected


@pytest.mark.parametrize(
    ("expr", "expected"),
    [
        ("1 + 2", 3),
        ("10 - 3", 7),
        ("4 * 5", 20),
        ("7 / 2", 3.5),
        ("10 // 3", 3),
        ("10 % 3", 1),
        ("2 ** 8", 256),
        ("-2 ** 2", -4),
    ],
)
def test_safe_eval_arithmetic(expr: str, expected: object) -> None:
    assert safe_eval(expr, {}) == expected


@pytest.mark.parametrize(
    ("expr", "expected"),
    [
        ("True and False", False),
        ("True or False", True),
        ("not True", False),
        ("1 < 2", True),
        ("1 == 1", True),
        ("1 != 2", True),
        ("'a' in 'abc'", True),
        ("'x' in 'abc'", False),
    ],
)
def test_safe_eval_logic(expr: str, expected: bool) -> None:
    assert safe_eval(expr, {}) == expected


def test_safe_eval_user_variable() -> None:
    """Names resolve to globals_dict entries."""
    assert safe_eval("x + 1", {"x": 41}) == 42
    with pytest.raises(ExpressionError, match="unknown variable"):
        safe_eval("undefined_name", {})


def test_safe_eval_disallows_function_calls() -> None:
    with pytest.raises(ExpressionError):
        safe_eval("len([1, 2])", {})


def test_safe_eval_disallows_attribute_access() -> None:
    with pytest.raises(ExpressionError):
        safe_eval("(1).__class__", {})


def test_safe_eval_disallows_subscript() -> None:
    with pytest.raises(ExpressionError):
        safe_eval("[1, 2][0]", {})


def test_safe_eval_disallows_import() -> None:
    with pytest.raises(ExpressionError):
        safe_eval("__import__('os')", {})


def test_safe_eval_ternary() -> None:
    assert safe_eval("1 if True else 2", {}) == 1
    assert safe_eval("1 if False else 2", {}) == 2


def test_no_deprecation_warnings_emitted() -> None:
    """Regression: safe_eval must not import or reference deprecated AST classes
    (ast.Num / Str / Bytes / NameConstant), all of which were deprecated in
    Python 3.8 and are scheduled for removal in 3.14 (issue #1046).

    On Python 3.14 these classes AttributeError at construction time, so
    referencing them in the module body (e.g. inside ``_ALLOWED_NODES``) would
    break ``import osimflow._eval_safe`` itself. The fix is to remove them
    from the allowed-node set entirely, since they all collapsed into
    ``ast.Constant`` in 3.8.
    """
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        # Exercise every literal path — each one used to print a DeprecationWarning
        # before the fix because the type was referenced inside the frozenset.
        safe_eval("1", {})
        safe_eval("'a'", {})
        safe_eval("b'bytes'", {})
        safe_eval("True", {})
        safe_eval("None", {})
        safe_eval("3.14", {})
    deprecations = [w for w in caught if issubclass(w.category, DeprecationWarning)]
    assert deprecations == [], (
        f"safe_eval triggered DeprecationWarning(s): {[str(w.message) for w in deprecations]}"
    )
