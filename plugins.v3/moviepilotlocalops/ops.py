"""本地运维工具集的核心实现：日志读取与受限命令执行。"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# 日志文件命名规则，覆盖 moviepilot.log 与其轮转文件 moviepilot.log.1 等。
LOG_FILE_PATTERN = re.compile(r"\.log(\.\d+)?$")
# 默认读取的日志文件名。
DEFAULT_LOG_NAME = "moviepilot.log"
# 单次读取的最大行数。
MAX_TAIL_LINES = 2000
# 单次检索的最大匹配条数。
MAX_SEARCH_MATCHES = 500
# 单次命令执行的最大超时。
MAX_TIMEOUT_SECONDS = 300
# 单次命令执行返回的最大字符数。
MAX_OUTPUT_CHARS = 200000
# 单个日志文件允许整体载入检索的大小上限。
MAX_SEARCH_FILE_BYTES = 32 * 1024 * 1024


class OpsError(Exception):
    """表示本地运维操作被策略拒绝或无法执行。"""


def _format_timestamp(timestamp: float) -> str:
    """把时间戳格式化为本地可读时间。"""
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")


def _list_files(log_path: Path) -> List[Path]:
    """列出日志目录及其 plugins 子目录下的日志文件。"""
    files: List[Path] = []
    if not log_path.is_dir():
        return files
    for base in (log_path, log_path / "plugins"):
        if not base.is_dir():
            continue
        for item in sorted(base.iterdir()):
            if item.is_file() and LOG_FILE_PATTERN.search(item.name):
                files.append(item)
    return files


def resolve_log_file(log_path: Path, filename: Optional[str]) -> Path:
    """把调用方给出的日志文件名解析为日志目录内的真实路径。"""
    base = log_path.resolve()
    if not base.is_dir():
        raise OpsError(f"日志目录不存在：{base}")
    raw = str(filename or "").strip() or DEFAULT_LOG_NAME
    candidate = Path(raw)
    if candidate.is_absolute():
        raise OpsError("日志文件名只能是日志目录内的相对文件名，不要使用绝对路径")
    if any(part in {"..", ""} for part in candidate.parts):
        raise OpsError("日志文件名不能包含上级目录跳转")
    target = (base / candidate).resolve()
    if not target.is_relative_to(base):
        raise OpsError("日志文件必须位于日志目录内")
    if not target.is_file():
        raise OpsError(f"日志文件不存在：{raw}")
    return target


def list_log_files(log_path: Path) -> Dict[str, Any]:
    """列出可读取的日志文件及其大小与修改时间。"""
    base = log_path.resolve()
    entries: List[Dict[str, Any]] = []
    for item in _list_files(base):
        stat = item.stat()
        entries.append(
            {
                "name": item.relative_to(base).as_posix(),
                "size": stat.st_size,
                "modified_at": _format_timestamp(stat.st_mtime),
                "is_current": item.name == DEFAULT_LOG_NAME,
            }
        )
    entries.sort(key=lambda entry: (not entry["is_current"], entry["name"]))
    return {
        "log_dir": str(base),
        "files": entries,
        "default_file": DEFAULT_LOG_NAME,
    }


def _tail_lines(path: Path, limit: int) -> Tuple[List[str], int]:
    """从文件尾部反向读取指定行数，返回行列表与文件总字节数。"""
    size = path.stat().st_size
    if size == 0:
        return [], 0
    chunk_size = 16384
    limit = max(1, min(int(limit), MAX_TAIL_LINES))
    with path.open("rb") as handle:
        position = size
        buffer = b""
        while position > 0 and buffer.count(b"\n") <= limit:
            read_size = min(chunk_size, position)
            position -= read_size
            handle.seek(position)
            buffer = handle.read(read_size) + buffer
    return buffer.decode("utf-8", errors="replace").splitlines()[-limit:], size


def read_log_tail(log_path: Path, filename: Optional[str], lines: int) -> Dict[str, Any]:
    """读取指定日志文件的末尾若干行。"""
    target = resolve_log_file(log_path, filename)
    tail, size = _tail_lines(target, lines)
    return {
        "file": target.relative_to(log_path.resolve()).as_posix(),
        "size": size,
        "returned_lines": len(tail),
        "lines": tail,
    }


def _search_one_file(
    path: Path,
    pattern: "re.Pattern[str]",
    context: int,
    max_matches: int,
) -> Tuple[List[Dict[str, Any]], int, bool]:
    """在单个日志文件中按正则检索，返回匹配、总行数与是否被截断。"""
    if path.stat().st_size > MAX_SEARCH_FILE_BYTES:
        raise OpsError(
            f"日志文件过大（超过 {MAX_SEARCH_FILE_BYTES // (1024 * 1024)}MB），请改用 tail 方式读取：{path.name}"
        )
    content = path.read_text(encoding="utf-8", errors="replace").splitlines()
    context = max(0, min(int(context), 20))
    matches: List[Dict[str, Any]] = []
    truncated = False
    for index, line in enumerate(content):
        if not pattern.search(line):
            continue
        if len(matches) >= max_matches:
            truncated = True
            break
        start = max(0, index - context)
        end = min(len(content), index + context + 1)
        matches.append(
            {
                "line": index + 1,
                "text": line,
                "context": [
                    {"line": number + 1, "text": content[number]}
                    for number in range(start, end)
                ],
            }
        )
    return matches, len(content), truncated


def search_log(
    log_path: Path,
    filename: Optional[str],
    keyword: str,
    context: int,
    max_matches: int,
    ignore_case: bool,
    all_files: bool,
) -> Dict[str, Any]:
    """在单个日志文件或全部日志文件中按正则检索关键字。"""
    raw_keyword = str(keyword or "")
    if not raw_keyword:
        raise OpsError("search 动作必须提供 keyword 正则")
    flags = re.IGNORECASE if ignore_case else 0
    try:
        pattern = re.compile(raw_keyword, flags)
    except re.error as error:
        raise OpsError(f"keyword 不是合法正则：{error}") from error

    max_matches = max(1, min(int(max_matches), MAX_SEARCH_MATCHES))
    base = log_path.resolve()
    if all_files:
        targets = _list_files(base)
        scope = "all"
    else:
        targets = [resolve_log_file(base, filename)]
        scope = "single"

    results: List[Dict[str, Any]] = []
    remaining = max_matches
    for target in targets:
        if remaining <= 0:
            break
        matches, total_lines, truncated = _search_one_file(
            target, pattern, context, remaining
        )
        remaining -= len(matches)
        results.append(
            {
                "file": target.relative_to(base).as_posix(),
                "total_lines": total_lines,
                "match_count": len(matches),
                "truncated": truncated,
                "matches": matches,
            }
        )
    return {
        "scope": scope,
        "keyword": raw_keyword,
        "max_matches": max_matches,
        "files": results,
        "match_count": sum(item["match_count"] for item in results),
    }


def _truncate_output(text: str, limit: int) -> Tuple[str, bool]:
    """按字符上限截断命令输出，并标记是否发生截断。"""
    if len(text) <= limit:
        return text, False
    return (
        text[:limit] + f"\n...[输出已截断，原始长度 {len(text)} 字符]",
        True,
    )


def run_restricted_command(
    *,
    command: str,
    allowlist: List[str],
    cwd: Optional[str],
    timeout: int,
    max_chars: int,
    root_path: Path,
    allow_outside_root: bool,
) -> Dict[str, Any]:
    """在可执行文件白名单与工作目录约束下运行单条命令。"""
    raw_command = str(command or "").strip()
    if not raw_command:
        raise OpsError("command 不能为空")
    try:
        parts = shlex.split(raw_command)
    except ValueError as error:
        raise OpsError(f"command 无法解析，请检查引号是否配对：{error}") from error
    if not parts:
        raise OpsError("command 不能为空")

    executable = parts[0]
    if "/" in executable or "\\" in executable:
        raise OpsError("只允许 PATH 中的可执行文件名，不要写绝对路径或相对路径")
    if executable not in allowlist:
        raise OpsError(
            f"可执行文件 {executable} 不在白名单内，当前白名单：{', '.join(allowlist) or '（空）'}"
        )
    resolved = shutil.which(executable)
    if not resolved:
        raise OpsError(f"在 PATH 中找不到可执行文件：{executable}")

    base = root_path.resolve()
    if cwd:
        candidate = Path(str(cwd)).expanduser()
        workdir = (candidate if candidate.is_absolute() else base / candidate).resolve()
        if not workdir.is_dir():
            raise OpsError(f"工作目录不存在：{cwd}")
        if not allow_outside_root and not workdir.is_relative_to(base):
            raise OpsError(
                f"工作目录必须位于 {base} 之内；如需在其它目录执行，请开启“允许在应用目录外执行”"
            )
    else:
        workdir = base

    safe_timeout = max(1, min(int(timeout), MAX_TIMEOUT_SECONDS))
    safe_max_chars = max(1000, min(int(max_chars), MAX_OUTPUT_CHARS))

    started = time.monotonic()
    try:
        completed = subprocess.run(  # noqa: S603 - 已按白名单与工作目录约束收敛
            [resolved, *parts[1:]],
            cwd=str(workdir),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=safe_timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        stdout, _ = _truncate_output(
            (error.stdout or b"").decode("utf-8", errors="replace")
            if isinstance(error.stdout, bytes)
            else (error.stdout or ""),
            safe_max_chars,
        )
        stderr, _ = _truncate_output(
            (error.stderr or b"").decode("utf-8", errors="replace")
            if isinstance(error.stderr, bytes)
            else (error.stderr or ""),
            safe_max_chars,
        )
        return {
            "success": False,
            "error": f"命令在 {safe_timeout} 秒内未结束，已被终止；可提高超时或缩小命令范围",
            "command": raw_command,
            "executable": resolved,
            "cwd": str(workdir),
            "timeout": safe_timeout,
            "stdout": stdout,
            "stderr": stderr,
            "duration_ms": int((time.monotonic() - started) * 1000),
        }
    except OSError as error:
        raise OpsError(f"启动命令失败：{error}") from error

    stdout, stdout_truncated = _truncate_output(completed.stdout or "", safe_max_chars)
    stderr, stderr_truncated = _truncate_output(completed.stderr or "", safe_max_chars)
    return {
        "success": completed.returncode == 0,
        "command": raw_command,
        "executable": resolved,
        "cwd": str(workdir),
        "returncode": completed.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "stdout_truncated": stdout_truncated,
        "stderr_truncated": stderr_truncated,
        "duration_ms": int((time.monotonic() - started) * 1000),
    }
