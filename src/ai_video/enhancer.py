from __future__ import annotations

import json
import logging
import re
from typing import Any

from .config import Settings
from .http_client import post_json
from .models import Storyboard


ENHANCE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "prompts": {
            "type": "array",
            "minItems": 5,
            "maxItems": 5,
            "items": {"type": "string"},
        }
    },
    "required": ["prompts"],
}


class PromptEnhancer:
    """生图前把每页画面描述扩写成具体、可画的英文提示词。"""

    def __init__(self, settings: Settings, logger: logging.Logger) -> None:
        self.settings = settings
        self.logger = logger

    def enhance(self, storyboard: Storyboard) -> str | None:
        if not self.settings.prompt_enhance:
            self.logger.info("提示词扩充已关闭（PROMPT_ENHANCE=false）")
            return None
        if self.settings.image_backend == "demo":
            self.logger.info("演示画面不需要提示词扩充")
            return None
        if not self.settings.can_use_api:
            self.logger.info("未配置 API Key，跳过提示词扩充")
            return None
        try:
            prompts = self._remote_enhance(storyboard)
        except Exception as exc:
            self.logger.warning("提示词扩充失败，沿用原提示词：%s", exc)
            return None
        for page, prompt in zip(storyboard.pages, prompts):
            text = prompt.strip()
            if text:
                page.image_prompt = text
        model = self.settings.prompt_enhance_model
        self.logger.info("提示词扩充完成，模型=%s", model)
        return f"api:{model}"

    def _remote_enhance(self, storyboard: Storyboard) -> list[str]:
        system = (
            "你是资深的图像提示词工程师。把每页的简要画面描述扩写成一条详细、"
            "具体、可直接生成的英文 image prompt。每条必须包含：具体主体"
            "（物体或人物或场景）、动作或状态、环境与背景、构图与镜头"
            "（远景、中景、特写、俯视等）、光线与材质、整体风格。"
            "避免抽象词：不要用 symbolizing、representing、metaphor、concept "
            "这类说法，改写成真实可见的场景。五页要有构图变化，不要重复同一种"
            "画面，同时保持统一的配色和风格。画面内可以包含少量中文文字"
            "（短标题、关键词标签、图表标注或简短逻辑图），文字要简短准确，"
            "不要出现 logo 或水印。只输出 JSON。"
        )
        page_lines: list[str] = []
        for page in storyboard.pages:
            points = "；".join(page.key_points[:3])
            page_lines.append(
                f"第{page.index}页 标题：{page.title}；要点：{points}；"
                f"现画面：{page.visual_prompt}"
            )
        user = (
            f"主题：{storyboard.topic}\n"
            f"统一视觉规范：{storyboard.style_guide}\n"
            "请扩写以下 5 页画面，每页输出一条英文 image prompt：\n"
            + "\n".join(page_lines)
            + '\n只输出 JSON，格式为 {"prompts": ["第1条", "第2条", "第3条", "第4条", "第5条"]}。'
        )
        if self.settings.is_siliconflow:
            response_format: dict[str, Any] = {"type": "json_object"}
        else:
            response_format = {
                "type": "json_schema",
                "json_schema": {
                    "name": "image_prompts",
                    "strict": True,
                    "schema": ENHANCE_SCHEMA,
                },
            }
        payload = {
            "model": self.settings.prompt_enhance_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.6,
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
        content = result["choices"][0]["message"]["content"]
        if isinstance(content, list):
            content = "".join(
                item.get("text", "") for item in content if isinstance(item, dict)
            )
        value = _parse_json_content(str(content))
        raw_prompts = value.get("prompts")
        if not isinstance(raw_prompts, list):
            raise RuntimeError("提示词扩充返回的数据结构不符合要求")
        merged: list[str] = []
        for index, page in enumerate(storyboard.pages):
            text = str(raw_prompts[index]).strip() if index < len(raw_prompts) else ""
            merged.append(text or page.visual_prompt)
        return merged


def _parse_json_content(content: str) -> dict[str, Any]:
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("返回的 JSON 不是对象")
    return value
