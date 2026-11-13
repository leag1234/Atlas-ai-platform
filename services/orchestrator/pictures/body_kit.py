"""The body kit (version 4.1): code draws each person from the pose the painter declares.

The painter draws only the head, in <g id="NAME-head"> inside <g id="NAME">,
and declares a pose as JSON in <desc class="pose">: facing, build, shape,
skin, joints, clothes (closed lists), contacts on anchor-* circles and
relations to things. Per person:

1. validate the pose (closed vocabularies, missing joints derived, defaults
   recorded); a pose that cannot be read leaves the painter's drawing alone;
2. head height h from the painter's skeleton (segment length over its
   proportion, 70th percentile); segment lengths set to the build's
   proportions, the painter's joint angles kept;
3. lying-on / lying-in: joints rebuilt along the support's surface, head at
   the head end, a cover for lying-in;
4. root on the seat anchor, else on the lowest supporting joint, else on the
   hips; contacts snapped by two-bone inverse kinematics (25 % stretch at
   most); anchors on the face move with their thing; two hands on one long
   thing re-gripped; arms through the head re-bent; crossed arms uncrossed;
5. body, limbs, clothes, hands and shoes drawn as one-outline layers in the
   painter's colours; long hair behind the body; hair over the face clipped;
6. the painter's head scaled and moved onto the neck;
7. depth: declared relations first (in: rim clip; behind: under the thing;
   in-front-of and lying: over it; a relation to a place is ignored), then
   containment by geometry on the things the person declared nothing about.

Ported from the spike rounds 20 to 22 (body kit 4.1), same rules and numbers.
"""

from __future__ import annotations

import copy
import json
import math
import re
import xml.etree.ElementTree as ET
from collections import Counter
from collections.abc import Callable, Iterable
from typing import Any, cast

import numpy as np
import numpy.typing as npt

from services.orchestrator.pictures import ndimage as ndi
from services.orchestrator.pictures import render
from services.orchestrator.pictures.drawing import (
    FACINGS,
    Anchors,
    Layer,
    Style,
    V,
    Vec,
    anchor_points,
    capsule,
    circle_d,
    cross,
    el,
    ellipse_d,
    find_anchor,
    find_thing,
    fmt,
    head_box,
    hexcol,
    hull,
    lca_children,
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
    SHAPES as SHAPE_TAGS,
    Doc,
    find_named,
    heads_of,
    lum,
    mstr,
    tag,
)

Mask = npt.NDArray[np.bool_]
Ints = npt.NDArray[np.intp]

BUILDS = {
    "child": "child",
    "kid": "child",
    "adult": "adult",
    "elderly": "elderly",
    "old": "elderly",
    "senior": "elderly",
}

BODY_SHAPES = ("slim", "average", "broad", "round")

TOPS = ("t-shirt", "shirt", "sweater", "jacket", "coat", "dress", "tank-top", "robe")

TOP_ALIAS = {
    "tshirt": "t-shirt",
    "tee": "t-shirt",
    "blouse": "shirt",
    "polo": "shirt",
    "hoodie": "sweater",
    "jumper": "sweater",
    "cardigan": "sweater",
    "pullover": "sweater",
    "raincoat": "coat",
    "overcoat": "coat",
    "blazer": "jacket",
    "uniform": "jacket",
    "tunic": "shirt",
    "gown": "robe",
    "vest-top": "tank-top",
    "tank": "tank-top",
    "top": "t-shirt",
    "chef-jacket": "jacket",
    "waistcoat": "jacket",
}

BOTTOMS = ("trousers", "shorts", "skirt", "long-skirt", "none")

BOTTOM_ALIAS = {
    "pants": "trousers",
    "jeans": "trousers",
    "overalls": "trousers",
    "leggings": "trousers",
    "dungarees": "trousers",
}

SHOES = ("shoes", "boots", "sneakers", "sandals", "bare")

SHOE_ALIAS = {
    "slippers": "shoes",
    "clogs": "shoes",
    "loafers": "shoes",
    "trainers": "sneakers",
    "barefoot": "bare",
    "none": "bare",
    "wellies": "boots",
}

EXTRAS = ("apron", "scarf", "belt", "vest", "gloves")

JOINTS = (
    "neck",
    "near_shoulder",
    "far_shoulder",
    "near_elbow",
    "far_elbow",
    "near_wrist",
    "far_wrist",
    "near_hip",
    "far_hip",
    "near_knee",
    "far_knee",
    "near_ankle",
    "far_ankle",
)

KINDS = ("sit", "hold", "step", "touch", "in")

# segment lengths in head heights (head = top of hair to chin). Adult: round 18b table (comicgen, 7 adults): 5.3 heads standing, legs
# crotch to sole 2.3, straight arm shoulder to fingertip 2.5, hand 0.35, shoulders 1.8 head widths. Child and elderly: my choices.
PROP: dict[str, dict[str, Any]] = {
    "adult": {
        "tall": 5.3,
        "neck": 0.22,
        "torso": 1.75,
        "upper": 1.10,
        "fore": 1.00,
        "hand": 0.36,
        "thigh": 1.15,
        "shin": 1.05,
        "foot_h": 0.12,
        "sw": 0.72,
        "cw": 0.62,
        "ww": 0.52,
        "hw": 0.55,
        "r_up": (0.15, 0.12),
        "r_fore": (0.12, 0.09),
        "r_thigh": (0.21, 0.155),
        "r_shin": (0.15, 0.10),
        "r_neck": 0.13,
        "foot": 0.50,
    },
    "child": {
        "tall": 4.0,
        "neck": 0.14,
        "torso": 1.22,
        "upper": 0.76,
        "fore": 0.68,
        "hand": 0.30,
        "thigh": 0.80,
        "shin": 0.72,
        "foot_h": 0.10,
        "sw": 0.55,
        "cw": 0.50,
        "ww": 0.47,
        "hw": 0.48,
        "r_up": (0.13, 0.11),
        "r_fore": (0.11, 0.085),
        "r_thigh": (0.18, 0.14),
        "r_shin": (0.135, 0.095),
        "r_neck": 0.12,
        "foot": 0.42,
    },
    "elderly": {
        "tall": 5.1,
        "neck": 0.20,
        "torso": 1.68,
        "upper": 1.06,
        "fore": 0.98,
        "hand": 0.36,
        "thigh": 1.10,
        "shin": 1.00,
        "foot_h": 0.12,
        "sw": 0.68,
        "cw": 0.60,
        "ww": 0.56,
        "hw": 0.56,
        "r_up": (0.15, 0.12),
        "r_fore": (0.12, 0.09),
        "r_thigh": (0.20, 0.15),
        "r_shin": (0.145, 0.10),
        "r_neck": 0.13,
        "foot": 0.50,
    },
}

SHAPE_MUL: dict[str, dict[str, float]] = {
    "slim": {"w": 0.88, "r": 0.88},
    "average": {"w": 1.0, "r": 1.0},
    "broad": {"w": 1.15, "r": 1.12, "sw": 1.08},
    "round": {"w": 1.1, "r": 1.12, "ww": 1.38, "hw": 1.15, "cw": 1.1},
}

FACING_W = {
    "front": 1.0,
    "back": 1.0,
    "three-quarter-left": 0.8,
    "three-quarter-right": 0.8,
    "profile-left": 0.52,
    "profile-right": 0.52,
}

MAX_STRETCH = 1.25


# ---------------------------------------------------------------- pose
def parse_pose(
    text: str, head: ET.Element | None
) -> tuple[dict[str, Any] | None, list[str], str | None]:
    """-> (pose dict, problems list, fatal reason or None)."""
    problems = []
    try:
        raw = json.loads(re.sub(r"^[^{]*|[^}]*$", "", text.strip()))
    except Exception as error:  # noqa: BLE001
        return None, [f"json: {type(error).__name__}"], "invalid JSON"
    if not isinstance(raw, dict):
        return None, ["json not an object"], "invalid JSON"
    pose: dict[str, Any] = {}
    f = str(raw.get("facing", "")).strip().lower()
    if f not in FACINGS:
        hf = (head.get("data-facing") if head is not None else "") or ""
        problems.append(f"facing={f!r}->{hf or 'front'}")
        f = hf if hf in FACINGS else "front"
    pose["facing"] = f
    b = str(raw.get("build", "")).strip().lower()
    if b not in BUILDS:
        ha = (head.get("data-age") if head is not None else "") or ""
        problems.append(f"build={b!r}")
        b = {"child": "child", "old": "elderly"}.get(ha, "adult")
    pose["build"] = BUILDS[b]
    s = str(raw.get("shape", "average")).strip().lower()
    if s not in BODY_SHAPES:
        problems.append(f"shape={s!r}")
        s = "average"
    pose["shape"] = s
    pose["skin"] = hexcol(raw.get("skin"), None)
    if pose["skin"] is None:
        problems.append("skin")
        pose["skin"] = "#e8b48c"
    js = raw.get("joints") or {}
    J, missing = {}, []
    for k in JOINTS:
        v = js.get(k)
        if (
            isinstance(v, (list, tuple))
            and len(v) >= 2
            and all(isinstance(x, (int, float)) for x in v[:2])
            and -200 <= v[0] <= 1224
            and -200 <= v[1] <= 1224
        ):
            J[k] = V(v)
        else:
            missing.append(k)
    if isinstance(js.get("head"), (list, tuple)) and len(js["head"]) >= 2:
        try:
            J["head"] = V(js["head"])
        except (TypeError, ValueError):
            pass
    # derive what is missing from its twin or its neighbours
    for side in ("shoulder", "elbow", "wrist", "hip", "knee", "ankle"):
        a, b2 = "near_" + side, "far_" + side
        if a in J and b2 not in J:
            J[b2] = J[a].copy()
        elif b2 in J and a not in J:
            J[a] = J[b2].copy()
    if "neck" not in J and "near_shoulder" in J:
        J["neck"] = (J["near_shoulder"] + J["far_shoulder"]) / 2
    if "near_hip" not in J or "neck" not in J:
        return None, problems + ["missing: " + ",".join(missing)], "no neck or hips"
    for side in ("near", "far"):
        sh, hp = side + "_shoulder", side + "_hip"
        if sh not in J:
            J[sh] = J["neck"].copy()
        if side + "_wrist" not in J:
            J[side + "_wrist"] = J[sh] + (J[hp] - J["neck"]) * 0.9
        if side + "_elbow" not in J:
            J[side + "_elbow"] = (J[sh] + J[side + "_wrist"]) / 2
        if side + "_ankle" not in J:
            J[side + "_ankle"] = J[hp] + (J[hp] - J["neck"]) * 1.2
        if side + "_knee" not in J:
            J[side + "_knee"] = (J[hp] + J[side + "_ankle"]) / 2
    if len(missing) > 6:
        return (
            None,
            problems + ["missing: " + ",".join(missing)],
            "too many missing joints",
        )
    if missing:
        problems.append("derived: " + ",".join(missing))
    pose["joints"] = J
    cl = raw.get("clothes") or {}
    top = cl.get("top") or {}
    tt = str(top.get("type", "t-shirt")).strip().lower().replace(" ", "-")
    tt2 = tt if tt in TOPS else TOP_ALIAS.get(tt, "t-shirt")
    if tt2 != tt:
        problems.append(f"top={tt}->{tt2}")
    bot = cl.get("bottom") or {}
    bt = str(bot.get("type", "trousers")).strip().lower().replace(" ", "-")
    bt2 = bt if bt in BOTTOMS else BOTTOM_ALIAS.get(bt, "trousers")
    if bt2 != bt:
        problems.append(f"bottom={bt}->{bt2}")
    shoes = cl.get("shoes") or {}
    st = str(shoes.get("type", "shoes")).strip().lower()
    st2 = st if st in SHOES else SHOE_ALIAS.get(st, "shoes")
    if st2 != st:
        problems.append(f"shoes={st}->{st2}")
    sl = str(cl.get("sleeves", "long")).strip().lower()
    if sl not in ("long", "short", "none"):
        problems.append(f"sleeves={sl}")
        sl = "long"
    extras = []
    for x in cl.get("extra") or []:
        if isinstance(x, dict):
            t = str(x.get("type", "")).strip().lower()
            if t in EXTRAS:
                extras.append({"type": t, "color": hexcol(x.get("color"), "#f2f2f2")})
            elif t:
                problems.append(f"extra={t} dropped")
    tc = hexcol(top.get("color"), "#5a7fb5")
    pose["clothes"] = {
        "top": tt2,
        "top_c": tc,
        "sleeves": "none" if tt2 == "tank-top" else ("long" if tt2 == "robe" else sl),
        "bottom": bt2,
        "bottom_c": hexcol(bot.get("color"), shade(tc, -0.35)),
        "shoes": st2,
        "shoes_c": hexcol(shoes.get("color"), "#3a2f2a"),
        "extra": extras,
    }
    contacts = []
    for c in raw.get("contacts") or []:
        if not isinstance(c, dict):
            continue
        j = str(c.get("joint", "")).strip().lower()
        k = str(c.get("kind", "")).strip().lower()
        a = str(c.get("anchor", "")).strip()
        if j in ("hip", "hips", "pelvis", "seat", "bottom"):
            j = "hips"
        if j in ("near_hand", "far_hand"):
            j = j.replace("hand", "wrist")
        if j in ("near_foot", "far_foot"):
            j = j.replace("foot", "ankle")
        if k not in KINDS:
            problems.append(f"contact kind={k}")
            k = "sit" if j == "hips" else ("step" if "ankle" in j else "hold")
        if j != "hips" and j not in JOINTS:
            problems.append(f"contact joint={j} dropped")
            continue
        if not a:
            problems.append("contact without anchor dropped")
            continue
        contacts.append({"joint": j, "kind": k, "anchor": a})
    pose["contacts"] = contacts
    rels, rp = parse_relations(raw)
    pose["relations"] = rels
    problems += rp
    return pose, problems, None


# ---------------------------------------------------------------- skeleton
SEGS = [
    ("neck", "mid_hip", "torso"),
    ("near_shoulder", "near_elbow", "upper"),
    ("far_shoulder", "far_elbow", "upper"),
    ("near_elbow", "near_wrist", "fore"),
    ("far_elbow", "far_wrist", "fore"),
    ("near_hip", "near_knee", "thigh"),
    ("far_hip", "far_knee", "thigh"),
    ("near_knee", "near_ankle", "shin"),
    ("far_knee", "far_ankle", "shin"),
]


def facing_dir(facing: str) -> Vec:
    if facing.endswith("right"):
        return np.array([1.0, 0.0])
    if facing.endswith("left"):
        return np.array([-1.0, 0.0])
    return np.array([0.0, 0.0])


def two_bone(
    root: Vec,
    target: Vec,
    l1: float,
    l2: float,
    sign: float,
    rec: dict[str, Any],
    label: str,
) -> tuple[Vec, Vec, float]:
    d = target - root
    D = float(np.hypot(*d))
    if D < 1e-6:
        return root + np.array([0, l1]), target, 1.0
    k = 1.0
    if D > (l1 + l2) * 0.999:
        k = D / ((l1 + l2) * 0.999)
        if k > MAX_STRETCH:
            rec["contacts_missed"].append(label)
            u = d / D
            return root + u * l1, root + u * (l1 + l2), 1.0
        rec["stretched"].append(f"{label}:{k:.2f}")
        l1, l2 = l1 * k, l2 * k
    if D < abs(l1 - l2):
        D = abs(l1 - l2) + 1e-3
    cosa = max(-1.0, min(1.0, (l1 * l1 + D * D - l2 * l2) / (2 * l1 * D)))
    a = math.acos(cosa)
    mid = root + rot(d / float(np.hypot(*d)), sign * a) * l1
    return mid, target, k


def bend_sign(r: Vec, m: Vec, t: Vec, fallback_dir: Vec) -> float:
    c = cross(t - r, m - r)
    if abs(c) > 1e-3 * max(1.0, float(np.hypot(*(t - r))) ** 2) * 0.02:
        return 1.0 if c > 0 else -1.0
    # straight in the painter's pose: bend toward fallback_dir
    for s in (1.0, -1.0):
        mid = rot(unit(t - r), s * 0.3)
        if float(np.dot(mid, fallback_dir)) > float(
            np.dot(rot(unit(t - r), -s * 0.3), fallback_dir)
        ):
            return s
    return 1.0


class Person:
    def __init__(
        self, name: str, group: ET.Element, pose: dict[str, Any], problems: list[str]
    ) -> None:
        self.name, self.group, self.pose, self.problems = name, group, pose, problems
        self.P = {k: v.copy() for k, v in pose["joints"].items()}
        self.P["mid_hip"] = (self.P["near_hip"] + self.P["far_hip"]) / 2
        self.rec: dict[str, Any] = {
            "name": name,
            "facing": pose["facing"],
            "build": pose["build"],
            "shape": pose["shape"],
            "problems": problems,
            "contacts": [],
            "contacts_snapped": [],
            "contacts_missed": [],
            "anchor_missing": [],
            "stretched": [],
            "moved": False,
        }
        # Set by build(), lie() and snap(); declared here for the type checker.
        self.h = self.h0 = 0.0
        self.side = 1.0
        self.u = self.p = self.fwd = np.zeros(2)
        self.J: dict[str, Vec] = {}
        self.L: dict[str, float] = {}
        self.hand_at: dict[str, Vec] = {}
        self.lying: dict[str, Any] | None = None

    def estimate_h(self) -> float | None:
        pr = PROP[self.pose["build"]]
        est = []
        for a, b, s in SEGS:
            L = float(np.hypot(*(self.P[b] - self.P[a])))
            if L > 1:
                est.append(L / pr[s])
        if not est:
            return None
        h = float(np.percentile(est, 70))
        self.rec["h_estimates"] = [round(x, 1) for x in sorted(est)]
        return h

    def build(self, h: float) -> None:
        pr = PROP[self.pose["build"]]
        mul = SHAPE_MUL[self.pose["shape"]]
        self.h = h
        P = self.P
        f = self.pose["facing"]
        fd = facing_dir(f)
        self.fwd = fd
        u = unit(P["mid_hip"] - P["neck"])
        p = np.array(
            [u[1], -u[0]]
        )  # perpendicular, toward the picture's right when u points down
        self.u, self.p = u, p
        Jn = {}
        mid = P["mid_hip"].copy()
        neck = mid - u * pr["torso"] * h
        Jn["mid_hip"], Jn["neck"] = mid, neck
        # side of the near limbs: from the painter's own shoulders and hips, else the picture's left
        sd = float(np.dot(P["near_shoulder"] - P["far_shoulder"], p)) + float(
            np.dot(P["near_hip"] - P["far_hip"], p)
        )
        side = 1.0 if sd > 0 else -1.0
        if abs(sd) < 1e-6:
            side = (
                -1.0
                if f in ("front", "back")
                else (1.0 if float(np.dot(fd, p)) > 0 else -1.0)
            )
        self.side = side
        sw = (pr["sw"] * mul.get("sw", 1.0) * mul["w"]) * h
        hw = (pr["hw"] * mul.get("hw", 1.0) * mul["w"]) * h
        if f in ("front", "back"):
            ns, fs = sw - 0.15 * h, sw - 0.15 * h
            nh, fh = hw - 0.22 * h, hw - 0.22 * h
        elif f.startswith("three"):
            ns, fs = (sw - 0.15 * h) * 0.85, (sw - 0.15 * h) * 0.55
            nh, fh = (hw - 0.22 * h) * 0.85, (hw - 0.22 * h) * 0.5
        else:
            ns, fs = 0.06 * h, 0.04 * h
            nh, fh = 0.06 * h, 0.04 * h
        Jn["near_shoulder"] = neck + u * 0.16 * h + p * side * ns
        Jn["far_shoulder"] = neck + u * 0.16 * h - p * side * fs
        Jn["near_hip"] = mid + p * side * nh
        Jn["far_hip"] = mid - p * side * fh
        for s in ("near", "far"):
            for a, b, c, l1, l2 in (
                ("shoulder", "elbow", "wrist", "upper", "fore"),
                ("hip", "knee", "ankle", "thigh", "shin"),
            ):
                r = Jn[f"{s}_{a}"]
                d1 = unit(P[f"{s}_{b}"] - P[f"{s}_{a}"], (0, 1))
                d2 = unit(P[f"{s}_{c}"] - P[f"{s}_{b}"], tuple(d1))
                if (
                    abs(cross(d1, d2)) < math.sin(math.radians(10))
                    and float(np.dot(d1, d2)) > 0
                ):
                    # round 21: a small natural bend on an elbow or knee drawn straight, in every view
                    if f not in ("front", "back"):
                        want = (
                            fd if a == "shoulder" else -fd
                        )  # forearm toward the front, shin toward the back
                        ang = 14 if a == "shoulder" else 8
                    elif a == "shoulder":
                        want = (
                            -p * side if s == "near" else p * side
                        )  # forearm toward the body's midline
                        ang = (
                            10 if s == "near" else 7
                        )  # not mirrored: the two arms differ a little
                    else:
                        want, ang = None, 0
                    if want is not None:
                        cand = [rot(d1, math.radians(ang)), rot(d1, -math.radians(ang))]
                        d2 = max(cand, key=lambda v: float(np.dot(v, want)))
                        self.rec.setdefault("bent", []).append(f"{s}_{b}")
                Jn[f"{s}_{b}"] = r + d1 * pr[l1] * h
                Jn[f"{s}_{c}"] = Jn[f"{s}_{b}"] + d2 * pr[l2] * h
        self.J = Jn
        self.L = {k: pr[k] * h for k in ("upper", "fore", "thigh", "shin", "hand")}

    def translate(self, d: Vec) -> None:
        for k in self.J:
            self.J[k] = self.J[k] + d

    def place(self, anchors: Anchors) -> None:
        """Root: seat anchor, else the painter's lowest supporting joint, else the painter's hips."""
        P, J, h = self.P, self.J, self.h
        sit = [
            c
            for c in self.pose["contacts"]
            if c["joint"] == "hips"
            and c["kind"] in ("sit", "in")
            and c["anchor"] in anchors
        ]
        if sit:
            a = anchors[sit[0]["anchor"]]["p"]
            self.translate(a - np.array([0.0, 0.10 * h]) - J["mid_hip"])
            self.rec["root"] = "seat"
            self.rec["contacts_snapped"].append("hips")
            return
        sup = max(
            ("near_ankle", "far_ankle", "near_knee", "far_knee"), key=lambda k: P[k][1]
        )
        legs = float(np.hypot(*(P["near_knee"] - P["near_hip"]))) + float(
            np.hypot(*(P["near_ankle"] - P["near_knee"]))
        )
        if P[sup][1] > P["mid_hip"][1] + 0.35 * legs:
            self.translate(P[sup] - J[sup])
            self.rec["root"] = sup
        else:
            self.translate(P["mid_hip"] - J["mid_hip"])
            self.rec["root"] = "hips"

    def snap(self, joint: str, target: Vec, label: str, hand: bool = True) -> None:
        J, P = self.J, self.P
        s = joint.split("_")[0]
        if joint.endswith("wrist") or joint.endswith("elbow"):
            a, b, c, l1, l2 = (
                f"{s}_shoulder",
                f"{s}_elbow",
                f"{s}_wrist",
                self.L["upper"],
                self.L["fore"],
            )
            fb = np.array([0.0, 1.0])
        else:
            a, b, c, l1, l2 = (
                f"{s}_hip",
                f"{s}_knee",
                f"{s}_ankle",
                self.L["thigh"],
                self.L["shin"],
            )
            fb = self.fwd if float(np.hypot(*self.fwd)) > 0 else np.array([0.0, -1.0])
        extra = 0.12 * self.h if (hand and c.endswith("wrist")) else 0.0
        sign = bend_sign(P[a], P[b], P[c], fb)
        mid, end, k = two_bone(J[a], target, l1, l2 + extra, sign, self.rec, label)
        J[b] = mid
        J[c] = mid + unit(end - mid) * (float(np.hypot(*(end - mid))) - extra * k)
        if c.endswith("wrist"):
            self.hand_at[s] = end
        if label not in self.rec["contacts_missed"]:
            self.rec["contacts_snapped"].append(label)


def limb_centre(
    a: Vec, b: Vec, c: Vec, n: int = 10, sag: float = 0.035
) -> tuple[list[Vec], list[float]]:
    """Centre line shoulder->elbow->wrist (or hip->knee->ankle) with a slight natural curve: each segment bows outward (away from the
    bend) by sag x its length; returns points and the parameter (0..1 on the first segment, 1..2 on the second)."""
    bend = cross(b - a, c - b)
    pts, ts = [], []
    for k, (p0, p1) in enumerate(((a, b), (b, c))):
        d = p1 - p0
        L = float(np.hypot(*d))
        nrm = np.array([-d[1], d[0]]) / max(L, 1e-6)
        sgn = -1.0 if bend > 0 else 1.0
        if abs(bend) < 1e-6:
            sgn = 1.0 if k == 0 else -1.0  # a straight limb gets a soft S
        for i in range(n + (1 if k == 1 else 0)):
            t = i / n
            pts.append(p0 + d * t + nrm * sgn * sag * L * math.sin(math.pi * t))
            ts.append(k + t)
    return pts, ts


def limb_outline(
    a: Vec, b: Vec, c: Vec, radius: Callable[[float], float], cap_a: bool = True
) -> str:
    """One smooth closed outline around the centre line, radius(t) for t in 0..2 (wide at the root, narrow at the end), round end cap."""
    pts, ts = limb_centre(a, b, c)
    n = len(pts)
    left, right = [], []
    for i in range(n):
        d = pts[min(i + 1, n - 1)] - pts[max(i - 1, 0)]
        d = unit(d)
        nr = np.array([-d[1], d[0]])
        r = radius(ts[i])
        left.append(pts[i] + nr * r)
        right.append(pts[i] - nr * r)
    # end cap (half circle) at c, start cap at a
    dc = unit(pts[-1] - pts[-2])
    rc = radius(ts[-1])
    cap_c = [
        pts[-1] + rot(np.array([-dc[1], dc[0]]), -math.pi * k / 4) * rc
        for k in range(1, 4)
    ]
    da = unit(pts[1] - pts[0])
    ra = radius(ts[0])
    cap0 = (
        [
            pts[0] + rot(np.array([da[1], -da[0]]), -math.pi * k / 4) * ra
            for k in range(1, 4)
        ]
        if cap_a
        else []
    )
    ring = left + cap_c + right[::-1] + cap0
    return smooth(ring)


def swell(
    r0: float, r1: float, k: float = 0.12, peak: float = 0.35
) -> Callable[[float], float]:
    """Radius along one segment: linear from r0 to r1 with a muscle swell of k x r0 peaking at t = peak."""
    e = math.log(0.5) / math.log(peak)

    def f(t: float) -> float:
        t = max(0.0, min(1.0, t))
        return r0 + (r1 - r0) * t + k * r0 * math.sin(math.pi * t**e)

    return f


def fold(p0: Vec, p1: Vec, bow: Vec) -> str:
    """A short curved fold line from p0 to p1 bowing by `bow` (vector)."""
    m = (p0 + p1) / 2 + bow
    return f"M{fmt(p0)} Q{fmt(m)} {fmt(p1)}"


def draw_person(pp: Person, style: Style) -> dict[str, Any]:
    """-> dict of layers: far (under a seat), body, near, hands_over (list per side)."""
    pose, J, h = pp.pose, pp.J, pp.h
    pr = PROP[pose["build"]]
    mul = SHAPE_MUL[pose["shape"]]
    cl = pose["clothes"]
    skin = pose["skin"]
    f = pose["facing"]
    fw = FACING_W[f]
    u, p, side, fd = pp.u, pp.p, pp.side, pp.fwd
    rm = mul["r"]
    gloves = next((x["color"] for x in cl["extra"] if x["type"] == "gloves"), None)
    sleeve_c = cl["top_c"]
    leg_c = {
        "trousers": cl["bottom_c"],
        "shorts": skin,
        "skirt": skin,
        "long-skirt": skin,
        "none": skin,
    }[cl["bottom"]]
    if (
        cl["top"] in ("dress", "robe")
        and cl["bottom"] in ("none", "trousers")
        and cl["top"] == "robe"
    ):
        leg_c = skin
    hand_c = gloves or skin
    profile = f.startswith("profile")
    front = f in ("front", "back")

    def arm(layer: Layer, s: str, darker: bool = False) -> None:
        """round 21: one tapered, slightly curved outline shoulder -> wrist; sleeves flare at the cuff, fold at the elbow."""
        sh, el_, wr = J[f"{s}_shoulder"], J[f"{s}_elbow"], J[f"{s}_wrist"]
        r_sh, r_el, r_wr = (
            pr["r_up"][0] * rm * h * (1.0 if pose["build"] == "child" else 1.08),
            (pr["r_up"][1] + pr["r_fore"][0]) / 2 * rm * h,
            pr["r_fore"][1] * rm * h * 0.92,
        )
        sl = cl["sleeves"]
        up, fo = swell(r_sh, r_el, 0.10, 0.3), swell(r_el, r_wr, 0.16, 0.25)

        def r_arm(t: float) -> float:
            return up(t) if t <= 1 else fo(t - 1)

        if sl == "long":
            cuff = (
                1.18
                if cl["top"] in ("shirt", "jacket", "coat", "sweater", "robe", "dress")
                else 1.08
            )

            def r_sleeve(t: float) -> float:
                return r_arm(t) * (
                    1.06 if t <= 1 else 1.06 + (cuff - 1.06) * max(0.0, (t - 1.6) / 0.4)
                )

            layer.add(limb_outline(sh, el_, wr, r_sleeve), sleeve_c)
            ink = shade(sleeve_c, -0.28)
            bd = unit(el_ - sh)
            nrm = np.array([-bd[1], bd[0]])
            inner = nrm if cross(el_ - sh, wr - el_) > 0 else -nrm
            if abs(cross(unit(el_ - sh), unit(wr - el_))) > 0.08:
                layer.line(
                    fold(
                        el_ + inner * r_el * 0.9 - bd * r_el * 0.6,
                        el_ + inner * r_el * 0.1 + bd * r_el * 0.3,
                        -inner * r_el * 0.15,
                    ),
                    ink,
                )
            fd2 = unit(wr - el_)
            layer.line(
                fold(
                    wr - fd2 * r_wr * 1.1 + np.array([-fd2[1], fd2[0]]) * r_wr * 0.9,
                    wr - fd2 * r_wr * 1.1 - np.array([-fd2[1], fd2[0]]) * r_wr * 0.9,
                    -fd2 * r_wr * 0.25,
                ),
                ink,
            )
        else:
            layer.add(limb_outline(sh, el_, wr, r_arm), skin)
            if sl == "short":
                hem = sh + (el_ - sh) * 0.55
                d = unit(el_ - sh)
                nr = np.array([-d[1], d[0]])
                ra, rb = r_sh * 1.12, r_arm(0.55) * 1.42
                pts = [
                    sh - d * ra * 0.9 + nr * ra * 0.7,
                    sh - d * ra * 0.9 - nr * ra * 0.7,
                    sh - nr * ra * 1.05,
                    hem - nr * rb,
                    hem + d * rb * 0.12,
                    hem + nr * rb,
                    sh + nr * ra * 1.05,
                ]
                layer.add(rounded(pts, 0.3), sleeve_c)
                layer.line(
                    fold(
                        hem - nr * rb * 0.6 - d * rb * 0.25,
                        hem - nr * rb * 0.1 - d * rb * 0.45,
                        d * rb * 0.08,
                    ),
                    shade(sleeve_c, -0.28),
                )

    def hand(layer: Layer, s: str) -> None:
        wr, el_ = J[f"{s}_wrist"], J[f"{s}_elbow"]
        d = unit(wr - el_)
        held = pp.hand_at.get(s)
        k = 1.0 if pose["build"] != "child" else 0.9
        if held is not None:
            c = wr + d * 0.09 * h
            layer.add(circle_d(c, 0.115 * h * k), hand_c)
            layer.add(
                capsule(
                    c - p * 0.02 * h,
                    c + rot(d, -side * 1.2) * 0.09 * h,
                    0.04 * h,
                    0.035 * h,
                ),
                hand_c,
                outline=False,
            )
        else:
            c = wr + d * 0.16 * h * k
            ang = math.atan2(d[1], d[0])
            layer.add(ellipse_d(c, 0.18 * h * k, 0.105 * h * k, ang), hand_c)
            tdir = rot(
                d,
                -0.9
                if float(np.dot(rot(d, -0.9), np.array([0, -1.0])))
                > float(np.dot(rot(d, 0.9), np.array([0, -1.0])))
                else 0.9,
            )
            layer.add(
                capsule(
                    wr + d * 0.06 * h,
                    wr + d * 0.06 * h + tdir * 0.12 * h,
                    0.045 * h * k,
                    0.04 * h * k,
                ),
                hand_c,
            )

    def leg(layer: Layer, s: str) -> None:
        """round 21: one tapered outline hip -> ankle (thigh swell, narrow ankle); trousers flare a little at the hem, fold at the knee."""
        hp, kn, an = J[f"{s}_hip"], J[f"{s}_knee"], J[f"{s}_ankle"]
        r_hp, r_kn, r_an = (
            pr["r_thigh"][0] * rm * h * (1.0 if pose["build"] == "child" else 1.05),
            (pr["r_thigh"][1] + pr["r_shin"][0]) / 2 * rm * h,
            pr["r_shin"][1] * rm * h * 0.9,
        )
        th, sh_ = swell(r_hp, r_kn, 0.06, 0.3), swell(r_kn, r_an, 0.14, 0.3)

        def r_leg(t: float) -> float:
            return th(t) if t <= 1 else sh_(t - 1)

        if cl["bottom"] == "trousers":

            def r_tr(t: float) -> float:
                return r_leg(t) * (1.08 if t <= 1.5 else 1.08 + 0.22 * (t - 1.5) / 0.5)

            layer.add(limb_outline(hp, kn, an, r_tr), leg_c)
            ink = shade(leg_c, -0.3)
            bk = cross(kn - hp, an - kn)
            if abs(cross(unit(kn - hp), unit(an - kn))) > 0.1:
                d = unit(kn - hp)
                nr = np.array([-d[1], d[0]])
                back = -nr if bk > 0 else nr
                layer.line(
                    fold(
                        kn + back * r_kn * 0.85 - d * r_kn * 0.5,
                        kn + back * r_kn * 0.2 + d * r_kn * 0.2,
                        -back * r_kn * 0.1,
                    ),
                    ink,
                )
            sd = unit(an - kn)
            nr2 = np.array([-sd[1], sd[0]])
            layer.line(
                fold(
                    an - sd * r_an * 1.6 + nr2 * r_an * 0.9,
                    an - sd * r_an * 1.2 - nr2 * r_an * 0.6,
                    sd * r_an * 0.2,
                ),
                ink,
            )
        else:
            layer.add(limb_outline(hp, kn, an, r_leg), leg_c)
            if cl["bottom"] == "shorts":
                hem = hp + (kn - hp) * 0.62
                d = unit(kn - hp)
                nr = np.array([-d[1], d[0]])
                ra, rb = r_hp * 1.1, th(0.62) * 1.3
                pts = [
                    hp - d * ra * 0.6 - nr * ra,
                    hem - nr * rb,
                    hem + d * rb * 0.1,
                    hem + nr * rb,
                    hp - d * ra * 0.6 + nr * ra,
                ]
                layer.add(rounded(pts, 0.25), cl["bottom_c"])
                layer.line(
                    fold(
                        hem - nr * rb * 0.5 - d * rb * 0.3,
                        hem - nr * rb * 0.05 - d * rb * 0.5,
                        d * rb * 0.08,
                    ),
                    shade(cl["bottom_c"], -0.3),
                )
        foot(layer, s, an, kn)

    def foot(layer: Layer, s: str, an: Vec, kn: Vec) -> None:
        sd = unit(an - kn)
        shoe = cl["shoes_c"] if cl["shoes"] != "bare" else skin
        if cl["shoes"] == "boots":
            layer.add(
                capsule(
                    an - sd * 0.42 * h,
                    an,
                    pr["r_shin"][1] * rm * h * 1.25,
                    pr["r_shin"][1] * rm * h * 1.25,
                ),
                shoe,
            )
        if front or float(np.hypot(*fd)) == 0:
            fdir = unit(
                np.array([sd[1], -sd[0]]) * (1 if s == "near" else -1) * side * 0.35
                + sd
            )
            c = an + sd * 0.06 * h
            layer.add(
                ellipse_d(
                    c + fdir * 0.05 * h,
                    0.15 * h,
                    0.11 * h,
                    math.atan2(fdir[1], fdir[0]),
                ),
                shoe,
            )
            return
        perp = np.array([-sd[1], sd[0]])
        fdir = perp if float(np.dot(perp, fd)) >= 0 else -perp
        if f.startswith("three"):
            fdir = unit(fdir * 0.8 + sd * 0.25)
        k = pr["foot"] / 0.5
        pts = [
            an - fdir * 0.10 * h * k - sd * 0.02 * h,
            an - fdir * 0.13 * h * k + sd * 0.12 * h,
            an + fdir * 0.30 * h * k + sd * 0.13 * h,
            an + fdir * 0.40 * h * k + sd * 0.08 * h,
            an + fdir * 0.32 * h * k + sd * 0.0 * h,
            an + fdir * 0.08 * h * k - sd * 0.06 * h,
        ]
        layer.add(smooth(pts), shoe)
        if cl["shoes"] == "sneakers":
            layer.add(
                capsule(
                    an - fdir * 0.11 * h * k + sd * 0.115 * h,
                    an + fdir * 0.33 * h * k + sd * 0.115 * h,
                    0.025 * h,
                    0.025 * h,
                ),
                "#f4f4f4",
                outline=False,
            )

    N, H = J["neck"], J["mid_hip"]
    Lt = float(np.hypot(*(H - N)))
    sw = pr["sw"] * mul.get("sw", 1.0) * mul["w"] * h * fw
    cw = pr["cw"] * mul.get("cw", 1.0) * mul["w"] * h * fw
    ww = pr["ww"] * mul.get("ww", 1.0) * mul["w"] * h * max(fw, 0.62)
    hw = pr["hw"] * mul.get("hw", 1.0) * mul["w"] * h * max(fw, 0.62)
    belly = (0.10 if pose["shape"] == "round" else 0.03) * h
    chest_f = fd * (0.05 * h if profile else 0.0)
    bel_f = fd * (belly if profile else 0.0)
    back_f = -fd * (0.02 * h if profile else 0.0)

    def torso_pts(extra: float = 0.0) -> list[Vec]:
        return [
            N - p * 0.17 * h * max(fw, 0.6) + u * 0.02 * h,
            N - p * sw * 0.85 + u * 0.07 * h,
            N - p * (sw + extra) + u * 0.22 * h,
            N
            - p * (cw + extra)
            + u * 0.45 * Lt
            + (chest_f if float(np.dot(-p, fd)) > 0 else back_f),
            N
            - p * (ww + extra)
            + u * 0.72 * Lt
            + (bel_f if float(np.dot(-p, fd)) > 0 else 0),
            H - p * (hw + extra) + u * 0.08 * h,
            H - p * hw * 0.6 + u * 0.22 * h,
            H + p * hw * 0.6 + u * 0.22 * h,
            H + p * (hw + extra) + u * 0.08 * h,
            N
            + p * (ww + extra)
            + u * 0.72 * Lt
            + (bel_f if float(np.dot(p, fd)) > 0 else 0),
            N
            + p * (cw + extra)
            + u * 0.45 * Lt
            + (chest_f if float(np.dot(p, fd)) > 0 else back_f),
            N + p * (sw + extra) + u * 0.22 * h,
            N + p * sw * 0.85 + u * 0.07 * h,
            N + p * 0.17 * h * max(fw, 0.6) + u * 0.02 * h,
        ]

    far = Layer(pp.name + "-far")
    body = Layer(pp.name + "-body")
    near = Layer(pp.name + "-near")
    far_arm_layer = Layer(pp.name + "-far-arm")
    # legs: far leg under, near leg over (side views); both in the body layer seen from the front
    if front:
        leg(body, "far")
        leg(body, "near")
    else:
        leg(far, "far")
        leg(body, "near")
    # pelvis in the bottom colour (trousers, shorts) or the top colour (dress, robe) or skin under a skirt
    pel_c = (
        cl["bottom_c"]
        if cl["bottom"] in ("trousers", "shorts")
        else (cl["top_c"] if cl["top"] in ("dress", "robe") else cl["bottom_c"])
    )
    pel = [
        N - p * ww + u * 0.75 * Lt,
        H - p * (hw + 0.02 * h),
        J["near_hip"] + unit(J["near_knee"] - J["near_hip"]) * 0.28 * h,
        J["far_hip"] + unit(J["far_knee"] - J["far_hip"]) * 0.28 * h,
        H + p * (hw + 0.02 * h),
        N + p * ww + u * 0.75 * Lt,
    ]
    body.add(rounded(hull(pel), 0.3), pel_c)
    # neck
    chin = N - u * pr["neck"] * h
    body.add(
        capsule(
            N + u * 0.05 * h,
            chin - u * 0.05 * h,
            pr["r_neck"] * h * max(fw, 0.75),
            pr["r_neck"] * h * max(fw, 0.75),
        ),
        skin,
    )
    # torso (the top)
    body.add(smooth(torso_pts()), cl["top_c"])
    # skirts and long garments over the legs
    hem = None
    if cl["bottom"] in ("skirt", "long-skirt") or cl["top"] in (
        "dress",
        "robe",
        "coat",
    ):
        long_ = cl["bottom"] == "long-skirt" or cl["top"] == "robe"
        frac = (
            0.85
            if long_
            else (0.15 if cl["top"] == "coat" and cl["bottom"] == "trousers" else 0.1)
        )
        pts = [
            N - p * ww + u * 0.7 * Lt,
            N + p * ww + u * 0.7 * Lt,
            H - p * hw,
            H + p * hw,
        ]
        for s in ("near", "far"):
            kn, an, hp = J[f"{s}_knee"], J[f"{s}_ankle"], J[f"{s}_hip"]
            if cl["top"] == "coat" and not long_:
                at = hp + (kn - hp) * 0.75
                dd = unit(kn - hp)
            else:
                at = kn + (an - kn) * frac
                dd = unit(an - kn)
            pn = np.array([dd[1], -dd[0]])
            flare = (0.30 if long_ else 0.26) * h
            pts += [at + pn * flare, at - pn * flare]
        hem = rounded(hull(pts), 0.22)
        skirt_c = (
            cl["top_c"]
            if cl["top"] in ("dress", "robe", "coat")
            and cl["bottom"] not in ("skirt", "long-skirt")
            else cl["bottom_c"]
        )
        body.add(hem, skirt_c)
        body.add(smooth(torso_pts()), cl["top_c"], outline=False)
    # clothing details
    if not front or f == "front":
        if cl["top"] in ("jacket", "coat") and not profile:
            body.add(
                capsule(
                    N + u * 0.1 * h + p * side * 0.02 * h,
                    H + u * 0.15 * h + p * side * 0.02 * h,
                    0.012 * h,
                    0.012 * h,
                ),
                style.ink(cl["top_c"]),
                outline=False,
            )
        if cl["top"] in ("shirt", "jacket", "coat"):
            for sgn in (-1, 1):
                body.add(
                    smooth(
                        [
                            N + p * sgn * 0.05 * h + u * 0.0 * h,
                            N + p * sgn * 0.22 * h * max(fw, 0.6) + u * 0.0 * h,
                            N + p * sgn * 0.08 * h + u * 0.2 * h,
                        ]
                    ),
                    shade(cl["top_c"], 0.25 if cl["top"] == "shirt" else -0.15),
                    outline=False,
                )
    for x in cl["extra"]:
        c = x["color"]
        if x["type"] == "apron" and f != "back":
            top_y = 0.30 * Lt
            kn = (J["near_knee"] + J["far_knee"]) / 2
            aw = cw * 0.55 if not profile else 0.10 * h
            off = p * float(np.dot(fd, p)) * (cw * 0.75) if profile else 0
            bot = H + (kn - H) * 0.8
            pts = [
                N + u * top_y - p * aw * 0.8 + off,
                N + u * top_y + p * aw * 0.8 + off,
                N + u * 0.78 * Lt + p * (ww * 0.85 if not profile else aw) + off,
                bot + p * (hw * 0.95 if not profile else aw * 1.1) + off,
                bot - p * (hw * 0.95 if not profile else aw * 1.1) + off,
                N + u * 0.78 * Lt - p * (ww * 0.85 if not profile else aw) + off,
            ]
            body.add(rounded(pts, 0.15), c)
            body.add(
                capsule(
                    N - p * 0.1 * h + u * 0.02 * h,
                    N + u * top_y - p * aw * 0.7 + off,
                    0.02 * h,
                    0.02 * h,
                ),
                c,
                outline=False,
            )
            body.add(
                capsule(
                    N + p * 0.1 * h + u * 0.02 * h,
                    N + u * top_y + p * aw * 0.7 + off,
                    0.02 * h,
                    0.02 * h,
                ),
                c,
                outline=False,
            )
        elif x["type"] == "vest" and f != "back":
            for sgn in (-1, 1):
                pts = [
                    N + p * sgn * 0.12 * h + u * 0.05 * h,
                    N + p * sgn * (sw * 0.8) + u * 0.18 * h,
                    N + p * sgn * (ww * 1.0) + u * 0.8 * Lt,
                    N + p * sgn * 0.04 * h + u * 0.95 * Lt,
                ]
                body.add(smooth(pts), c, outline=False)
        elif x["type"] == "belt":
            body.add(
                capsule(
                    N - p * ww * 1.02 + u * 0.8 * Lt,
                    N + p * ww * 1.02 + u * 0.8 * Lt,
                    0.045 * h,
                    0.045 * h,
                ),
                c,
                outline=False,
            )
        elif x["type"] == "scarf":
            body.add(
                capsule(
                    N - p * 0.2 * h * max(fw, 0.7) + u * 0.01 * h,
                    N + p * 0.2 * h * max(fw, 0.7) + u * 0.01 * h,
                    0.08 * h,
                    0.08 * h,
                ),
                c,
            )
            tail0 = N + p * side * 0.08 * h + u * 0.04 * h
            body.add(
                capsule(
                    tail0, tail0 + u * 0.55 * h + fd * 0.05 * h, 0.07 * h, 0.06 * h
                ),
                c,
            )
    # arms: far arm under the torso in side views; both over it seen from the front
    if front:
        arm(near, "far")
        hand(near, "far")
    else:
        arm(far_arm_layer, "far")
        hand(far_arm_layer, "far")
    arm(near, "near")
    hand(near, "near")
    hands_over = {}
    for s in ("near", "far"):
        if pp.hand_at.get(s) is not None:
            hl = Layer(f"{pp.name}-hand-{s}")
            hand(hl, s)
            hands_over[s] = hl
    return {
        "far": far,
        "far_arm": far_arm_layer,
        "body": body,
        "near": near,
        "hands_over": hands_over,
        "chin": chin,
    }


# ---------------------------------------------------------------- round 21: silhouettes, containment, hair, anchors, uncrossing
def _fill_none(n: ET.Element) -> bool:
    f = (n.get("fill") or "").strip().lower()
    m = re.search(r"(?:^|;)\s*fill\s*:\s*([^;]+)", n.get("style") or "")
    if m:
        f = m.group(1).strip().lower()
    fo = (n.get("fill-opacity") or "").strip()
    m = re.search(r"fill-opacity\s*:\s*([^;]+)", n.get("style") or "")
    if m:
        fo = m.group(1).strip()
    try:
        if fo and float(fo) == 0:
            return True
    except ValueError:
        pass
    return f in ("none", "transparent")


def _stroke_on(n: ET.Element) -> bool:
    s_ = (n.get("stroke") or "").strip().lower()
    m = re.search(r"(?:^|;)\s*stroke\s*:\s*([^;]+)", n.get("style") or "")
    if m:
        s_ = m.group(1).strip().lower()
    return bool(s_) and s_ not in ("none", "transparent")


def sil(
    doc: Doc, shapes: Iterable[ET.Element], px: int = 512
) -> tuple[Mask, float, float, float]:
    """Silhouette (bool array) of the given leaf shapes alone, over the whole frame, px pixels wide; -> (mask, unit per pixel, x0, y0)."""
    x, y, w, hh = doc.vb
    ph = max(8, int(round(px * hh / w)))
    root = el("svg", viewBox=f"{x} {y} {w} {hh}", width=px, height=ph)
    for sh in shapes:
        t = tag(sh)
        if t in ("text", "use") or (sh.get("id") or "").startswith("anchor-"):
            continue
        par = doc.parent.get(id(sh))
        T = doc.ctm(par) if par is not None and par is not doc.root else np.eye(3)
        c = copy.deepcopy(sh)
        for k in (
            "style",
            "class",
            "filter",
            "mask",
            "clip-path",
            "opacity",
            "fill-opacity",
            "stroke-opacity",
        ):
            if k in c.attrib:
                del c.attrib[k]
        for ch in list(c):
            c.remove(ch)
        nofill = t in ("line", "polyline") or _fill_none(sh)
        c.set("fill", "none" if nofill else "#000000")
        if _stroke_on(sh):
            c.set("stroke", "#000000")
        elif "stroke" in c.attrib:
            del c.attrib["stroke"]
        wrap = el("g", transform=mstr(T)) if not np.allclose(T, np.eye(3)) else el("g")
        wrap.append(c)
        root.append(wrap)
    if not len(root):
        return np.zeros((ph, px), bool), w / px, x, y
    a = render.pixels(ET.tostring(root, encoding="unicode"), px, ph)[..., 3]
    return a >= 100, w / px, x, y


def mask_box(m: Mask, sc: float, ox: float, oy: float) -> list[float] | None:
    ys, xs = np.nonzero(m)
    if len(xs) == 0:
        return None
    return [
        ox + xs.min() * sc,
        oy + ys.min() * sc,
        ox + (xs.max() + 1) * sc,
        oy + (ys.max() + 1) * sc,
    ]


def seg_cross(a: Vec, b: Vec, c: Vec, d: Vec) -> bool:
    """Proper intersection of segments ab and cd."""

    def o(p: Vec, q: Vec, r: Vec) -> float:
        return cross(q - p, r - p)

    o1, o2, o3, o4 = o(a, b, c), o(a, b, d), o(c, d, a), o(c, d, b)
    return (o1 * o2 < 0) and (o3 * o4 < 0)


def arms_cross(J: dict[str, Vec]) -> bool:
    na = [(J["near_shoulder"], J["near_elbow"]), (J["near_elbow"], J["near_wrist"])]
    fa = [(J["far_shoulder"], J["far_elbow"]), (J["far_elbow"], J["far_wrist"])]
    return any(seg_cross(a, b, c, d) for a, b in na for c, d in fa)


def uncross(pp: Person) -> None:
    """Arms of one person crossing in an X after the contacts: swap the two hand targets if that removes the crossing."""
    ha = pp.hand_at
    if not arms_cross(pp.J):
        return
    if "near" not in ha or "far" not in ha:
        pp.rec["arms_crossed"] = "kept (one free hand)"
        return
    saveJ = {k: v.copy() for k, v in pp.J.items()}
    save_ha, save_rec = (
        dict(ha),
        (
            list(pp.rec["contacts_snapped"]),
            list(pp.rec["contacts_missed"]),
            list(pp.rec["stretched"]),
        ),
    )
    tn, tf = ha["near"], ha["far"]
    pp.snap("near_wrist", tf, "near_wrist@swap", hand=True)
    pp.snap("far_wrist", tn, "far_wrist@swap", hand=True)
    if (
        arms_cross(pp.J)
        or "near_wrist@swap" in pp.rec["contacts_missed"]
        or "far_wrist@swap" in pp.rec["contacts_missed"]
    ):
        pp.J = saveJ
        pp.hand_at = save_ha
        pp.rec["contacts_snapped"], pp.rec["contacts_missed"], pp.rec["stretched"] = (
            save_rec
        )
        pp.rec["arms_crossed"] = "kept (swap does not help)"
    else:
        pp.rec["arms_crossed"] = "uncrossed by swapping hand targets"


def head_centre(pp: Person) -> Vec:
    pr = PROP[pp.pose["build"]]
    centre: Vec = pp.J["neck"] - pp.u * (pr["neck"] + 0.5) * pp.h
    return centre


def translate_group(doc: Doc, g: ET.Element, delta: Vec) -> None:
    par = doc.parent.get(id(g))
    Cp = doc.ctm(par) if par is not None and par is not doc.root else np.eye(3)
    L = np.linalg.inv(Cp[:2, :2])
    dl = L @ np.asarray(delta, float)
    g.set(
        "transform", f"translate({dl[0]:.2f},{dl[1]:.2f}) " + (g.get("transform") or "")
    )


def anchors_off_face(
    doc: Doc, persons: list[Person], anchors: Anchors, posed_groups: list[ET.Element]
) -> None:
    """A held thing (hold or touch contact) whose anchor lands on the person's head, or whose shape covers a third of the face, moves with
    its anchors: a load onto the near shoulder (then pushed back, or outward in a front view, until the face is clear); a thing held
    overhead (its shape mostly above the anchor) up until the face is clear, then sideways."""
    moved = set()
    for pp in persons:
        hc = head_centre(pp)
        hr = 0.5 * pp.h
        for c in pp.pose["contacts"]:
            if c["kind"] not in ("hold", "touch") or ":" in c["anchor"]:
                continue
            a = find_anchor(anchors, c["anchor"])
            if a is None:
                continue
            owner = anchors[a]["owner"]
            if (
                owner is None
                or id(owner) in moved
                or owner is pp.group
                or pp.group in doc.ancestors(owner)
                or owner in doc.ancestors(pp.group)
                or any(owner is q or q in doc.ancestors(owner) for q in posed_groups)
            ):
                continue
            m, sc, ox, oy = sil(doc, doc.shapes_under(owner), 256)
            ob = mask_box(m, sc, ox, oy)
            if ob is None or m.sum() * sc * sc > 8 * pp.h * pp.h:
                continue
            yy, xx = np.mgrid[0 : m.shape[0], 0 : m.shape[1]]
            disc = (
                (ox + (xx + 0.5) * sc - hc[0]) ** 2
                + (oy + (yy + 0.5) * sc - hc[1]) ** 2
            ) <= hr * hr
            di, dj = np.nonzero(disc)
            if not len(di):
                continue

            def cover(delta: Vec) -> float:
                si = di - int(round(delta[1] / sc))
                sj = dj - int(round(delta[0] / sc))
                ok = (si >= 0) & (si < m.shape[0]) & (sj >= 0) & (sj < m.shape[1])
                return float(m[si[ok], sj[ok]].sum()) / len(di)

            q = anchors[a]["p"]
            on_face = float(np.hypot(*(q - hc))) < 0.6 * pp.h
            c0 = cover(np.zeros(2))
            if not (on_face or c0 > 0.33):
                continue
            overhead = (ob[1] + ob[3]) / 2 < q[1] - 0.3 * (ob[3] - ob[1])
            out = pp.p * pp.side
            back = -pp.fwd if float(np.hypot(*pp.fwd)) > 0 else out
            sh = pp.J["near_shoulder"]
            if overhead:
                tries = [np.array([0.0, -0.1 * k * pp.h]) for k in range(0, 21)] + [
                    out * 0.1 * k * pp.h for k in range(1, 16)
                ]
                kind = "overhead: up"
            else:
                base = np.array(
                    [sh[0] - (ob[0] + ob[2]) / 2, sh[1] - 0.12 * pp.h - ob[3]]
                ) + np.array([0.0, 0.35 * (ob[3] - ob[1])])
                tries = [base + back * 0.1 * k * pp.h for k in range(0, 16)]
                kind = "load: on the shoulder"
            best = None
            for k, d in enumerate(tries):
                if cover(d) < 0.12:
                    best = d
                    break
            if best is None:
                best = min(tries, key=cover)
            translate_group(doc, owner, best)
            moved.add(id(owner))
            for k2, v in anchors.items():
                if v["owner"] is owner:
                    v["p"] = v["p"] + best
            pp.rec.setdefault("anchor_off_face", []).append(
                f"{a}: {kind}, face covered {c0:.2f} -> {cover(best):.2f}, moved {np.hypot(*best) / pp.h:.1f} head"
            )


def _col_top(m: Mask) -> Ints:
    """Per column, the first filled row (or -1)."""
    any_ = m.any(axis=0)
    top = np.where(any_, m.argmax(axis=0), -1)
    return top


def _col_bottom(m: Mask) -> Ints:
    any_ = m.any(axis=0)
    bot = np.where(any_, m.shape[0] - 1 - m[::-1].argmax(axis=0), -1)
    return bot


def contain(
    doc: Doc,
    pp: Person,
    cands: list[ET.Element],
    style: Style,
    anchors: Anchors,
    force: bool = False,
) -> dict[str, Any] | None:
    """Round 21 containment by code. -> dict for the record, or None. Clips the person group below the rim, moves it over the thing,
    draws the rim line over the body."""
    J, h, g = pp.J, pp.h, pp.group
    H = J["mid_hip"]
    knees = [J["near_knee"], J["far_knee"]]
    tried = []
    _holds = {
        find_anchor(anchors, c["anchor"])
        for c in pp.pose["contacts"]
        if c["kind"] in ("hold", "touch") and ":" not in c["anchor"]
    }
    steps = {
        find_anchor(anchors, c["anchor"])
        for c in pp.pose["contacts"]
        if c["kind"] == "step" and ":" not in c["anchor"]
    }
    xs_body = [float(v[0]) for v in J.values()]
    px0, px1 = min(xs_body) - 0.6 * h, max(xs_body) + 0.6 * h
    for T in cands:
        tid = T.get("id")
        shapes = [
            s
            for s in doc.shapes_under(T)
            if tag(s) not in ("text", "use")
            and not (s.get("id") or "").startswith("anchor-")
        ]
        if not shapes:
            continue
        M, sc, ox, oy = sil(doc, shapes)
        vw, vh = doc.vb[2], doc.vb[3]
        if M.sum() * sc * sc > 0.45 * vw * vh:
            tried.append(f"{tid}: too large (background)")
            continue
        k = max(1, int(round(0.12 * h / sc)))
        Mc = ndi.binary_closing(M, structure=np.ones((k, k), bool)) | M
        Mf = ndi.binary_fill_holes(Mc)
        H_, W_ = M.shape

        def ij(q: Vec) -> tuple[int, int]:
            return int((q[1] - oy) / sc), int((q[0] - ox) / sc)

        def inside(q: Vec) -> bool:
            i, j = ij(q)
            return 0 <= i < H_ and 0 <= j < W_ and bool(Mf[i, j])

        def above(q: Vec) -> bool:
            i, j = ij(q)
            if not (0 <= j < W_):
                return False
            col = Mf[max(0, i) : min(H_, i + int(0.35 * h / sc) + 1), j]
            if not col.any():
                return False
            first = max(0, i) + int(col.argmax())
            run = Mf[first : min(H_, first + int(0.15 * h / sc) + 1), j]
            return bool(run.all())

        hips_in = inside(H) or above(H)
        knees_in = all(inside(q) or above(q) for q in knees)
        if not (hips_in or knees_in) and not force:
            continue
        owned = {
            a
            for a, v in anchors.items()
            if v["owner"] is T
            or (v["owner"] is not None and T in doc.ancestors(v["owner"]))
        }
        if steps & owned and not force:
            tried.append(f"{tid}: ridden (foot contact on it)")
            continue
        if pp.rec.get("in_front_of_body") == tid and not force:
            tried.append(f"{tid}: held in front of the body")
            continue
        ref = H if (hips_in or not knees_in) else max(knees, key=lambda q: q[1])
        i0, j0 = ij(ref)
        i0 = max(0, min(H_ - 1, i0))
        colm = Mc[i0:, max(0, j0) : j0 + 1]
        start = i0 + (int(colm[:, 0].argmax()) if colm.size and colm[:, 0].any() else 0)
        b0, b1 = int(start), min(H_, int(start + 0.5 * h / sc) + 1)
        c0, c1 = (
            max(0, int((ref[0] - 0.4 * h - ox) / sc)),
            min(W_, int((ref[0] + 0.4 * h - ox) / sc) + 1),
        )
        cov = float(Mc[b0:b1, c0:c1].mean()) if b1 > b0 and c1 > c0 else 0.0
        if cov < 0.45 and not force:
            tried.append(f"{tid}: sparse under the contact ({cov:.2f})")
            continue
        # rim shapes
        cj0, cj1 = max(0, int((px0 - ox) / sc)), min(W_, int((px1 - ox) / sc) + 1)
        lo, hi = ref[1] - 0.9 * h, ref[1] + 0.6 * h
        opens: list[str] = []
        masks = []
        for s_ in shapes:
            ms, _, _, _ = sil(doc, [s_])
            sub = ms[:, cj0:cj1]
            if not sub.any():
                continue
            top = _col_top(sub)
            tv = top[top >= 0]
            mt = oy + float(np.median(tv)) * sc
            rows = np.nonzero(sub.any(axis=1))[0]
            mid_row = (
                oy + float(np.median(np.nonzero(sub)[0])) * sc if len(rows) else mt
            )
            if (
                lo <= mt <= hi and mid_row >= ref[1] - 0.1 * h
            ):  # a shape lying mostly above the hips (a backrest) is behind, not a rim
                masks.append((s_, ms))
        if not masks:
            tried.append(f"{tid}: no rim shape near the hips")
            continue
        U = np.zeros_like(M)
        for _, ms in masks:
            U |= ms
        utop, ubot = _col_top(U), _col_bottom(U)
        rim = utop.astype(float)
        for s_, ms in masks:
            t_, b_ = _col_top(ms), _col_bottom(ms)
            cols = np.nonzero(t_ >= 0)[0]
            if not len(cols):
                continue
            jr = max(0, min(W_ - 1, int((H[0] - ox) / sc)))
            if not (cols.min() <= jr <= cols.max()):
                continue
            inner = (t_[cols] > utop[cols] + 0.05 * h / sc) & (
                b_[cols] < ubot[cols] - 0.05 * h / sc
            )
            if inner.mean() < 0.8:
                continue
            hi_, hj_ = ij(H)
            rr = int(0.3 * h / sc) + 1
            near_h = ms[
                max(0, hi_ - rr) : hi_ + rr + 1, max(0, hj_ - rr) : hj_ + rr + 1
            ]
            if not near_h.any():
                continue  # the hips are not in it
            wall = [
                Mc[b_[c_] + 1 : min(H_, b_[c_] + 1 + int(0.3 * h / sc)), c_].mean()
                for c_ in cols
                if b_[c_] + 1 < H_
            ]
            if not wall or float(np.mean(wall)) < 0.6:
                continue  # nothing solid under it: a seat top, not the inside of a container
            ttop = oy + t_[jr] * sc if t_[jr] >= 0 else None
            tbot = oy + b_[jr] * sc if b_[jr] >= 0 else None
            if ttop is None or ttop > H[1] + 0.3 * h or tbot < H[1] - 0.3 * h:
                continue
            rim[cols] = np.maximum(
                rim[cols], b_[cols]
            )  # the opening's near (lower) edge
            opens.append(s_.get("id") or tag(s_))
        # in or on: hips in an opening (cockpit, water, interior), or both ankles inside the thing, or hips well under its rim
        jr_ = max(0, min(W_ - 1, int((H[0] - ox) / sc)))
        ankles_in = (
            all(inside(J[k_]) for k_ in ("near_ankle", "far_ankle"))
            and utop[jr_] >= 0
            and (oy + utop[jr_] * sc) < H[1] - 0.05 * h
        )
        deep = inside(H) and utop[jr_] >= 0 and (oy + utop[jr_] * sc) < H[1] - 0.25 * h
        why = (
            "opening"
            if opens
            else (
                "ankles inside"
                if ankles_in
                else ("hips under the rim" if deep else None)
            )
        )
        if why is None and force:
            why = "declared"
        if why is None:
            tried.append(f"{tid}: on it (sits or stands on top, legs outside)")
            continue
        cols = np.nonzero(utop >= 0)[0]
        cols = cols[(cols >= cj0) & (cols < cj1)]
        if len(cols) < 2:
            tried.append(f"{tid}: rim outside the person")
            continue
        xa, xb = int(cols.min()), int(cols.max())
        allc = np.arange(xa, xb + 1)
        good = utop[allc] >= 0
        rv = np.interp(allc, allc[good], rim[allc][good])
        rv = ndi.median_filter(rv, size=5, mode="nearest")
        ry = oy + rv * sc
        floor_y = J["neck"][1] + 0.6 * h
        clamped = bool((ry < floor_y).any())
        ry = np.maximum(ry, floor_y)
        rx = ox + (allc + 0.5) * sc
        # clip path in root user units: everything except what lies below the rim over the thing's columns
        big = 1e5
        pts = [
            (-big, -big),
            (big, -big),
            (big, big),
            (rx[-1] + 0.5 * sc, big),
            (rx[-1] + 0.5 * sc, ry[-1]),
        ]
        pts += [(x_, y_) for x_, y_ in zip(rx[::-1], ry[::-1])]
        pts += [(rx[0] - 0.5 * sc, ry[0]), (rx[0] - 0.5 * sc, big), (-big, big)]
        defs = next((c for c in doc.root if tag(c) == "defs"), None)
        if defs is None:
            defs = el("defs")
            doc.root.insert(0, defs)
        cid = f"b21c-{pp.name}"
        cp = el("clipPath", id=cid, clipPathUnits="userSpaceOnUse")
        cp.append(
            el("polygon", points=" ".join(f"{x_:.1f},{y_:.1f}" for x_, y_ in pts))
        )
        defs.append(cp)
        # move the person over the thing
        common, kp, ko = lca_children(doc, g, T)
        moved = False
        if (
            kp is not None
            and ko is not None
            and kp is not ko
            and list(common).index(kp) < list(common).index(ko)
        ):
            common.remove(kp)
            common.insert(list(common).index(ko) + 1, kp)
            moved = True
        doc.parent = {id(c): p for p in doc.root.iter() for c in p}
        Cg = doc.ctm(g)
        kids_ = [c for c in list(g) if tag(c) != "desc"]
        outer = (
            el("g", transform=mstr(np.linalg.inv(Cg)))
            if not np.allclose(Cg, np.eye(3))
            else el("g")
        )
        clipped = el("g", clip_path=f"url(#{cid})")
        inner2 = (
            el("g", transform=mstr(Cg)) if not np.allclose(Cg, np.eye(3)) else el("g")
        )
        for c in kids_:
            g.remove(c)
            inner2.append(c)
        clipped.append(inner2)
        outer.append(clipped)
        # the rim drawn again over the body, in the thing's own ink, over the person's columns
        fills: Counter[str] = Counter()
        for s_, _ in masks:
            f_ = hexcol(s_.get("fill"), None)
            if f_:
                fills[f_] += 1
        ink = style.line or (
            shade(fills.most_common(1)[0][0], -0.45) if fills else "#333333"
        )
        sel = (rx >= min(xs_body) - 0.3 * h) & (rx <= max(xs_body) + 0.3 * h)
        if sel.sum() >= 2:
            d = "M" + " L".join(
                f"{x_:.1f},{y_:.1f}" for x_, y_ in zip(rx[sel], ry[sel])
            )
            outer.append(
                el(
                    "path",
                    d=d,
                    fill="none",
                    stroke=ink,
                    stroke_width=f"{1.2 * style.lw(h):.2f}",
                    stroke_linecap="round",
                    stroke_linejoin="round",
                )
            )
        g.append(outer)
        doc.parent = {id(c): p for p in doc.root.iter() for c in p}
        return {
            "thing": tid,
            "by": "declared" if force else ("hips" if hips_in else "knees"),
            "why": why,
            "cover": round(cov, 2),
            "rim_shapes": len(masks),
            "openings": opens,
            "clamped_at_chest": clamped,
            "moved_over": moved,
            "rim_y_at_hips": round(float(np.interp(H[0], rx, ry)), 1),
            "tried": tried,
        }
    return {"thing": None, "tried": tried} if tried else None


def hair_behind(
    doc: Doc,
    head: ET.Element,
    excl: list[ET.Element],
    hb: list[float],
    eff: float,
    skin: str,
) -> list[ET.Element]:
    """Long hair of the painter's head: -> list of shapes to put behind the body. The chin is the bottom of the face skin shape (the
    largest skin-coloured shape of the head), else the head box estimate. A shape is long hair when it is not skin-coloured, reaches
    below the chin, and either starts in the upper part of the head, or has the colour of such a hair shape and is wider than the face
    (hair framing the face; a beard is narrower than the face and starts lower)."""

    def col(n: ET.Element) -> str:
        return paint_col(doc, n)  # round 22: gradients resolved

    def dist(a_: str, b_: str) -> int:
        a2, b2 = hexcol(a_, None), hexcol(b_, None)
        if a2 and b2:
            return sum(
                abs(int(a2[i : i + 2], 16) - int(b2[i : i + 2], 16)) for i in (1, 3, 5)
            )
        return 0 if a_ == b_ else 999

    items = []
    for s_ in doc.shapes_under(head, exclude=excl):
        if tag(s_) in ("text", "use"):
            continue
        m, sc, ox, oy = sil(doc, [s_], 512)
        b = mask_box(m, sc, ox, oy)
        if b is not None:
            items.append((s_, b, col(s_)))
    skins = [(s_, b) for s_, b, c in items if skin and dist(c, skin) < 90]
    if skins:
        fs, fb = max(skins, key=lambda x: (x[1][2] - x[1][0]) * (x[1][3] - x[1][1]))
        chin, fw, ftop = fb[3], fb[2] - fb[0], fb[1]
    else:
        fs, chin, fw, ftop = None, hb[1] + eff, hb[2] - hb[0], hb[1]
    fh = max(chin - ftop, 1e-3)
    upper = [
        (s_, b, c)
        for s_, b, c in items
        if s_ is not fs and b[1] < ftop + 0.3 * fh and dist(c, skin) >= 60
    ]
    hair_cols = [c for _, _, c in upper]
    out = []
    for s_, b, c in items:
        if s_ is fs or (skin and dist(c, skin) < 60) or b[3] <= chin + 0.1 * fh:
            continue
        starts_up = b[1] < ftop + 0.3 * fh and (b[2] - b[0]) >= 0.4 * fw
        framing = (
            any(dist(c, hc_) < 40 for hc_ in hair_cols) and (b[2] - b[0]) >= 1.05 * fw
        )
        if starts_up or framing:
            out.append(s_)
    return out


LYING = ("lying-on", "lying-in")

COVER_WORDS = re.compile(
    r"blanket|duvet|quilt|cover|sheet|plaid|couette|couverture|drap|sleeping|comforter|throw",
    re.I,
)

PILLOW_WORDS = re.compile(r"pillow|cushion|oreiller|coussin|headrest|head", re.I)


def paint_col(doc: Doc, n: ET.Element) -> str:
    """Fill colour of a shape, a gradient's first stop colour when the fill is url(#id) (round 22: painter skin and hair often use gradients)."""
    f_ = (n.get("fill") or "").strip()
    m_ = re.search(r"(?:^|;)\s*fill\s*:\s*([^;]+)", n.get("style") or "")
    f_ = m_.group(1).strip() if m_ else f_
    m2 = re.match(r"url\(\s*#([^)\s]+)\s*\)", f_)
    if m2:
        gnode = doc.by_id(m2.group(1))
        for _ in range(3):
            if gnode is None:
                break
            stops = [x for x in gnode if tag(x) == "stop"]
            if stops:
                st = stops[len(stops) // 2]
                c_ = st.get("stop-color") or ""
                m3 = re.search(r"stop-color\s*:\s*([^;]+)", st.get("style") or "")
                return (m3.group(1).strip() if m3 else c_).lower()
            href = (
                gnode.get("href")
                or gnode.get("{http://www.w3.org/1999/xlink}href")
                or ""
            )
            gnode = doc.by_id(href.lstrip("#")) if href.startswith("#") else None
    return f_.lower()


def lie(doc: Doc, pp: Person, T: ET.Element, rel: str) -> dict[str, Any]:
    """Lay the person along the support T (lying-on / lying-in): rewrites the painter's joints (pp.P) along the support's surface, head at the
    head end; returns a record. Called before build(); h may shrink so that the body fits the support."""
    P, h = pp.P, pp.h0
    pr = PROP[pp.pose["build"]]
    shapes = thing_shapes(doc, T)
    rec: dict[str, Any] = {"thing": T.get("id"), "rel": rel}
    if not shapes:
        rec["skipped"] = "empty thing"
        return rec
    M, sc, ox, oy = sil(doc, shapes)
    vw, vh = doc.vb[2], doc.vb[3]
    big = M.sum() * sc * sc > 0.45 * vw * vh
    k = max(1, int(round(0.08 * h / sc)))
    Mf = ndi.binary_fill_holes(
        ndi.binary_closing(M, structure=np.ones((k, k), bool)) | M
    )
    H_, W_ = Mf.shape
    head = P.get("head", P["neck"] - np.array([0, 0.7 * h]))
    ank = (P["near_ankle"] + P["far_ankle"]) / 2
    hint = float(P["mid_hip"][1])
    # the support's extent in a band around the painter's hips
    r0, r1 = (
        max(0, int((hint - 1.6 * h - oy) / sc)),
        min(H_, int((hint + 1.2 * h - oy) / sc) + 1),
    )
    band = Mf[r0:r1].any(axis=0)
    if not band.any():
        band = Mf.any(axis=0)
    if not band.any():
        rec["skipped"] = "no silhouette"
        return rec
    # the run of columns nearest to the painter's hips
    lab, nl = ndi.label(band)
    jh = max(0, min(W_ - 1, int((P["mid_hip"][0] - ox) / sc)))
    if lab[jh]:
        run = lab == lab[jh]
    else:
        js = np.nonzero(band)[0]
        run = lab == lab[js[np.argmin(abs(js - jh))]]
    cols = np.nonzero(run)[0]
    xa, xb = ox + cols.min() * sc, ox + (cols.max() + 1) * sc
    # head side: the painter's head against its feet, else a pillow-like shape, else the left
    dx = float(head[0] - ank[0])
    if abs(dx) > 0.5 * h:
        a = 1.0 if dx < 0 else -1.0
        rec["head_side"] = "painter"
    else:
        pil = [
            s
            for s in shapes
            if PILLOW_WORDS.search(s.get("id") or "")
            or any(
                PILLOW_WORDS.search(x.get("id") or "")
                for x in doc.ancestors(s)
                if x is not T and T in doc.ancestors(x)
            )
        ]
        if pil:
            pb = mask_box(sil(doc, pil)[0], sc, ox, oy)
            a = 1.0 if pb and (pb[0] + pb[2]) / 2 < (xa + xb) / 2 else -1.0
            rec["head_side"] = "pillow"
        else:
            a = 1.0
            rec["head_side"] = "default left"
    rel_len = (
        0.5 + pr["neck"] + pr["torso"] + pr["thigh"] + pr["shin"]
    )  # head centre to ankle, in heads
    full = rel_len + 0.5 + 0.25
    if not big and (xb - xa) < 0.95 * full * h:
        h2 = max(0.7 * h, (xb - xa) * 0.95 / full)
        rec["h_fit"] = [round(h, 1), round(h2, 1)]
        h = h2
    pp.h0 = h
    half = full * h / 2
    cp = float(head[0] + ank[0]) / 2
    if not big and (xb - xa) >= full * h:
        c = min(max(cp, xa + half), xb - half)
    elif not big:
        c = (xa + xb) / 2
    else:
        c = cp
    xhead = c - a * half + a * 0.5 * h
    xs = {"head": xhead, "neck": xhead + a * (0.5 + pr["neck"]) * h}
    xs["hip"] = xs["neck"] + a * pr["torso"] * h
    xs["knee"] = xs["hip"] + a * pr["thigh"] * 0.95 * h
    xs["ankle"] = xs["knee"] + a * pr["shin"] * 0.97 * h

    def surf(x: float, up: float) -> float | None:
        j = max(0, min(W_ - 1, int((x - ox) / sc)))
        i0 = max(0, int((hint - up * h - oy) / sc))
        col = Mf[i0:, j]
        if not col.any():
            return None
        return oy + (i0 + int(col.argmax())) * sc

    s: dict[str, Any] = {}
    s["hip"] = surf(xs["hip"], 0.6)
    if s["hip"] is None:
        s["hip"] = hint + 0.3 * h
    for kx, up in (("neck", 1.6), ("head", 2.0), ("knee", 0.6), ("ankle", 0.6)):
        v = surf(xs[kx], up)
        s[kx] = s["hip"] if v is None else v
    t40 = math.tan(math.radians(40))
    s["knee"] = min(max(s["knee"], s["hip"] - 0.4 * h), s["hip"] + 0.4 * h)
    s["ankle"] = min(max(s["ankle"], s["knee"] - 0.4 * h), s["knee"] + 0.4 * h)
    s["neck"] = min(
        max(s["neck"], s["hip"] - t40 * pr["torso"] * h), s["hip"] + 0.2 * h
    )
    s["head"] = min(
        max(s["head"], s["neck"] - t40 * (0.5 + pr["neck"]) * h), s["neck"] + 0.1 * h
    )
    hipP = np.array([xs["hip"], s["hip"] - 0.32 * h])
    neckP = np.array([xs["neck"], s["neck"] - 0.38 * h])
    kneeP = np.array([xs["knee"], min(s["knee"], s["hip"]) - 0.36 * h])
    ankP = np.array([xs["ankle"], s["ankle"] - 0.14 * h])
    td = unit(hipP - neckP)
    n1 = np.array([-td[1], td[0]])
    down = n1 if n1[1] > 0 else -n1
    elb = neckP + td * pr["upper"] * h * 0.92 + down * 0.12 * h
    wri = elb + td * pr["fore"] * h * 0.75 - down * 0.22 * h
    newP = {
        "head": np.array([xs["head"], s["head"] - 0.5 * h]),
        "neck": neckP,
        "near_shoulder": neckP.copy(),
        "far_shoulder": neckP.copy(),
        "near_elbow": elb,
        "far_elbow": elb - td * 0.04 * h,
        "near_wrist": wri,
        "far_wrist": wri - td * 0.06 * h,
        "near_hip": hipP,
        "far_hip": hipP.copy(),
        "near_knee": kneeP,
        "far_knee": kneeP - td * 0.05 * h + down * 0.03 * h,
        "near_ankle": ankP,
        "far_ankle": ankP - td * 0.08 * h,
    }
    for k_, v_ in newP.items():
        P[k_] = v_
    P["mid_hip"] = hipP.copy()
    pp.pose["facing"] = (
        "profile-right" if a < 0 else "profile-left"
    )  # chest up; the body lies with its head at the head end
    pp.pose["contacts"] = [
        c
        for c in pp.pose["contacts"]
        if c["joint"] != "hips" and c["kind"] not in ("step", "sit", "in")
    ]
    rec.update(
        {
            "head_end": "left" if a > 0 else "right",
            "support": [round(xa, 1), round(xb, 1)],
            "big_support": bool(big),
            "surface": {k_: round(float(v_), 1) for k_, v_ in s.items()},
        }
    )
    pp.lying = {"a": a, "rel": rel, "T": T, "surf": s}
    return rec


def cover_shape(
    doc: Doc, pp: Person, style: Style
) -> tuple[ET.Element, dict[str, str]]:
    """lying-in: a cover from mid-chest to past the feet, over the body, in the colour of the picture's own cover if there is one."""
    J, h = pp.J, pp.h
    pr = PROP[pp.pose["build"]]
    assert pp.lying is not None
    T = pp.lying["T"]
    fills: Counter[str] = Counter()
    for node in doc.root.iter():
        if node.get("id") and COVER_WORDS.search(str(node.get("id"))):
            for s_ in [node] if tag(node) in SHAPE_TAGS else doc.shapes_under(node):
                f_ = hexcol(s_.get("fill"), None)
                if f_:
                    fills[f_] += 1
    if fills:
        col = fills.most_common(1)[0][0]
        src = "picture's cover"
    else:
        fc: Counter[str] = Counter()
        for s_ in thing_shapes(doc, T):
            f_ = hexcol(s_.get("fill"), None)
            if f_ and lum([float(int(f_[i : i + 2], 16)) for i in (1, 3, 5)]) > 110:
                fc[f_] += 1
        col = shade(fc.most_common(1)[0][0], 0.15) if fc else "#a9c4e0"
        src = "support colour" if fc else "default"
    N, H = J["neck"], J["mid_hip"]
    u = unit(H - N)
    n = np.array([u[1], -u[0]])
    if n[1] > 0:
        n = -n
    kn = (J["near_knee"] + J["far_knee"]) / 2
    an = (J["near_ankle"] + J["far_ankle"]) / 2
    chest = N + u * 0.42 * pr["torso"] * h
    feet = an + unit(an - kn) * 0.4 * h
    top = [
        chest + n * 0.42 * h,
        H + n * 0.44 * h,
        kn + n * 0.34 * h,
        an + n * 0.30 * h,
        feet + n * 0.24 * h,
    ]
    bot = [
        feet - n * 0.42 * h,
        an - n * 0.46 * h,
        kn - n * 0.5 * h,
        H - n * 0.52 * h,
        chest - n * 0.5 * h,
    ]
    pts = top + bot + [chest - u * 0.04 * h]
    g_ = el("g", id=pp.name + "-cover")
    g_.append(
        el(
            "path",
            d=smooth(pts),
            fill=col,
            stroke=style.line or shade(col, -0.45),
            stroke_width=f"{style.lw(h):.2f}",
            stroke_linejoin="round",
        )
    )
    ink = shade(col, -0.25)
    turn = chest + u * 0.18 * h
    g_.append(
        el(
            "path",
            d=f"M{fmt(turn + n * 0.43 * h)} Q{fmt(turn + u * 0.05 * h)} {fmt(turn - n * 0.48 * h)}",
            fill="none",
            stroke=ink,
            stroke_width=f"{0.8 * style.lw(h):.2f}",
            stroke_linecap="round",
        )
    )
    for t_ in (0.45, 0.75):
        q = H + (kn - H) * t_ if t_ < 0.6 else kn + (an - kn) * (t_ - 0.4)
        g_.append(
            el(
                "path",
                d=f"M{fmt(q + n * 0.2 * h)} l{fmt(-n * 0.18 * h + u * 0.08 * h)}",
                fill="none",
                stroke=ink,
                stroke_width=f"{0.6 * style.lw(h):.2f}",
                stroke_linecap="round",
            )
        )
    return g_, {"colour_from": src}


def long_axis(doc: Doc, T: ET.Element) -> tuple[Vec, Vec, float, float] | None:
    """Principal axis of a thing's silhouette: (centre, unit direction, half length, half width) or None."""
    shapes = thing_shapes(doc, T)
    if not shapes:
        return None
    M, sc, ox, oy = sil(doc, shapes)
    ys, xs = np.nonzero(M)
    if len(xs) < 20:
        return None
    pts = np.stack([ox + (xs + 0.5) * sc, oy + (ys + 0.5) * sc], 1)
    c = pts.mean(0)
    cov = np.cov((pts - c).T)
    w, v = np.linalg.eigh(cov)
    d = v[:, 1]
    pr_ = (pts - c) @ d
    pw = (pts - c) @ v[:, 0]
    return (
        c,
        d,
        max(float(-pr_.min()), float(pr_.max())),
        float(np.percentile(abs(pw), 75)),
    )  # 75th: blades or heads at the ends do not count as width


def seg_point_dist(a: Vec, b: Vec, p: Vec) -> float:
    ab = b - a
    t = max(0.0, min(1.0, float(np.dot(p - a, ab) / max(float(np.dot(ab, ab)), 1e-9))))
    return float(np.hypot(*(a + ab * t - p)))


def arm_solve(
    pp: Person, s: str, target: Vec, sign: float, hand: bool = True
) -> tuple[Vec, Vec, Vec, bool]:
    """Two-bone solution for one arm with a given bend sign, without touching pp: -> (elbow, wrist, grip, stretch ok)."""
    J = pp.J
    l1, l2 = pp.L["upper"], pp.L["fore"]
    extra = 0.12 * pp.h if hand else 0.0
    rec: dict[str, Any] = {"contacts_missed": [], "stretched": []}
    mid, end, k = two_bone(J[f"{s}_shoulder"], target, l1, l2 + extra, sign, rec, "x")
    wr = mid + unit(end - mid) * (float(np.hypot(*(end - mid))) - extra * k)
    return mid, wr, end, not rec["contacts_missed"]


def arm_penalty(
    pp: Person, sh: Vec, el_: Vec, wr: Vec, other: tuple[Vec, Vec, Vec] | None = None
) -> float:
    """Arm through the head (worst), through the torso, elbow raised above the shoulder."""
    hc = head_centre(pp)
    chin = pp.J["neck"] - unit(pp.J["mid_hip"] - pp.J["neck"]) * 0.12 * pp.h
    pen = 0.0
    for a_, b_ in ((sh, el_), (el_, wr)):
        for cc, rh in ((hc, 0.5 * pp.h), (chin, 0.22 * pp.h)):
            d = seg_point_dist(a_, b_, cc)
            if d < rh:
                pen += 200 * (1 + (rh - d) / rh)
    N, H = pp.J["neck"], pp.J["mid_hip"]
    N2 = N + unit(H - N) * 0.25 * pp.h
    if seg_cross(sh + unit(el_ - sh) * 0.15 * pp.h, el_, N2, H):
        pen += 30
    if seg_cross(el_, wr, N2, H) and pp.pose["facing"].startswith("profile"):
        pen += 10
    pen += 8 * max(0.0, float(sh[1] - el_[1]) / pp.h - 0.2) if wr[1] > sh[1] else 0.0
    if other is not None:
        osh, oel, owr = other
        if any(
            seg_cross(a_, b_, c_, d_)
            for a_, b_ in ((sh, el_), (el_, wr))
            for c_, d_ in ((osh, oel), (oel, owr))
        ):
            pen += 100
    return pen


def set_arm(pp: Person, s: str, el_: Vec, wr: Vec, grip: Vec, label: str) -> None:
    pp.J[f"{s}_elbow"], pp.J[f"{s}_wrist"] = el_, wr
    pp.hand_at[s] = grip
    pp.rec.setdefault("arms_fixed", []).append(label)


def fix_arms(doc: Doc, pp: Person, anchors: Anchors) -> None:
    """Round 22: (1) both hands on one long thing: grips re-chosen along the thing's own axis and elbows chosen so that the arms do not
    cross in an X and no arm passes through the head or the torso; (2) any other arm through the head: the other elbow bend is tried."""
    h = pp.h
    holds = {}
    for c in pp.pose["contacts"]:
        if (
            c["kind"] in ("hold", "touch")
            and c["joint"].endswith("wrist")
            and ":" not in c["anchor"]
        ):
            a = find_anchor(anchors, c["anchor"])
            if a and anchors[a]["owner"] is not None:
                holds[c["joint"].split("_")[0]] = (anchors[a]["owner"], anchors[a]["p"])
    done = set()
    if "near" in holds and "far" in holds:
        Tn, Tf = holds["near"][0], holds["far"][0]
        T = (
            Tn
            if (Tn is Tf or Tf in doc.ancestors(Tn))
            else (Tf if Tn in doc.ancestors(Tf) else None)
        )
        ax = long_axis(doc, T) if T is not None else None
        before = arm_penalty(
            pp, pp.J["near_shoulder"], pp.J["near_elbow"], pp.J["near_wrist"]
        ) + arm_penalty(
            pp,
            pp.J["far_shoulder"],
            pp.J["far_elbow"],
            pp.J["far_wrist"],
            (pp.J["near_shoulder"], pp.J["near_elbow"], pp.J["near_wrist"]),
        )
        if ax is not None and not (
            ax[2] >= 0.8 * h and ax[2] >= 2.5 * max(ax[3], 1e-3)
        ):
            pp.rec["two_hands_skipped"] = "thing not long"
        elif ax is not None and before < 5:
            pp.rec["two_hands"] = {
                "thing": T.get("id") if T is not None else None,
                "penalty_before": round(before, 1),
                "kept": "painter's grips are fine",
            }
            done |= {"near", "far"}
        if (
            ax is not None
            and ax[2] >= 0.8 * h
            and ax[2] >= 2.5 * max(ax[3], 1e-3)
            and before >= 5
        ):
            c, d, hl, hw_ = ax
            tn = float(np.dot(holds["near"][1] - c, d))
            tf = float(np.dot(holds["far"][1] - c, d))
            reach = (pp.L["upper"] + pp.L["fore"] + 0.12 * h) * 1.02
            grid = np.arange(-hl * 0.95, hl * 0.95 + 1e-6, max(0.12 * h, hl / 40))
            best: Any = None
            sh_n, sh_f = pp.J["near_shoulder"], pp.J["far_shoulder"]
            for ta in grid:
                pa = c + d * ta
                if float(np.hypot(*(pa - sh_n))) > reach:
                    continue
                for tb in grid:
                    if abs(ta - tb) < 0.45 * h or abs(ta - tb) > 2.0 * h:
                        continue
                    pb = c + d * tb
                    if float(np.hypot(*(pb - sh_f))) > reach:
                        continue
                    for sn in (1.0, -1.0):
                        en, wn, gn, okn = arm_solve(pp, "near", pa, sn)
                        if not okn:
                            continue
                        pn = arm_penalty(pp, sh_n, en, wn)
                        for sf in (1.0, -1.0):
                            ef, wf, gf, okf = arm_solve(pp, "far", pb, sf)
                            if not okf:
                                continue
                            pen = pn + arm_penalty(pp, sh_f, ef, wf, (sh_n, en, wn))
                            pen += 4 * (abs(ta - tn) + abs(tb - tf)) / h
                            if best is None or pen < best[0]:
                                best = (pen, (en, wn, gn), (ef, wf, gf), ta, tb)
            if best is not None and best[0] < before:
                el_near, wr_near, grip_near = best[1]
                set_arm(pp, "near", el_near, wr_near, grip_near, "near:two-hands")
                el_far, wr_far, grip_far = best[2]
                set_arm(pp, "far", el_far, wr_far, grip_far, "far:two-hands")
                pp.rec["two_hands"] = {
                    "thing": T.get("id") if T is not None else None,
                    "penalty_before": round(before, 1),
                    "penalty_after": round(best[0], 1),
                    "grips_moved_heads": [
                        round((best[3] - tn) / h, 2),
                        round((best[4] - tf) / h, 2),
                    ],
                }
                done |= {"near", "far"}
    for s in ("near", "far"):
        if s in done:
            continue
        sh, el_, wr = pp.J[f"{s}_shoulder"], pp.J[f"{s}_elbow"], pp.J[f"{s}_wrist"]
        if arm_penalty(pp, sh, el_, wr) < 200:
            continue
        target = pp.hand_at.get(s)
        best = None
        for sg in (1.0, -1.0):
            e2, w2, g2, ok = arm_solve(
                pp, s, target if target is not None else wr, sg, hand=target is not None
            )
            if ok:
                pen = arm_penalty(pp, sh, e2, w2)
                if best is None or pen < best[0]:
                    best = (pen, e2, w2, g2)
        if best is not None and best[0] < arm_penalty(pp, sh, el_, wr):
            if target is not None:
                set_arm(pp, s, best[1], best[2], best[3], f"{s}:off-head")
            else:
                pp.J[f"{s}_elbow"], pp.J[f"{s}_wrist"] = best[1], best[2]
                pp.rec.setdefault("arms_fixed", []).append(f"{s}:off-head")
        elif target is None:
            # a free arm through the head: lowered beside the body
            d0 = unit(pp.J["mid_hip"] - pp.J["neck"])
            pp.J[f"{s}_elbow"] = sh + d0 * pp.L["upper"]
            pp.J[f"{s}_wrist"] = pp.J[f"{s}_elbow"] + rot(d0, 0.15) * pp.L["fore"]
            pp.rec.setdefault("arms_fixed", []).append(f"{s}:free arm lowered")


def hair_off_face(
    doc: Doc,
    head: ET.Element,
    excl: list[ET.Element],
    skin: str,
    face_g: ET.Element | None,
    name: str,
) -> tuple[int, list[float] | None]:
    """Round 22: the painter's non-skin head shapes drawn over the face (eyes, nose, mouth band) are clipped out of that band; beards
    (starting low), small marks and the face group itself are left alone. -> number of shapes clipped."""

    def dist(a_: str, b_: str) -> int:
        a2, b2 = hexcol(a_, None), hexcol(b_, None)
        if a2 and b2:
            return sum(
                abs(int(a2[i : i + 2], 16) - int(b2[i : i + 2], 16)) for i in (1, 3, 5)
            )
        return 0 if a_ == b_ else 999

    def col(n: ET.Element) -> str:
        return paint_col(doc, n)

    order = {id(n): k for k, n in enumerate(doc.root.iter())}
    shapes = [
        s_
        for s_ in doc.shapes_under(head, exclude=[x for x in excl if x is not face_g])
        if tag(s_) not in ("text", "use")
    ]
    items = []
    for s_ in shapes:
        m, sc, ox, oy = sil(doc, [s_], 512)
        b = mask_box(m, sc, ox, oy)
        if b is not None:
            items.append((s_, b, col(s_), m))
    skins = [(s_, b, m) for s_, b, c, m in items if skin and dist(c, skin) < 90]
    if not skins:
        return 0, None
    fs, fb, fm = max(skins, key=lambda x: int(x[2].sum()))
    fw, fh = fb[2] - fb[0], fb[3] - fb[1]
    cx, cy = (fb[0] + fb[2]) / 2, fb[1] + 0.64 * fh
    rx, ry = 0.40 * fw, 0.31 * fh
    sc_ = doc.vb[2] / 512
    yy, xx = np.mgrid[0 : fm.shape[0], 0 : fm.shape[1]]
    ell = (((doc.vb[0] + (xx + 0.5) * sc_) - cx) / rx) ** 2 + (
        ((doc.vb[1] + (yy + 0.5) * sc_) - cy) / ry
    ) ** 2 <= 1
    clipped = 0
    for s_, b, c, m in items:
        if s_ is fs or (
            face_g is not None and (s_ is face_g or face_g in doc.ancestors(s_))
        ):
            continue
        if dist(c, skin) < 60 or order.get(id(s_), 0) < order.get(id(fs), 0):
            continue
        if b[1] > fb[1] + 0.5 * fh or (
            (b[2] - b[0]) < 0.3 * fw and (b[3] - b[1]) < 0.35 * fh
        ):
            continue  # a beard or moustache starts low; eyebrows and small marks are small
        over = float((m & ell).sum()) / max(1.0, float(ell.sum()))
        if over < 0.04:
            continue
        par = doc.parent.get(id(s_))
        if par is None:
            continue
        T = doc.ctm(par) if par is not doc.root else np.eye(3)
        Ti = np.linalg.inv(T)
        ring = [
            Ti @ np.array([cx + rx * math.cos(t_), cy + ry * math.sin(t_), 1.0])
            for t_ in np.linspace(0, 2 * math.pi, 40, endpoint=False)
        ]
        big = [
            Ti @ np.array([x_, y_, 1.0])
            for x_, y_ in ((-1e4, -1e4), (1e4, -1e4), (1e4, 1e4), (-1e4, 1e4))
        ]
        d = (
            "M"
            + " L".join(f"{q[0]:.1f},{q[1]:.1f}" for q in big)
            + " Z M"
            + " L".join(f"{q[0]:.1f},{q[1]:.1f}" for q in ring)
            + " Z"
        )
        defs = next((x for x in doc.root if tag(x) == "defs"), None)
        if defs is None:
            defs = el("defs")
            doc.root.insert(0, defs)
        cid = f"b22h-{name}-{clipped}"
        cp = el("clipPath", id=cid, clipPathUnits="userSpaceOnUse")
        cp.append(el("path", d=d, clip_rule="evenodd"))
        defs.append(cp)
        w_ = el("g", clip_path=f"url(#{cid})")
        k_ = list(par).index(s_)
        par.remove(s_)
        w_.append(s_)
        par.insert(k_, w_)
        doc.parent = {id(c2): p2 for p2 in doc.root.iter() for c2 in p2}
        clipped += 1
    return clipped, [round(v, 1) for v in (cx, cy, rx, ry)]


def put_behind(
    doc: Doc, pp: Person, T: ET.Element, style: Style, anchors: Anchors
) -> dict[str, Any]:
    """Declared "behind": the person under the thing, unless the thing is background-sized or would hide the head: then rim clip."""
    shapes = thing_shapes(doc, T)
    if not shapes:
        return {"how": "empty thing"}
    M, sc, ox, oy = sil(doc, shapes)
    Mf = ndi.binary_fill_holes(M)
    hc = head_centre(pp)
    i, j = int((hc[1] - oy) / sc), int((hc[0] - ox) / sc)
    covers_head = 0 <= i < Mf.shape[0] and 0 <= j < Mf.shape[1] and bool(Mf[i, j])
    bigt = M.sum() * sc * sc > 0.45 * doc.vb[2] * doc.vb[3]
    if not covers_head and not bigt:
        r = reorder(doc, pp.group, T, over=False)
        if r:
            return {"how": "drawn under the thing"}
    c_ = contain(doc, pp, [T], style, anchors, force=True)
    return {
        "how": "rim clip"
        + (
            " (thing hides the head)"
            if covers_head
            else (" (background-sized)" if bigt else " (nested groups)")
        ),
        "contain": c_,
    }


def compose(
    svg: str, ck: dict[str, Any] | None = None, living: dict[str, bool] | None = None
) -> tuple[str, dict[str, Any]]:
    doc = Doc(svg)
    ids = {t["id"] for t in (ck or {}).get("things", [])}
    report: dict[str, Any] = {
        "persons": [],
        "invalid": [],
        "human_heads_without_pose": [],
    }
    persons = []
    pose_groups = []
    for g in list(doc.root.iter()):
        if tag(g) != "g" or not g.get("id"):
            continue
        desc = next(
            (
                c
                for c in g
                if tag(c) == "desc"
                and (
                    (c.get("class") or "") == "pose"
                    or (c.text or "").lstrip().startswith("{")
                )
            ),
            None,
        )
        if desc is None or '"joints"' not in (desc.text or ""):
            continue
        pose_groups.append((g, desc))
    for g, desc in pose_groups:
        name = str(g.get("id"))
        head = named_part(doc, name, "head", g)
        pose, problems, fatal = parse_pose(desc.text or "", head)
        if fatal:
            report["invalid"].append(
                {"name": name, "reason": fatal, "problems": problems}
            )
            continue
        assert pose is not None
        persons.append(Person(name, g, pose, problems))
    posed = {pp.name for pp in persons} | {r["name"] for r in report["invalid"]}
    for base, head in heads_of(doc):
        if (
            (head.get("data-species") or "").strip().lower() == "human"
            and base not in posed
            and re.sub(r"-\d+$", "", base) not in posed
        ):
            report["human_heads_without_pose"].append(base)
    if not persons:
        return svg, report
    style = Style(doc)
    report["style"] = style.rec
    anchors = anchor_points(doc, ids)
    report["anchors"] = sorted(anchors)
    for pp in list(persons):
        h = pp.estimate_h()
        if not h:
            report["invalid"].append(
                {
                    "name": pp.name,
                    "reason": "all joints at one point",
                    "problems": pp.problems,
                }
            )
            persons.remove(pp)
            continue
        pp.lying = None
        lyr = [r for r in (pp.pose.get("relations") or []) if r["rel"] in LYING]
        if lyr:
            # 4.1: "lying-on" a support and "lying-in" its cover may both be declared: lie on the support, under the cover
            sup_ = next((r for r in lyr if r["rel"] == "lying-on"), lyr[0])
            lyr = [
                {
                    "thing": sup_["thing"],
                    "rel": "lying-in"
                    if any(r["rel"] == "lying-in" for r in lyr)
                    else sup_["rel"],
                }
            ]
            T = find_thing(doc, lyr[0]["thing"], [q.group for q in persons], pp.group)
            if T is None:
                pp.rec["lying"] = {"thing_missing": lyr[0]["thing"]}
            else:
                pp.h0 = h
                try:
                    pp.rec["lying"] = lie(doc, pp, T, lyr[0]["rel"])
                    h = pp.h0
                except Exception as error:  # noqa: BLE001
                    pp.lying = None
                    pp.rec["lying"] = {
                        "error": f"{type(error).__name__}: {error}"[:300]
                    }
        pp.build(h)
        if pp.lying:
            pp.rec["root"] = "lying"
        else:
            pp.place(anchors)
    if not persons:
        return svg, report
    # round 21: anchors on the face move with their thing (shoulder, or beside the head for a thing held overhead)
    anchors_off_face(doc, persons, anchors, [pp.group for pp in persons])
    # contacts on things (hands and feet)
    for pp in persons:
        for c in pp.pose["contacts"]:
            pp.rec["contacts"].append(f"{c['joint']}:{c['kind']}:{c['anchor']}")
            if c["joint"] == "hips":
                if find_anchor(anchors, c["anchor"]) is None and ":" not in c["anchor"]:
                    pp.rec["anchor_missing"].append(c["anchor"])
                continue
            if ":" in c["anchor"]:
                continue
            a = find_anchor(anchors, c["anchor"])
            if a is None:
                pp.rec["anchor_missing"].append(c["anchor"])
                continue
            pp.snap(
                c["joint"],
                anchors[a]["p"],
                c["joint"] + "@" + a,
                hand=c["kind"] in ("hold", "touch"),
            )
    # contacts between people: both hands meet half way
    byname = {pp.name: pp for pp in persons}
    done = set()
    for pp in persons:
        for c in pp.pose["contacts"]:
            if ":" not in c["anchor"]:
                continue
            other, oj = c["anchor"].split(":", 1)
            oj = oj.strip().lower().replace("hand", "wrist").replace("foot", "ankle")
            q = byname.get(other.strip())
            if q is None or oj not in q.J or q is pp:
                pp.rec["anchor_missing"].append(c["anchor"])
                continue
            key = tuple(sorted([(pp.name, c["joint"]), (q.name, oj)]))
            if key in done:
                continue
            done.add(key)
            m = (pp.J[c["joint"]] + q.J[oj]) / 2
            pp.snap(c["joint"], m, f"{c['joint']}@{q.name}", hand=True)
            if oj.endswith("wrist") or oj.endswith("ankle"):
                q.snap(oj, m, f"{oj}@{pp.name}", hand=True)
    # round 22: two hands on one long thing, arms off the head
    for pp in persons:
        try:
            fix_arms(doc, pp, anchors)
        except Exception as error:  # noqa: BLE001
            pp.rec["fix_arms_error"] = f"{type(error).__name__}: {error}"[:300]
    # round 21: arms crossing in an X
    for pp in persons:
        if "two_hands" not in pp.rec:
            uncross(pp)
    kinds = {t["id"]: t.get("kind") for t in (ck or {}).get("things", [])}
    posed_groups = [pp.group for pp in persons]
    # draw and insert
    for pp in persons:
        g = pp.group
        name = pp.name
        decl = []
        for r in pp.pose.get("relations") or []:
            T_ = find_thing(doc, r["thing"], posed_groups, g)
            if (
                T_ is not None
                and kinds.get(re.sub(r"-\d+$", "", T_.get("id") or "")) == "place"
            ):
                pp.rec.setdefault("relations_ignored", []).append(
                    f"{r['thing']}:{r['rel']}: place"
                )
                T_ = None
            decl.append((r, T_))
        pp.rec["relations_declared"] = pp.pose.get("relations") is not None
        pp.rec["relations"] = [
            f"{r['thing']}:{r['rel']}:{'found' if T is not None else 'missing'}"
            for r, T in decl
        ]
        declT = {id(T): r["rel"] for r, T in decl if T is not None}
        layers = draw_person(pp, style)
        head = named_part(doc, name, "head", g)
        hat = find_named(doc, name, "hat", g)
        eyew = find_named(doc, name, "eyewear", g)
        keep = [x for x in (head, hat, eyew) if x is not None]
        # the painter's own body shapes (if it drew one anyway) are removed; other things' groups inside the person group are kept
        removed = 0
        others = []

        def strip(node: ET.Element) -> None:
            nonlocal removed
            for ch in list(node):
                if ch in keep or tag(ch) == "desc":
                    continue
                if any(ch in doc.ancestors(k) for k in keep):
                    strip(ch)
                    continue
                cid = ch.get("id") or ""
                if (cid.startswith("anchor-")) or (
                    tag(ch) == "g" and cid and cid != name and cid in ids
                ):
                    others.append(ch)
                    continue
                if tag(ch) in CONTAINERS:
                    continue
                removed += (
                    len(doc.shapes_under(ch))
                    if tag(ch) == "g"
                    else (1 if tag(ch) in SHAPE_TAGS else 0)
                )
                node.remove(ch)

        strip(g)
        pp.rec["painter_body_shapes_removed"] = removed
        # head: scale and move so that the chin sits on the neck, head height = h
        hb = None
        if head is not None:
            hb = head_box(doc, head, [x for x in (hat, eyew) if x is not None])
        if hb is None:
            pp.rec["head"] = "missing: plain head drawn"
            head = el(
                "g",
                id=name + "-head",
                **{
                    "data-facing": pp.pose["facing"],
                    "data-expression": "neutral",
                    "data-gaze": "forward",
                    "data-species": "human",
                    "data-age": {"child": "child", "elderly": "old"}.get(
                        pp.pose["build"], "adult"
                    ),
                },
            )
            c = layers["chin"] - pp.u * 0.5 * pp.h
            head.append(
                el(
                    "circle",
                    cx=f"{c[0]:.1f}",
                    cy=f"{c[1]:.1f}",
                    r=f"{0.42 * pp.h:.1f}",
                    fill=pp.pose["skin"],
                )
            )
            face = el("g", id=name + "-face")
            for sgn in (-1, 1):
                face.append(
                    el(
                        "circle",
                        cx=f"{c[0] + sgn * 0.14 * pp.h:.1f}",
                        cy=f"{c[1] - 0.02 * pp.h:.1f}",
                        r=f"{0.03 * pp.h:.1f}",
                        fill="#222",
                    )
                )
            face.append(
                el(
                    "path",
                    d=f"M{c[0] - 0.08 * pp.h:.1f},{c[1] + 0.18 * pp.h:.1f} q{0.08 * pp.h:.1f},{0.05 * pp.h:.1f} {0.16 * pp.h:.1f},0",
                    fill="none",
                    stroke="#222",
                )
            )
            head.append(face)
            head_items = [head]
            M = np.eye(3)
        else:
            assert head is not None
            bw, bh = hb[2] - hb[0], hb[3] - hb[1]
            eff = min(bh, 1.2 * bw)
            s = max(0.35, min(1.8, pp.h / max(eff, 1e-3)))
            src = np.array([(hb[0] + hb[2]) / 2, hb[1] + eff])
            off = (
                pp.fwd
                * (
                    0.10
                    if pp.pose["facing"].startswith("profile")
                    else (0.05 if pp.pose["facing"].startswith("three") else 0.0)
                )
                * pp.h
            )
            dst = layers["chin"] + off + pp.u * 0.04 * pp.h
            M = np.array(
                [[s, 0, dst[0] - s * src[0]], [0, s, dst[1] - s * src[1]], [0, 0, 1]]
            )
            pp.rec["head_scale"] = round(s, 3)
            pp.rec["painter_heads_tall"] = None
            head_items = [x for x in (head, hat, eyew) if x is not None]
            # round 21: long hair (from the upper head to below the chin) goes behind the body
            face_g = named_part(doc, name, "face", head)
            hair = hair_behind(
                doc,
                head,
                [x for x in (hat, eyew, face_g) if x is not None],
                hb,
                eff,
                pp.pose["skin"],
            )
            hair_wraps = []
            for sh_ in hair:
                par_ = doc.parent.get(id(sh_))
                Tsh = (
                    np.linalg.inv(doc.ctm(g))
                    @ M
                    @ (
                        doc.ctm(par_)
                        if par_ is not None and par_ is not doc.root
                        else np.eye(3)
                    )
                )
                hair_wraps.append((sh_, par_, Tsh))
            for sh_, par_, Tsh in hair_wraps:
                if par_ is not None and sh_ in list(par_):
                    par_.remove(sh_)
                w_ = el("g", transform=mstr(Tsh))
                w_.append(sh_)
                g.insert(1, w_)
            if hair_wraps:
                pp.rec["hair_behind"] = len(hair_wraps)
                doc.parent = {id(c): p for p in doc.root.iter() for c in p}
            # round 22: hair drawn over the face band is clipped out of it
            try:
                nclip, ell_ = hair_off_face(
                    doc,
                    head,
                    [x for x in (hat, eyew, face_g) if x is not None],
                    pp.pose["skin"],
                    face_g,
                    name,
                )
                if nclip:
                    pp.rec["hair_off_face"] = {"shapes": nclip, "band": ell_}
            except Exception as error:  # noqa: BLE001
                pp.rec["hair_off_face_error"] = f"{type(error).__name__}: {error}"[:300]
        J = pp.J
        top = J["neck"] - pp.u * (PROP[pp.pose["build"]]["neck"] + 1.0) * pp.h
        low = (
            max(J["near_ankle"][1], J["far_ankle"][1])
            + PROP[pp.pose["build"]]["foot_h"] * pp.h
        )
        pp.rec["h"] = round(pp.h, 1)
        pp.rec["heads_tall_drawn"] = round((low - top[1]) / pp.h, 2)
        # painter's own proportion, for the record: painter skeleton height over the painter head height
        if hb is not None:
            ptop = hb[1]
            plow = max(pp.P["near_ankle"][1], pp.P["far_ankle"][1])
            pp.rec["painter_heads_tall"] = round(
                (plow - ptop) / max(hb[3] - hb[1], 1e-3), 2
            )
        # rebuild the person group: desc, body, near limbs, head (+hat, eyewear)
        body_g = el("g")
        if not layers["far"].items:
            pass
        far_el = (
            layers["far"].element(style, pp.h, darker=0.12)
            if layers["far"].items
            else None
        )
        far_arm_el = (
            layers["far_arm"].element(style, pp.h, darker=0.12)
            if layers["far_arm"].items
            else None
        )
        body_g.append(layers["body"].element(style, pp.h))
        near_el = layers["near"].element(style, pp.h)
        body_g.append(near_el)
        # head items moved into the person group, under a wrapper that maps them onto the neck
        heads_wrap = []
        for item in head_items:
            par = doc.parent.get(id(item))
            if par is not None and item in list(par):
                Cold = doc.ctm(par) if par is not doc.root else np.eye(3)
                par.remove(item)
            else:
                Cold = np.eye(3)
            Cp = doc.ctm(g)
            W = np.linalg.inv(Cp) @ M @ Cold
            w = el("g", transform=mstr(W)) if not np.allclose(W, np.eye(3)) else el("g")
            w.append(item)
            heads_wrap.append(w)
        inner = el("g")
        if far_arm_el is not None:
            inner.append(far_arm_el)
        for ch in list(body_g):
            inner.append(ch)
        near_parent = inner
        sit = [
            c
            for c in pp.pose["contacts"]
            if c["joint"] == "hips"
            and c["kind"] in ("sit", "in")
            and find_anchor(anchors, c["anchor"])
        ]
        seat_owner = (
            anchors[cast(str, find_anchor(anchors, sit[0]["anchor"]))]["owner"]
            if sit
            else None
        )
        if seat_owner is not None and (
            seat_owner is g
            or g in doc.ancestors(seat_owner)
            or seat_owner in doc.ancestors(g)
        ):
            seat_owner = None
        if far_el is not None and seat_owner is None:
            inner.insert(0, far_el)
        g.append(wrap_inverse(doc, g, inner))
        if pp.lying and pp.lying["rel"] == "lying-in":
            try:
                cg, crec = cover_shape(doc, pp, style)
                g.append(wrap_inverse(doc, g, cg))
                for s_, hl in layers["hands_over"].items():
                    g.append(wrap_inverse(doc, g, hl.element(style, pp.h)))
                pp.rec["cover"] = crec
            except Exception as error:  # noqa: BLE001
                pp.rec["cover_error"] = f"{type(error).__name__}: {error}"[:300]
        for w in heads_wrap:
            g.append(w)
        doc.parent = {id(c): p for p in doc.root.iter() for c in p}
        # depth with the seat: far limbs under it, the person over it
        if seat_owner is not None:
            common, kp, ko = lca_children(doc, g, seat_owner)
            if kp is not None and ko is not None and kp is not ko:
                kids = list(common)
                if kids.index(kp) < kids.index(ko):
                    common.remove(kp)
                    common.insert(list(common).index(ko) + 1, kp)
                    pp.rec["moved"] = True
                if far_el is not None:
                    fg = wrap_inverse(doc, common, far_el)
                    common.insert(list(common).index(ko), fg)
                doc.parent = {id(c): p for p in doc.root.iter() for c in p}
            elif far_el is not None:
                g.insert(1, wrap_inverse(doc, g, far_el))
                doc.parent = {id(c): p for p in doc.root.iter() for c in p}
        # a held thing that overlaps the torso goes between the body and the near arm and head (things held against the body)
        prb = PROP[pp.pose["build"]]
        tp = [
            J["neck"] + pp.p * prb["sw"] * pp.h,
            J["neck"] - pp.p * prb["sw"] * pp.h,
            J["mid_hip"] + pp.p * prb["hw"] * pp.h,
            J["mid_hip"] - pp.p * prb["hw"] * pp.h,
        ]
        tb = [
            min(q[0] for q in tp),
            min(q[1] for q in tp),
            max(q[0] for q in tp),
            max(q[1] for q in tp),
        ]
        front = None
        for c in pp.pose["contacts"]:
            if c["kind"] != "hold" or ":" in c["anchor"]:
                continue
            a = find_anchor(anchors, c["anchor"])
            owner = anchors[a]["owner"] if a else None
            if (
                owner is None
                or owner is seat_owner
                or owner is g
                or g in doc.ancestors(owner)
                or owner in doc.ancestors(g)
            ):
                continue
            if declT.get(id(owner), "holding") != "holding":
                continue
            ob = doc.box(doc.shapes_under(owner))
            if not ob:
                continue
            ix = max(0.0, min(ob[2], tb[2]) - max(ob[0], tb[0])) * max(
                0.0, min(ob[3], tb[3]) - max(ob[1], tb[1])
            )
            small = min(
                (ob[2] - ob[0]) * (ob[3] - ob[1]), (tb[2] - tb[0]) * (tb[3] - tb[1])
            )
            if small > 0 and ix / small >= 0.2:
                front = owner
                break
        if front is not None:
            common, kp, ko = lca_children(doc, g, front)
            if kp is not None and ko is not None and kp is not ko:
                if list(common).index(kp) > list(common).index(ko):
                    common.remove(kp)
                    common.insert(list(common).index(ko), kp)
                fg = el("g", id=name + "-front")
                if near_el in list(near_parent):
                    near_parent.remove(near_el)
                    fg.append(wrap_inverse(doc, common, near_el))
                Cc = doc.ctm(common) if common is not doc.root else np.eye(3)
                Cp = doc.ctm(g)
                for w in heads_wrap:
                    if w in list(g):
                        g.remove(w)
                        t = np.linalg.inv(Cc) @ Cp
                        hw_ = (
                            el("g", transform=mstr(t))
                            if not np.allclose(t, np.eye(3))
                            else el("g")
                        )
                        hw_.append(w)
                        fg.append(hw_)
                common.insert(list(common).index(ko) + 1, fg)
                pp.rec["in_front_of_body"] = front.get("id")
                doc.parent = {id(c): p for p in doc.root.iter() for c in p}
        # a hand that holds a thing painted in front of the person is drawn again over that thing
        for c in pp.pose["contacts"]:
            if (
                c["kind"] not in ("hold", "touch")
                or ":" in c["anchor"]
                or not c["joint"].endswith("wrist")
            ):
                continue
            a = find_anchor(anchors, c["anchor"])
            if a is None:
                continue
            owner = anchors[a]["owner"]
            s = c["joint"].split("_")[0]
            hl = layers["hands_over"].get(s)
            if (
                owner is None
                or hl is None
                or owner is g
                or g in doc.ancestors(owner)
                or owner in doc.ancestors(g)
            ):
                continue
            common, kp, ko = lca_children(doc, g, owner)
            if kp is None or ko is None or kp is ko:
                continue
            kids = list(common)
            if kids.index(kp) < kids.index(ko):
                common.insert(
                    kids.index(ko) + 1,
                    wrap_inverse(doc, common, hl.element(style, pp.h)),
                )
                pp.rec.setdefault("hands_over", []).append(f"{s}@{owner.get('id')}")
                doc.parent = {id(c2): p2 for p2 in doc.root.iter() for c2 in p2}
        # round 21: containment by code
        cands = []
        for tid, kd in kinds.items():
            if kd != "object":
                continue
            for n in doc.root.iter():
                i_ = n.get("id") or ""
                if tag(n) == "g" and (
                    i_ == tid or re.fullmatch(re.escape(tid) + r"-\d+", i_)
                ):
                    cands.append(n)
        for c in pp.pose["contacts"]:
            a = find_anchor(anchors, c["anchor"]) if ":" not in c["anchor"] else None
            if (
                a
                and c["joint"] == "hips"
                and anchors[a]["owner"] is not None
                and anchors[a]["owner"] not in cands
            ):
                cands.insert(0, anchors[a]["owner"])
        alive = {k for k, v in (living or {}).items() if v} | {
            k for k, v in kinds.items() if v == "character"
        }
        cands = [
            T
            for T in cands
            if (T.get("id") or "") not in alive
            and re.sub(r"-\d+$", "", T.get("id") or "") not in alive
            and not any(
                a_ in alive for a_ in [(x.get("id") or "") for x in doc.ancestors(T)]
            )
        ]
        cands = [
            T
            for T in cands
            if T is not g
            and g not in doc.ancestors(T)
            and T not in doc.ancestors(g)
            and not any(
                T is q or q in doc.ancestors(T) or T in doc.ancestors(q)
                for q in posed_groups
            )
        ]
        # round 22: declared relations first; geometry only on things the person declared nothing about
        dd, cr = [], None
        for r, T in decl:
            if T is None:
                continue
            rel = r["rel"]
            try:
                if rel == "in":
                    c_ = contain(doc, pp, [T], style, anchors, force=True)
                    dd.append(
                        {
                            "thing": r["thing"],
                            "rel": rel,
                            "how": "rim clip"
                            if c_ and c_.get("thing")
                            else "no rim found",
                            "contain": c_,
                        }
                    )
                    if c_ and c_.get("thing"):
                        cr = c_
                elif rel == "behind":
                    dd.append(
                        {
                            "thing": r["thing"],
                            "rel": rel,
                            **put_behind(doc, pp, T, style, anchors),
                        }
                    )
                elif rel in ("in-front-of",) + LYING:
                    ok_ = reorder(doc, g, T, over=True)
                    dd.append(
                        {
                            "thing": r["thing"],
                            "rel": rel,
                            "how": "drawn over the thing" if ok_ else "nested, kept",
                        }
                    )
            except Exception as error:  # noqa: BLE001
                dd.append(
                    {
                        "thing": r["thing"],
                        "rel": rel,
                        "error": f"{type(error).__name__}: {error}"[:300],
                    }
                )
        if dd:
            pp.rec["declared_depth"] = dd
        dn = [T for r, T in decl if T is not None and r["rel"] != "on"]
        cands = [
            T
            for T in cands
            if not any(
                T is D or D in doc.ancestors(T) or T in doc.ancestors(D) for D in dn
            )
        ]
        if pp.lying:
            cands = []
        if cr is None:
            try:
                cr = contain(doc, pp, cands, style, anchors)
                if cr:
                    cr["by_geometry"] = True
            except Exception as error:  # noqa: BLE001
                cr = {"error": f"{type(error).__name__}: {error}"[:300]}
        if cr:
            pp.rec["contain"] = cr
        pp.rec["joints_painter"] = {
            k: [round(float(v[0]), 1), round(float(v[1]), 1)] for k, v in pp.P.items()
        }
        pp.rec["joints_final"] = {
            k: [round(float(v[0]), 1), round(float(v[1]), 1)] for k, v in J.items()
        }
        pp.rec["painter_head_box"] = (
            [round(float(x), 1) for x in hb] if hb is not None else None
        )
        report["persons"].append(pp.rec)
    return ET.tostring(doc.root, encoding="unicode"), report
