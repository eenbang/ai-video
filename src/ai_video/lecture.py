"""单图讲解：根据一张产品图的内容，生成 5 页风格一致的分镜与解说词。"""

from __future__ import annotations

import base64
import io
import json
import logging
import re
from pathlib import Path
from typing import Any

from PIL import Image

from .config import Settings
from .http_client import post_json
from .models import Storyboard, StoryboardPage
from .planner import PLAN_SCHEMA


class ImageStoryboardService:
    """用视觉模型理解产品图，产出恰好 5 页的分镜（标题/要点/画面提示词/解说词）。"""

    def __init__(self, settings: Settings, logger: logging.Logger) -> None:
        self.settings = settings
        self.logger = logger

    def plan(self, image_path: Path) -> Storyboard:
        image_path = image_path.resolve()
        if not image_path.is_file():
            raise ValueError(f"产品图片不存在：{image_path}")
        if self.settings.use_api():
            try:
                return self._remote_plan(image_path)
            except Exception as exc:  # noqa: BLE001
                self.logger.warning("根据产品图规划失败，使用本地模板：%s", exc)
                if self.settings.mode == "api":
                    raise
        return self._local_plan(image_path)

    def _remote_plan(self, image_path: Path) -> Storyboard:
        data_uri = _image_data_uri(image_path)
        system = (
            "你是中文讲解短视频的分镜策划。先仔细观察用户提供的产品图，提炼图中的产品名称、"
            "卖点、参数、结构或使用场景，然后严格拆成恰好 5 页。信息必须忠于图片，不编造图中没有的事实；"
            "五页要有清晰的起承转合。每页给出短标题、2-3 个要点、画面描述和 30-100 字自然口播。"
            "标题、要点和口播用中文；visual_prompt 与 style_guide 必须用英文。"
            "五页画面要风格统一：同一配色、同一排版、干净的 PPT 单页风格，"
            "画面内可包含少量中文文字（短标题、关键词标签、图表标注或简单逻辑图）。"
        )
        user = (
            "请根据这张产品图制作 5 页中文图文讲解分镜。"
            "只输出一个 JSON 对象，不要输出任何其他文字、注释或 Markdown 代码块。"
            "JSON 顶层只能包含 style_guide 和 pages 两个键，"
            "不要重复输出 schema 本身（不要出现 type、properties、required 等字段）。"
            "pages 恰好 5 项，每项包含 index、title、key_points、visual_prompt、narration。"
        )
        payload: dict[str, Any] = {
            "model": self.settings.qc_model,
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
            "temperature": 0.5,
        }
        if self.settings.is_siliconflow:
            payload["response_format"] = {"type": "json_object"}
            payload["messages"][1]["content"][0]["text"] += (
                "\n\n结构要求如下：\n"
                + json.dumps(PLAN_SCHEMA, ensure_ascii=False)
            )
        else:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "five_page_storyboard",
                    "strict": True,
                    "schema": PLAN_SCHEMA,
                },
            }
        result = post_json(
            f"{self.settings.base_url}/chat/completions",
            payload,
            api_key=self.settings.api_key,
            timeout=self.settings.request_timeout_seconds,
            retries=self.settings.max_retries,
            logger=self.logger,
        )
        content = result["choices"][0]["message"]["content"]
        if isinstance(content, list):
            content = "".join(
                item.get("text", "") for item in content if isinstance(item, dict)
            )
        raw = _parse_json(str(content))
        storyboard = Storyboard.from_dict(
            {
                "topic": image_path.stem,
                "style_guide": str(raw.get("style_guide", "")).strip(),
                "pages": raw.get("pages", []),
                "planner": f"api:{self.settings.qc_model}",
            }
        )
        storyboard.validate()
        self.logger.info("根据产品图规划完成，模型=%s", self.settings.qc_model)
        return storyboard

    def _local_plan(self, image_path: Path) -> Storyboard:
        topic = image_path.stem
        style_guide = (
            "Clean modern business presentation slide; consistent deep-blue and white "
            "palette with a single amber accent; title at top, short Chinese bullet "
            "points, flat vector icons and a simple diagram; generous margins; 16:9; "
            "no logos or watermarks."
        )
        titles = ["产品概览", "核心卖点", "细节与参数", "使用场景", "总结与建议"]
        points = [
            ["整体外观", "产品定位"],
            ["主要功能", "突出优势"],
            ["关键参数", "细节设计"],
            ["适用人群", "典型场景"],
            ["回顾要点", "行动建议"],
        ]
        scenes = [
            "a clean product overview slide with the product image centered and a short Chinese title on top",
            "a clean slide with three bullet points and flat icons highlighting key selling points",
            "a clean slide with a simple annotated diagram of the product structure and key specs",
            "a clean slide showing the product in a real usage scenario with two short labels",
            "a clean summary slide with five key takeaways arranged in a row",
        ]
        pages: list[StoryboardPage] = []
        for index in range(5):
            pages.append(
                StoryboardPage(
                    index=index + 1,
                    title=titles[index],
                    key_points=points[index],
                    visual_prompt=(
                        f"{style_guide} Slide {index + 1}: {scenes[index]}. "
                        "Include a short Chinese title and a few Chinese labels if helpful."
                    ),
                    narration=_template_narration(index, topic),
                )
            )
        storyboard = Storyboard(
            topic=topic,
            style_guide=style_guide,
            pages=pages,
            planner="image-template",
        )
        storyboard.validate()
        self.logger.info("无 API 时使用图片分镜模板")
        return storyboard


def _image_data_uri(image_path: Path) -> str:
    with Image.open(image_path) as image:
        buffer = io.BytesIO()
        image.convert("RGB").save(buffer, format="PNG")
        encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def _parse_json(content: str) -> dict[str, Any]:
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    value = json.loads(text)
    return value if isinstance(value, dict) else {}


def _template_narration(index: int, topic: str) -> str:
    texts = [
        f"这期用五个画面介绍“{topic}”。先看整体概览，再逐步了解它的卖点、参数和使用场景。",
        f"“{topic}”的核心卖点，重点看它的主要功能和突出优势。",
        f"再看“{topic}”的细节与关键参数，这部分决定了它的实际表现。",
        f"把“{topic}”放进真实使用场景里，看它适合哪些人群和场合。",
        f"最后回顾五个要点，并给出下一步建议。",
    ]
    return texts[index]
