# claude-ext 技术文档

基于 Claude Code CLI 构建的可扩展框架。核心理念：Claude Code 本身已经是一个完整的 AI coding agent，本框架只做两件事——**封装 CLI 调用** 和 **管理独立扩展的生命周期**。不重复造轮子。

## 目录结构

```
claude-ext/
├── core/                          # 核心层（稳定，极少修改）
│   ├── engine.py                  # Claude Code CLI 封装 + SessionManager 入口
│   ├── extension.py               # 扩展基类（接口契约）
│   ├── registry.py                # 扩展发现、加载、生命周期
│   ├── session.py                 # tmux-backed 多会话管理（核心模块）
│   └── status.py                  # 状态查询（auth + usage API）
├── extensions/                    # 扩展层（每个子目录完全独立）
│   └── telegram/
│       ├── extension.py           # Telegram Bot 桥接（多会话）
│       └── requirements.txt       # 该扩展的独立依赖
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
     │  (telegram)   │ │  (未来扩展)   │ │  (未来扩展)   │
     └──────┬───────┘ └──────┬───────┘ └──────┬───────┘
            │                │                 │
            ▼                ▼                 ▼
     ┌─────────────────────────────────────────────┐
     │       ClaudeEngine + SessionManager          │
     │                                              │
     │  SessionManager（多会话，推荐）:              │
     │    tmux session → run.sh → claude -p         │
     │    文件 IPC: prompt.txt → output.json        │
     │    状态持久化: state.json                     │
     │    与主进程解耦，支持崩溃恢复                  │
     │                                              │
     │  engine.ask()（轻量单次调用，向后兼容）:       │
     │    直接 subprocess → claude -p                │
     └─────────────────────────────────────────────┘
                         │
                         ▼
                   tmux sessions
                   └── claude -p <prompt> --output-format json
```

**数据流方向是单向的：扩展 → engine/session_manager → tmux → CLI。扩展之间互不通信。**

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
    user_id: int               # 所属用户（Telegram user ID）
    chat_id: int               # 投递结果的 Telegram chat ID
    working_dir: str           # Claude Code 工作目录
    status: SessionStatus      # idle / busy / dead / stopped
    claude_session_id: str     # Claude CLI session UUID（用于 --resume）
    tmux_session: str          # tmux session 名称 "cc-{uuid}"
    prompt_count: int          # 已发送 prompt 数
```

#### 文件式 IPC

每个 session 在 `~/.claude-ext/sessions/{uuid}/` 下有：

| 文件 | 用途 |
|------|------|
| `state.json` | 持久化的 session 元数据（原子写入） |
| `prompt.txt` | 当前 prompt 内容 |
| `run.sh` | 自动生成的执行脚本 |
| `output.json` | claude 的 JSON 输出（先写 .tmp 再 mv，原子写入） |
| `stderr.log` | 错误输出 |
| `exitcode` | 完成标记（文件存在 = 命令结束），内容为 claude 退出码 |

#### run.sh 模板

```bash
#!/bin/bash
unset CLAUDECODE                  # 防止嵌套 session 检测
PROMPT=$(cat "/path/prompt.txt")  # 从文件读取，避免 shell 转义问题
cd "/working/dir"
claude -p "$PROMPT" --output-format json \
  --session-id "uuid"             # 首次用 --session-id
  # 或 --resume "uuid"            # 后续用 --resume
  --permission-mode bypassPermissions \
  > "/path/output.json.tmp" 2>"/path/stderr.log"
CLAUDE_EXIT=$?
mv "/path/output.json.tmp" "/path/output.json" 2>/dev/null || true
echo $CLAUDE_EXIT > "/path/exitcode"
```

**关于 prompt 安全性**：`PROMPT=$(cat file)` 将文件内容赋值给变量时不会发生 shell 解释。`"$PROMPT"` 作为双引号包裹的变量传给 `claude -p` 时，内容中的 `$`、反引号等特殊字符不会被递归解释。这是 bash 变量展开的标准行为。

#### 核心方法

| 方法 | 职责 |
|------|------|
| `create_session()` | 分配槽位 + 创建 tmux session + 状态目录 + 启动 queue worker |
| `send_prompt()` | 将 prompt 放入 per-session 队列，返回队列位置 |
| `stop_session()` | 清空队列 → 标记 STOPPED → Ctrl-C → 后台 5s 写 exitcode（非阻塞） |
| `destroy_session()` | 杀 tmux + 取消 worker + 删除状态目录 |
| `recover()` | 启动时扫描磁盘状态 + 检查 tmux 存活，恢复/重连（结果缓冲到 pending） |
| `shutdown()` | 取消所有 worker，**不杀 tmux session**（核心设计点） |
| `set_delivery_callback()` | 注册结果投递回调 + 刷新 recover 期间缓冲的待投递结果 |

#### 槽位机制

每个用户最多 N 个并发 session（默认 5，可配置）。每个 session 创建时分配最小可用槽位号。槽位号在 session 生命周期内固定不变，删除后释放供新 session 复用。用户通过槽位号而非列表序号引用 session，避免删除后编号混乱。

#### 队列机制

每个 session 有独立的 asyncio.Queue 和 worker task。多条消息发给同一个 session 时自动排队，依次执行。`/stop` 会同时清空队列。

#### 状态机

```
创建 → IDLE → (send_prompt) → BUSY → (完成) → IDLE
                                   → (stop) → STOPPED → (send_prompt) → IDLE
                                   → (tmux 死亡) → DEAD
```

**状态保护**：`_execute_prompt` 在 poll 返回后检查 `session.status`，如果已被 `stop_session` 设为 STOPPED 则不覆盖。

#### 启动恢复矩阵

| state.json status | tmux 存活? | exitcode 存在? | 动作 |
|---|---|---|---|
| busy | Yes | Yes | 读取结果，标记 idle，缓冲到 pending_deliveries |
| busy | Yes | No | 仍在运行，恢复 poll 监控 |
| busy | No | — | 标记 dead |
| idle/stopped | Yes | — | 直接重连 |
| idle/stopped | No | — | 重建 tmux session |

**关键时序**：`recover()` 在 `start_all()` 之前执行（此时 delivery callback 尚未设置），因此完成的结果缓冲到 `_pending_deliveries`。当扩展调用 `set_delivery_callback()` 时自动刷新缓冲区。

#### 心跳

长时间运行的任务（>60s）会每分钟通过 delivery callback 发送 "Still working..." 通知，避免用户端完全沉默。

### core/engine.py — ClaudeEngine

提供两种调用模式：

1. **SessionManager（推荐）**：通过 `engine.session_manager` 访问，tmux-backed，支持多会话、崩溃恢复、异步投递。Telegram 扩展使用此模式。
2. **`engine.ask()`（轻量）**：直接 subprocess 调用 `claude -p`，同步等待结果。适合不需要持久化/多会话的简单扩展（如 cron、webhook）。

```python
# SessionManager 模式（Telegram 等需要多会话的扩展）
session = await engine.session_manager.create_session(name="task1", ...)
await engine.session_manager.send_prompt(session.id, "fix the bug")

# 轻量模式（简单一次性调用）
response = await engine.ask(prompt="what is 1+1", cwd="/tmp")
```

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
        self.engine.session_manager.set_delivery_callback(self._deliver)
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
| `/new [name] [dir]` | 创建新 session（可选名称和工作目录） |
| `/sessions` | 列出所有 session，`*` 标记当前活跃 |
| `/switch <slot\|name>` | 切换活跃 session（按槽位号或名称） |
| `/status` | Auth + Usage + 当前 session 信息 |
| `/stop` | 停止当前 session 的运行任务 + 清空队列 |
| `/delete <slot\|name>` | 删除 session（杀 tmux + 清文件） |

**消息处理流程：**
1. 用户发消息 → 检查是否有活跃 session，没有则自动创建 "default"（槽位 #1）
2. 活跃 session busy → 消息入队列，回复 "Queued (position N)"
3. 活跃 session idle → 提交 prompt，回复 "Processing..."
4. 后台 worker 执行完成 → 通过 delivery callback 异步投递结果到 chat

**结果投递**：异步模式。用户发消息后立即收到确认，结果完成后推送。每条结果前加 `[#slot name]` 前缀标识来源。

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
  - telegram

extensions:                       # 每个扩展的独立配置
  telegram:
    token: "BOT_TOKEN"
    allowed_users: [123456789]    # Telegram user ID 白名单
    working_dir: null             # 默认工作目录，null = 当前目录；可通过 /new 覆盖
```

**注意：`config.yaml` 包含敏感信息（bot token），已在 `.gitignore` 中排除。请复制 `config.yaml.example` 并填入实际值。**

---

## 解耦设计原则

以下原则是本框架的硬性要求，新增功能时必须遵守：

1. **core 不 import 任何 extension。** 扩展发现完全通过 `importlib` 动态导入。如果你发现自己在 core/ 中写了 `from extensions.xxx import ...`，说明设计有问题。

2. **扩展之间互不依赖。** 扩展 A 不 import 扩展 B，不调用扩展 B 的方法，不读取扩展 B 的状态。如果两个扩展需要共享数据，应该通过 core 层的共享服务（如 `engine.session_manager`）间接实现。

3. **每个扩展是完全自包含的目录。** 删除一个扩展目录 + 从 `enabled` 列表移除 = 零影响。

4. **新增功能 = 新增目录，不改已有代码。** 如果添加新扩展需要修改 `core/` 或其他扩展，说明抽象泄漏了。

5. **core 层的公共服务要通用化。** `core/session.py` 不绑定 Telegram，通过 delivery callback 解耦。任何扩展都可以注册自己的回调。

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
- pip 依赖：`python-telegram-bot>=21.0`, `pyyaml>=6.0`

---

## 已知限制

- **Context window 百分比**：`claude -p --output-format json` 的响应中不包含 `context_window.used_percentage`，此字段仅在交互模式的 statusline 数据中可用。如需此信息，需改用 `--output-format stream-json` 解析流式事件。
- **Token 刷新**：`~/.claude/.credentials.json` 中的 `accessToken` 有过期时间，当前未处理自动刷新（Claude Code CLI 自身会处理刷新，但直接调用 usage API 时如果 token 过期会失败）。
- **Delivery callback 单一**：当前 `SessionManager.set_delivery_callback()` 只支持一个回调。如果多个扩展都需要投递结果，需改为回调列表或 pub/sub 模式。
- **全局 session 上限**：当前只有 per-user 的 `max_sessions_per_user` 限制，没有全局上限。多用户场景下需注意服务器资源。
