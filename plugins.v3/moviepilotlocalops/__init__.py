"""本地运维工具集：把日志读取与受限命令执行注册为 MoviePilot Agent / MCP 工具。"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Type

from app.sdk.config import settings
from app.sdk.logging import logger
from app.sdk.plugin import _PluginBase

from .agenttools import LocalExecTool, LocalLogsTool
from .ops import (
    DEFAULT_LOG_NAME,
    MAX_OUTPUT_CHARS,
    MAX_TAIL_LINES,
    MAX_TIMEOUT_SECONDS,
    OpsError,
    list_log_files,
    read_log_tail,
    run_restricted_command,
    search_log,
)

# 默认命令白名单：只保留只读诊断类工具，刻意不包含解释器与 printenv/env，
# 避免一旦开启命令执行就等于放开任意代码执行与环境变量读取。
DEFAULT_EXEC_ALLOWLIST = (
    "ls",
    "head",
    "tail",
    "wc",
    "rg",
    "grep",
    "find",
    "stat",
    "sort",
    "uniq",
    "cut",
    "tr",
    "sed",
    "awk",
    "jq",
    "df",
    "du",
    "ps",
    "free",
    "uptime",
    "nproc",
    "id",
    "whoami",
    "date",
)

# 审计日志文件名。
AUDIT_LOG_NAME = "audit.log"
# 审计日志单文件大小上限，超过后轮转为 audit.log.1。
AUDIT_MAX_BYTES = 2 * 1024 * 1024


class MoviePilotLocalOps(_PluginBase):
    """本地运维工具集插件。"""

    # 插件名称
    plugin_name = "本地运维工具集"
    # 插件描述
    plugin_desc = "把日志读取与受限命令执行注册为 Agent / MCP 工具，用于排查 MoviePilot 运行时问题。"
    # 插件图标
    plugin_icon = "MoviePilotLocalOps.png"
    # 插件版本
    plugin_version = "1.0.0"
    # 插件作者
    plugin_author = "local"
    # 作者主页
    author_url = "https://github.com/jxxghp/MoviePilot"
    # 插件配置项ID前缀
    plugin_config_prefix = "moviepilotlocalops_"
    # 加载顺序
    plugin_order = 100
    # 可使用的用户级别
    auth_level = 1

    # 插件启用状态
    _enabled: bool = False
    # 是否允许命令执行
    _allow_exec: bool = False
    # 命令白名单
    _exec_allowlist: List[str] = []
    # 命令执行超时（秒）
    _exec_timeout: int = 30
    # 命令输出字符上限
    _exec_max_chars: int = 20000
    # 是否允许在应用目录之外执行命令
    _allow_outside_root: bool = False
    # 日志读取默认行数
    _log_max_lines: int = 200
    # 是否记录审计日志
    _audit_enabled: bool = True

    def init_plugin(self, config: Optional[Dict[str, Any]] = None) -> None:
        """根据插件配置初始化运行状态。"""
        self.stop_service()
        self._enabled = False
        self._allow_exec = False
        self._exec_allowlist = list(DEFAULT_EXEC_ALLOWLIST)
        self._exec_timeout = 30
        self._exec_max_chars = 20000
        self._allow_outside_root = False
        self._log_max_lines = 200
        self._audit_enabled = True

        if not config:
            return

        self._enabled = bool(config.get("enabled"))
        self._allow_exec = bool(config.get("allow_exec"))
        self._exec_allowlist = self._parse_allowlist(config.get("exec_allowlist"))
        self._exec_timeout = self._parse_int(config.get("exec_timeout"), 30, 1, MAX_TIMEOUT_SECONDS)
        self._exec_max_chars = self._parse_int(
            config.get("exec_max_chars"), 20000, 1000, MAX_OUTPUT_CHARS
        )
        self._allow_outside_root = bool(config.get("allow_outside_root"))
        self._log_max_lines = self._parse_int(
            config.get("log_max_lines"), 200, 1, MAX_TAIL_LINES
        )
        self._audit_enabled = bool(config.get("audit_enabled", True))

    def get_state(self) -> bool:
        """获取插件启用状态。"""
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """返回插件远程命令列表。"""
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        """返回插件 API 列表，本插件仅提供 Agent / MCP 工具。"""
        return []

    def get_agent_tools(self) -> List[Type[Any]]:
        """返回注册到 Agent 与 MCP 的工具类列表。"""
        return [LocalLogsTool, LocalExecTool]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """返回插件配置表单与默认配置。"""
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "enabled",
                                            "label": "启用插件",
                                            "hint": "启用后 mp_logs 与 mp_local_exec 会出现在 Agent 与 MCP 工具列表中",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "audit_enabled",
                                            "label": "记录审计日志",
                                            "hint": "把每次工具调用写入插件数据目录的 audit.log",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "log_max_lines",
                                            "label": "日志默认读取行数",
                                            "type": "number",
                                            "hint": f"1 - {MAX_TAIL_LINES}",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "allow_exec",
                                            "label": "允许命令执行",
                                            "hint": "默认关闭。开启后 mp_local_exec 才可执行命令，请只授权给你信任的 MCP 客户端",
                                            "persistent-hint": True,
                                            "color": "warning",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "allow_outside_root",
                                            "label": "允许在应用目录外执行",
                                            "hint": f"默认只允许在 {settings.ROOT_PATH} 内设置工作目录",
                                            "persistent-hint": True,
                                            "color": "warning",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "exec_timeout",
                                            "label": "命令执行超时（秒）",
                                            "type": "number",
                                            "hint": f"1 - {MAX_TIMEOUT_SECONDS}",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "exec_max_chars",
                                            "label": "命令输出字符上限",
                                            "type": "number",
                                            "hint": f"1000 - {MAX_OUTPUT_CHARS}",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextarea",
                                        "props": {
                                            "model": "exec_allowlist",
                                            "label": "命令白名单",
                                            "rows": 8,
                                            "placeholder": "每行一个可执行文件名，例如：\nls\nrg\nfind\ndf",
                                            "hint": "只校验命令的第一个词；白名单内的工具仍能读取容器内可读文件，请谨慎授权",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "warning",
                                            "variant": "tonal",
                                            "text": (
                                                "这两个工具不经过 MoviePilot 核心的 MCP 隐藏名单，因此一旦插件启用，"
                                                "任何持有有效凭证的 MCP 客户端都可以调用。请把 MCP 客户端当作管理员级信任对象。"
                                            ),
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                ],
            }
        ], {
            "enabled": False,
            "audit_enabled": True,
            "log_max_lines": 200,
            "allow_exec": False,
            "allow_outside_root": False,
            "exec_timeout": 30,
            "exec_max_chars": 20000,
            "exec_allowlist": "\n".join(DEFAULT_EXEC_ALLOWLIST),
        }

    def get_page(self) -> List[dict]:
        """返回插件详情页面，展示当前策略与最近的审计记录。"""
        if not self._enabled:
            return [
                {
                    "component": "VAlert",
                    "props": {
                        "type": "info",
                        "variant": "tonal",
                        "text": "插件未启用。启用后 mp_logs 与 mp_local_exec 才会注册到 Agent 与 MCP 工具列表。",
                    },
                }
            ]

        audit_lines = self._read_audit_tail(10)
        return [
            {
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 6},
                        "content": [
                            {
                                "component": "VCard",
                                "props": {"variant": "tonal", "title": "已注册工具"},
                                "content": [
                                    {
                                        "component": "VCardText",
                                        "text": "mp_logs：只读日志查询，路径锁定在日志目录内",
                                    },
                                    {
                                        "component": "VCardText",
                                        "text": "mp_local_exec：受限命令执行，受白名单、工作目录、超时与输出上限约束",
                                    },
                                ],
                            }
                        ],
                    },
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 6},
                        "content": [
                            {
                                "component": "VCard",
                                "props": {"variant": "tonal", "title": "当前策略"},
                                "content": [
                                    {
                                        "component": "VCardText",
                                        "text": f"日志目录：{settings.LOG_PATH}（默认文件 {DEFAULT_LOG_NAME}）",
                                    },
                                    {
                                        "component": "VCardText",
                                        "text": f"命令执行：{'已开启' if self._allow_exec else '已关闭'}",
                                    },
                                    {
                                        "component": "VCardText",
                                        "text": f"白名单：{', '.join(self._exec_allowlist) or '（空）'}",
                                    },
                                    {
                                        "component": "VCardText",
                                        "text": (
                                            f"超时 {self._exec_timeout} 秒，输出上限 {self._exec_max_chars} 字符，"
                                            f"工作目录{'可' if self._allow_outside_root else '不可'}越出 {settings.ROOT_PATH}"
                                        ),
                                    },
                                ],
                            }
                        ],
                    },
                ],
            },
            {
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 12},
                        "content": [
                            {
                                "component": "VCard",
                                "props": {"variant": "tonal", "title": "最近审计记录"},
                                "content": [
                                    {"component": "VCardText", "text": line}
                                    for line in (audit_lines or ["暂无记录"])
                                ],
                            }
                        ],
                    }
                ],
            },
        ]

    def stop_service(self) -> None:
        """停止插件后台服务并释放资源。"""
        return None

    async def tool_read_logs(
        self,
        action: str,
        filename: Optional[str] = None,
        keyword: Optional[str] = None,
        lines: Optional[int] = None,
        context: Optional[int] = 2,
        max_matches: Optional[int] = 50,
        ignore_case: bool = True,
        all_files: bool = False,
        actor: Optional[str] = None,
    ) -> str:
        """执行日志查询动作并把结果序列化为 JSON 字符串。"""
        if not self._enabled:
            return self._error("插件未启用，请先在插件配置中开启本地运维工具集")

        normalized_action = str(action or "list").strip().lower()
        log_path = Path(settings.LOG_PATH)
        try:
            if normalized_action == "list":
                data = list_log_files(log_path)
            elif normalized_action == "tail":
                data = read_log_tail(
                    log_path, filename, lines or self._log_max_lines
                )
            elif normalized_action == "search":
                data = search_log(
                    log_path,
                    filename,
                    keyword or "",
                    context if context is not None else 2,
                    max_matches or 50,
                    ignore_case,
                    all_files,
                )
            else:
                return self._error(f"不支持的 action：{action}，可用值：list、tail、search")
        except OpsError as error:
            self._write_audit("mp_logs", normalized_action, str(error), actor, False)
            return self._error(str(error))

        self._write_audit(
            "mp_logs",
            normalized_action,
            filename or keyword or "",
            actor,
            True,
        )
        return json.dumps(
            {"success": True, "action": normalized_action, "data": data},
            ensure_ascii=False,
        )

    async def tool_exec_command(
        self,
        command: str,
        cwd: Optional[str] = None,
        timeout: Optional[int] = None,
        actor: Optional[str] = None,
    ) -> str:
        """在插件策略允许时执行单条命令并把结果序列化为 JSON 字符串。"""
        if not self._enabled:
            return self._error("插件未启用，请先在插件配置中开启本地运维工具集")
        if not self._allow_exec:
            self._write_audit("mp_local_exec", "exec", command, actor, False)
            return self._error("命令执行未开启，请在插件配置中显式打开“允许命令执行”")

        try:
            data = run_restricted_command(
                command=command,
                allowlist=self._exec_allowlist,
                cwd=cwd,
                timeout=timeout or self._exec_timeout,
                max_chars=self._exec_max_chars,
                root_path=Path(settings.ROOT_PATH),
                allow_outside_root=self._allow_outside_root,
            )
        except OpsError as error:
            self._write_audit("mp_local_exec", "exec", command, actor, False)
            return self._error(str(error))

        self._write_audit("mp_local_exec", "exec", command, actor, bool(data.get("success")))
        return json.dumps({"action": "exec", "data": data}, ensure_ascii=False)

    @staticmethod
    def _error(message: str) -> str:
        """构造统一的失败结果。"""
        return json.dumps(
            {
                "success": False,
                "execution_outcome": "failed",
                "error": message,
            },
            ensure_ascii=False,
        )

    @staticmethod
    def _parse_int(value: Any, default: int, minimum: int, maximum: int) -> int:
        """把配置值解析为指定范围内的整数。"""
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return default
        return max(minimum, min(parsed, maximum))

    @staticmethod
    def _parse_allowlist(value: Any) -> List[str]:
        """把配置值解析为去重后的命令白名单。"""
        if isinstance(value, str):
            items = value.splitlines()
        elif isinstance(value, (list, tuple, set)):
            items = [str(item) for item in value]
        else:
            return list(DEFAULT_EXEC_ALLOWLIST)
        allowlist: List[str] = []
        for item in items:
            name = str(item).strip()
            if name and name not in allowlist:
                allowlist.append(name)
        return allowlist

    def _audit_path(self) -> Path:
        """返回审计日志文件路径。"""
        return self.get_data_path() / AUDIT_LOG_NAME

    def _write_audit(
        self,
        tool: str,
        action: str,
        detail: str,
        actor: Optional[str],
        success: bool,
    ) -> None:
        """追加一条审计记录，失败时不影响工具返回。"""
        if not self._audit_enabled:
            return
        try:
            audit_path = self._audit_path()
            if audit_path.exists() and audit_path.stat().st_size > AUDIT_MAX_BYTES:
                audit_path.replace(audit_path.with_name(f"{AUDIT_LOG_NAME}.1"))
            record = {
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "tool": tool,
                "action": action,
                "actor": actor or "",
                "success": bool(success),
                "detail": str(detail or "")[:500],
            }
            with audit_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as error:
            logger.error(f"写入本地运维工具集审计日志失败：{error}")

    def _read_audit_tail(self, limit: int) -> List[str]:
        """读取最近的审计记录用于详情页展示。"""
        try:
            audit_path = self._audit_path()
            if not audit_path.is_file():
                return []
            with audit_path.open("r", encoding="utf-8", errors="replace") as handle:
                lines = handle.read().splitlines()
            return [line for line in lines[-limit:] if line.strip()]
        except Exception as error:
            logger.error(f"读取本地运维工具集审计日志失败：{error}")
            return []
