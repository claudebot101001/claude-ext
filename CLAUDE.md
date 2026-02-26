# claude-ext 技术文档

基于 Claude Code CLI 构建的可扩展框架。核心理念：Claude Code 本身已经是一个完整的 AI coding agent，本框架只做两件事——**封装 CLI 调用** 和 **管理独立扩展的生命周期**。不重复造轮子。

## 目录结构

```
claude-ext/
├── core/                          # 核心层（稳定，极少修改）
│   ├── engine.py                  # Claude Code CLI 封装
│   ├── extension.py               # 扩展基类（接口契约）
│   ├── registry.py                # 扩展发现、加载、生命周期
│   └── status.py                  # 状态查询（auth + usage API）
├── extensions/                    # 扩展层（每个子目录完全独立）
│   └── telegram/
│       ├── extension.py           # Telegram Bot 桥接
│       └── requirements.txt       # 该扩展的独立依赖
├── config.yaml                    # 全局配置（引擎参数 + 扩展开关 + 扩展配置）
├── main.py                        # 入口（加载配置 → 创建引擎 → 注册扩展 → 运行）
├── requirements.txt               # 全局 Python 依赖
└── CLAUDE.md                      # 本文档
```

---

## 架构总览

```
┌─────────────┐     ┌──────────────────────────────────┐
│  config.yaml │────▶│            main.py                │
└─────────────┘     │  load config → build engine →     │
                    │  registry.discover/load/start_all │
                    └──────────┬───────────────────────┘
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
     │              ClaudeEngine                    │
     │  claude -p <prompt> --output-format json     │
     │  --permission-mode bypassPermissions         │
     └─────────────────────────────────────────────┘
                         │
                         ▼
                  Claude Code CLI
```

**数据流方向是单向的：扩展 → engine → CLI。扩展之间互不通信。**

---

## 核心层详解

### core/engine.py — ClaudeEngine

Claude Code CLI 的 async 封装。**全框架唯一与 `claude` 命令交互的地方。**

```python
engine = ClaudeEngine(
    model="claude-sonnet-4-6",       # 可选，不传则用 CLI 默认模型
    max_turns=0,                     # 0 = 无限制
    permission_mode="bypassPermissions",  # -p 模式必须设置，否则无法授权工具
    allowed_tools=["Bash", "Edit"],  # 可选白名单，null = 全部允许
)

response_text = await engine.ask(
    prompt="fix the bug in main.py",
    cwd="/path/to/project",          # 工作目录
    continue_session=True,           # 是否 --continue 续接上次会话
    timeout=300,                     # 超时秒数
)
```

**关键设计决策：**
- 使用 `--output-format json` 而非 `text`，这样每次调用后自动解析 JSON，将 `result` 文本返回给调用者，同时将 session 元数据存入 `engine.last_session`。
- `last_session` 字段包含：`session_id`, `total_cost_usd`, `duration_ms`, `duration_api_ms`, `num_turns`, `is_error`, `model`。供 `/status` 等功能读取。
- 关于 `--permission-mode bypassPermissions`：`claude -p` 是非交互模式，没有人能点击"允许"按钮，因此 **必须** 设置此参数才能让 Claude Code 正常执行工具调用（读写文件、运行命令等）。这意味着安全边界完全由 `allowed_users` 白名单保证。

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

每个扩展只需实现 `start()` 和 `stop()`。通过 `self.engine` 调用 Claude Code，通过 `self.config` 读取自身配置。

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
| `format_status(auth, usage, session)` | 上述两者 + `engine.last_session` | 格式化的文本字符串 |

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
        # 从 config 中读取该扩展自己的配置
        self.token = config["token"]

    async def start(self) -> None:
        # 启动逻辑（连接、轮询等）
        ...

    async def stop(self) -> None:
        # 停止逻辑（断开连接、清理资源）
        ...
```

**第三步：在 config.yaml 中注册**

```yaml
enabled:
  - telegram
  - discord        # 加到启用列表

extensions:
  telegram:
    ...
  discord:          # 添加该扩展的配置段
    token: "YOUR_DISCORD_BOT_TOKEN"
```

**第四步：安装依赖（如有）**

```bash
pip install -r extensions/discord/requirements.txt
```

**完成。无需修改 core/ 下任何文件，无需修改 main.py，无需修改其他扩展。**

### 现有扩展：telegram

Telegram Bot 桥接，将聊天消息转发到 `claude -p`。

**功能：**
- 普通消息 → `engine.ask(prompt, continue_session=True)` → 返回结果
- `/start` → 欢迎信息
- `/new` → 重置会话（下次消息不再 `--continue`）
- `/status` → 调用 `core/status.py` 展示 Auth + Usage Quota + Session 信息
- `set_my_commands()` → 注册 Telegram 命令菜单（输入 `/` 时自动补全）
- `allowed_users` → 白名单鉴权（Telegram user ID）
- 自动分片 → 超过 4000 字符的响应自动拆分发送

**会话续接机制：**
- 首条消息：`continue_session=False`，Claude Code 创建新 session
- 后续消息：`continue_session=True`，CLI 自动加 `--continue` 续接上一个 session
- `/new` 命令将 `ctx.user_data["continue"]` 重置为 `False`

---

## 配置文件 config.yaml

```yaml
engine:
  # model: claude-sonnet-4-6     # 不传则用 CLI 默认
  max_turns: 0                    # 0 = 无限
  permission_mode: bypassPermissions  # 必须，-p 模式下不设此项则工具无法执行
  allowed_tools: null             # null = 全部允许；或指定白名单列表

enabled:                          # 启用的扩展名（对应 extensions/ 下的目录名）
  - telegram

extensions:                       # 每个扩展的独立配置
  telegram:
    token: "BOT_TOKEN"
    allowed_users: [123456789]    # Telegram user ID 白名单
    working_dir: null             # Claude Code 工作目录，null = 当前目录
```

**配置传递路径：** `config.yaml` → `main.py` 解析 → `engine` 段传给 `ClaudeEngine` 构造器 → `extensions.<name>` 段传给对应扩展的 `configure(engine, config)` 方法。

---

## 解耦设计原则

以下原则是本框架的硬性要求，新增功能时必须遵守：

1. **core 不 import 任何 extension。** 扩展发现完全通过 `importlib` 动态导入。如果你发现自己在 core/ 中写了 `from extensions.xxx import ...`，说明设计有问题。

2. **扩展之间互不依赖。** 扩展 A 不 import 扩展 B，不调用扩展 B 的方法，不读取扩展 B 的状态。如果两个扩展需要共享数据，应该通过 core 层的共享服务（如 `engine.last_session`）间接实现。

3. **每个扩展是完全自包含的目录。** 删除一个扩展目录 + 从 `enabled` 列表移除 = 零影响。不需要改任何其他代码。

4. **新增功能 = 新增目录，不改已有代码。** 这是检验解耦是否成功的标准。如果添加新扩展需要修改 `core/` 或其他扩展，说明抽象泄漏了。

5. **core 层的公共服务要通用化。** 如 `core/status.py` 不绑定 Telegram，任何扩展都可以调用它。如果新功能对所有扩展都有价值（如日志、通知），放在 core/ 里；如果只对某个扩展有价值，放在该扩展目录内。

6. **配置即声明。** 扩展的行为由 `config.yaml` 控制，不硬编码。新扩展的可调参数都应该放在 `extensions.<name>` 配置段中。

---

## 运行

```bash
cd ~/tmp/claude-ext
source .venv/bin/activate    # Python 3.12+, 依赖装在 .venv 内
python main.py
```

环境要求：
- Python 3.12+（用到了 `str | None` 类型语法）
- Claude Code CLI 已安装且 `claude` 命令在 PATH 中
- 已通过 `claude auth login` 登录（订阅用户）
- pip 依赖：`python-telegram-bot>=21.0`, `pyyaml>=6.0`

---

## 已知限制

- **Context window 百分比**：`claude -p --output-format json` 的响应中不包含 `context_window.used_percentage`，此字段仅在交互模式的 statusline 数据中可用。如需此信息，需改用 `--output-format stream-json` 解析流式事件。
- **并发**：当前 `engine.last_session` 是单实例共享的，如果未来有多用户并发场景，需要改为按用户存储。
- **Token 刷新**：`~/.claude/.credentials.json` 中的 `accessToken` 有过期时间，当前未处理自动刷新（Claude Code CLI 自身会处理刷新，但直接调用 usage API 时如果 token 过期会失败）。

---

## 可能的后续扩展方向（仅供参考）

| 扩展名 | 用途 | 实现思路 |
|--------|------|----------|
| `cron` | 定时任务 | `asyncio` 定时器 + `engine.ask()` |
| `discord` | Discord Bot 桥接 | 同 telegram 模式，换 `discord.py` 库 |
| `webhook` | HTTP API 接口 | `aiohttp` 起一个轻量 web server |
| `notify` | 任务完成通知 | hook 到 `engine.ask()` 返回后，推送到指定渠道 |
| `filewatch` | 文件变更触发 | `watchdog` 监听文件系统事件 → 触发 `engine.ask()` |
