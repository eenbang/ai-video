"""Web 服务：主题/文案 -> 5 页图文配音视频的浏览器界面。

通过 FastAPI 提供 REST 接口与一个无构建步骤的单页前端。任务在后台线程中
运行，进度通过 SSE（Server-Sent Events）推送到浏览器。
"""

from __future__ import annotations

import asyncio
import collections
import importlib.util
import json
import os
import re
import shutil
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Path as PathParam, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .config import Settings, _load_dotenv
from .pipeline import VideoPipeline, _make_task_id


PROJECT_ROOT = Path(__file__).resolve().parents[2]
WEB_DIR = PROJECT_ROOT / "web"
ENV_PATH = PROJECT_ROOT / ".env"
OUTPUT_DIR_DEFAULT = PROJECT_ROOT / "outputs"
TASK_ID_RE = re.compile(r"^[0-9A-Za-z_-]+$")

# 将进程工作目录固定到项目根目录，避免从其他目录启动服务时找不到 outputs/.env。
os.chdir(PROJECT_ROOT)
_load_dotenv(ENV_PATH)


class TaskCancelled(Exception):
    """协作式取消：在步骤边界抛出，中断后续步骤。"""


_ENV_WHITELIST = {
    "PIPELINE_MODE",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "PLANNER_MODEL",
    "PROMPT_ENHANCE",
    "PROMPT_ENHANCE_MODEL",
    "IMAGE_MODEL",
    "IMAGE_BACKEND",
    "IMAGE_SIZE",
    "IMAGE_TEXT",
    "QC_ENABLED",
    "QC_MODEL",
    "QC_MIN_SCORE",
    "QC_MAX_REDRAWS",
    "COMFYUI_BASE_URL",
    "COMFYUI_WORKFLOW_FILE",
    "COMFYUI_OUTPUT_NODE_ID",
    "EDGE_TTS_VOICE",
    "EDGE_TTS_RATE",
    "OUTPUT_DIR",
    "VIDEO_WIDTH",
    "VIDEO_HEIGHT",
    "FPS",
    "PAGE_AUDIO_TAIL_SECONDS",
    "BURN_SUBTITLES",
    "SLIDE_TRANSITIONS",
    "AI_DISCLOSURE",
    "BACKGROUND_MUSIC",
    "BACKGROUND_MUSIC_VOLUME",
    "REQUEST_TIMEOUT_SECONDS",
    "MAX_RETRIES",
    "EST_PLANNER_CNY_PER_CALL",
    "EST_IMAGE_CNY_PER_IMAGE",
    "EST_TTS_CNY_PER_1K_CHARS",
    "EST_QC_CNY_PER_CALL",
}


class Job:
    """一个后台任务及其事件队列。"""

    def __init__(self, task_id: str, task_dir: Path) -> None:
        self.task_id = task_id
        self.task_dir = task_dir
        self.status = "queued"
        self.error: str | None = None
        self.events: list[dict[str, Any]] = []
        self._events_lock = threading.Lock()
        self.cancel_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.video_path: str | None = None
        self.manifest_path: str | None = None
        self.source_text: str | None = None
        self.settings: Settings | None = None
        self.resume: bool = False
        self.kind: str = "run"
        self.page_index: int = 0
        self.redraw_image: bool = True
        self.redo_audio: bool = True

    def emit(self, event_type: str, **data: Any) -> None:
        with self._events_lock:
            self.events.append({"type": event_type, **data})


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _write_task_status(
    task_dir: Path,
    task_id: str,
    status: str,
    step: int,
    total: int,
    message: str,
) -> None:
    task_dir.mkdir(parents=True, exist_ok=True)
    _write_json(
        task_dir / "task_status.json",
        {
            "task_id": task_id,
            "status": status,
            "step": step,
            "total_steps": total,
            "message": message,
            "updated_at": datetime.now().astimezone().isoformat(),
        },
    )


def _mark_status(task_dir: Path, task_id: str, status: str, message: str) -> None:
    task_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now().astimezone()
    manifest_path = task_dir / "manifest.json"
    manifest = _read_json(manifest_path) or {}
    manifest.update(
        {
            "task_id": task_id,
            "status": status,
            "error": None,
            "finished_at": now.isoformat(),
        }
    )
    _write_json(manifest_path, manifest)
    _write_json(
        task_dir / "task_status.json",
        {
            "task_id": task_id,
            "status": status,
            "message": message,
            "updated_at": now.isoformat(),
        },
    )


class TaskManager:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._pending: collections.deque[Job] = collections.deque()
        self._cv = threading.Condition()
        self._shutdown = False
        concurrency = max(1, _env_int("WEB_MAX_CONCURRENT_TASKS", 2))
        self._workers = [
            threading.Thread(target=self._worker_loop, daemon=True)
            for _ in range(concurrency)
        ]
        for worker in self._workers:
            worker.start()

    def get(self, task_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(task_id)

    def start_run(
        self,
        source_text: str,
        settings: Settings,
    ) -> Job:
        created_at = datetime.now().astimezone()
        task_id = _make_task_id(created_at, source_text)
        task_dir = settings.output_dir / task_id
        job = Job(task_id, task_dir)
        job.source_text = source_text
        job.settings = settings
        job.resume = False
        job.kind = "run"
        job.emit(
            "status",
            status="queued",
            step=0,
            total=VideoPipeline.total_steps,
            message="排队中",
        )
        with self._lock:
            self._jobs[task_id] = job
        with self._cv:
            self._pending.append(job)
            self._cv.notify()
        return job

    def start_resume(self, task_dir: Path, settings: Settings) -> Job:
        manifest_path = task_dir / "manifest.json"
        if not manifest_path.is_file():
            raise HTTPException(status_code=400, detail="任务缺少 manifest.json，无法续跑。")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        task_id = str(manifest.get("task_id") or task_dir.name)
        job = Job(task_id, task_dir)
        job.source_text = ""
        job.settings = settings
        job.resume = True
        job.kind = "resume"
        job.emit(
            "status",
            status="queued",
            step=0,
            total=VideoPipeline.total_steps,
            message="排队中",
        )
        with self._lock:
            self._jobs[task_id] = job
        with self._cv:
            self._pending.append(job)
            self._cv.notify()
        return job

    def start_regenerate(
        self,
        task_dir: Path,
        page_index: int,
        redraw_image: bool,
        redo_audio: bool,
        settings: Settings,
    ) -> Job:
        manifest_path = task_dir / "manifest.json"
        if not manifest_path.is_file():
            raise HTTPException(status_code=400, detail="任务缺少 manifest.json，无法重做。")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        task_id = str(manifest.get("task_id") or task_dir.name)
        job = Job(task_id, task_dir)
        job.source_text = ""
        job.settings = settings
        job.kind = "regenerate"
        job.page_index = page_index
        job.redraw_image = redraw_image
        job.redo_audio = redo_audio
        job.emit(
            "status",
            status="queued",
            step=0,
            total=VideoPipeline.total_steps,
            message=f"排队中：重做第 {page_index} 页",
        )
        with self._lock:
            self._jobs[task_id] = job
        with self._cv:
            self._pending.append(job)
            self._cv.notify()
        return job

    def cancel(self, task_id: str) -> bool:
        job = self.get(task_id)
        if job is None:
            return False
        if job.status == "queued":
            with self._cv:
                try:
                    self._pending.remove(job)
                except ValueError:
                    pass
            job.status = "cancelled"
            job.cancel_event.set()
            _mark_status(job.task_dir, job.task_id, "cancelled", "已取消")
            job.emit("done", status="cancelled", task_id=task_id)
            return True
        job.cancel_event.set()
        job.emit(
            "status",
            status=job.status,
            step=0,
            total=VideoPipeline.total_steps,
            message="已请求停止，将在当前步骤结束后中断",
        )
        return True

    def live_jobs(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {
                    "task_id": job.task_id,
                    "status": job.status,
                    "source_text": job.source_text or "",
                }
                for job in self._jobs.values()
                if job.status in {"queued", "running"}
            ]

    def _worker_loop(self) -> None:
        while True:
            with self._cv:
                while not self._pending and not self._shutdown:
                    self._cv.wait()
                if self._shutdown:
                    return
                job = self._pending.popleft()
            if job.status == "cancelled":
                continue
            self._run_job(job)

    def _run_job(self, job: Job) -> None:
        settings = job.settings
        if settings is None:
            job.status = "failed"
            job.emit("done", status="failed", task_id=job.task_id)
            return
        task_dir = job.task_dir
        task_id = job.task_id
        source_text = job.source_text or ""
        resume = job.resume
        job.status = "running"
        job.emit(
            "status",
            status="running",
            step=0,
            total=VideoPipeline.total_steps,
            message=(
                "任务开始"
                if job.kind != "regenerate"
                else f"重新生成第 {job.page_index} 页"
            ),
            resume=resume,
        )

        def progress(step: int, total: int, message: str) -> None:
            if job.cancel_event.is_set():
                raise TaskCancelled()
            job.emit("progress", step=step, total=total, message=message)
            _write_task_status(task_dir, task_id, "running", step, total, message)

        try:
            pipeline = VideoPipeline(settings, progress)
            if job.kind == "resume":
                result = pipeline.resume(task_dir)
            elif job.kind == "regenerate":
                result = pipeline.regenerate_page(
                    task_dir,
                    job.page_index,
                    job.redraw_image,
                    job.redo_audio,
                )
            else:
                result = pipeline.run_in(source_text, task_dir, task_id)
            job.status = "completed"
            job.video_path = str(result.video_path)
            job.manifest_path = str(result.manifest_path)
            job.emit(
                "done",
                status="completed",
                task_id=result.task_id,
                video_path=str(result.video_path),
            )
        except TaskCancelled:
            job.status = "cancelled"
            _mark_status(task_dir, task_id, "cancelled", "用户已停止")
            job.emit("done", status="cancelled", task_id=task_id)
        except Exception as exc:  # noqa: BLE001
            job.status = "failed"
            job.error = str(exc)
            job.emit("error", message=str(exc))
            job.emit("done", status="failed", task_id=task_id)


MANAGER = TaskManager()


def _load_settings() -> Settings:
    return Settings.load()


def _read_env() -> dict[str, str]:
    values: dict[str, str] = {}
    if not ENV_PATH.is_file():
        return values
    for raw_line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key:
            values[key] = value.strip().strip('"').strip("'")
    return values


def _write_env(updates: dict[str, str]) -> None:
    lines = (
        ENV_PATH.read_text(encoding="utf-8").splitlines()
        if ENV_PATH.is_file()
        else []
    )
    seen: set[str] = set()
    result: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            result.append(line)
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in updates:
            result.append(f"{key}={updates[key]}")
            seen.add(key)
            continue
        result.append(line)
    for key, value in updates.items():
        if key not in seen:
            result.append(f"{key}={value}")
    ENV_PATH.write_text("\n".join(result) + "\n", encoding="utf-8")


def _mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return value[:3] + "*" * (len(value) - 7) + value[-4:]


def _resolve_task_dir(settings: Settings, task_id: str) -> Path | None:
    for base in (settings.output_dir, settings.output_dir / "_archive"):
        candidate = base / task_id
        if candidate.is_dir():
            return candidate
    return None


def _require_task_dir(task_id: str) -> Path:
    if not TASK_ID_RE.fullmatch(task_id):
        raise HTTPException(status_code=404, detail="任务不存在")
    task_dir = _resolve_task_dir(_load_settings(), task_id)
    if task_dir is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return task_dir


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _task_summary(task_dir: Path) -> dict[str, Any]:
    manifest = _read_json(task_dir / "manifest.json") or {}
    status = _read_json(task_dir / "task_status.json") or {}
    artifacts = manifest.get("artifacts") or {}
    video_rel = artifacts.get("video", "video.mp4")
    return {
        "task_id": task_dir.name,
        "status": manifest.get("status") or status.get("status") or "unknown",
        "source_text": manifest.get("source_text", ""),
        "created_at": manifest.get("created_at") or "",
        "finished_at": manifest.get("finished_at") or "",
        "elapsed_seconds": manifest.get("elapsed_seconds"),
        "providers": manifest.get("providers") or {},
        "error": manifest.get("error"),
        "page_count": manifest.get("page_count"),
        "total_duration_seconds": manifest.get("total_duration_seconds"),
        "has_video": (task_dir / video_rel).is_file() if video_rel else False,
        "archived": task_dir.parent.name == "_archive",
    }


def _safe_child(task_dir: Path, rel_path: str) -> Path:
    root = task_dir.resolve()
    child = (root / rel_path).resolve()
    if root not in child.parents and child != root:
        raise HTTPException(status_code=404, detail="文件不存在")
    if not child.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    return child


def _run_checks(settings: Settings) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []

    def add(name: str, passed: bool, detail: str) -> None:
        checks.append({"name": name, "passed": passed, "detail": detail})

    import sys

    add("Python", sys.version_info >= (3, 11), sys.version.split()[0])
    for package in ("PIL", "edge_tts", "imageio_ffmpeg"):
        available = importlib.util.find_spec(package) is not None
        add(package, available, "已安装" if available else "未安装")
    try:
        import imageio_ffmpeg

        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        add("FFmpeg", Path(ffmpeg).is_file(), str(ffmpeg))
    except Exception as exc:  # noqa: BLE001
        add("FFmpeg", False, str(exc))
    font_candidates = [
        Path("C:/Windows/Fonts/msyh.ttc"),
        Path("C:/Windows/Fonts/simhei.ttf"),
    ]
    font = next((p for p in font_candidates if p.is_file()), None)
    add("中文字体", font is not None, str(font or "未找到"))
    add(
        "API 配置",
        bool(settings.api_key),
        "已配置" if settings.api_key else "未配置，将使用演示兜底",
    )
    effective = (
        "api"
        if settings.mode == "api"
        or (settings.mode == "auto" and settings.api_key)
        else "demo"
    )
    add("实际模式", True, effective)
    return checks


app = FastAPI(title="AI 图文配音演示视频生成系统", version="0.1.0")


@app.get("/api/tasks")
def list_tasks() -> dict[str, Any]:
    settings = _load_settings()
    output_dir = settings.output_dir
    active: list[dict[str, Any]] = []
    archived: list[dict[str, Any]] = []
    if output_dir.is_dir():
        for path in sorted(output_dir.iterdir()):
            if not path.is_dir():
                continue
            if path.name == "_archive":
                continue
            active.append(_task_summary(path))
    archive_dir = output_dir / "_archive"
    if archive_dir.is_dir():
        for path in sorted(archive_dir.iterdir()):
            if path.is_dir():
                archived.append(_task_summary(path))
    known = {item["task_id"] for item in active}
    for live in MANAGER.live_jobs():
        task_id = live["task_id"]
        if task_id in known:
            for item in active:
                if item["task_id"] == task_id:
                    item["status"] = live["status"]
                    break
        else:
            active.append(
                {
                    "task_id": task_id,
                    "status": live["status"],
                    "source_text": live["source_text"],
                    "created_at": "",
                    "finished_at": "",
                    "elapsed_seconds": None,
                    "providers": {},
                    "error": None,
                    "page_count": None,
                    "total_duration_seconds": None,
                    "has_video": False,
                    "archived": False,
                }
            )
    active.sort(key=lambda item: item.get("created_at", ""), reverse=True)
    archived.sort(key=lambda item: item.get("created_at", ""), reverse=True)
    return {
        "active": active,
        "archived": archived,
        "output_dir": str(output_dir),
    }


@app.get("/api/tasks/{task_id}")
def task_detail(task_id: str) -> dict[str, Any]:
    task_dir = _require_task_dir(task_id)
    manifest = _read_json(task_dir / "manifest.json") or {}
    storyboard = _read_json(task_dir / "storyboard.json")
    quality = _read_json(task_dir / "quality_report.json")
    quality_checks = _read_json(task_dir / "quality_checks.json")
    run_config = _read_json(task_dir / "run_config.json")
    status = _read_json(task_dir / "task_status.json")
    artifacts = manifest.get("artifacts") or {}
    files = []
    for category in ("video", "subtitles", "storyboard", "quality_report", "log"):
        rel = artifacts.get(category)
        if rel and (task_dir / rel).is_file():
            files.append({"kind": category, "path": rel})
    for group in ("images", "audio", "segments"):
        for rel in artifacts.get(group) or []:
            if rel and (task_dir / rel).is_file():
                files.append({"kind": group, "path": rel})
    return {
        "task_id": task_dir.name,
        "archived": task_dir.parent.name == "_archive",
        "manifest": manifest,
        "storyboard": storyboard,
        "quality_report": quality,
        "quality_checks": quality_checks,
        "run_config": run_config,
        "task_status": status,
        "files": files,
    }


@app.get("/api/tasks/{task_id}/files/{rel_path:path}")
def task_file(task_id: str, rel_path: str, request: Request) -> FileResponse:
    task_dir = _require_task_dir(task_id)
    child = _safe_child(task_dir, rel_path)
    if request.query_params.get("download") == "1":
        return FileResponse(
            child,
            filename=child.name,
            content_disposition_type="attachment",
        )
    return FileResponse(
        child,
        filename=child.name,
        content_disposition_type="inline",
    )


@app.get("/api/tasks/{task_id}/events")
async def task_events(task_id: str, request: Request) -> StreamingResponse:
    if not TASK_ID_RE.fullmatch(task_id):
        raise HTTPException(status_code=404, detail="任务不存在")
    job = MANAGER.get(task_id)
    task_dir = _resolve_task_dir(_load_settings(), task_id)
    if job is None and task_dir is None:
        raise HTTPException(status_code=404, detail="任务不存在")

    async def stream():
        if job is None:
            summary = _task_summary(task_dir) if task_dir else {}
            yield _sse({"type": "done", "status": summary.get("status", "unknown"), "task_id": task_id})
            return
        cursor = 0
        while True:
            with job._events_lock:
                pending = job.events[cursor:]
                cursor = len(job.events)
            for item in pending:
                yield _sse(item)
                if item.get("type") == "done":
                    return
            if job.status in {"completed", "failed", "cancelled"}:
                yield _sse(
                    {
                        "type": "done",
                        "status": job.status,
                        "task_id": task_id,
                    }
                )
                return
            try:
                if await request.is_disconnected():
                    return
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(0.25)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


def _sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


@app.post("/api/tasks")
def create_task(body: dict[str, Any]) -> dict[str, Any]:
    source_text = str(body.get("source_text", "")).strip()
    if not source_text:
        raise HTTPException(status_code=400, detail="主题或文案不能为空")
    mode = body.get("mode") or None
    image_backend = body.get("image_backend") or None
    settings = Settings.load(mode, None, image_backend)
    job = MANAGER.start_run(source_text, settings)
    return {"task_id": job.task_id, "status": job.status}


@app.post("/api/tasks/{task_id}/cancel")
def cancel_task(task_id: str) -> dict[str, Any]:
    if not MANAGER.cancel(task_id):
        raise HTTPException(status_code=404, detail="任务不存在或未在运行")
    return {"ok": True}


@app.post("/api/tasks/{task_id}/resume")
def resume_task(task_id: str) -> dict[str, Any]:
    task_dir = _require_task_dir(task_id)
    settings = _load_settings()
    job = MANAGER.start_resume(task_dir, settings)
    return {"task_id": job.task_id, "status": job.status}


@app.put("/api/tasks/{task_id}/storyboard")
def update_storyboard(task_id: str, body: dict[str, Any]) -> dict[str, Any]:
    task_dir = _require_task_dir(task_id)
    pages = body.get("pages")
    if not isinstance(pages, list):
        raise HTTPException(status_code=400, detail="pages 必须是数组")
    settings = _load_settings()
    try:
        storyboard = VideoPipeline(settings).edit_storyboard(task_dir, pages)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"storyboard": storyboard.to_dict()}


@app.post("/api/tasks/{task_id}/pages/{page_index}/regenerate")
def regenerate_page(
    task_id: str,
    page_index: int,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    task_dir = _require_task_dir(task_id)
    body = body or {}
    redraw_image = bool(body.get("redraw_image", True))
    redo_audio = bool(body.get("redo_audio", True))
    settings = _load_settings()
    job = MANAGER.start_regenerate(
        task_dir, page_index, redraw_image, redo_audio, settings
    )
    return {"task_id": job.task_id, "status": job.status}


@app.post("/api/tasks/{task_id}/archive")
def archive_task(task_id: str) -> dict[str, Any]:
    settings = _load_settings()
    task_dir = _require_task_dir(task_id)
    if task_dir.parent.name == "_archive":
        raise HTTPException(status_code=400, detail="任务已在归档区")
    live = MANAGER.get(task_id)
    if live is not None and live.status in {"queued", "running"}:
        raise HTTPException(
            status_code=409,
            detail="任务正在运行或排队，请先停止再归档",
        )
    output_dir = settings.output_dir.resolve()
    archive_dir = output_dir / "_archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    shutil.move(str(task_dir), str(archive_dir / task_dir.name))
    return {"ok": True, "archived": task_id}


@app.post("/api/tasks/archive-all")
def archive_all_tasks() -> dict[str, Any]:
    settings = _load_settings()
    output_dir = settings.output_dir.resolve()
    archive_dir = output_dir / "_archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    moved = 0
    for path in sorted(output_dir.iterdir()):
        if path.is_dir() and path.name != "_archive":
            shutil.move(str(path), str(archive_dir / path.name))
            moved += 1
    return {"ok": True, "archived_count": moved}


@app.post("/api/archive/{task_id}/delete")
def delete_archived_task(task_id: str) -> dict[str, Any]:
    settings = _load_settings()
    archive_dir = settings.output_dir.resolve() / "_archive"
    if not TASK_ID_RE.fullmatch(task_id):
        raise HTTPException(status_code=404, detail="任务不存在")
    target = archive_dir / task_id
    if not target.is_dir():
        raise HTTPException(status_code=404, detail="归档任务不存在")
    shutil.rmtree(target)
    return {"ok": True, "deleted": task_id}


@app.post("/api/archive/delete-all")
def delete_all_archived() -> dict[str, Any]:
    settings = _load_settings()
    archive_dir = settings.output_dir.resolve() / "_archive"
    removed = 0
    if archive_dir.is_dir():
        for path in sorted(archive_dir.iterdir()):
            if path.is_dir():
                shutil.rmtree(path)
                removed += 1
    return {"ok": True, "deleted_count": removed}


@app.get("/api/check")
def run_check() -> dict[str, Any]:
    settings = _load_settings()
    checks = _run_checks(settings)
    passed = all(item["passed"] for item in checks if item["name"] != "API 配置")
    return {"passed": passed, "checks": checks}


@app.get("/api/settings")
def get_settings() -> dict[str, Any]:
    values = _read_env()
    api_key = values.get("OPENAI_API_KEY", "")
    values_for_ui = {key: value for key, value in values.items() if key in _ENV_WHITELIST}
    return {
        "values": values_for_ui,
        "api_key_set": bool(api_key),
        "api_key_masked": _mask_secret(api_key),
    }


@app.put("/api/settings")
def update_settings(body: dict[str, Any]) -> dict[str, Any]:
    incoming = body.get("values") or {}
    updates: dict[str, str] = {}
    for key, value in incoming.items():
        if key not in _ENV_WHITELIST:
            continue
        text = str(value).strip()
        if key == "OPENAI_API_KEY" and not text:
            continue
        if key == "OUTPUT_DIR" and not text:
            text = "outputs"
        updates[key] = text
    if updates:
        _write_env(updates)
    return get_settings()


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"ok": True, "project_root": str(PROJECT_ROOT)}


app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")


def main() -> None:
    import uvicorn

    uvicorn.run(
        app,
        host="127.0.0.1",
        port=8000,
        log_level="info",
        timeout_graceful_shutdown=3,
    )


if __name__ == "__main__":
    main()
