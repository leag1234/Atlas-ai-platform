# Atlas visual identity

Symbol **D1, "arc and dot"**: an elliptical arc open at the bottom, with unequal
branches, and a solid disc set below the short branch. The asymmetry of the two
branches gives the movement; the gap between the disc and the arc is what keeps the
mark readable even without colour contrast.

## Colours

| Role | Hex | Use |
|---|---|---|
| Ink | `#1A1A18` | the arc, on a light background |
| Brick | `#C2410C` | the disc, on a light background |
| Light ink | `#EDECE8` | the arc, on a dark background |
| Light brick | `#E05A2B` | the disc, on a dark background |

The accent is lightened on dark backgrounds, otherwise it closes into the background.

## Which file for which use

**SVG, preferred wherever possible**

| File | Use |
|---|---|
| `svg/atlas-symbol.svg` | two-colour symbol, light background. The reference file. |
| `svg/atlas-symbol-dark.svg` | two-colour symbol, dark background |
| `svg/atlas-symbol-mono-noir.svg` | single ink (black), for stamps, engraving, one-colour print |
| `svg/atlas-symbol-mono-brique.svg` | single ink, coloured version |
| `svg/atlas-symbol-currentcolor.svg` | inherits the surrounding CSS text colour (`color`) |
| `svg/atlas-symbol-variables.svg` | driven by `--atlas-ink` and `--atlas-accent` |
| `svg/atlas-logotype.svg` | symbol + the word "Atlas", light background, `viewBox` 314.1 × 100 |
| `svg/atlas-logotype-dark.svg` | same, dark background |

**PNG**, transparent background: `png/atlas-symbol-{16,32,64,128,256,512}.png`,
`png/atlas-symbol-dark-512.png`, `png/atlas-symbol-mono-noir-512.png`,
`png/atlas-logotype-1600.png` and its dark version.

The 16 and 32 px files are provided for checking only. For a favicon, serving the SVG
and letting the browser rasterise it gives a better result.

## Geometry, if the drawing must be redone

Drawn in a 100-unit square, exported with a `viewBox` of `5.6 2.6 92.8 92.8`.

- Arc: ellipse with semi-axes 34 and 24 centred at (52, 38), drawn from (22.6, 50)
  to (65.6, 60) through the top. Stroke width 10, round caps.
- Disc: centre (33, 76), radius 13.

The right branch ends ten units lower than the left one: that is the point of this
variant, do not equalise it.

## Typography

The logotype is set in **Inter SemiBold** (SIL Open Font License), outlined in the
SVG: no font is needed to display it. To reset the word in another font, the cap
height is 56 units for a 100-unit symbol, and the gap between the symbol and the
word is 26 units.

## Clear space and minimum size

Keep a free margin around the symbol equal to the disc radius, 13 units out of 100.
Recommended minimum size: 20 px for the symbol alone, 100 px wide for the logotype.

## Known caveat

At small sizes and in black, the silhouette can suggest a question mark. If that
reading is a problem, two fixes exist: reduce the height difference between the two
branches, or move the disc slightly further left.
