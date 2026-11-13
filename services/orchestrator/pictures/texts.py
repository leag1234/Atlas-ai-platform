"""What the picture turn says, in the five interface languages."""

from __future__ import annotations

TEXTS: dict[str, dict[str, str]] = {
    "en": {
        "intro": "Here are {n} proposals; the first, framed, is the one I suggest.",
        "single": "Here is the picture.",
        "partial_one": "Only one of the four drafts could be finished.",
        "partial": "Only {n} of the four drafts could be finished.",
        "hint": (
            "Click a number to choose a picture and see it full size, or answer "
            'for example "the second". Without a choice, the first one is used '
            "for any change."
        ),
        "links": "Pictures:",
        "sheet": "Proposals 1 to {n}",
        "photo": (
            "These are illustrations drawn by code, not photographs: no realistic "
            "photo is made here."
        ),
        "chosen": "Picture {n} is chosen; changes will apply to it.",
        "missing": "The last proposals have {count} pictures: there is no picture {n}.",
        "expired": "These proposals are no longer stored (30-day retention).",
        "reading": "Reading the request…",
        "drawing": "Drawing four proposals…",
        "drawing_done": "Drawing four proposals… {done} of 4 finished",
        "finishing": "Finishing the pictures…",
    },
    "fr": {
        "intro": (
            "Voici {n} propositions ; la première, encadrée, est celle que je suggère."
        ),
        "single": "Voici l'image.",
        "partial_one": "Une seule des quatre esquisses a pu être terminée.",
        "partial": "Seules {n} des quatre esquisses ont pu être terminées.",
        "hint": (
            "Cliquez sur un numéro pour choisir une image et la voir en grand, ou "
            "répondez par exemple « la deuxième ». Sans choix, la première sert "
            "pour la suite."
        ),
        "links": "Images :",
        "sheet": "Propositions 1 à {n}",
        "photo": (
            "Ce sont des illustrations dessinées par du code, pas des photographies : "
            "aucune photo réaliste n'est faite ici."
        ),
        "chosen": "L'image {n} est choisie ; les modifications s'appliqueront à elle.",
        "missing": (
            "Les dernières propositions comptent {count} images : il n'y a pas "
            "d'image {n}."
        ),
        "expired": (
            "Ces propositions ne sont plus conservées (conservation de 30 jours)."
        ),
        "reading": "Lecture de la demande…",
        "drawing": "Dessin de quatre propositions…",
        "drawing_done": "Dessin de quatre propositions… {done} sur 4 terminées",
        "finishing": "Finition des images…",
    },
    "de": {
        "intro": (
            "Hier sind {n} Vorschläge; der erste, umrahmt, ist der, den ich empfehle."
        ),
        "single": "Hier ist das Bild.",
        "partial_one": "Nur einer der vier Entwürfe konnte fertiggestellt werden.",
        "partial": "Nur {n} der vier Entwürfe konnten fertiggestellt werden.",
        "hint": (
            "Klicken Sie auf eine Nummer, um ein Bild zu wählen und groß zu sehen, "
            "oder antworten Sie zum Beispiel „das zweite“. Ohne Wahl gilt das erste "
            "für weitere Änderungen."
        ),
        "links": "Bilder:",
        "sheet": "Vorschläge 1 bis {n}",
        "photo": (
            "Das sind mit Code gezeichnete Illustrationen, keine Fotos: realistische "
            "Fotos entstehen hier nicht."
        ),
        "chosen": "Bild {n} ist gewählt; Änderungen gelten für dieses Bild.",
        "missing": "Die letzten Vorschläge haben {count} Bilder: es gibt kein Bild {n}.",
        "expired": (
            "Diese Vorschläge sind nicht mehr gespeichert (Aufbewahrung 30 Tage)."
        ),
        "reading": "Anfrage wird gelesen…",
        "drawing": "Vier Vorschläge werden gezeichnet…",
        "drawing_done": "Vier Vorschläge werden gezeichnet… {done} von 4 fertig",
        "finishing": "Bilder werden fertiggestellt…",
    },
    "es": {
        "intro": "Aquí tienes {n} propuestas; la primera, enmarcada, es la que sugiero.",
        "single": "Aquí está la imagen.",
        "partial_one": "Solo se pudo terminar uno de los cuatro bocetos.",
        "partial": "Solo se pudieron terminar {n} de los cuatro bocetos.",
        "hint": (
            "Haz clic en un número para elegir una imagen y verla en grande, o "
            "responde por ejemplo «la segunda». Sin elección, la primera se usa "
            "para los cambios."
        ),
        "links": "Imágenes:",
        "sheet": "Propuestas 1 a {n}",
        "photo": (
            "Son ilustraciones dibujadas con código, no fotografías: aquí no se "
            "hacen fotos realistas."
        ),
        "chosen": "La imagen {n} está elegida; los cambios se aplicarán a ella.",
        "missing": (
            "Las últimas propuestas tienen {count} imágenes: no hay imagen {n}."
        ),
        "expired": "Estas propuestas ya no se conservan (conservación de 30 días).",
        "reading": "Leyendo la petición…",
        "drawing": "Dibujando cuatro propuestas…",
        "drawing_done": "Dibujando cuatro propuestas… {done} de 4 terminadas",
        "finishing": "Terminando las imágenes…",
    },
    "it": {
        "intro": "Ecco {n} proposte; la prima, incorniciata, è quella che suggerisco.",
        "single": "Ecco l'immagine.",
        "partial_one": "È stato possibile completare solo uno dei quattro bozzetti.",
        "partial": "È stato possibile completare solo {n} dei quattro bozzetti.",
        "hint": (
            "Fai clic su un numero per scegliere un'immagine e vederla in grande, "
            "oppure rispondi per esempio «la seconda». Senza scelta, la prima vale "
            "per le modifiche."
        ),
        "links": "Immagini:",
        "sheet": "Proposte da 1 a {n}",
        "photo": (
            "Sono illustrazioni disegnate dal codice, non fotografie: qui non si "
            "fanno foto realistiche."
        ),
        "chosen": "L'immagine {n} è scelta; le modifiche si applicheranno a essa.",
        "missing": (
            "Le ultime proposte hanno {count} immagini: non c'è l'immagine {n}."
        ),
        "expired": "Queste proposte non sono più conservate (conservazione di 30 giorni).",
        "reading": "Lettura della richiesta…",
        "drawing": "Disegno di quattro proposte…",
        "drawing_done": "Disegno di quattro proposte… {done} su 4 completate",
        "finishing": "Rifinitura delle immagini…",
    },
}


def text(language: str, key: str, **values: object) -> str:
    table = TEXTS.get(language, TEXTS["en"])
    return table[key].format(**values)
