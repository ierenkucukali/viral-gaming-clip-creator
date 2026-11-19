"""Instagram-style headline (local gaming fork): a white rounded box with black bold text, an
optional colour emoji, on screen for the whole clip.

The box is drawn here with Pillow as a transparent PNG and laid over the picture by the
renderer (clips.py), because libass can't draw colour emoji or rounded boxes. On the split
layout the box's vertical centre sits exactly on the line between the streamer and the game
panel; otherwise its top sits where the hook text would be (clips.banner_y).

Sizes are for the 1080 px wide canvas and scale with Geometry.scale. The live preview
(frontend/src/components/VerticalPreview.tsx, headlineBox) uses the same numbers: change both.
"""

from __future__ import annotations

import functools
import platform
import re
from dataclasses import dataclass
from pathlib import Path

FONT_PX = 54  # at 1080 px wide
LINE_H = 1.22  # line height, x font size
PAD_X = 0.45  # x font size
PAD_Y = 0.30
RADIUS = 0.30  # x line height
MAX_W = 0.88  # of the canvas width
MAX_LINES = 2
MIN_SHRINK = 0.6
EMOJI_PX = 109  # the only size Noto Color Emoji (CBDT) has
EMOJI_SCALE = 1.08  # emoji height x font size

# Emoji and their joiners: pictographs, symbols, flags, skin tones, ZWJ, variation selectors, keycaps.
_EMOJI_RE = re.compile(
    "(?:[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U00002B00-\U00002BFF\U00002300-\U000023FF"
    "\U0001F1E6-\U0001F1FF\U0000200D\U0000FE0F\U000020E3\U00002190-\U000021FF\U00003030\U0000303D"
    "\U00003297\U00003299\U000000A9\U000000AE\U00002122])+")


def _font_files(system: str) -> list[str]:
    return {
        "Windows": [r"C:\Windows\Fonts\arialbd.ttf", r"C:\Windows\Fonts\segoeuib.ttf"],
        "Darwin": ["/System/Library/Fonts/Supplemental/Arial Bold.ttf", "/Library/Fonts/Arial Bold.ttf",
                   "/System/Library/Fonts/Helvetica.ttc"],
    }.get(system, ["/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
                   "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
                   "/usr/share/fonts/truetype/roboto/unhinted/RobotoTTF/Roboto-Bold.ttf",
                   "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"])


def _emoji_files(system: str) -> list[str]:
    return {
        "Windows": [r"C:\Windows\Fonts\seguiemj.ttf"],
        "Darwin": ["/System/Library/Fonts/Apple Color Emoji.ttc"],
    }.get(system, ["/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",
                   "/usr/share/fonts/noto/NotoColorEmoji.ttf",
                   "/usr/share/fonts/google-noto-emoji/NotoColorEmoji.ttf"])


def font_file() -> str | None:
    return next((f for f in _font_files(platform.system()) if Path(f).is_file()), None)


def emoji_file() -> str | None:
    return next((f for f in _emoji_files(platform.system()) if Path(f).is_file()), None)


@functools.lru_cache(maxsize=32)
def _font(size: int):
    from PIL import ImageFont

    f = font_file()
    return ImageFont.truetype(f, size) if f else ImageFont.load_default(size)


@functools.lru_cache(maxsize=2)
def _emoji_font():
    from PIL import ImageFont

    f = emoji_file()
    if not f:
        return None
    for size in (EMOJI_PX, 96, 64, 160):  # CBDT fonts open only at their bitmap size
        try:
            kw = {"layout_engine": ImageFont.Layout.RAQM} if _has_raqm() else {}
            return ImageFont.truetype(f, size, **kw)
        except OSError:
            continue
    return None


@functools.lru_cache(maxsize=1)
def _has_raqm() -> bool:
    try:
        from PIL import features

        return bool(features.check("raqm"))
    except Exception:  # noqa: BLE001
        return False


def runs(text: str) -> list[tuple[bool, str]]:
    """[(is_emoji, piece)] in order."""
    out: list[tuple[bool, str]] = []
    pos = 0
    for m in _EMOJI_RE.finditer(text):
        if m.start() > pos:
            out.append((False, text[pos:m.start()]))
        piece = m.group(0)
        if piece.strip("\u200d\ufe0f\u20e3"):  # a lone joiner / selector is not an emoji
            out.append((True, piece))
        pos = m.end()
    if pos < len(text):
        out.append((False, text[pos:]))
    return out


def clean(text: str, emoji: bool = True) -> str:
    """One line of headline text: spaces collapsed; emoji dropped when there is no emoji font."""
    if not emoji:
        text = "".join(p for e, p in runs(text) if not e)
    return " ".join(str(text or "").split())


@dataclass
class Layout:
    lines: list[str]
    font_px: int
    line_h: int
    pad_x: int
    pad_y: int
    radius: int
    width: int  # box size
    height: int


def _emoji_w(piece: str, size: int) -> float:
    ef = _emoji_font()
    if ef is None:
        return 0.0
    w = ef.getlength(piece)
    return w * (size * EMOJI_SCALE) / ef.size


def text_w(line: str, size: int) -> float:
    f = _font(size)
    return sum(_emoji_w(p, size) if e else f.getlength(p) for e, p in runs(line))


def _wrap(words: list[str], size: int, max_text_w: float) -> list[str] | None:
    lines: list[str] = []
    cur = ""
    for w in words:
        test = f"{cur} {w}" if cur else w
        if cur and text_w(test, size) > max_text_w:
            lines.append(cur)
            cur = w
        else:
            cur = test
    if cur:
        lines.append(cur)
    if len(lines) > MAX_LINES or any(text_w(ln, size) > max_text_w for ln in lines):
        return None
    return lines


def layout(text: str, canvas_w: int, scale: float = 1.0) -> Layout | None:
    """Lines, font size and box size for the headline (None: no text)."""
    text = clean(text, emoji_file() is not None)
    if not text:
        return None
    words = text.split(" ")
    base = max(8, round(FONT_PX * scale))
    max_box = canvas_w * MAX_W
    size = base
    lines = None
    while size >= base * MIN_SHRINK:
        pad_x = round(size * PAD_X)
        lines = _wrap(words, size, max_box - 2 * pad_x)
        if lines:
            break
        size = int(size * 0.94)
    if not lines:  # still too long: hard-cut the second line
        size = max(8, round(base * MIN_SHRINK))
        pad_x = round(size * PAD_X)
        lines = _wrap(words, size, 10 ** 9) or [text]
        lines = lines[:MAX_LINES]
    pad_x, pad_y = round(size * PAD_X), round(size * PAD_Y)
    line_h = round(size * LINE_H)
    width = min(round(max(text_w(ln, size) for ln in lines)) + 2 * pad_x, round(max_box))
    height = line_h * len(lines) + 2 * pad_y
    return Layout(lines, size, line_h, pad_x, pad_y, round(RADIUS * line_h), width, height)


def draw(text: str, canvas_w: int, scale: float = 1.0):
    """The headline as an RGBA Pillow image (None: no text)."""
    from PIL import Image, ImageDraw

    lay = layout(text, canvas_w, scale)
    if lay is None:
        return None
    ss = 2  # supersample: smooth rounded corners
    img = Image.new("RGBA", (lay.width * ss, lay.height * ss), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, lay.width * ss - 1, lay.height * ss - 1), radius=lay.radius * ss,
                        fill=(255, 255, 255, 255))
    font = _font(lay.font_px * ss)
    ef = _emoji_font()
    for i, line in enumerate(lay.lines):
        lw = text_w(line, lay.font_px) * ss
        x = (lay.width * ss - lw) / 2
        mid = (lay.pad_y + i * lay.line_h + lay.line_h / 2) * ss  # the line's vertical centre
        for is_emoji, piece in runs(line):
            if is_emoji:
                if ef is None:
                    continue
                k = lay.font_px * EMOJI_SCALE * ss / ef.size
                l0, t0, r0, b0 = ef.getbbox(piece)
                glyph = Image.new("RGBA", (max(1, r0 - l0), max(1, b0 - t0)), (0, 0, 0, 0))
                ImageDraw.Draw(glyph).text((-l0, -t0), piece, font=ef, embedded_color=True)
                gw, gh = max(1, round(glyph.width * k)), max(1, round(glyph.height * k))
                glyph = glyph.resize((gw, gh), Image.LANCZOS)
                # the glyph's box centred on the line, at its own left bearing
                img.alpha_composite(glyph, (max(0, round(x + l0 * k)), max(0, round(mid - gh / 2))))
                x += ef.getlength(piece) * k
            else:
                # "lm" = left, middle: the text's vertical middle sits on the line centre
                d.text((x, mid), piece, font=font, fill=(0, 0, 0, 255), anchor="lm")
                x += font.getlength(piece)
    return img.resize((lay.width, lay.height), Image.LANCZOS)


def render_png(text: str, canvas_w: int, scale: float, dest: Path) -> tuple[int, int] | None:
    """Write the headline PNG (RGBA) to dest; returns its (width, height), None when empty."""
    img = draw(text, canvas_w, scale)
    if img is None:
        return None
    dest.parent.mkdir(parents=True, exist_ok=True)
    img.save(dest, "PNG")
    return img.width, img.height


def position(size: tuple[int, int], canvas_w: int, layout_name: str, seam: int | None, top_y: int) -> tuple[int, int]:
    """Top-left corner of the box: centred; on the split line (box centre = seam) for "split",
    else its top at top_y (the hook / banner position)."""
    w, h = size
    x = (canvas_w - w) // 2
    y = seam - h // 2 if layout_name == "split" and seam is not None else top_y
    return x, max(0, y)
