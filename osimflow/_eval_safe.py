"""Safe expression evaluator — replaces eval() with a minimal AST validator.

This module provides :func:`safe_eval`, a whitelist-only expression evaluator
that prevents arbitrary code execution even when an attacker controls the
expression string.

Allowed syntax
--------------
* Comparison: ``==``, ``!=``, ``<``, ``>``, ``<=``, ``>=``
* Boolean: ``and``, ``or``, ``not``
* Arithmetic: ``+``, ``-``, ``*``, ``/``
* Names: any identifier present in the variables dict
* Literals: integers, floats, strings (``"..."`` or ``'...'``), booleans
  (``True`` / ``False``), ``None``

Blocked (will raise :exc:`ValueError`)
---------------------------------------
* Function calls (``len(x)``, ``abs(...)``)
* Attribute access (``x.attr``, ``x.__class__``)
* Subscript / slice (``x[0]``, ``x["key"]``)
* Lambda / comprehension / generator expressions
* Import / reload / open / eval / exec / compile
* Any hidden back-door via introspection objects (``().__class__``,
  ``().__class__.__bases__[0].__subclasses__()`` …)
"""

from __future__ import annotations

import ast
import operator
from collections.abc import Callable
from typing import Any

__all__ = ["safe_eval", "ExpressionError"]

_MAX_CONTAINER_DEPTH = 10
_MAX_CONTAINER_SIZE = 1000

# ── operators ─────────────────────────────────────────────────────────────────


def _bool_and(a: bool, b: bool) -> bool:
    return a and b


def _bool_or(a: bool, b: bool) -> bool:
    return a or b


def _cmp_in(a: Any, b: Any) -> bool:
    return a in b


def _cmp_not_in(a: Any, b: Any) -> bool:
    return a not in b


_BOOL_OPS: dict[type[ast.boolop], Callable[[bool, bool], bool]] = {
    ast.And: _bool_and,
    ast.Or: _bool_or,
}

_CMP_OPS: dict[type[ast.cmpop], Callable[[Any, Any], bool]] = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.Is: operator.is_,
    ast.IsNot: operator.is_not,
    ast.In: _cmp_in,
    ast.NotIn: _cmp_not_in,
}

_ARITH_OPS: dict[type[ast.operator], Callable[[Any, Any], Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}


class ExpressionError(ValueError):
    """Raised when an expression contains disallowed syntax."""


# ── visitor ────────────────────────────────────────────────────────────────────

_UNSAFE_NODES = frozenset(
    (
        # Code execution
        ast.Call,
        ast.Lambda,
        ast.ListComp,
        ast.SetComp,
        ast.DictComp,
        ast.GeneratorExp,
        ast.Yield,
        ast.YieldFrom,
        ast.Await,
        # Import / exec
        ast.Import,
        ast.ImportFrom,
        ast.alias,
        # Slice / subscript — allows ``x[0]`` style indexing
        ast.Subscript,
        ast.Slice,
        ast.Index,
    )
)

_ALLOWED_NODES = frozenset(
    (
        # Literals
        ast.Expression,
        ast.BoolOp,
        ast.BinOp,
        ast.UnaryOp,
        ast.Compare,
        ast.IfExp,
        # Basic containers (allowed as literals, not as subscript targets)
        ast.Tuple,
        ast.List,
        ast.Set,
        ast.Dict,
        # Literals (Python 3.8+ unified into ast.Constant;
        # ast.Num/Str/Bytes/NameConstant were deprecated and are
        # removed in 3.14, so they are no longer listed here)
        ast.Constant,
        ast.Name,
        # Formatted value (f-string) — reject if contains conversion/spec
        ast.JoinedStr,
        ast.FormattedValue,
    )
)


class _SafeVisitor(ast.NodeVisitor):
    """Walk an AST and raise :exc:`ExpressionError` on unsafe nodes."""

    __slots__ = ("_depth",)

    def __init__(self) -> None:
        self._depth = 0

    def generic_visit(self, node: ast.AST) -> None:  # noqa: PLR0912
        if type(node) in _UNSAFE_NODES:
            raise ExpressionError(f"disallowed node type {node.__class__.__name__!r} in expression")
        if type(node) not in _ALLOWED_NODES:
            raise ExpressionError(f"unknown node type {node.__class__.__name__!r} in expression")
        if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
            if len(node.elts) > _MAX_CONTAINER_SIZE:
                raise ExpressionError(
                    f"container has {len(node.elts)} elements, max is {_MAX_CONTAINER_SIZE}"
                )
        elif isinstance(node, ast.Dict):
            if len(node.keys) > _MAX_CONTAINER_SIZE:
                raise ExpressionError(
                    f"dict has {len(node.keys)} entries, max is {_MAX_CONTAINER_SIZE}"
                )
        self._depth += 1
        try:
            if self._depth > _MAX_CONTAINER_DEPTH:
                raise ExpressionError(
                    f"container nesting depth {self._depth} exceeds max {_MAX_CONTAINER_DEPTH}"
                )
            # Only recurse into operands, not operator labels (BinOp.op, UnaryOp.op, Compare.ops, etc.)
            if isinstance(node, ast.Expression):
                self.visit(node.body)
            elif isinstance(node, ast.BinOp):
                self.visit(node.left)
                self.visit(node.right)
            elif isinstance(node, ast.UnaryOp):
                self.visit(node.operand)
            elif isinstance(node, ast.Compare):
                self.visit(node.left)
                for comparator in node.comparators:
                    self.visit(comparator)
            elif isinstance(node, ast.BoolOp):
                for value in node.values:
                    self.visit(value)
            elif isinstance(node, ast.IfExp):
                self.visit(node.test)
                self.visit(node.body)
                self.visit(node.orelse)
            elif isinstance(node, ast.Dict):
                for k, v in zip(node.keys, node.values, strict=True):
                    if k is not None:
                        self.visit(k)
                    self.visit(v)
            elif isinstance(node, (ast.Tuple, ast.List, ast.Set)):
                for elt in node.elts:
                    self.visit(elt)
            elif isinstance(node, ast.Name):
                pass  # Name has `id` (str) and `ctx` (Load/Store) — no AST children to recurse
            elif isinstance(node, ast.Constant):
                pass  # Leaf node
            elif isinstance(node, ast.FormattedValue):
                self.visit(node.value)
                if node.format_spec is not None:
                    self.visit(node.format_spec)
            elif isinstance(node, ast.JoinedStr):
                for value in node.values:
                    self.visit(value)
        finally:
            self._depth -= 1


def _check_ast(node: ast.AST) -> None:
    """Validate that *node* only uses the allowed AST node types."""
    _SafeVisitor().visit(node)


# ── evaluator ──────────────────────────────────────────────────────────────────


def _eval_name(node: ast.Name, globals_dict: dict[str, Any]) -> Any:
    try:
        return globals_dict[node.id]
    except KeyError:
        raise ExpressionError(f"unknown variable {node.id!r} in expression") from None


def _eval_constant(node: ast.Constant) -> Any:
    return node.value


def _eval_node(node: ast.AST, globals_dict: dict[str, Any], depth: int = 0) -> Any:  # noqa: PLR0911, PLR0912
    """Recursively evaluate an already-validated AST node."""
    if depth > _MAX_CONTAINER_DEPTH:
        raise ExpressionError(
            f"container nesting depth {depth} exceeds max {_MAX_CONTAINER_DEPTH}"
        )
    # Literals
    if isinstance(node, ast.Constant):
        return _eval_constant(node)
    if isinstance(node, ast.Name):
        return _eval_name(node, globals_dict)

    # Arithmetic
    if isinstance(node, ast.BinOp):
        op_func = _ARITH_OPS.get(type(node.op))
        if op_func is None:
            raise ExpressionError(f"unsupported binary operator {node.op!r}")
        return op_func(
            _eval_node(node.left, globals_dict, depth + 1),
            _eval_node(node.right, globals_dict, depth + 1),
        )

    # Unary (minus, not)
    if isinstance(node, ast.UnaryOp):
        if isinstance(node.op, ast.Not):
            return not _eval_node(node.operand, globals_dict, depth + 1)
        if isinstance(node.op, ast.USub):
            return -_eval_node(node.operand, globals_dict, depth + 1)
        if isinstance(node.op, ast.UAdd):
            return +_eval_node(node.operand, globals_dict, depth + 1)
        raise ExpressionError(f"unsupported unary operator {node.op!r}")

    # Compare
    if isinstance(node, ast.Compare):
        left = _eval_node(node.left, globals_dict, depth + 1)
        for op, right_node in zip(node.ops, node.comparators, strict=True):
            op_func = _CMP_OPS.get(type(op))
            if op_func is None:
                raise ExpressionError(f"unsupported comparison {op!r}")
            right = _eval_node(right_node, globals_dict, depth + 1)
            if not op_func(left, right):
                return False
            left = right
        return True

    # Boolean (and / or)
    if isinstance(node, ast.BoolOp):
        op_func = _BOOL_OPS.get(type(node.op))
        if op_func is None:
            raise ExpressionError(f"unsupported bool operator {node.op!r}")
        # Short-circuit evaluation using all()/any()
        if isinstance(node.op, ast.And):
            return all(_eval_node(v, globals_dict, depth + 1) for v in node.values)
        return any(_eval_node(v, globals_dict, depth + 1) for v in node.values)

    # If expression (ternary)
    if isinstance(node, ast.IfExp):
        return (
            _eval_node(node.body, globals_dict, depth + 1)
            if _eval_node(node.test, globals_dict, depth + 1)
            else _eval_node(node.orelse, globals_dict, depth + 1)
        )

    # Containers (tuple, list, set, dict) — allowed as literals
    if isinstance(node, ast.Tuple):
        return tuple(_eval_node(elt, globals_dict, depth + 1) for elt in node.elts)
    if isinstance(node, ast.List):
        return [_eval_node(elt, globals_dict, depth + 1) for elt in node.elts]
    if isinstance(node, ast.Set):
        return {_eval_node(elt, globals_dict, depth + 1) for elt in node.elts}
    if isinstance(node, ast.Dict):
        return {
            _eval_node(k, globals_dict, depth + 1): _eval_node(v, globals_dict, depth + 1)  # type: ignore[arg-type]
            for k, v in zip(node.keys, node.values, strict=True)
        }

    raise ExpressionError(f"unhandled node type {node.__class__.__name__!r}")


def safe_eval(expr: str, globals_dict: dict[str, Any]) -> Any:
    """Evaluate *expr* as a safe Python expression.

    Parameters
    ----------
    expr:
        The expression string to evaluate.
    globals_dict:
        Dictionary of variable names → values available to the expression.

    Returns
    -------
    The result of the expression (typically a :class:`bool`).

    Raises
    ------
    ExpressionError
        If the expression contains disallowed syntax.
    SyntaxError
        If the expression is not valid Python.
    """
    try:
        tree = ast.parse(expr.strip(), mode="eval")
    except SyntaxError:
        raise

    _check_ast(tree)
    body = tree.body
    assert body is not None
    result = _eval_node(body, globals_dict)
    return result
