from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from PIL import Image

from .config import Settings
from .enhancer import PromptEnhancer
from .lecture import ImageLectureService, build_slides_from_image
from .media import MediaService
from .models import Storyboard, StoryboardPage
from .planner import PlanningService
from .qc import ImageQcService
from .tts import SpeechService
from .visuals import VisualService


ProgressCallback = Callable[[int, int, str], None]


@dataclass(frozen=True, slots=True)
class PipelineResult:
    task_id: str
    task_dir: Path
    video_path: Path
    manifest_path: Path


class VideoPipeline:
    total_steps = 10

    def __init__(
        self,
        settings: Settings,
        progress: ProgressCallback | None = None,
    ) -> None:
        self.settings = settings
        self.progress = progress or (lambda step, total, message: None)
        self._usage = {"image_generations": 0, "qc_calls": 0, "redraws": 0}

    def run(
        self,
        source_text: str,
        product_image: Path | None = None,
    ) -> PipelineResult:
        source_text = source_text.strip()
        if not source_text:
            raise ValueError("主题或文案不能为空")

        created_at = datetime.now().astimezone()
        task_id = _make_task_id(created_at, source_text)
        task_dir = self.settings.output_dir / task_id
        task_dir.mkdir(parents=True, exist_ok=False)
        return self._execute(
            source_text, task_dir, task_id, resume=False, product_image=product_image
        )

    def run_in(
        self,
        source_text: str,
        task_dir: Path,
        task_id: str,
        product_image: Path | None = None,
    ) -> PipelineResult:
        """在指定的任务目录中运行完整流程，供 Web 端预先分配任务 ID。"""
        source_text = source_text.strip()
        if not source_text:
            raise ValueError("主题或文案不能为空")
        task_dir.mkdir(parents=True, exist_ok=False)
        return self._execute(
            source_text, task_dir, task_id, resume=False, product_image=product_image
        )

    def resume(self, task_dir: Path) -> PipelineResult:
        task_dir = task_dir.resolve()
        manifest_path = task_dir / "manifest.json"
        input_path = task_dir / "input.json"
        if not manifest_path.is_file() or not input_path.is_file():
            raise ValueError(
                f"任务目录缺少 manifest.json 或 input.json，无法续跑：{task_dir}"
            )
        try:
            manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
            input_data = json.loads(input_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"无法读取任务文件：{exc}") from exc
        task_id = str(manifest_data.get("task_id") or task_dir.name)
        source_text = str(input_data.get("source_text", "")).strip()
        if not source_text:
            raise ValueError("任务缺少原始输入，无法续跑")
        product_image = _resolve_product_image(input_data.get("product_image"))
        return self._execute(
            source_text, task_dir, task_id, resume=True, product_image=product_image
        )

    def run_image_lecture(self, image_path: Path) -> PipelineResult:
        """把一张产品图拆成 5 页，逐页配音讲解，合成类似 PPT 的展示视频。"""
        image_path = image_path.expanduser().resolve()
        if not image_path.is_file():
            raise ValueError(f"产品图片不存在：{image_path}")
        created_at = datetime.now().astimezone()
        task_id = _make_task_id(created_at, f"image:{image_path.stem}")
        task_dir = self.settings.output_dir / task_id
        return self.run_image_lecture_in(image_path, task_dir, task_id)

    def run_image_lecture_in(
        self,
        image_path: Path,
        task_dir: Path,
        task_id: str,
    ) -> PipelineResult:
        """在指定任务目录中运行单图讲解，供 Web 端预先分配任务 ID。"""
        image_path = image_path.expanduser().resolve()
        if not image_path.is_file():
            raise ValueError(f"产品图片不存在：{image_path}")
        task_dir.mkdir(parents=True, exist_ok=False)
        return self._execute_image_lecture(image_path, task_dir, task_id)

    def _execute(
        self,
        source_text: str,
        task_dir: Path,
        task_id: str,
        resume: bool,
        product_image: Path | None = None,
    ) -> PipelineResult:
        logger = _task_logger(task_dir, task_id)
        self._usage = {"image_generations": 0, "qc_calls": 0, "redraws": 0}
        manifest_path = task_dir / "manifest.json"
        status_path = task_dir / "task_status.json"
        started = datetime.now().astimezone()

        if resume and manifest_path.is_file():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["status"] = "running"
            manifest["error"] = None
        else:
            manifest = {
                "task_id": task_id,
                "status": "running",
                "created_at": started.isoformat(),
                "source_text": source_text,
                "product_image": str(product_image) if product_image else None,
                "mode_requested": self.settings.mode,
                "providers": {},
                "artifacts": {},
                "cost_estimate": {},
                "error": None,
            }
        _write_json(manifest_path, manifest)

        try:
            self._step(1, "保存输入与运行配置")
            if not (task_dir / "input.json").is_file():
                _write_json(
                    task_dir / "input.json",
                    {
                        "task_id": task_id,
                        "source_text": source_text,
                        "product_image": (
                            str(product_image) if product_image else None
                        ),
                        "created_at": manifest.get(
                            "created_at", started.isoformat()
                        ),
                    },
                )
            _write_json(
                task_dir / "run_config.json",
                _safe_config(self.settings),
            )
            logger.info(
                "%s task_id=%s mode=%s",
                "续跑" if resume else "任务开始",
                task_id,
                self.settings.mode,
            )

            storyboard = self._load_or_plan(
                source_text,
                task_dir,
                manifest,
                logger,
            )

            self._step(3, "扩充画面提示词，提升生图贴题度")
            self._enhance_prompts(storyboard, task_dir, manifest, logger)

            self._step(4, "生成 5 张风格一致的主题画面")
            self._generate_images(
                storyboard,
                task_dir,
                manifest,
                logger,
                task_id,
                product_image,
            )

            self._step(5, "逐页生成中文人声配音")
            self._synthesize_speech(
                storyboard,
                task_dir,
                manifest,
                logger,
            )

            video_path = self._compose_video(storyboard, task_dir, logger)

            self._step(10, "写入清单、费用估算与最终状态")
            character_count = sum(len(page.narration) for page in storyboard.pages)
            usage = self._usage
            estimated_cost = {
                "currency": "CNY",
                "kind": "user_supplied_estimate",
                "planner_calls": 1,
                "images": usage["image_generations"],
                "qc_calls": usage["qc_calls"],
                "redraws": usage["redraws"],
                "tts_characters": character_count,
                "planner": round(self.settings.est_planner_cny_per_call, 4),
                "image": round(
                    self.settings.est_image_cny_per_image
                    * usage["image_generations"],
                    4,
                ),
                "tts": round(
                    self.settings.est_tts_cny_per_1k_chars
                    * character_count
                    / 1000,
                    4,
                ),
                "qc": round(
                    self.settings.est_qc_cny_per_call * usage["qc_calls"],
                    4,
                ),
            }
            estimated_cost["total"] = round(
                estimated_cost["planner"]
                + estimated_cost["image"]
                + estimated_cost["tts"]
                + estimated_cost["qc"],
                4,
            )
            finished = datetime.now().astimezone()
            manifest.update(
                {
                    "status": "completed",
                    "finished_at": finished.isoformat(),
                    "elapsed_seconds": round((finished - started).total_seconds(), 2),
                    "page_count": len(storyboard.pages),
                    "total_duration_seconds": round(
                        sum(page.display_duration_seconds for page in storyboard.pages),
                        3,
                    ),
                    "cost_estimate": estimated_cost,
                    "artifacts": {
                        "video": "video.mp4",
                        "subtitles": "subtitles.srt",
                        "storyboard": "storyboard.json",
                        "quality_report": "quality_report.json",
                        "images": [
                            page.image_file for page in storyboard.pages
                        ],
                        "audio": [page.audio_file for page in storyboard.pages],
                        "segments": [
                            page.segment_file for page in storyboard.pages
                        ],
                        "log": "pipeline.log",
                    },
                }
            )
            _write_json(manifest_path, manifest)
            _write_json(
                status_path,
                {
                    "task_id": task_id,
                    "status": "completed",
                    "step": self.total_steps,
                    "total_steps": self.total_steps,
                    "message": "完成",
                    "updated_at": finished.isoformat(),
                },
            )
            logger.info(
                "任务完成 video=%s duration=%.2fs",
                video_path,
                manifest["total_duration_seconds"],
            )
            return PipelineResult(task_id, task_dir, video_path, manifest_path)
        except Exception as exc:
            failed_at = datetime.now().astimezone()
            manifest["status"] = "failed"
            manifest["finished_at"] = failed_at.isoformat()
            manifest["error"] = {
                "type": type(exc).__name__,
                "message": str(exc),
            }
            _write_json(manifest_path, manifest)
            _write_json(
                status_path,
                {
                    "task_id": task_id,
                    "status": "failed",
                    "message": str(exc),
                    "updated_at": failed_at.isoformat(),
                },
            )
            logger.exception("任务失败")
            raise
        finally:
            for handler in list(logger.handlers):
                handler.close()
                logger.removeHandler(handler)

    def _execute_image_lecture(
        self,
        image_path: Path,
        task_dir: Path,
        task_id: str,
    ) -> PipelineResult:
        logger = _task_logger(task_dir, task_id)
        self._usage = {"image_generations": 0, "qc_calls": 0, "redraws": 0}
        manifest_path = task_dir / "manifest.json"
        status_path = task_dir / "task_status.json"
        started = datetime.now().astimezone()
        manifest = {
            "task_id": task_id,
            "status": "running",
            "created_at": started.isoformat(),
            "source_text": image_path.name,
            "product_image": str(image_path),
            "kind": "image_lecture",
            "mode_requested": self.settings.mode,
            "providers": {},
            "artifacts": {},
            "cost_estimate": {},
            "error": None,
        }
        _write_json(manifest_path, manifest)
        try:
            self._step(1, "保存输入与运行配置")
            _write_json(
                task_dir / "input.json",
                {
                    "task_id": task_id,
                    "source_text": image_path.name,
                    "product_image": str(image_path),
                    "kind": "image_lecture",
                    "created_at": manifest["created_at"],
                },
            )
            _write_json(task_dir / "run_config.json", _safe_config(self.settings))

            self._step(2, "把产品图拆分为 5 页画面")
            slides = build_slides_from_image(image_path, self.settings, task_dir)

            self._step(3, "为每一页生成中文讲解词")
            service = ImageLectureService(self.settings, logger)
            entries = service.plan(slides)
            manifest["providers"]["planner"] = (
                f"api:{self.settings.qc_model}"
                if self.settings.use_api()
                else "template"
            )

            pages = []
            for slide, entry in zip(slides, entries):
                pages.append(
                    StoryboardPage(
                        index=slide.index,
                        title=entry["title"],
                        key_points=[entry["title"]],
                        visual_prompt=entry["narration"],
                        narration=entry["narration"],
                        image_file=_relative(slide.slide_path, task_dir),
                    )
                )
            storyboard = Storyboard(
                topic=image_path.name,
                style_guide="product image lecture",
                pages=pages,
                planner="image-lecture",
            )
            storyboard.validate()
            _write_json(task_dir / "storyboard.json", storyboard.to_dict())

            self._step(4, "逐页生成中文人声配音")
            self._synthesize_speech(storyboard, task_dir, manifest, logger)

            video_path = self._compose_video(storyboard, task_dir, logger)

            self._step(10, "写入清单、费用估算与最终状态")
            character_count = sum(len(page.narration) for page in storyboard.pages)
            vision_calls = len(storyboard.pages) if self.settings.use_api() else 0
            estimated_cost = {
                "currency": "CNY",
                "kind": "user_supplied_estimate",
                "planner_calls": 1,
                "images": 0,
                "qc_calls": 0,
                "redraws": 0,
                "vision_calls": vision_calls,
                "tts_characters": character_count,
                "planner": 0.0,
                "image": 0.0,
                "tts": round(
                    self.settings.est_tts_cny_per_1k_chars * character_count / 1000,
                    4,
                ),
                "qc": 0.0,
                "vision": round(
                    self.settings.est_qc_cny_per_call * vision_calls, 4
                ),
            }
            estimated_cost["total"] = round(
                estimated_cost["planner"]
                + estimated_cost["image"]
                + estimated_cost["tts"]
                + estimated_cost["qc"]
                + estimated_cost["vision"],
                4,
            )
            finished = datetime.now().astimezone()
            manifest.update(
                {
                    "status": "completed",
                    "finished_at": finished.isoformat(),
                    "elapsed_seconds": round((finished - started).total_seconds(), 2),
                    "page_count": len(storyboard.pages),
                    "total_duration_seconds": round(
                        sum(page.display_duration_seconds for page in storyboard.pages),
                        3,
                    ),
                    "cost_estimate": estimated_cost,
                    "artifacts": {
                        "video": "video.mp4",
                        "subtitles": "subtitles.srt",
                        "storyboard": "storyboard.json",
                        "quality_report": "quality_report.json",
                        "images": [page.image_file for page in storyboard.pages],
                        "audio": [page.audio_file for page in storyboard.pages],
                        "segments": [page.segment_file for page in storyboard.pages],
                        "log": "pipeline.log",
                    },
                }
            )
            _write_json(manifest_path, manifest)
            _write_json(
                status_path,
                {
                    "task_id": task_id,
                    "status": "completed",
                    "step": self.total_steps,
                    "total_steps": self.total_steps,
                    "message": "完成",
                    "updated_at": finished.isoformat(),
                },
            )
            logger.info("单图讲解完成 video=%s", video_path)
            return PipelineResult(task_id, task_dir, video_path, manifest_path)
        except Exception as exc:  # noqa: BLE001
            failed_at = datetime.now().astimezone()
            manifest["status"] = "failed"
            manifest["finished_at"] = failed_at.isoformat()
            manifest["error"] = {"type": type(exc).__name__, "message": str(exc)}
            _write_json(manifest_path, manifest)
            _write_json(
                status_path,
                {
                    "task_id": task_id,
                    "status": "failed",
                    "message": str(exc),
                    "updated_at": failed_at.isoformat(),
                },
            )
            logger.exception("单图讲解任务失败")
            raise
        finally:
            for handler in list(logger.handlers):
                handler.close()
                logger.removeHandler(handler)

    def _plan_text(
        self,
        source_text: str,
        task_dir: Path,
        manifest: dict[str, Any],
        logger: logging.Logger,
    ) -> Storyboard:
        """阶段一：生成文字，即主题/文案 -> 恰好 5 页分镜与解说词。"""
        planner = PlanningService(self.settings, logger)
        storyboard = planner.create(source_text)
        storyboard.validate()
        manifest["providers"]["planner"] = storyboard.planner
        _write_json(task_dir / "storyboard.json", storyboard.to_dict())
        return storyboard

    def _load_or_plan(
        self,
        source_text: str,
        task_dir: Path,
        manifest: dict[str, Any],
        logger: logging.Logger,
    ) -> Storyboard:
        """续跑时优先加载已有分镜；不可用则重新规划。"""
        storyboard_path = task_dir / "storyboard.json"
        if storyboard_path.is_file():
            try:
                storyboard = Storyboard.from_dict(
                    json.loads(storyboard_path.read_text(encoding="utf-8"))
                )
                storyboard.validate()
                manifest.setdefault("providers", {})["planner"] = storyboard.planner
                logger.info("续跑：加载已有分镜，planner=%s", storyboard.planner)
                return storyboard
            except Exception as exc:
                logger.warning("已有分镜不可用，重新规划：%s", exc)
        self._step(2, "理解主题并生成恰好 5 页分镜")
        return self._plan_text(source_text, task_dir, manifest, logger)

    def _enhance_prompts(
        self,
        storyboard: Storyboard,
        task_dir: Path,
        manifest: dict[str, Any],
        logger: logging.Logger,
    ) -> None:
        """阶段一补充：生图前把画面描述扩写得更具体。"""
        if all(page.image_prompt for page in storyboard.pages):
            logger.info("画面提示词已扩充，跳过")
            return
        enhancer = PromptEnhancer(self.settings, logger)
        provider = enhancer.enhance(storyboard)
        if provider:
            manifest["providers"]["prompt_enhancer"] = provider
        _write_json(task_dir / "storyboard.json", storyboard.to_dict())

    def _generate_images(
        self,
        storyboard: Storyboard,
        task_dir: Path,
        manifest: dict[str, Any],
        logger: logging.Logger,
        task_id: str,
        product_image: Path | None = None,
    ) -> None:
        """阶段二：生图，按分镜逐页生成五张画面。"""
        visuals = VisualService(self.settings, logger, task_id)
        qc_active = (
            self.settings.qc_enabled
            and self.settings.can_use_api
            and self.settings.image_backend in {"openai", "comfyui"}
        )
        qc = ImageQcService(self.settings, logger) if qc_active else None
        image_providers: list[str] = []
        qc_entries: list[dict[str, Any]] = []
        for page in storyboard.pages:
            image_path = task_dir / "images" / f"page_{page.index:02d}.png"
            is_product_page = (
                product_image is not None
                and page.index == 1
                and product_image.is_file()
            )
            if _usable_image(image_path):
                logger.info("第 %d 页画面已存在，跳过", page.index)
                provider = "cached"
            elif is_product_page:
                provider = _import_product_image(
                    product_image, image_path, self.settings
                )
                logger.info(
                    "第 %d 页使用产品图片：%s", page.index, product_image
                )
            else:
                provider = visuals.generate(
                    page,
                    storyboard.style_guide,
                    image_path,
                )
                self._usage["image_generations"] += 1
                if qc is not None:
                    original_prompt = page.image_prompt or page.visual_prompt
                    redraws = 0
                    entry: dict[str, Any] = {"page": page.index}
                    while True:
                        try:
                            result = qc.check(image_path, page)
                            self._usage["qc_calls"] += 1
                        except Exception as exc:  # noqa: BLE001
                            logger.warning(
                                "第 %d 页质检失败，保留原图：%s", page.index, exc
                            )
                            entry = {
                                "page": page.index,
                                "status": "qc_error",
                                "reason": str(exc),
                            }
                            break
                        entry = {
                            "page": page.index,
                            "status": "generated",
                            "score": result["score"],
                            "pass": result["pass"],
                            "reason": result["reason"],
                        }
                        if result["pass"] or redraws >= self.settings.qc_max_redraws:
                            break
                        suggestion = (
                            result.get("suggestion")
                            or result.get("reason")
                            or "画面与内容不贴题"
                        )
                        page.image_prompt = (
                            f"{original_prompt} IMPORTANT: {suggestion}"
                        )
                        logger.info(
                            "第 %d 页质检 %.1f 分未达标，第 %d 次重画",
                            page.index,
                            result["score"],
                            redraws + 1,
                        )
                        provider = visuals.generate(
                            page, storyboard.style_guide, image_path
                        )
                        self._usage["image_generations"] += 1
                        redraws += 1
                        self._usage["redraws"] += 1
                    page.image_prompt = original_prompt
                    entry["redraws"] = redraws
                    entry["provider"] = provider
                    qc_entries.append(entry)
            page.image_file = _relative(image_path, task_dir)
            image_providers.append(provider)
        manifest["providers"]["image"] = _single_provider(image_providers)
        _write_json(task_dir / "storyboard.json", storyboard.to_dict())
        if qc_entries:
            _merge_qc_entries(task_dir, qc_entries)

    def _synthesize_speech(
        self,
        storyboard: Storyboard,
        task_dir: Path,
        manifest: dict[str, Any],
        logger: logging.Logger,
    ) -> None:
        """阶段三：配音，根据每页解说词生成 WAV，并计算展示时长。"""
        media = MediaService(self.settings, logger)
        speech = SpeechService(self.settings, logger, media.ffmpeg)
        audio_providers: list[str] = []
        for page in storyboard.pages:
            audio_path = task_dir / "audio" / f"page_{page.index:02d}.wav"
            if _usable_audio(audio_path, media):
                logger.info("第 %d 页配音已存在，跳过", page.index)
                provider = "cached"
            else:
                provider = speech.synthesize(page.narration, audio_path)
            page.audio_file = _relative(audio_path, task_dir)
            page.audio_duration_seconds = round(media.audio_duration(audio_path), 3)
            page.display_duration_seconds = round(
                page.audio_duration_seconds + self.settings.tail_seconds, 3
            )
            audio_providers.append(provider)
        manifest["providers"]["tts"] = _single_provider(audio_providers)

        cursor = 0.0
        for page in storyboard.pages:
            page.start_seconds = round(cursor, 3)
            cursor += page.display_duration_seconds
        _write_json(task_dir / "storyboard.json", storyboard.to_dict())

    def _compose_video(
        self,
        storyboard: Storyboard,
        task_dir: Path,
        logger: logging.Logger,
    ) -> Path:
        """阶段四：合成视频，覆盖分段渲染、拼接、字幕与质量检查。"""
        media = MediaService(self.settings, logger)

        self._step(6, "按语音时长合成五段画面")
        segment_paths: list[Path] = []
        for page in storyboard.pages:
            segment_path = task_dir / "segments" / f"page_{page.index:02d}.mp4"
            media.compose_page(
                task_dir / page.image_file,
                task_dir / page.audio_file,
                segment_path,
                page.display_duration_seconds,
                caption=page.narration,
            )
            page.segment_file = _relative(segment_path, task_dir)
            segment_paths.append(segment_path)

        self._step(7, "拼接完整视频并可选混入背景音乐")
        video_path = task_dir / "video.mp4"
        if self.settings.background_music:
            no_music_path = task_dir / "video_no_bgm.mp4"
            media.concatenate(segment_paths, no_music_path)
            media.mix_background_music(no_music_path, video_path)
        else:
            media.concatenate(segment_paths, video_path)

        self._step(8, "生成字幕与可追溯分镜")
        media.write_srt(storyboard, task_dir / "subtitles.srt")
        _write_json(task_dir / "storyboard.json", storyboard.to_dict())

        self._step(9, "检查五图、配音时长和 MP4 可解码性")
        media.validate_video(video_path)
        quality = _quality_report(storyboard, task_dir, video_path)
        if not quality["passed"]:
            failed = [
                check
                for check, passed in quality["checks"].items()
                if not passed
            ]
            raise RuntimeError(f"成片验收未通过：{', '.join(failed)}")
        _write_json(task_dir / "quality_report.json", quality)
        return video_path

    def edit_storyboard(
        self,
        task_dir: Path,
        pages: list[dict[str, Any]],
    ) -> Storyboard:
        """更新分镜的可编辑字段并写回 storyboard.json。"""
        task_dir = task_dir.resolve()
        storyboard_path = task_dir / "storyboard.json"
        if not storyboard_path.is_file():
            raise ValueError("任务缺少 storyboard.json")
        storyboard = Storyboard.from_dict(
            json.loads(storyboard_path.read_text(encoding="utf-8"))
        )
        for update in pages:
            index = int(update.get("index", 0))
            page = next((item for item in storyboard.pages if item.index == index), None)
            if page is None:
                continue
            if "title" in update:
                page.title = str(update["title"]).strip()
            if "narration" in update:
                page.narration = str(update["narration"]).strip()
            if "key_points" in update and isinstance(update["key_points"], list):
                page.key_points = [
                    str(item).strip() for item in update["key_points"] if str(item).strip()
                ][:3]
            if "visual_prompt" in update:
                page.visual_prompt = str(update["visual_prompt"]).strip()
            if "image_prompt" in update:
                page.image_prompt = str(update["image_prompt"]).strip()
        storyboard.validate()
        _write_json(storyboard_path, storyboard.to_dict())
        return storyboard

    def regenerate_page(
        self,
        task_dir: Path,
        page_index: int,
        redraw_image: bool = True,
        redo_audio: bool = True,
    ) -> PipelineResult:
        """重新生成某一页的画面与配音，再重新合成视频。"""
        task_dir = task_dir.resolve()
        storyboard_path = task_dir / "storyboard.json"
        manifest_path = task_dir / "manifest.json"
        if not storyboard_path.is_file():
            raise ValueError("任务缺少 storyboard.json")
        storyboard = Storyboard.from_dict(
            json.loads(storyboard_path.read_text(encoding="utf-8"))
        )
        if not (1 <= page_index <= len(storyboard.pages)):
            raise ValueError("页码超出范围")
        manifest: dict[str, Any] = {}
        if manifest_path.is_file():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        task_id = str(manifest.get("task_id") or task_dir.name)
        logger = _task_logger(task_dir, task_id)
        self._usage = {"image_generations": 0, "qc_calls": 0, "redraws": 0}
        started = datetime.now().astimezone()

        page = storyboard.pages[page_index - 1]
        if redraw_image:
            img_rel = page.image_file or f"images/page_{page_index:02d}.png"
            img_path = task_dir / img_rel
            if img_path.is_file():
                img_path.unlink()
            page.image_file = ""
        if redo_audio:
            aud_rel = page.audio_file or f"audio/page_{page_index:02d}.wav"
            aud_path = task_dir / aud_rel
            if aud_path.is_file():
                aud_path.unlink()
            page.audio_file = ""
            page.audio_duration_seconds = 0.0
            page.display_duration_seconds = 0.0
        _write_json(storyboard_path, storyboard.to_dict())
        manifest["status"] = "running"
        manifest["error"] = None
        _write_json(manifest_path, manifest)

        try:
            self._step(4, f"重新生成第 {page_index} 页画面")
            self._generate_images(storyboard, task_dir, manifest, logger, task_id)
            self._step(5, f"重新生成第 {page_index} 页配音")
            self._synthesize_speech(storyboard, task_dir, manifest, logger)
            video_path = self._compose_video(storyboard, task_dir, logger)
            finished = datetime.now().astimezone()
            usage = self._usage
            prev_cost = manifest.get("cost_estimate") or {}

            def _num(key: str) -> int:
                try:
                    return int(prev_cost.get(key, 0))
                except (TypeError, ValueError):
                    return 0

            character_count = sum(len(p.narration) for p in storyboard.pages)
            images = _num("images") + usage["image_generations"]
            qc_calls = _num("qc_calls") + usage["qc_calls"]
            redraws = _num("redraws") + usage["redraws"]
            planner_calls = max(1, _num("planner_calls"))
            estimated_cost = {
                "currency": "CNY",
                "kind": "user_supplied_estimate",
                "planner_calls": planner_calls,
                "images": images,
                "qc_calls": qc_calls,
                "redraws": redraws,
                "tts_characters": character_count,
                "planner": round(
                    self.settings.est_planner_cny_per_call * planner_calls, 4
                ),
                "image": round(
                    self.settings.est_image_cny_per_image * images, 4
                ),
                "tts": round(
                    self.settings.est_tts_cny_per_1k_chars
                    * character_count
                    / 1000,
                    4,
                ),
                "qc": round(self.settings.est_qc_cny_per_call * qc_calls, 4),
            }
            estimated_cost["total"] = round(
                estimated_cost["planner"]
                + estimated_cost["image"]
                + estimated_cost["tts"]
                + estimated_cost["qc"],
                4,
            )
            manifest.update(
                {
                    "status": "completed",
                    "finished_at": finished.isoformat(),
                    "elapsed_seconds": round((finished - started).total_seconds(), 2),
                    "page_count": len(storyboard.pages),
                    "total_duration_seconds": round(
                        sum(page.display_duration_seconds for page in storyboard.pages),
                        3,
                    ),
                    "cost_estimate": estimated_cost,
                    "artifacts": {
                        "video": "video.mp4",
                        "subtitles": "subtitles.srt",
                        "storyboard": "storyboard.json",
                        "quality_report": "quality_report.json",
                        "images": [page.image_file for page in storyboard.pages],
                        "audio": [page.audio_file for page in storyboard.pages],
                        "segments": [page.segment_file for page in storyboard.pages],
                        "log": "pipeline.log",
                    },
                }
            )
            _write_json(manifest_path, manifest)
            _write_json(
                task_dir / "task_status.json",
                {
                    "task_id": task_id,
                    "status": "completed",
                    "step": self.total_steps,
                    "total_steps": self.total_steps,
                    "message": "完成",
                    "updated_at": finished.isoformat(),
                },
            )
            logger.info("第 %d 页重新生成完成 video=%s", page_index, video_path)
            return PipelineResult(task_id, task_dir, video_path, manifest_path)
        except Exception as exc:  # noqa: BLE001
            manifest["status"] = "failed"
            manifest["finished_at"] = datetime.now().astimezone().isoformat()
            manifest["error"] = {"type": type(exc).__name__, "message": str(exc)}
            _write_json(manifest_path, manifest)
            logger.exception("单页重新生成失败")
            raise
        finally:
            for handler in list(logger.handlers):
                handler.close()
                logger.removeHandler(handler)

    def _step(self, step: int, message: str) -> None:
        self.progress(step, self.total_steps, message)


def _make_task_id(now: datetime, source_text: str) -> str:
    digest = hashlib.sha1(source_text.encode("utf-8")).hexdigest()[:6]
    short_uuid = uuid.uuid4().hex[:6]
    return f"{now:%Y%m%d_%H%M%S}_{digest}_{short_uuid}"


def _task_logger(task_dir: Path, task_id: str) -> logging.Logger:
    logger = logging.getLogger(f"ai_video.task.{task_id}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.handlers.clear()
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler = logging.FileHandler(
        task_dir / "pipeline.log",
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    return logger


def _safe_config(settings: Settings) -> dict[str, Any]:
    return {
        "mode": settings.mode,
        "api_configured": bool(settings.api_key),
        "base_url": settings.base_url,
        "planner_model": settings.planner_model,
        "prompt_enhance": settings.prompt_enhance,
        "prompt_enhance_model": settings.prompt_enhance_model,
        "image_model": settings.image_model,
        "image_backend": settings.image_backend,
        "image_size": settings.image_size,
        "image_text": settings.image_text,
        "comfyui": {
            "base_url": settings.comfyui_base_url,
            "workflow_file": (
                str(settings.comfyui_workflow_file)
                if settings.comfyui_workflow_file
                else None
            ),
            "output_node_id": settings.comfyui_output_node_id or None,
        },
        "edge_tts_voice": settings.edge_tts_voice,
        "video": {
            "width": settings.width,
            "height": settings.height,
            "fps": settings.fps,
            "page_audio_tail_seconds": settings.tail_seconds,
            "burn_subtitles": settings.burn_subtitles,
            "slide_transitions": settings.slide_transitions,
        },
        "background_music": (
            str(settings.background_music) if settings.background_music else None
        ),
        "ai_disclosure": settings.ai_disclosure,
    }


def _quality_report(
    storyboard: Storyboard,
    task_dir: Path,
    video_path: Path,
) -> dict[str, Any]:
    image_paths = [task_dir / page.image_file for page in storyboard.pages]
    audio_paths = [task_dir / page.audio_file for page in storyboard.pages]
    image_hashes = [
        hashlib.sha256(path.read_bytes()).hexdigest()
        for path in image_paths
        if path.is_file()
    ]
    dimensions = []
    for path in image_paths:
        if path.is_file():
            with Image.open(path) as image:
                dimensions.append(list(image.size))
    checks = {
        "exactly_five_pages": len(storyboard.pages) == 5,
        "five_distinct_image_files": (
            len(image_paths) == 5 and len(image_hashes) == 5
            and len(set(image_hashes)) == 5
        ),
        "five_nonempty_audio_files": (
            len(audio_paths) == 5
            and all(path.is_file() and path.stat().st_size > 44 for path in audio_paths)
        ),
        "audio_not_truncated": all(
            page.display_duration_seconds
            >= page.audio_duration_seconds + 0.2
            for page in storyboard.pages
        ),
        "all_images_have_expected_ratio": all(
            width > 0 and height > 0 and abs((width / height) - (16 / 9)) < 0.01
            for width, height in dimensions
        ),
        "mp4_is_nonempty": video_path.is_file() and video_path.stat().st_size > 1024,
    }
    return {
        "passed": all(checks.values()),
        "checks": checks,
        "image_dimensions": dimensions,
        "image_sha256": image_hashes,
        "audio_seconds": [
            page.audio_duration_seconds for page in storyboard.pages
        ],
        "display_seconds": [
            page.display_duration_seconds for page in storyboard.pages
        ],
    }


def _single_provider(providers: list[str]) -> str:
    unique = list(dict.fromkeys(providers))
    return unique[0] if len(unique) == 1 else ", ".join(unique)


def _usable_image(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size <= 1024:
        return False
    try:
        with Image.open(path) as image:
            image.verify()
        return True
    except Exception:
        return False


def _usable_audio(path: Path, media: MediaService) -> bool:
    if not path.is_file() or path.stat().st_size <= 44:
        return False
    try:
        return media.audio_duration(path) > 0.05
    except Exception:
        return False


def _relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _resolve_product_image(value: Any) -> Path | None:
    if not value:
        return None
    path = Path(str(value)).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path if path.is_file() else None


def _import_product_image(
    source: Path,
    destination: Path,
    settings: Settings,
) -> str:
    """把用户提供的产品图片规范成 16:9 画布并居中放置，作为指定页画面。"""
    destination.parent.mkdir(parents=True, exist_ok=True)
    image = Image.open(source).convert("RGB")
    width, height = settings.width, settings.height
    margin = int(min(width, height) * 0.04)
    available_w = max(1, width - margin * 2)
    available_h = max(1, height - margin * 2)
    ratio = min(available_w / image.width, available_h / image.height)
    target_w = max(1, round(image.width * ratio))
    target_h = max(1, round(image.height * ratio))
    resized = image.resize((target_w, target_h), Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", (width, height), (7, 11, 23))
    canvas.paste(resized, ((width - target_w) // 2, (height - target_h) // 2))
    canvas.save(destination, format="PNG", optimize=True)
    return f"product-image:{source.name}"


def _write_json(path: Path, value: Any) -> None:
    text = json.dumps(value, ensure_ascii=False, indent=2)
    path.write_text(text + "\n", encoding="utf-8")


def _merge_qc_entries(task_dir: Path, new_entries: list[dict[str, Any]]) -> None:
    path = task_dir / "quality_checks.json"
    by_page: dict[int, Any] = {}
    if path.is_file():
        try:
            old = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(old, list):
                for item in old:
                    if isinstance(item, dict) and "page" in item:
                        by_page[int(item["page"])] = item
        except (OSError, json.JSONDecodeError, ValueError):
            pass
    for item in new_entries:
        by_page[int(item["page"])] = item
    _write_json(path, [by_page[key] for key in sorted(by_page)])

