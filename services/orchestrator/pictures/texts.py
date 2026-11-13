"""What the picture turn says, in the five interface languages."""

from __future__ import annotations

TEXTS: dict[str, dict[str, str]] = {
    "en": {
        "intro": "Here are {n} proposals; the first, framed, is the one I suggest.",
        "single": "Here is the picture.",
        "partial_one": "Only one of the four drafts could be finished.",
        "partial": "Only {n} of the four drafts could be finished.",
        "hint": (
            'Click "Picture 2" (for example) to choose it and see it full size, '
            'or answer "the second". Without a choice, the first one is used for '
            "any change."
        ),
        "links": "Choose:",
        "label": "Picture {n}",
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
        "changed": "Here is the changed picture.",
        "redrawn": "The stored drawing was too large to edit: the picture was drawn again from your first request with this change.",
        "changing": "Changing the picture…",
    },
    "fr": {
        "intro": (
            "Voici {n} propositions ; la première, encadrée, est celle que je suggère."
        ),
        "single": "Voici l'image.",
        "partial_one": "Une seule des quatre esquisses a pu être terminée.",
        "partial": "Seules {n} des quatre esquisses ont pu être terminées.",
        "hint": (
            "Cliquez par exemple sur « Image 2 » pour la choisir et la voir en "
            "grand, ou répondez « la deuxième ». Sans choix, la première sert pour "
            "la suite."
        ),
        "links": "Choisir :",
        "label": "Image {n}",
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
        "changed": "Voici l'image modifiée.",
        "redrawn": "Le dessin conservé était trop volumineux pour être retouché : l'image a été redessinée à partir de votre première demande avec cette modification.",
        "changing": "Modification de l'image…",
    },
    "de": {
        "intro": (
            "Hier sind {n} Vorschläge; der erste, umrahmt, ist der, den ich empfehle."
        ),
        "single": "Hier ist das Bild.",
        "partial_one": "Nur einer der vier Entwürfe konnte fertiggestellt werden.",
        "partial": "Nur {n} der vier Entwürfe konnten fertiggestellt werden.",
        "hint": (
            "Klicken Sie zum Beispiel auf „Bild 2“, um es zu wählen und groß zu "
            "sehen, oder antworten Sie „das zweite“. Ohne Wahl gilt das erste für "
            "weitere Änderungen."
        ),
        "links": "Wählen:",
        "label": "Bild {n}",
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
        "changed": "Hier ist das geänderte Bild.",
        "redrawn": "Die gespeicherte Zeichnung war zu groß zum Bearbeiten: Das Bild wurde aus Ihrer ersten Anfrage mit dieser Änderung neu gezeichnet.",
        "changing": "Bild wird geändert…",
    },
    "es": {
        "intro": "Aquí tienes {n} propuestas; la primera, enmarcada, es la que sugiero.",
        "single": "Aquí está la imagen.",
        "partial_one": "Solo se pudo terminar uno de los cuatro bocetos.",
        "partial": "Solo se pudieron terminar {n} de los cuatro bocetos.",
        "hint": (
            "Haz clic por ejemplo en «Imagen 2» para elegirla y verla en grande, o "
            "responde «la segunda». Sin elección, la primera se usa para los "
            "cambios."
        ),
        "links": "Elegir:",
        "label": "Imagen {n}",
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
        "changed": "Aquí está la imagen modificada.",
        "redrawn": "El dibujo guardado era demasiado grande para retocarlo: la imagen se ha vuelto a dibujar a partir de su primera petición con este cambio.",
        "changing": "Modificando la imagen…",
    },
    "it": {
        "intro": "Ecco {n} proposte; la prima, incorniciata, è quella che suggerisco.",
        "single": "Ecco l'immagine.",
        "partial_one": "È stato possibile completare solo uno dei quattro bozzetti.",
        "partial": "È stato possibile completare solo {n} dei quattro bozzetti.",
        "hint": (
            "Fai clic per esempio su «Immagine 2» per sceglierla e vederla in "
            "grande, oppure rispondi «la seconda». Senza scelta, la prima vale per "
            "le modifiche."
        ),
        "links": "Scegli:",
        "label": "Immagine {n}",
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
        "changed": "Ecco l'immagine modificata.",
        "redrawn": "Il disegno salvato era troppo grande da ritoccare: l'immagine è stata ridisegnata dalla prima richiesta con questa modifica.",
        "changing": "Modifica dell'immagine…",
    },
}


def text(language: str, key: str, **values: object) -> str:
    table = TEXTS.get(language, TEXTS["en"])
    return table[key].format(**values)
