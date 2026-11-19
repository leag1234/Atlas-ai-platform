"""Unit checks of the v6 code comparisons: python -m pytest test_v6_checks.py (PYTHONPATH as in runcase.sh)."""

from longform import claims, derive, units


def fact(subject, value, **conditions):
    c = {"precision": None, "sparsity": None, "scope": None, "bound": None, "status": None, "config": None, "year": None}
    c.update(conditions)
    return {"subject": subject, "value": value, "conditions": c}


def kinds(found):
    return sorted(i["kind"] for i in found)


def test_units_convert_and_reject():
    assert abs((units.Q.of(140, "Go") / units.Q.of(3.35, "To/s")).to("ms") - 41.79) < 0.01
    assert units.parse("teraFLOPS")[1] == {"flop": 1, "s": -1}
    try:
        units.Q.of(1, "GB") + units.Q.of(1, "TB/s")
    except units.UnitError:
        pass
    else:
        raise AssertionError("a sum of bytes and bytes per second must fail")


def test_derive_rejects_conversion_factors_and_mixed_precision():
    facts = {
        "F1": {"id": "F1", "value": 3.35, "unit": "TB/s", "conditions": {}},
        "F3": {"id": "F3", "value": 3958, "unit": "TFLOPS", "conditions": {"precision": "FP8", "sparsity": "sparse"}},
        "F4": {"id": "F4", "value": 989, "unit": "TFLOPS", "conditions": {"precision": "FP16", "sparsity": "dense"}},
    }
    for f in facts.values():
        f["_q"] = units.fact_quantity(f["value"], f["unit"])
    kept, rejected = derive._evaluate_rows(
        [{"formula": "F1 * 1000", "unit": "GB/s"}, {"formula": "F3 / F4", "unit": ""}, {"formula": "size / F1", "unit": "ms", "assumptions": {"size": {"value": 140, "unit": "GB"}}}],
        facts, 10)
    assert len(kept) == 1 and abs(kept[0]["value"] - 41.79) < 0.01
    assert len(rejected) == 2


def test_subject_variants_and_levels():
    full = fact("GH100 full chip", 144)
    assert kinds(claims.compare(full, "La carte H100 SXM5 compte 144 SM.", "")) == ["subject_mismatch"]
    assert claims.compare(full, "Le GH100 complet compte 144 SM, la version SXM5 en active 132.", "") == []
    sxm = fact("H100 SXM5", 132)
    assert kinds(claims.compare(sxm, "La H100 PCIe en a 132.", "")) == ["subject_mismatch"]
    assert claims.compare(sxm, "Elle en a 132.", "La H100 SXM5 est une carte.") == []


def test_conditions_from_quote_and_sentence():
    c = claims.normalise_conditions({}, "FP8 Tensor Core 3,958 teraFLOPS with sparsity", "TFLOPS")
    assert c["sparsity"] == "sparse" and c["precision"] == "FP8"
    f = {"subject": "H100 SXM5", "value": 3958, "conditions": c}
    assert kinds(claims.compare(f, "La H100 SXM5 atteint 3 958 TFLOPS en FP8.", "")) == ["condition_dropped"]
    assert claims.compare(f, "La H100 SXM5 atteint 3 958 TFLOPS en FP8 avec sparsité.", "") == []


def test_years_are_not_quantities():
    assert claims.years_in("2000 TFLOPS en FP8 et 2048 tokens de contexte") == set()
    assert claims.years_in("annoncée en 2022, livrée en 2023.") == {"2022", "2023"}


def test_capacity_and_units_in_relations():
    facts = {"F2": {"id": "F2", "value": 80, "unit": "GB", "statement": "H100 SXM5 memory capacity 80 GB HBM3", "subject": "H100 SXM5"}}
    facts["F2"]["_q"] = units.fact_quantity(80, "GB")
    items = [("Un modèle de 140 Go tourne sur une H100.", "", []), ("3,35 To/s, soit trois fois plus que 1000 TFLOPS.", "", [])]
    answer = {
        "placements": [{"n": 1, "item": "model", "item_size": "140 Go", "container": "H100", "containers": None}],
        "comparisons": [{"n": 2, "a": "3,35 To/s", "b": "1000 TFLOPS", "kind": "ratio", "stated_factor": 3}],
    }
    assert kinds(claims.check_relations(answer, items, facts, "S1")) == ["capacity_exceeded", "unit_inhomogeneous"]
