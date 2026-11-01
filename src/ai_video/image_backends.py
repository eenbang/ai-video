from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import math
import time
import uuid
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from PIL import Image, ImageDraw, ImageFont, ImageOps

from .config import Settings
from .http_client import get_bytes, post_json
from .models import StoryboardPage


class ImageBackendError(RuntimeError):
    """图像后端生成失败。"""


class ImageBackend(ABC):
    """单页图像后端接口。

    每个后端只负责把一页分镜渲染为一张 PNG，并返回 provider 标识。
    视频流水线不关心具体后端实现。
    """

    name = "base"

    def __init__(
        self,
        settings: Settings,
        logger: logging.Logger,
        task_id: str,
    ) -> None:
        self.settings = settings
        self.logger = logger
        self.task_id = task_id

    @abstractmethod
    def generate(
        self,
        page: StoryboardPage,
        style_guide: str,
        destination: Path,
    ) -> str:
        """为单页生成图片并保存到 destination，返回 provider 标识。"""


def _build_visual_prompt(
    page: StoryboardPage,
    style_guide: str,
    allow_text: bool = True,
) -> str:
    subject = (page.image_prompt or page.visual_prompt).strip()
    text_rule = (
        "Include minimal, legible Chinese text where it helps understanding: "
        "a short title, keyword labels, chart annotations, or a simple logic "
        "diagram. Keep text short and accurate; no long paragraphs. "
        "No watermarks or logos."
        if allow_text
        else "No text, letters, numbers, logos, watermarks or UI labels."
    )
    return (
        f"{subject}. "
        f"Art direction: {style_guide}. "
        "16:9 cinematic composition, clear focal point, soft balanced lighting, "
        "modest breathing room in the lower third for a caption bar. "
        f"{text_rule}"
    )


def _fitted_image(image: Image.Image, settings: Settings) -> Image.Image:
    return ImageOps.fit(
        image.convert("RGB"),
        (settings.width, settings.height),
        method=Image.Resampling.LANCZOS,
        centering=(0.5, 0.45),
    )


def _save_png(image: Image.Image, destination: Path) -> None:
    image.convert("RGB").save(destination, format="PNG", optimize=True)


def _add_generated_caption(
    image: Image.Image,
    page: StoryboardPage,
    settings: Settings,
) -> None:
    width, height = image.size
    draw = ImageDraw.Draw(image, "RGBA")
    band_height = int(height * 0.18)
    top = height - band_height
    for offset in range(band_height):
        alpha = int(210 * (offset / max(1, band_height - 1)) ** 0.75)
        draw.line((0, top + offset, width, top + offset), fill=(5, 11, 25, alpha))
    draw.rounded_rectangle(
        (64, top + 34, width - 64, height - 34),
        radius=26,
        fill=(6, 13, 30, 190),
        outline=(255, 255, 255, 28),
        width=2,
    )
    title_font = _font(int(height * 0.052), bold=True)
    meta_font = _font(int(height * 0.022))
    draw.text(
        (102, top + 56),
        page.title,
        font=title_font,
        fill=(248, 244, 232, 255),
    )
    label = f"{page.index:02d} / 05"
    if settings.ai_disclosure:
        label += "    AI 辅助生成画面"
    draw.text(
        (width - 102, top + 63),
        label,
        font=meta_font,
        fill=(237, 185, 92, 255),
        anchor="ra",
    )


class DemoImageBackend(ImageBackend):
    """本地 PIL 演示画面，不依赖网络。"""

    name = "local-demo"

    def generate(
        self,
        page: StoryboardPage,
        style_guide: str,
        destination: Path,
    ) -> str:
        self._render_demo(page, destination)
        return self.name

    def _render_demo(self, page: StoryboardPage, destination: Path) -> None:
        width, height = self.settings.width, self.settings.height
        palette = [
            ((7, 18, 42), (14, 62, 78), (244, 184, 82)),
            ((12, 20, 48), (42, 45, 94), (104, 205, 173)),
            ((8, 28, 45), (28, 77, 78), (244, 152, 95)),
            ((12, 17, 46), (59, 45, 91), (114, 183, 238)),
            ((8, 23, 42), (25, 69, 64), (241, 194, 91)),
        ]
        bg_top, bg_bottom, accent = palette[(page.index - 1) % len(palette)]
        image = Image.new("RGB", (width, height), bg_top)
        draw = ImageDraw.Draw(image, "RGBA")
        for y in range(height):
            ratio = y / max(1, height - 1)
            color = tuple(
                int(bg_top[channel] * (1 - ratio) + bg_bottom[channel] * ratio)
                for channel in range(3)
            )
            draw.line((0, y, width, y), fill=(*color, 255))

        for x in range(-200, width + 200, 76):
            draw.line((x, 0, x - 360, height), fill=(255, 255, 255, 8), width=1)
        for radius in range(460, 40, -28):
            alpha = int(26 * (1 - radius / 480))
            draw.ellipse(
                (
                    width - 640 - radius,
                    -180 - radius,
                    width - 640 + radius,
                    220 + radius,
                ),
                fill=(*accent, alpha),
            )
        draw.rounded_rectangle(
            (
                int(width * 0.065),
                int(height * 0.10),
                int(width * 0.48),
                int(height * 0.86),
            ),
            radius=38,
            fill=(4, 10, 25, 178),
            outline=(255, 255, 255, 30),
            width=2,
        )

        title_font = _font(66, bold=True)
        body_font = _font(31)
        meta_font = _font(23, bold=True)
        index_font = _font(250, bold=True)

        draw.rounded_rectangle(
            (145, 166, 290, 214),
            radius=24,
            fill=(*accent, 235),
        )
        draw.text(
            (217, 190),
            f"PAGE {page.index:02d}",
            font=meta_font,
            fill=(8, 16, 29, 255),
            anchor="mm",
        )
        title_lines = _wrap_text(draw, page.title, title_font, 520)
        title_y = 256
        for line in title_lines[:3]:
            draw.text((146, title_y), line, font=title_font, fill=(247, 244, 235, 255))
            title_y += 85

        line_y = title_y + 22
        draw.rounded_rectangle(
            (146, line_y, 326, line_y + 7),
            radius=3,
            fill=(*accent, 255),
        )
        point_y = line_y + 52
        for point in page.key_points[:3]:
            draw.ellipse(
                (149, point_y + 11, 161, point_y + 23),
                fill=(*accent, 255),
            )
            point_lines = _wrap_text(draw, point, body_font, 585)
            for line in point_lines[:2]:
                draw.text(
                    (184, point_y),
                    line,
                    font=body_font,
                    fill=(218, 224, 233, 255),
                )
                point_y += 43
            point_y += 16

        cx, cy = int(width * 0.735), int(height * 0.45)
        draw.text(
            (cx, cy + 30),
            f"{page.index:02d}",
            font=index_font,
            fill=(255, 255, 255, 18),
            anchor="mm",
        )
        radius = 170 + page.index * 12
        draw.ellipse(
            (cx - radius, cy - radius, cx + radius, cy + radius),
            outline=(*accent, 200),
            width=6,
        )
        draw.ellipse(
            (cx - 78, cy - 78, cx + 78, cy + 78),
            fill=(*accent, 46),
            outline=(*accent, 220),
            width=4,
        )
        for i in range(5):
            angle = -1.05 + i * 0.52
            x = cx + int(math.cos(angle) * radius)
            y = cy + int(math.sin(angle) * radius)
            active = i < page.index
            dot_fill = (*accent, 245 if active else 55)
            draw.ellipse((x - 18, y - 18, x + 18, y + 18), fill=dot_fill)

        footer = "AI 辅助生成 · 演示画面（非 AI 生图）"
        if not self.settings.ai_disclosure:
            footer = "演示画面（非 AI 生图）"
        draw.text(
            (146, height - 74),
            footer,
            font=_font(21),
            fill=(182, 193, 208, 220),
        )
        _save_png(image, destination)
        self.logger.info("第 %d 页演示画面完成", page.index)


class OpenAIImageBackend(ImageBackend):
    """OpenAI 兼容的 /images/generations 后端。"""

    name = "openai"

    def generate(
        self,
        page: StoryboardPage,
        style_guide: str,
        destination: Path,
    ) -> str:
        image = self._generate_remote(page, style_guide)
        _save_png(image, destination)
        provider = f"api:{self.settings.image_model}"
        self.logger.info("第 %d 页画面完成（%s）", page.index, provider)
        return provider

    def _generate_remote(
        self,
        page: StoryboardPage,
        style_guide: str,
    ) -> Image.Image:
        prompt = _build_visual_prompt(
            page, style_guide, self.settings.image_text
        )
        payload: dict[str, object] = {
            "model": self.settings.image_model,
            "prompt": prompt,
        }
        if self.settings.is_siliconflow:
            payload["image_size"] = self.settings.image_size
        else:
            payload["size"] = "1536x1024"
            payload["n"] = 1
            if "api.openai.com" in self.settings.base_url:
                payload["safety_identifier"] = hashlib.sha256(
                    self.task_id.encode("utf-8")
                ).hexdigest()[:64]
        result = post_json(
            f"{self.settings.base_url}/images/generations",
            payload,
            api_key=self.settings.api_key,
            timeout=self.settings.request_timeout_seconds,
            retries=self.settings.max_retries,
            logger=self.logger,
        )
        items = result.get("data") or result.get("images") or []
        if not items or not isinstance(items[0], dict):
            raise ImageBackendError("生图接口未返回图片")
        item = items[0]
        if item.get("b64_json"):
            raw = base64.b64decode(item["b64_json"])
        elif item.get("url"):
            raw = get_bytes(
                str(item["url"]),
                timeout=self.settings.request_timeout_seconds,
                retries=self.settings.max_retries,
                logger=self.logger,
            )
        else:
            raise ImageBackendError("生图接口返回了空图片")
        image = Image.open(io.BytesIO(raw))
        fitted = _fitted_image(image, self.settings)
        _add_generated_caption(fitted, page, self.settings)
        return fitted


class ComfyUIImageBackend(ImageBackend):
    """本地 ComfyUI HTTP API 后端。

    工作流模板使用 API 格式 JSON，并支持以下占位符：
    {{prompt}} / {{positive_prompt}}、{{negative_prompt}}、{{width}}、{{height}}、{{seed}}。
    """

    name = "comfyui"

    def __init__(
        self,
        settings: Settings,
        logger: logging.Logger,
        task_id: str,
    ) -> None:
        super().__init__(settings, logger, task_id)
        self._client_id = uuid.uuid4().hex

    def generate(
        self,
        page: StoryboardPage,
        style_guide: str,
        destination: Path,
    ) -> str:
        graph = self._load_workflow(page, style_guide)
        prompt_id = self._queue_prompt(graph)
        history = self._wait_for_result(prompt_id)
        raw = self._download_result(history)
        image = Image.open(io.BytesIO(raw))
        fitted = _fitted_image(image, self.settings)
        _add_generated_caption(fitted, page, self.settings)
        _save_png(fitted, destination)
        provider = self._provider_label()
        self.logger.info("第 %d 页画面完成（%s）", page.index, provider)
        return provider

    def _load_workflow(
        self,
        page: StoryboardPage,
        style_guide: str,
    ) -> dict[str, Any]:
        workflow_file = self.settings.comfyui_workflow_file
        if workflow_file is None or not workflow_file.is_file():
            raise ImageBackendError(
                "ComfyUI 后端需要 COMFYUI_WORKFLOW_FILE 指向存在的 API 格式 JSON 工作流"
            )
        try:
            text = workflow_file.read_text(encoding="utf-8")
        except OSError as exc:
            raise ImageBackendError(f"无法读取 ComfyUI 工作流：{workflow_file}") from exc

        seed = _seed_for(self.task_id, page.index)
        prompt = _build_visual_prompt(
            page, style_guide, self.settings.image_text
        )
        negative = (
            "lowres, bad anatomy, bad hands, text, letters, numbers, error, "
            "missing fingers, extra digit, fewer digits, cropped, worst "
            "quality, low quality, normal quality, jpeg artifacts, signature, "
            "watermark, username, blurry, deformed"
            if not self.settings.image_text
            else (
                "lowres, bad anatomy, bad hands, error, missing fingers, "
                "extra digit, fewer digits, cropped, worst quality, low quality, "
                "normal quality, jpeg artifacts, signature, watermark, username, "
                "blurry, deformed"
            )
        )
        replacements = {
            "{{positive_prompt}}": json.dumps(prompt, ensure_ascii=False),
            "{{prompt}}": json.dumps(prompt, ensure_ascii=False),
            "{{negative_prompt}}": json.dumps(negative, ensure_ascii=False),
            "{{width}}": str(self.settings.width),
            "{{height}}": str(self.settings.height),
            "{{seed}}": str(seed),
        }
        for key, value in replacements.items():
            text = text.replace(key, value)
        try:
            graph = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ImageBackendError("ComfyUI 工作流不是合法的 JSON") from exc
        if not isinstance(graph, dict) or not graph:
            raise ImageBackendError("ComfyUI 工作流必须是非空 JSON 对象")
        return graph

    def _queue_prompt(self, graph: dict[str, Any]) -> str:
        try:
            result = post_json(
                f"{self.settings.comfyui_base_url}/prompt",
                {"prompt": graph, "client_id": self._client_id},
                timeout=self.settings.request_timeout_seconds,
                retries=self.settings.max_retries,
                logger=self.logger,
            )
        except Exception as exc:
            raise ImageBackendError(
                f"提交 ComfyUI 任务失败：{self.settings.comfyui_base_url}"
            ) from exc
        prompt_id = result.get("prompt_id")
        if not prompt_id:
            raise ImageBackendError("ComfyUI 未返回 prompt_id")
        return str(prompt_id)

    def _wait_for_result(self, prompt_id: str) -> dict[str, Any]:
        history_url = f"{self.settings.comfyui_base_url}/history/{prompt_id}"
        deadline = time.monotonic() + self.settings.comfyui_timeout_seconds
        while time.monotonic() < deadline:
            try:
                raw = get_bytes(
                    history_url,
                    timeout=self.settings.request_timeout_seconds,
                    retries=self.settings.max_retries,
                    logger=self.logger,
                )
                history = json.loads(raw.decode("utf-8"))
            except Exception as exc:
                raise ImageBackendError(f"查询 ComfyUI 结果失败：{exc}") from exc
            entry = history.get(prompt_id)
            if entry:
                return entry
            time.sleep(self.settings.comfyui_poll_interval_seconds)
        raise ImageBackendError(
            f"等待 ComfyUI 完成超时（{self.settings.comfyui_timeout_seconds:.0f} 秒）"
        )

    def _download_result(self, history: dict[str, Any]) -> bytes:
        outputs = history.get("outputs")
        if not isinstance(outputs, dict):
            raise ImageBackendError("ComfyUI 历史记录缺少 outputs")
        selected = None
        node_id = self.settings.comfyui_output_node_id
        if node_id:
            node = outputs.get(node_id)
            if not isinstance(node, dict):
                raise ImageBackendError(f"ComfyUI 输出节点不存在：{node_id}")
            selected = _first_image(node.get("images"))
        else:
            for node in outputs.values():
                if isinstance(node, dict):
                    selected = _first_image(node.get("images"))
                    if selected:
                        break
        if selected is None:
            raise ImageBackendError("ComfyUI 输出中未找到图片")
        filename = selected.get("filename")
        if not filename:
            raise ImageBackendError("ComfyUI 输出缺少文件名")
        query = urlencode(
            {
                "filename": filename,
                "subfolder": selected.get("subfolder", ""),
                "type": selected.get("type", "output"),
            }
        )
        url = f"{self.settings.comfyui_base_url}/view?{query}"
        return get_bytes(
            url,
            timeout=self.settings.request_timeout_seconds,
            retries=self.settings.max_retries,
            logger=self.logger,
        )

    def _provider_label(self) -> str:
        workflow = self.settings.comfyui_workflow_file
        label = workflow.name if workflow is not None else "comfyui"
        return f"comfyui:{label}"


def _first_image(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, list) or not value:
        return None
    item = value[0]
    return item if isinstance(item, dict) else None


def _seed_for(task_id: str, page_index: int) -> int:
    digest = hashlib.sha256(f"{task_id}:{page_index}".encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") & 0x7FFFFFFF


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    font_paths = []
    if bold:
        font_paths.extend(
            [
                Path("C:/Windows/Fonts/msyhbd.ttc"),
                Path("C:/Windows/Fonts/simhei.ttf"),
                Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"),
                Path("/System/Library/Fonts/PingFang.ttc"),
            ]
        )
    font_paths.extend(
        [
            Path("C:/Windows/Fonts/msyh.ttc"),
            Path("C:/Windows/Fonts/simsun.ttc"),
            Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
            Path("/System/Library/Fonts/PingFang.ttc"),
        ]
    )
    for path in font_paths:
        if path.is_file():
            try:
                return ImageFont.truetype(str(path), size=size)
            except OSError:
                continue
    return ImageFont.load_default(size=size)


def _wrap_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    max_width: int,
) -> list[str]:
    if not text:
        return [""]
    lines: list[str] = []
    current = ""
    for char in text:
        candidate = current + char
        if current and draw.textlength(candidate, font=font) > max_width:
            lines.append(current)
            current = char
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines
