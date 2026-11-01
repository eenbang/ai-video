from __future__ import annotations

import json
import logging
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class RemoteServiceError(RuntimeError):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def _read_error(exc: HTTPError) -> str:
    try:
        body = exc.read().decode("utf-8", errors="replace")
    except Exception:
        body = ""
    if len(body) > 2000:
        body = body[:2000] + "..."
    return f"HTTP {exc.code}: {body or exc.reason}"


def post_json(
    url: str,
    payload: dict[str, Any],
    *,
    api_key: str = "",
    timeout: float = 180.0,
    retries: int = 3,
    logger: logging.Logger | None = None,
) -> dict[str, Any]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = Request(url, data=data, headers=headers, method="POST")
    return _request_json(request, timeout, retries, logger)


def get_bytes(
    url: str,
    *,
    timeout: float = 180.0,
    retries: int = 3,
    logger: logging.Logger | None = None,
) -> bytes:
    request = Request(url, method="GET")
    for attempt in range(1, retries + 1):
        try:
            with urlopen(request, timeout=timeout) as response:
                return response.read()
        except HTTPError as exc:
            if exc.code not in {408, 409, 429, 500, 502, 503, 504}:
                raise RemoteServiceError(_read_error(exc), exc.code) from exc
            if attempt == retries:
                raise RemoteServiceError(_read_error(exc), exc.code) from exc
            _wait(attempt, logger, _read_error(exc))
        except URLError as exc:
            if attempt == retries:
                raise RemoteServiceError(f"下载图片失败：{exc.reason}") from exc
            _wait(attempt, logger, str(exc.reason))
    raise AssertionError("unreachable")


def _request_json(
    request: Request,
    timeout: float,
    retries: int,
    logger: logging.Logger | None,
) -> dict[str, Any]:
    for attempt in range(1, retries + 1):
        try:
            with urlopen(request, timeout=timeout) as response:
                body = response.read().decode("utf-8")
            parsed = json.loads(body)
            if not isinstance(parsed, dict):
                raise RemoteServiceError("服务返回的 JSON 不是对象")
            return parsed
        except HTTPError as exc:
            if exc.code not in {408, 409, 429, 500, 502, 503, 504}:
                raise RemoteServiceError(_read_error(exc), exc.code) from exc
            if attempt == retries:
                raise RemoteServiceError(_read_error(exc), exc.code) from exc
            _wait(attempt, logger, _read_error(exc))
        except URLError as exc:
            if attempt == retries:
                raise RemoteServiceError(f"网络请求失败：{exc.reason}") from exc
            _wait(attempt, logger, str(exc.reason))
        except json.JSONDecodeError as exc:
            raise RemoteServiceError("服务返回了无法解析的 JSON") from exc
    raise AssertionError("unreachable")


def _wait(attempt: int, logger: logging.Logger | None, reason: str) -> None:
    delay = min(8.0, 1.5 * (2 ** (attempt - 1)))
    if logger:
        logger.warning("请求失败，%.1f 秒后重试：%s", delay, reason)
    time.sleep(delay)
