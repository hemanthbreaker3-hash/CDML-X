from asyncio import create_subprocess_exec
from asyncio.subprocess import PIPE
from contextlib import suppress
import json
import math
import os
from os import path as ospath, walk
import random
import subprocess
import tempfile

from PIL import Image, ImageDraw, ImageEnhance, ImageFont
from aiofiles.os import path as aiopath, remove, makedirs
from aioshutil import move
import numpy as np

from ... import LOGGER, task_dict, task_dict_lock
from ...core.config_manager import BinConfig, Config
from ...core.cpu import ffmpeg_layout
from ..ext_utils.bot_lock import ff_lock
from ..ext_utils.bot_utils import sync_to_async
from ..ext_utils.files_utils import get_path_size, is_archive, is_archive_split
from ..ext_utils.media_utils import (
    FFMpeg,
    download_image_thumb,
    get_document_type,
    get_media_info,
    get_streams,
)
from ..mirror_leech_utils.status_utils.ffmpeg_status import FFmpegStatus

MIN_INTRO_DURATION = 1
MAX_INTRO_DURATION = 60
DEFAULT_INTRO_DURATION = 5

INTRO_ANIMATIONS = [
    "None",
    "Appear",
    "Fade",
    "Fly In",
    "Float In",
    "Split",
    "Wipe",
    "Shape",
    "Wheel",
    "Random Bars",
    "Grow & Turn",
    "Zoom",
    "Swivel",
    "Bounce",
]

INTRO_TRANSITIONS = [
    "None",
    "Cut",
    "Fade",
    "Push",
    "Wipe",
    "Split",
    "Reveal",
    "Random Bars",
    "Shape",
    "Uncover",
    "Cover",
    "Flash",
]

INTRO_POSITIONS = [
    "Top-Left",
    "Top-Center",
    "Top-Right",
    "Center-Left",
    "Center",
    "Center-Right",
    "Bottom-Left",
    "Bottom-Center",
    "Bottom-Right",
]

UNICODE_FONT_STYLES = {
    "normal": "Normal (As Typed)",
    "bold": "𝐁𝐨𝐥𝐝",
    "italic": "𝐼𝑡𝑎𝑙𝑖𝑐",
    "bold_italic": "𝑩𝒐𝒍𝒅 𝑰𝒕𝒂𝒍𝒊𝒄",
    "sans_bold": "𝗦𝗮𝗻𝘀 𝗕𝗼𝗹𝗱",
    "monospace": "𝙼𝚘𝚗𝚘𝚜𝚙𝚊𝚌𝚎",
    "script": "𝒮𝒸𝓇𝒾𝓅𝓉",
    "double_struck": "𝔻𝕠𝕦𝕓𝕝𝕖 𝕊𝕥𝕣𝕦𝕔𝕜",
    "small_caps": "Sᴍᴀʟʟ Cᴀᴘs",
    "circled": "Ⓒⓘⓡⓒⓛⓔⓓ",
}


def apply_unicode_font_style(text: str, style: str) -> str:
    """Transform ASCII alphanumeric characters into the selected Unicode font style while preserving all symbols/Unicode."""
    if not text or not style or style == "normal":
        return text

    small_caps_map = str.maketrans(
        "abcdefghijklmnopqrstuvwxyz",
        "ᴀʙᴄᴅᴇꜰɢʜɪᴊᴋʟᴍɴᴏᴘǫʀsᴛᴜᴠᴡxʏᴢ",
    )
    if style == "small_caps":
        return text.translate(small_caps_map)

    ranges = {
        "bold": (0x1D400, 0x1D41A, 0x1D7CE),
        "italic": (0x1D434, 0x1D44E, None),
        "bold_italic": (0x1D468, 0x1D482, None),
        "sans_bold": (0x1D5D4, 0x1D5EE, 0x1D7EC),
        "monospace": (0x1D670, 0x1D68A, 0x1D7F6),
        "script": (0x1D4D0, 0x1D4EA, None),
        "double_struck": (0x1D538, 0x1D552, 0x1D7D8),
    }
    if style == "circled":
        out = []
        for ch in text:
            o = ord(ch)
            if 65 <= o <= 90:
                out.append(chr(0x24B6 + (o - 65)))
            elif 97 <= o <= 122:
                out.append(chr(0x24D0 + (o - 97)))
            elif 49 <= o <= 57:
                out.append(chr(0x2460 + (o - 49)))
            elif o == 48:
                out.append(chr(0x24EA))
            else:
                out.append(ch)
        return "".join(out)

    if style not in ranges:
        return text

    up_base, low_base, dig_base = ranges[style]
    out = []
    for ch in text:
        o = ord(ch)
        if 65 <= o <= 90:
            out.append(chr(up_base + (o - 65)))
        elif 97 <= o <= 122:
            if style == "italic" and ch == "h":
                out.append("\u210e")
            else:
                out.append(chr(low_base + (o - 97)))
        elif dig_base is not None and 48 <= o <= 57:
            out.append(chr(dig_base + (o - 48)))
        else:
            out.append(ch)
    return "".join(out)


def validate_intro_duration(raw_value):
    """Validate that the Intro Subtitle duration is strictly an integer from 1 to 60 seconds."""
    if isinstance(raw_value, bool) or raw_value is None:
        return None
    s = str(raw_value).strip().lower()
    for suffix in ("seconds", "second", "secs", "sec", "s"):
        if s.endswith(suffix):
            s = s[: -len(suffix)].strip()
            break
    if not s or not s.lstrip("+-").isdigit():
        return None
    try:
        val = int(s)
    except ValueError:
        return None
    if val < MIN_INTRO_DURATION or val > MAX_INTRO_DURATION:
        return None
    return val


def normalize_animation_name(name):
    if not name:
        return "None"
    s = str(name).strip()
    for anim in INTRO_ANIMATIONS:
        if anim.casefold() == s.casefold():
            return anim
    return "None"


def normalize_transition_name(name):
    if not name:
        return "None"
    s = str(name).strip()
    for trans in INTRO_TRANSITIONS:
        if trans.casefold() == s.casefold():
            return trans
    return "None"


def normalize_position_name(pos, default="Bottom-Center"):
    if not pos:
        return default
    s = str(pos).strip()
    for p in INTRO_POSITIONS:
        if p.casefold() == s.casefold():
            return p
    return default


def _select_font_for_text(text: str, size: int):
    """Pick the best installed TrueType/OpenType font capable of rendering the Unicode text."""
    has_telugu = any(0x0C00 <= ord(c) <= 0x0C7F for c in text)
    has_tamil = any(0x0B80 <= ord(c) <= 0x0BFF for c in text)
    has_devanagari = any(0x0900 <= ord(c) <= 0x097F for c in text)
    has_malayalam = any(0x0D00 <= ord(c) <= 0x0D7F for c in text)
    has_kannada = any(0x0C80 <= ord(c) <= 0x0CFF for c in text)
    has_bengali = any(0x0980 <= ord(c) <= 0x09FF for c in text)
    has_arabic = any(0x0600 <= ord(c) <= 0x06FF for c in text)
    has_hebrew = any(0x0590 <= ord(c) <= 0x05FF for c in text)
    has_cjk = any(0x3000 <= ord(c) <= 0x9FFF or 0xAC00 <= ord(c) <= 0xD7AF for c in text)
    has_math_or_sym = any(ord(c) > 0x2000 for c in text)

    candidates = []
    if has_telugu:
        candidates += [
            "/usr/share/fonts/truetype/noto/NotoSansTelugu-Bold.ttf",
            "/usr/share/fonts/truetype/noto/NotoSansTelugu-Regular.ttf",
            "/usr/share/fonts/truetype/noto/NotoSerifTelugu-Bold.ttf",
            "/usr/share/fonts/truetype/noto/NotoSerifTelugu-Regular.ttf",
        ]
    if has_tamil:
        candidates += [
            "/usr/share/fonts/truetype/noto/NotoSansTamil-Bold.ttf",
            "/usr/share/fonts/truetype/noto/NotoSansTamil-Regular.ttf",
        ]
    if has_devanagari:
        candidates += [
            "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Bold.ttf",
            "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Regular.ttf",
        ]
    if has_malayalam:
        candidates += [
            "/usr/share/fonts/truetype/noto/NotoSansMalayalam-Bold.ttf",
            "/usr/share/fonts/truetype/noto/NotoSerifMalayalam-Bold.ttf",
            "/usr/share/fonts/truetype/noto/NotoSerifMalayalam-Regular.ttf",
        ]
    if has_kannada:
        candidates += [
            "/usr/share/fonts/truetype/noto/NotoSansKannada-Bold.ttf",
            "/usr/share/fonts/truetype/noto/NotoSansKannada-Regular.ttf",
        ]
    if has_bengali:
        candidates += [
            "/usr/share/fonts/truetype/noto/NotoSansBengali-Bold.ttf",
            "/usr/share/fonts/truetype/noto/NotoSansBengali-Regular.ttf",
        ]
    if has_arabic:
        candidates += [
            "/usr/share/fonts/truetype/noto/NotoNaskhArabic-Bold.ttf",
            "/usr/share/fonts/truetype/noto/NotoSansArabic-Bold.ttf",
        ]
    if has_hebrew:
        candidates += [
            "/usr/share/fonts/truetype/noto/NotoSerifHebrew-Bold.ttf",
            "/usr/share/fonts/truetype/noto/NotoSansHebrew-Bold.ttf",
        ]
    if has_cjk:
        candidates += [
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
            "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
        ]
    if has_math_or_sym:
        candidates += [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/noto/NotoSansMath-Regular.ttf",
            "/usr/share/fonts/truetype/noto/NotoSansSymbols2-Regular.ttf",
            "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
        ]

    candidates += [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for path in candidates:
        if ospath.exists(path):
            try:
                return ImageFont.truetype(path, size=size)
            except Exception:
                continue
    return ImageFont.load_default()


def _compute_anchor_xy(pos_name: str, canvas_w: int, canvas_h: int, item_w: int, item_h: int, margin_x: int = 36, margin_y: int = 36):
    pos_name = normalize_position_name(pos_name)
    if "Left" in pos_name:
        x = margin_x
    elif "Right" in pos_name:
        x = max(margin_x, canvas_w - item_w - margin_x)
    else:
        x = (canvas_w - item_w) // 2

    if "Top" in pos_name:
        y = margin_y
    elif "Bottom" in pos_name:
        y = max(margin_y, canvas_h - item_h - margin_y)
    else:
        y = (canvas_h - item_h) // 2

    return int(x), int(y)


def _render_text_element(text: str, canvas_w: int, canvas_h: int) -> Image.Image | None:
    if not text or not str(text).strip():
        return None
    text = str(text).strip()
    font_size = max(22, min(72, int(canvas_h * 0.052)))
    font = _select_font_for_text(text, font_size)

    # Wrap long lines to fit within 86% of canvas width
    max_text_w = int(canvas_w * 0.86)
    dummy = Image.new("RGBA", (16, 16), (0, 0, 0, 0))
    d_draw = ImageDraw.Draw(dummy)

    wrapped_lines = []
    for raw_line in text.splitlines():
        raw_line = raw_line.strip()
        if not raw_line:
            wrapped_lines.append("")
            continue
        words = raw_line.split(" ")
        cur = words[0]
        for w in words[1:]:
            test = f"{cur} {w}"
            bbox = d_draw.textbbox((0, 0), test, font=font)
            if (bbox[2] - bbox[0]) <= max_text_w:
                cur = test
            else:
                wrapped_lines.append(cur)
                cur = w
        wrapped_lines.append(cur)

    final_text = "\n".join(wrapped_lines)
    stroke_w = max(2, font_size // 14)
    bbox = d_draw.multiline_textbbox((0, 0), final_text, font=font, stroke_width=stroke_w, align="center", spacing=6)
    tw = max(1, int(round(bbox[2] - bbox[0])))
    th = max(1, int(round(bbox[3] - bbox[1])))

    pad_x = max(18, font_size // 2)
    pad_y = max(12, font_size // 3)
    box_w = min(canvas_w, tw + pad_x * 2)
    box_h = min(canvas_h, th + pad_y * 2)

    elem = Image.new("RGBA", (box_w, box_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(elem)

    # Semi-transparent rounded pill backdrop for crisp legibility
    radius = min(18, box_h // 3)
    draw.rounded_rectangle(
        [0, 0, box_w - 1, box_h - 1],
        radius=radius,
        fill=(12, 14, 20, 155),
        outline=(255, 255, 255, 55),
        width=1,
    )

    tx = int(round(pad_x - bbox[0]))
    ty = int(round(pad_y - bbox[1]))
    # Drop shadow
    draw.multiline_text(
        (tx + 2, ty + 2),
        final_text,
        font=font,
        fill=(0, 0, 0, 210),
        align="center",
        spacing=6,
    )
    # Crisp foreground text with dark stroke
    draw.multiline_text(
        (tx, ty),
        final_text,
        font=font,
        fill=(255, 255, 255, 255),
        stroke_width=stroke_w,
        stroke_fill=(0, 0, 0, 245),
        align="center",
        spacing=6,
    )
    return elem


def _render_image_element(image_path: str, canvas_w: int, canvas_h: int) -> Image.Image | None:
    if not image_path or not ospath.exists(image_path):
        return None
    try:
        with Image.open(image_path) as src:
            img = src.convert("RGBA")
            max_w = max(96, int(canvas_w * 0.24))
            max_h = max(96, int(canvas_h * 0.24))
            resample = Image.Resampling.LANCZOS if hasattr(Image, "Resampling") else Image.LANCZOS
            img.thumbnail((max_w, max_h), resample)
            return img.copy()
    except Exception as e:
        LOGGER.warning(f"Failed to load Intro Subtitle image {image_path}: {e}")
        return None


def _compose_static_elements(
    canvas_w: int,
    canvas_h: int,
    text_elem: Image.Image | None,
    text_pos: str,
    img_elem: Image.Image | None,
    img_pos: str,
) -> Image.Image:
    """Compose the full-resolution static RGBA overlay frame with Text and Image at their configured positions."""
    canvas = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
    margin_x = max(24, int(canvas_w * 0.035))
    margin_y = max(24, int(canvas_h * 0.045))

    img_box = None
    if img_elem is not None:
        ix, iy = _compute_anchor_xy(img_pos, canvas_w, canvas_h, img_elem.width, img_elem.height, margin_x, margin_y)
        canvas.alpha_composite(img_elem, (ix, iy))
        img_box = (ix, iy, ix + img_elem.width, iy + img_elem.height)

    if text_elem is not None:
        tx, ty = _compute_anchor_xy(text_pos, canvas_w, canvas_h, text_elem.width, text_elem.height, margin_x, margin_y)
        # If both Image and Text share the exact same position, stack Text cleanly below/above Image
        if img_box is not None and normalize_position_name(text_pos) == normalize_position_name(img_pos):
            if "Bottom" in normalize_position_name(text_pos):
                ty = max(margin_y, img_box[1] - text_elem.height - 12)
            else:
                ty = min(canvas_h - text_elem.height - margin_y, img_box[3] + 12)
        canvas.alpha_composite(text_elem, (tx, ty))

    return canvas


def _apply_powerpoint_animation_frame(base_canvas: Image.Image, anim_name: str, progress: float) -> Image.Image:
    """Render a single frame at normalized entrance progress in [0.0, 1.0] for any of the 14 PowerPoint animations."""
    anim = normalize_animation_name(anim_name)
    p = max(0.0, min(1.0, float(progress)))
    if anim in ("None", "Appear") or p >= 0.999:
        return base_canvas

    w, h = base_canvas.size
    out = Image.new("RGBA", (w, h), (0, 0, 0, 0))

    if anim == "Fade":
        alpha = base_canvas.getchannel("A").point(lambda a: int(a * p))
        res = base_canvas.copy()
        res.putalpha(alpha)
        return res

    if anim == "Fly In":
        # Ease-out fly in from bottom
        ease = 1.0 - (1.0 - p) ** 3
        dy = int((1.0 - ease) * (h * 0.45))
        out.alpha_composite(base_canvas, (0, dy))
        return out

    if anim == "Float In":
        ease = 1.0 - (1.0 - p) ** 2
        dy = int((1.0 - ease) * (h * 0.14))
        temp = base_canvas.copy()
        alpha = temp.getchannel("A").point(lambda a: int(a * ease))
        temp.putalpha(alpha)
        out.alpha_composite(temp, (0, dy))
        return out

    if anim == "Split":
        # Horizontal outward split from center
        half_w = int((w / 2.0) * p)
        cx = w // 2
        left = max(0, cx - half_w)
        right = min(w, cx + half_w)
        if right > left:
            cropped = base_canvas.crop((left, 0, right, h))
            out.alpha_composite(cropped, (left, 0))
        return out

    if anim == "Wipe":
        # Left-to-right wipe
        vis_w = max(1, int(w * p))
        cropped = base_canvas.crop((0, 0, vis_w, h))
        out.alpha_composite(cropped, (0, 0))
        return out

    if anim == "Shape":
        # Circular/elliptical iris expanding from center
        mask = Image.new("L", (w, h), 0)
        mdraw = ImageDraw.Draw(mask)
        max_rx = int(math.hypot(w / 2.0, h / 2.0) * p)
        max_ry = int(math.hypot(w / 2.0, h / 2.0) * p)
        cx, cy = w // 2, h // 2
        if max_rx > 0 and max_ry > 0:
            mdraw.ellipse([cx - max_rx, cy - max_ry, cx + max_rx, cy + max_ry], fill=255)
        res = base_canvas.copy()
        orig_a = np.array(res.getchannel("A"), dtype=np.uint16)
        mask_a = np.array(mask, dtype=np.uint16)
        combined = ((orig_a * mask_a) // 255).astype(np.uint8)
        res.putalpha(Image.fromarray(combined, mode="L"))
        return res

    if anim == "Wheel":
        # Clockwise radial pieslice sweep from top (PowerPoint 1-spoke Wheel)
        mask = Image.new("L", (w, h), 0)
        mdraw = ImageDraw.Draw(mask)
        r = int(math.hypot(w, h))
        cx, cy = w // 2, h // 2
        end_angle = -90.0 + 360.0 * p
        mdraw.pieslice([cx - r, cy - r, cx + r, cy + r], start=-90.0, end=end_angle, fill=255)
        res = base_canvas.copy()
        orig_a = np.array(res.getchannel("A"), dtype=np.uint16)
        mask_a = np.array(mask, dtype=np.uint16)
        combined = ((orig_a * mask_a) // 255).astype(np.uint8)
        res.putalpha(Image.fromarray(combined, mode="L"))
        return res

    if anim == "Random Bars":
        num_bars = 24
        rng = random.Random(42)
        order = list(range(num_bars))
        rng.shuffle(order)
        active = set(order[: max(1, int(round(num_bars * p)))])
        bar_h = max(1, h // num_bars)
        mask = Image.new("L", (w, h), 0)
        mdraw = ImageDraw.Draw(mask)
        for idx in active:
            y0 = idx * bar_h
            y1 = h if idx == num_bars - 1 else min(h, (idx + 1) * bar_h)
            mdraw.rectangle([0, y0, w, y1], fill=255)
        res = base_canvas.copy()
        orig_a = np.array(res.getchannel("A"), dtype=np.uint16)
        mask_a = np.array(mask, dtype=np.uint16)
        combined = ((orig_a * mask_a) // 255).astype(np.uint8)
        res.putalpha(Image.fromarray(combined, mode="L"))
        return res

    if anim == "Grow & Turn":
        scale = max(0.1, 0.2 + 0.8 * p)
        angle = (1.0 - p) * -90.0
        resample = Image.Resampling.BICUBIC if hasattr(Image, "Resampling") else Image.BICUBIC
        sw, sh = max(1, int(w * scale)), max(1, int(h * scale))
        scaled = base_canvas.resize((sw, sh), resample)
        rotated = scaled.rotate(angle, resample=resample, expand=False)
        alpha = rotated.getchannel("A").point(lambda a: int(a * p))
        rotated.putalpha(alpha)
        ox = (w - sw) // 2
        oy = (h - sh) // 2
        out.alpha_composite(rotated, (ox, oy))
        return out

    if anim == "Zoom":
        scale = max(0.08, p)
        resample = Image.Resampling.BICUBIC if hasattr(Image, "Resampling") else Image.BICUBIC
        sw, sh = max(1, int(w * scale)), max(1, int(h * scale))
        scaled = base_canvas.resize((sw, sh), resample)
        ox = (w - sw) // 2
        oy = (h - sh) // 2
        out.alpha_composite(scaled, (ox, oy))
        return out

    if anim == "Swivel":
        # Horizontal 3D swivel (width oscillates via |cos| while settling to 1.0)
        horiz_scale = max(0.06, abs(math.cos((1.0 - p) * math.pi * 1.5)) * (0.35 + 0.65 * p))
        resample = Image.Resampling.BICUBIC if hasattr(Image, "Resampling") else Image.BICUBIC
        sw = max(1, int(w * horiz_scale))
        scaled = base_canvas.resize((sw, h), resample)
        ox = (w - sw) // 2
        out.alpha_composite(scaled, (ox, 0))
        return out

    if anim == "Bounce":
        # Damped bounce curve
        decay = (1.0 - p) ** 2
        bounce_offset = int(-abs(math.cos(p * math.pi * 3.5)) * (h * 0.28) * decay)
        out.alpha_composite(base_canvas, (0, bounce_offset))
        return out

    return base_canvas


def _apply_powerpoint_transition_frame(base_canvas: Image.Image, trans_name: str, progress: float) -> Image.Image:
    """Render a single frame at normalized exit transition progress in [0.0, 1.0] for any of the 12 PowerPoint transitions."""
    trans = normalize_transition_name(trans_name)
    p = max(0.0, min(1.0, float(progress)))
    rem = 1.0 - p
    if trans in ("None", "Cut") or p <= 0.001:
        return base_canvas

    w, h = base_canvas.size
    out = Image.new("RGBA", (w, h), (0, 0, 0, 0))

    if trans == "Fade":
        alpha = base_canvas.getchannel("A").point(lambda a: int(a * rem))
        res = base_canvas.copy()
        res.putalpha(alpha)
        return res

    if trans == "Push":
        # Pushes upward off-screen
        dy = -int(p * h * 0.45)
        out.alpha_composite(base_canvas, (0, dy))
        return out

    if trans == "Wipe":
        vis_w = max(1, int(w * rem))
        cropped = base_canvas.crop((0, 0, vis_w, h))
        out.alpha_composite(cropped, (0, 0))
        return out

    if trans == "Split":
        half_w = int((w / 2.0) * rem)
        cx = w // 2
        left = max(0, cx - half_w)
        right = min(w, cx + half_w)
        if right > left:
            cropped = base_canvas.crop((left, 0, right, h))
            out.alpha_composite(cropped, (left, 0))
        return out

    if trans == "Reveal":
        # Smooth slide-right + dissolve reveal
        dx = int(p * (w * 0.18))
        res = base_canvas.copy()
        alpha = res.getchannel("A").point(lambda a: int(a * (rem ** 1.5)))
        res.putalpha(alpha)
        out.alpha_composite(res, (dx, 0))
        return out

    if trans == "Random Bars":
        num_bars = 24
        rng = random.Random(99)
        order = list(range(num_bars))
        rng.shuffle(order)
        active = set(order[: max(0, int(round(num_bars * rem)))])
        bar_h = max(1, h // num_bars)
        mask = Image.new("L", (w, h), 0)
        mdraw = ImageDraw.Draw(mask)
        for idx in active:
            y0 = idx * bar_h
            y1 = h if idx == num_bars - 1 else min(h, (idx + 1) * bar_h)
            mdraw.rectangle([0, y0, w, y1], fill=255)
        res = base_canvas.copy()
        orig_a = np.array(res.getchannel("A"), dtype=np.uint16)
        mask_a = np.array(mask, dtype=np.uint16)
        combined = ((orig_a * mask_a) // 255).astype(np.uint8)
        res.putalpha(Image.fromarray(combined, mode="L"))
        return res

    if trans == "Shape":
        mask = Image.new("L", (w, h), 0)
        mdraw = ImageDraw.Draw(mask)
        max_r = int(math.hypot(w / 2.0, h / 2.0) * rem)
        cx, cy = w // 2, h // 2
        if max_r > 0:
            mdraw.ellipse([cx - max_r, cy - max_r, cx + max_r, cy + max_r], fill=255)
        res = base_canvas.copy()
        orig_a = np.array(res.getchannel("A"), dtype=np.uint16)
        mask_a = np.array(mask, dtype=np.uint16)
        combined = ((orig_a * mask_a) // 255).astype(np.uint8)
        res.putalpha(Image.fromarray(combined, mode="L"))
        return res

    if trans == "Uncover":
        # Slides diagonally up-left to uncover video
        dx = -int(p * (w * 0.35))
        dy = -int(p * (h * 0.25))
        out.alpha_composite(base_canvas, (dx, dy))
        return out

    if trans == "Cover":
        # Slides down-right while fading out
        dx = int(p * (w * 0.35))
        dy = int(p * (h * 0.25))
        res = base_canvas.copy()
        alpha = res.getchannel("A").point(lambda a: int(a * rem))
        res.putalpha(alpha)
        out.alpha_composite(res, (dx, dy))
        return out

    if trans == "Flash":
        # Camera flash burst at start of transition followed by quick fade
        res = base_canvas.copy()
        if p < 0.35:
            enhancer = ImageEnhance.Brightness(res)
            res = enhancer.enhance(1.0 + (1.0 - p / 0.35) * 2.5)
        alpha = res.getchannel("A").point(lambda a: int(a * rem))
        res.putalpha(alpha)
        return res

    return base_canvas


def _build_intro_overlay_mov_sync(
    output_mov: str,
    canvas_w: int,
    canvas_h: int,
    duration: int,
    text: str,
    text_pos: str,
    image_path: str,
    img_pos: str,
    animation: str,
    transition: str,
) -> bool:
    """Generate a transparent RGBA QuickTime RLE (.mov) overlay clip of `duration` seconds."""
    text_elem = _render_text_element(text, canvas_w, canvas_h) if text else None
    img_elem = _render_image_element(image_path, canvas_w, canvas_h) if image_path else None
    if text_elem is None and img_elem is None:
        return False

    base_canvas = _compose_static_elements(
        canvas_w, canvas_h, text_elem, text_pos, img_elem, img_pos
    )
    static_bytes = base_canvas.tobytes()

    fps = 15
    total_frames = max(fps, int(duration) * fps)
    anim = normalize_animation_name(animation)
    trans = normalize_transition_name(transition)

    # Keep entrance and exit ranges disjoint, including very short (1–2s) intros.
    # Previously both effects could claim all frames on a 1-second intro, causing
    # the exit transition to be skipped completely.
    anim_frames = min(fps, max(1, total_frames // 3)) if anim not in ("None", "Appear") else 0
    trans_frames = min(fps, max(1, total_frames // 3)) if trans not in ("None", "Cut") else 0
    if anim_frames + trans_frames > total_frames:
        overflow = anim_frames + trans_frames - total_frames
        reduce_trans = min(overflow, trans_frames)
        trans_frames -= reduce_trans
        overflow -= reduce_trans
        anim_frames = max(0, anim_frames - overflow)

    cmd = [
        BinConfig.FFMPEG_NAME,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgba",
        "-s",
        f"{canvas_w}x{canvas_h}",
        "-r",
        str(fps),
        "-i",
        "pipe:0",
        "-c:v",
        "qtrle",
        output_mov,
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        for idx in range(total_frames):
            if idx < anim_frames:
                prog = (idx + 1) / float(anim_frames)
                frame_img = _apply_powerpoint_animation_frame(base_canvas, anim, prog)
                proc.stdin.write(frame_img.tobytes())
            elif idx >= total_frames - trans_frames and trans_frames > 0:
                t_idx = idx - (total_frames - trans_frames)
                prog = (t_idx + 1) / float(trans_frames)
                frame_img = _apply_powerpoint_transition_frame(base_canvas, trans, prog)
                proc.stdin.write(frame_img.tobytes())
            else:
                proc.stdin.write(static_bytes)
        proc.stdin.close()
        proc.wait(timeout=120)
        return proc.returncode == 0 and ospath.exists(output_mov) and ospath.getsize(output_mov) > 0
    except Exception as e:
        LOGGER.error(f"Failed generating Intro Subtitle overlay clip: {e}")
        with suppress(Exception):
            proc.kill()
        return False


def _build_intro_ass_subtitle(
    ass_path: str,
    canvas_w: int,
    canvas_h: int,
    duration: int,
    text: str,
    text_pos: str,
    animation: str,
    transition: str,
) -> bool:
    """Write an ASS subtitle file with position & animation override tags for the 0..duration intro window."""
    if not text or not str(text).strip():
        return False

    pos_align_map = {
        "Top-Left": (7, 40, 40),
        "Top-Center": (8, canvas_w // 2, 40),
        "Top-Right": (9, canvas_w - 40, 40),
        "Center-Left": (4, 40, canvas_h // 2),
        "Center": (5, canvas_w // 2, canvas_h // 2),
        "Center-Right": (6, canvas_w - 40, canvas_h // 2),
        "Bottom-Left": (1, 40, canvas_h - 45),
        "Bottom-Center": (2, canvas_w // 2, canvas_h - 45),
        "Bottom-Right": (3, canvas_w - 40, canvas_h - 45),
    }
    an, px, py = pos_align_map.get(normalize_position_name(text_pos), (2, canvas_w // 2, canvas_h - 45))

    anim = normalize_animation_name(animation)
    trans = normalize_transition_name(transition)

    fade_in_ms = 650 if anim in ("Fade", "Float In", "Zoom", "Grow & Turn") else 0
    fade_out_ms = 650 if trans in ("Fade", "Reveal", "Cover", "Flash") else 0

    tags = [f"\\an{an}"]
    if anim in ("Fly In", "Float In", "Bounce"):
        start_y = min(canvas_h + 60, py + 120)
        tags.append(f"\\move({px},{start_y},{px},{py},0,700)")
    else:
        tags.append(f"\\pos({px},{py})")

    if fade_in_ms or fade_out_ms:
        tags.append(f"\\fad({fade_in_ms},{fade_out_ms})")
    if anim in ("Zoom", "Grow & Turn"):
        tags.append("\\fscx25\\fscy25\\t(0,650,\\fscx100\\fscy100)")

    mins, secs = divmod(int(duration), 60)
    end_ts = f"0:{mins:02d}:{secs:02d}.00"
    clean_text = str(text).strip().replace("\r\n", "\n").replace("\n", "\\N")
    font_size = max(24, min(64, int(canvas_h * 0.05)))

    ass_content = f"""[Script Info]
Title: Intro Subtitle
ScriptType: v4.00+
PlayResX: {canvas_w}
PlayResY: {canvas_h}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: IntroSub,DejaVu Sans,{font_size},&H00FFFFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,3,2,{an},30,30,30,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.00,{end_ts},IntroSub,,0,0,0,,{{{"".join(tags)}}}{clean_text}
"""
    try:
        with open(ass_path, "w", encoding="utf-8") as f:
            f.write(ass_content)
        return True
    except Exception as e:
        LOGGER.warning(f"Failed writing ASS subtitle file: {e}")
        return False


def _video_dimensions_and_codec(streams):
    width, height = 1280, 720
    v_codec = "h264"
    for st in streams or []:
        if st.get("codec_type") == "video" and not (st.get("disposition") or {}).get("attached_pic", 0):
            w = int(st.get("width") or 0)
            h = int(st.get("height") or 0)
            if w > 0 and h > 0:
                # Ensure even dimensions for yuv420p encoders
                width = w - (w % 2)
                height = h - (h % 2)
            v_codec = str(st.get("codec_name") or "h264").lower()
            break
    return max(160, width), max(120, height), v_codec


def _video_encoder_args_for_ext(ext: str):
    ext_l = (ext or ".mkv").lower()
    if ext_l == ".webm":
        return ["-c:v:0", "libvpx-vp9", "-crf", "30", "-b:v", "0", "-deadline", "realtime", "-cpu-used", "5"]
    if ext_l == ".wmv":
        return ["-c:v:0", "wmv2", "-b:v", "2500k"]
    if ext_l == ".avi":
        return ["-c:v:0", "libx264", "-preset", "superfast", "-crf", "23"]
    return ["-c:v:0", "libx264", "-preset", "superfast", "-crf", "23", "-pix_fmt", "yuv420p"]


async def _apply_intro_to_single_video(
    listener,
    ffmpeg: FFMpeg,
    file_path: str,
    duration: int,
    text: str,
    text_pos: str,
    image_path: str,
    img_pos: str,
    animation: str,
    transition: str,
) -> bool:
    streams = await get_streams(file_path)
    if not streams:
        return False

    has_video = any(
        st.get("codec_type") == "video" and not (st.get("disposition") or {}).get("attached_pic", 0)
        for st in streams
    )
    if not has_video:
        return False

    canvas_w, canvas_h, _ = _video_dimensions_and_codec(streams)
    ext = ospath.splitext(file_path)[1] or ".mkv"
    base_dir = ospath.dirname(file_path) or "."

    fd_mov, temp_mov = tempfile.mkstemp(prefix=".intro_ovl_", suffix=".mov", dir=base_dir)
    os.close(fd_mov)
    fd_ass, temp_ass = tempfile.mkstemp(prefix=".intro_sub_", suffix=".ass", dir=base_dir)
    os.close(fd_ass)
    temp_out = f"{ospath.splitext(file_path)[0]}.intro_temp{ext}"

    try:
        ok_mov = await sync_to_async(
            _build_intro_overlay_mov_sync,
            temp_mov,
            canvas_w,
            canvas_h,
            duration,
            text,
            text_pos,
            image_path,
            img_pos,
            animation,
            transition,
        )
        if not ok_mov:
            return False

        has_ass = bool(text) and await sync_to_async(
            _build_intro_ass_subtitle,
            temp_ass,
            canvas_w,
            canvas_h,
            duration,
            text,
            text_pos,
            animation,
            transition,
        )

        cores, threads = ffmpeg_layout()
        ffmpeg.clear()
        media_info = await get_media_info(file_path)
        if media_info:
            ffmpeg._total_time = media_info[0]

        ext_l = ext.lower()
        can_mux_soft_sub = has_ass and ext_l in (".mkv", ".mp4", ".m4v", ".mov")

        async def _run_ffmpeg_mux(include_soft_sub: bool) -> bool:
            cmd = [
                "taskset",
                "-c",
                f"{cores}",
                BinConfig.FFMPEG_NAME,
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-progress",
                "pipe:1",
                "-i",
                file_path,
                "-i",
                temp_mov,
            ]
            if include_soft_sub:
                cmd.extend(["-i", temp_ass])

            # Overlay the animated RGBA intro during 0..duration; pass original frames untouched afterwards
            cmd.extend(
                [
                    "-filter_complex",
                    # Keep the source video as the timeline master. The overlay is
                    # visible only from t=0 through the configured intro duration;
                    # once it ends, FFmpeg passes the untouched source frames through.
                    f"[0:v:0][1:v:0]overlay=0:0:eof_action=pass:repeatlast=0:format=auto:enable=between(t\\,0\\,{duration})[outv]",
                    "-map",
                    "[outv]",
                ]
            )

            # Map remaining streams from input 0 (preserving attached pictures, audio, subtitles, attachments)
            existing_sub_count = 0
            first_video_seen = False
            for st in streams:
                idx = st.get("index")
                ctype = st.get("codec_type")
                if ctype == "video":
                    if not first_video_seen and not (st.get("disposition") or {}).get("attached_pic", 0):
                        first_video_seen = True
                        continue
                    cmd.extend(["-map", f"0:{idx}"])
                elif ctype == "audio":
                    cmd.extend(["-map", f"0:{idx}"])
                elif ctype == "subtitle":
                    cmd.extend(["-map", f"0:{idx}"])
                    existing_sub_count += 1
                elif ext_l == ".mkv":
                    cmd.extend(["-map", f"0:{idx}"])

            if include_soft_sub:
                cmd.extend(["-map", "2:s:0"])

            # Preserve all container & stream metadata and chapters processed earlier
            cmd.extend(
                [
                    "-map_metadata",
                    "0",
                    "-map_chapters",
                    "0",
                ]
            )

            cmd.extend(_video_encoder_args_for_ext(ext_l))
            # Copy any attached cover-art video streams
            v_total = sum(1 for st in streams if st.get("codec_type") == "video")
            for v_idx in range(1, v_total):
                cmd.extend([f"-c:v:{v_idx}", "copy"])

            cmd.extend(["-c:a", "copy"])

            if include_soft_sub:
                for s_idx in range(existing_sub_count):
                    cmd.extend([f"-c:s:{s_idx}", "copy"])
                sub_codec = "ass" if ext_l == ".mkv" else "mov_text"
                cmd.extend(
                    [
                        f"-c:s:{existing_sub_count}",
                        sub_codec,
                        f"-metadata:s:s:{existing_sub_count}",
                        "title=Intro Subtitle",
                        f"-metadata:s:s:{existing_sub_count}",
                        "language=eng",
                    ]
                )
            else:
                cmd.extend(["-c:s", "copy"])

            if ext_l in (".mp4", ".m4v", ".mov"):
                cmd.extend(["-movflags", "+faststart"])

            cmd.extend(["-threads", str(threads), temp_out])

            listener.subproc = await create_subprocess_exec(*cmd, stdout=PIPE, stderr=PIPE)
            await ffmpeg._ffmpeg_progress()
            _, stderr = await listener.subproc.communicate()
            if listener.is_cancelled:
                return False
            if listener.subproc.returncode == 0 and await aiopath.exists(temp_out) and await get_path_size(temp_out) > 0:
                return True
            err_txt = stderr.decode("utf-8", "replace").strip() if stderr else ""
            LOGGER.warning(f"Intro Subtitle FFmpeg pass (soft_sub={include_soft_sub}) failed on {file_path}: {err_txt}")
            if await aiopath.exists(temp_out):
                with suppress(Exception):
                    await remove(temp_out)
            return False

        success = await _run_ffmpeg_mux(include_soft_sub=can_mux_soft_sub)
        if not success and can_mux_soft_sub and not listener.is_cancelled:
            # Fallback without soft-subtitle muxing if container rejected extra subtitle track
            success = await _run_ffmpeg_mux(include_soft_sub=False)

        if success and await aiopath.exists(temp_out):
            await remove(file_path)
            await move(temp_out, file_path)
            LOGGER.info(f"Applied Intro Subtitle ({duration}s, anim={animation}, trans={transition}) to {file_path}")
            return True
        return False
    finally:
        for p in (temp_mov, temp_ass, temp_out):
            if p and await aiopath.exists(p):
                with suppress(Exception):
                    await remove(p)


async def apply_intro_subtitle(listener, dl_path: str, gid: str) -> str:
    """Apply configured Intro Subtitle to every applicable video in dl_path right after metadata processing."""
    if not dl_path or not await aiopath.exists(dl_path) or is_archive(dl_path) or is_archive_split(dl_path):
        return dl_path

    user_dict = getattr(listener, "user_dict", {}) or {}
    enabled = bool(
        user_dict.get("INTRO_SUB_ENABLED")
        if "INTRO_SUB_ENABLED" in user_dict
        else getattr(Config, "INTRO_SUB_ENABLED", False)
    )
    if not enabled:
        return dl_path

    raw_dur = user_dict.get("INTRO_SUB_DURATION", DEFAULT_INTRO_DURATION)
    duration = validate_intro_duration(raw_dur) or DEFAULT_INTRO_DURATION

    raw_text = user_dict.get("INTRO_SUB_TEXT") or ""
    font_style = user_dict.get("INTRO_SUB_FONT_STYLE", "normal")
    text = apply_unicode_font_style(str(raw_text), font_style) if raw_text else ""

    raw_img = user_dict.get("INTRO_SUB_IMAGE") or f"intro_sub/{listener.user_id}_intro.png"
    image_path = ""
    temp_downloaded_img = ""
    if raw_img:
        if isinstance(raw_img, str) and raw_img.startswith(("http://", "https://")):
            dl_img = await download_image_thumb(raw_img)
            if dl_img and await aiopath.exists(dl_img):
                image_path = dl_img
                temp_downloaded_img = dl_img
        elif isinstance(raw_img, str) and await aiopath.exists(raw_img):
            image_path = raw_img

    if not text and not image_path:
        return dl_path

    text_pos = normalize_position_name(
        user_dict.get("INTRO_SUB_TEXT_POSITION"), default="Bottom-Center"
    )
    img_pos = normalize_position_name(
        user_dict.get("INTRO_SUB_IMAGE_POSITION"), default="Top-Right"
    )
    animation = normalize_animation_name(user_dict.get("INTRO_SUB_ANIMATION", "None"))
    transition = normalize_transition_name(user_dict.get("INTRO_SUB_TRANSITION", "None"))

    is_file = await aiopath.isfile(dl_path)
    video_files = []
    if is_file:
        if (await get_document_type(dl_path))[0]:
            video_files.append(dl_path)
    else:
        for dirpath, _, files in await sync_to_async(walk, dl_path, topdown=False):
            for file_ in sorted(files):
                f_path = ospath.join(dirpath, file_)
                if (await get_document_type(f_path))[0]:
                    video_files.append(f_path)

    if not video_files:
        if temp_downloaded_img and await aiopath.exists(temp_downloaded_img):
            with suppress(Exception):
                await remove(temp_downloaded_img)
        return dl_path

    ffmpeg = FFMpeg(listener)
    async with task_dict_lock:
        task_dict[listener.mid] = FFmpegStatus(listener, ffmpeg, gid, "Intro Subtitle")

    listener.progress = False
    try:
        async with ff_lock:
            listener.progress = True
            for f_path in video_files:
                if listener.is_cancelled:
                    break
                listener.proceed_count += 1
                listener.subname = ospath.basename(f_path)
                listener.subsize = await get_path_size(f_path)
                await _apply_intro_to_single_video(
                    listener,
                    ffmpeg,
                    f_path,
                    duration=duration,
                    text=text,
                    text_pos=text_pos,
                    image_path=image_path,
                    img_pos=img_pos,
                    animation=animation,
                    transition=transition,
                )
    finally:
        if temp_downloaded_img and await aiopath.exists(temp_downloaded_img):
            with suppress(Exception):
                await remove(temp_downloaded_img)

    return dl_path
