from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


class StoryboardError(ValueError):
    """分镜不符合 MVP 约束。"""


@dataclass(slots=True)
class StoryboardPage:
    index: int
    title: str
    key_points: list[str]
    visual_prompt: str
    narration: str
    image_prompt: str = ""
    image_file: str = ""
    audio_file: str = ""
    segment_file: str = ""
    audio_duration_seconds: float = 0.0
    display_duration_seconds: float = 0.0
    start_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "StoryboardPage":
        points = value.get("key_points", [])
        if not isinstance(points, list):
            points = []
        return cls(
            index=int(value.get("index", 0)),
            title=str(value.get("title", "")).strip(),
            key_points=[str(item).strip() for item in points if str(item).strip()][:3],
            visual_prompt=str(value.get("visual_prompt", "")).strip(),
            narration=str(value.get("narration", "")).strip(),
            image_prompt=str(value.get("image_prompt", "")).strip(),
            image_file=str(value.get("image_file", "")),
            audio_file=str(value.get("audio_file", "")),
            segment_file=str(value.get("segment_file", "")),
            audio_duration_seconds=float(value.get("audio_duration_seconds", 0)),
            display_duration_seconds=float(value.get("display_duration_seconds", 0)),
            start_seconds=float(value.get("start_seconds", 0)),
        )


@dataclass(slots=True)
class Storyboard:
    topic: str
    style_guide: str
    pages: list[StoryboardPage] = field(default_factory=list)
    planner: str = "local-template"

    def validate(self) -> None:
        if len(self.pages) != 5:
            raise StoryboardError(f"分镜必须恰好为 5 页，当前为 {len(self.pages)} 页")
        if not self.topic.strip():
            raise StoryboardError("主题不能为空")
        for expected, page in enumerate(self.pages, start=1):
            if page.index != expected:
                raise StoryboardError(f"第 {expected} 页序号错误：{page.index}")
            if not page.title:
                raise StoryboardError(f"第 {expected} 页缺少标题")
            if not page.visual_prompt:
                raise StoryboardError(f"第 {expected} 页缺少画面提示词")
            if not page.narration:
                raise StoryboardError(f"第 {expected} 页缺少解说词")
            if len(page.narration) > 240:
                raise StoryboardError(f"第 {expected} 页解说词过长，需控制在 240 字以内")

    def to_dict(self) -> dict[str, Any]:
        return {
            "topic": self.topic,
            "style_guide": self.style_guide,
            "planner": self.planner,
            "pages": [page.to_dict() for page in self.pages],
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Storyboard":
        raw_pages = value.get("pages", [])
        storyboard = cls(
            topic=str(value.get("topic", "")).strip(),
            style_guide=str(value.get("style_guide", "")).strip(),
            planner=str(value.get("planner", "unknown")),
            pages=[
                StoryboardPage.from_dict(page)
                for page in raw_pages
                if isinstance(page, dict)
            ],
        )
        storyboard.validate()
        return storyboard

