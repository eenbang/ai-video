from __future__ import annotations

import asyncio
import logging
import subprocess
import tempfile
from pathlib import Path

from .config import Settings


class SpeechService:
    def __init__(
        self,
        settings: Settings,
        logger: logging.Logger,
        ffmpeg_executable: str,
    ) -> None:
        self.settings = settings
        self.logger = logger
        self.ffmpeg_executable = ffmpeg_executable

    def synthesize(self, text: str, destination: Path) -> str:
        destination.parent.mkdir(parents=True, exist_ok=True)
        self._edge_tts(text, destination)
        return "edge-tts"

    def _edge_tts(self, text: str, destination: Path) -> None:
        try:
            import edge_tts
        except ImportError as exc:
            raise RuntimeError(
                "演示模式需要 edge-tts，请先执行 pip install -e ."
            ) from exc
        last_error: Exception | None = None
        for attempt in range(1, 3):
            try:
                with tempfile.TemporaryDirectory(
                    prefix="ai-video-edge-tts-"
                ) as temp_dir:
                    mp3_path = Path(temp_dir) / "speech.mp3"

                    async def save_speech() -> None:
                        communicate = edge_tts.Communicate(
                            text=text,
                            voice=self.settings.edge_tts_voice,
                            rate=self.settings.edge_tts_rate,
                        )
                        await communicate.save(str(mp3_path))

                    asyncio.run(asyncio.wait_for(save_speech(), timeout=60.0))
                    if not mp3_path.is_file() or mp3_path.stat().st_size == 0:
                        raise RuntimeError("Edge TTS 未生成音频")
                    self._normalize_wav(mp3_path, destination)
                self.logger.info("Edge TTS 完成，%d 字", len(text))
                return
            except Exception as exc:
                last_error = exc
                if attempt < 2:
                    self.logger.warning(
                        "Edge TTS 第 %d 次失败，重试：%s", attempt, exc
                    )
        raise RuntimeError(f"Edge TTS 失败：{last_error}")

    def _normalize_wav(self, source: Path, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        command = [
            self.ffmpeg_executable,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-ar",
            "48000",
            "-ac",
            "1",
            str(destination),
        ]
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(f"音频标准化失败：{completed.stderr.strip()}")

