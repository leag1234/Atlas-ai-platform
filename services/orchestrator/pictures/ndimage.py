"""Typed access to the few scipy.ndimage operations the picture steps use.

scipy ships no type information; every call goes through these wrappers so the
rest of the package stays strictly typed. Results keep scipy's own dtypes, so
measurements match the spike code they were ported from.
"""

from __future__ import annotations

from typing import Any, cast

import numpy as np
import numpy.typing as npt
from scipy import ndimage as _ndi  # type: ignore[import-untyped]

Mask = npt.NDArray[np.bool_]
Labels = npt.NDArray[np.int32]
Array = npt.NDArray[Any]


def binary_dilation(mask: Mask, iterations: int = 1) -> Mask:
    return cast(Mask, _ndi.binary_dilation(mask, iterations=iterations))


def binary_erosion(mask: Mask, iterations: int = 1) -> Mask:
    return cast(Mask, _ndi.binary_erosion(mask, iterations=iterations))


def binary_opening(mask: Mask, iterations: int = 1) -> Mask:
    return cast(Mask, _ndi.binary_opening(mask, iterations=iterations))


def distance_transform_edt(mask: Array) -> npt.NDArray[np.float64]:
    return cast(npt.NDArray[np.float64], _ndi.distance_transform_edt(mask))


def label(mask: Mask) -> tuple[Labels, int]:
    labelled, count = _ndi.label(mask)
    return cast(Labels, labelled), int(count)


def center_of_mass(values: Array) -> tuple[float, float]:
    """(row, column) of the centre of mass of a mask or weight array."""
    row, column = _ndi.center_of_mass(values)
    return float(row), float(column)


def label_sums(values: Array, labelled: Labels, count: int) -> list[float]:
    """Sum of `values` over each label 1..count."""
    sums = _ndi.sum(values, labelled, range(1, count + 1))
    return [float(v) for v in np.atleast_1d(np.asarray(sums))]


def uniform_filter(values: Array, size: int) -> Array:
    return cast(Array, _ndi.uniform_filter(values, size))
