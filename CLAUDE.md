# claude-ext 技术文档

基于 Claude Code CLI 构建的可扩展框架。核心理念：Claude Code 本身已经是一个完整的 AI coding agent，本框架只做两件事——**封装 CLI 调用** 和 **管理独立扩展的生命周期**。不重复造轮子。

## 目录结构

```
claude-ext/
├── core/                          # 核心层（稳定，极少修改）
│   ├── engine.py                  # Claude Code CLI 封装 + SessionManager 入口 + services 注册表
│   ├── extension.py               # 扩展基类（接口契约）
│   ├── bridge.py                  # Unix socket RPC 桥接（主进程 ↔ MCP 子进程）
│   ├── mcp_base.py                # MCP stdio server 基类（JSON-RPC 样板）
│   ├── pending.py                 # 异步请求/响应注册表（register → wait → resolve）
│   ├── registry.py                # 扩展发现、加载、生命周期
│   ├── session.py                 # tmux-backed 多会话管理（核心模块）
│   └── status.py                  # 状态查询（auth + usage API）
├── extensions/                    # 扩展层（每个子目录完全独立）
│   ├── telegram/
│   │   ├── extension.py           # Telegram Bot 桥接（多会话）
│   │   └── requirements.txt       # 该扩展的独立依赖
│   ├── cron/
│   │   ├── extension.py           # 定时任务调度器 + MCP 工具注册
│   │   ├── mcp_server.py          # MCP stdio server（Claude 可调用的 cron 工具）
│   │   ├── store.py               # Job 持久化（JSON + flock）
│   │   └── requirements.txt       # croniter
│   ├── vault/
│   │   ├── extension.py           # 加密凭证库（bridge + MCP + 访问控制）
│   │   ├── store.py               # VaultStore: Fernet 加解密 + 统一 lockfile
│   │   ├── mcp_server.py          # MCP stdio server（vault 四工具）
│   │   └── requirements.txt       # cryptography>=42.0
│   ├── memory/
│   │   ├── extension.py           # 跨 session 记忆（MCP 注册 + 系统提示 + seed）
│   │   ├── store.py               # MemoryStore: Markdown I/O + 路径安全 + flock
│   │   └── mcp_server.py          # MCP stdio server（memory 五工具）
│   └── ask_user/
│       ├── extension.py           # 交互式提问扩展（bridge + PendingStore）
│       └── mcp_server.py          # MCP stdio server（ask_user 工具）
├── config.yaml                    # 全局配置（引擎参数 + 扩展开关 + 扩展配置）（.gitignore）
├── config.yaml.example            # 配置模板（已跟踪）
├── main.py                        # 入口（加载配置 → 创建引擎 → 初始化会话 → 注册扩展 → 运行）
├── requirements.txt               # 全局 Python 依赖
└── CLAUDE.md                      # 本文档
```

---

## 架构总览

```
┌─────────────┐     ┌──────────────────────────────────────────┐
│  config.yaml │────▶│              main.py                      │
└─────────────┘     │  load config → build engine →             │
                    │  init_sessions → recover → registry.start │
                    └──────────┬─────────────────────────────── ┘
                               │
              ┌────────────────┼────────────────┐
              ▼                ▼                 ▼
     ┌──────────────┐ ┌──────────────┐ ┌──────────────┐
     │  Extension A  │ │  Extension B  │ │  Extension C  │
     │  (telegram)   │ │  (cron)       │ │  (未来扩展)   │
     │  用户桥接     │ │  调度+MCP工具  │ │               │
     └──────┬───────┘ └──────┬───────┘ └──────┬───────┘
            │ cb_tg          │ cb_cron         │
            ▼                ▼                 ▼
     ┌─────────────────────────────────────────────┐
     │       ClaudeEngine + SessionManager          │
     │                                              │
     │  SessionManager（多会话，推荐）:              │
     │    tmux session → run.sh → claude -p         │
     │    文件 IPC: prompt.txt → stream.jsonl       │
     │    状态持久化: state.json                     │
     │    与主进程解耦，支持崩溃恢复                  │
     │    delivery_callbacks: [cb_tg, cb_cron, ...]  │
     │    mcp_servers: {"cron": {...}}  → run.sh     │
     │                                              │
     │  engine.ask()（轻量单次调用，向后兼容）:       │
     │    直接 subprocess → claude -p                │
     └─────────────────────────────────────────────┘
                         │
                         ▼
                   tmux sessions
                   └── claude -p ... --mcp-config mcp_config.json
```

**数据流**：主方向为 扩展 → engine/session_manager → tmux → CLI。反向通道：CLI 内的 MCP server → bridge.sock (Unix socket RPC) → 主进程 handler → PendingStore/deliver。扩展之间不直接通信，通过 `engine.services` 共享服务实例。

---

## 核心层详解

### core/session.py — SessionManager（核心模块）

**tmux-backed 多会话管理器。每个 Claude Code 会话运行在独立的 tmux session 中，与主进程完全解耦。**

#### Session 数据结构

```python
@dataclass
class Session:
    id: str                    # UUID
    name: str                  # 用户可见名称
    slot: int                  # 固定槽位号（1-N），删除后可被复用
    user_id: str               # 所属用户（通用字符串标识，如 str(telegram_user_id)）
    working_dir: str           # Claude Code 工作目录
    context: dict              # 扩展自定义路由数据（如 Telegram 放 {"chat_id": ...}）
    status: SessionStatus      # idle / busy / dead / stopped
    claude_session_id: str     # Claude CLI session UUID（用于 --resume）
    tmux_session: str          # tmux session 名称 "cc-{uuid}"
    prompt_count: int          # 已发送 prompt 数
```

#### DeliveryCallback 签名

```python
# (session_id, result_text, metadata)
DeliveryCallback = Callable[[str, str, dict], Awaitable[None]]
```

扩展通过 `session_manager.sessions[session_id]` 获取 session 对象，从 `session.context` 读取路由信息（如 chat_id）。core 不传递任何扩展特有的路由参数。

#### create_session 签名

```python
async def create_session(
    self, name: str, user_id: str, working_dir: str,
    context: dict | None = None,
) -> Session
```

`context` 字段用于扩展自定义数据。例如 Telegram 扩展传入 `{"chat_id": chat_id}`，Slack 扩展可传入 `{"channel_id": ...}`。core 不解读 context 内容，仅原样持久化和恢复。

#### 文件式 IPC

每个 session 在 `~/.claude-ext/sessions/{uuid}/` 下有：

| 文件 | 用途 |
|------|------|
| `state.json` | 持久化的 session 元数据（原子写入） |
| `prompt.txt` | 当前 prompt 内容 |
| `claude_cmd.sh` | 内层脚本：实际 claude 调用（含 `--output-format stream-json --verbose`） |
| `run.sh` | 外层脚本：PTY 包装（`script -qfec` → 强制 line buffering） |
| `stream.jsonl` | claude 的流式 JSON 输出（由 `script` 写入，逐行增长） |
| `stderr.log` | 错误输出 |
| `exitcode` | 完成标记（文件存在 = 命令结束），内容为 claude 退出码 |
| `mcp_config.json` | 可选。MCP server 配置（含 session-specific 环境变量） |

#### run.sh 双文件模板

**问题**：`claude -p ... > file.jsonl` 时，Node.js 对文件 stdout 采用 block buffering，导致文件长时间为 0 字节。
**解决**：`script -qfec` 创建 PTY → Node.js 检测到 TTY → line buffering → 事件逐行写入。

**claude_cmd.sh**（内层，实际 claude 调用）：
```bash
#!/bin/bash
unset CLAUDECODE
PROMPT=$(cat "/path/prompt.txt")
cd "/working/dir"
claude -p "$PROMPT" --output-format stream-json --verbose \
  --session-id "uuid" \           # 首次用 --session-id
  # 或 --resume "uuid"            # 后续用 --resume
  --permission-mode bypassPermissions \
  --mcp-config "/path/mcp_config.json" \  # 可选
  2>"/path/stderr.log"
```

**run.sh**（外层，PTY 包装）：
```bash
#!/bin/bash
script -qfec "bash /path/claude_cmd.sh" /path/stream.jsonl
echo $? > /path/exitcode
```

关键变化（相比旧的单文件 run.sh）：
- `--output-format json` → `--output-format stream-json --verbose`
- stdout 不再重定向到 `output.json`，由 `script` 写入 `stream.jsonl`
- `script -f` 每次写入后 flush；`-e` 传递子进程 exit code
- `stream.jsonl` 开头可能有一行 script header（非 JSON），解析时跳过
- `exitcode` 仍是完成信号

**关于 prompt 安全性**：`PROMPT=$(cat file)` 将文件内容赋值给变量时不会发生 shell 解释。`"$PROMPT"` 作为双引号包裹的变量传给 `claude -p` 时，内容中的 `$`、反引号等特殊字符不会被递归解释。这是 bash 变量展开的标准行为。

#### 核心方法

| 方法 | 职责 |
|------|------|
| `create_session()` | 分配槽位 + 创建 tmux session + 状态目录 + 启动 queue worker |
| `send_prompt()` | 将 prompt 放入 per-session 队列，返回队列位置。拒绝 DEAD session，自动重置 STOPPED |
| `stop_session()` | 清空队列 → 标记 STOPPED → Ctrl-C → 后台 5s 写 exitcode（非阻塞）。对 IDLE session 仅清空队列。返回 `(bool, int)` |
| `destroy_session()` | 杀 tmux + 取消 worker + 删除状态目录 |
| `recover()` | 启动时扫描磁盘状态 + 检查 tmux 存活，恢复/重连（结果缓冲到 pending） |
| `shutdown()` | 取消所有 worker，**不杀 tmux session**（核心设计点） |
| `add_delivery_callback()` | 注册结果投递回调（支持多个）。首次注册时刷新 recover 期间缓冲的待投递结果 |
| `register_mcp_server()` | 注册 MCP server 配置。所有后续 session 的 run.sh 自动添加 `--mcp-config` |
| `register_env_unset()` | 注册需要在 Claude session 中 unset 的环境变量（防敏感信息泄漏） |

#### 槽位机制

每个用户最多 N 个并发 session（默认 5，可配置）。每个 session 创建时分配最小可用槽位号。槽位号在 session 生命周期内固定不变，删除后释放供新 session 复用。用户通过槽位号而非列表序号引用 session，避免删除后编号混乱。自动命名规则为 `session-{slot}`，确保槽位号与名称数字一致（如 `#1 session-1`、`#2 session-2`）。

#### 队列机制

每个 session 有独立的 asyncio.Queue 和 worker task。多条消息发给同一个 session 时自动排队，依次执行。`/stop` 会同时清空队列。worker 在取出 prompt 后会检查 session 状态，跳过 STOPPED 和 DEAD 的 session。

#### 状态机

```
创建 → IDLE → (send_prompt) → BUSY → (完成) → IDLE
                                   → (stop) → STOPPED → (send_prompt) → IDLE
                                   → (tmux 死亡) → DEAD
```

**状态保护**：`_execute_prompt` 在流式循环返回后检查 `session.status`，如果已被 `stop_session` 设为 STOPPED 则投递 `is_stopped` 通知而非静默丢弃。

#### 启动恢复矩阵

| state.json status | tmux 存活? | exitcode 存在? | 动作 |
|---|---|---|---|
| busy | Yes | Yes | 用 `_parse_stream_result` 读取结果，标记 idle，缓冲到 pending_deliveries |
| busy | Yes | No | 仍在运行，恢复流式监控 |
| busy | No | — | 标记 dead |
| idle/stopped | Yes | — | 直接重连 |
| idle/stopped | No | — | 重建 tmux session |
| dead | — | — | 加载到内存，用户可查看并 /delete 清理 |

**关键时序**：`recover()` 在 `start_all()` 之前执行（此时 delivery callback 尚未设置），因此完成的结果缓冲到 `_pending_deliveries`。当扩展调用 `add_delivery_callback()` 时自动刷新缓冲区。

#### 流式输出与心跳

SessionManager 使用 `--output-format stream-json --verbose`，通过 `_stream_completion` 增量读取 `stream.jsonl`，实时将事件投递给 delivery callback。

**流式事件分类**（`_classify_stream_event`）：

| 事件类型 | content block | 动作 | metadata |
|----------|--------------|------|----------|
| `assistant` | `text` | **投递** | `{"is_stream": True, "stream_type": "text"}` |
| `assistant` | `tool_use` | **投递** | `{"is_stream": True, "stream_type": "tool_use", "tool_name": ..., "tool_input": ...}` |
| `assistant` | `thinking` | 跳过 | — |
| `user` | `tool_result` | 跳过 | — |
| `system` | — | 跳过 | — |
| `result` | — | 提取 metadata | `{"is_final": True, "claude_session_id": ..., "total_cost_usd": ..., ...}` |

**Delivery metadata 约定**：

| 字段 | 含义 |
|------|------|
| `is_stream: True` | 这是一个流式中间事件 |
| `stream_type: "text"` | Claude 的文字回复 |
| `stream_type: "tool_use"` | Claude 调用了工具 |
| `is_heartbeat: True` | 心跳事件（仅在无近期投递时发送） |
| `is_final: True` | 任务完成，包含 cost/turns 等汇总信息 |
| `is_stopped: True` | 任务被 /stop 中断 |
| `is_error: True` | 发生错误（超时、tmux 死亡等） |

**心跳**：仅在最后一次投递后超过 30 秒无新事件时发送（`HEARTBEAT_INTERVAL = 30.0`）。流式事件本身就是活跃信号，因此正常执行时不会触发心跳。

#### MCP Server 注册

扩展可通过 `register_mcp_server()` 给 Claude session 提供自定义工具：

```python
# 扩展的 start() 中注册
self.engine.session_manager.register_mcp_server("cron", {
    "command": "python",
    "args": ["/path/to/mcp_server.py"],
    "env": {"CRON_STORE_PATH": "/path/to/store.json"},
})
```

SessionManager 自动为每个 session 生成 `mcp_config.json`，注入 session-specific 环境变量（`CLAUDE_EXT_SESSION_ID`、`CLAUDE_EXT_STATE_DIR`），并在 run.sh 中添加 `--mcp-config` 标志。MCP server 进程通过这些环境变量获取当前 session 的上下文。

#### 环境变量隔离

扩展可通过 `register_env_unset()` 注册需要在 Claude session 中清除的环境变量，防止敏感信息泄漏到 LLM 可访问的进程环境中：

```python
# vault 扩展 start() 中注册
self.sm.register_env_unset("CLAUDE_EXT_VAULT_PASSPHRASE")
```

SessionManager 在生成 `claude_cmd.sh` 时将所有已注册的变量与 `CLAUDECODE` 一起 unset。

#### 多 Delivery Callback

`add_delivery_callback()` 支持注册多个回调。每次结果投递时所有回调都被触发，各自根据 `session.context` 判断是否需要处理：

```python
# Telegram callback: 检查 context["chat_id"]
# Cron callback: 检查 context["cron_job_id"]
# 两者对同一事件独立触发，互不干扰
```

#### 活跃 session 持久化（扩展层职责）

活跃 session 选择是扩展层的 UX 概念，不属于 core。Telegram 扩展自行管理 `active_sessions.json` 的读写和清理。其他扩展可使用不同的持久化策略或不持久化。

### core/engine.py — ClaudeEngine

提供两种调用模式：

1. **SessionManager（推荐）**：通过 `engine.session_manager` 访问，tmux-backed，支持多会话、崩溃恢复、异步投递。Telegram 扩展使用此模式。
2. **`engine.ask()`（轻量）**：直接 subprocess 调用 `claude -p`，同步等待结果。适合不需要持久化/多会话的简单扩展（如 cron、webhook）。

**`engine.services: dict[str, Any]`** — 跨扩展服务注册表。扩展在 `start()` 中注册服务实例，其他扩展通过 `.get()` 查找。`config.yaml` 的 `enabled` 列表顺序即加载/启动顺序，先启动的扩展先注册 service。

```python
# SessionManager 模式（Telegram 等需要多会话的扩展）
session = await engine.session_manager.create_session(name="task1", ...)
await engine.session_manager.send_prompt(session.id, "fix the bug")

# 轻量模式（简单一次性调用）
response = await engine.ask(prompt="what is 1+1", cwd="/tmp")

# 跨扩展服务发现
self.engine.services["vault"] = self._vault              # vault 扩展 start() 中注册
vault = self.engine.services.get("vault")                # crypto 扩展中查找
```

### core/bridge.py — Bridge RPC

Unix Domain Socket 桥接，让 MCP 子进程（sync blocking）回调主进程（async）。

- **BridgeServer**：主进程 async 端。`add_handler(handler)` 注册多个 handler，请求依次尝试，首个非 None 响应胜出
- **BridgeClient**：MCP 子进程 sync blocking 端。`call(method, params, timeout)` 发送请求并阻塞等待响应，支持自动重连
- **协议**：行分隔 JSON（`{"method": ..., "params": ...}` → `{"result": ...}`），Unix Domain Socket
- **零核心模块依赖**：仅 stdlib（json, socket, asyncio）

### core/pending.py — PendingStore

通用异步 register → wait → resolve 模式。

- **PendingEntry** 数据结构：`key`（16-char hex）, `session_id`, `data`（扩展自定义 payload）, `future`, `timeout`
- `register(session_id, data, timeout)` → 创建 entry，返回供 `await wait(key)` 使用
- `resolve(key, value)` → 交付响应，唤醒 waiter
- `cancel_for_session(session_id)` → 批量取消（如 session 被 stop/destroy）
- `get_for_session(session_id)` → 获取某 session 的待处理 entry（前端用于展示 UI）
- **使用场景**：ask_user（当前）、未来可用于 email_wait、approval_gate 等

### core/mcp_base.py — MCPServerBase

MCP stdio JSON-RPC 协议样板代码，扩展的 MCP server 只需继承并定义 tools + handlers。

- 通过环境变量获取 session 上下文：`CLAUDE_EXT_SESSION_ID`、`CLAUDE_EXT_STATE_DIR`
- `session_context()` 读取当前 session 的 `state.json`（获取 user_id、context 等）
- 惰性 `bridge` 属性：仅在 `CLAUDE_EXT_BRIDGE_SOCKET` 存在时初始化 BridgeClient
- 子类只需设置 `name`、`tools`（schema list）和 `self.handlers`（name → callable 映射）
- 零外部依赖（纯 stdlib）

### core/extension.py — Extension 基类

**这是扩展唯一需要遵守的接口契约：**

```python
class Extension(ABC):
    name: str = "unnamed"

    def configure(self, engine: ClaudeEngine, config: dict) -> None:
        """启动前调用一次，接收 engine 实例和该扩展的配置字典"""
        self.engine = engine
        self.config = config

    @abstractmethod
    async def start(self) -> None: ...  # 启动（开始轮询、打开 webhook 等）

    @abstractmethod
    async def stop(self) -> None: ...   # 优雅停止
```

扩展通过 `self.engine.session_manager` 管理多会话，或通过 `self.engine.ask()` 做简单调用。

### core/registry.py — Registry

扩展的发现和生命周期管理。**core 从不硬编码 import 任何扩展。**

发现机制：扫描 `extensions/` 下所有包含 `extension.py` 的子目录。
加载机制：`importlib.import_module(f"extensions.{name}.extension")` 动态导入，获取其中的 `ExtensionImpl` 类。
生命周期：`load()` → `start_all()` → （运行中）→ `stop_all()`（逆序停止）。

### core/status.py — 状态查询

独立工具模块，不依赖任何扩展。提供三个函数：

| 函数 | 数据来源 | 返回内容 |
|------|----------|----------|
| `get_auth_info()` | `claude auth status` 命令 | `{loggedIn, email, subscriptionType, ...}` |
| `get_usage()` | `GET api.anthropic.com/api/oauth/usage`（读取 `~/.claude/.credentials.json` 中的 OAuth token） | `{five_hour: {utilization, resets_at}, seven_day: {...}, extra_usage: {...}}` |
| `format_status(auth, usage, session)` | 上述两者 + session 元数据 | 格式化的文本字符串 |

---

## 扩展层详解

### 如何添加新扩展（完整步骤）

以添加一个名为 `discord` 的扩展为例：

**第一步：创建目录和文件**

```
extensions/discord/
├── __init__.py          # 空文件
├── extension.py         # 必须包含 ExtensionImpl 类
└── requirements.txt     # 该扩展的独立依赖（可选）
```

**第二步：实现 ExtensionImpl**

```python
# extensions/discord/extension.py
from core.extension import Extension

class ExtensionImpl(Extension):
    name = "discord"

    def configure(self, engine, config):
        super().configure(engine, config)
        self.token = config["token"]

    async def start(self) -> None:
        # 注册 delivery callback（如需多会话）
        self.engine.session_manager.add_delivery_callback(self._deliver)
        # 启动逻辑
        ...

    async def stop(self) -> None:
        # 停止逻辑
        ...
```

**第三步：在 config.yaml 中注册**

```yaml
enabled:
  - telegram
  - discord

extensions:
  discord:
    token: "YOUR_DISCORD_BOT_TOKEN"
```

**完成。无需修改 core/ 下任何文件，无需修改 main.py，无需修改其他扩展。**

### 现有扩展：telegram

Telegram Bot 桥接，基于 tmux 多会话管理。

**命令：**

| 命令 | 功能 |
|------|------|
| `/start` | 欢迎信息 |
| `/new [name] [dir]` | 创建新 session（可选名称和工作目录）。单参数为目录时自动识别。目录支持 `~` 展开和相对于 `working_dir` 的解析 |
| `/sessions` | 列出所有 session，`*` 标记当前活跃 |
| `/switch <slot\|name>` | 切换活跃 session（按槽位号或名称） |
| `/status` | Auth + Usage + 当前 session 信息 |
| `/stop` | 停止当前 session 的运行任务 + 清空队列 |
| `/delete <slot\|name> [force]` | 删除 session（杀 tmux + 清文件）。BUSY session 需 `force` |

**消息处理流程：**
1. 用户发消息 → 检查是否有活跃 session，没有则自动创建 `session-1`（槽位 #1）。自动选择时优先 IDLE > STOPPED > BUSY，跳过 DEAD
2. 活跃 session busy → 消息入队列，回复 "Queued (position N)"
3. 活跃 session idle → 提交 prompt，回复 "Processing..."
4. 后台 worker 执行完成 → 通过 delivery callback 异步投递结果到 chat

**流式投递**：Claude 的每一步操作实时推送到 Telegram。文字事件做 2 秒 debounce 聚合（避免消息洪水），工具调用事件立即发送。

| 事件 | 显示 |
|------|------|
| 文字回复 | 聚合后发送完整文本块 |
| 工具调用 | `🔧 Read: /path/to/file.py`、`🔧 Bash: git status...` 等摘要 |
| 任务完成 | `--- $cost \| N turns ---` |
| 任务停止 | `Task stopped.` |
| 心跳 | `Still working... (Nm elapsed)` |

每条消息前加 `[#slot name]` 前缀标识来源。长消息按换行符边界分片（上限 4000 字符），发送失败时中断后续分片避免 log 刷屏。

**Debounce 机制**：每个 session 维护一个 `_StreamBuffer`。文字事件追加到 buffer 并重置 2 秒定时器。定时器到期、工具调用、或任务结束时 flush 发送。

### 现有扩展：cron

定时任务调度器。同时扮演两个角色：
1. **Scheduler**：按 cron 表达式或一次性延迟触发 Claude 会话执行任务。
2. **MCP 工具提供者**：注册 MCP server 让 Claude 在对话中动态创建/管理定时任务。

#### 两种 Job 来源

| 来源 | 创建方式 | 典型场景 |
|------|----------|----------|
| 静态（config.yaml） | 管理员在配置中预定义 | 每日代码审查、每周依赖检查 |
| 动态（Claude 调用 MCP 工具） | Claude 在对话中调用 `cron_create` | "20分钟后检查上传状态"、"每天8点总结邮箱" |

#### MCP 工具

Claude 在 session 中自动获得以下工具（通过 `--mcp-config` 注入）：

| 工具 | 功能 |
|------|------|
| `cron_create` | 创建定时任务。`cron_expr` 用于周期性，`run_at` 用于一次性延迟（如 `+20m`） |
| `cron_list` | 列出当前用户的所有 job |
| `cron_delete` | 删除 job（支持 ID 前缀匹配） |
| `cron_status` | 查询 job 详情 |

MCP server 通过环境变量 `CLAUDE_EXT_SESSION_ID` 和 `CLAUDE_EXT_STATE_DIR` 获取当前 session 上下文，自动继承 `user_id`、`context`（含 `chat_id`）和 `working_dir`。

#### Session 策略

| 策略 | 说明 | 典型场景 |
|------|------|----------|
| `new` | 每次触发创建独立 session，完成后自动清理 | 无上下文依赖的独立任务 |
| `reuse` | 沿用创建 job 时的 session，保留完整对话上下文 | "20分钟后检查数据是否上传完成" |

#### Reuse 策略的健壮性

当 reuse 目标 session 被用户 DELETE 时：
1. 调度器检测到 session 不存在
2. **Fallback**：创建新 session，设置 `claude_session_id` 为原 session 的 Claude CLI UUID + `prompt_count=1`
3. run.sh 自动使用 `--resume` 而非 `--session-id`，从 Claude Code 自身的存储中恢复对话上下文
4. 通知用户："原 session 已删除，在新 session 中恢复了 Claude 上下文"

关键原因：`destroy_session()` 只删除 `~/.claude-ext/sessions/{uuid}/` 和杀 tmux，不碰 Claude Code 的 `~/.claude/` 目录。`--resume` 从 Claude Code 自身存储中恢复。

当 reuse 目标 session 被 STOP 时：无需特殊处理，`send_prompt()` 会自动将 STOPPED 重置为 IDLE。

#### 槽位与自动回收

`new` 策略的 session 占用槽位。如果用户槽位已满：
1. 调度器尝试回收已完成的 cron session（标记了 `cron_auto_cleanup`）
2. 若无可回收槽位，job 延迟到下一个检查周期，并通知用户

#### 结果投递

Cron 任务的结果通过 `session.context` 路由：
- `context["chat_id"]` → Telegram callback 投递到用户
- `context["cron_job_id"]` → Cron callback 更新 job 状态

两个 callback 对同一个 delivery 事件独立触发，互不干扰。

#### Job 数据模型

```python
@dataclass
class CronJob:
    id: str                          # UUID
    name: str                        # 人类可读名称
    prompt: str                      # 触发时发送的 prompt
    working_dir: str                 # 工作目录
    user_id: str                     # 所属用户

    cron_expr: str | None            # "0 8 * * *" — 周期性
    run_at: str | None               # ISO timestamp — 一次性

    session_strategy: str            # "new" | "reuse"
    session_id: str | None           # reuse 目标（我们的 session ID）
    claude_session_id: str | None    # Claude CLI session UUID（用于 --resume fallback）

    notify_context: dict             # 通知路由（如 {"chat_id": 12345}）
    enabled: bool
    created_by: str                  # 来源 session_id 或 "config"
    last_run: str | None
    next_run: str | None
```

### 现有扩展：vault

加密凭证存储。让 Agent 安全地存取 API key、密码、私钥等敏感信息。

**架构三层**：

```
Claude session
  └─ MCP server (vault_store/list/retrieve/delete)
       └─ bridge RPC (Unix socket, 携带 session_id)
            └─ 主进程 bridge handler → VaultStore (加解密)
```

MCP server 进程**不持有 passphrase**，所有加解密通过 bridge RPC 在主进程完成。Passphrase 优先级：`CLAUDE_EXT_VAULT_PASSPHRASE` 环境变量 > `{vault_dir}/.passphrase` 文件 > 自动生成（`secrets.token_urlsafe(32)`, 0600 权限）。零配置即可启用。通过 `register_env_unset()` 确保环境变量不泄漏到 Claude session。

**安全边界**：加密是 defense-in-depth（防止密文文件被意外拷贝后直接可读），不是主安全边界。在 `bypassPermissions` 模式下 Claude 有完整文件系统访问权，真正的访问控制是 `_internal_prefixes`（控制 MCP 能读什么）和 OS 权限（控制谁能跑进程）。

**store.py — VaultStore**：

- 密钥派生：PBKDF2-HMAC-SHA256（600K iterations）+ 随机 salt → Fernet key
- 加密：Fernet（AES-128-CBC + HMAC 认证加密），JSON blob 整体加密
- 并发控制：统一 lockfile（`secrets.lock`）。读操作 `LOCK_SH`，写操作 `LOCK_EX` 覆盖完整的 read-modify-write 周期，防止并发写入丢失更新
- 原子写入：临时文件 + rename
- 文件权限：0700 目录 + 0600 文件

**MCP 工具**：

| 工具 | 功能 |
|------|------|
| `vault_store` | 存入秘密（key + value + 可选 tags） |
| `vault_list` | 列出所有 key 和 tags（不返回 value） |
| `vault_retrieve` | 读取秘密值（进入 LLM 上下文） |
| `vault_delete` | 删除秘密 |

**Key 命名校验**：bridge handler 强制 `category/service/name` 格式（正则 `^[a-zA-Z0-9_-]+(/[a-zA-Z0-9._-]+)+$`）。不符合格式的 key 在 `vault_store` 时被拒绝。示例：`api/github/token`、`email/smtp/password`、`wallet/eth/0xABC.../privkey`。

**访问控制预留**：`extension.py` 维护 `_internal_prefixes: list[str]`（当前为空列表）。`vault_retrieve` 检查 key 前缀，匹配的请求被拒绝并提示使用专用工具。Phase 4 Wallet 只需 `_internal_prefixes.append("wallet/")`，私钥即不可通过 MCP 读取。其他扩展通过 `engine.services["vault"].get()` 程序内直接访问，不受前缀限制。

**系统提示约束**：注入指令要求 Claude 永远不向用户回显秘密值，取到后直接用在后续工具调用中。

**审计**：每次 bridge 调用携带 `session_id`，handler 记录审计日志（`vault_store`、`vault_retrieve`、`vault_delete`）。

### 现有扩展：memory

跨 session 持久记忆系统。让 Agent 在多次对话间积累知识、记住用户偏好和项目上下文。

**设计决策：直接文件 I/O，不走 bridge RPC。** Memory 是明文 Markdown，无加密/访问控制需求，不存在"不能进入 LLM 上下文"的安全约束。MCP server 进程持有自己的 MemoryStore 实例直接读写磁盘，省去 socket round-trip。如需审计，在 MemoryStore 方法中加 `log.info` 即可。

**store.py — MemoryStore**：

- 存储格式：纯 Markdown 文件，人类可读，grep 友好
- 三层结构：`MEMORY.md`（热索引）/ `topics/<name>.md`（深度知识）/ `daily/YYYY-MM-DD.md`（日志）
- 路径安全：拒绝绝对路径、`..` 遍历、非 `.md` 文件、symlink 逃逸（`resolve()` + `is_relative_to`）
- 并发控制：统一 lockfile（`memory.lock`）。读操作 `LOCK_SH`，写操作 `LOCK_EX`
- 原子写入：`write()` 用 temp+rename；`append()` 在 `LOCK_EX` 下直接追加（避免大文件拷贝）
- 读取上限：512 KB 截断，防止大文件爆上下文

**MCP 工具**：

| 工具 | 功能 |
|------|------|
| `memory_read` | 读取记忆文件 |
| `memory_write` | 覆写/创建文件（自动创建父目录） |
| `memory_append` | 追加内容（自动 UTC 时间戳，适合 daily log） |
| `memory_search` | 全目录正则搜索（大小写不敏感，上限 50 条） |
| `memory_list` | 列出文件（按修改时间降序，可按子目录过滤） |

**系统提示驱动**：注入 SESSION START PROTOCOL（每次 session 开始读 `MEMORY.md`）+ CURATION 规则（超 150 行时精炼，移入 topic 文件）。Agent 自主维护记忆质量。

**Seed 文件**：首次启动自动创建 `MEMORY.md` 模板（含 User Preferences / Active Projects / Key Decisions / Topic Files 四个段落），不覆盖已有内容。

### 现有扩展：ask_user

交互式提问扩展。让 Claude 在 session 执行中途向用户提问并等待回答。

**数据流**：
```
Claude → MCP tool(ask_user) → bridge.call("ask_user") → BridgeServer handler
  → PendingStore.register + SessionManager.deliver(is_question=True)
  → [用户通过 Telegram/其他前端回答]
  → PendingStore.resolve → bridge 返回 → MCP tool 返回 → Claude 继续
```

**MCP 工具**：`ask_user(question, options?)`
- `question`：要问用户的问题（必填）
- `options`：可选选项列表，省略则用户自由文本输入

**系统提示注入**：扩展通过 `add_system_prompt()` 重定向 Claude 使用 MCP ask_user 工具而非内置 AskUserQuestion（内置工具在本环境中不可用）。

**前端对接**：delivery callback 收到 `{"is_question": True, "request_id": ..., "options": [...]}` 后展示 UI（如 Telegram inline keyboard）。用户回答后调用 `engine.pending.resolve(request_id, answer)` 交付响应。

---

## 配置文件 config.yaml

```yaml
engine:
  # model: claude-sonnet-4-6     # 不传则用 CLI 默认
  max_turns: 0                    # 0 = 无限
  permission_mode: bypassPermissions  # 必须，-p 模式下不设此项则工具无法执行
  allowed_tools: null             # null = 全部允许；或指定白名单列表

state_dir: ~/.claude-ext          # session 状态持久化目录

sessions:
  max_sessions_per_user: 5        # 每用户最大并发 session 数（= 槽位数）

enabled:                          # 启用的扩展名（对应 extensions/ 下的目录名）
  - vault                         # 加密凭证库（零配置，passphrase 自动生成）
  - memory                        # 跨 session 记忆（零配置）
  - telegram
  # - ask_user                    # 交互式提问
  # - cron                        # 定时任务调度器

extensions:                       # 每个扩展的独立配置
  vault:
    {}                            # passphrase 自动生成并存储在 {state_dir}/vault/.passphrase
  memory:
    {}                            # 文件存储在 {state_dir}/memory/
  telegram:
    token: "BOT_TOKEN"
    allowed_users: [123456789]    # Telegram user ID 白名单
    working_dir: null             # 默认工作目录，null = 当前目录；/new 支持相对路径和 ~ 展开
  cron:
    jobs:                         # 静态 job 列表（可选，Claude 也可动态创建）
      - name: daily-review
        cron_expr: "0 9 * * *"
        prompt: "Review yesterday's commits"
        working_dir: /path/to/project
        user_id: "123456789"
        notify_context: {chat_id: 123456789}
```

**注意：`config.yaml` 包含敏感信息（bot token），已在 `.gitignore` 中排除。请复制 `config.yaml.example` 并填入实际值。**

---

## 解耦设计原则

以下原则是本框架的硬性要求，新增功能时必须遵守：

1. **core 不 import 任何 extension。** 扩展发现完全通过 `importlib` 动态导入。如果你发现自己在 core/ 中写了 `from extensions.xxx import ...`，说明设计有问题。

2. **扩展之间互不依赖。** 扩展 A 不 import 扩展 B，不调用扩展 B 的方法，不读取扩展 B 的状态。如果两个扩展需要共享数据，应该通过 core 层的共享服务间接实现：`engine.session_manager`（会话管理）、`engine.services`（跨扩展服务注册表）、`engine.pending`（异步请求/响应）。

3. **每个扩展是完全自包含的目录。** 删除一个扩展目录 + 从 `enabled` 列表移除 = 零影响。

4. **新增功能 = 新增目录，不改已有代码。** 如果添加新扩展需要修改 `core/` 或其他扩展，说明抽象泄漏了。

5. **core 层的公共服务要通用化。** `core/session.py` 不绑定任何特定扩展。Session 使用 `user_id: str`（通用标识）和 `context: dict`（扩展自定义数据）替代特定平台字段。DeliveryCallback 只传 `(session_id, text, metadata)`，扩展自行从 session 对象获取路由信息。心跳以结构化事件形式发出，扩展自行格式化。

6. **配置即声明。** 扩展的行为由 `config.yaml` 控制，不硬编码。

---

## 运行

```bash
cd ~/claude-ext
source .venv/bin/activate    # Python 3.12+, 依赖装在 .venv 内
cp config.yaml.example config.yaml  # 首次运行前填入实际配置
python main.py
```

环境要求：
- Python 3.12+（用到了 `str | None` 类型语法）
- tmux 3.x+（用于多会话管理）
- Claude Code CLI 已安装且 `claude` 命令在 PATH 中
- 已通过 `claude auth login` 登录（订阅用户）
- pip 依赖：`python-telegram-bot>=21.0`, `pyyaml>=6.0`, `cryptography>=42.0`（vault 扩展）, `croniter>=1.0.0`（cron 扩展）

---

## 已知限制

- **script PTY 副作用**：`script -qfec` 在 `stream.jsonl` 开头可能写入一行 header（如 `Script started on ...`），解析时需跳过非 JSON 行。极少数情况下 PTY 可能注入 ANSI 转义序列。
- **Token 刷新**：`~/.claude/.credentials.json` 中的 `accessToken` 有过期时间，当前未处理自动刷新（Claude Code CLI 自身会处理刷新，但直接调用 usage API 时如果 token 过期会失败）。
- **全局 session 上限**：当前只有 per-user 的 `max_sessions_per_user` 限制，没有全局上限。多用户场景下需注意服务器资源。
- **MCP server 状态共享**：stdio 模式下每个 Claude session 启动独立 MCP server 进程。多进程通过 flock 共享同一个 `cron_jobs.json` 文件。高并发写入时可能有短暂锁竞争。
- **Cron session 恢复**：`--resume` 依赖 Claude Code 自身的 session 存储（`~/.claude/`）。极旧的 session 可能已被 Claude Code 清理，此时 fallback resume 会退化为新对话。
- **Backward compat**：旧 session 目录中可能存在 `output.json`（batch 模式遗留）。`_parse_stream_result` 优先读取 `stream.jsonl`，不存在时 fallback 到 `_parse_result`（读取 `output.json`）。
