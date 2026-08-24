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


def render_flame_svg(
    folded: dict[str, int],
    title: str = "",
    width: int = 1160,
    row_height: int = 17,
    font_size: int = 11,
) -> tuple[str, int]:
    """Return (svg, height). Flame grows bottom-up (roots at the bottom)."""
    root = build_tree(folded)
    total = max(root.value, 1)

    # collect rows top-down for layout, then flip when rendering
    levels: list[list[tuple[_Node, float]]] = []

    def walk(node: _Node, x0: float, depth: int) -> None:
        if len(levels) <= depth:
            levels.append([])
        node.value / total
        levels[depth].append((node, x0))
        cx = x0
        for child in sorted(node.children.values(), key=lambda c: -c.value):
            walk(child, cx, depth + 1)
            cx += child.value / total * width

    walk(root, 0.0, 0)
    height = len(levels) * row_height + (22 if title else 8)

    out = [
        (f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" font-family="Verdana,sans-serif" font-size="{font_size}">')
    ]
    if title:
        out.append(
            f'<text x="4" y="14" fill="#ccc">{_esc(title)}</text>'
        )

    def pct(v: int) -> float:
        return v / total * 100.0

    for li, level in enumerate(levels):
        y = height - (li + 1) * row_height
        for node, x0 in level:
            w = node.value / total * width
            if w < 0.15:
                continue
            label = f"{node.name} ({pct(node.value):.1f}%, {node.value:,})"
            out.append(
                f'<g><title>{_esc(label)}</title>'
                f'<rect x="{x0:.2f}" y="{y}" width="{max(w - 0.5, 0.5):.2f}" '
                f'height="{row_height - 2}" rx="1" fill="{_color(node.name)}"/>'
            )
            if w > 28:
                text = node.name
                max_chars = int(w / (font_size * 0.62)) - 2
                if max_chars < len(text):
                    text = text[: max(max_chars - 1, 1)] + "…"
                if max_chars >= 1:
                    out.append(
                        f'<text x="{x0 + 2:.2f}" y="{y + row_height - 5}" fill="#111">{_esc(text)}</text>'
                    )
            out.append("</g>")
    out.append("</svg>")
    return "".join(out), height
