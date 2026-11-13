"""The painter's instructions: what the text model is asked to write.

The texts are the measured ones, verbatim: the round 10 baseline (system,
request, naming of the groups), the face block of round 17a, the body block
of round 22 (BODY4) and the animal block of round 23 (ANIMAL2, provisional,
sent only when the animal kit is on). Diagrams add one block (labels by
code, not measured on SVG drafts yet). Prompts live in code, not in
prompts/ (the M25 gate hashes that folder).
"""

from __future__ import annotations

from typing import Any

from services.orchestrator.pictures.checklist import painter_ids

# Sampling of the four drafts (measured at 0.7) and their output room.
TEMPERATURE = 0.7
MAX_TOKENS = 12000
DRAFTS = 4

FREE_SYSTEM = (
    "You are a painter who makes pictures by writing SVG by hand. Return one "
    'complete SVG document and nothing else: viewBox="0 0 1024 1024" width="1024" '
    'height="1024", no external resource, no script, no <image>, no '
    "foreignObject. Text only if the request asks for a title (font-family "
    '"Liberation Sans").'
)

FREE_USER = (
    "Paint this: {subject}\nAim for a finished painting with light, shadow and "
    "depth (gradients and blur filters are allowed), not a pictogram. Every "
    "element of the request must be present and correct."
)

NAMED = (
    "\nNaming, for the program that reads your SVG (it changes nothing in how you "
    "paint): put every thing the request names in its own group, whole (its body "
    "and all that belongs to it), with exactly these ids; give each limb and each "
    "countable part of a character its own sub-group (the id of the thing, the "
    'part, a number: for instance <g id="NAME-leg-1">). Everything else you add '
    "(background, ground, light, shadows) stays outside these groups.\n{ids}"
)

HEAD = (
    "\nFaces, for the program that finishes your picture: for every living "
    "character (person or animal), put its head in its own sub-group <g "
    'id="NAME-head"> holding the head shape (skull, ears, hair, beard), with the '
    "face features (eyes, brows, nose or muzzle, mouth, cheeks) in a sub-group <g "
    'id="NAME-face"> inside it. Anything worn on the head goes in <g '
    'id="NAME-hat">, anything worn over the eyes in <g id="NAME-eyewear">, both '
    "drawn after the head. NAME is the id of the character (NAME-1-head for "
    "numbered ones). On every head group put these attributes, each with one "
    "value from its list:\ndata-facing: front, three-quarter-left, "
    "three-quarter-right, profile-left, profile-right, back (where the face "
    "turns, as seen in the picture)\ndata-expression: neutral, smile, laugh, "
    "surprise, sad, angry, asleep, focused\ndata-gaze: forward, left, right, up, "
    "down\ndata-species: human, feline, canine, bovine, ovine, porcine, ursine, "
    "lagomorph, bird, other\ndata-age: child, adult, old\nFor instance <g "
    'id="girl-head" data-facing="three-quarter-right" data-expression="smile" '
    'data-gaze="forward" data-species="human" data-age="child">. If the request '
    "covers the eyes (a hat pulled over the face, a mask, dark glasses), add "
    'data-covers="eyes" to the group that covers them.'
)

BODY4 = (
    "\nPeople, for the program that draws their bodies: for every person (human "
    "character), do NOT draw the body, neck, arms, legs, hands, feet or clothes; "
    "the program draws them from a pose you declare, in your colours. The "
    'person\'s group <g id="NAME"> holds only:\n1. <desc class="pose"> with one '
    "JSON object (format below), as its first child;\n2. the head group NAME-head "
    "(with NAME-face inside it, and NAME-hat, NAME-eyewear as said below), drawn "
    "where the head goes, at the size you like (the program sets its final "
    "size).\nThings the person holds or uses keep their own groups, outside the "
    "person's group. Any limb sub-groups asked above do not apply to people.\nPose "
    'JSON keys:\n- "facing": front, three-quarter-left, three-quarter-right, '
    "profile-left, profile-right or back (where the chest turns, as seen in the "
    'picture)\n- "build": child, adult or elderly; "shape": slim, average, broad '
    'or round; "skin": "#rrggbb"\n- "joints": canvas coordinates [x, y] of head '
    "(centre), neck (base of the neck, between the shoulders), near_shoulder, "
    "far_shoulder, near_elbow, far_elbow, near_wrist, far_wrist, near_hip, "
    'far_hip, near_knee, far_knee, near_ankle, far_ankle. "near" is the limb '
    "closer to the viewer; in a front or back view, near is the one on the left "
    "of the picture. Put each joint where the pose needs it: a bent elbow or knee "
    "out of the line between its ends, a raised arm with the wrist above the "
    "shoulder, a seated person with the knees about level with the hips, a "
    'walking or running person with the legs apart.\n- "clothes": {"top": {"type": '
    't-shirt, shirt, sweater, jacket, coat, dress, tank-top or robe, "color": '
    '"#rrggbb"}, "sleeves": long, short or none, "bottom": {"type": trousers, '
    'shorts, skirt, long-skirt or none, "color"}, "shoes": {"type": shoes, boots, '
    'sneakers, sandals or bare, "color"}, "extra": [{"type": apron, scarf, belt, '
    'vest or gloves, "color"}]}\n- "relations": for each thing (by its group id) '
    "that the person is in, on, behind, in front of, holds or overlaps in the "
    'picture: [{"thing": the thing\'s id, "rel": in, on, behind, in-front-of, '
    "holding, beside, lying-on or lying-in}]. in: the thing's sides hide the "
    "lower body (a car, a bathtub); behind: the thing stands between the viewer "
    "and the person and hides the lower body (a desk, a fence); in-front-of: the "
    "person is seen in front of it; lying-on / lying-in: the person lies along it "
    "(lying-in: inside it or under its covers, as in a sleeping bag), head at its "
    'head end; for a lying person put the joints along it.\n- "contacts": where '
    'the person sits on, holds, steps on or touches something: [{"joint": "hips" '
    'or a joint name, "kind": sit, hold, step or touch, "anchor": an anchor id}]. '
    "An anchor is a small invisible marker you put inside that thing's own group "
    "at the exact spot (the seat, the handle, the pedal, the step): <circle "
    'id="anchor-WORD" cx=".." cy=".." r="3" fill="none"/>. To touch another '
    'person, write "anchor": "OTHERNAME:near_wrist" (a joint of the other '
    "person).\nFor instance, for a child sitting on a swing and holding its ropes: "
    'in the swing\'s group <circle id="anchor-swing-seat" cx="500" cy="575" r="3" '
    'fill="none"/> and <circle id="anchor-swing-rope" cx="556" cy="455" r="3" '
    'fill="none"/>; then\n<g id="girl"><desc class="pose">{"facing": '
    '"profile-right", "build": "child", "shape": "average", "skin": "#f1c27d", '
    '"joints": {"head": [522, 380], "neck": [515, 430], "near_shoulder": [517, '
    '445], "far_shoulder": [510, 443], "near_elbow": [548, 498], "far_elbow": '
    '[541, 494], "near_wrist": [556, 455], "far_wrist": [551, 452], "near_hip": '
    '[502, 566], "far_hip": [496, 562], "near_knee": [572, 576], "far_knee": '
    '[566, 572], "near_ankle": [588, 650], "far_ankle": [578, 655]}, "clothes": '
    '{"top": {"type": "t-shirt", "color": "#e05a47"}, "sleeves": "short", '
    '"bottom": {"type": "shorts", "color": "#3b5998"}, "shoes": {"type": '
    '"sneakers", "color": "#ffffff"}, "extra": []}, "contacts": [{"joint": '
    '"hips", "kind": "sit", "anchor": "anchor-swing-seat"}, {"joint": '
    '"near_wrist", "kind": "hold", "anchor": "anchor-swing-rope"}, {"joint": '
    '"far_wrist", "kind": "hold", "anchor": "anchor-swing-rope"}], "relations": '
    '[{"thing": "swing", "rel": "on"}]}</desc><g id="girl-head" '
    'data-facing="profile-right" ...>...</g></g>'
)

ANIMAL2 = (
    "\nAnimals, for the program that draws their bodies: for every animal of these "
    "families, do NOT draw its body, neck, legs, wings, tail or coat; the program "
    "draws them from a pose you declare, in your colours. Families: canine (dogs, "
    "wolves, foxes), equine (horses, ponies, donkeys, zebras), bovine (cows, "
    "bulls, oxen), ovine-caprine (sheep, goats), suid (pigs, boars), elephant, "
    "giraffe. Any other creature (a cat, a bear, a rabbit, a bird, a fish, an "
    "insect, a dragon...) you draw yourself as before. The animal's group <g "
    'id="NAME"> holds only:\n1. <desc class="animal"> with one JSON object (format '
    "below), as its first child;\n2. the head group NAME-head (with NAME-face "
    "inside it; ears, horns, forelock, beak or trunk belong to the head), drawn "
    "where the head goes, seen from the side when the body is seen from the side "
    "(the program sets its final size and joins it to the neck).\nAnimal JSON "
    'keys:\n- "family": one from the list above\n- "pose": standing, walking, '
    "running, jumping, sitting, lying, rearing (up on the hind legs) or flying\n- "
    '"facing": profile-left, profile-right, three-quarter-left, '
    "three-quarter-right, front or back (where the head end of the body points, "
    'as seen in the picture)\n- "coat": {"color": "#rrggbb", "belly": "#rrggbb", '
    '"pattern": plain, spots, stripes or patches, "pattern_color": "#rrggbb", '
    '"fur": short, long, fluffy, wool or feathers, "mane": "#rrggbb" (mane and '
    'tail hair), "feet": "#rrggbb" (hooves, paws or bird feet)}\n- "tail": short, '
    'thin, bushy, curly, hair, tuft, fan or none\n- "joints": canvas coordinates '
    "[x, y] of head (centre), neck_base (where the neck leaves the chest), "
    "withers (top of the shoulders), croup (top of the hips), tail_base, "
    "tail_tip, and for each of the legs near_front, far_front, near_hind, "
    "far_hind: the front legs near_front_elbow, near_front_knee, near_front_foot "
    "(same for far_front), the hind legs near_hind_knee, near_hind_hock, "
    "near_hind_foot (same for far_hind). The foot is where the hoof or paw "
    "touches the ground; near is the leg closer to the viewer. Put the joints "
    "where the pose needs them: a walking animal with its legs apart in diagonal "
    "pairs, a running or jumping one with the legs stretched forward and back, a "
    "sitting dog or cat with the croup low and the hind feet under it, a lying "
    "animal with its body along the ground, a flying bird with the wing tips "
    'spread.\n- "relations" and "contacts": as for people (an animal on a rock, in '
    "a basket, behind a fence, lying on a sofa).\nA person riding an animal writes "
    'in its own contacts {"joint": "hips", "kind": "sit", "anchor": '
    '"anchor-NAME-back"} (NAME is the animal\'s id): the program puts that anchor '
    "on the animal's back. Reins or a leash are a thing of their own, with "
    "anchors.\nFor instance, for a grey donkey walking to the right:\n<g "
    'id="donkey"><desc class="animal">{"family": "equine", "pose": "walking", '
    '"facing": "profile-right", "coat": {"color": "#9a948c", "belly": "#d9d4cc", '
    '"pattern": "plain", "fur": "short", "mane": "#4a4540", "feet": "#3a3530"}, '
    '"tail": "tuft", "joints": {"head": [655, 350], "neck_base": [565, 440], '
    '"withers": [535, 420], "croup": [365, 425], "tail_base": [350, 435], '
    '"tail_tip": [335, 560], "near_front_elbow": [540, 515], "near_front_knee": '
    '[560, 585], "near_front_foot": [572, 660], "far_front_elbow": [525, 512], '
    '"far_front_knee": [515, 585], "far_front_foot": [505, 660], '
    '"near_hind_knee": [400, 520], "near_hind_hock": [362, 590], '
    '"near_hind_foot": [372, 660], "far_hind_knee": [418, 515], "far_hind_hock": '
    '[398, 585], "far_hind_foot": [430, 660]}, "relations": [], "contacts": '
    '[]}</desc><g id="donkey-head" data-facing="profile-right" '
    'data-species="other" ...>...</g></g>'
)

LABELS = (
    "\nLabels, for the program that writes them: write no label, caption or "
    "name of a part yourself. Put every part to be named in its own group "
    "with its name in a data-label attribute, as it must appear, in the "
    "language of the request, at most four words: "
    '<g id="part-WORD" data-label="Name">. The program writes the names in the '
    "left and right margins with leader lines to the parts: keep the subject "
    "in the middle three fifths of the picture."
)
_LABEL_LINE = '- every label is a <text> element inside <g id="{id}">'
_LABEL_BY_CODE = (
    "- {words}: no <text>; each named part in its own group with data-label "
    "(see Labels below)"
)


def has_labels(ck: dict[str, Any]) -> bool:
    """Labels by code: the request asks for labels, or for a diagram (the
    checklist does not always list "the names of the parts" as labels)."""
    return ck.get("genre") == "diagram" or any(
        thing.get("kind") == "labels" for thing in ck.get("things", [])
    )


def labels_group(ck: dict[str, Any]) -> str:
    """Id of the group the labels by code go in (the checklist's labels thing)."""
    return next(
        (str(t["id"]) for t in ck.get("things", []) if t.get("kind") == "labels"),
        "labels",
    )


def ids_text(ck: dict[str, Any]) -> str:
    """The id list of the request, with labels left to the program."""
    text = painter_ids(ck)
    for thing in ck.get("things", []):
        if thing.get("kind") == "labels":
            text = text.replace(
                _LABEL_LINE.format(id=thing["id"]),
                _LABEL_BY_CODE.format(words=thing.get("words", "")),
            )
    return text


def messages(
    request: str, ck: dict[str, Any], *, animal_kit: bool
) -> list[dict[str, str]]:
    """System and user messages of one draft."""
    user = FREE_USER.format(subject=request) + NAMED.format(ids=ids_text(ck))
    user += HEAD + BODY4
    if animal_kit:
        user += ANIMAL2
    if has_labels(ck):
        user += LABELS
    return [
        {"role": "system", "content": FREE_SYSTEM},
        {"role": "user", "content": user},
    ]
