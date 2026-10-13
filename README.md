# MoviePilot Plugins

个人 MoviePilot 插件源（V3）。

## 作为插件源使用

在 MoviePilot 的 **设置 → 插件市场** 中把本仓库地址加入插件市场列表：

```
https://github.com/yuzhi-jiang/MoviePilot-Plugins
```

保存后刷新插件市场即可看到本仓库中的插件。

## 插件列表

| 插件 | 版本 | 说明 |
|---|---|---|
| [本地运维工具集](plugins.v3/moviepilotlocalops/README.md) | v1.0.0 | 把日志读取与受限命令执行注册为 Agent / MCP 工具 |

## 仓库结构

```text
.
├── package.v3.json          # V3 插件索引
└── plugins.v3/
    └── <plugin_id_lower>/   # 插件源码目录
```

## 安全提示

`本地运维工具集` 会注册两个工具：

- `mp_logs`：只读日志查询，路径锁定在 MoviePilot 的日志目录内。
- `mp_local_exec`：在可执行文件白名单内执行命令，**默认关闭**，需在插件配置中显式开启。

本仓库为 public 仓库，请勿在其中提交任何 token、Cookie、账号或其他敏感信息。
