#!/usr/bin/env python3
"""Samples FLORES-200 (dev) for the E9 suite — 15 professional pairs.

FLORES-200 (Meta, CC-BY-SA 4.0) : mêmes phrases traduites par des professionnels
dans 200+ langues → références de traduction fiables, alignées par index.

Usage:
    pip install datasets pyyaml
    python fetch_flores.py --out ../golden/e9_flores.yaml --seed 42

Le seed est FIGÉ (reproductibilité) ; ne pas le changer sans versionner le résultat.
"""
from __future__ import annotations
import argparse, random
import yaml

LANGS = {"fr": "fra_Latn", "de": "deu_Latn", "es": "spa_Latn",
         "it": "ita_Latn", "en": "eng_Latn"}
DIRECTIONS = [("fr","de"), ("de","fr"), ("fr","es"), ("es","fr"),
              ("fr","it"), ("it","fr"), ("de","en"), ("en","de"),
              ("es","en"), ("en","es")]
PAIRS_PER_DIRECTION = 2  # 10 directions x 2 = 20 (marge > 15 requis)
MIN_LEN, MAX_LEN = 60, 220  # caractères : ni trivial ni interminable


def main(out: str, seed: int) -> None:
    from datasets import load_dataset  # import tardif : message d'erreur plus clair
    rng = random.Random(seed)

    # 'facebook/flores' exposes each language by configuration; dev = 997 aligned sentences
    texts = {}
    for short, code in LANGS.items():
        ds = load_dataset("facebook/flores", code, split="dev",
                          trust_remote_code=True)
        texts[short] = [row["sentence"] for row in ds]
        print(f"[flores] {short}: {len(texts[short])} phrases")

    n = len(next(iter(texts.values())))
    assert all(len(v) == n for v in texts.values()), "corpus non alignés"

    eligible = [i for i in range(n)
                if all(MIN_LEN <= len(texts[l][i]) <= MAX_LEN for l in LANGS)]
    rng.shuffle(eligible)

    cases, cursor = [], 0
    for src, tgt in DIRECTIONS:
        for k in range(PAIRS_PER_DIRECTION):
            idx = eligible[cursor]; cursor += 1
            cases.append({
                "id": f"E9-F{len(cases)+1:02d}",
                "direction": f"{src}->{tgt}",
                "source": "flores",
                "statut": "valide",   # traduction professionnelle = référence
                "flores_index": idx,
                "input_text": texts[src][idx],
                "reference": texts[tgt][idx],
            })

    with open(out, "w", encoding="utf-8") as f:
        yaml.safe_dump(cases, f, allow_unicode=True, sort_keys=False, width=100)
    print(f"[ok] {len(cases)} paires écrites dans {out}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--out", default="../golden/e9_flores.yaml")
    p.add_argument("--seed", type=int, default=42)
    a = p.parse_args()
    main(a.out, a.seed)
