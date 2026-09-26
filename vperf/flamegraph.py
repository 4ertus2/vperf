"""Render folded stacks as an interactive SVG flame graph (no dependencies)."""

from __future__ import annotations

import html
from dataclasses import dataclass, field


@dataclass
class _Node:
    name: str
    value: int = 0
    self_value: int = 0
    children: dict[str, _Node] = field(default_factory=dict)


def _insert(root: _Node, chain: list[str], w: int) -> None:
    node = root
    node.value += w
    for name in chain[:-1]:
        node.children.setdefault(name, _Node(name))
        node = node.children[name]
        node.value += w
    leaf = chain[-1]
    node.children.setdefault(leaf, _Node(leaf))
    leaf_node = node.children[leaf]
    leaf_node.value += w
    leaf_node.self_value += w


def build_tree(folded: dict[str, int]) -> _Node:
    root = _Node("root")
    for key, w in folded.items():
        _insert(root, key.split(";"), w)
    return root


def _color(name: str) -> str:
    h = 0
    for ch in name.encode("utf-8", errors="replace"):
        h = (h * 31 + ch) & 0xFFFFFFFF
    r = 205 + (h % 50)
    g = 90 + ((h >> 3) % 110)
    b = 30 + ((h >> 6) % 60)
    return f"rgb({r},{g},{b})"


def _esc(s: str) -> str:
    return html.escape(s, quote=True)


# Layout constants. The ones the browser needs for click-to-zoom are emitted as
# data-* attributes on the <svg> so the client never re-guesses the geometry.
FRAME_GAP = 0.5        # px kept between neighbouring frames
LABEL_MIN_W = 28.0     # frames narrower than this get no text label
CHAR_W = 0.62          # approximate glyph width as a fraction of font-size


def _frame_text(name: str, w: float, font_size: int) -> str:
    """Frame label truncated to fit `w` px, or '' when the frame is too narrow."""
    if w <= LABEL_MIN_W:
        return ""
    max_chars = int(w / (font_size * CHAR_W)) - 2
    if max_chars < 1:
        return ""
    if max_chars < len(name):
        return name[: max(max_chars - 1, 1)] + "…"
    return name


def render_flame_svg(
    folded: dict[str, int],
    title: str = "",
    width: int = 1160,
    row_height: int = 17,
    font_size: int = 11,
) -> tuple[str, int]:
    """Return (svg, height). Flame grows bottom-up (roots at the bottom).

    Every frame is emitted as `<g class="fg">` carrying its name, weight, depth
    and x-extent in data attributes. `report_html` re-lays those out in the
    browser so a click can zoom into a branch and a "Reset Zoom" link can undo
    it, mirroring what `flamegraph.pl` does with a plain PNG.
    """
    root = build_tree(folded)
    total = max(root.value, 1)

    # Collect rows top-down for layout, then flip when rendering.  Iterative:
    # a target with a broken frame-pointer chain can produce chains thousands
    # of frames deep, which would blow the interpreter's recursion limit.
    levels: list[list[tuple[_Node, float]]] = []
    pending: list[tuple[_Node, float, int]] = [(root, 0.0, 0)]
    while pending:
        node, x0, depth = pending.pop()
        if len(levels) <= depth:
            levels.append([])
        levels[depth].append((node, x0))
        cx = x0
        children = sorted(node.children.values(), key=lambda c: -c.value)
        for child in reversed(children):
            pending.append((child, cx, depth + 1))
            cx += child.value / total * width
    height = len(levels) * row_height + (22 if title else 8)

    out = [
        (f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
         f'viewBox="0 0 {width} {height}" font-family="Verdana,sans-serif" font-size="{font_size}" '
         f'data-row="{row_height}" data-font="{font_size}" data-gap="{FRAME_GAP}" '
         f'data-lmin="{LABEL_MIN_W}" data-cw="{CHAR_W}">')
    ]
    if title:
        out.append(
            f'<text class="ftitle" x="4" y="14" fill="#ccc">{_esc(title)}</text>'
        )

    def pct(v: int) -> float:
        return v / total * 100.0

    for li, level in enumerate(levels):
        y = height - (li + 1) * row_height
        for node, x0 in level:
            w = node.value / total * width
            if w <= 0.01:
                continue  # below a hundredth of a pixel: nothing to show or click
            label = f"{node.name} ({pct(node.value):.1f}%, {node.value:,})"
            out.append(
                f'<g class="fg" data-n="{_esc(node.name)}" data-v="{node.value}" '
                f'data-d="{li}" data-y="{y}" data-x="{x0:.4f}" data-w="{w:.4f}">'
                f'<title>{_esc(label)}</title>'
                f'<rect x="{x0:.2f}" y="{y}" width="{max(w - FRAME_GAP, FRAME_GAP):.2f}" '
                f'height="{row_height - 2}" rx="1" fill="{_color(node.name)}"/>'
            )
            text = _frame_text(node.name, w, font_size)
            if text:
                out.append(
                    f'<text x="{x0 + 2:.2f}" y="{y + row_height - 5}" fill="#111">{_esc(text)}</text>'
                )
            out.append("</g>")

    # Overlay: the greyed call path plus the "Reset Zoom" link, both revealed by
    # flameRender() once the user zooms into a branch.
    out.append(
        '<g class="fovl" style="display:none">'
        '<g class="fctx"></g>'
        f'<text class="freset" x="{width - 6}" y="14" text-anchor="end" fill="#409cff">Reset Zoom</text>'
        f'<rect class="fhit" x="{width - 100}" y="0" width="100" height="22" fill="none"/>'
        '</g>')
    out.append("</svg>")
    return "".join(out), height
