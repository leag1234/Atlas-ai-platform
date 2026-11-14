"""Pictures by code: four SVG drafts written by a text model, finished by code.

The design note and the measurements behind each step live outside the
repository (spike rounds 10 to 23); docs/ describes the shipped chain. The
package is split by step:

- svg_text, sanitise: recover and clean the untrusted SVG a model wrote;
- render, geometry, svg_doc, ndimage: rasterise and measure it;
- checklist, judge, checks, ordering: what the request asks and how each
  draft meets it (ordering only: every finished draft is shown);
- animal_kit, body_kit, face_kit, finish, labels: what code redraws;
- worker, job: the child process those steps run in, with limits;
- painter, turn, choice: the chat turn, the four candidates and the choice;
- rewrite, cpu_server, cpu_turn, cpu_finish: pictures drawn by an open image
  model on a CPU server (two candidates), with painting by code as fallback.

Everything stays off unless ATLAS_PICTURES_BY_CODE=1; the provisional animal
kit has its own switch, ATLAS_PICTURE_ANIMAL_KIT=1. The short plan asked of
the painter (ATLAS_PICTURE_BRIEF_PLAN=1) and the spare drafts
(ATLAS_PICTURE_SPARE_DRAFTS) are off by default; the contour filters bounded
to their group are on (ATLAS_PICTURE_BOUNDED_CONTOURS=0 turns them off).
ATLAS_PICTURES_CPU=1 (off by default, needs ATLAS_PICTURES_BY_CODE=1) draws
on the CPU picture server first and paints by code when it cannot answer.
"""

from __future__ import annotations

import os


def enabled() -> bool:
    """The whole picture chain (off by default)."""
    return os.environ.get("ATLAS_PICTURES_BY_CODE", "").strip() == "1"


def animal_kit_enabled() -> bool:
    """The provisional animal kit (round 23), switchable on its own."""
    return os.environ.get("ATLAS_PICTURE_ANIMAL_KIT", "").strip() == "1"


def brief_plan_enabled() -> bool:
    """The painter is asked for a short plan before its SVG (off by default)."""
    return os.environ.get("ATLAS_PICTURE_BRIEF_PLAN", "").strip() == "1"


def bounded_contours_enabled() -> bool:
    """Contour filters bounded to their group (finish.bound_contours); on by
    default, ATLAS_PICTURE_BOUNDED_CONTOURS=0 keeps the canvas-wide regions."""
    return os.environ.get("ATLAS_PICTURE_BOUNDED_CONTOURS", "").strip() != "0"


SPARE_DRAFTS_LIMIT = 2


def spare_drafts() -> int:
    """Drafts started beyond the four shown (ATLAS_PICTURE_SPARE_DRAFTS, 0 to 2,
    default 0): the first four painted are kept, the others are stopped."""
    try:
        value = int(os.environ.get("ATLAS_PICTURE_SPARE_DRAFTS", "").strip() or 0)
    except ValueError:
        return 0
    return min(max(value, 0), SPARE_DRAFTS_LIMIT)


def cpu_enabled() -> bool:
    """Pictures from the CPU image server first (off by default); painting by
    code stays the fallback, so the picture turn itself must be on."""
    return enabled() and os.environ.get("ATLAS_PICTURES_CPU", "").strip() == "1"
