#!/usr/bin/env python3
from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont


ROOT = Path(__file__).resolve().parents[1]
IMAGES_DIR = ROOT / "docs" / "images"
SIZE = (3840, 2160)

BG_TOP = "#fffcf8"
BG_BOTTOM = "#efe2cf"
PANEL_FILL = (255, 250, 242, 242)
CARD_FILL = (250, 242, 229, 238)
CARD_ALT = (248, 236, 218, 236)
INK = "#30261f"
MUTED = "#756452"
ACCENT = "#ae7b34"
ACCENT_DEEP = "#7f5a28"
LINE = "#d5b487"
GREEN = "#2f7d62"
BLUE = "#496ab5"
SHADOW = (96, 72, 36, 34)


FONT_CANDIDATES_CN = [
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/AppleSDGothicNeo.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
]
FONT_CANDIDATES_EN = [
    "/System/Library/Fonts/Helvetica.ttc",
    "/System/Library/Fonts/Avenir Next.ttc",
    "/System/Library/Fonts/SFNS.ttf",
]
FONT_CANDIDATES_SERIF = [
    "/System/Library/Fonts/Times.ttc",
    "/System/Library/Fonts/NewYork.ttf",
]


def resolve_font_path(candidates: list[str]) -> str | None:
    for candidate in candidates:
        if Path(candidate).is_file():
            return candidate
    return None


CN_FONT = resolve_font_path(FONT_CANDIDATES_CN)
EN_FONT = resolve_font_path(FONT_CANDIDATES_EN)
SERIF_FONT = resolve_font_path(FONT_CANDIDATES_SERIF)


def load_font(path: str | None, size: int) -> ImageFont.FreeTypeFont:
    if path:
        try:
            return ImageFont.truetype(path, size=size)
        except Exception:
            pass
    return ImageFont.load_default()


TITLE_CN = load_font(CN_FONT, 90)
TITLE_EN = load_font(EN_FONT, 42)
SUB_CN = load_font(CN_FONT, 36)
SECTION = load_font(CN_FONT, 44)
CARD_TITLE_CN = load_font(CN_FONT, 40)
CARD_TITLE_EN = load_font(EN_FONT, 24)
BODY_CN = load_font(CN_FONT, 28)
BODY_EN = load_font(EN_FONT, 24)
SMALL_EN = load_font(EN_FONT, 22)
VALUE_FONT = load_font(SERIF_FONT, 54)


def vertical_gradient(size: tuple[int, int], top: str, bottom: str) -> Image.Image:
    image = Image.new("RGB", size, top)
    draw = ImageDraw.Draw(image)
    top_rgb = tuple(int(top[i : i + 2], 16) for i in (1, 3, 5))
    bottom_rgb = tuple(int(bottom[i : i + 2], 16) for i in (1, 3, 5))
    for y in range(size[1]):
        ratio = y / max(1, size[1] - 1)
        color = tuple(int(top_rgb[i] * (1 - ratio) + bottom_rgb[i] * ratio) for i in range(3))
        draw.line((0, y, size[0], y), fill=color)
    return image.convert("RGBA")


def add_glow(draw: ImageDraw.ImageDraw, x: int, y: int, radius: int, color: tuple[int, int, int, int]) -> None:
    draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=color)


def rounded_panel(base: Image.Image, box: tuple[int, int, int, int], fill: tuple[int, int, int, int], radius: int = 44) -> None:
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    shadow = ImageDraw.Draw(overlay)
    x0, y0, x1, y1 = box
    shadow.rounded_rectangle((x0 + 10, y0 + 22, x1 + 10, y1 + 22), radius=radius, fill=SHADOW)
    overlay = overlay.filter(ImageFilter.GaussianBlur(14))
    base.alpha_composite(overlay)
    draw = ImageDraw.Draw(base)
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=LINE, width=3)


def draw_header(draw: ImageDraw.ImageDraw, title_cn: str, title_en: str, subtitle_cn: str) -> None:
    draw.text((180, 150), title_cn, font=TITLE_CN, fill=INK)
    draw.text((186, 262), title_en, font=TITLE_EN, fill=MUTED)
    draw.text((180, 340), subtitle_cn, font=SUB_CN, fill=ACCENT_DEEP)


def draw_wrapped_text(draw: ImageDraw.ImageDraw, text: str, box: tuple[int, int, int, int], font: ImageFont.FreeTypeFont, fill: str | tuple, line_spacing: int = 8) -> None:
    x0, y0, x1, _ = box
    max_width = x1 - x0
    lines: list[str] = []
    for paragraph in text.split("\n"):
        words = list(paragraph) if any("\u4e00" <= ch <= "\u9fff" for ch in paragraph) else paragraph.split(" ")
        current = ""
        for token in words:
            candidate = f"{current}{token}" if " " not in paragraph else (f"{current} {token}".strip())
            width = draw.textbbox((0, 0), candidate, font=font)[2]
            if width <= max_width or not current:
                current = candidate
            else:
                lines.append(current)
                current = token
        if current:
            lines.append(current)
    y = y0
    for line in lines:
        draw.text((x0, y), line, font=font, fill=fill)
        bbox = draw.textbbox((0, 0), line, font=font)
        y += (bbox[3] - bbox[1]) + line_spacing


def draw_card(
    canvas: Image.Image,
    box: tuple[int, int, int, int],
    title_cn: str,
    title_en: str,
    body_cn: str,
    body_en: str,
    *,
    fill: tuple[int, int, int, int] = CARD_FILL,
) -> None:
    rounded_panel(canvas, box, fill, radius=34)
    draw = ImageDraw.Draw(canvas)
    x0, y0, x1, _ = box
    draw.text((x0 + 38, y0 + 42), title_cn, font=CARD_TITLE_CN, fill=INK)
    draw.text((x0 + 42, y0 + 94), title_en, font=CARD_TITLE_EN, fill=MUTED)
    draw_wrapped_text(draw, body_cn, (x0 + 38, y0 + 158, x1 - 42, y0 + 280), BODY_CN, INK, line_spacing=10)
    draw_wrapped_text(draw, body_en, (x0 + 40, y0 + 246, x1 - 44, y0 + 360), BODY_EN, MUTED, line_spacing=8)


def draw_arrow(draw: ImageDraw.ImageDraw, start: tuple[int, int], end: tuple[int, int]) -> None:
    draw.line((start, end), fill=ACCENT, width=10)
    ex, ey = end
    draw.polygon([(ex, ey), (ex - 32, ey - 20), (ex - 32, ey + 20)], fill=ACCENT)


def save(image: Image.Image, name: str) -> None:
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    image.convert("RGB").save(IMAGES_DIR / name, "PNG", quality=96)


def base_canvas() -> Image.Image:
    canvas = vertical_gradient(SIZE, BG_TOP, BG_BOTTOM)
    draw = ImageDraw.Draw(canvas)
    add_glow(draw, 3460, 260, 280, (189, 155, 90, 150))
    add_glow(draw, 340, 1840, 220, (212, 175, 55, 40))
    add_glow(draw, 3560, 1820, 220, (212, 175, 55, 35))
    return canvas


def main() -> None:
    return


if __name__ == "__main__":
    main()
