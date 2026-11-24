"""Device Replacement icon: a greyed-out old device, the new device in front, and a
swap arrow from one to the other. Light and dark variants, 256 and 512 px."""

import math
from pathlib import Path
import sys

import cairosvg

OUT = Path(sys.argv[1])


def power(cx, cy, r, color, w):
    # power symbol: open ring + vertical bar
    a0, a1 = math.radians(38), math.radians(322)  # gap at the top
    x0, y0 = cx + r * math.sin(a0), cy - r * math.cos(a0)
    x1, y1 = cx + r * math.sin(a1), cy - r * math.cos(a1)
    return (
        f'<path d="M{x0:.1f},{y0:.1f} A{r},{r} 0 1 1 {x1:.1f},{y1:.1f}" fill="none" '
        f'stroke="{color}" stroke-width="{w}" stroke-linecap="round"/>'
        f'<line x1="{cx}" y1="{cy - r - 6}" x2="{cx}" y2="{cy - r * 0.15:.1f}" '
        f'stroke="{color}" stroke-width="{w}" stroke-linecap="round"/>'
    )


def rrect(x, y, w, h, r):
    """Rounded rectangle as a path (counter-clockwise, to cut a hole)."""
    return (
        f"M{x + r},{y} A{r},{r} 0 0 0 {x},{y + r} V{y + h - r} "
        f"A{r},{r} 0 0 0 {x + r},{y + h} H{x + w - r} "
        f"A{r},{r} 0 0 0 {x + w},{y + h - r} V{y + r} "
        f"A{r},{r} 0 0 0 {x + w - r},{y} Z"
    )


def svg(dark: bool) -> str:
    old_fill = "#5B6B75" if dark else "#CFD8DC"
    old_mark = "#8FA3AE" if dark else "#90A4AE"
    new_fill = "#18BCF2" if dark else "#1E88E5"
    arrow = "#ECEFF1" if dark else "#263238"
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
  <defs>
    <!-- cut the old device away around the new one, so they stay apart at any size -->
    <clipPath id="gap">
      <path clip-rule="evenodd" d="M0,0 H512 V512 H0 Z {rrect(186, 194, 304, 304, 76)}"/>
    </clipPath>
  </defs>
  <!-- old device, behind -->
  <g clip-path="url(#gap)">
    <rect x="40" y="48" width="252" height="252" rx="56" fill="{old_fill}"/>
    {power(166, 174, 58, old_mark, 22)}
  </g>
  <!-- new device, in front -->
  <rect x="204" y="212" width="268" height="268" rx="60" fill="{new_fill}"/>
  {power(338, 346, 64, "#FFFFFF", 24)}
  <!-- swap arrow: from the old device over to the new one -->
  <path d="M318,92 C410,92 448,124 448,176" fill="none" stroke="{arrow}"
        stroke-width="30" stroke-linecap="round"/>
  <path d="M410,160 L448,206 L486,160 Z" fill="{arrow}" stroke="{arrow}"
        stroke-width="10" stroke-linejoin="round"/>
</svg>'''


for dark in (False, True):
    s = svg(dark)
    name = "dark_icon" if dark else "icon"
    (OUT / f"{name}.svg").write_text(s)
    cairosvg.svg2png(
        bytestring=s.encode(),
        write_to=str(OUT / f"{name}.png"),
        output_width=256,
        output_height=256,
    )
    cairosvg.svg2png(
        bytestring=s.encode(),
        write_to=str(OUT / f"{name}@2x.png"),
        output_width=512,
        output_height=512,
    )
