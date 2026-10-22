"""Bounded decimal arithmetic; Python syntax is parsed but never executed."""

from packages.limits import LimitError

import ast
import re
from decimal import Decimal, DecimalException, localcontext


def calculate(expr: str) -> str:
    if len(expr) > 512:
        raise LimitError("expression_limit", len(expr), 512, "characters")
    if not expr.strip() or re.search(r"[^0-9.()+*/\s-]", expr):
        raise ValueError("invalid_expression")
    try:
        tree = ast.parse(expr.strip(), mode="eval")
        nodes = sum(1 for _ in ast.walk(tree))
        if nodes > 128:
            raise LimitError("expression_limit", nodes, 128, "syntax nodes")
        with localcontext() as context:
            context.prec = 64
            result = _number(tree.body, expr.strip())
        if not result.is_finite() or abs(result) > Decimal("1e50"):
            raise LimitError(
                "expression_limit", int(abs(result)), 10**50, "absolute value"
            )
        return format(result, "f")
    except (SyntaxError, DecimalException, RecursionError):
        raise ValueError("invalid_expression") from None


def _number(node: ast.AST, source: str) -> Decimal:
    if isinstance(node, ast.Constant) and type(node.value) in (int, float):
        segment = ast.get_source_segment(source, node)
        if segment is None:
            raise ValueError("invalid_expression")
        return Decimal(segment)
    if isinstance(node, ast.UnaryOp):
        number = _number(node.operand, source)
        if isinstance(node.op, ast.USub):
            return -number
        if isinstance(node.op, ast.UAdd):
            return number
    if isinstance(node, ast.BinOp):
        left, right = _number(node.left, source), _number(node.right, source)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, ast.Div):
            return left / right
    raise ValueError("invalid_expression")
