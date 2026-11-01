from __future__ import annotations

import logging
import subprocess
import wave
from pathlib import Path

import imageio_ffmpeg
from PIL import Image, ImageDraw, ImageFont

from .config import Settings
from .models import Storyboard


class MediaError(RuntimeError):
    pass


class MediaService:
    def __init__(self, settings: Settings, logger: logging.Logger) -> None:
        self.settings = settings
        self.logger = logger
        self.ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()

    def audio_duration(self, audio_path: Path) -> float:
        try:
            with wave.open(str(audio_path), "rb") as handle:
                frames = handle.getnframes()
                rate = handle.getframerate()
                if rate <= 0:
                    raise MediaError(f"无效采样率：{audio_path}")
                return frames / rate
        except (wave.Error, EOFError) as exc:
            raise MediaError(f"无法读取 WAV 时长：{audio_path}") from exc

    def compose_page(
        self,
        image_path: Path,
        audio_path: Path,
        output_path: Path,
        duration: float,
        caption: str = "",
    ) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        width, height = self.settings.width, self.settings.height
        input_image = image_path
        temp_image: Path | None = None
        if self.settings.burn_subtitles and caption.strip():
            framed = _render_subtitle(
                image_path, caption, width, height
            )
            temp_image = output_path.parent / f".subtitle_{output_path.stem}.png"
            framed.save(temp_image)
            input_image = temp_image

        try:
            base_filter = (
                f"[0:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=0x070B17,"
                f"fps={self.settings.fps}"
            )
            if self.settings.slide_transitions:
                fade = 0.35
                out_start = max(0.0, duration - fade)
                video_filter = (
                    base_filter
                    + f",fade=t=in:st=0:d={fade},"
                    + f"fade=t=out:st={out_start:.3f}:d={fade},"
                    + "format=yuv420p[v]"
                )
            else:
                video_filter = base_filter + ",format=yuv420p[v]"
            filter_graph = video_filter + ";[1:a]aresample=48000,apad[a]"
            command = [
                self.ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-loop",
                "1",
                "-framerate",
                str(self.settings.fps),
                "-i",
                str(input_image),
                "-i",
                str(audio_path),
                "-filter_complex",
                filter_graph,
                "-map",
                "[v]",
                "-map",
                "[a]",
                "-t",
                f"{duration:.3f}",
                "-r",
                str(self.settings.fps),
                "-c:v",
                "libx264",
                "-preset",
                "medium",
                "-crf",
                "19",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-movflags",
                "+faststart",
                str(output_path),
            ]
            self._run(command, f"第 {output_path.stem} 段视频合成失败")
        finally:
            if temp_image is not None and temp_image.is_file():
                temp_image.unlink()

    def concatenate(self, segment_paths: list[Path], output_path: Path) -> None:
        if not segment_paths:
            raise MediaError("没有可拼接的视频片段")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        concat_file = output_path.parent / "concat.txt"
        lines = []
        for path in segment_paths:
            escaped = str(path.resolve()).replace("'", "'\\''")
            lines.append(f"file '{escaped}'")
        concat_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        command = [
            self.ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_file),
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(output_path),
        ]
        self._run(command, "视频拼接失败")

    def mix_background_music(self, video_path: Path, output_path: Path) -> None:
        if not self.settings.background_music:
            return
        if not self.settings.background_music.is_file():
            raise MediaError(f"背景音乐不存在：{self.settings.background_music}")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        volume = self.settings.background_music_volume
        filter_graph = (
            f"[1:a]volume={volume:.3f},afade=t=in:st=0:d=1[bg];"
            "[0:a][bg]amix=inputs=2:duration=first:dropout_transition=2[a]"
        )
        command = [
            self.ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(video_path),
            "-stream_loop",
            "-1",
            "-i",
            str(self.settings.background_music),
            "-filter_complex",
            filter_graph,
            "-map",
            "0:v:0",
            "-map",
            "[a]",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-shortest",
            "-movflags",
            "+faststart",
            str(output_path),
        ]
        self._run(command, "背景音乐混音失败")

    def validate_video(self, video_path: Path) -> None:
        if not video_path.is_file() or video_path.stat().st_size < 1024:
            raise MediaError("最终视频不存在或文件过小")
        command = [
            self.ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-v",
            "error",
            "-i",
            str(video_path),
            "-f",
            "null",
            "-",
        ]
        self._run(command, "最终视频无法完整解码")

    def write_srt(self, storyboard: Storyboard, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        blocks: list[str] = []
        for index, page in enumerate(storyboard.pages, start=1):
            start = _srt_time(page.start_seconds)
            end = _srt_time(page.start_seconds + page.display_duration_seconds)
            blocks.append(f"{index}\n{start} --> {end}\n{page.narration}\n")
        destination.write_text("\n".join(blocks), encoding="utf-8-sig")

    def _run(self, command: list[str], context: str) -> None:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if completed.returncode != 0:
            details = completed.stderr.strip() or completed.stdout.strip()
            raise MediaError(f"{context}：{details[-3000:]}")


def _srt_time(seconds: float) -> str:
    total_ms = max(0, int(round(seconds * 1000)))
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def _render_subtitle(
    image_path: Path,
    text: str,
    width: int,
    height: int,
) -> Image.Image:
    image = Image.open(image_path).convert("RGB")
    if image.size != (width, height):
        image = image.resize((width, height), Image.Resampling.LANCZOS)
    draw = ImageDraw.Draw(image, "RGBA")
    font = _subtitle_font(max(26, int(height * 0.030)))
    lines = _wrap_text(draw, text.strip(), font, width - 300)[:2]
    if not lines:
        return image
    line_height = int(height * 0.046)
    padding = int(height * 0.020)
    box_height = line_height * len(lines) + padding * 2
    box_bottom = int(height * 0.80)
    box_top = box_bottom - box_height
    draw.rounded_rectangle(
        (120, box_top, width - 120, box_bottom),
        radius=16,
        fill=(5, 10, 24, 185),
    )
    y = box_top + padding
    for line in lines:
        line_width = draw.textlength(line, font=font)
        draw.text(
            ((width - line_width) / 2, y),
            line,
            font=font,
            fill=(245, 246, 250, 255),
        )
        y += line_height
    return image


def _subtitle_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        Path("C:/Windows/Fonts/msyh.ttc"),
        Path("C:/Windows/Fonts/simhei.ttf"),
        Path("C:/Windows/Fonts/simsun.ttc"),
        Path("/System/Library/Fonts/PingFang.ttc"),
    ]
    for path in candidates:
        if path.is_file():
            try:
                return ImageFont.truetype(str(path), size=size)
            except OSError:
                continue
    return ImageFont.load_default(size=size)


def _wrap_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    max_width: int,
) -> list[str]:
    if not text:
        return []
    lines: list[str] = []
    current = ""
    for char in text:
        candidate = current + char
        if current and draw.textlength(candidate, font=font) > max_width:
            lines.append(current)
            current = char
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines
