"""Exact unit conversion with a small, audited table of definitions.

Factors are the legal definitions (SI brochure, NIST SP 811 and the 1959
international yard and pound agreement), held as decimals so that a conversion
by an exact factor stays exact. Temperatures convert through kelvin with their
offsets; fuel consumption converts through kilometres per litre, which is an
inverse relation for litres per 100 km. Incompatible dimensions are refused.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, DecimalException, localcontext

MAX_MAGNITUDE = Decimal(10) ** 30
SIGNIFICANT = 12


@dataclass(frozen=True)
class Unit:
    name: str
    dimension: str
    factor: Decimal  # value in the dimension's base unit
    exact: bool = True
    note: str = ""


def _d(text: str) -> Decimal:
    return Decimal(text)


_KMH = _d("1000") / _d("3600")
_KNOT = _d("1852") / _d("3600")
# Pound-force: the pound times standard gravity (9.80665 m/s2), in newtons.
_LBF = _d("0.45359237") * _d("9.80665")
# One litre per 100 km is 100 km per litre; the other entries are km per litre.
_MPG_US = _d("1.609344") / _d("3.785411784")
_MPG_UK = _d("1.609344") / _d("4.54609")

UNITS: dict[str, Unit] = {}
ALIASES: dict[str, str] = {}
AMBIGUOUS: dict[str, str] = {
    "ton": "use tonne (1000 kg), short_ton (US, 2000 lb) or long_ton (UK, 2240 lb)",
    "tons": "use tonne (1000 kg), short_ton (US, 2000 lb) or long_ton (UK, 2240 lb)",
    "mille": "use mile (1609.344 m) or nautical_mile (1852 m)",
    "milles": "use mile (1609.344 m) or nautical_mile (1852 m)",
    "tasse": "use cup (US, 236.6 mL) or metric_cup (250 mL)",
    "tasses": "use cup (US, 236.6 mL) or metric_cup (250 mL)",
    "degre": "use celsius, fahrenheit or kelvin",
    "degres": "use celsius, fahrenheit or kelvin",
    "degree": "use celsius, fahrenheit or kelvin",
    "degrees": "use celsius, fahrenheit or kelvin",
    "calorie": "use kcal (food Calorie, 4184 J) or cal (4.184 J)",
    "calories": "use kcal (food Calorie, 4184 J) or cal (4.184 J)",
}


def _add(
    name: str,
    dimension: str,
    factor: Decimal | str,
    aliases: str = "",
    exact: bool = True,
    note: str = "",
) -> None:
    value = factor if isinstance(factor, Decimal) else _d(factor)
    UNITS[name] = Unit(name, dimension, value, exact, note)
    for alias in [name, *aliases.split("|")]:
        if alias:
            ALIASES[_key(alias)] = name


def _key(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text.strip().lower())
    plain = "".join(c for c in folded if not unicodedata.combining(c))
    plain = plain.replace("²", "2").replace("³", "3").replace("^", "")
    plain = plain.replace("µ", "u").replace("μ", "u").replace("º", "°")
    plain = re.sub(r"[\s_\-.]+", " ", plain).strip()
    return plain.replace("deg ", "°").replace("° ", "°")


# Length, base metre.
_add("metre", "length", "1", "m|meter|meters|metres|metre s")
_add("kilometre", "length", "1000", "km|kilometer|kilometers|kilometres|kms")
_add("centimetre", "length", "0.01", "cm|centimeter|centimeters|centimetres")
_add("millimetre", "length", "0.001", "mm|millimeter|millimeters|millimetres")
_add("micrometre", "length", "0.000001", "um|micron|microns|micrometer|micrometers")
_add("nanometre", "length", "0.000000001", "nm|nanometer|nanometers|nanometres")
_add("inch", "length", "0.0254", 'in|inches|"|pouce|pouces')
_add("foot", "length", "0.3048", "ft|feet|'|pied|pieds")
_add("yard", "length", "0.9144", "yd|yards|yds|verge|verges")
_add("mile", "length", "1609.344", "mi|miles|statute mile")
_add(
    "nautical_mile",
    "length",
    "1852",
    "nmi|nautical miles|mille marin|milles marins|mille nautique|milles nautiques",
)
# Mass, base kilogram.
_add("kilogram", "mass", "1", "kg|kilo|kilos|kilogramme|kilogrammes|kilograms|kgs")
_add("gram", "mass", "0.001", "g|gr|grams|gramme|grammes")
_add("milligram", "mass", "0.000001", "mg|milligrams|milligramme|milligrammes")
_add("tonne", "mass", "1000", "t|tonnes|metric ton|metric tons")
_add(
    "pound",
    "mass",
    "0.45359237",
    "lb|lbs|pounds|livre|livres",
    note="avoirdupois pound; a French market livre is 500 g",
)
_add("ounce", "mass", "0.028349523125", "oz|ounces|once|onces")
_add("stone", "mass", "6.35029318", "st|stones")
_add("short_ton", "mass", "907.18474", "short ton|short tons|us ton")
_add("long_ton", "mass", "1016.0469088", "long ton|long tons|imperial ton")
_add("carat", "mass", "0.0002", "ct|carats")
# Volume, base litre.
_add("litre", "volume", "1", "l|liter|liters|litres|lt")
_add("millilitre", "volume", "0.001", "ml|milliliter|milliliters|millilitres")
_add("centilitre", "volume", "0.01", "cl|centiliter|centiliters|centilitres")
_add("decilitre", "volume", "0.1", "dl|deciliter|deciliters|decilitres")
_add(
    "cubic_metre",
    "volume",
    "1000",
    "m3|cubic meter|cubic meters|metre cube|metres cubes",
)
_add("cubic_centimetre", "volume", "0.001", "cm3|cc|cubic centimeter|cubic centimeters")
_add(
    "gallon",
    "volume",
    "3.785411784",
    "gal|gallons|us gallon|us gallons",
    note="US gallon; the imperial gallon is 4.54609 L",
)
_add(
    "imperial_gallon", "volume", "4.54609", "imperial gallon|imperial gallons|uk gallon"
)
_add("quart", "volume", "0.946352946", "qt|quarts|us quart", note="US liquid quart")
_add(
    "pint",
    "volume",
    "0.473176473",
    "pt|pints|us pint|pinte|pintes",
    note="US pint; the imperial pint is 568.26125 mL",
)
_add("imperial_pint", "volume", "0.56826125", "imperial pint|imperial pints|uk pint")
_add(
    "cup",
    "volume",
    "0.2365882365",
    "cups|us cup|us cups",
    note="US customary cup; a metric cup is 250 mL",
)
_add("metric_cup", "volume", "0.25", "metric cup|metric cups")
_add(
    "fluid_ounce",
    "volume",
    "0.0295735295625",
    "fl oz|floz|fluid ounce|fluid ounces|us fl oz",
    note="US fluid ounce",
)
_add("imperial_fluid_ounce", "volume", "0.0284130625", "imperial fl oz|uk fl oz")
_add(
    "tablespoon",
    "volume",
    "0.01478676478125",
    "tbsp|tablespoons|tbs|us tablespoon",
    note="US tablespoon; the metric tablespoon is 15 mL",
)
_add(
    "teaspoon",
    "volume",
    "0.00492892159375",
    "tsp|teaspoons|us teaspoon",
    note="US teaspoon; the metric teaspoon is 5 mL",
)
_add(
    "metric_tablespoon",
    "volume",
    "0.015",
    "cuillere a soupe|cuilleres a soupe|cas|c a s|metric tablespoon",
)
_add(
    "metric_teaspoon",
    "volume",
    "0.005",
    "cuillere a cafe|cuilleres a cafe|cac|c a c|metric teaspoon",
)
_add("oil_barrel", "volume", "158.987294928", "bbl|barrel|barrels|baril|barils")
# Area, base square metre.
_add(
    "square_metre",
    "area",
    "1",
    "m2|sq m|square meter|square meters|metre carre|metres carres",
)
_add(
    "square_kilometre",
    "area",
    "1000000",
    "km2|sq km|square kilometer|square kilometers",
)
_add(
    "square_centimetre",
    "area",
    "0.0001",
    "cm2|sq cm|square centimeter|square centimeters",
)
_add("hectare", "area", "10000", "ha|hectares")
_add("are", "area", "100", "ares")
_add("acre", "area", "4046.8564224", "acres|ac")
_add(
    "square_foot",
    "area",
    "0.09290304",
    "ft2|sq ft|square feet|square foot|pied carre|pieds carres",
)
_add("square_inch", "area", "0.00064516", "in2|sq in|square inch|square inches")
_add("square_yard", "area", "0.83612736", "yd2|sq yd|square yard|square yards")
_add("square_mile", "area", "2589988.110336", "mi2|sq mi|square mile|square miles")
# Speed, base metre per second.
_add("metre_per_second", "speed", "1", "m/s|mps|meters per second|metres per second")
_add(
    "kilometre_per_hour",
    "speed",
    _KMH,
    "km/h|kmh|kph|kmph|km per hour|kilometres per hour|kilometers per hour|km h",
)
_add("mile_per_hour", "speed", "0.44704", "mph|mi/h|miles per hour|miles an hour")
_add("knot", "speed", _KNOT, "kn|kt|knots|noeud|noeuds")
_add("foot_per_second", "speed", "0.3048", "ft/s|fps|feet per second")
# Temperature: handled by offsets, factors unused.
_add(
    "celsius",
    "temperature",
    "1",
    "c|degc|°c|degre celsius|degres celsius|degree celsius|degrees celsius|centigrade",
)
_add(
    "fahrenheit",
    "temperature",
    "1",
    "f|degf|°f|degre fahrenheit|degres fahrenheit|degree fahrenheit|degrees fahrenheit",
)
_add("kelvin", "temperature", "1", "k|kelvins")
# Energy, base joule.
_add("joule", "energy", "1", "j|joules")
_add("kilojoule", "energy", "1000", "kj|kilojoules")
_add("megajoule", "energy", "1000000", "mj|megajoules")
_add("watt_hour", "energy", "3600", "wh|watt hour|watt hours|watt heure|watts heure")
_add(
    "kilowatt_hour",
    "energy",
    "3600000",
    "kwh|kw h|kilowatt hour|kilowatt hours|kilowatt heure|kilowatts heure",
)
_add("megawatt_hour", "energy", "3600000000", "mwh|megawatt hour|megawatt hours")
_add(
    "cal",
    "energy",
    "4.184",
    "small calorie|thermochemical calorie",
    note="thermochemical calorie",
)
_add(
    "kcal",
    "energy",
    "4184",
    "kilocalorie|kilocalories|food calorie|food calories|kcals",
)
_add(
    "btu",
    "energy",
    "1055.05585262",
    "btus|british thermal unit",
    note="International Table BTU",
)
_add(
    "electronvolt",
    "energy",
    "0.0000000000000000001602176634",
    "ev|electron volt|electronvolts",
)
# Power, base watt.
_add("watt", "power", "1", "w|watts")
_add("kilowatt", "power", "1000", "kw|kilowatts")
_add("megawatt", "power", "1000000", "mw|megawatts")
_add(
    "horsepower",
    "power",
    _d("550") * _d("0.3048") * _LBF,
    "hp|bhp|mechanical horsepower",
    note="mechanical horsepower (550 ft lbf/s)",
)
_add(
    "metric_horsepower",
    "power",
    "735.49875",
    "ch|cv|ps|cheval|chevaux|cheval vapeur|chevaux vapeur|metric horsepower",
)
# Pressure, base pascal.
_add("pascal", "pressure", "1", "pa|pascals")
_add("hectopascal", "pressure", "100", "hpa|hectopascals")
_add("kilopascal", "pressure", "1000", "kpa|kilopascals")
_add("megapascal", "pressure", "1000000", "mpa|megapascals")
_add("bar", "pressure", "100000", "bars")
_add("millibar", "pressure", "100", "mbar|millibars")
_add("atmosphere", "pressure", "101325", "atm|atmospheres")
_add(
    "psi",
    "pressure",
    _LBF / _d("0.00064516"),
    "lbf/in2|pound per square inch|pounds per square inch",
)
_add(
    "mmhg",
    "pressure",
    "133.322387415",
    "mm hg|millimetre of mercury|millimeters of mercury",
)
# Data size, base byte. French octets (o, ko, Mo...) are bytes.
_add("bit", "data", "0.125", "bits")
_add("byte", "data", "1", "bytes|octet|octets|o")
for _prefix, _power, _short, _french in (
    ("kilo", 1, "kb", "ko"),
    ("mega", 2, "mb", "mo"),
    ("giga", 3, "gb", "go"),
    ("tera", 4, "tb", "to"),
    ("peta", 5, "pb", "po"),
):
    _add(
        f"{_prefix}byte",
        "data",
        Decimal(1000) ** _power,
        f"{_short}|{_french}|{_prefix}bytes|{_prefix}octet|{_prefix}octets",
    )
    _add(
        f"{_prefix[:2]}bibyte",
        "data",
        Decimal(1024) ** _power,
        f"{_short[0]}ib|{_short[0]}io|{_prefix[:2]}bibytes",
    )
    _add(
        f"{_prefix}bit",
        "data",
        Decimal(1000) ** _power / 8,
        f"{_short[0]}bit|{_short[0]}bits|{_prefix}bits",
    )
# Duration, base second (calendar months and years belong to the date tool).
_add("second", "duration", "1", "s|sec|secs|seconds|seconde|secondes")
_add("millisecond", "duration", "0.001", "ms|milliseconds|milliseconde|millisecondes")
_add("minute", "duration", "60", "min|mins|minutes")
_add("hour", "duration", "3600", "h|hr|hrs|hours|heure|heures")
_add("day", "duration", "86400", "d|days|jour|jours")
_add("week", "duration", "604800", "wk|weeks|semaine|semaines")
# Fuel economy, base kilometre per litre (litres per 100 km is inverse).
_add("kilometre_per_litre", "fuel_economy", "1", "km/l|kmpl|km per litre|km per liter")
_add(
    "litre_per_100_km",
    "fuel_economy",
    "100",
    "l/100km|l/100 km|l per 100 km|litres per 100 km|liters per 100 km|l aux 100|l/100",
)
_add(
    "mile_per_gallon",
    "fuel_economy",
    _MPG_US,
    "mpg|us mpg|miles per gallon",
    note="US gallon",
)
_add("mile_per_imperial_gallon", "fuel_economy", _MPG_UK, "uk mpg|imperial mpg|mpg uk")

INVERSE = frozenset({"litre_per_100_km"})
# Byte, bit and the bare "b" differ by case only.
CASED = {"B": "byte", "b": "bit", "Mb": "megabit", "Gb": "gigabit", "Kb": "kilobit"}


class UnitError(ValueError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code


def unit(text: str) -> Unit:
    raw = text.strip()
    if raw in CASED:
        return UNITS[CASED[raw]]
    key = _key(raw)
    if key in AMBIGUOUS:
        raise UnitError("ambiguous_unit", f"{raw[:40]!r}: {AMBIGUOUS[key]}")
    name = ALIASES.get(key) or ALIASES.get(key.replace(" ", ""))
    if name is None and key.endswith("s"):
        name = ALIASES.get(key[:-1])
    if name is None:
        raise UnitError("unknown_unit", f"{raw[:40]!r} is not a known unit")
    return UNITS[name]


def _to_kelvin(value: Decimal, name: str) -> Decimal:
    if name == "celsius":
        return value + _d("273.15")
    if name == "fahrenheit":
        return (value + _d("459.67")) * 5 / 9
    return value


def _from_kelvin(value: Decimal, name: str) -> Decimal:
    if name == "celsius":
        return value - _d("273.15")
    if name == "fahrenheit":
        return value * 9 / 5 - _d("459.67")
    return value


def _shown(value: Decimal) -> str:
    if value == 0:
        return "0"
    with localcontext() as context:
        context.prec = SIGNIFICANT
        rounded = +value
    text = format(rounded.normalize(), "f")
    return text


def convert(value: Decimal, source: str, target: str) -> dict[str, object]:
    first, second = unit(source), unit(target)
    if first.dimension != second.dimension:
        raise UnitError(
            "incompatible_units",
            f"{first.name} is a {first.dimension} unit and {second.name} is a "
            f"{second.dimension} unit; they cannot be converted",
        )
    if not value.is_finite() or abs(value) > MAX_MAGNITUDE:
        raise UnitError("value_out_of_range", "at most 1e30 in absolute value")
    try:
        with localcontext() as context:
            context.prec = 40
            if first.dimension == "temperature":
                kelvin = _to_kelvin(value, first.name)
                if kelvin < 0:
                    raise UnitError("below_absolute_zero", f"{value} {first.name}")
                result = _from_kelvin(kelvin, second.name)
            elif first.dimension == "fuel_economy":
                base = (
                    first.factor / value
                    if first.name in INVERSE
                    else value * first.factor
                )
                result = (
                    second.factor / base
                    if second.name in INVERSE
                    else base / second.factor
                )
            else:
                result = value * first.factor / second.factor
    except (DecimalException, ZeroDivisionError):
        raise UnitError("invalid_value", f"{value} cannot be converted") from None
    notes = [entry.note for entry in (first, second) if entry.note]
    if first.dimension == "data" and {first.name, second.name} & {
        name for name in UNITS if "bibyte" in name
    }:
        notes.append("kB, MB, GB are powers of 1000; KiB, MiB, GiB are powers of 1024")
    return {
        "value": _shown(value),
        "from_unit": first.name,
        "result": _shown(result),
        "to_unit": second.name,
        "dimension": first.dimension,
        "significant_digits": SIGNIFICANT,
        "notes": notes,
    }
