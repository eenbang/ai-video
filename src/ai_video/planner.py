from __future__ import annotations

import json
import logging
import re
from typing import Any

from .config import Settings
from .http_client import RemoteServiceError, post_json
from .models import Storyboard, StoryboardPage


PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "style_guide": {
            "type": "string",
            "description": "English shared visual direction covering medium, palette, lighting, composition and avoidances",
        },
        "pages": {
            "type": "array",
            "minItems": 5,
            "maxItems": 5,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "index": {"type": "integer", "minimum": 1, "maximum": 5},
                    "title": {"type": "string"},
                    "key_points": {
                        "type": "array",
                        "minItems": 2,
                        "maxItems": 3,
                        "items": {"type": "string"},
                    },
                    "visual_prompt": {
                        "type": "string",
                        "description": "English, concrete, drawable 16:9 scene: subject, action, environment, composition and lighting; may include short Chinese text labels, chart annotations or a simple logic diagram",
                    },
                    "narration": {
                        "type": "string",
                        "description": "自然中文口播，30 至 100 字，不使用 Markdown",
                    },
                },
                "required": [
                    "index",
                    "title",
                    "key_points",
                    "visual_prompt",
                    "narration",
                ],
            },
        },
    },
    "required": ["style_guide", "pages"],
}


class PlanningService:
    def __init__(self, settings: Settings, logger: logging.Logger) -> None:
        self.settings = settings
        self.logger = logger

    def create(self, source_text: str) -> Storyboard:
        source_text = source_text.strip()
        if not source_text:
            raise ValueError("请输入主题或文案")

        if self.settings.mode == "demo":
            return self._local_plan(source_text)
        if self.settings.mode == "api" and not self.settings.can_use_api:
            raise RuntimeError("api 模式需要设置 OPENAI_API_KEY")

        if self.settings.can_use_api:
            last_error: Exception | None = None
            for attempt in range(1, 3):
                try:
                    return self._remote_plan(source_text)
                except Exception as exc:
                    last_error = exc
                    self.logger.warning(
                        "AI 内容规划第 %d 次失败：%s", attempt, exc
                    )
            if self.settings.mode == "api":
                raise RuntimeError(
                    f"AI 内容规划失败：{last_error}"
                ) from last_error
            self.logger.warning(
                "AI 内容规划失败，切换本地模板：%s", last_error
            )
        return self._local_plan(source_text)

    def _remote_plan(self, source_text: str) -> Storyboard:
        system = (
            "你是中文讲解短视频的分镜策划。把用户输入严格拆成恰好 5 页，"
            "信息必须忠于输入，不扩写未经提供的事实。五页要有清晰的起承转合。"
            "每页给出短标题、2 至 3 个要点、画面描述、以及 30 至 100 字自然口播。"
            "标题、要点和解说词用中文；visual_prompt 与 style_guide 必须用英文写。"
            "visual_prompt 必须是具体、可直接作画的场景：写清主体、动作或状态、"
            "环境、构图与光线，不要用象征、代表、隐喻这类抽象说法，"
            "画面内可包含少量中文文字（短标题、关键词标签、图表标注或简短逻辑图），"
            "文字要简短准确，"
            "也不要用抽象几何代替真实场景。五页构图要有变化"
            "（远景、中景、特写、俯视等），主体不同但风格统一。不要输出 Markdown。"
        )
        user = (
            "请为下列主题或文案制作 5 页中文图文讲解分镜。"
            "每页画面采用 16:9，画面内可包含少量中文文字"
            "（短标题、关键词标签、图表标注或简短逻辑图）；"
            "在下半部预留干净区域。\n\n"
            f"输入：\n{source_text}"
        )
        response_format: dict[str, Any]
        if self.settings.is_siliconflow:
            response_format = {"type": "json_object"}
            user += (
                "\n\n只输出一个 JSON 对象，不要输出任何其他文字、注释或 Markdown 代码块。"
                "JSON 顶层只能包含 style_guide 和 pages 两个键，"
                "不要重复输出 schema 本身（不要出现 type、properties、required 等字段）。"
                "结构要求如下：\n"
                + json.dumps(PLAN_SCHEMA, ensure_ascii=False)
            )
        else:
            response_format = {
                "type": "json_schema",
                "json_schema": {
                    "name": "five_page_storyboard",
                    "strict": True,
                    "schema": PLAN_SCHEMA,
                },
            }
        payload = {
            "model": self.settings.planner_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.55,
            "response_format": response_format,
        }
        result = post_json(
            f"{self.settings.base_url}/chat/completions",
            payload,
            api_key=self.settings.api_key,
            timeout=self.settings.request_timeout_seconds,
            retries=self.settings.max_retries,
            logger=self.logger,
        )
        try:
            content = result["choices"][0]["message"]["content"]
            if isinstance(content, list):
                content = "".join(
                    item.get("text", "")
                    for item in content
                    if isinstance(item, dict)
                )
            raw = self._parse_json_content(str(content))
            storyboard = Storyboard.from_dict(
                {
                    "topic": source_text,
                    "style_guide": raw.get("style_guide", ""),
                    "pages": raw.get("pages", []),
                    "planner": f"api:{self.settings.planner_model}",
                }
            )
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise RemoteServiceError("内容规划返回的数据结构不符合要求") from exc
        self.logger.info("AI 分镜规划完成，模型=%s", self.settings.planner_model)
        return storyboard

    @staticmethod
    def _parse_json_content(content: str) -> dict[str, Any]:
        text = content.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
        value = json.loads(text)
        if not isinstance(value, dict):
            raise ValueError("返回的 JSON 不是对象")
        return value

    def _local_plan(self, source_text: str) -> Storyboard:
        source_text = re.sub(r"\s+", " ", source_text).strip()
        is_copy = len(source_text) > 100 or "\n" in source_text
        chunks = _split_balanced(source_text, 5) if is_copy else []
        topic_label = source_text if len(source_text) <= 42 else source_text[:42] + "..."
        style_guide = (
            "Modern 3D isometric knowledge-explainer visuals; deep navy blue background "
            "with amber gold and teal accents; soft diffused lighting and gentle "
            "geometric glow; clean composition with a clear focal point and a calm "
            "lower third; may include short Chinese text labels or a simple logic "
            "diagram; no logos or watermarks."
        )

        titles = [
            f"认识：{topic_label}",
            "为什么值得关注",
            "核心内容拆解",
            "从理解到实践",
            "回顾与行动",
        ]
        points = [
            ["明确主题范围", "建立整体认知"],
            ["理解问题背景", "发现关键价值"],
            ["梳理核心概念", "形成结构脉络"],
            ["连接真实场景", "找到落地步骤"],
            ["回看五个要点", "沉淀下一步行动"],
        ]
        visual_scenes = [
            "a glowing glass cube on a dark navy desk, surrounded by five translucent spheres connected by thin light lines, three-quarter view, soft studio lighting",
            "a hand holding a glowing compass above a tangled wire maze on a dark desk, warm key light, shallow depth of field",
            "three stacked translucent glass panels on a dark desk, each holding different-sized spheres, light beams passing through, isometric view",
            "a winding path of glowing stepping stones from a dark foreground to a bright doorway, isometric view, warm accents",
            "five glowing rings converging into one bright sphere on a dark reflective floor, soft rim light, symmetrical composition",
        ]

        pages: list[StoryboardPage] = []
        for i in range(5):
            if is_copy and i < len(chunks):
                body = chunks[i].strip(" ，,；;\n")
                title = titles[i]
                page_points = [_ellipsis(body, 22), points[i][1]]
                narration = body if body.endswith(("。", "！", "？")) else body + "。"
            else:
                title = titles[i]
                page_points = points[i]
                narration = _template_narration(i, topic_label)
            narration = _ellipsis(narration, 190)
            pages.append(
                StoryboardPage(
                    index=i + 1,
                    title=title,
                    key_points=page_points,
                    visual_prompt=(
                        f"{style_guide} Slide {i + 1} subject: {visual_scenes[i]}. "
                        "Include a short Chinese title or a few keyword labels if helpful."
                    ),
                    narration=narration,
                )
            )
        storyboard = Storyboard(
            topic=source_text,
            style_guide=style_guide,
            pages=pages,
            planner="local-template",
        )
        storyboard.validate()
        self.logger.info("使用本地分镜模板；演示画面会明确标注为非 AI 生图")
        return storyboard


def _template_narration(index: int, topic: str) -> str:
    texts = [
        f"本期用五个画面，快速了解“{topic}”。先建立整体认识，再逐步看清它的价值和应用方式。",
        f"理解“{topic}”为什么值得关注，重点不只在于概念本身，更在于它回应了哪些真实问题。",
        f"把“{topic}”拆成几个关键部分来看，有助于我们抓住核心，而不是停留在零散信息上。",
        f"从理解走向实践，需要把概念连接场景、方法和行动。每一步都应当有明确目标，并能被检验。",
        f"最后回顾这五页：认识主题、理解价值、拆解内容、连接实践，并形成下一步行动。",
    ]
    return texts[index]


def _split_balanced(text: str, parts: int) -> list[str]:
    normalized = re.sub(r"[ \t]+", " ", text).strip()
    if len(normalized) <= parts:
        return [normalized[i::parts] for i in range(parts)]
    sentences = [
        item.strip()
        for item in re.split(r"(?<=[。！？!?；;\n])", normalized)
        if item.strip()
    ]
    target = max(1, len(normalized) // parts)
    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        if current and len(current) + len(sentence) > target and len(chunks) < parts - 1:
            chunks.append(current.strip())
            current = sentence
        else:
            current += sentence
    if current.strip():
        chunks.append(current.strip())
    while len(chunks) < parts:
        chunks.append("")
    if len(chunks) > parts:
        chunks = chunks[: parts - 1] + ["".join(chunks[parts - 1 :])]
    return chunks


def _ellipsis(text: str, limit: int) -> str:
    clean = re.sub(r"\s+", " ", text).strip()
    if len(clean) <= limit:
        return clean
    return clean[: max(1, limit - 1)].rstrip("，,；;。.") + "…"

