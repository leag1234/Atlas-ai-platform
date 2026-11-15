"""What the picture turn says, in the five interface languages."""

from __future__ import annotations

import re

LANGUAGES = ("en", "fr", "de", "es", "it")

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
        "cpu_intro": "Here are {n} proposals.",
        "cpu_partial_one": "Only one of the two pictures could be made.",
        "cpu_photo": "These are illustrations, not photographs: no realistic photo is made here.",
        "cpu_drawing": "Drawing two proposals…",
        "cpu_drawing_done": "Drawing two proposals… {done} of 2 finished",
        "cpu_refused": "I can't draw this: the request asks for sexual, violent, cruel or hateful content. I can draw something else.",
        "cpu_filtered": "The pictures drawn for this request did not pass the content check, so none is shown. You can rephrase the request.",
        "cpu_check_one": "An automatic check found that this picture may not show {items}.",
        "cpu_check_all": "An automatic check found that these pictures may not show {items}.",
        "cpu_check_some": "An automatic check found that picture {n} may not show {items}.",
        "cpu_check_unknown": "The image model does not always follow exact counts and unusual details, and they could not be checked this time: {items}.",
        "cpu_variation": "I found nothing precise to change in the description, so this is a new draw of the same description (another random start), not a corrected picture. Tell me what should change and I will change exactly that.",
        "cpu_same_image": "The image model drew exactly the same picture again: nothing changed, and version {version} stays as it is. Tell me precisely what should change.",
        "same_drawing": "The drawing came back identical: nothing changed, and version {version} stays as it is. Tell me precisely what should change.",
        "reviewing": "Looking at the picture…",
        "review_seen": "I looked at the current picture (version {version}).",
        "review_unseen": "I could not look at the picture itself this time: what follows only compares your requests with the description it was drawn from.",
        "review_shown": "It shows: {items}.",
        "review_missing": "Not shown, or wrong: {items}.",
        "review_other": "Other defects I see: {items}.",
        "review_plan": "What I propose to change: {items}.",
        "review_none": "I see nothing that differs from what you asked.",
        "review_asked": "What you asked for: {items}.",
        "review_next": "Say “go ahead” and I will redraw the picture with these corrections, or tell me what to change.",
        "review_method_cpu": "How changes work here: each change rewrites the picture's description and draws it again, so details the description does not fix can move.",
        "review_method_edit": "How changes work here: each change edits the current picture with the image model, and the rest is kept as it is.",
        "review_method_code": "How changes work here: each change asks the drawing model to edit its own drawing.",
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
        "cpu_intro": "Voici {n} propositions.",
        "cpu_partial_one": "Une seule des deux images a pu être faite.",
        "cpu_photo": "Ce sont des illustrations, pas des photographies : aucune photo réaliste n'est faite ici.",
        "cpu_drawing": "Dessin de deux propositions…",
        "cpu_drawing_done": "Dessin de deux propositions… {done} sur 2 terminées",
        "cpu_refused": "Je ne peux pas dessiner cela : la demande porte sur un contenu sexuel, violent, cruel ou haineux. Je peux dessiner autre chose.",
        "cpu_filtered": "Les images dessinées pour cette demande n'ont pas passé le contrôle de contenu : aucune n'est montrée. Vous pouvez reformuler la demande.",
        "cpu_check_one": "Vérification automatique : cette image ne montre peut-être pas {items}.",
        "cpu_check_all": "Vérification automatique : ces images ne montrent peut-être pas {items}.",
        "cpu_check_some": "Vérification automatique : l'image {n} ne montre peut-être pas {items}.",
        "cpu_check_unknown": "Le modèle d'images ne respecte pas toujours les nombres exacts et les détails inhabituels, et ils n'ont pas pu être vérifiés cette fois : {items}.",
        "cpu_variation": "Je n'ai trouvé aucune correction précise à apporter à la description : voici donc un nouveau tirage de la même description (un autre départ aléatoire), pas une image corrigée. Dites-moi ce qui doit changer et je changerai exactement cela.",
        "cpu_same_image": "Le modèle d'images a redessiné exactement la même image : rien n'a changé, la version {version} reste telle quelle. Dites-moi précisément ce qui doit changer.",
        "same_drawing": "Le dessin est revenu identique : rien n'a changé, la version {version} reste telle quelle. Dites-moi précisément ce qui doit changer.",
        "reviewing": "Examen de l'image…",
        "review_seen": "J'ai regardé l'image actuelle (version {version}).",
        "review_unseen": "Je n'ai pas pu regarder l'image elle-même cette fois : ce qui suit compare seulement vos demandes avec la description d'après laquelle elle a été dessinée.",
        "review_shown": "Elle montre : {items}.",
        "review_missing": "Absent ou faux : {items}.",
        "review_other": "Autres défauts visibles : {items}.",
        "review_plan": "Ce que je propose de changer : {items}.",
        "review_none": "Je ne vois rien qui diffère de ce que vous avez demandé.",
        "review_asked": "Ce que vous avez demandé : {items}.",
        "review_next": "Dites « vas-y » et je redessinerai l'image avec ces corrections, ou dites-moi ce qu'il faut changer.",
        "review_method_cpu": "Comment fonctionnent les modifications ici : chaque modification réécrit la description de l'image et la redessine, donc les détails que la description ne fixe pas peuvent bouger.",
        "review_method_edit": "Comment fonctionnent les modifications ici : chaque modification retouche l'image actuelle avec le modèle d'images, et le reste est conservé.",
        "review_method_code": "Comment fonctionnent les modifications ici : chaque modification demande au modèle de dessin de retoucher son propre dessin.",
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
        "cpu_intro": "Hier sind {n} Vorschläge.",
        "cpu_partial_one": "Nur eines der zwei Bilder konnte erstellt werden.",
        "cpu_photo": "Das sind Illustrationen, keine Fotos: realistische Fotos entstehen hier nicht.",
        "cpu_drawing": "Zwei Vorschläge werden gezeichnet…",
        "cpu_drawing_done": "Zwei Vorschläge werden gezeichnet… {done} von 2 fertig",
        "cpu_refused": "Das kann ich nicht zeichnen: Die Anfrage verlangt sexuelle, gewalttätige, grausame oder hasserfüllte Inhalte. Ich kann gern etwas anderes zeichnen.",
        "cpu_filtered": "Die für diese Anfrage gezeichneten Bilder haben die Inhaltsprüfung nicht bestanden, daher wird keines gezeigt. Sie können die Anfrage umformulieren.",
        "cpu_check_one": "Automatische Prüfung: Dieses Bild zeigt möglicherweise nicht {items}.",
        "cpu_check_all": "Automatische Prüfung: Diese Bilder zeigen möglicherweise nicht {items}.",
        "cpu_check_some": "Automatische Prüfung: Bild {n} zeigt möglicherweise nicht {items}.",
        "cpu_check_unknown": "Das Bildmodell hält genaue Anzahlen und ungewöhnliche Details nicht immer ein, und sie konnten diesmal nicht geprüft werden: {items}.",
        "cpu_variation": "Ich habe nichts Genaues gefunden, das an der Beschreibung zu ändern wäre: Das ist daher ein neuer Entwurf derselben Beschreibung (ein anderer Zufallsstart), kein korrigiertes Bild. Sagen Sie mir, was sich ändern soll, dann ändere ich genau das.",
        "cpu_same_image": "Das Bildmodell hat genau dasselbe Bild noch einmal gezeichnet: Nichts hat sich geändert, Version {version} bleibt, wie sie ist. Sagen Sie mir genau, was sich ändern soll.",
        "same_drawing": "Die Zeichnung kam unverändert zurück: Nichts hat sich geändert, Version {version} bleibt, wie sie ist. Sagen Sie mir genau, was sich ändern soll.",
        "reviewing": "Bild wird angesehen…",
        "review_seen": "Ich habe mir das aktuelle Bild angesehen (Version {version}).",
        "review_unseen": "Ich konnte mir das Bild selbst diesmal nicht ansehen: Das Folgende vergleicht nur Ihre Wünsche mit der Beschreibung, nach der es gezeichnet wurde.",
        "review_shown": "Es zeigt: {items}.",
        "review_missing": "Fehlt oder ist falsch: {items}.",
        "review_other": "Weitere sichtbare Fehler: {items}.",
        "review_plan": "Was ich ändern würde: {items}.",
        "review_none": "Ich sehe nichts, was von Ihren Wünschen abweicht.",
        "review_asked": "Was Sie sich gewünscht haben: {items}.",
        "review_next": "Sagen Sie „mach weiter“, dann zeichne ich das Bild mit diesen Korrekturen neu, oder sagen Sie mir, was sich ändern soll.",
        "review_method_cpu": "So funktionieren Änderungen hier: Jede Änderung schreibt die Bildbeschreibung neu und zeichnet das Bild neu, daher können Details, die die Beschreibung nicht festlegt, sich verschieben.",
        "review_method_edit": "So funktionieren Änderungen hier: Jede Änderung bearbeitet das aktuelle Bild mit dem Bildmodell, der Rest bleibt erhalten.",
        "review_method_code": "So funktionieren Änderungen hier: Jede Änderung lässt das Zeichenmodell seine eigene Zeichnung bearbeiten.",
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
        "cpu_intro": "Aquí tienes {n} propuestas.",
        "cpu_partial_one": "Solo se pudo hacer una de las dos imágenes.",
        "cpu_photo": "Son ilustraciones, no fotografías: aquí no se hacen fotos realistas.",
        "cpu_drawing": "Dibujando dos propuestas…",
        "cpu_drawing_done": "Dibujando dos propuestas… {done} de 2 terminadas",
        "cpu_refused": "No puedo dibujar esto: la petición pide contenido sexual, violento, cruel u odioso. Puedo dibujar otra cosa.",
        "cpu_filtered": "Las imágenes dibujadas para esta petición no pasaron el control de contenido, así que no se muestra ninguna. Puedes reformular la petición.",
        "cpu_check_one": "Comprobación automática: puede que esta imagen no muestre {items}.",
        "cpu_check_all": "Comprobación automática: puede que estas imágenes no muestren {items}.",
        "cpu_check_some": "Comprobación automática: puede que la imagen {n} no muestre {items}.",
        "cpu_check_unknown": "El modelo de imágenes no siempre respeta los números exactos ni los detalles inusuales, y esta vez no se pudieron comprobar: {items}.",
        "cpu_variation": "No encontré nada concreto que cambiar en la descripción, así que esta es una nueva versión de la misma descripción (otro inicio aleatorio), no una imagen corregida. Dígame qué debe cambiar y cambiaré exactamente eso.",
        "cpu_same_image": "El modelo de imágenes volvió a dibujar exactamente la misma imagen: no ha cambiado nada y la versión {version} se queda como está. Dígame con precisión qué debe cambiar.",
        "same_drawing": "El dibujo volvió idéntico: no ha cambiado nada y la versión {version} se queda como está. Dígame con precisión qué debe cambiar.",
        "reviewing": "Revisando la imagen…",
        "review_seen": "He mirado la imagen actual (versión {version}).",
        "review_unseen": "Esta vez no pude mirar la imagen en sí: lo que sigue solo compara sus peticiones con la descripción a partir de la cual se dibujó.",
        "review_shown": "Muestra: {items}.",
        "review_missing": "Falta o está mal: {items}.",
        "review_other": "Otros defectos visibles: {items}.",
        "review_plan": "Lo que propongo cambiar: {items}.",
        "review_none": "No veo nada que difiera de lo que pidió.",
        "review_asked": "Lo que pidió: {items}.",
        "review_next": "Diga «adelante» y volveré a dibujar la imagen con estas correcciones, o dígame qué cambiar.",
        "review_method_cpu": "Cómo funcionan los cambios aquí: cada cambio reescribe la descripción de la imagen y la vuelve a dibujar, así que los detalles que la descripción no fija pueden moverse.",
        "review_method_edit": "Cómo funcionan los cambios aquí: cada cambio retoca la imagen actual con el modelo de imágenes y el resto se conserva.",
        "review_method_code": "Cómo funcionan los cambios aquí: cada cambio pide al modelo de dibujo que retoque su propio dibujo.",
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
        "cpu_intro": "Ecco {n} proposte.",
        "cpu_partial_one": "È stato possibile fare solo una delle due immagini.",
        "cpu_photo": "Sono illustrazioni, non fotografie: qui non si fanno foto realistiche.",
        "cpu_drawing": "Disegno di due proposte…",
        "cpu_drawing_done": "Disegno di due proposte… {done} su 2 completate",
        "cpu_refused": "Non posso disegnarlo: la richiesta chiede contenuti sessuali, violenti, crudeli o d'odio. Posso disegnare altro.",
        "cpu_filtered": "Le immagini disegnate per questa richiesta non hanno superato il controllo dei contenuti, quindi non ne viene mostrata nessuna. Puoi riformulare la richiesta.",
        "cpu_check_one": "Controllo automatico: questa immagine potrebbe non mostrare {items}.",
        "cpu_check_all": "Controllo automatico: queste immagini potrebbero non mostrare {items}.",
        "cpu_check_some": "Controllo automatico: l'immagine {n} potrebbe non mostrare {items}.",
        "cpu_check_unknown": "Il modello di immagini non rispetta sempre i numeri esatti e i dettagli insoliti, e questa volta non è stato possibile verificarli: {items}.",
        "cpu_variation": "Non ho trovato nulla di preciso da cambiare nella descrizione: questa è quindi una nuova estrazione della stessa descrizione (un altro avvio casuale), non un'immagine corretta. Dimmi cosa deve cambiare e cambierò esattamente quello.",
        "cpu_same_image": "Il modello di immagini ha ridisegnato esattamente la stessa immagine: non è cambiato nulla e la versione {version} resta com'è. Dimmi con precisione cosa deve cambiare.",
        "same_drawing": "Il disegno è tornato identico: non è cambiato nulla e la versione {version} resta com'è. Dimmi con precisione cosa deve cambiare.",
        "reviewing": "Esame dell'immagine…",
        "review_seen": "Ho guardato l'immagine attuale (versione {version}).",
        "review_unseen": "Questa volta non ho potuto guardare l'immagine stessa: quanto segue confronta solo le tue richieste con la descrizione da cui è stata disegnata.",
        "review_shown": "Mostra: {items}.",
        "review_missing": "Manca o è sbagliato: {items}.",
        "review_other": "Altri difetti visibili: {items}.",
        "review_plan": "Cosa propongo di cambiare: {items}.",
        "review_none": "Non vedo nulla che differisca da quanto hai chiesto.",
        "review_asked": "Cosa hai chiesto: {items}.",
        "review_next": "Di' «vai» e ridisegnerò l'immagine con queste correzioni, oppure dimmi cosa cambiare.",
        "review_method_cpu": "Come funzionano le modifiche qui: ogni modifica riscrive la descrizione dell'immagine e la ridisegna, quindi i dettagli che la descrizione non fissa possono spostarsi.",
        "review_method_edit": "Come funzionano le modifiche qui: ogni modifica ritocca l'immagine attuale con il modello di immagini e il resto viene conservato.",
        "review_method_code": "Come funzionano le modifiche qui: ogni modifica chiede al modello di disegno di ritoccare il proprio disegno.",
    },
}


def language_code(value: object) -> str:
    """A language code as a model reply gives it ("es", "ES", "es-ES",
    "pt_BR"), reduced to its lower-case language part; "" when it is not one."""
    if not isinstance(value, str):
        return ""
    code = re.split(r"[-_]", value.strip().lower(), maxsplit=1)[0]
    return code if re.fullmatch(r"[a-z]{2,3}", code) else ""


def answer_language(found: object, fallback: str) -> str:
    """The language of the picture turn's fixed texts.

    The request's own language, as the turn's model call identified it, when it
    is one of the five: a short request carries too little prose for the
    conversation's language detection ("dibujame un gato con sombrero" with an
    English interface was answered in English, nightly 2026-11-15). Otherwise
    the conversation's language (`fallback`), and English for any other one.
    """
    code = language_code(found)
    if code in LANGUAGES:
        return code
    return fallback if fallback in LANGUAGES else "en"


def text(language: str, key: str, **values: object) -> str:
    table = TEXTS.get(language, TEXTS["en"])
    return table[key].format(**values)
