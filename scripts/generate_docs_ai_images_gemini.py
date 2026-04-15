#!/usr/bin/env python3
from __future__ import annotations

import base64
import json
import os
import sys
import urllib.parse
import urllib.request
import urllib.error
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps


ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT / "docs" / "images"
MODEL = os.getenv("GEMINI_IMAGE_MODEL", "gemini-3-pro-image-preview")
FALLBACK_MODEL = os.getenv("GEMINI_IMAGE_FALLBACK_MODEL", "gemini-3.1-flash-image-preview")
API_KEY = os.getenv("GEMINI_API_KEY", "")
BASE_URL = os.getenv("GEMINI_IMAGE_BASE_URL", "")

TARGET_SIZE = (3840, 2160)
SAFE_TIMEOUT = int(os.getenv("GEMINI_IMAGE_TIMEOUT_SEC", "180"))

CN_FONT = "/System/Library/Fonts/Hiragino Sans GB.ttc"
EN_FONT = "/System/Library/Fonts/Helvetica.ttc"
SERIF_FONT = "/System/Library/Fonts/Times.ttc"


def _load_font(path: str, size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(path, size=size)
    except Exception:
        return ImageFont.load_default()


PALETTE = {
    "paper": "#f5ecdf",
    "paper_soft": "#fbf6ee",
    "ink": "#38291b",
    "muted": "#6f5a44",
    "accent": "#c59b57",
    "accent_deep": "#8a6738",
    "card": (255, 250, 243, 228),
    "card_border": (194, 155, 87, 110),
}


PROMPTS = {
    "profiles": {
        "filename": "profile_ladder_bilingual_4k.png",
        "prompt": (
            "Create a premium 16:9 infographic-style illustration with no embedded text. Style: warm parchment, soft glass, refined gold, "
            "archive drawers and elegant cards. The composition should communicate a progression ladder from low-capability to high-capability deployment modes. "
            "No neon, no purple, no blue corporate SaaS look, no dark mode."
        ),
        "title_cn": "Profile 能力阶梯",
        "title_en": "Profile Capability Ladder",
        "subtitle_cn": "B 默认起步，C 推荐目标，D 全量高级面",
        "subtitle_en": "B Safe Bootstrap, C Recommended Target, D Full Advanced Suite",
        "chips": [
            ("A 最保守", "A Most Conservative"),
            ("B 默认起步", "B Safe Bootstrap"),
            ("C 推荐目标", "C Recommended Target"),
            ("D 全量高级面", "D Full Advanced Suite"),
        ],
    },
    "architecture": {
        "filename": "runtime_architecture_bilingual_4k.png",
        "prompt": (
            "Create a premium 16:9 architecture illustration with no embedded text. Style: warm parchment, soft glass, restrained gold, "
            "museum archive atmosphere, and elegant information design. The composition should communicate the real runtime architecture "
            "of the current Memory Palace for OpenClaw project: OpenClaw host surfaces, the memory-palace plugin as the primary entry, "
            "bundled onboarding tools, diagnostics, ACL/profile-memory logic, a shared FastAPI plus MCP backend, SQLite with indexing and snapshots, "
            "a shared dashboard runtime, and a secondary direct MCP/skill lane. No neon, no purple, no dark mode, no cyberpunk."
        ),
        "title_cn": "当前真实运行架构",
        "title_en": "Current Runtime Architecture",
        "subtitle_cn": "OpenClaw 插件主线 · 共享运行时 · 辅助 MCP 路线",
        "subtitle_en": "Plugin-First Host Path · Shared Runtime · Auxiliary MCP Lane",
        "chips": [
            ("宿主插件主线", "Plugin-First Host Path"),
            ("共享运行时", "Shared Runtime"),
            ("SQLite 与索引", "SQLite and Indexing"),
            ("辅路线 MCP", "Auxiliary MCP Path"),
        ],
    },
    "onboarding": {
        "filename": "openclaw_dialog_install_bilingual_4k.png",
        "prompt": (
            "Create a premium 16:9 onboarding-flow illustration with no embedded text. Style: warm ivory parchment, restrained gold, "
            "soft glass cards, editorial archive mood, and a clear product-guidance composition. The scene should show a conversational install "
            "journey inside OpenClaw: check whether the plugin is installed, reuse existing provider settings if present, guide the user to "
            "Profile B/C/D in chat, then probe, apply, and verify. Profile B should feel like the safe zero-config start, Profile C like "
            "embedding plus reranker with optional LLM assists, and Profile D like the full advanced suite. No dashboard-first composition, "
            "no neon, no purple, no dark mode."
        ),
        "title_cn": "OpenClaw 对话式安装",
        "title_en": "OpenClaw Conversational Install",
        "subtitle_cn": "先检查插件 · 复用已有配置 · Probe → Apply → Verify",
        "subtitle_en": "Check Plugin First · Reuse Existing Providers · Probe → Apply → Verify",
        "chips": [
            ("先检查插件", "Check Plugin First"),
            ("复用现有配置", "Reuse Existing Providers"),
            ("B 默认起步", "B Safe Bootstrap"),
            ("D 全功能高级面", "D Full Advanced Suite"),
        ],
    },
}


def build_request(prompt: str) -> bytes:
    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": prompt}],
            }
        ],
        "generationConfig": {
            "responseModalities": ["IMAGE"],
            "imageConfig": {"aspectRatio": "16:9"},
        },
    }
    return json.dumps(payload).encode("utf-8")


def call_gemini_image(prompt: str) -> dict:
    if not API_KEY:
        raise SystemExit("Missing GEMINI_API_KEY")
    candidate_models = [MODEL]
    if FALLBACK_MODEL and FALLBACK_MODEL not in candidate_models:
        candidate_models.append(FALLBACK_MODEL)

    last_error: Exception | None = None
    for model_name in candidate_models:
        url = BASE_URL or f"https://aiplatform.googleapis.com/v1/publishers/google/models/{model_name}:generateContent"
        url = f"{url}?key={urllib.parse.quote(API_KEY)}"
        request = urllib.request.Request(
            url,
            data=build_request(prompt),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=SAFE_TIMEOUT) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code == 429 and model_name != candidate_models[-1]:
                continue
            raise
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            raise

    if last_error:
        raise last_error
    raise RuntimeError("No model candidates were attempted")


def extract_image_bytes(payload: dict) -> bytes:
    candidates = payload.get("candidates") or []
    for candidate in candidates:
        content = candidate.get("content") or {}
        for part in content.get("parts") or []:
            inline = part.get("inlineData") or part.get("inline_data") or {}
            data = inline.get("data")
            if data:
                return base64.b64decode(data)
    raise RuntimeError(f"No inline image data found: {json.dumps(payload)[:1200]}")


def fit_4k(image: Image.Image) -> Image.Image:
    fitted = ImageOps.fit(image.convert("RGB"), TARGET_SIZE, method=Image.Resampling.LANCZOS)
    return fitted


def draw_card(draw: ImageDraw.ImageDraw, xy, radius: int = 38) -> None:
    draw.rounded_rectangle(xy, radius=radius, fill=PALETTE["card"], outline=PALETTE["card_border"], width=3)


def draw_chip(draw: ImageDraw.ImageDraw, x: int, y: int, cn: str, en: str) -> int:
    cn_font = _load_font(CN_FONT, 42)
    en_font = _load_font(EN_FONT, 24)
    pad_x, pad_y = 30, 20
    cn_bbox = draw.textbbox((0, 0), cn, font=cn_font)
    en_bbox = draw.textbbox((0, 0), en, font=en_font)
    w = max(cn_bbox[2], en_bbox[2]) + pad_x * 2
    h = (cn_bbox[3] - cn_bbox[1]) + (en_bbox[3] - en_bbox[1]) + pad_y * 2 + 12
    draw.rounded_rectangle((x, y, x + w, y + h), radius=26, fill=(255, 251, 245, 230), outline=PALETTE["card_border"], width=2)
    draw.text((x + pad_x, y + 16), cn, font=cn_font, fill=PALETTE["ink"])
    draw.text((x + pad_x, y + 16 + (cn_bbox[3] - cn_bbox[1]) + 10), en, font=en_font, fill=PALETTE["muted"])
    return w


def compose_labelled_image(name: str, image_bytes: bytes) -> Path:
    spec = PROMPTS[name]
    base = fit_4k(Image.open(BytesIO(image_bytes)))
    overlay = Image.new("RGBA", TARGET_SIZE, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    title_cn_font = _load_font(CN_FONT, 84)
    title_en_font = _load_font(SERIF_FONT, 52)
    subtitle_cn_font = _load_font(CN_FONT, 40)
    subtitle_en_font = _load_font(EN_FONT, 26)

    draw.rounded_rectangle((84, 86, 2210, 520), radius=54, fill=(250, 244, 235, 218), outline=PALETTE["card_border"], width=3)
    draw.text((150, 140), spec["title_cn"], font=title_cn_font, fill=PALETTE["ink"])
    draw.text((150, 252), spec["title_en"], font=title_en_font, fill=PALETTE["accent_deep"])
    draw.text((150, 346), spec["subtitle_cn"], font=subtitle_cn_font, fill=PALETTE["muted"])
    draw.text((150, 412), spec["subtitle_en"], font=subtitle_en_font, fill=PALETTE["muted"])

    if "chips" in spec:
        x = 118
        y = 1760
        for cn, en in spec["chips"]:
            w = draw_chip(draw, x, y, cn, en)
            x += w + 24

    if "metrics" in spec:
        metric_cn_font = _load_font(CN_FONT, 44)
        metric_en_font = _load_font(EN_FONT, 24)
        value_font = _load_font(SERIF_FONT, 46)
        x, y = 150, 1500
        for cn, en, value in spec["metrics"]:
            draw.rounded_rectangle((x, y, x + 790, y + 300), radius=34, fill=(255, 251, 244, 228), outline=PALETTE["card_border"], width=2)
            draw.text((x + 30, y + 28), cn, font=metric_cn_font, fill=PALETTE["ink"])
            draw.text((x + 30, y + 92), en, font=metric_en_font, fill=PALETTE["muted"])
            draw.text((x + 30, y + 176), value, font=value_font, fill=PALETTE["accent_deep"])
            x += 860
            if x + 790 > 3700:
                x = 150
                y += 340

    combined = Image.alpha_composite(base.convert("RGBA"), overlay)
    combined = combined.filter(ImageFilter.UnsharpMask(radius=1.6, percent=130, threshold=3))
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    output_path = OUTPUT_DIR / spec["filename"]
    combined.save(output_path, "PNG")
    return output_path


def generate_one(name: str) -> Path:
    if name not in PROMPTS:
        raise SystemExit(f"Unknown prompt key: {name}")
    payload = call_gemini_image(PROMPTS[name]["prompt"])
    image_bytes = extract_image_bytes(payload)
    return compose_labelled_image(name, image_bytes)


def main() -> None:
    target = sys.argv[1] if len(sys.argv) > 1 else "all"
    names = list(PROMPTS) if target == "all" else [target]
    outputs = [str(generate_one(name)) for name in names]
    print(json.dumps({"generated": outputs}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
