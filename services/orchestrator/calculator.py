"""Bounded decimal arithmetic; Python syntax is parsed but never executed.

Operations: + - * / and parentheses, powers (``**`` or ``^``), and the
functions listed in ``FUNCTIONS``. Every size is bounded before any work is
done: expression length, syntax nodes, power exponents, result magnitude.
"""

from packages.limits import LimitError

import ast
import re
from collections.abc import Callable
from decimal import ROUND_HALF_UP, Decimal, DecimalException, localcontext

MAX_EXPRESSION = 512
MAX_NODES = 128
MAX_EXPONENT = 10000
# Results, and every power, stay between 1e-50 and 1e50 in absolute value.
MAX_DIGITS = 50
MAX_ROUND = 20

DESCRIPTION = (
    "Arithmetic expression, evaluated exactly in decimal. Operators: + - * / and "
    "parentheses; powers with ** or ^ (exponent at most 10000 in absolute value, "
    "a fractional exponent needs a base >= 0). Functions: sqrt(x); pow(x, y); "
    "round(x, n) rounds half up to n decimals (n from -20 to 20, default 0); "
    "percent(p, x) is p percent of x; percent_change(old, new) is the change "
    "from old to new in percent. Use a dot for decimals, no thousands separators "
    "and no % sign. Results stay below 1e50 in absolute value."
)


def calculate(expr: str) -> str:
    if len(expr) > MAX_EXPRESSION:
        raise LimitError("expression_limit", len(expr), MAX_EXPRESSION, "characters")
    text = expr.strip().lower()
    if "%" in text:
        raise ValueError("invalid_expression: write percent(p, x), not %")
    if not text or re.search(r"[^0-9a-z_.,()+*/^\s-]", text):
        raise ValueError("invalid_expression")
    unknown = sorted(set(re.findall(r"[a-z_]+", text)) - set(FUNCTIONS))
    if unknown:
        raise ValueError("invalid_expression: unknown name " + unknown[0][:20])
    text = text.replace("^", "**")
    try:
        tree = ast.parse(text, mode="eval")
        nodes = sum(1 for _ in ast.walk(tree))
        if nodes > MAX_NODES:
            raise LimitError("expression_limit", nodes, MAX_NODES, "syntax nodes")
        if isinstance(tree.body, ast.Tuple):
            raise ValueError("invalid_expression: use a dot for decimals")
        with localcontext() as context:
            context.prec = 64
            result = _number(tree.body, text)
        if not result.is_finite() or abs(result) > Decimal(10) ** MAX_DIGITS:
            raise LimitError(
                "expression_limit", int(abs(result)), 10**MAX_DIGITS, "absolute value"
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
        if isinstance(node.op, ast.Pow):
            return _power(left, right)
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in FUNCTIONS
        and not node.keywords
    ):
        arities, function = FUNCTIONS[node.func.id]
        if len(node.args) not in arities:
            counts = " or ".join(str(count) for count in arities)
            raise ValueError(
                f"invalid_expression: {node.func.id} takes {counts} argument(s)"
            )
        return function(*(_number(argument, source) for argument in node.args))
    raise ValueError("invalid_expression")


def _power(base: Decimal, exponent: Decimal) -> Decimal:
    if abs(exponent) > MAX_EXPONENT:
        raise LimitError(
            "expression_limit", int(abs(exponent)), MAX_EXPONENT, "exponent"
        )
    integral = exponent == exponent.to_integral_value()
    if base == 0:
        if exponent < 0:
            raise ValueError("invalid_expression: division by zero")
        return Decimal(1) if exponent == 0 else Decimal(0)
    if base < 0 and not integral:
        raise ValueError("invalid_expression: fractional power of a negative number")
    # Bound the size of the result before computing it.
    digits = abs(base).log10() * exponent
    if abs(digits) > MAX_DIGITS:
        raise LimitError(
            "expression_limit", int(abs(digits)), MAX_DIGITS, "result digits"
        )
    return base**exponent


def _sqrt(value: Decimal) -> Decimal:
    if value < 0:
        raise ValueError("invalid_expression: square root of a negative number")
    return value.sqrt()


def _round(value: Decimal, places: Decimal = Decimal(0)) -> Decimal:
    if places != places.to_integral_value() or abs(places) > MAX_ROUND:
        raise ValueError(
            f"invalid_expression: round needs a whole number of decimals "
            f"from -{MAX_ROUND} to {MAX_ROUND}"
        )
    return value.quantize(Decimal(1).scaleb(-int(places)), rounding=ROUND_HALF_UP)


def _percent(rate: Decimal, value: Decimal) -> Decimal:
    return value * rate / 100


def _percent_change(old: Decimal, new: Decimal) -> Decimal:
    if old == 0:
        raise ValueError("invalid_expression: percent_change from zero")
    return (new - old) / abs(old) * 100


FUNCTIONS: dict[str, tuple[tuple[int, ...], Callable[..., Decimal]]] = {
    "sqrt": ((1,), _sqrt),
    "pow": ((2,), _power),
    "round": ((1, 2), _round),
    "percent": ((2,), _percent),
    "percent_change": ((2,), _percent_change),
}
