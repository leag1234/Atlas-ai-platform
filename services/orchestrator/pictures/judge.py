"""The judge: the checklist measured by code on a free drawing after it is made.

Presence, explicit counts, relations, actions, support, frame, detached pieces
and text, each an item that passes or fails with a measured reason. Nothing
names a subject; every threshold is a share of the frame or of the thing
measured. This is version 2 of the spike judge (support judged shape by shape
inside a group; lettering inside a group is not a detached piece), the one the
ordering was measured with.
"""

from __future__ import annotations

import math
import re
import time
from typing import Any

import numpy as np

from services.orchestrator.pictures import ndimage as ndi
from services.orchestrator.pictures.checklist import fold
from services.orchestrator.pictures.geometry import (
    Geometry,
    Mask,
    norm_id,
    tag,
    thing_instances,
)

LIMBS = (
    "leg",
    "arm",
    "paw",
    "foot",
    "feet",
    "hand",
    "wing",
    "tentacle",
    "flipper",
    "fin",
    "hoof",
    "limb",
    "claw",
    "patte",
    "jambe",
    "bras",
    "pied",
    "main",
    "aile",
    "tentacule",
    "nageoire",
    "sabot",
)
HEADS = (
    "head",
    "face",
    "beak",
    "snout",
    "muzzle",
    "nose",
    "eye",
    "tete",
    "bec",
    "museau",
    "visage",
)


def _need(gap: float | None) -> float:
    """A measured gap; a missing one (an empty mask) fails the whole measure,
    as in the measured judge."""
    if gap is None:
        raise TypeError("empty_mask")
    return gap


class Judge:
    def __init__(self, svg: str, ck: dict[str, Any], frame: int = 400) -> None:
        self.ck = ck
        self.g = Geometry(svg, frame=frame)
        self.items: list[dict[str, Any]] = []
        self.things: dict[str, dict[str, Any]] = {t["id"]: t for t in ck["things"]}
        self.g.unit_masks()
        self._inst: dict[str, list[str]] = {}
        self.present: dict[str, bool] = {}

    # ---- which groups stand for a thing
    def instances(self, tid: str) -> list[str]:
        """Groups that are instances of the thing: tid-1, tid-2 ... if present,
        else tid (or its plural)."""
        if tid not in self._inst:
            self._inst[tid] = thing_instances(self.g.named, tid)
        return self._inst[tid]

    def container(self, tid: str) -> str | None:
        return next(
            (
                n
                for n in (tid, tid + "s", tid + "es", tid + "-group")
                if n in self.g.named
            ),
            None,
        )

    def tmask(self, tid: str) -> Mask:
        key = ("t", tid)
        if key not in self.g.cache:
            m = np.zeros((self.g.ch, self.g.cw), bool)
            level = (
                60
                if self.things.get(tid, {}).get("kind") in ("text", "labels")
                else 128
            )
            names = list(self.instances(tid))
            box = self.container(tid)
            if box and box not in names:
                names.append(box)
            for n in names:
                m |= self.g.mask(n, level)
            self.g.cache[key] = m
        return self.g.cache[key]

    def tvisible(self, tid: str) -> Mask:
        m = np.zeros((self.g.ch, self.g.cw), bool)
        for n in self.instances(tid):
            m |= self.g.visible(n)
        return m

    def parts(self, tid: str, name: str) -> list[str]:
        """Named sub-groups of a thing that are the given part (tid-name,
        tid-name-k, tid-k-name-j, also left/right variants)."""
        stem = name.rstrip("s")
        pattern = re.compile(
            rf"{re.escape(tid)}s?(-\d+)?-(?:[a-z]+-)?{re.escape(stem)}s?(-[a-z0-9]+)*"
        )
        return [
            n
            for n in self.g.named
            if pattern.fullmatch(n) and n not in self.instances(tid)
        ]

    def subparts(self, tid: str, words: tuple[str, ...]) -> list[str]:
        inst = set(self.instances(tid)) | {tid}
        return [
            n
            for n in self.g.named
            if any(n.startswith(i + "-") for i in inst)
            and any(
                w in n.split("-") or any(p.startswith(w) for p in n.split("-"))
                for w in words
            )
        ]

    def item(
        self,
        key: str,
        kind: str,
        ok: bool,
        msg: str = "",
        groups: list[str] | tuple[str, ...] = (),
        data: dict[str, Any] | None = None,
        verified: bool = True,
    ) -> None:
        self.items.append(
            {
                "key": key,
                "kind": kind,
                "ok": bool(ok),
                "msg": "" if ok else msg,
                "groups": list(groups),
                "data": data or {},
                "verified": verified,
            }
        )

    def u(self, pixels: float) -> int:
        return self.g.units_of(pixels)

    def box_text(self, mask: Mask) -> str:
        b = self.g.ubox(mask)
        return "nowhere" if b is None else f"x {b[0]} to {b[2]}, y {b[1]} to {b[3]}"

    # ---- checks
    def run(self) -> Judge:
        g = self.g
        frame_area = g.fw * g.fh
        present: dict[str, bool] = {}
        for t in self.ck["things"]:
            tid, kind = t["id"], t["kind"]
            inst = self.instances(tid)
            if kind == "text":
                want = fold(t["text"])
                texts = [fold(g.text_of(n)) for n in g.text_nodes()]
                joined = " ".join(texts)
                ok = want in joined or want.replace(" ", "") in joined.replace(" ", "")
                present[tid] = ok
                self.item(
                    f"text:{tid}",
                    "text",
                    ok,
                    f'The words "{t["text"]}" must be written exactly, in <text id="{tid}">.',
                    [tid],
                )
                continue
            if kind == "labels":
                box = self.container(tid)
                n = (
                    len([x for x in g.shapes_of(g.named[box]) if tag(x) == "text"])
                    if box
                    else 0
                )
                if not n:
                    n = len(g.text_nodes()) - sum(
                        1 for x in self.ck["things"] if x["kind"] == "text"
                    )
                present[tid] = n >= 1
                self.item(
                    f"present:{tid}",
                    "presence",
                    n >= 1,
                    f"The request asks for {t['words']}: write them as <text> elements "
                    f'inside <g id="{tid}">.',
                    [tid],
                )
                continue
            m = self.tmask(tid)
            area = int(g.in_frame(m).sum())
            floor = 0.0012 * frame_area if kind != "place" else 0.004 * frame_area
            ok = bool(inst) and area >= floor
            present[tid] = ok
            if not inst:
                msg = f'"{t["words"]}" is not found: draw it in <g id="{tid}">' + (
                    "."
                    if not (t["count"] and t["count"] > 1)
                    else f" (one group per item: {tid}-1 ... {tid}-{t['count']})."
                )
            else:
                msg = (
                    f'"{tid}" is too small to be seen '
                    f"({100 * area / frame_area:.2f} % of the picture): draw it larger."
                )
            self.item(
                f"present:{tid}",
                "presence",
                ok,
                msg,
                [tid],
                {"area_share": round(area / frame_area, 4)},
            )
            if t["count"] is not None and t["count"] != 1:
                self.count_check(
                    f"count:{tid}",
                    tid,
                    [n for n in inst if re.search(r"-\d+$", n)],
                    t["count"],
                    t["words"],
                    f"{tid}-1 ... {tid}-{t['count']}",
                )
            elif t["count"] == 1:
                numbered = [n for n in inst if re.search(r"-\d+$", n)]
                self.item(
                    f"count:{tid}",
                    "count",
                    len(numbered) <= 1,
                    f"The request says {t['words']}: exactly one, the drawing has "
                    f"{len(numbered)}.",
                    [tid],
                )
            for p in t["parts"]:
                if p["count"] is None:
                    continue
                self.count_check(
                    f"count:{tid}.{p['name']}",
                    tid,
                    self.parts(tid, p["name"]),
                    p["count"],
                    p["words"],
                    f"{tid}-{p['name']}-1 ... {tid}-{p['name']}-{p['count']}",
                    part=True,
                )
        self.present = present
        for r in self.ck["relations"]:
            self.relation(r)
        for a in self.ck["actions"]:
            self.action(a)
        self.support()
        self.frame_and_pieces()
        self.text_checks()
        return self

    def count_check(
        self,
        key: str,
        tid: str,
        groups: list[str],
        want: int,
        words: str,
        naming: str,
        part: bool = False,
    ) -> None:
        g = self.g
        masks = [g.mask(n) for n in groups]
        seen = [(n, m) for n, m in zip(groups, masks) if g.in_frame(m).sum() >= 6]
        got = len(seen)
        if got != want:
            if not groups:
                msg = (
                    f"The request says {words}: the program finds none it can count. "
                    f"Give each one its own group: {naming}."
                )
            else:
                msg = (
                    f"The request says {words}: the drawing has {got} "
                    f"({', '.join(n for n, _ in seen) or 'none visible'}). Draw exactly "
                    f"{want}, each in its own group: {naming}."
                )
            self.item(key, "count", False, msg, [tid], {"want": want, "got": got})
            return
        bad: list[tuple[str, str, int]] = []
        for i in range(len(seen)):
            for j in range(i):
                a, b = seen[i][1], seen[j][1]
                ca, cb = ndi.center_of_mass(a), ndi.center_of_mass(b)
                close = math.hypot(ca[0] - cb[0], ca[1] - cb[1]) < g.px(0.012)
                over = (a & b).sum() > 0.6 * np.minimum(a.sum(), b.sum())
                if close or over:
                    bad.append((seen[j][0], seen[i][0], int((a & b).sum())))
        hidden: list[tuple[str, float]] = []
        if not part:
            for n, m in seen:
                vis = g.visible(n).sum() / np.maximum(1, m.sum())
                if vis < 0.25:
                    hidden.append((n, round(float(vis), 2)))
        ok = not bad and not hidden
        msg = ""
        if bad:
            first, second, shared = bad[0]
            msg = (
                f'The {want} items of "{words}" cannot be counted: "{first}" and '
                f'"{second}" lie on top of each other (shared area '
                f"{self.u(math.sqrt(shared))} units across). Move them apart so that "
                "each one shows side by side."
            )
        elif hidden:
            msg = (
                f'"{hidden[0][0]}" is almost entirely hidden ({int(100 * hidden[0][1])} % '
                f'visible): the {want} items of "{words}" must all be seen.'
            )
        self.item(
            key,
            "count",
            ok,
            msg,
            [tid] + [x for pair in bad for x in pair[:2]],
            {"want": want, "got": got},
        )

    def relation(self, r: dict[str, Any]) -> None:
        g = self.g
        t, a, b = r["type"], r["a"], r["b"]
        key = f"rel:{t}:{a}:{b}"
        if not self.present.get(a) or not self.present.get(b):
            missing = a if not self.present.get(a) else b
            self.item(
                key,
                "relation",
                False,
                f'"{r["words"]}": "{missing}" is missing, so this cannot be shown.',
                [a, b],
            )
            return
        ka, kb = self.things[a]["kind"], self.things[b]["kind"]
        if "text" in (ka, kb) or "labels" in (ka, kb):
            self.item(key, "relation", True, verified=False)
            return
        am, bm = self.tmask(a), self.tmask(b)
        ab, bb = g.bbox(am), g.bbox(bm)
        gap = _need(
            self.linked(am, bm)
            if t in ("holding", "wearing", "against", "inside")
            else g.gap(am, bm)
        )
        gap_u = self.u(gap)
        touching = gap <= g.px(0.02)
        where = f'"{a}" is at {self.box_text(am)}; "{b}" is at {self.box_text(bm)}'
        ok, msg = True, ""
        assert ab is not None and bb is not None
        if kb == "place" and t in ("inside", "on", "near", "around", "in_front_of"):
            ok = gap <= g.px(0.10)
            msg = (
                f'"{r["words"]}": "{a}" must be in or on "{b}", they are {gap_u} units '
                f"apart ({where})."
            )
        elif t == "inside":
            aw = ab[2] - ab[0] + 1
            grow = int(0.08 * (bb[2] - bb[0] + 1))
            within = (
                max(0, min(ab[2], bb[2] + grow) - max(ab[0], bb[0] - grow) + 1) / aw
            )
            ok = touching and within >= 0.6 and ab[3] >= bb[1]
            msg = (
                f'"{r["words"]}": "{a}" must be inside "{b}" (touching it, between its '
                f"left and right edges). Measured: gap {gap_u} units, {int(100 * within)} "
                f'% of the width of "{a}" lies within "{b}" ({where}).'
            )
        elif t == "on":
            base = g.base_band(am, 0.2)
            base_gap = _need(g.gap(base, bm))
            ca = ndi.center_of_mass(am)
            ok = base_gap <= g.px(0.02) and ca[0] < bb[3]
            msg = (
                f'"{r["words"]}": the base of "{a}" must rest on "{b}". Measured: the '
                f'base of "{a}" is {self.u(base_gap)} units away from "{b}" ({where}). '
                f'Move "{a}" (or "{b}") so that they touch.'
            )
        elif t in ("holding", "wearing", "against"):
            ok = touching
            msg = (
                f'"{r["words"]}": "{a}" and "{b}" must touch (overlap them). Measured '
                f"gap: {gap_u} units ({where})."
            )
            if ok and t == "wearing":
                grow = g.px(0.03)
                box = np.zeros_like(bm)
                box[
                    max(0, bb[1] - grow) : bb[3] + grow + 1,
                    max(0, bb[0] - grow) : bb[2] + grow + 1,
                ] = True
                share = (am & box).sum() / np.maximum(1, am.sum())
                ok = bool(share >= 0.5)
                msg = (
                    f'"{r["words"]}": "{a}" must be worn by "{b}", only '
                    f'{int(100 * share)} % of it lies on "{b}" ({where}).'
                )
        elif t == "near" or t == "around":
            ok = gap <= g.px(0.25 if t == "near" else 0.10)
            msg = (
                f'"{r["words"]}": "{a}" and "{b}" are {gap_u} units apart; bring them '
                f"closer ({where})."
            )
        elif t == "versus":
            ca, cb = ndi.center_of_mass(am), ndi.center_of_mass(bm)
            ok = abs(ca[1] - cb[1]) >= g.px(0.12)
            msg = (
                f'"{r["words"]}": "{a}" and "{b}" must face each other from two sides; '
                f"their centres are only {self.u(abs(ca[1] - cb[1]))} units apart "
                "horizontally."
            )
        elif t in ("above", "below"):
            ca, cb = ndi.center_of_mass(am), ndi.center_of_mass(bm)
            ok = (
                (ca[0] < cb[0] - g.px(0.02))
                if t == "above"
                else (ca[0] > cb[0] + g.px(0.02))
            )
            msg = f'"{r["words"]}": "{a}" must be {t} "{b}" ({where}).'
        elif t in ("behind", "in_front_of"):
            oa = min(
                g.order[id(n)]
                for i in self.instances(a)
                for n in g.shapes_of(g.named[i])
            )
            ob = min(
                g.order[id(n)]
                for i in self.instances(b)
                for n in g.shapes_of(g.named[i])
            )
            ok = oa < ob if t == "behind" else oa > ob
            msg = (
                f'"{r["words"]}": draw "{a}" {"before" if t == "behind" else "after"} '
                f'"{b}" in the document so that it is {t.replace("_", " ")} it.'
            )
        elif t == "facing":
            d = self.direction(a)
            if d is None:
                self.item(key, "relation", True, verified=False)
                return
            ca, cb = ndi.center_of_mass(am), ndi.center_of_mass(bm)
            side = 1 if cb[1] > ca[1] else -1
            ok = d == 0 or d == side
            msg = (
                f'"{r["words"]}": "{a}" looks away from "{b}"; turn its head toward '
                f'"{b}" (which is on its {"right" if side > 0 else "left"}).'
            )
        self.item(key, "relation", ok, msg, [a, b], {"gap_units": gap_u})

    def linked(self, am: Mask, bm: Mask) -> float | None:
        """Gap in pixels between two things, 0 if an unnamed thin piece (a string,
        a rope, a line: under 2 % of the frame) touches both."""
        g = self.g
        gap = g.gap(am, bm)
        if gap is None or gap <= g.px(0.02):
            return gap
        ga = ndi.binary_dilation(am, iterations=g.px(0.02))
        gb = ndi.binary_dilation(bm, iterations=g.px(0.02))
        for k, (unit, um) in enumerate(zip(g.units, g.unit_masks())):
            if unit.id is not None or um.sum() >= 0.02 * g.fw * g.fh:
                continue
            # A thin line rendered small is fainter than the mask level.
            key = ("soft", k)
            if key not in g.cache:
                g.cache[key] = g.alpha(unit.nodes, 25)
            soft = g.cache[key]
            if (
                soft.any()
                and soft.sum() < 0.02 * g.fw * g.fh
                and (soft & ga).any()
                and (soft & gb).any()
            ):
                return 0.0
        return gap

    def direction(self, tid: str) -> int | None:
        """-1 looks left, 1 looks right, 0 faces the viewer, None unknown."""
        heads = self.subparts(tid, HEADS)
        if not heads:
            return None
        g = self.g
        m = self.tmask(tid)
        b = g.bbox(m)
        hm = np.zeros_like(m)
        for n in heads:
            hm |= g.mask(n)
        if not hm.any() or b is None:
            return None
        dx = ndi.center_of_mass(hm)[1] - ndi.center_of_mass(m)[1]
        if abs(dx) < 0.06 * (b[2] - b[0] + 1):
            return 0
        return 1 if dx > 0 else -1

    def tilt(self, mask: Mask) -> float | None:
        ys, xs = np.nonzero(mask)
        if len(xs) < 30:
            return None
        vals, vecs = np.linalg.eigh(np.cov(np.stack([xs, ys]).astype(np.float64)))
        if vals[1] < 1.3 * max(vals[0], 1e-6):
            return None
        ang = abs(math.degrees(math.atan2(vecs[1, 1], vecs[0, 1]))) % 180
        ang = min(ang, 180 - ang)
        return float(min(ang, 90 - ang))

    def dynamic(self, who: str) -> tuple[bool, str]:
        """Movement shown by the pose: one of several like limbs clearly higher
        than the others, a limb above the head, the body leaning, or the figure
        off its support."""
        g = self.g
        m = self.tmask(who)
        b = g.bbox(m)
        if b is None:
            return False, "not drawn"
        height = b[3] - b[1] + 1
        limbs = self.subparts(who, LIMBS)
        notes: list[str] = []
        groups: dict[str, list[str]] = {}
        for n in limbs:
            stem = re.sub(r"(-\d+|-left|-right|-front|-back|-rear|-l|-r)+$", "", n)
            groups.setdefault(stem, []).append(n)
        for stem, names in groups.items():
            boxes = [g.bbox(g.mask(n)) for n in names]
            lows = [x[3] for x in boxes if x is not None]
            if len(lows) >= 2:
                spread = (max(lows) - min(lows)) / height
                notes.append(
                    f"{stem}: ends differ by {int(100 * spread)} % of the height"
                )
                if spread >= 0.10:
                    return True, notes[-1]
        for n in limbs:
            lb = g.bbox(g.mask(n))
            if (
                lb is not None
                and lb[1] <= b[1] + 0.12 * height
                and (lb[3] - lb[1]) > 0.12 * height
            ):
                return True, f"{n} reaches the top of the figure"
        heads = self.subparts(who, HEADS)
        if heads and limbs:
            head_top = min(
                x[1] for x in (g.bbox(g.mask(n)) for n in heads) if x is not None
            )
            for n in limbs:
                lb = g.bbox(g.mask(n))
                if lb is not None and lb[1] < head_top - 0.02 * height:
                    return True, f"{n} reaches above the head"
        lean = self.tilt(m)
        if lean is not None:
            notes.append(f"lean {lean:.0f} degrees")
            if lean >= 10:
                return True, notes[-1]
        if not limbs:
            notes.append("no limb group is named")
        return False, "; ".join(notes) or "nothing measurable"

    def action(self, act: dict[str, Any]) -> None:
        g = self.g
        who, verb = act["who"], act["verb"]
        key = f"act:{who}:{norm_id(verb)}"
        if not self.present.get(who):
            self.item(
                key,
                "action",
                False,
                f'Action "{act["words"]}": "{who}" is missing.',
                [who],
            )
            return
        am = self.tmask(who)
        fails: list[str] = []
        groups = [who]
        measured = False
        for c in act["checks"]:
            kind = c["check"]
            other = c.get("with")
            if kind in ("contact", "near", "facing"):
                if not self.present.get(other or ""):
                    fails.append(f'"{other}" is missing')
                    measured = True
                    continue
                assert other is not None
                om = self.tmask(other)
                if self.things[other]["kind"] in ("text", "labels"):
                    continue
                gap = self.linked(am, om)
                groups.append(other)
                if kind == "contact":
                    measured = True
                    limit = (
                        g.px(0.10)
                        if self.things[other]["kind"] == "place"
                        else g.px(0.02)
                    )
                    if _need(gap) > limit:
                        fails.append(
                            f'"{who}" must touch "{other}" (measured gap: '
                            f'{self.u(_need(gap))} units; "{who}" at '
                            f'{self.box_text(am)}, "{other}" at {self.box_text(om)})'
                        )
                elif kind == "near":
                    measured = True
                    if _need(gap) > g.px(0.2):
                        fails.append(
                            f'"{who}" must be close to "{other}" (measured gap: '
                            f"{self.u(_need(gap))} units)"
                        )
                else:
                    d = self.direction(who)
                    if d is None:
                        continue
                    measured = True
                    side = (
                        1
                        if ndi.center_of_mass(om)[1] > ndi.center_of_mass(am)[1]
                        else -1
                    )
                    if d not in (0, side):
                        fails.append(
                            f'"{who}" must look toward "{other}" (on its '
                            f"{'right' if side > 0 else 'left'})"
                        )
            elif kind == "dynamic":
                measured = True
                ok, what = self.dynamic(who)
                if not ok:
                    fails.append(
                        f'the pose of "{who}" shows no movement ({what}): lift one limb '
                        "clearly higher than the others (by at least 10 % of the height "
                        "of the body) or lean the whole body by 10 degrees or more, and "
                        f"keep each limb in its own sub-group ({who}-leg-1, {who}-arm-1 ...)"
                    )
            elif kind == "airborne":
                measured = True
                if self.supported(who)[0]:
                    fails.append(
                        f'"{who}" must be off the ground: leave clear space under it'
                    )
        self.item(
            key,
            "action",
            not fails,
            f'Action "{act["words"]}": ' + "; ".join(fails) + ".",
            groups,
            verified=measured,
        )

    # ---- support: nothing hangs in the air unless the request says so
    def supported(self, tid: str) -> tuple[bool, str | int | None]:
        """(ok, what): does the base of the thing rest on something drawn below
        it, or on the bottom edge of the frame?"""
        g = self.g
        m = self.tmask(tid)
        b = g.bbox(m)
        if b is None:
            return True, "not drawn"
        if b[3] >= g.oy + g.fh - g.px(0.015):
            return True, "bottom edge of the frame"
        base = ndi.binary_dilation(g.base_band(m, 0.12), iterations=g.px(0.02))
        own: set[int] = set()
        container = self.container(tid)
        for n in self.instances(tid) + ([container] if container else []):
            k = g.unit_of(n)
            if k is not None:
                own.add(k)
        return self.supported2(m, b, base, own)

    def supported2(
        self,
        m: Mask,
        b: tuple[int, int, int, int],
        base: Mask,
        own: set[int],
    ) -> tuple[bool, str | int | None]:
        """A group often holds the ground AND what stands behind it, so the shapes
        of every group touching the base are looked at one by one. A shape is a
        support if it starts lower than the top of the thing, or if it is a wide
        surface going on under the thing without reaching the top of the frame. A
        backdrop painted in one piece counts only in the lower part of the frame."""
        g = self.g
        height, width = b[3] - b[1] + 1, b[2] - b[0] + 1
        full = g.fw * g.fh
        best_gap: int | None = None
        backdrop = False
        for k, (unit, um) in enumerate(zip(g.units, g.unit_masks())):
            if k in own or not um.any() or all(tag(n) == "text" for n in unit.nodes):
                continue
            if not (base & um).any():
                below = um[b[3] :, max(0, b[0]) : b[2] + 1]
                if below.any() and g.in_frame(um).sum() <= 0.9 * full:
                    d = int(np.nonzero(below.any(axis=1))[0][0])
                    best_gap = d if best_gap is None else min(best_gap, d)
                continue
            nodes = [n for n in unit.nodes if tag(n) != "text"][:80]
            for node in nodes:
                key = ("shape", id(node))
                if key not in g.cache:
                    g.cache[key] = um if len(nodes) == 1 else g.alpha([node], 128)
                sm = g.cache[key]
                if not sm.any() or not (base & sm).any():
                    continue
                sb = g.bbox(sm)
                assert sb is not None
                if g.in_frame(sm).sum() > 0.9 * full:
                    backdrop = True
                    continue
                if sb[1] >= b[1] + 0.10 * height:
                    return True, unit.id or "an unnamed surface"
                if (
                    sb[1] > g.oy + 0.02 * g.fh
                    and sb[3] >= b[3] + 0.05 * g.fh
                    and (sb[2] - sb[0] + 1) >= 1.5 * width
                ):
                    return True, unit.id or "an unnamed surface"
        if backdrop and b[3] >= g.oy + 0.55 * g.fh:
            return True, "the lower part of the backdrop"
        return False, best_gap

    def support(self) -> None:
        if self.ck["genre"] in ("portrait", "diagram"):
            return  # a bust and a diagram have no ground
        carried = {
            r["a"]
            for r in self.ck["relations"]
            if r["type"] in ("inside", "on", "holding", "wearing", "against")
        }
        for t in self.ck["things"]:
            tid = t["id"]
            if (
                t["kind"] not in ("character", "object")
                or t["where"] != "ground"
                or tid in carried
                or not self.present.get(tid)
            ):
                continue
            ok, what = self.supported(tid)
            gap = (
                f"the nearest surface under it is {self.u(what)} units lower"
                if isinstance(what, (int, float))
                else "nothing is drawn under it"
            )
            box = self.g.ubox(self.tmask(tid))
            self.item(
                f"support:{tid}",
                "support",
                ok,
                f'"{tid}" hangs in the air: its base (y {box[3] if box else "?"}) '
                f"touches nothing; {gap}. Move it down onto its support, or draw the "
                "surface it stands on up to its base.",
                [tid],
                {"on": what if ok else None},
            )

    def frame_and_pieces(self) -> None:
        g = self.g
        supports = {
            r["b"] for r in self.ck["relations"] if r["type"] in ("inside", "on")
        }
        for t in self.ck["things"]:
            tid = t["id"]
            if t["kind"] not in ("character", "object") or not self.present.get(tid):
                continue
            m = self.tmask(tid)
            total = int(m.sum())
            if tid not in supports:
                out = m.copy()
                out[g.oy : g.oy + g.fh, g.ox : g.ox + g.fw] = False
                if self.ck["genre"] == "portrait":
                    out[g.oy + g.fh :, :] = False  # a portrait is cut at the bottom
                share = out.sum() / max(1, total)
                b = g.ubox(m)
                assert b is not None
                sides: list[str] = []
                if b[0] < g.vb[0]:
                    sides.append(f"left by {int(g.vb[0] - b[0])} units")
                if b[2] > g.vb[0] + g.vb[2]:
                    sides.append(f"right by {int(b[2] - g.vb[0] - g.vb[2])} units")
                if b[1] < g.vb[1]:
                    sides.append(f"top by {int(g.vb[1] - b[1])} units")
                if b[3] > g.vb[1] + g.vb[3] and self.ck["genre"] != "portrait":
                    sides.append(f"bottom by {int(b[3] - g.vb[1] - g.vb[3])} units")
                self.item(
                    f"frame:{tid}",
                    "frame",
                    bool(share <= 0.08),
                    f'"{tid}" is cut by the frame ({int(100 * share)} % of it is '
                    f"outside: {', '.join(sides)}). Move or shrink it so that it is "
                    "whole inside the picture.",
                    [tid],
                    {"outside_share": round(float(share), 3)},
                )
            # A single thing must be in one piece.
            if t["plural"] or (t["count"] or 1) > 1:
                for n in self.instances(tid) if (t["count"] or 1) > 1 else []:
                    self.pieces(f"piece:{n}", n, g.mask(n))
            else:
                # Lettering inside the group of a thing is not a piece of it.
                container = self.container(tid)
                shapes = [
                    x
                    for n in self.instances(tid) + ([container] if container else [])
                    for x in g.shapes_of(g.named[n])
                    if tag(x) != "text"
                ]
                m = g.alpha(shapes, 128) if shapes else m
                self.pieces(f"piece:{tid}", tid, m)

    def pieces(self, key: str, name: str, m: Mask) -> None:
        g = self.g
        total = int(m.sum())
        if total < 20:
            return
        grown = ndi.binary_dilation(m, iterations=g.px(0.015))
        lab, n = ndi.label(grown)
        if n <= 1:
            self.item(key, "pieces", True)
            return
        sizes = ndi.label_sums(m, lab, n)
        main = int(np.argmax(sizes)) + 1
        loose = [
            (k + 1, s)
            for k, s in enumerate(sizes)
            if k + 1 != main and s >= 0.03 * total
        ]
        if not loose:
            self.item(key, "pieces", True)
            return
        k, s = max(loose, key=lambda x: x[1])
        piece = (lab == k) & m
        gap = _need(g.gap(piece, (lab == main) & m))
        self.item(
            key,
            "pieces",
            False,
            f'A piece of "{name}" is detached from the rest: the piece at '
            f"{self.box_text(piece)} ({int(100 * s / total)} % of it) is "
            f"{self.u(gap)} units away from the main body at "
            f"{self.box_text((lab == main) & m)}. Join them (move the piece by "
            f"{self.u(gap)} units toward the body, or draw what connects them).",
            [name],
            {"gap_units": self.u(gap)},
        )

    def text_checks(self) -> None:
        g = self.g
        nodes = g.text_nodes()
        outside: list[str] = []
        overlaps: list[str] = []
        hidden: list[str] = []
        masks: list[Mask | None] = []
        g.unit_masks()
        for node in nodes:
            m = g.alpha([node], 50)
            if not m.any():
                masks.append(None)
                continue
            masks.append(m)
            words = g.text_of(node)[:30]
            out = m.copy()
            out[g.oy : g.oy + g.fh, g.ox : g.ox + g.fw] = False
            b = g.ubox(m)
            assert b is not None
            if out.sum() > 0.03 * m.sum() or not g.in_frame(m).any():
                sides: list[str] = []
                if b[0] < g.vb[0]:
                    sides.append(f"left by {int(g.vb[0] - b[0])}")
                if b[2] > g.vb[0] + g.vb[2]:
                    sides.append(f"right by {int(b[2] - g.vb[0] - g.vb[2])}")
                if b[1] < g.vb[1]:
                    sides.append(f"top by {int(g.vb[1] - b[1])}")
                if b[3] > g.vb[1] + g.vb[3]:
                    sides.append(f"bottom by {int(b[3] - g.vb[1] - g.vb[3])}")
                width = b[2] - b[0]
                fit = int(100 * min(1.0, 0.92 * g.vb[2] / max(1, width)))
                outside.append(
                    f'"{words}" leaves the frame ({", ".join(sides)} units; it is '
                    f"{width} units wide at x {b[0]} to {b[2]}): move it inside"
                    + (
                        f" and reduce its font-size to {fit} % of the present size"
                        if width > 0.95 * g.vb[2]
                        else ""
                    )
                )
            # Covered by something drawn after it.
            order = g.order[id(node)]
            later = [s for s in g.shapes if g.order[id(s)] > order and tag(s) != "text"]
            if later:
                cover = g.alpha(later, 128)
                share = (m & cover).sum() / np.maximum(1, m.sum())
                if share > 0.3:
                    hidden.append(
                        f'"{words}" is covered at {int(100 * share)} % by shapes drawn '
                        "after it: put the text last in the document"
                    )
        boxes = [g.bbox(m) if m is not None else None for m in masks]
        for i in range(len(nodes)):
            for j in range(i):
                a, b2 = boxes[i], boxes[j]
                if a is None or b2 is None:
                    continue
                w = min(a[2], b2[2]) - max(a[0], b2[0]) + 1
                h = min(a[3], b2[3]) - max(a[1], b2[1]) + 1
                if (
                    w > 0
                    and h > 0
                    and w * h
                    > 0.12
                    * min(
                        (a[2] - a[0] + 1) * (a[3] - a[1] + 1),
                        (b2[2] - b2[0] + 1) * (b2[3] - b2[1] + 1),
                    )
                ):
                    overlaps.append(
                        f'"{g.text_of(nodes[j])[:24]}" and "{g.text_of(nodes[i])[:24]}" '
                        f"overlap by {self.u(w)} x {self.u(h)} units: move one of them by "
                        f"{self.u(min(w, h)) + 6} units "
                        f"{'vertically' if h <= w else 'horizontally'}"
                    )
        self.item(
            "text:frame",
            "text",
            not outside,
            "Text out of the frame: " + "; ".join(outside[:4]) + ".",
            ["text"],
            {"n": len(outside)},
        )
        self.item(
            "text:overlap",
            "text",
            not overlaps,
            "Texts on top of each other: " + "; ".join(overlaps[:4]) + ".",
            ["text"],
            {"n": len(overlaps)},
        )
        self.item(
            "text:covered",
            "text",
            not hidden,
            "Text hidden: " + "; ".join(hidden[:3]) + ".",
            ["text"],
            {"n": len(hidden)},
        )

    # ---- results
    def failures(self) -> list[dict[str, Any]]:
        return [i for i in self.items if not i["ok"]]

    def quality(self) -> tuple[float, dict[str, Any]]:
        """Tie-break between drawings failing the same number of checks:
        0.45 x coverage of the frame by the named characters and objects (full at
        30 %) + 0.35 x their value contrast with what surrounds them (full at 50
        levels of 255) + 0.20 x richness (number of shapes, full at 120)."""
        g = self.g
        subject = np.zeros((g.ch, g.cw), bool)
        kinds: tuple[str, ...] = ("character", "object")
        if not any(t["kind"] in kinds for t in self.ck["things"]):
            kinds = ("place",)
        for t in self.ck["things"]:
            if t["kind"] in kinds and self.present.get(t["id"]):
                subject |= self.tvisible(t["id"])
        subject = g.in_frame(subject)
        cover = subject.sum() / subject.size
        contrast = 0.0
        if subject.any() and not subject.all():
            lum = np.asarray(
                g.render(padded=False, size=g.frame_px, background="#ffffff", mode="L"),
                np.float32,
            )
            ring = ndi.binary_dilation(subject, iterations=g.px(0.04)) & ~subject
            if ring.any():
                contrast = abs(float(lum[subject].mean()) - float(lum[ring].mean()))
        rich = len(g.shapes)
        score = (
            0.45 * min(cover / 0.30, 1)
            + 0.35 * min(contrast / 50, 1)
            + 0.20 * min(rich / 120, 1)
        )
        # Rounded as measured (numpy rounding when the score is a numpy float).
        return float(round(score, 4)), {
            "coverage": round(float(cover), 3),
            "contrast": round(contrast, 1),
            "shapes": rich,
        }

    def summary(self) -> dict[str, Any]:
        counted = [i for i in self.items if i["verified"]]
        q, parts = self.quality()
        kinds: dict[str, list[int]] = {}
        for i in counted:
            k = kinds.setdefault(i["kind"], [0, 0])
            k[1] += 1
            k[0] += i["ok"]
        return {
            "items": len(counted),
            "ok": sum(i["ok"] for i in counted),
            "unverified": len(self.items) - len(counted),
            "failed": [
                {"key": i["key"], "msg": i["msg"], "groups": i["groups"]}
                for i in counted
                if not i["ok"]
            ],
            "passed": [i["key"] for i in counted if i["ok"]],
            "by_kind": kinds,
            "quality": q,
            "quality_parts": parts,
            "floating": [
                i["key"]
                for i in counted
                if not i["ok"] and i["kind"] in ("support", "pieces")
            ],
            "named_groups": len(self.g.named),
            "units": len(self.g.units),
            "shapes": len(self.g.shapes),
        }


def judge(
    svg: str, ck: dict[str, Any], frame: int = 400
) -> tuple[Judge, dict[str, Any]]:
    started = time.monotonic()
    j = Judge(svg, ck, frame).run()
    s = j.summary()
    s["seconds"] = round(time.monotonic() - started, 2)
    return j, s
