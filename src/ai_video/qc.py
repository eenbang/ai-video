"""生图后的图文相关性质检：用视觉大模型给每页画面打分，不合格则给出改进建议。"""

from __future__ import annotations

import base64
import json
import logging
import re
from pathlib import Path
from typing import Any

from .config import Settings
from .http_client import post_json
from .models import StoryboardPage


class ImageQcService:
    """调用视觉大模型，判断一张画面是否贴合该页的文字内容。"""

    def __init__(self, settings: Settings, logger: logging.Logger) -> None:
        self.settings = settings
        self.logger = logger

    def check(self, image_path: Path, page: StoryboardPage) -> dict[str, Any]:
        data_uri = _image_data_uri(image_path)
        system = (
            "你是严格的图文质检员。根据页面文字内容，判断这张画面是否贴题。"
            "只输出 JSON。"
        )
        points = "；".join(page.key_points[:3])
        user_text = (
            f"页面标题：{page.title}\n"
            f"要点：{points}\n"
            f"解说词：{page.narration}\n\n"
            "请从 0 到 10 给这张画面打分：是否与标题和要点相关、主体是否清楚、"
            "构图是否合理、画面里的中文文字是否清晰无乱码。"
            "输出 JSON：{\"score\": 数字, \"reason\": \"一句话说明\", "
            "\"suggestion\": \"如果不合格，给出改进方向；合格则为空字符串\"}"
        )
        payload: dict[str, Any] = {
            "model": self.settings.qc_model,
            "messages": [
                {"role": "system", "content": system},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": user_text},
                        {
                            "type": "image_url",
                            "image_url": {"url": data_uri},
                        },
                    ],
                },
            ],
            "temperature": 0.2,
        }
        if self.settings.is_siliconflow:
            payload["response_format"] = {"type": "json_object"}
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
        value = _parse_json(str(content))
        score = _to_float(value.get("score"), 0.0)
        score = max(0.0, min(10.0, score))
        return {
            "score": round(score, 1),
            "pass": score >= self.settings.qc_min_score,
            "reason": str(value.get("reason", "")).strip(),
            "suggestion": str(value.get("suggestion", "")).strip(),
        }


def _image_data_uri(image_path: Path) -> str:
    raw = image_path.read_bytes()
    encoded = base64.b64encode(raw).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def _to_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_json(content: str) -> dict[str, Any]:
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("返回的 JSON 不是对象")
    return value
