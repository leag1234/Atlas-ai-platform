"""Genre routing on varied requests (classification only): python classify_eval.py."""

import json

from longform import request as req
from longform.llm import Client

CASES = [
    ("quick", "Quelle heure est-il à Tokyo quand il est midi à Paris ?"),
    ("quick", "whats the capital of australia"),
    ("quick", "Combien de litres dans un gallon américain ?"),
    ("quick", "Is it safe to put a cast iron pan in the dishwasher?"),
    ("quick", "Traduis 'merci pour ton aide' en allemand"),
    ("quick", "Explique-moi en deux phrases ce qu'est un taux d'intérêt."),
    ("quick", "c koi la diff entre virus et bactérie en gros ?"),
    ("long", "Explique moi en détail comment fonctionne une centrale nucléaire à eau pressurisée, avec un schéma du circuit primaire et secondaire."),
    ("long", "Can you write a thorough comparison of PostgreSQL, MySQL and SQLite for a small SaaS: performance, operations, licensing, backups? Table please and a recommendation."),
    ("long", "Rapport : faut-il équiper notre flotte de 12 utilitaires en électrique ? 1. coûts sur 6 ans 2. autonomie et recharge 3. aides disponibles 4. recommandation"),
    ("long", "I'm a teacher. Explain step by step how vaccines train the immune system for 15-year-olds, with a diagram."),
    ("either", "Comment marche le GPS ?"),
    ("either", "Why is the sky blue?"),
]

if __name__ == "__main__":
    client = Client("classify_eval", 0.2)
    rows = []
    for expected, text in CASES:
        sig = req.signals(text)
        exp = req.expectations(text)
        decision = req.classify(client, text, sig, exp)
        ok = expected == "either" or (expected == "quick") == (decision["genre"] == "quick")
        rows.append({"expected": expected, "genre": decision["genre"], "depth": decision["depth"], "ok": ok, "rules": decision.get("violations_first_pass"), "text": text})
        print(("OK  " if ok else "BAD ") + f"{expected:6} -> {decision['genre']:11} {decision['depth']:6} {text[:70]}")
    json.dump(rows, open("out/classify_eval.json", "w"), ensure_ascii=False, indent=1)
    print("cost", round(client.spent, 4))
