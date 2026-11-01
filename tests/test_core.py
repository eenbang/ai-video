from __future__ import annotations

import logging
import json
import tempfile
import unittest
import wave
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from ai_video.config import Settings
from ai_video.image_backends import ComfyUIImageBackend, DemoImageBackend
from ai_video.media import MediaService, _srt_time
from ai_video.models import Storyboard, StoryboardError, StoryboardPage
from ai_video.pipeline import VideoPipeline
from ai_video.planner import PlanningService
from ai_video.visuals import VisualService


def workspace_tempdir() -> tempfile.TemporaryDirectory[str]:
    return tempfile.TemporaryDirectory(
        dir=Path(__file__).resolve().parents[1],
        prefix=".test-",
    )


class _FakeSpeech:
    """生成 0.25 秒静音 WAV，避免测试依赖外部 TTS。"""

    def __init__(self, settings: Settings, logger: logging.Logger, ffmpeg: str) -> None:
        self.settings = settings
        self.logger = logger
        self.ffmpeg = ffmpeg

    def synthesize(self, text: str, destination: Path) -> str:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(destination), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(48000)
            handle.writeframes(b"\x00\x00" * 12000)
        return "fake-tts"


class CoreTests(unittest.TestCase):
    def test_local_plan_has_exactly_five_valid_pages(self) -> None:
        with workspace_tempdir() as temp_dir:
            settings = Settings.load("demo", temp_dir)
            planner = PlanningService(settings, logging.getLogger("test"))
            storyboard = planner.create("大模型如何改变软件工程")
            storyboard.validate()
            self.assertEqual(storyboard.planner, "local-template")
            self.assertEqual(len(storyboard.pages), 5)
            self.assertEqual([page.index for page in storyboard.pages], [1, 2, 3, 4, 5])
            self.assertTrue(all(page.narration for page in storyboard.pages))

    def test_long_copy_is_split_into_five_narrations(self) -> None:
        with workspace_tempdir() as temp_dir:
            settings = Settings.load("demo", temp_dir)
            planner = PlanningService(settings, logging.getLogger("test"))
            source = (
                "人工智能正在改变软件工程。需求分析可以借助模型完成初步归纳。"
                "代码生成能够减少重复劳动。测试阶段可以自动发现常见缺陷。"
                "部署阶段可以通过智能监控辅助定位异常。团队仍然需要人工复核。"
            )
            storyboard = planner.create(source)
            self.assertEqual(len(storyboard.pages), 5)
            self.assertEqual(len({page.narration for page in storyboard.pages}), 5)

    def test_storyboard_rejects_wrong_page_count(self) -> None:
        storyboard = Storyboard(topic="测试", style_guide="风格")
        with self.assertRaises(StoryboardError):
            storyboard.validate()

    def test_srt_timestamp(self) -> None:
        self.assertEqual(_srt_time(0), "00:00:00,000")
        self.assertEqual(_srt_time(65.123), "00:01:05,123")
        self.assertEqual(_srt_time(3661.5), "01:01:01,500")

    def test_media_creates_missing_output_directories(self) -> None:
        with workspace_tempdir() as temp_dir:
            root = Path(temp_dir)
            settings = Settings.load("demo", temp_dir)
            media = MediaService(settings, logging.getLogger("test"))

            segment = root / "nested" / "segments" / "page.mp4"
            with patch.object(media, "_run"):
                media.compose_page(
                    root / "source.png",
                    root / "source.wav",
                    segment,
                    1.0,
                )
            self.assertTrue(segment.parent.is_dir())

            joined = root / "joined" / "video.mp4"
            with patch.object(media, "_run"):
                media.concatenate([segment], joined)
            self.assertTrue(joined.parent.is_dir())
            self.assertTrue((joined.parent / "concat.txt").is_file())

    def test_visual_service_uses_demo_backend_in_demo_mode(self) -> None:
        with workspace_tempdir() as temp_dir:
            settings = Settings.load("demo", temp_dir, "demo")
            service = VisualService(settings, logging.getLogger("test"), "task-1")
            self.assertEqual(settings.image_backend, "demo")
            self.assertIsInstance(service._backend, DemoImageBackend)

    def test_cli_image_backend_override(self) -> None:
        with workspace_tempdir() as temp_dir:
            settings = Settings.load("demo", temp_dir, "comfyui")
            self.assertEqual(settings.image_backend, "comfyui")

    def test_full_pipeline_with_fake_tts(self) -> None:
        with workspace_tempdir() as temp_dir:
            settings = Settings.load("demo", temp_dir, "demo")
            pipeline = VideoPipeline(settings)
            with patch("ai_video.pipeline.SpeechService", _FakeSpeech):
                result = pipeline.run("流水线分工验证")

            manifest = json.loads(
                (Path(result.task_dir) / "manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertTrue(result.video_path.is_file())
            self.assertTrue((Path(result.task_dir) / "subtitles.srt").is_file())
            self.assertEqual(manifest["status"], "completed")
            self.assertEqual(manifest["providers"]["image"], "local-demo")
            self.assertEqual(manifest["providers"]["tts"], "fake-tts")

    def test_comfyui_workflow_placeholders_are_replaced(self) -> None:
        with workspace_tempdir() as temp_dir:
            root = Path(temp_dir)
            workflow_file = root / "workflow_api.json"
            workflow_file.write_text(
                '{"3": {"inputs": {"text": {{prompt}}, '
                '"width": {{width}}, "height": {{height}}, "seed": {{seed}}}}}',
                encoding="utf-8",
            )
            settings = replace(
                Settings.load("demo", temp_dir),
                image_backend="comfyui",
                comfyui_workflow_file=workflow_file,
                comfyui_output_node_id="3",
            )
            backend = ComfyUIImageBackend(
                settings,
                logging.getLogger("test"),
                "task-2",
            )
            page = StoryboardPage(
                index=1,
                title="标题",
                key_points=["要点一"],
                visual_prompt="一个主题相关画面",
                narration="解说词。",
            )
            graph = backend._load_workflow(page, "统一视觉风格")
            node_inputs = graph["3"]["inputs"]
            self.assertIn("统一视觉风格", node_inputs["text"])
            self.assertEqual(node_inputs["width"], 1920)
            self.assertEqual(node_inputs["height"], 1080)
            self.assertGreater(node_inputs["seed"], 0)


if __name__ == "__main__":
    unittest.main()
