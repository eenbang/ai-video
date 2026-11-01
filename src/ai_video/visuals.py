from __future__ import annotations

import logging
from pathlib import Path

from .config import Settings
from .image_backends import (
    ComfyUIImageBackend,
    DemoImageBackend,
    ImageBackend,
    OpenAIImageBackend,
)
from .models import StoryboardPage


class VisualService:
    """图像服务门面，按配置选择后端并保持逐页生成接口不变。"""

    def __init__(
        self,
        settings: Settings,
        logger: logging.Logger,
        task_id: str,
    ) -> None:
        self.settings = settings
        self.logger = logger
        self.task_id = task_id
        self._backend = _resolve_backend(settings, logger, task_id)

    def generate(
        self,
        page: StoryboardPage,
        style_guide: str,
        destination: Path,
    ) -> str:
        destination.parent.mkdir(parents=True, exist_ok=True)
        return self._backend.generate(page, style_guide, destination)


def _resolve_backend(
    settings: Settings,
    logger: logging.Logger,
    task_id: str,
) -> ImageBackend:
    backend = settings.image_backend
    if backend == "demo":
        return DemoImageBackend(settings, logger, task_id)
    if backend == "openai":
        if settings.can_use_api:
            return OpenAIImageBackend(settings, logger, task_id)
        if settings.mode == "api":
            raise RuntimeError("image_backend=openai 需要设置 OPENAI_API_KEY")
        logger.warning("未配置 API Key，生图回退到本地演示画面")
        return DemoImageBackend(settings, logger, task_id)
    if backend == "comfyui":
        return ComfyUIImageBackend(settings, logger, task_id)

    raise ValueError(f"不支持的图像后端：{backend}")
