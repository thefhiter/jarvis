"""Generates the JARVIS app icon (an arc reactor) as a multi-size .ico.

Pillow only — no external assets. The icon is used for the window, the Windows
taskbar (via ``webview.start(icon=...)``) and the system-tray icon.
"""
from __future__ import annotations

import math
import os

from PIL import Image, ImageDraw, ImageFilter

CYAN = (90, 225, 255)
PALE = (190, 245, 255)


def render(size: int = 256) -> Image.Image:
    """Render a single arc-reactor icon at ``size`` px (transparent background)."""
    S = size
    c = S / 2
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))

    # ── soft outer glow (concentric rings, then blur) ──────────────
    glow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    for frac, alpha in ((0.46, 55), (0.40, 90), (0.34, 120)):
        r = S * frac
        gd.ellipse([c - r, c - r, c + r, c + r], outline=CYAN + (alpha,),
                   width=max(1, int(S * 0.03)))
    img.alpha_composite(glow.filter(ImageFilter.GaussianBlur(S * 0.022)))

    d = ImageDraw.Draw(img)
    # ── crisp housing rings ────────────────────────────────────────
    r1 = S * 0.40
    d.ellipse([c - r1, c - r1, c + r1, c + r1], outline=CYAN + (235,),
              width=max(1, int(S * 0.025)))
    r2 = S * 0.30
    d.ellipse([c - r2, c - r2, c + r2, c + r2], outline=PALE + (205,),
              width=max(1, int(S * 0.02)))

    # ── reactor coils (triangular ticks around the core) ───────────
    for k in range(8):
        ang = k * math.pi / 4 + math.pi / 8
        x1, y1 = c + math.cos(ang) * S * 0.235, c + math.sin(ang) * S * 0.235
        x2, y2 = c + math.cos(ang) * S * 0.29, c + math.sin(ang) * S * 0.29
        d.line([x1, y1, x2, y2], fill=CYAN + (220,), width=max(1, int(S * 0.022)))

    # ── glowing core ───────────────────────────────────────────────
    core = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    cd = ImageDraw.Draw(core)
    for frac, alpha in ((0.22, 110), (0.16, 175), (0.10, 255)):
        r = S * frac
        cd.ellipse([c - r, c - r, c + r, c + r], fill=(225, 250, 255, alpha))
    img.alpha_composite(core.filter(ImageFilter.GaussianBlur(S * 0.012)))
    return img


def ensure_icon(path: str) -> str:
    """Create the .ico at ``path`` if it does not already exist; return ``path``."""
    if os.path.exists(path):
        return path
    os.makedirs(os.path.dirname(path), exist_ok=True)
    sizes = [16, 24, 32, 48, 64, 128, 256]
    base = render(256)
    base.save(path, format="ICO", sizes=[(s, s) for s in sizes])
    return path


if __name__ == "__main__":
    from pathlib import Path
    out = Path(__file__).resolve().parent.parent / "assets" / "jarvis.ico"
    # force regenerate when run directly
    if out.exists():
        out.unlink()
    print("wrote", ensure_icon(str(out)))
