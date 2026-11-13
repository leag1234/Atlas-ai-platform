"""The animal kit (version 1.1, provisional): code draws the body of a declared animal.

For an animal of a family it knows, the painter draws only the head
(<g id="NAME-head">) and declares in <desc class="animal"> its family, pose
(gait), facing, coat, tail, joints, relations and contacts. Per animal:

1. read the JSON (light repair), map the closed vocabularies; a desc that
   cannot be read falls back to the head's species and a standing pose;
   every animal desc is removed so the body kit never reads it;
2. head length HL from the painter's skeleton (median of the measures over
   the family proportions); lengths set to the family proportions, the
   painter's angles kept;
3. support: standing, walking, sitting on the painter's ground line; rearing
   on the hind feet; lying with folded legs, belly on the ground; running,
   jumping and flying where the painter put them;
4. legs by two-bone inverse kinematics with the family's joint directions;
   family silhouette, neck, tail, feet, coat (belly, spots, stripes,
   patches, wool) clipped to the silhouette; birds have their own drawing;
5. the painter's head scaled to HL and moved onto the end of the neck;
6. relations: behind and in under the thing (in: clipped below the thing's
   bottom edge), on, in-front-of and lying over it; a rider's back anchor
   anchor-NAME-back and a saddle cloth when a person sits there.

It runs before the body kit, so the rider's anchor exists when the body kit
seats the rider. The prompt (ANIMAL2) asks for the restricted family list;
an animal outside the known families is left to the painter. Ported from
the spike round 23 (animal kit 1.1), same rules and numbers, except a stable
seed for coat patterns (the spike used Python's per-process string hash).
"""

from __future__ import annotations

import json
import math
import re
import traceback
import xml.etree.ElementTree as ET
import zlib
from collections.abc import Mapping, Sequence
from typing import Any, cast

import numpy as np

from services.orchestrator.pictures.drawing import (
    FACINGS,
    Anchors,
    Layer,
    Style,
    V,
    Vec,
    anchor_points,
    circle_d,
    el,
    ellipse_d,
    find_anchor,
    find_thing,
    fmt,
    head_box,
    hexcol,
    named_part,
    parse_relations,
    reorder,
    rot,
    rounded,
    shade,
    smooth,
    thing_shapes,
    unit,
    wrap_inverse,
)
from services.orchestrator.pictures.svg_doc import (
    CONTAINERS,
    SHAPES,
    Doc,
    find_named,
    mstr,
    tag,
)

FAMILY_ALIAS = {
    "canine": "canine",
    "dog": "canine",
    "wolf": "canine",
    "fox": "canine",
    "puppy": "canine",
    "feline": "feline",
    "cat": "feline",
    "lion": "feline",
    "tiger": "feline",
    "kitten": "feline",
    "leopard": "feline",
    "equine": "equine",
    "horse": "equine",
    "pony": "equine",
    "donkey": "equine",
    "zebra": "equine",
    "foal": "equine",
    "bovine": "bovine",
    "cow": "bovine",
    "bull": "bovine",
    "ox": "bovine",
    "calf": "bovine",
    "cattle": "bovine",
    "ovine-caprine": "ovine",
    "ovine": "ovine",
    "caprine": "ovine",
    "sheep": "ovine",
    "goat": "ovine",
    "lamb": "ovine",
    "suid": "suid",
    "pig": "suid",
    "porcine": "suid",
    "boar": "suid",
    "piglet": "suid",
    "ursine": "ursine",
    "bear": "ursine",
    "small-mammal": "small",
    "small mammal": "small",
    "small": "small",
    "rabbit": "small",
    "hare": "small",
    "mouse": "small",
    "squirrel": "small",
    "hedgehog": "small",
    "lagomorph": "small",
    "rodent": "small",
    "bird": "bird",
    "avian": "bird",
    "elephant": "elephant",
    "giraffe": "giraffe",
}

SPECIES_TO_FAMILY = {
    "feline": "feline",
    "canine": "canine",
    "bovine": "bovine",
    "ovine": "ovine",
    "porcine": "suid",
    "ursine": "ursine",
    "lagomorph": "small",
    "bird": "bird",
}

POSES = (
    "standing",
    "walking",
    "running",
    "jumping",
    "sitting",
    "lying",
    "rearing",
    "flying",
)

POSE_ALIAS = {
    "stand": "standing",
    "walk": "walking",
    "trotting": "walking",
    "trot": "walking",
    "galloping": "running",
    "gallop": "running",
    "run": "running",
    "jump": "jumping",
    "leaping": "jumping",
    "sit": "sitting",
    "seated": "sitting",
    "perched": "sitting",
    "lying-down": "lying",
    "lie": "lying",
    "sleeping": "lying",
    "resting": "lying",
    "fly": "flying",
    "rear": "rearing",
    "standing-up": "rearing",
    "begging": "rearing",
}

TAILS = ("short", "thin", "bushy", "curly", "hair", "tuft", "fan", "none")

FURS = ("short", "long", "fluffy", "wool", "feathers")

PATTERNS = ("plain", "spots", "stripes", "patches")

# Proportions in head lengths (HL = the head, muzzle tip to back of the skull). trunk: withers to croup along the back; depth: withers
# to brisket; fo / ro: point of shoulder ahead of the withers / buttock behind the croup; belly, tuck: depth at mid-belly and at the
# flank as a share of depth; hum, fem: upper bones (shoulder->elbow, hip->stifle); fl: elbow->knee, knee->ground; hl: stifle->hock,
# hock->ground; neck: neck length, nw0/nw1 its width at the base / at the head; tail: tail length; lw: forearm width; foot: kind;
# rise: withers above the croup; sag: back dip at mid-back (negative: arched); hk: head box factor (ears, horns, trunk, beak).
# Sources: equine and canine ratios from the pages listed in the round 23 report (equine conformation: neck = 1.5 head; dog breed
# standards: chest depth 45-50 % of the height at the withers, length 1.0-1.1 x height); the rest are my own estimates, marked so.
PROP: dict[str, dict[str, Any]] = {
    "equine": dict(
        trunk=1.85,
        depth=1.15,
        fo=0.45,
        ro=0.45,
        belly=1.0,
        tuck=0.86,
        hum=0.55,
        fem=0.62,
        fl=(0.72, 0.68),
        hl=(0.82, 0.78),
        neck=1.45,
        nw0=0.95,
        nw1=0.45,
        tail=1.3,
        lw=0.24,
        foot="hoof",
        rise=0.05,
        sag=0.12,
        hk=1.0,
        tailk="hair",
    ),
    "canine": dict(
        trunk=1.75,
        depth=1.15,
        fo=0.40,
        ro=0.30,
        belly=0.82,
        tuck=0.52,
        hum=0.50,
        fem=0.55,
        fl=(0.75, 0.45),
        hl=(0.80, 0.55),
        neck=0.85,
        nw0=0.80,
        nw1=0.45,
        tail=1.25,
        lw=0.22,
        foot="paw",
        rise=0.05,
        sag=0.03,
        hk=1.05,
        tailk="thin",
    ),
    "feline": dict(
        trunk=2.20,
        depth=1.05,
        fo=0.35,
        ro=0.32,
        belly=0.88,
        tuck=0.72,
        hum=0.50,
        fem=0.60,
        fl=(0.75, 0.42),
        hl=(0.85, 0.60),
        neck=0.55,
        nw0=0.75,
        nw1=0.55,
        tail=2.3,
        lw=0.22,
        foot="paw",
        rise=0.0,
        sag=0.05,
        hk=1.15,
        tailk="thin",
    ),
    "bovine": dict(
        trunk=2.40,
        depth=1.45,
        fo=0.40,
        ro=0.32,
        belly=1.05,
        tuck=0.92,
        hum=0.55,
        fem=0.65,
        fl=(0.62, 0.60),
        hl=(0.70, 0.66),
        neck=0.85,
        nw0=1.10,
        nw1=0.70,
        tail=1.7,
        lw=0.26,
        foot="cloven",
        rise=0.0,
        sag=0.0,
        hk=1.15,
        tailk="tuft",
    ),
    "ovine": dict(
        trunk=2.20,
        depth=1.25,
        fo=0.42,
        ro=0.40,
        belly=1.0,
        tuck=0.92,
        hum=0.45,
        fem=0.55,
        fl=(0.62, 0.55),
        hl=(0.70, 0.60),
        neck=0.75,
        nw0=0.85,
        nw1=0.50,
        tail=0.5,
        lw=0.16,
        foot="cloven",
        rise=0.0,
        sag=0.0,
        hk=1.1,
        tailk="short",
    ),
    "suid": dict(
        trunk=2.50,
        depth=1.40,
        fo=0.45,
        ro=0.48,
        belly=1.02,
        tuck=0.95,
        hum=0.40,
        fem=0.45,
        fl=(0.42, 0.36),
        hl=(0.48, 0.38),
        neck=0.25,
        nw0=1.20,
        nw1=1.00,
        tail=0.55,
        lw=0.22,
        foot="cloven",
        rise=0.0,
        sag=-0.10,
        hk=1.0,
        tailk="curly",
    ),
    "ursine": dict(
        trunk=2.30,
        depth=1.45,
        fo=0.45,
        ro=0.48,
        belly=0.98,
        tuck=0.90,
        hum=0.50,
        fem=0.55,
        fl=(0.62, 0.50),
        hl=(0.66, 0.52),
        neck=0.45,
        nw0=1.15,
        nw1=0.85,
        tail=0.22,
        lw=0.36,
        foot="pad",
        rise=0.18,
        sag=0.0,
        hk=1.0,
        tailk="short",
    ),
    "small": dict(
        trunk=1.70,
        depth=1.30,
        fo=0.40,
        ro=0.55,
        belly=1.0,
        tuck=0.95,
        hum=0.30,
        fem=0.40,
        fl=(0.38, 0.30),
        hl=(0.50, 0.38),
        neck=0.20,
        nw0=1.00,
        nw1=0.90,
        tail=0.45,
        lw=0.18,
        foot="paw",
        rise=-0.05,
        sag=-0.30,
        hk=1.35,
        tailk="short",
    ),
    "elephant": dict(
        trunk=2.20,
        depth=1.40,
        fo=0.45,
        ro=0.40,
        belly=1.02,
        tuck=0.95,
        hum=0.60,
        fem=0.65,
        fl=(0.85, 0.75),
        hl=(0.85, 0.72),
        neck=0.25,
        nw0=1.20,
        nw1=1.10,
        tail=1.1,
        lw=0.42,
        foot="pad",
        rise=0.05,
        sag=-0.08,
        hk=1.55,
        tailk="tuft",
    ),
    "giraffe": dict(
        trunk=2.30,
        depth=1.55,
        fo=0.40,
        ro=0.38,
        belly=0.85,
        tuck=0.75,
        hum=0.80,
        fem=0.85,
        fl=(1.75, 1.65),
        hl=(1.65, 1.50),
        neck=3.6,
        nw0=1.00,
        nw1=0.40,
        tail=1.5,
        lw=0.22,
        foot="cloven",
        rise=0.9,
        sag=0.0,
        hk=1.25,
        tailk="tuft",
    ),
    "bird": dict(
        trunk=2.00,
        depth=1.35,
        fo=0.45,
        ro=0.30,
        belly=1.0,
        tuck=1.0,
        hum=0.0,
        fem=0.30,
        fl=(0.0, 0.0),
        hl=(0.75, 0.70),
        neck=0.45,
        nw0=0.80,
        nw1=0.60,
        tail=1.1,
        lw=0.10,
        foot="bird",
        rise=0.0,
        sag=0.0,
        hk=1.35,
        tailk="fan",
        wing=3.4,
    ),
}

DEFAULT_COAT = {
    "equine": "#8b5a2b",
    "canine": "#a0703c",
    "feline": "#d08a3a",
    "bovine": "#f2efe8",
    "ovine": "#f2eee4",
    "suid": "#f2b5b0",
    "ursine": "#6b4426",
    "small": "#a08a70",
    "elephant": "#8e8a88",
    "giraffe": "#e2b066",
    "bird": "#7a7f8a",
}

HEAD_GAIN = 1.25  # dev draws: the painter's head box holds ears, forelock and loose strokes; the head read about 0.75 HL without it

QUAD_J = (
    "near_front_elbow",
    "near_front_knee",
    "near_front_foot",
    "far_front_elbow",
    "far_front_knee",
    "far_front_foot",
    "near_hind_knee",
    "near_hind_hock",
    "near_hind_foot",
    "far_hind_knee",
    "far_hind_hock",
    "far_hind_foot",
)

BASE_J = ("head", "neck_base", "withers", "croup", "tail_base", "tail_tip")

BIRD_J = (
    "near_wing_tip",
    "far_wing_tip",
    "near_hind_knee",
    "near_hind_foot",
    "far_hind_knee",
    "far_hind_foot",
)


def norm(v: Vec) -> float:
    return float(np.hypot(*v))


# ---------------------------------------------------------------- parsing
def loads_loose(text: str | None) -> tuple[Any, int | None]:
    t = (text or "").strip()
    t = t[t.find("{") : t.rfind("}") + 1] if "{" in t else t
    for attempt in range(3):
        try:
            return json.loads(t), attempt
        except json.JSONDecodeError:
            if attempt == 0:
                t = re.sub(r",\s*([}\]])", r"\1", t)
            else:
                t = t.replace("'", '"')
    return None, None


def pick(
    v: object, allowed: Sequence[str], alias: Mapping[str, str] | None, default: str
) -> tuple[str, str | None]:
    s = str(v or "").strip().lower().replace("_", "-")
    if s in allowed:
        return s, None
    if alias and s in alias:
        return alias[s], f"{v}->{alias[s]}"
    for a in allowed:
        if a in s:
            return a, f"{v}->{a}"
    return default, f"{v}->default {default}"


def parse_animal(
    text: str, head: ET.Element | None
) -> tuple[dict[str, Any] | None, list[str], str | None]:
    problems = []
    d, rep = loads_loose(text)
    if rep:
        problems.append("json repaired")
    if not isinstance(d, dict):
        return None, problems, "json unreadable"
    fam_raw = str(d.get("family") or d.get("species") or "").strip().lower()
    fam = FAMILY_ALIAS.get(fam_raw) or next(
        (v for k, v in FAMILY_ALIAS.items() if k in fam_raw), None
    )
    if fam is None and head is not None:
        fam = SPECIES_TO_FAMILY.get((head.get("data-species") or "").strip().lower())
        if fam:
            problems.append(f"family {fam_raw!r} -> from head species {fam}")
    if fam is None:
        return None, problems, f"family not in list: {fam_raw!r}"
    pose, p = pick(d.get("pose") or d.get("gait"), POSES, POSE_ALIAS, "standing")
    if p:
        problems.append("pose " + p)
    facing, p = pick(
        d.get("facing") or (head.get("data-facing") if head is not None else None),
        FACINGS,
        None,
        "profile-right",
    )
    if p:
        problems.append("facing " + p)
    coat: dict[str, Any] = (
        cast(dict[str, Any], d.get("coat")) if isinstance(d.get("coat"), dict) else {}
    )
    color = hexcol(coat.get("color") or d.get("color"), DEFAULT_COAT[fam])
    pat, p = pick(
        coat.get("pattern") or "plain",
        PATTERNS,
        {
            "spotted": "spots",
            "striped": "stripes",
            "patched": "patches",
            "piebald": "patches",
            "dots": "spots",
            "tabby": "stripes",
        },
        "plain",
    )
    if p and coat.get("pattern"):
        problems.append("pattern " + p)
    fur, _ = pick(
        coat.get("fur") or ("feathers" if fam == "bird" else "short"),
        FURS,
        {"woolly": "wool", "fleece": "wool", "fluffy": "fluffy"},
        "short",
    )
    tail, p = pick(
        d.get("tail") or PROP[fam]["tailk"],
        TAILS,
        {
            "long": "thin",
            "fluffy": "bushy",
            "tufted": "tuft",
            "stub": "short",
            "flowing": "hair",
        },
        PROP[fam]["tailk"],
    )
    coat = {
        "color": color,
        "belly": hexcol(coat.get("belly"), None),
        "pattern": pat,
        "pattern_color": hexcol(coat.get("pattern_color"), shade(color, -0.45)),
        "fur": fur,
        "mane": hexcol(coat.get("mane"), shade(color, -0.35)),
        "feet": hexcol(
            coat.get("feet"), shade(color, -0.6) if fam != "bird" else "#e0a030"
        ),
    }
    J = {}
    raw: dict[str, Any] = (
        cast(dict[str, Any], d.get("joints"))
        if isinstance(d.get("joints"), dict)
        else {}
    )
    for k, v in raw.items():
        k2 = str(k).strip().lower().replace("-", "_").replace(" ", "_")
        k2 = (
            k2.replace("_paw", "_foot")
            .replace("_hoof", "_foot")
            .replace("_wrist", "_knee")
            .replace("_carpus", "_knee")
            .replace("_stifle", "_knee")
        )
        k2 = {
            "hips": "croup",
            "hip": "croup",
            "shoulders": "withers",
            "neck": "neck_base",
            "tail": "tail_base",
            "wing_root": "withers",
        }.get(k2, k2)
        try:
            if isinstance(v, (list, tuple)) and len(v) >= 2:
                J[k2] = V(v)
            elif isinstance(v, dict) and "x" in v:
                J[k2] = V((v["x"], v["y"]))
        except (TypeError, ValueError):
            problems.append(f"bad joint {k}")
    rels, rp = parse_relations(d)
    rels = rels or []
    problems += rp
    contacts = []
    for c in d.get("contacts") or []:
        if isinstance(c, dict) and c.get("anchor"):
            contacts.append(
                {
                    "joint": str(c.get("joint") or "")
                    .strip()
                    .lower()
                    .replace("-", "_"),
                    "kind": str(c.get("kind") or "step").lower(),
                    "anchor": str(c["anchor"]).strip(),
                }
            )
    return (
        {
            "family": fam,
            "pose": pose,
            "facing": facing,
            "coat": coat,
            "tail": tail,
            "J": J,
            "relations": rels,
            "contacts": contacts,
        },
        problems,
        None,
    )


# ---------------------------------------------------------------- geometry helpers
def two_bone(
    root: Vec, target: Vec, l1: float, l2: float, prefer: Vec
) -> tuple[Vec, Vec, float]:
    """Mid joint of a two-bone chain root->mid->end reaching target (clamped to reach); prefer: unit vector the mid joint should bulge
    toward. -> (mid, end, stretched share)."""
    d = target - root
    L = norm(d)
    reach = (l1 + l2) * 0.999
    short = abs(l1 - l2) * 1.001 + 1e-3
    miss = 0.0
    if L > reach:
        miss = (L - reach) / reach
        target = root + d / L * reach
        L = reach
    if L < short:
        target = root + unit(d) * short
        L = short
    u = unit(target - root)
    a = (l1 * l1 - l2 * l2 + L * L) / (2 * L)
    h = math.sqrt(max(l1 * l1 - a * a, 0.0))
    n = np.array([-u[1], u[0]])
    if float(n @ prefer) < 0:
        n = -n
    mid = root + u * a + n * h
    return mid, target, miss


def tube(pts: Sequence[Vec], radii: Sequence[float], cap: bool = True) -> str:
    """Closed smooth outline around a polyline with a radius per point."""
    n = len(pts)
    left, right = [], []
    for i in range(n):
        d = unit(pts[min(i + 1, n - 1)] - pts[max(i - 1, 0)])
        nr = np.array([-d[1], d[0]])
        left.append(pts[i] + nr * radii[i])
        right.append(pts[i] - nr * radii[i])
    ring = left
    if cap:
        de = unit(pts[-1] - pts[-2])
        ring = ring + [pts[-1] + de * radii[-1] * 0.8]
    ring = ring + right[::-1]
    if cap:
        d0 = unit(pts[1] - pts[0])
        ring = ring + [pts[0] - d0 * radii[0] * 0.8]
    return smooth(ring)


def chain(points: Sequence[Vec], n: int = 6) -> list[Vec]:
    """Points along a polyline, n per segment, with a parameter per point."""
    out = []
    for k in range(len(points) - 1):
        for i in range(n):
            out.append(points[k] + (points[k + 1] - points[k]) * i / n)
    out.append(points[-1])
    return out


def bezier(p0: Vec, p1: Vec, p2: Vec, n: int = 12) -> list[Vec]:
    return [
        (1 - t) ** 2 * p0 + 2 * (1 - t) * t * p1 + t * t * p2
        for t in np.linspace(0, 1, n)
    ]


# ---------------------------------------------------------------- one animal
class Animal:
    def __init__(
        self, name: str, group: ET.Element, a: dict[str, Any], problems: list[str]
    ) -> None:
        self.name, self.group, self.a, self.problems = name, group, a, problems
        self.P = PROP[a["family"]]
        self.rec = {
            "name": name,
            "family": a["family"],
            "pose": a["pose"],
            "facing": a["facing"],
            "problems": problems,
        }
        self.head_facing: str | None = None
        self.mirrored = False
        # Set by frame(), build(), draw() and one_animal(); declared here for the
        # type checker (sit_ground and inner stay None unless set).
        self.f = self.d = self.W = self.C = self.mid = np.zeros(2)
        self.ax = self.dd = self.body_c = np.zeros(2)
        self.head_attach = self.neck_dir = self.neck_nrm = np.zeros(2)
        self.h = self.T = self.L = 0.0
        self.ground: float | None = None
        self.sit_ground: float | None = None
        self.legs: dict[str, dict[str, Any]] = {}
        self.sil_paths: list[str] = []
        self.neck_top: list[Vec] = []
        self.bird_leg_layers: tuple[Layer, Layer] | None = None
        self.inner: ET.Element | None = None

    # ---- frame and unit
    def frame(self) -> tuple[Vec, Vec]:
        J, a = self.a["J"], self.a
        W, C = J.get("withers"), J.get("croup")
        fsign = -1.0 if a["facing"] in ("profile-left", "three-quarter-left") else 1.0
        if W is not None and C is not None and norm(W - C) > 1e-3:
            f = unit(W - C)
            if a["facing"] in ("front", "back") and abs(f[0]) < 0.2:
                f = np.array([fsign, 0.0])
        else:
            f = np.array([fsign, 0.0])
        # sanity: the head end of the back line is the one nearer the head (the painter sometimes swaps withers and croup)
        hd = J.get("head")
        if (
            hd is not None
            and W is not None
            and C is not None
            and norm(W - C) > 1e-3
            and float((hd - (W + C) / 2) @ f) < -0.05 * norm(W - C)
            and a["family"] != "bird"
        ):
            f = -f
            J["withers"], J["croup"] = C, W
            self.problems.append("withers/croup swapped (head at the croup end)")
        # the body extends behind a head seen from the side: a skeleton on the side the head looks to is mirrored about the head
        hf = (self.head_facing or "").strip().lower()
        hsign = (
            1.0 if hf == "profile-right" else (-1.0 if hf == "profile-left" else 0.0)
        )  # profile heads only (dev: a 3/4 head misled it)
        if (
            hsign
            and hd is not None
            and float(f[0]) * hsign < -0.3
            and a["family"] != "bird"
        ):
            for k in list(J):
                J[k] = np.array([2 * hd[0] - J[k][0], J[k][1]])
            f = np.array([-f[0], f[1]])
            W, C = J.get("withers"), J.get("croup")
            a["facing"] = hf
            self.problems.append("skeleton mirrored to the side behind the head")
            self.mirrored = True
        if (W is None or C is None) and hd is not None:
            f = (
                np.array(
                    [
                        1.0
                        if hd[0]
                        >= (W if W is not None else (C if C is not None else hd))[0]
                        else -1.0,
                        0.0,
                    ]
                )
                if (W is not None or C is not None)
                else f
            )
        # down vector: perpendicular to f, pointing down on the screen
        d = np.array([-f[1], f[0]])
        if d[1] < 0:
            d = -d
        self.f, self.d = f, d
        return f, d

    def estimate_h(self, head_box: list[float] | None) -> float | None:
        P, J = self.P, self.a["J"]
        c = []
        W, C = J.get("withers"), J.get("croup")
        side = self.a["facing"].startswith("profile")
        if W is not None and C is not None and self.a["family"] != "bird":
            L = norm(W - C)
            if side or self.a["facing"].startswith("three"):
                c.append(("trunk", L / (P["trunk"] * (1.0 if side else 0.8))))
        if self.a["family"] == "bird":
            nb = J.get("neck_base")
            if nb is not None and C is not None:
                c.append(("body", norm(nb - C) / (P["trunk"] * 0.9)))
            for s in ("near", "far"):
                wt = J.get(f"{s}_wing_tip")
                if wt is not None and W is not None and self.a["pose"] == "flying":
                    c.append(("wing", norm(wt - W) / P["wing"]))
        elif self.a["pose"] in ("standing", "walking"):
            hw = P["depth"] + P["fl"][0] + P["fl"][1]
            for s in ("near", "far"):
                ft = J.get(f"{s}_front_foot")
                if ft is not None and W is not None and ft[1] > W[1]:
                    c.append(("height_front", (ft[1] - W[1]) / hw))
        hd, nb = J.get("head"), J.get("neck_base")
        if (
            hd is not None
            and nb is not None
            and self.a["family"] not in ("suid", "small", "elephant")
        ):
            c.append(("neck", norm(hd - nb) / (P["neck"] + 0.45)))
        if head_box is not None:
            bw, bh = head_box[2] - head_box[0], head_box[3] - head_box[1]
            fc = self.a["facing"]
            eff = (
                bw
                if fc.startswith("profile")
                else (
                    0.9 * max(bw, 0.75 * bh)
                    if fc.startswith("three")
                    else 0.8 * max(bh, 0.8 * bw)
                )
            )
            c.append(("head", eff / P["hk"]))
        vals = [v for _, v in c if v and v > 1]
        self.rec["h_candidates"] = {k: round(v, 1) for k, v in c}
        if not vals:
            return None
        return float(np.median(vals))

    # ---- skeleton
    def build(self, h: float, head_box: list[float] | None) -> None:
        a, P, J = self.a, self.P, self.a["J"]
        f, d = self.f, self.d
        fam = a["family"]
        self.h = h
        side = a["facing"].startswith("profile")
        three = a["facing"].startswith("three")
        T = P["trunk"] * h * (1.0 if side else (0.75 if three else 0.4))
        if fam == "bird":
            T = P["trunk"] * h * (1.0 if side or a["pose"] == "flying" else 0.85)
        W0, C0 = J.get("withers"), J.get("croup")
        if W0 is None and C0 is None:
            hd = J.get("head")
            if hd is None and head_box is not None:
                hd = V(
                    ((head_box[0] + head_box[2]) / 2, (head_box[1] + head_box[3]) / 2)
                )
            mid = hd - f * (0.6 * T + 0.8 * h) + d * 0.8 * h
            self.problems.append("withers and croup missing: placed from the head")
        elif W0 is None:
            mid = C0 + f * T / 2
        elif C0 is None:
            mid = W0 - f * T / 2
        else:
            mid = (W0 + C0) / 2
        # the back line keeps the painter's slope, the withers rise of the family is added for the side views
        W = mid + f * T / 2
        C = mid - f * T / 2
        rise = P["rise"] * h
        if rise and fam != "bird":
            W = W - d * rise / 2
            C = C + d * rise / 2
        self.W, self.C, self.T = W, C, T
        self.mid = mid
        # sitting quadruped: croup on the ground, back raised so that the straight front legs reach the ground
        feet0 = [
            J.get(k)
            for k in (
                "near_front_foot",
                "far_front_foot",
                "near_hind_foot",
                "far_hind_foot",
            )
            if J.get(k) is not None
        ]
        if (
            a["pose"] == "sitting"
            and fam not in ("bird", "elephant", "giraffe")
            and feet0
        ):
            gy = max(p[1] for p in feet0)
            D = P["depth"] * h
            Lf = (P["hum"] * 0.55 + P["fl"][0] + P["fl"][1]) * h
            sa = max(0.4, min(0.92, (Lf - 0.13 * D) / T))
            ang = math.asin(sa)
            fx = 1.0 if f[0] >= 0 else -1.0
            C = np.array([mid[0] - fx * T * math.cos(ang) / 2, gy - 0.55 * D])
            W = C + np.array([fx * math.cos(ang), -math.sin(ang)]) * T
            self.f = f = unit(W - C)
            dd_ = np.array([-f[1], f[0]])
            self.d = d = dd_ if dd_[1] >= 0 else -dd_
            self.W, self.C, self.mid = W, C, (W + C) / 2
            self.sit_ground = gy
            self.rec["sit_angle"] = round(math.degrees(ang), 1)
        # pose support
        pose = a["pose"]
        feet = [
            J.get(k)
            for k in (
                "near_front_foot",
                "far_front_foot",
                "near_hind_foot",
                "far_hind_foot",
            )
            if J.get(k) is not None
        ]
        ground = max((p[1] for p in feet), default=None)
        self.ground = ground
        self.legs = {}
        self.rec["h"] = round(h, 1)
        self.rec["T"] = round(T, 1)
        if fam == "bird":
            self.build_bird(ground)
            return
        if ground is None and pose in (
            "standing",
            "walking",
            "sitting",
            "rearing",
            "lying",
        ):
            ground = W[1] + (P["depth"] + sum(P["fl"])) * h * 0.95
            self.problems.append("no foot declared: ground set from the family height")
        self.ground = ground
        offs = 0.0
        if pose in ("standing", "walking", "sitting", "rearing"):
            sup = (
                ["near_front", "far_front", "near_hind", "far_hind"]
                if pose != "rearing"
                else ["near_hind", "far_hind"]
            )
            if pose == "sitting":
                sup = ["near_front", "far_front"]
            ratio = 0.975 if pose == "standing" else 0.95
            need = []
            for leg in sup:
                S, E, tgt, l1, l2 = self.leg_plan(leg, pose, ground)
                dx = float(tgt[0] - E[0])
                need.append(
                    ground
                    - E[1]
                    - math.sqrt(max((ratio * (l1 + l2)) ** 2 - dx * dx, 0.0))
                )
            offs = (
                max(need)
                if pose != "sitting" or not self.sit_ground is not None
                else 0.0
            )
        elif pose == "lying":
            low = self.trunk_points()
            bottom = max(p[1] for p in low)
            offs = ground - bottom - 0.02 * h
        self.W, self.C, self.mid = (
            self.W + np.array([0, offs]),
            self.C + np.array([0, offs]),
            self.mid + np.array([0, offs]),
        )
        self.rec["lift"] = round(offs / h, 2)
        for leg in ("near_front", "far_front", "near_hind", "far_hind"):
            self.solve_leg(leg, pose, ground)

    def leg_root(self, leg: str) -> tuple[Vec, float]:
        P, h, f, d = self.P, self.h, self.f, self.d
        front = "front" in leg
        if front:
            S = self.W + f * (P["fo"] * 0.55) * h + d * P["depth"] * 0.42 * h
            L = (P["hum"] * 0.55 + P["fl"][0] + P["fl"][1]) * h
        else:
            S = self.C - f * (P["ro"] * 0.25) * h + d * P["depth"] * 0.30 * h
            L = (P["fem"] * 0.6 + P["hl"][0] + P["hl"][1]) * h
        return S, L

    def leg_plan(
        self, leg: str, pose: str, ground: float | None
    ) -> tuple[Vec, Vec, Vec, float, float]:
        """-> (root, upper joint, foot target, l1, l2) for the current body position."""
        P, h, f, d, J = self.P, self.h, self.f, self.d, self.a["J"]
        front = "front" in leg
        S, L = self.leg_root(leg)
        foot = J.get(f"{leg}_foot")
        l1, l2 = P["fl"] if front else P["hl"]
        l1, l2 = l1 * h, l2 * h
        upper = (P["hum"] if front else P["fem"]) * h
        if foot is None:
            foot = S + d * L * 0.95 + (f * (0.1 if front else -0.1) * h)
            if f"{leg}_foot missing" not in self.problems:
                self.problems.append(f"{leg}_foot missing")
        tgt = V(foot)
        support = pose in ("standing", "walking", "sitting") or (
            pose == "rearing" and not front
        )
        if support and ground is not None:
            # the foot stays near the painter's spot along the body but under its leg: spread limited by the gait
            lim = {"standing": 0.18, "walking": 0.38, "sitting": 0.25, "rearing": 0.3}[
                pose
            ] * (l1 + l2 + upper)
            hx = 1.0 if f[0] >= 0 else -1.0  # along the ground, toward the head end
            if pose == "sitting" and not front and self.sit_ground is not None:
                tgt = np.array(
                    [S[0] + hx * 0.85 * h, ground]
                )  # hind paw forward under the chest
            else:
                fx = max(-lim, min(lim, float(tgt[0] - S[0]) * hx))
                tgt = np.array([S[0] + hx * fx, ground])
        if pose == "lying" and ground is not None:
            hx = 1.0 if f[0] >= 0 else -1.0
            reach = l1 + l2 + upper
            # 1.1: folded legs, sphinx-like: front paws ahead of the chest, hind paws under the belly, on the ground
            tgt = np.array(
                [S[0] + hx * (0.7 if front else 0.45) * reach, ground - 0.05 * h]
            )
        for c in self.a["contacts"]:
            if (
                c["joint"] in (f"{leg}_foot", leg)
                and c["kind"] in ("step", "stand", "hold", "touch")
                and c.get("_p") is not None
                and not self.mirrored
                and norm(c["_p"] - tgt) < 0.6 * (l1 + l2 + upper)
            ):
                tgt = c["_p"]
        swing = math.atan2(
            float((tgt - S) @ f), float((tgt - S) @ d)
        )  # 0 = straight down, + = forward
        swing = max(-1.2, min(1.2, swing))
        sgn = 1 if cross_sign(f, d) > 0 else -1
        if front:  # humerus: from the point of the shoulder back and down to the elbow
            udir = rot(d, (0.32 - 0.5 * swing) * sgn)
        else:  # femur: from the hip forward and down to the stifle
            udir = rot(d, -(0.55 + 0.5 * swing) * sgn)
        E = S + unit(udir) * upper
        return S, E, tgt, l1, l2

    def solve_leg(self, leg: str, pose: str, ground: float | None) -> None:
        f = self.f
        front = "front" in leg
        S, E, tgt, l1, l2 = self.leg_plan(leg, pose, ground)
        prefer = (
            f if front else -f
        )  # front knee (carpus) bulges forward, the hock points back
        mid, end, miss = two_bone(E, tgt, l1, l2, prefer)
        self.legs[leg] = {
            "S": S,
            "E": E,
            "K": mid,
            "F": end,
            "miss": round(miss, 3),
            "front": front,
            "far": leg.startswith("far"),
        }

    def build_bird(self, ground: float | None) -> None:
        J, P, h, f = self.a["J"], self.P, self.h, self.f
        pose = self.a["pose"]
        # body axis: tail base (croup) -> breast (neck base)
        nb, C0 = J.get("neck_base"), J.get("croup")
        if nb is not None and C0 is not None and norm(nb - C0) > 1:
            ax = unit(nb - C0)
        else:
            ax = f
        # keep the axis pointing to the facing side
        fsign = (
            -1.0 if self.a["facing"] in ("profile-left", "three-quarter-left") else 1.0
        )
        if self.a["facing"] not in ("front", "back") and ax[0] * fsign < -0.2:
            ax = np.array([-ax[0], ax[1]])
        self.ax = ax
        dd = np.array([-ax[1], ax[0]])
        if dd[1] < 0 or (abs(dd[1]) < 0.2 and dd[0] * fsign > 0):
            dd = -dd
        self.dd = dd
        L = P["trunk"] * h
        centre = self.mid
        if nb is not None and C0 is not None:
            centre = (nb + C0) / 2
        self.body_c = centre
        self.L = L
        self.legs = {}
        if pose in ("standing", "walking", "sitting") and ground is not None:
            hip = centre + dd * 0.35 * P["depth"] * h - ax * 0.05 * h
            lmax = (P["hl"][0] + P["hl"][1]) * h
            off = ground - hip[1] - lmax * (0.9 if pose != "sitting" else 0.45)
            if pose == "sitting":
                off = min(off, ground - (centre[1] + 0.5 * P["depth"] * h))
            self.body_c = centre + np.array([0, off])
            self.rec["lift"] = round(off / h, 2)
        for s in ("near", "far"):
            hip = (
                self.body_c
                + dd * 0.38 * P["depth"] * h
                + (np.array([0.06 * h, 0]) if s == "far" else 0)
            )
            foot = J.get(f"{s}_hind_foot")
            if pose == "flying":
                if foot is None:
                    continue
                tgt = hip - ax * 0.9 * h + dd * 0.3 * h
            else:
                tgt = (
                    V(foot)
                    if foot is not None
                    else hip + np.array([0, (P["hl"][0] + P["hl"][1]) * h])
                )
                if ground is not None and pose in ("standing", "walking"):
                    tgt = np.array([tgt[0], ground])
            for c in self.a["contacts"]:
                if (
                    c["joint"] in (f"{s}_hind_foot", f"{s}_foot")
                    and c.get("_p") is not None
                    and norm(c["_p"] - tgt) < 1.5 * h
                ):
                    tgt = c["_p"]
            mid, end, miss = two_bone(hip, tgt, P["hl"][0] * h, P["hl"][1] * h, -ax)
            self.legs[s] = {
                "S": hip,
                "K": mid,
                "F": end,
                "miss": round(miss, 3),
                "far": s == "far",
            }

    def trunk_points(self) -> list[Vec]:
        """Closed outline of the trunk (root coords), family silhouette in the body frame."""
        P, h, f, d = self.P, self.h, self.f, self.d
        W, C, T = self.W, self.C, self.T
        D = P["depth"] * h
        fam = self.a["family"]
        k = T / max(P["trunk"] * h, 1e-6)  # foreshortening in 3/4 and front views
        fo, ro = P["fo"] * h * max(k, 0.5), P["ro"] * h * max(k, 0.5)
        bk = W - C

        def at(
            t: float, depth: float
        ) -> Vec:  # t: 0 croup .. 1 withers; depth below the back line
            base = C + bk * t
            return base + d * depth

        sag = P["sag"] * D
        pts = [
            at(0.0, 0.02 * D),  # croup / tail head
            at(0.25, 0.25 * sag),
            at(0.5, sag),  # mid-back
            at(0.78, 0.4 * sag),
            at(1.0, -0.02 * D),  # withers
            at(1.0, 0.0) + f * fo * 0.55 + d * 0.10 * D,  # neck root on top
            at(1.0, 0.0) + f * fo + d * 0.48 * D,  # point of shoulder
            at(1.0, 0.0) + f * fo * 0.55 + d * 0.88 * D,  # chest
            at(0.9, D),  # brisket / girth
            at(0.6, D * P["belly"]),  # belly
            at(0.32, D * (P["belly"] + P["tuck"]) / 2),
            at(0.12, D * P["tuck"]),  # flank / tuck
            at(0.0, 0.0) - f * ro * 0.6 + d * 0.78 * D,  # stifle / back of the thigh
            at(0.0, 0.0) - f * ro + d * 0.38 * D,  # buttock
            at(0.0, 0.0) - f * ro * 0.6 + d * 0.08 * D,  # tail head
        ]
        if fam == "ursine":  # shoulder hump
            pts[4] = pts[4] - d * 0.12 * D
        if fam == "giraffe":
            pts[12] = at(0.0, 0.0) - f * ro * 0.3 + d * 0.95 * D
        return pts

    # ---- drawing
    def draw(self, style: Style) -> tuple[Layer, Layer, Layer, Layer]:
        if self.a["family"] == "bird":
            return self.draw_bird(style)
        a, P, h, d = self.a, self.P, self.h, self.d
        coat = a["coat"]
        col = coat["color"]
        far_l, tail_l, main, near_l = (
            Layer(self.name + "-farlegs"),
            Layer(self.name + "-tail"),
            Layer(self.name + "-body"),
            Layer(self.name + "-nearlegs"),
        )
        trunk = self.trunk_points()
        main.add(smooth(trunk), col)
        sil_paths = [smooth(trunk)]
        # thigh and forearm masses (part of the silhouette)
        for leg, L in self.legs.items():
            if L["far"]:
                continue
            if L["front"]:
                pts = [L["S"] - d * 0.25 * h, L["E"], L["E"] + (L["K"] - L["E"]) * 0.35]
                rad = [P["lw"] * 1.6 * h, P["lw"] * 1.25 * h, P["lw"] * 0.9 * h]
            else:
                pts = [L["S"] - d * 0.2 * h, L["E"], L["E"] + (L["K"] - L["E"]) * 0.3]
                rad = [P["lw"] * 2.4 * h, P["lw"] * 1.7 * h, P["lw"] * 1.0 * h]
            tp = tube(pts, rad)
            main.add(tp, col)
            sil_paths.append(tp)
        # neck
        neck_paths, head_attach, nd = self.neck()
        for p_ in neck_paths:
            main.add(p_, col)
            sil_paths.append(p_)
        self.head_attach, self.neck_dir = head_attach, nd
        # legs
        for leg, L in self.legs.items():
            lay = far_l if L["far"] else near_l
            self.leg_shapes(lay, L, col if not L["far"] else col, coat)
        # tail
        tk = a["tail"]
        if tk != "none":
            self.tail_shapes(tail_l, tk, coat)
        self.sil_paths = sil_paths
        return far_l, tail_l, main, near_l

    def neck(self) -> tuple[list[str], Vec, Vec]:
        a, P, h, f, d, J = self.a, self.P, self.h, self.f, self.d, self.a["J"]
        D = P["depth"] * h
        k = self.T / max(P["trunk"] * h, 1e-6)
        fo = P["fo"] * h * max(k, 0.5)
        top0 = self.W + f * fo * 0.25 - d * 0.02 * D
        bot0 = self.W + f * fo * 0.95 + d * 0.55 * D
        base = (top0 + bot0) / 2
        hd = J.get("head")
        nb = J.get("neck_base")
        if hd is not None and nb is not None and norm(hd - nb) > 1:
            nd = unit(hd - nb)
        elif hd is not None:
            nd = unit(hd - base)
        else:
            nd = unit(f - d * 1.0)
        # clamp the neck angle against the back line: from 75 deg down (grazing) to 110 deg up
        ang = math.atan2(float(nd @ (-d)), float(nd @ f))
        lo, hi = (
            (
                -1.3
                if a["family"]
                in ("equine", "bovine", "ovine", "giraffe", "canine", "feline")
                else -0.6
            ),
            1.92,
        )
        if a["facing"] in ("front", "back"):
            lo, hi = -1.3, 1.92
        if ang < lo or ang > hi:
            self.problems.append(f"neck angle {math.degrees(ang):.0f} clamped")
        ang = max(lo, min(hi, ang))
        nd = unit(f * math.cos(ang) - d * math.sin(ang))
        nl = P["neck"] * h * (1.0 if a["facing"].startswith("profile") else 0.8)
        end = base + nd * nl
        w0, w1 = P["nw0"] * h / 2, P["nw1"] * h / 2
        nrm = np.array([-nd[1], nd[0]])
        if (
            float(nrm @ (d + f)) > 0
        ):  # keep nrm toward the crest (up and back), away from the throat
            nrm = -nrm
        crest = 0.10 * nl if a["family"] in ("equine", "giraffe") else 0.04 * nl
        top = [
            top0,
            base + nd * nl * 0.5 + nrm * (w0 + w1) / 2 + nrm * crest,
            end + nrm * w1,
        ]
        bot = [end - nrm * w1, base + nd * nl * 0.45 - nrm * (w0 + w1) / 2 * 0.95, bot0]
        pts = (
            bezier(top[0], top[1], top[2], 10)
            + [end + nd * w1 * 0.6]
            + bezier(bot[0], bot[1], bot[2], 10)
        )
        paths = [smooth(pts)]
        self.neck_top = bezier(top[0], top[1], top[2], 12)
        self.neck_nrm = nrm
        return paths, end, nd

    def leg_shapes(
        self, lay: Layer, L: dict[str, Any], col: str, coat: dict[str, Any]
    ) -> None:
        P, h, f = self.P, self.h, self.f
        fam = self.a["family"]
        lw = P["lw"] * h
        foot = P["foot"]
        if L["front"]:
            pts = [L["E"], L["K"], L["F"]]
            rad = [lw * 0.95, lw * 0.62, lw * 0.5]
        else:
            pts = [L["E"], L["K"], L["F"]]
            rad = [lw * 1.25, lw * 0.6, lw * 0.5]
        if fam in ("elephant", "ursine"):
            rad = [lw * 1.0, lw * 0.85, lw * 0.85]
        cp = chain(pts, 6)
        rr = list(np.interp(np.linspace(0, 2, len(cp)), [0, 1, 2], rad))
        hoof_h = 0.0
        if foot in ("hoof", "cloven"):
            hoof_h = 0.16 * h
            cp[-1] = cp[-1] - unit(L["F"] - L["K"]) * hoof_h * 0.7
        lay.add(tube(cp, rr), col)
        Fp = L["F"]
        down = unit(L["F"] - L["K"])
        fwd = f if float(f[0]) != 0 else np.array([1.0, 0.0])
        if foot in ("hoof", "cloven"):
            w = lw * 0.62
            top_c = Fp - down * hoof_h * 0.9
            q = [
                top_c - fwd * w * 0.9,
                top_c + fwd * w * 0.8,
                Fp + fwd * w * 1.15 + np.array([0, 0.01 * h]),
                Fp - fwd * w * 0.95,
            ]
            lay.add(rounded(q, 0.2), coat["feet"])
            if foot == "cloven":
                lay.line(
                    f"M{fmt(Fp + fwd * w * 0.15 - down * hoof_h * 0.1)} L{fmt(Fp + fwd * w * 0.05 - down * hoof_h * 0.7)}",
                    shade(coat["feet"], -0.5),
                    0.6,
                )
        elif foot == "paw":
            c = Fp + fwd * lw * 0.35 - down * lw * 0.25
            lay.add(
                ellipse_d(c, lw * 0.75, lw * 0.42, math.atan2(fwd[1], fwd[0])),
                col if not coat.get("socks") else coat["feet"],
            )
            for t in (-0.25, 0.25):
                lay.line(
                    f"M{fmt(c + fwd * lw * (0.35 + t))} l{fmt(-down * lw * 0.3)}",
                    shade(col, -0.4),
                    0.5,
                )
        elif foot == "pad":
            c = Fp + fwd * lw * (0.25 if fam == "ursine" else 0.0) - down * lw * 0.28
            lay.add(
                ellipse_d(
                    c,
                    lw * (1.0 if fam == "ursine" else 0.95),
                    lw * 0.38,
                    math.atan2(fwd[1], fwd[0]),
                ),
                col,
            )
            if fam == "elephant":
                for t in (-0.45, 0.0, 0.45):
                    lay.add(
                        ellipse_d(
                            c + fwd * lw * t + down * lw * 0.12, lw * 0.16, lw * 0.11, 0
                        ),
                        shade(col, 0.35),
                        outline=False,
                    )

    def tail_shapes(self, lay: Layer, kind: str, coat: dict[str, Any]) -> None:
        a, P, h, f, d, J = self.a, self.P, self.h, self.f, self.d, self.a["J"]
        k = self.T / max(P["trunk"] * h, 1e-6)
        ro = P["ro"] * h * max(k, 0.5)
        base = self.C - f * ro * 0.55 + d * 0.06 * P["depth"] * h
        tb, tt = J.get("tail_base"), J.get("tail_tip")
        if tb is not None and tt is not None and norm(tt - tb) > 1:
            td = unit(tt - tb)
        else:
            td = unit(-f * 0.4 + d)
        # a tail never points into the body
        if float(td @ f) > 0.3:
            td = unit(td - f * (float(td @ f) - 0.1))
        tl = P["tail"] * h
        if kind in ("thin", "bushy") and P["tail"] < 0.8:
            tl = 1.2 * h
        end = base + td * tl
        ctrl = (
            base
            + td * tl * 0.5
            + rot(td, math.pi / 2)
            * tl
            * 0.12
            * (1 if float(rot(td, math.pi / 2) @ -f) > 0 else -1)
        )
        line = bezier(base, ctrl, end, 10)
        col, mane = coat["color"], coat["mane"]
        if kind == "thin":
            lay.add(tube(line, list(np.linspace(0.12 * h, 0.06 * h, len(line)))), col)
        elif kind == "bushy":
            r = [
                0.10 * h + 0.22 * h * math.sin(math.pi * min(1, t * 1.15))
                for t in np.linspace(0, 1, len(line))
            ]
            r[-1] = 0.05 * h
            lay.add(tube(line, r), col)
            if a["family"] == "canine":
                tip = bezier(line[-4], line[-2], line[-1], 4)
                lay.add(tube(tip, [0.16 * h, 0.12 * h, 0.08 * h, 0.04 * h]), "#f4f1ea")
        elif kind == "hair":
            r = [
                0.10 * h + 0.16 * h * math.sin(math.pi * min(1, t * 0.9))
                for t in np.linspace(0, 1, len(line))
            ]
            r[-1] = 0.03 * h
            lay.add(tube(line, r), mane)
            for s in (-0.5, 0.5):
                lay.line(
                    smooth(
                        [p + rot(td, math.pi / 2) * s * 0.08 * h for p in line[2:]],
                        closed=False,
                    ),
                    shade(mane, -0.35),
                    0.5,
                )
        elif kind == "tuft":
            lay.add(tube(line, list(np.linspace(0.07 * h, 0.04 * h, len(line)))), col)
            lay.add(
                ellipse_d(
                    end + td * 0.12 * h,
                    0.09 * h,
                    0.2 * h,
                    math.atan2(td[1], td[0]) + math.pi / 2,
                ),
                mane,
            )
        elif kind == "short":
            lay.add(
                ellipse_d(
                    base + td * 0.18 * h, 0.16 * h, 0.12 * h, math.atan2(td[1], td[0])
                ),
                "#f6f3ee" if a["family"] == "small" else col,
            )
        elif kind == "curly":
            c0 = base - f * 0.12 * h
            pts = [
                c0 + np.array([math.cos(t) * r_, math.sin(t) * r_])
                for t, r_ in zip(
                    np.linspace(0, 3.6 * math.pi, 22),
                    np.linspace(0.02 * h, 0.13 * h, 22),
                )
            ]
            lay.line(smooth(pts, closed=False), shade(col, -0.35), 2.6)
        elif kind == "fan":
            lay.add(
                smooth(
                    [
                        base + rot(td, s) * tl * (0.98 if abs(s) < 0.3 else 0.88)
                        for s in (-0.35, -0.12, 0.12, 0.35)
                    ]
                    + [base]
                ),
                col,
            )

    # ---- birds
    def draw_bird(self, style: Style) -> tuple[Layer, Layer, Layer, Layer]:
        a, P, h = self.a, self.P, self.h
        coat = a["coat"]
        col = coat["color"]
        ax, dd, c, L = self.ax, self.dd, self.body_c, self.L
        D = P["depth"] * h
        far_l, tail_l, main, near_l = (
            Layer(self.name + "-farwing"),
            Layer(self.name + "-tail"),
            Layer(self.name + "-body"),
            Layer(self.name + "-nearwing"),
        )
        # egg body: fuller at the breast (front), tapering to the tail
        pts = []
        for t in np.linspace(0, 2 * math.pi, 28, endpoint=False):
            x, y = math.cos(t), math.sin(t)
            rx = L / 2 * (1.0 if x > 0 else 0.92)
            ry = D / 2 * (1.0 + 0.12 * x)
            pts.append(c + ax * x * rx + dd * y * ry)
        main.add(smooth(pts), col)
        self.sil_paths = [smooth(pts)]
        # neck and head attach
        J = a["J"]
        hd = J.get("head")
        base = c + ax * L * 0.36 - dd * D * 0.18
        nd = unit(hd - base) if hd is not None else unit(ax - dd)
        ang = math.atan2(float(nd @ (-dd)), float(nd @ ax))
        ang = max(-0.9, min(2.2, ang))
        nd = unit(ax * math.cos(ang) - dd * math.sin(ang))
        end = base + nd * P["neck"] * h
        npth = tube(
            [base, (base + end) / 2, end],
            [P["nw0"] * h / 2, (P["nw0"] + P["nw1"]) * h / 4, P["nw1"] * h / 2],
            cap=False,
        )
        main.add(npth, col)
        self.sil_paths.append(npth)
        self.head_attach, self.neck_dir = end, nd
        # tail fan
        croup = c - ax * L * 0.45 - dd * D * 0.05
        tt = J.get("tail_tip")
        td = (
            unit(tt - croup)
            if tt is not None and norm(tt - croup) > 1
            else unit(-ax + dd * 0.25)
        )
        if float(td @ ax) > 0.2:
            td = unit(-ax + dd * 0.25)
        tl = P["tail"] * h
        tail_l.add(
            smooth(
                [
                    croup + rot(td, s) * tl * (1.0 if abs(s) < 0.2 else 0.85)
                    for s in (-0.28, -0.1, 0.1, 0.28)
                ]
                + [croup + td * 0.1 * h]
            ),
            coat["pattern_color"] if coat["pattern"] != "plain" else shade(col, -0.2),
        )
        # wings
        wc = shade(col, -0.18) if coat["pattern"] == "plain" else coat["pattern_color"]
        root = c + ax * L * 0.18 - dd * D * 0.28
        if a["pose"] == "flying":
            for s, lay in (("far", far_l), ("near", near_l)):
                tip = J.get(f"{s}_wing_tip")
                wd = (
                    unit(tip - root)
                    if tip is not None and norm(tip - root) > 1
                    else unit(-dd + ax * (0.2 if s == "near" else -0.2))
                )
                wl = P["wing"] * h
                r0 = root + (ax * 0.12 * h if s == "far" else 0)
                tipp = r0 + wd * wl
                perp = np.array([-wd[1], wd[0]])
                if float(perp @ ax) > 0:
                    perp = -perp  # trailing edge toward the tail
                lead = bezier(
                    r0 + ax * 0.32 * h, r0 + wd * wl * 0.45 - perp * 0.35 * h, tipp, 8
                )
                trail = []
                for i, t in enumerate(np.linspace(1, 0, 7)):
                    p = (
                        r0
                        + wd * wl * t
                        + perp
                        * (
                            0.25 * h
                            + 0.75
                            * h
                            * math.sin(math.pi * min(t * 1.4, 1))
                            * (1 if t > 0.15 else 0.5)
                        )
                    )
                    trail.append(p)
                    if 0 < i < 6:
                        trail.append(
                            r0
                            + wd * wl * (t - 0.07)
                            + perp
                            * (
                                0.1 * h
                                + 0.55
                                * h
                                * math.sin(math.pi * min((t - 0.07) * 1.4, 1))
                            )
                        )
                lay.add(
                    smooth(lead + trail + [r0 - ax * 0.3 * h]),
                    wc if s == "near" else wc,
                )
        else:
            wing = [
                root + ax * 0.15 * h,
                root - ax * L * 0.25 + dd * D * 0.05,
                root - ax * L * 0.62 + dd * D * 0.12,
                root - ax * L * 0.4 + dd * D * 0.42,
                root + ax * 0.05 * h + dd * D * 0.38,
            ]
            near_l.add(smooth(wing), wc)
            near_l.line(
                smooth(
                    [
                        root - ax * L * 0.05 + dd * D * 0.22,
                        root - ax * L * 0.35 + dd * D * 0.28,
                        root - ax * L * 0.55 + dd * D * 0.2,
                    ],
                    closed=False,
                ),
                shade(wc, -0.35),
                0.6,
            )
        # legs
        legs_far, legs_near = (
            Layer(self.name + "-farleg"),
            Layer(self.name + "-nearleg"),
        )
        for s, Lg in self.legs.items():
            lay = legs_far if Lg["far"] else legs_near
            fc = coat["feet"]
            lay.add(tube([Lg["S"], Lg["K"]], [0.10 * h, 0.07 * h]), col)
            lay.add(tube([Lg["K"], Lg["F"]], [0.045 * h, 0.04 * h], cap=True), fc)
            fwd = (
                ax
                if abs(ax[0]) > 0.3
                else np.array([1.0 if a["facing"] != "profile-left" else -1.0, 0.0])
            )
            fwd = unit(np.array([fwd[0], 0.0]))
            for tilt in (-0.35, 0.0, 0.35):
                tdir = unit(rot(fwd, tilt * 0.6))
                lay.line(f"M{fmt(Lg['F'])} L{fmt(Lg['F'] + tdir * 0.38 * h)}", fc, 1.6)
            lay.line(f"M{fmt(Lg['F'])} L{fmt(Lg['F'] - fwd * 0.2 * h)}", fc, 1.6)
        self.bird_leg_layers = (legs_far, legs_near)
        return far_l, tail_l, main, near_l


def cross_sign(f: Vec, d: Vec) -> float:
    return float(f[0] * d[1] - f[1] * d[0])


# ---------------------------------------------------------------- coat: belly, pattern, wool, mane (clipped to the silhouette)
def coat_overlay(an: Animal, clip_id: str) -> ET.Element | None:
    a, h = an.a, an.h
    coat = a["coat"]
    g = el("g", clip_path=f"url(#{clip_id})")
    # A stable seed: the same draft always gives the same coat.
    rng = np.random.default_rng(zlib.crc32(an.name.encode("utf-8")))
    xs = []
    if a["family"] != "bird":
        tp = an.trunk_points()
        xs = tp
    else:
        xs = [an.body_c + an.ax * an.L * 0.5, an.body_c - an.ax * an.L * 0.5]
    allp = np.array([p for p in xs] + [an.head_attach])
    x0, y0 = allp.min(axis=0) - h
    x1, y1 = allp.max(axis=0) + h
    if coat["belly"] and a["family"] != "bird":
        f, d = an.f, an.d
        D = an.P["depth"] * h
        c = (an.W + an.C) / 2 + d * D * 1.05
        g.append(
            el(
                "path",
                d=ellipse_d(c, an.T * 0.62 + 0.3 * h, D * 0.38, math.atan2(f[1], f[0])),
                fill=coat["belly"],
            )
        )
    elif coat["belly"] and a["family"] == "bird":
        g.append(
            el(
                "path",
                d=ellipse_d(
                    an.body_c + an.dd * an.P["depth"] * h * 0.3 + an.ax * an.L * 0.15,
                    an.L * 0.42,
                    an.P["depth"] * h * 0.32,
                    math.atan2(an.ax[1], an.ax[0]),
                ),
                fill=coat["belly"],
            )
        )
    pc = coat["pattern_color"]
    if coat["pattern"] == "spots":
        n = int(max(8, min(40, (x1 - x0) * (y1 - y0) / (0.5 * h) ** 2 * 0.35)))
        for _ in range(n):
            p = np.array([rng.uniform(x0, x1), rng.uniform(y0, y1)])
            r = rng.uniform(0.07, 0.16) * h
            g.append(
                el(
                    "path",
                    d=ellipse_d(p, r * rng.uniform(0.8, 1.3), r, rng.uniform(0, 3)),
                    fill=pc,
                )
            )
    elif coat["pattern"] == "patches":
        for _ in range(5):
            p = np.array(
                [rng.uniform(x0 + h, x1 - h), rng.uniform(y0 + h * 0.8, y1 - h * 0.8)]
            )
            pts = [
                p + rot(np.array([1.0, 0]), t) * h * rng.uniform(0.35, 0.65)
                for t in np.linspace(0, 2 * math.pi, 9, endpoint=False)
            ]
            g.append(el("path", d=smooth(pts), fill=pc))
    elif coat["pattern"] == "stripes":
        f = an.f if a["family"] != "bird" else an.ax
        d = an.d if a["family"] != "bird" else an.dd
        span = np.array([x1 - x0, y1 - y0])
        c = np.array([(x0 + x1) / 2, (y0 + y1) / 2])
        n = int(norm(span) / (0.32 * h)) + 2
        for i in range(n):
            o = (i - n / 2) * 0.32 * h
            p0 = c + f * o - d * norm(span)
            p1 = c + f * (o + 0.08 * h) + d * 0.1 * h
            p2 = c + f * o + d * norm(span)
            line = bezier(p0, p1, p2, 9)
            g.append(el("path", d=tube(line, [0.07 * h] * len(line)), fill=pc))
    if len(g) == 0:
        return None
    return g


def wool_bumps(an: Animal) -> list[str]:
    """Scalloped wool outline: circles along the trunk outline."""
    pts = an.trunk_points()
    ring = []
    for i in range(len(pts)):
        p0, p1 = pts[i], pts[(i + 1) % len(pts)]
        n = max(1, int(norm(p1 - p0) / (0.28 * an.h)))
        for k in range(n):
            ring.append(p0 + (p1 - p0) * k / n)
    return [circle_d(p, 0.19 * an.h) for p in ring]


# ---------------------------------------------------------------- composition
def compose(
    svg: str, ck: dict[str, Any] | None = None, living: dict[str, bool] | None = None
) -> tuple[str, dict[str, Any]]:
    doc = Doc(svg)
    ids = {t["id"] for t in (ck or {}).get("things", [])}
    report: dict[str, Any] = {
        "animals": [],
        "invalid": [],
        "animal_heads_without_pose": [],
    }
    found = []
    for g in list(doc.root.iter()):
        if tag(g) != "g" or not g.get("id"):
            continue
        desc = next(
            (
                c
                for c in g
                if tag(c) == "desc"
                and ((c.get("class") or "") == "animal" or '"family"' in (c.text or ""))
            ),
            None,
        )
        if desc is None:
            continue
        found.append((g, desc))
    animals = []
    for g, desc in found:
        name = str(g.get("id"))
        head = named_part(doc, name, "head", g)
        a, problems, fatal = parse_animal(desc.text or "", head)
        g.remove(desc)
        if fatal:
            fam = (
                SPECIES_TO_FAMILY.get((head.get("data-species") or "").strip().lower())
                if head is not None
                else None
            )
            if fam and head is not None:
                a = {
                    "family": fam,
                    "pose": "standing",
                    "facing": head.get("data-facing")
                    if head.get("data-facing") in FACINGS
                    else "profile-right",
                    "coat": {
                        "color": DEFAULT_COAT[fam],
                        "belly": None,
                        "pattern": "plain",
                        "pattern_color": shade(DEFAULT_COAT[fam], -0.45),
                        "fur": "short",
                        "mane": shade(DEFAULT_COAT[fam], -0.35),
                        "feet": shade(DEFAULT_COAT[fam], -0.6),
                    },
                    "tail": PROP[fam]["tailk"],
                    "J": {},
                    "relations": [],
                    "contacts": [],
                }
                problems.append(f"defaulted ({fatal})")
                report["invalid"].append(
                    {"name": name, "reason": fatal, "defaulted": True}
                )
            else:
                report["invalid"].append(
                    {"name": name, "reason": fatal, "problems": problems}
                )
                continue
        assert a is not None
        animals.append(Animal(name, g, a, problems))
    # remove any other desc that the body kit would read as a person pose (an animal desc without class, nested)
    if not animals:
        return (ET.tostring(doc.root, encoding="unicode") if found else svg), report
    style = Style(doc)
    anchors = anchor_points(doc, ids)
    pose_texts = " ".join((c.text or "") for c in doc.root.iter() if tag(c) == "desc")
    for an in animals:
        try:
            one_animal(
                doc,
                an,
                style,
                anchors,
                ids,
                pose_texts,
                [x.group for x in animals],
                (ck or {}).get("things", []),
            )
            report["animals"].append(an.rec)
        except Exception:  # noqa: BLE001
            an.rec["error"] = traceback.format_exc()[-1200:]
            report["animals"].append(an.rec)
    return ET.tostring(doc.root, encoding="unicode"), report


def one_animal(
    doc: Doc,
    an: Animal,
    style: Style,
    anchors: Anchors,
    ids: set[str],
    pose_texts: str,
    animal_groups: list[ET.Element],
    ck_things: list[dict[str, Any]] | None = None,
) -> None:
    g, name, a = an.group, an.name, an.a
    head = named_part(doc, name, "head", g)
    hat = find_named(doc, name, "hat", g)
    eyew = find_named(doc, name, "eyewear", g)
    hb = (
        head_box(doc, head, [x for x in (hat, eyew) if x is not None])
        if head is not None
        else None
    )
    for c in a["contacts"]:
        k = find_anchor(anchors, c["anchor"])
        c["_p"] = anchors[k]["p"] if k else None
    an.head_facing = head.get("data-facing") if head is not None else None
    an.frame()
    h = an.estimate_h(hb)
    if not h:
        h = 60.0
        an.problems.append("no size: default 60")
    an.build(h, hb)
    layers = an.draw(style)
    an.rec["legs"] = {k: {"miss": v["miss"]} for k, v in an.legs.items()}
    an.rec["painter_joints"] = sorted(a["J"])
    # strip the painter's own body shapes (keep the head, hat, eyewear, anchors, other things' groups)
    keep = [x for x in (head, hat, eyew) if x is not None]
    removed = 0

    def strip(node: ET.Element) -> None:
        nonlocal removed
        for ch in list(node):
            if ch in keep or tag(ch) == "desc":
                continue
            if any(ch in doc.ancestors(k) for k in keep):
                strip(ch)
                continue
            cid = ch.get("id") or ""
            if cid.startswith("anchor-") or (
                tag(ch) == "g" and cid and cid != name and cid in ids
            ):
                continue
            if tag(ch) in CONTAINERS:
                continue
            removed += (
                len(doc.shapes_under(ch))
                if tag(ch) == "g"
                else (1 if tag(ch) in SHAPES else 0)
            )
            node.remove(ch)

    strip(g)
    doc.parent = {id(c): p for p in doc.root.iter() for c in p}
    an.rec["painter_body_shapes_removed"] = removed
    # head transform: scale to HL, back-lower part of the head onto the neck end
    M = np.eye(3)
    if hb is not None:
        assert head is not None
        bw, bh = hb[2] - hb[0], hb[3] - hb[1]
        hf = (head.get("data-facing") or a["facing"]).strip().lower()
        eff = (
            bw
            if hf.startswith("profile")
            else (
                0.9 * max(bw, 0.75 * bh)
                if hf.startswith("three")
                else 0.8 * max(bh, 0.8 * bw)
            )
        )
        s = max(0.3, min(3.0, HEAD_GAIN * an.h * an.P["hk"] / max(eff, 1e-3)))
        if hf in ("profile-right", "three-quarter-right"):
            src = np.array(
                [
                    hb[0] + (0.30 if hf.startswith("profile") else 0.4) * bw,
                    hb[1] + 0.66 * bh,
                ]
            )
        elif hf in ("profile-left", "three-quarter-left"):
            src = np.array(
                [
                    hb[2] - (0.30 if hf.startswith("profile") else 0.4) * bw,
                    hb[1] + 0.66 * bh,
                ]
            )
        else:
            src = np.array([(hb[0] + hb[2]) / 2, hb[1] + 0.72 * bh])
        if a["family"] == "elephant":
            src = np.array([src[0], hb[1] + 0.42 * bh])
        dst = an.head_attach
        M = np.array(
            [[s, 0, dst[0] - s * src[0]], [0, s, dst[1] - s * src[1]], [0, 0, 1]]
        )
        an.rec["head_scale"] = round(s, 3)
    else:
        an.rec["head"] = "missing"
    # build the group: far legs, tail, body (+ coat overlay), near legs, head
    inner = el("g")
    far_l, tail_l, main, near_l = layers
    defs = el("defs")
    cid = f"{name}-coatclip"
    cp = el("clipPath", id=cid)
    for p in an.sil_paths:
        cp.append(el("path", d=p))
    defs.append(cp)
    inner.append(defs)
    if a["family"] == "bird":
        assert an.bird_leg_layers is not None
        lf, ln = an.bird_leg_layers
        if lf.items:
            inner.append(lf.element(style, an.h, darker=0.12))
        if far_l.items:
            inner.append(far_l.element(style, an.h, darker=0.15))
        if tail_l.items:
            inner.append(tail_l.element(style, an.h))
        if ln.items:
            inner.append(ln.element(style, an.h))
    else:
        if far_l.items:
            inner.append(far_l.element(style, an.h, darker=0.14))
        if tail_l.items and a["family"] not in ("suid",):
            inner.append(tail_l.element(style, an.h))
    if a["coat"]["fur"] == "wool" and a["family"] != "bird":
        for c in wool_bumps(an):
            main.add(c, a["coat"]["color"])
    inner.append(main.element(style, an.h))
    ov = coat_overlay(an, cid)
    if ov is not None:
        inner.append(ov)
    if a["family"] == "equine":
        # mane along the crest of the neck
        top = an.neck_top
        nrm = an.neck_nrm
        pts = [p + nrm * 0.02 * an.h for p in top[1:]]
        lower = [
            p - nrm * 0.20 * an.h + (an.neck_dir * 0.03 * an.h if i % 2 else 0)
            for i, p in enumerate(top[1:][::-1])
        ]
        mane = Layer(name + "-mane")
        mane.add(smooth(pts + lower), a["coat"]["mane"])
        inner.append(mane.element(style, an.h))
    if a["family"] == "suid" and tail_l.items:
        inner.append(tail_l.element(style, an.h))
    if near_l.items:
        inner.append(near_l.element(style, an.h))
    # rider: saddle cloth when a person declares the back anchor
    back = (
        an.C + (an.W - an.C) * 0.66 - an.d * 0.0
        if a["family"] != "bird"
        else an.body_c - an.dd * an.P["depth"] * an.h * 0.5
    )
    anchor_id = f"anchor-{name}-back"
    if a["family"] != "bird" and (
        anchor_id in pose_texts or f"{name}-back" in pose_texts
    ):
        f, d = an.f, an.d
        D = an.P["depth"] * an.h
        sc = [
            back - f * 0.45 * an.h - d * 0.05 * D,
            back + f * 0.35 * an.h - d * 0.05 * D,
            back + f * 0.30 * an.h + d * 0.55 * D,
            back - f * 0.40 * an.h + d * 0.55 * D,
        ]
        sl = Layer(name + "-saddle")
        sl.add(rounded(sc, 0.3), "#7a3b2e")
        inner.append(sl.element(style, an.h))
        an.rec["saddle"] = True
    inner.append(
        el(
            "circle",
            id=anchor_id,
            cx=f"{back[0]:.1f}",
            cy=f"{back[1]:.1f}",
            r="3",
            fill="none",
        )
    )
    an.rec["back_anchor"] = [round(float(back[0]), 1), round(float(back[1]), 1)]
    g.insert(0, wrap_inverse(doc, g, inner))
    an.inner = inner
    # head moved into the group under a wrapper
    if head is not None and hb is not None:
        for item in [x for x in (head, hat, eyew) if x is not None]:
            par = doc.parent.get(id(item))
            if par is not None and item in list(par):
                Cold = doc.ctm(par) if par is not doc.root else np.eye(3)
                par.remove(item)
            else:
                Cold = np.eye(3)
            Cp = doc.ctm(g)
            Wm = np.linalg.inv(Cp) @ M @ Cold
            w = (
                el("g", transform=mstr(Wm))
                if not np.allclose(Wm, np.eye(3))
                else el("g")
            )
            w.append(item)
            g.append(w)
    doc.parent = {id(c): p for p in doc.root.iter() for c in p}
    # relations
    done = []
    kinds = {t["id"]: t.get("kind") for t in (ck_things or [])}
    for r in a["relations"]:
        T_ = find_thing(doc, r["thing"], animal_groups, g)
        if (
            T_ is not None
            and kinds.get(re.sub(r"-\d+$", "", T_.get("id") or "")) == "place"
        ):
            done.append(f"{r['thing']}:{r['rel']}:place ignored")
            continue
        if T_ is None:
            done.append(f"{r['thing']}:{r['rel']}:missing")
            continue
        over = r["rel"] not in ("behind", "in")
        if r["rel"] == "in" and an.inner is not None:
            # 1.1: nothing of the animal below the container's bottom edge over its columns
            try:
                tb = doc.box(thing_shapes(doc, T_))
                if tb:
                    x0, y0, x1, y1 = tb
                    cpid = f"{name}-inclip"
                    cp_ = el("clipPath", id=cpid)
                    cp_.append(
                        el(
                            "path",
                            d=f"M-9000,-9000 H9000 V9000 H-9000 Z M{x0:.1f},{y1 - 0.02 * (y1 - y0):.1f} H{x1:.1f} V9000 H{x0:.1f} Z",
                            clip_rule="evenodd",
                        )
                    )
                    an.inner.insert(0, cp_)
                    an.inner.set("clip-path", f"url(#{cpid})")
                    done.append(f"{r['thing']}:in:clipped below {y1:.0f}")
            except Exception as error:  # noqa: BLE001
                done.append(f"{r['thing']}:in:clip error {type(error).__name__}")
        if r["rel"] in ("holding", "beside"):
            done.append(f"{r['thing']}:{r['rel']}:kept")
            continue
        res = reorder(doc, g, T_, over=over)
        done.append(
            f"{r['thing']}:{r['rel']}:{'over' if over else 'under'}"
            + ("" if res else ":nested")
        )
    an.rec["relations"] = done
