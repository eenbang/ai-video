from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path


def _load_dotenv(path: Path = Path(".env")) -> None:
    """读取简单的 KEY=VALUE .env 文件，不覆盖已有环境变量。"""
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


@dataclass(frozen=True, slots=True)
class Settings:
    mode: str
    api_key: str
    base_url: str
    planner_model: str
    prompt_enhance: bool
    prompt_enhance_model: str
    image_model: str
    image_backend: str
    image_size: str
    image_text: bool
    qc_enabled: bool
    qc_model: str
    qc_min_score: float
    qc_max_redraws: int
    comfyui_base_url: str
    comfyui_workflow_file: Path | None
    comfyui_output_node_id: str
    comfyui_timeout_seconds: float
    comfyui_poll_interval_seconds: float
    edge_tts_voice: str
    edge_tts_rate: str
    output_dir: Path
    width: int
    height: int
    fps: int
    tail_seconds: float
    burn_subtitles: bool
    slide_transitions: bool
    ai_disclosure: bool
    background_music: Path | None
    background_music_volume: float
    request_timeout_seconds: float
    max_retries: int
    est_planner_cny_per_call: float
    est_image_cny_per_image: float
    est_tts_cny_per_1k_chars: float
    est_qc_cny_per_call: float

    @classmethod
    def load(
        cls,
        mode: str | None = None,
        output_dir: str | None = None,
        image_backend: str | None = None,
    ) -> "Settings":
        _load_dotenv()
        selected_mode = (mode or os.getenv("PIPELINE_MODE", "auto")).strip().lower()
        if selected_mode not in {"auto", "api", "demo"}:
            raise ValueError("mode 必须是 auto、api 或 demo")
        image_backend = (
            image_backend or os.getenv("IMAGE_BACKEND", "demo")
        ).strip().lower()
        if image_backend not in {"demo", "openai", "comfyui"}:
            raise ValueError("IMAGE_BACKEND 必须是 demo、openai 或 comfyui")
        image_size = os.getenv("IMAGE_SIZE", "1024x576").strip().lower()
        if not re.fullmatch(r"\d+x\d+", image_size):
            raise ValueError("IMAGE_SIZE 必须是 宽x高 格式，例如 1024x576")
        planner_model = os.getenv("PLANNER_MODEL", "gpt-4o-mini").strip()
        prompt_enhance_model = (
            os.getenv("PROMPT_ENHANCE_MODEL", "").strip() or planner_model
        )
        music_raw = os.getenv("BACKGROUND_MUSIC", "").strip()
        music = Path(music_raw).expanduser().resolve() if music_raw else None
        comfyui_workflow_raw = os.getenv("COMFYUI_WORKFLOW_FILE", "").strip()
        comfyui_workflow_file = (
            Path(comfyui_workflow_raw).expanduser().resolve()
            if comfyui_workflow_raw
            else None
        )
        raw_output_dir = os.getenv("OUTPUT_DIR", "outputs").strip()
        return cls(
            mode=selected_mode,
            api_key=os.getenv("OPENAI_API_KEY", "").strip(),
            base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
            .strip()
            .rstrip("/"),
            planner_model=planner_model,
            prompt_enhance=_bool("PROMPT_ENHANCE", True),
            prompt_enhance_model=prompt_enhance_model,
            image_model=os.getenv("IMAGE_MODEL", "gpt-image-2.5-sunburst").strip(),
            image_backend=image_backend,
            image_size=image_size,
            image_text=_bool("IMAGE_TEXT", True),
            qc_enabled=_bool("QC_ENABLED", True),
            qc_model=os.getenv("QC_MODEL", "Qwen/Qwen3-VL-8B-Instruct").strip(),
            qc_min_score=max(0.0, min(10.0, _float("QC_MIN_SCORE", 6.0))),
            qc_max_redraws=max(0, _int("QC_MAX_REDRAWS", 2)),
            comfyui_base_url=os.getenv(
                "COMFYUI_BASE_URL", "http://127.0.0.1:8188"
            ).strip().rstrip("/"),
            comfyui_workflow_file=comfyui_workflow_file,
            comfyui_output_node_id=os.getenv(
                "COMFYUI_OUTPUT_NODE_ID", ""
            ).strip(),
            comfyui_timeout_seconds=max(
                30.0, _float("COMFYUI_TIMEOUT_SECONDS", 300.0)
            ),
            comfyui_poll_interval_seconds=min(
                10.0, max(0.1, _float("COMFYUI_POLL_INTERVAL_SECONDS", 0.5))
            ),
            edge_tts_voice=os.getenv("EDGE_TTS_VOICE", "zh-CN-XiaoxiaoNeural").strip(),
            edge_tts_rate=os.getenv("EDGE_TTS_RATE", "+0%").strip(),
            output_dir=Path(output_dir or raw_output_dir or "outputs")
            .expanduser()
            .resolve(),
            width=_int("VIDEO_WIDTH", 1920),
            height=_int("VIDEO_HEIGHT", 1080),
            fps=_int("FPS", 30),
            tail_seconds=max(0.1, _float("PAGE_AUDIO_TAIL_SECONDS", 0.65)),
            burn_subtitles=_bool("BURN_SUBTITLES", True),
            slide_transitions=_bool("SLIDE_TRANSITIONS", True),
            ai_disclosure=_bool("AI_DISCLOSURE", True),
            background_music=music,
            background_music_volume=min(
                1.0, max(0.0, _float("BACKGROUND_MUSIC_VOLUME", 0.10))
            ),
            request_timeout_seconds=max(
                10.0, _float("REQUEST_TIMEOUT_SECONDS", 180.0)
            ),
            max_retries=max(1, _int("MAX_RETRIES", 3)),
            est_planner_cny_per_call=max(
                0.0, _float("EST_PLANNER_CNY_PER_CALL", 0.0)
            ),
            est_image_cny_per_image=max(
                0.0, _float("EST_IMAGE_CNY_PER_IMAGE", 0.0)
            ),
            est_tts_cny_per_1k_chars=max(
                0.0, _float("EST_TTS_CNY_PER_1K_CHARS", 0.0)
            ),
            est_qc_cny_per_call=max(
                0.0, _float("EST_QC_CNY_PER_CALL", 0.0)
            ),
        )

    @property
    def can_use_api(self) -> bool:
        return bool(self.api_key)

    @property
    def is_siliconflow(self) -> bool:
        return "siliconflow" in self.base_url.lower()

    def use_api(self, allow_fallback: bool = True) -> bool:
        if self.mode == "api":
            return True
        if self.mode == "demo":
            return False
        return self.can_use_api or not allow_fallback

