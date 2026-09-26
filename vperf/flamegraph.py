"""Render folded stacks as an interactive SVG flame graph (no dependencies)."""

from __future__ import annotations

import bisect
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
MIN_FRAME_W = 2.0      # a frame thinner than this is a hairline, not a flame
MAX_FLAME_DEPTH = 48   # rows of frames; anything below the last one is folded in


def _title_pad(title: str) -> int:
    """Height of the band above the topmost frame: the title, or a thin gap."""
    return 22 if title else 8


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


def _rows_below(node: _Node, depth: int, max_depth: int) -> int:
    """How many rows below `node` the depth cap drops.

    The deepest chain below the frame is what fixes how far the rows go, so a
    chain shorter than the room left below `depth` is not cut at all.
    """
    pending: list[tuple[_Node, int]] = [(node, 0)]
    best = 0
    while pending:
        cur, d = pending.pop()
        if d > best:
            best = d
        for child in cur.children.values():
            pending.append((child, d + 1))
    return max(0, best - (max_depth - 1 - depth))


def render_flame_svg(
    folded: dict[str, int],
    title: str = "",
    width: int = 1160,
    row_height: int = 17,
    font_size: int = 11,
    max_depth: int = MAX_FLAME_DEPTH,
) -> tuple[str, int]:
    """Return (svg, height). Flame grows bottom-up (roots at the bottom).

    Every frame is emitted as `<g class="fg">` carrying its name, weight, depth
    and x-extent in data attributes. `report_html` re-lays those out in the
    browser so a click can zoom into a branch, mirroring what `flamegraph.pl`
    does with a plain PNG. The way back out is a link in the panel footer, not
    a control drawn into the picture.

    A chain deeper than `max_depth` rows is folded into the last one, and so is
    everything below the last row with a frame wide enough to read: a broken
    frame-pointer chain hands us one stack thousands of frames deep, and drawn
    in full it is a hairline tower over the flame that matters. Both cut the
    rows off, the frames keep the width the cut rows gave them, and the frame
    they land on says in its tooltip how many rows it stands for.
    """
    root = build_tree(folded)
    total = max(root.value, 1)

    # Collect rows top-down for layout, then flip when rendering.  Iterative:
    # a target with a broken frame-pointer chain can produce chains thousands
    # of frames deep, which would blow the interpreter's recursion limit.
    levels: list[list[tuple[_Node, float, int]]] = []
    pending: list[tuple[_Node, float, int, int]] = [(root, 0.0, 0, 0)]
    while pending:
        node, x0, depth, folded = pending.pop()
        if len(levels) <= depth:
            levels.append([])
        levels[depth].append((node, x0, folded))
        if depth + 1 >= max_depth:
            continue  # last row: the rest of the chain lives in this frame
        cx = x0
        children = sorted(node.children.values(), key=lambda c: -c.value)
        for child in reversed(children):
            pending.append((child, cx, depth + 1, _rows_below(child, depth + 1, max_depth)))
            cx += child.value / total * width

    # The picture ends at the last row holding a frame worth reading.  The
    # root always spans the full width, so there is always one row to keep.
    last = 0
    for li, level in enumerate(levels):
        if any(node.value / total * width > MIN_FRAME_W for node, _x, _f in level):
            last = li

    # A frame in that last row only stands for the cut rows it still has frames
    # in; one whose branch ended above them gets no note.  The last row's frames
    # partition the width, so the owner of a cut frame is a binary search.
    starts = [x0 for _n, x0, _f in levels[last]]
    folded_rows = [0] * len(starts)
    for level in levels[last + 1:]:
        for node, x0, _f in level:
            owner = bisect.bisect_right(starts, x0 + 1e-6) - 1
            if owner >= 0:
                folded_rows[owner] += 1
    if len(levels) > last + 1:
        levels = levels[: last + 1]
    height = len(levels) * row_height + _title_pad(title)

    out = [
        (f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
         f'viewBox="0 0 {width} {height}" font-family="Verdana,sans-serif" font-size="{font_size}" '
         f'data-row="{row_height}" data-font="{font_size}" data-gap="{FRAME_GAP}" '
         f'data-lmin="{LABEL_MIN_W}" data-cw="{CHAR_W}" data-pad="{_title_pad(title)}">')
    ]
    if title:
        out.append(
            f'<text class="ftitle" x="4" y="14" fill="#ccc">{_esc(title)}</text>'
        )
    out.append('<g class="fbody">')

    def pct(v: int) -> float:
        return v / total * 100.0

    for li, level in enumerate(levels):
        y = height - (li + 1) * row_height
        for idx, (node, x0, capped) in enumerate(level):
            w = node.value / total * width
            if w <= 0.01:
                continue  # below a hundredth of a pixel: nothing to show or click
            folded = capped + (folded_rows[idx] if li == last else 0)
            note = f" +{folded} deeper rows folded in" if folded else ""
            folds = f' data-folds="{folded}" data-fold-note="{_esc(note)}"' if folded else ""
            label = f"{node.name} ({pct(node.value):.1f}%, {node.value:,})"
            out.append(
                f'<g class="fg" data-n="{_esc(node.name)}" data-v="{node.value}" '
                f'data-d="{li}" data-y="{y}" data-x="{x0:.4f}" data-w="{w:.4f}"{folds}>'
                f'<title>{_esc(label + note)}</title>'
                f'<rect x="{x0:.2f}" y="{y}" width="{max(w - FRAME_GAP, FRAME_GAP):.2f}" '
                f'height="{row_height - 2}" rx="1" fill="{_color(node.name)}"/>'
            )
            text = _frame_text(node.name, w, font_size)
            if text:
                out.append(
                    f'<text x="{x0 + 2:.2f}" y="{y + row_height - 5}" fill="#111">{_esc(text)}</text>'
                )
            out.append("</g>")

    # The greyed call path to the focused frame, revealed by flameRender().
    out.append(
        '<g class="fovl" style="display:none">'
        '<g class="fctx"></g>'
        '</g>')
    out.append("</g>")
    out.append("</svg>")
    return "".join(out), height
