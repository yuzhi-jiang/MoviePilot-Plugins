# 本地运维工具集（MoviePilotLocalOps）

把 MoviePilot 的**日志读取**与**受限命令执行**注册成 Agent / MCP 工具，用于排查启动失败、插件报错、路径映射、依赖缺失这类需要碰本机文件系统的问题。

## 背景

MoviePilot 内置 MCP 会隐藏 `execute_command`、`read_file`、`write_file`、`edit_file`、`apply_patch`、`search_web`、`read_skill`、`view_image` 这些本地高风险工具（见核心 `app/api/endpoints/mcp.py` 的 `MCP_HIDDEN_TOOLS`）。该隐藏只是收敛暴露面，并非权限边界。

`MCP_HIDDEN_TOOLS` 是硬编码常量，插件无法解锁原工具。但 MCP 的工具列表来自同一份运行态工具目录，只按**名字**过滤，因此插件通过 `get_agent_tools()` 注册**新名字**的工具后，这些工具会自动出现在 `GET /api/v1/mcp/tools` 并可直接调用。本插件就是这个机制的最小实现。

## 注册的工具

### `mp_logs`（只读）

| 参数 | 说明 |
|---|---|
| `action` | `list` 列出日志文件；`tail` 读取尾部；`search` 正则检索 |
| `filename` | 相对日志目录的文件名，如 `moviepilot.log`、`moviepilot.log.1`、`plugins/synccookiecloud.log` |
| `keyword` | `search` 使用的正则 |
| `lines` | `tail` 返回行数，1 - 2000 |
| `context` | `search` 上下文行数，0 - 20 |
| `max_matches` | `search` 最大匹配数，1 - 500 |
| `ignore_case` | `search` 是否忽略大小写 |
| `all_files` | `search` 是否跨全部日志（含轮转文件与 `plugins/*.log`） |

路径被强制约束在 `settings.LOG_PATH` 内：拒绝绝对路径与 `..`，拒绝解析后越出日志目录的目标。不经过 shell。

### `mp_local_exec`（默认关闭）

| 参数 | 说明 |
|---|---|
| `command` | 单条命令行，第一个词必须是白名单内的裸可执行文件名 |
| `cwd` | 工作目录，默认 MoviePilot 根目录，默认不得越出 |
| `timeout` | 超时秒数，1 - 300 |

约束：

- **不加 shell**：`shlex.split` 后用 `subprocess` 以 `shell=False` 执行。因此 `|`、`>`、`&&`、`$(...)` 不会被解释，会作为普通字符串参数传给程序——**一次只跑一条命令**。
- **可执行文件必须用裸文件名**：拒绝含 `/` 的写法，再用 `shutil.which` 在 `PATH` 中解析，避免指定任意路径的同名程序。
- **工作目录边界**：默认必须位于 `settings.ROOT_PATH` 之内，需要越出时得显式打开"允许在应用目录外执行"。
- **超时与输出上限**：超时强杀并返回已产生的输出；stdout/stderr 超过上限时截断并标记。

## 配置项

| 配置项 | 默认 | 说明 |
|---|---|---|
| 启用插件 | 关 | 关闭时两个工具都不注册 |
| 记录审计日志 | 开 | 每次调用写一行 JSON 到插件数据目录的 `audit.log`，超过 2MB 轮转为 `audit.log.1` |
| 日志默认读取行数 | 200 | `lines` 缺省值 |
| 允许命令执行 | 关 | 显式开启后 `mp_local_exec` 才可用 |
| 允许在应用目录外执行 | 关 | 放开工作目录边界 |
| 命令执行超时（秒） | 30 | 1 - 300 |
| 命令输出字符上限 | 20000 | 1000 - 200000 |
| 命令白名单 | 见下 | 每行一个可执行文件名 |

默认白名单：`ls` `head` `tail` `wc` `rg` `grep` `find` `stat` `sort` `uniq` `cut` `tr` `sed` `awk` `jq` `df` `du` `ps` `free` `uptime` `nproc` `id` `whoami` `date`。

刻意**不包含**解释器（`python`、`sh`、`node`）与 `printenv`、`env`：一旦允许，命令白名单就等价于任意代码执行或环境变量读取。

## 安全边界（必读）

- 两个工具都声明 `require_admin = True`。MCP 走 `API_TOKEN`，核心把它当管理员级集成入口，因此该门禁不会拦住 MCP 客户端。
- 白名单只校验命令的第一个词，**不限制参数路径**。例如 `rg`、`head` 仍能读取容器内任意可读文件，包括 `/config/app.env`。`mp_logs` 才是路径受约束的那个工具。
- 结论：一旦开启命令执行，任何持有有效 MCP 凭证的客户端实际获得容器内的读执行能力。请只把 MCP 接入给你信任的客户端，不要为了省事长期开着 `allow_exec`。
- 若需要更严的边界，建议把 `exec_allowlist` 收缩到只读诊断工具，或只启用 `mp_logs` 而保持 `allow_exec` 关闭。

## 安装（本地插件源）

1. 把本目录放入某个本地插件仓库，仓库结构为：

   ```text
   <repo>/
   ├── package.v3.json
   └── plugins.v3/
       └── moviepilotlocalops/
           ├── __init__.py
           ├── agenttools.py
           ├── ops.py
           └── README.md
   ```

2. 在 MoviePilot 系统设置中把 `PLUGIN_LOCAL_REPO_PATHS` 指向 `<repo>`（默认约定为 `/config/local-plugins`），相对路径按 MoviePilot 根目录解析。
3. 在插件市场搜索"本地运维工具集"安装，或直接对 `MoviePilotLocalOps` 执行插件安装。
4. `__init__.py` 的改动会在插件重载或重启后生效；首次安装需在插件详情页打开开关。

## 验证

- `moviepilot tool list` 应包含 `mp_logs` 与 `mp_local_exec`。
- `GET /api/v1/mcp/tools` 应包含同名条目。
- 关于工具列表刷新：核心 MCP 的 `initialize` 返回 `listChanged: false`，外部客户端需重连才会看到新工具。

## 卸载

删除本地插件仓库中的 `plugins.v3/moviepilotlocalops/` 并在插件市场卸载即可。审计日志位于插件数据目录（`/config/plugins/MoviePilotLocalOps/audit.log`），卸载插件不会自动删除，需手动清理。
