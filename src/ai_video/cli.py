from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import sys
from pathlib import Path

from . import __version__
from .config import Settings
from .pipeline import VideoPipeline


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ai-video",
        description="根据主题或文案生成恰好 5 页的图文配音讲解视频。",
    )
    parser.add_argument("source", nargs="?", help="主题或文案")
    parser.add_argument("--input-file", help="从 UTF-8 文本文件读取主题或文案")
    parser.add_argument(
        "--mode",
        choices=["auto", "api", "demo"],
        help="运行模式：auto（默认）、api、demo",
    )
    parser.add_argument(
        "--image-backend",
        choices=["demo", "openai", "comfyui"],
        help="图像生成后端（默认 demo，可覆盖 .env 中的 IMAGE_BACKEND）",
    )
    parser.add_argument(
        "--product-image",
        help="产品图片路径：用这张图作为第 1 页画面，跳过该页生图",
    )
    parser.add_argument(
        "--image-lecture",
        help="单图讲解：把一张产品图拆成 5 页，逐页配音讲解成展示视频",
    )
    parser.add_argument("--output-dir", help="任务输出目录，默认 outputs")
    parser.add_argument(
        "--resume",
        help="续跑已有任务：任务 ID 或任务目录",
    )
    parser.add_argument(
        "--resume-latest",
        action="store_true",
        help="续跑 outputs 下最近一次任务",
    )
    parser.add_argument("--archive", help="归档指定任务：任务 ID 或任务目录")
    parser.add_argument(
        "--archive-latest",
        action="store_true",
        help="归档 outputs 下最近一次任务",
    )
    parser.add_argument(
        "--archive-all",
        action="store_true",
        help="归档 outputs 下全部任务",
    )
    parser.add_argument("--delete", help="永久删除 _archive 内指定任务")
    parser.add_argument(
        "--delete-all",
        action="store_true",
        help="永久删除 _archive 内全部任务",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="检查依赖、FFmpeg、API 配置和中文字体，不生成视频",
    )
    parser.add_argument("--version", action="version", version=__version__)
    return parser


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")

    args = _parser().parse_args(argv)
    try:
        settings = Settings.load(args.mode, args.output_dir, args.image_backend)
    except Exception as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2

    def progress(step: int, total: int, message: str) -> None:
        print(f"[{step}/{total}] {message}", flush=True)

    if args.check:
        return _run_check(settings)

    if args.archive is not None or args.archive_latest or args.archive_all:
        return _run_archive(
            settings, args.archive, args.archive_latest, args.archive_all
        )

    if args.delete is not None or args.delete_all:
        return _run_delete(settings, args.delete, args.delete_all)

    if args.resume or args.resume_latest:
        task_dir = _resolve_task_dir(
            settings, args.resume, args.resume_latest
        )
        if task_dir is None:
            print("找不到可续跑的任务", file=sys.stderr)
            return 2
        print(f"续跑任务目录：{task_dir}", flush=True)
        try:
            result = VideoPipeline(settings, progress).resume(task_dir)
        except KeyboardInterrupt:
            print(
                "\n续跑已中断；已完成的中间文件保存在任务目录。",
                file=sys.stderr,
            )
            return 130
        except Exception as exc:
            print(f"\n续跑失败：{exc}", file=sys.stderr)
            return 1
        print(f"\n任务 ID：{result.task_id}")
        print(f"任务目录：{result.task_dir}")
        print(f"最终视频：{result.video_path}")
        print(f"运行清单：{result.manifest_path}")
        return 0

    if args.image_lecture:
        if args.source or args.input_file:
            print(
                "--image-lecture 与 source/--input-file 只能选一种",
                file=sys.stderr,
            )
            return 2
        image_path = Path(args.image_lecture).expanduser().resolve()
        if not image_path.is_file():
            print(f"产品图片不存在：{image_path}", file=sys.stderr)
            return 2
        try:
            result = VideoPipeline(settings, progress).run_image_lecture(image_path)
        except KeyboardInterrupt:
            print("\n任务已中断；已完成的中间文件保存在 outputs。", file=sys.stderr)
            return 130
        except Exception as exc:
            print(f"\n生成失败：{exc}", file=sys.stderr)
            return 1
        print(f"\n任务 ID：{result.task_id}")
        print(f"任务目录：{result.task_dir}")
        print(f"最终视频：{result.video_path}")
        print(f"运行清单：{result.manifest_path}")
        return 0

    if args.source and args.input_file:
        print("source 与 --input-file 只能选择一个", file=sys.stderr)
        return 2
    if args.input_file:
        try:
            source_text = Path(args.input_file).expanduser().read_text(
                encoding="utf-8-sig"
            )
        except OSError as exc:
            print(f"无法读取输入文件：{exc}", file=sys.stderr)
            return 2
    else:
        source_text = args.source or ""

    product_image: Path | None = None
    if args.product_image:
        product_image = Path(args.product_image).expanduser().resolve()
        if not product_image.is_file():
            print(f"产品图片不存在：{product_image}", file=sys.stderr)
            return 2

    try:
        result = VideoPipeline(settings, progress).run(
            source_text, product_image
        )
    except KeyboardInterrupt:
        print("\n任务已中断；已完成的中间文件保存在 outputs。", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"\n生成失败：{exc}", file=sys.stderr)
        print(
            "请查看最新任务目录中的 pipeline.log、task_status.json 和 manifest.json。",
            file=sys.stderr,
        )
        return 1

    print(f"\n任务 ID：{result.task_id}")
    print(f"任务目录：{result.task_dir}")
    print(f"最终视频：{result.video_path}")
    print(f"运行清单：{result.manifest_path}")
    return 0


def _resolve_task_dir(
    settings: Settings,
    arg: str | None,
    latest: bool,
) -> Path | None:
    if arg:
        path = Path(arg).expanduser()
        if path.is_absolute():
            return path if path.is_dir() else None
        if path.is_dir():
            return path
        candidate = settings.output_dir / arg
        return candidate if candidate.is_dir() else None
    if latest:
        candidates = [
            path
            for path in settings.output_dir.iterdir()
            if path.is_dir() and path.name != "_archive"
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda path: path.stat().st_mtime)
    return None


def _resolve_archive_dir(settings: Settings, arg: str) -> Path | None:
    archive_dir = settings.output_dir.resolve() / "_archive"
    path = Path(arg).expanduser()
    if path.is_absolute():
        return path if path.is_dir() else None
    if path.is_dir():
        return path
    candidate = archive_dir / arg
    return candidate if candidate.is_dir() else None


def _run_archive(
    settings: Settings,
    arg: str | None,
    latest: bool,
    archive_all: bool,
) -> int:
    output_dir = settings.output_dir.resolve()
    archive_dir = output_dir / "_archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    if archive_all:
        tasks = [
            path
            for path in sorted(output_dir.iterdir())
            if path.is_dir() and path.name != "_archive"
        ]
        if not tasks:
            print("没有可归档的任务")
            return 0
        for task in tasks:
            shutil.move(str(task), str(archive_dir / task.name))
            print(f"已归档：{task.name}")
        print(f"共归档 {len(tasks)} 个任务")
        return 0
    target = _resolve_task_dir(settings, arg, latest)
    if target is None:
        print("找不到要归档的任务", file=sys.stderr)
        return 2
    target = target.resolve()
    if (
        output_dir not in target.parents
        or target == archive_dir
        or archive_dir in target.parents
    ):
        print(
            f"只能归档 outputs 内、且不在 _archive 里的任务：{target}",
            file=sys.stderr,
        )
        return 2
    shutil.move(str(target), str(archive_dir / target.name))
    print(f"已归档：{target.name}")
    return 0


def _run_delete(
    settings: Settings,
    arg: str | None,
    delete_all: bool,
) -> int:
    archive_dir = settings.output_dir.resolve() / "_archive"
    if delete_all:
        if not archive_dir.is_dir():
            print("归档目录为空或不存在")
            return 0
        removed = 0
        for child in sorted(archive_dir.iterdir()):
            if child.is_dir():
                shutil.rmtree(child)
                print(f"已删除：{child.name}")
                removed += 1
        print(f"共删除 {removed} 个归档任务")
        return 0
    target = _resolve_archive_dir(settings, arg)
    if target is None:
        print("在 _archive 中找不到要删除的任务", file=sys.stderr)
        return 2
    target = target.resolve()
    if archive_dir not in target.parents:
        print(f"只能删除 _archive 内的任务：{target}", file=sys.stderr)
        return 2
    shutil.rmtree(target)
    print(f"已删除：{target.name}")
    return 0


def _run_check(settings: Settings) -> int:
    checks: list[tuple[str, bool, str]] = []
    checks.append(("Python", sys.version_info >= (3, 11), sys.version.split()[0]))
    for package in ("PIL", "edge_tts", "imageio_ffmpeg"):
        available = importlib.util.find_spec(package) is not None
        checks.append((package, available, "已安装" if available else "未安装"))
    try:
        import imageio_ffmpeg

        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        checks.append(("FFmpeg", Path(ffmpeg).is_file(), ffmpeg))
    except Exception as exc:
        checks.append(("FFmpeg", False, str(exc)))

    font_candidates = [
        Path("C:/Windows/Fonts/msyh.ttc"),
        Path("C:/Windows/Fonts/simhei.ttf"),
    ]
    font = next((path for path in font_candidates if path.is_file()), None)
    checks.append(("中文字体", font is not None, str(font or "未找到")))
    checks.append(
        (
            "OpenAI API",
            True,
            "已配置" if settings.api_key else "未配置，将使用演示兜底",
        )
    )
    effective = (
        "api"
        if settings.mode == "api"
        or (settings.mode == "auto" and settings.api_key)
        else "demo"
    )
    checks.append(("实际模式", True, effective))

    failed = False
    for name, passed, detail in checks:
        status = "OK" if passed else "FAIL"
        print(f"[{status:4}] {name}: {detail}")
        if not passed and name not in {"OpenAI API"}:
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

