"""本地运维工具集向 Agent 与 MCP 暴露的工具类。"""

from __future__ import annotations

import json
from typing import Any, Optional, Type

from pydantic import BaseModel, Field

from app.agent.tools.base import MoviePilotTool
from app.agent.tools.tags import ToolTag
from app.sdk.plugin import PluginManager

# 插件类名，同时作为运行态插件查询键。
PLUGIN_ID = "MoviePilotLocalOps"


def get_running_plugin() -> Any:
    """获取运行态的本地运维工具集插件实例。"""
    return PluginManager().running_plugins.get(PLUGIN_ID)


def _unavailable(reason: str) -> str:
    """构造插件不可用时的统一错误结果。"""
    return json.dumps(
        {
            "success": False,
            "execution_outcome": "failed",
            "error": reason,
        },
        ensure_ascii=False,
    )


class LocalLogsInput(BaseModel):
    """本地日志查询工具的输入参数模型。"""

    action: str = Field(
        "list",
        description=(
            "One of: list, tail, search. 'list' returns log files with size and modification time; "
            "'tail' returns the last lines of one log file; 'search' runs a regular-expression search "
            "with surrounding context inside one file or across every log file."
        ),
    )
    filename: Optional[str] = Field(
        None,
        description=(
            "Log file name relative to the MoviePilot log directory, for example 'moviepilot.log', "
            "'moviepilot.log.1', or 'plugins/synccookiecloud.log'. Ignored for 'list' and when "
            "all_files is true. Defaults to 'moviepilot.log'."
        ),
    )
    keyword: Optional[str] = Field(
        None,
        description="Regular expression to search for. Required for the 'search' action.",
    )
    lines: Optional[int] = Field(
        200,
        description="Number of trailing lines to return for the 'tail' action, from 1 to 2000.",
    )
    context: Optional[int] = Field(
        2,
        description="Context lines kept before and after each match for the 'search' action, from 0 to 20.",
    )
    max_matches: Optional[int] = Field(
        50,
        description="Maximum number of matches returned by the 'search' action, from 1 to 500.",
    )
    ignore_case: Optional[bool] = Field(
        True, description="Whether the 'search' action ignores letter case."
    )
    all_files: Optional[bool] = Field(
        False,
        description=(
            "For the 'search' action, search every log file in the log directory (including rotated "
            "files and plugins/*.log) instead of one file."
        ),
    )


class LocalExecInput(BaseModel):
    """受限命令执行工具的输入参数模型。"""

    command: str = Field(
        ...,
        description=(
            "Single command line to run, for example \"rg -n error /config/logs/moviepilot.log\". "
            "The first token must be a bare executable name present in the plugin allowlist. "
            "Shell syntax is not evaluated: pipes, redirection, command substitution and chaining "
            "are passed through as literal arguments, so run one command per call."
        ),
    )
    cwd: Optional[str] = Field(
        None,
        description=(
            "Working directory, absolute or relative to the MoviePilot root. Defaults to the "
            "MoviePilot root and must stay inside it unless the plugin allows executing outside."
        ),
    )
    timeout: Optional[int] = Field(
        30, description="Execution timeout in seconds, from 1 to 300."
    )


class LocalLogsTool(MoviePilotTool):
    """读取 MoviePilot 日志文件的只读工具。"""

    name: str = "mp_logs"
    tags: list[str] = [ToolTag.Read, ToolTag.System, ToolTag.Admin]
    description: str = (
        "Read MoviePilot container logs without a shell. Use it to list log files, tail the newest "
        "log, or run a regular-expression search with context across current and rotated logs. "
        "Every path is confined to the MoviePilot log directory. Available actions: list, tail, search."
    )
    require_admin: bool = True
    args_schema: Type[BaseModel] = LocalLogsInput

    def get_tool_message(self, **kwargs) -> Optional[str]:
        """根据查询动作生成友好的执行提示。"""
        action = str(kwargs.get("action") or "list")
        if action == "search":
            return f"检索日志：{kwargs.get('keyword') or ''}"
        if action == "tail":
            return f"读取日志尾部：{kwargs.get('filename') or 'moviepilot.log'}"
        return "列出日志文件"

    async def run(
        self,
        action: str = "list",
        filename: Optional[str] = None,
        keyword: Optional[str] = None,
        lines: Optional[int] = 200,
        context: Optional[int] = 2,
        max_matches: Optional[int] = 50,
        ignore_case: Optional[bool] = True,
        all_files: Optional[bool] = False,
        **kwargs: Any,
    ) -> str:
        """执行日志查询并返回 JSON 字符串结果。"""
        plugin = get_running_plugin()
        if not plugin:
            return _unavailable("本地运维工具集插件未运行")
        return await plugin.tool_read_logs(
            action=action,
            filename=filename,
            keyword=keyword,
            lines=lines,
            context=context,
            max_matches=max_matches,
            ignore_case=bool(ignore_case),
            all_files=bool(all_files),
            actor=self._user_id,
        )


class LocalExecTool(MoviePilotTool):
    """在可执行文件白名单内运行命令的工具。"""

    name: str = "mp_local_exec"
    tags: list[str] = [ToolTag.System, ToolTag.Admin, ToolTag.Command]
    description: str = (
        "Run one command inside the MoviePilot container when the plugin explicitly enables it, "
        "with an executable allowlist, a working-directory boundary, a timeout and an output cap. "
        "Use it for local diagnostics that the business API cannot express, such as inspecting files, "
        "processes or disk usage. Shell syntax is not evaluated; run one command per call."
    )
    require_admin: bool = True
    args_schema: Type[BaseModel] = LocalExecInput

    def get_tool_message(self, **kwargs) -> Optional[str]:
        """生成命令执行提示消息。"""
        return f"执行受控命令：{kwargs.get('command') or ''}"

    async def run(
        self,
        command: str,
        cwd: Optional[str] = None,
        timeout: Optional[int] = 30,
        **kwargs: Any,
    ) -> str:
        """在插件策略允许时执行单条命令并返回 JSON 字符串结果。"""
        plugin = get_running_plugin()
        if not plugin:
            return _unavailable("本地运维工具集插件未运行")
        return await plugin.tool_exec_command(
            command=command,
            cwd=cwd,
            timeout=timeout,
            actor=self._user_id,
        )
