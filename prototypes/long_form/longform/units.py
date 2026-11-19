"""Small dimensional analysis: units written on pages parsed into a scale and base dimensions.

A quantity is (value in base units, dimensions). Additions, comparisons, min and max need the
same dimensions; products and quotients combine them. The derive step evaluates formulas on
quantities, so a conversion factor is never guessed by the model and a sum of gigabytes and
seconds is rejected by the code.
"""

from __future__ import annotations

import math
import re
import unicodedata

PREFIX = {"": 1.0, "k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9, "T": 1e12, "P": 1e15, "E": 1e18, "m": 1e-3, "µ": 1e-6, "u": 1e-6, "n": 1e-9}
BINARY = {"Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40}

# name -> (scale to base, {dimension: power})
BASE: dict[str, tuple[float, dict[str, int]]] = {
    "b": (1.0, {"byte": 1}),  # replaced by the byte/bit rule below
    "byte": (1.0, {"byte": 1}), "bytes": (1.0, {"byte": 1}), "octet": (1.0, {"byte": 1}), "octets": (1.0, {"byte": 1}),
    "bit": (0.125, {"byte": 1}), "bits": (0.125, {"byte": 1}),
    "s": (1.0, {"s": 1}), "sec": (1.0, {"s": 1}), "second": (1.0, {"s": 1}), "seconds": (1.0, {"s": 1}), "seconde": (1.0, {"s": 1}), "secondes": (1.0, {"s": 1}),
    "min": (60.0, {"s": 1}), "minute": (60.0, {"s": 1}), "minutes": (60.0, {"s": 1}),
    "h": (3600.0, {"s": 1}), "hour": (3600.0, {"s": 1}), "hours": (3600.0, {"s": 1}), "heure": (3600.0, {"s": 1}), "heures": (3600.0, {"s": 1}),
    "day": (86400.0, {"s": 1}), "days": (86400.0, {"s": 1}), "jour": (86400.0, {"s": 1}), "jours": (86400.0, {"s": 1}), "j": (86400.0, {"s": 1}),
    "month": (2629800.0, {"s": 1}), "months": (2629800.0, {"s": 1}), "mois": (2629800.0, {"s": 1}),
    "year": (31557600.0, {"s": 1}), "years": (31557600.0, {"s": 1}), "yr": (31557600.0, {"s": 1}), "an": (31557600.0, {"s": 1}), "ans": (31557600.0, {"s": 1}), "année": (31557600.0, {"s": 1}), "années": (31557600.0, {"s": 1}),
    "flop": (1.0, {"flop": 1}), "flops": (1.0, {"flop": 1, "s": -1}), "op": (1.0, {"flop": 1}), "ops": (1.0, {"flop": 1, "s": -1}),
    "token": (1.0, {"token": 1}), "tokens": (1.0, {"token": 1}), "tok": (1.0, {"token": 1}), "jeton": (1.0, {"token": 1}), "jetons": (1.0, {"token": 1}),
    "parameter": (1.0, {"param": 1}), "parameters": (1.0, {"param": 1}), "param": (1.0, {"param": 1}), "params": (1.0, {"param": 1}), "paramètre": (1.0, {"param": 1}), "paramètres": (1.0, {"param": 1}),
    "eur": (1.0, {"eur": 1}), "€": (1.0, {"eur": 1}), "euro": (1.0, {"eur": 1}), "euros": (1.0, {"eur": 1}),
    "usd": (1.0, {"usd": 1}), "$": (1.0, {"usd": 1}), "dollar": (1.0, {"usd": 1}), "dollars": (1.0, {"usd": 1}),
    "w": (1.0, {"j": 1, "s": -1}), "watt": (1.0, {"j": 1, "s": -1}), "watts": (1.0, {"j": 1, "s": -1}),
    "wh": (3600.0, {"j": 1}), "j": (1.0, {"j": 1}), "joule": (1.0, {"j": 1}), "joules": (1.0, {"j": 1}),
    "v": (1.0, {"v": 1}), "volt": (1.0, {"v": 1}), "volts": (1.0, {"v": 1}),
    "ah": (3600.0, {"a": 1, "s": 1}), "a": (1.0, {"a": 1}),
    "m": (1.0, {"m": 1}), "m2": (1.0, {"m": 2}), "m²": (1.0, {"m": 2}), "m3": (1.0, {"m": 3}), "m³": (1.0, {"m": 3}), "l": (1e-3, {"m": 3}), "litre": (1e-3, {"m": 3}), "litres": (1e-3, {"m": 3}),
    "g": (1e-3, {"kg": 1}), "t": (1000.0, {"kg": 1}), "tonne": (1000.0, {"kg": 1}), "tonnes": (1000.0, {"kg": 1}), "tco2": (1000.0, {"kg": 1}), "tco2e": (1000.0, {"kg": 1}), "gco2": (1e-3, {"kg": 1}), "gco2e": (1e-3, {"kg": 1}), "kgco2": (1.0, {"kg": 1}), "kgco2e": (1.0, {"kg": 1}),
    "hz": (1.0, {"s": -1}),
    "%": (0.01, {}), "percent": (0.01, {}), "pourcent": (0.01, {}),
    "user": (1.0, {"user": 1}), "users": (1.0, {"user": 1}), "utilisateur": (1.0, {"user": 1}), "utilisateurs": (1.0, {"user": 1}),
    "request": (1.0, {"request": 1}), "requests": (1.0, {"request": 1}), "requête": (1.0, {"request": 1}), "requêtes": (1.0, {"request": 1}),
    "gpu": (1.0, {"device": 1}), "gpus": (1.0, {"device": 1}), "card": (1.0, {"device": 1}), "cards": (1.0, {"device": 1}), "carte": (1.0, {"device": 1}), "cartes": (1.0, {"device": 1}), "device": (1.0, {"device": 1}), "devices": (1.0, {"device": 1}), "chip": (1.0, {"device": 1}), "chips": (1.0, {"device": 1}), "puce": (1.0, {"device": 1}), "puces": (1.0, {"device": 1}),
    "x": (1.0, {}), "×": (1.0, {}), "times": (1.0, {}), "fois": (1.0, {}), "": (1.0, {}),
}
WORD_SCALE = {"thousand": 1e3, "mille": 1e3, "million": 1e6, "millions": 1e6, "billion": 1e9, "billions": 1e9, "milliard": 1e9, "milliards": 1e9, "md": 1e9, "bn": 1e9, "trillion": 1e12, "trillions": 1e12, "k": 1e3}


class UnitError(ValueError):
    pass


def _clean(unit: str) -> str:
    unit = unicodedata.normalize("NFKC", unit or "").strip()
    unit = unit.replace("·", "*").replace("⋅", "*").replace("per ", "/").replace(" par ", "/").replace("/an", "/year")
    unit = re.sub(r"\s+", " ", unit)
    return unit


def _atom(token: str) -> tuple[float, dict[str, int]]:
    """One unit word, with an optional SI prefix and power ("GB", "To", "kWh", "m2", "teraFLOPS")."""
    raw = token.strip()
    if not raw:
        return 1.0, {}
    power = 1
    match = re.fullmatch(r"(.+?)\^?([23])", raw)
    if match and match.group(1).lower() not in ("m",) and not raw.lower().endswith(("co2", "m2", "m3")):
        raw, power = match.group(1), int(match.group(2))
    low = raw.lower()
    for word, scale in (("tera", 1e12), ("giga", 1e9), ("mega", 1e6), ("méga", 1e6), ("kilo", 1e3), ("peta", 1e15), ("péta", 1e15), ("téra", 1e12), ("milli", 1e-3), ("micro", 1e-6), ("nano", 1e-9)):
        if low.startswith(word) and low[len(word):] in BASE:
            s, d = BASE[low[len(word):]]
            return (scale * s) ** power, {k: v * power for k, v in d.items()}
    # Bytes and bits: B / o is a byte, b a bit (GB vs Gb), octets in French.
    m = re.fullmatch(r"(Ki|Mi|Gi|Ti|[kKMGTPE]?)([Bbo])", raw)
    if m:
        scale = BINARY.get(m.group(1)) or PREFIX[m.group(1)]
        return scale * (0.125 if m.group(2) == "b" else 1.0), {"byte": 1}
    if low in BASE and low not in ("b",):
        s, d = BASE[low]
        return s ** power, {k: v * power for k, v in d.items()}
    if raw[:1] in PREFIX and raw[:1] and raw[1:].lower() in BASE and raw[1:].lower() not in ("", "b"):
        s, d = BASE[raw[1:].lower()]
        return (PREFIX[raw[:1]] * s) ** power, {k: v * power for k, v in d.items()}
    if low in WORD_SCALE:
        return WORD_SCALE[low], {}
    raise UnitError(f"unknown unit {token!r}")


def parse(unit: str) -> tuple[float, dict[str, int]]:
    """Scale and dimensions of a written unit: "TB/s", "To/s", "tokens/s", "EUR/kWh", "g CO2/kWh", "billion parameters"."""
    text = _clean(unit)
    if not text:
        return 1.0, {}
    text = re.sub(r"(?i)\b([gk]?g|t)\s*co2(e|eq)?\b", lambda m: m.group(1) + "co2", text)
    text = re.sub(r"(?i)\bteraflops\b", "TFLOPS", text)
    parts = re.split(r"\s*/\s*", text)
    scale, dims = 1.0, {}
    for index, part in enumerate(parts):
        sign = 1 if index == 0 else -1
        for word in re.split(r"[\s*]+", part):
            if not word or word.lower() in ("of", "de", "d'", "per", "par", "a", "un", "une"):
                continue
            s, d = _atom(word)
            scale *= s if sign > 0 else 1 / s
            for k, v in d.items():
                dims[k] = dims.get(k, 0) + sign * v
    return scale, {k: v for k, v in dims.items() if v}


def same_dims(a: dict, b: dict) -> bool:
    return {k: v for k, v in a.items() if v} == {k: v for k, v in b.items() if v}


def family(unit: str) -> str | None:
    """A readable dimension signature, or None when the unit is not understood."""
    try:
        _, dims = parse(unit)
    except UnitError:
        return None
    return signature(dims)


def signature(dims: dict) -> str:
    return "*".join(f"{k}^{v}" for k, v in sorted(dims.items()) if v) or "1"


class Q:
    """A quantity in base units with dimensions."""

    __slots__ = ("v", "d")

    def __init__(self, value: float, dims: dict | None = None):
        self.v = float(value)
        self.d = {k: v for k, v in (dims or {}).items() if v}

    @classmethod
    def of(cls, value: float, unit: str) -> "Q":
        scale, dims = parse(unit)
        return cls(value * scale, dims)

    def _coerce(self, other):
        return other if isinstance(other, Q) else Q(other)

    def _same(self, other, op):
        other = self._coerce(other)
        if not same_dims(self.d, other.d):
            raise UnitError(f"{op} of {signature(self.d)} and {signature(other.d)}")
        return other

    def __add__(self, other):
        other = self._same(other, "sum")
        return Q(self.v + other.v, self.d)

    __radd__ = __add__

    def __sub__(self, other):
        other = self._same(other, "difference")
        return Q(self.v - other.v, self.d)

    def __rsub__(self, other):
        return self._coerce(other).__sub__(self)

    def __mul__(self, other):
        other = self._coerce(other)
        dims = dict(self.d)
        for k, v in other.d.items():
            dims[k] = dims.get(k, 0) + v
        return Q(self.v * other.v, dims)

    __rmul__ = __mul__

    def __truediv__(self, other):
        other = self._coerce(other)
        dims = dict(self.d)
        for k, v in other.d.items():
            dims[k] = dims.get(k, 0) - v
        return Q(self.v / other.v, dims)

    def __rtruediv__(self, other):
        return self._coerce(other).__truediv__(self)

    def __pow__(self, power):
        power = power.v if isinstance(power, Q) else power
        if self.d and float(power) != int(power):
            raise UnitError("fractional power of a dimensioned quantity")
        return Q(self.v ** power, {k: int(v * power) for k, v in self.d.items()})

    def __neg__(self):
        return Q(-self.v, self.d)

    def __pos__(self):
        return self

    def to(self, unit: str) -> float:
        scale, dims = parse(unit)
        if not same_dims(self.d, dims):
            raise UnitError(f"result is {signature(self.d)}, declared unit {unit!r} is {signature(dims)}")
        return self.v / scale

    def __lt__(self, other):
        other = self._same(other, "comparison")
        return self.v < other.v

    def __gt__(self, other):
        other = self._same(other, "comparison")
        return self.v > other.v

    def __repr__(self):
        return f"Q({self.v:g}, {signature(self.d)})"


def qmin(*args):
    return min(args, key=lambda q: (q._same(args[0], "min") if isinstance(q, Q) else q, q.v if isinstance(q, Q) else q)[1])


def qmax(*args):
    return max(args, key=lambda q: (q._same(args[0], "max") if isinstance(q, Q) else q, q.v if isinstance(q, Q) else q)[1])


def ratio(a: Q, b: Q) -> float | None:
    if not same_dims(a.d, b.d) or b.v == 0:
        return None
    return a.v / b.v


def magnitude_gap(a: Q, b: Q) -> float | None:
    r = ratio(a, b)
    if r is None or r <= 0:
        return None
    return abs(math.log10(r))


def loose_unit(text: str) -> str | None:
    """The longest leading run of words that parses as a unit ("To/s de bande passante" -> "To/s"); "" when none is written."""
    text = (text or "").strip()
    if not text:
        return ""
    words = text.split()
    for k in range(min(len(words), 4), 0, -1):
        candidate = " ".join(words[:k]).rstrip(".,;:)")
        try:
            parse(candidate)
            return candidate
        except UnitError:
            continue
    if not re.match(r"[A-Za-zÀ-ÿ€$%]", text):
        return ""
    return None


def custom_dims(unit: str) -> dict[str, int]:
    """A unit word the table does not know becomes its own dimension ("cores", "SM"), so counts combine consistently."""
    word = re.sub(r"[^a-zà-ÿ0-9]", "", (unit or "").split()[0].lower()) if (unit or "").split() else ""
    word = re.sub(r"(?<=[a-z]{3})s$", "", word)
    return {"u:" + word: 1} if word else {}


def fact_quantity(value: float, unit: str) -> "Q":
    unit = unit or ""
    loose = loose_unit(unit)
    if loose is not None:
        try:
            return Q.of(value, loose)
        except UnitError:
            pass
    return Q(value, custom_dims(unit))
