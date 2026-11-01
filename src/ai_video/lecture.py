"""单图讲解：把一张产品图拆成 5 页，逐页生成中文解说词与配音画面。"""

from __future__ import annotations

import base64
import io
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont, ImageOps

from .config import Settings
from .http_client import post_json


@dataclass(slots=True)
class LectureSlide:
    index: int
    default_title: str
    content: Image.Image
    slide_path: Path


def build_slides_from_image(
    image_path: Path,
    settings: Settings,
    task_dir: Path,
) -> list[LectureSlide]:
    """把整张图拆成 1 张全图 + 4 个 2x2 区域，渲染为 5 张 16:9 幻灯片。"""
    image = Image.open(image_path).convert("RGB")
    width, height = image.size
    half_w = max(1, width // 2)
    half_h = max(1, height // 2)
    regions: list[tuple[int, str, tuple[int, int, int, int]]] = [
        (1, "整体概览", (0, 0, width, height)),
        (2, "左上区域", (0, 0, half_w, half_h)),
        (3, "右上区域", (half_w, 0, width, half_h)),
        (4, "左下区域", (0, half_h, half_w, height)),
        (5, "右下区域", (half_w, half_h, width, height)),
    ]
    images_dir = task_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)
    slides: list[LectureSlide] = []
    for index, default_title, box in regions:
        content = image.crop(box)
        slide_path = images_dir / f"page_{index:02d}.png"
        _render_lecture_slide(content, default_title, index, settings, slide_path)
        slides.append(LectureSlide(index, default_title, content, slide_path))
    return slides


class ImageLectureService:
    """为每一页画面生成中文标题与解说词；无 API 时使用模板。"""

    def __init__(self, settings: Settings, logger: logging.Logger) -> None:
        self.settings = settings
        self.logger = logger

    def plan(self, slides: list[LectureSlide]) -> list[dict[str, str]]:
        results: list[dict[str, str]] = []
        for slide in slides:
            results.append(self._describe(slide))
        return results

    def _describe(self, slide: LectureSlide) -> dict[str, str]:
        fallback_narration = _fallback_narration(slide.index)
        if not self.settings.use_api():
            return {"title": slide.default_title, "narration": fallback_narration}
        try:
            result = _call_vision(
                self.settings, self.logger, _pil_data_uri(slide.content)
            )
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("第 %d 页画面解说失败，使用模板：%s", slide.index, exc)
            return {"title": slide.default_title, "narration": fallback_narration}
        title = str(result.get("title", "")).strip()
        narration = str(result.get("narration", "")).strip()
        title = title[:14] if title else slide.default_title
        narration = narration[:120] if narration else fallback_narration
        return {"title": title, "narration": narration}


def _call_vision(
    settings: Settings,
    logger: logging.Logger,
    data_uri: str,
) -> dict[str, Any]:
    system = (
        "你是产品图讲解员。给这张画面写一句中文解说（25～50 字）作为配音旁白，"
        "再给一个不超过 8 字的中文标题。只输出 JSON，不要输出 Markdown。"
    )
    user = '请输出 JSON：{"title":"标题","narration":"一句话解说"}'
    payload: dict[str, Any] = {
        "model": settings.qc_model,
        "messages": [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user},
                    {"type": "image_url", "image_url": {"url": data_uri}},
                ],
            },
        ],
        "temperature": 0.4,
    }
    if settings.is_siliconflow:
        payload["response_format"] = {"type": "json_object"}
    result = post_json(
        f"{settings.base_url}/chat/completions",
        payload,
        api_key=settings.api_key,
        timeout=settings.request_timeout_seconds,
        retries=settings.max_retries,
        logger=logger,
    )
    content = result["choices"][0]["message"]["content"]
    if isinstance(content, list):
        content = "".join(
            item.get("text", "") for item in content if isinstance(item, dict)
        )
    value = _parse_json(str(content))
    if not isinstance(value, dict):
        raise ValueError("解说返回的不是 JSON 对象")
    return value


def _parse_json(content: str) -> dict[str, Any]:
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    value = json.loads(text)
    return value if isinstance(value, dict) else {}


def _pil_data_uri(image: Image.Image) -> str:
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def _fallback_narration(index: int) -> str:
    texts = {
        1: "先看这张产品图的整体画面，建立完整印象。",
        2: "这是画面的左上部分，留意其中的产品信息与细节。",
        3: "这是画面的右上部分，注意这里展示的关键信息。",
        4: "这是画面的左下部分，聚焦这一区域的细节内容。",
        5: "这是画面的右下部分，结合整体来理解这一块内容。",
    }
    return texts.get(index, "这是产品图的一个局部画面。")


def _render_lecture_slide(
    content: Image.Image,
    title: str,
    index: int,
    settings: Settings,
    destination: Path,
) -> None:
    width, height = settings.width, settings.height
    canvas = Image.new("RGB", (width, height), (9, 14, 28))
    draw = ImageDraw.Draw(canvas, "RGBA")
    margin_x = int(width * 0.04)
    top_band = int(height * 0.15)
    bottom_margin = int(height * 0.04)
    available_w = width - margin_x * 2
    available_h = height - top_band - bottom_margin
    fitted = ImageOps.contain(
        content, (available_w, available_h), Image.Resampling.LANCZOS
    )
    x = (width - fitted.width) // 2
    y = top_band + (available_h - fitted.height) // 2
    canvas.paste(fitted, (x, y))
    draw.rounded_rectangle(
        (margin_x, int(height * 0.025), width - margin_x, top_band - int(height * 0.018)),
        radius=14,
        fill=(5, 10, 24, 195),
        outline=(255, 255, 255, 22),
        width=2,
    )
    title_font = _font(int(height * 0.038), bold=True)
    meta_font = _font(int(height * 0.022))
    draw.text(
        (int(width * 0.06), int(height * 0.05)),
        title,
        font=title_font,
        fill=(245, 247, 250, 255),
    )
    draw.text(
        (width - int(width * 0.06), int(height * 0.058)),
        f"{index:02d} / 05",
        font=meta_font,
        fill=(237, 185, 92, 255),
        anchor="ra",
    )
    canvas.save(destination, format="PNG", optimize=True)


def _font(
    size: int, bold: bool = False
) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    paths: list[Path] = []
    if bold:
        paths.extend(
            [
                Path("C:/Windows/Fonts/msyhbd.ttc"),
                Path("C:/Windows/Fonts/simhei.ttf"),
                Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"),
                Path("/System/Library/Fonts/PingFang.ttc"),
            ]
        )
    paths.extend(
        [
            Path("C:/Windows/Fonts/msyh.ttc"),
            Path("C:/Windows/Fonts/simsun.ttc"),
            Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
            Path("/System/Library/Fonts/PingFang.ttc"),
        ]
    )
    for path in paths:
        if path.is_file():
            try:
                return ImageFont.truetype(str(path), size=size)
            except OSError:
                continue
    return ImageFont.load_default(size=size)
