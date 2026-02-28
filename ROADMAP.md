# claude-ext Autonomous Agent Roadmap

从被动助手到自主 Agent 个体的演进路线。核心理念不变：**Claude Code 是运行时，我们只封装 CLI 调用和管理扩展生命周期**。所有新能力作为独立扩展实现，遵守解耦原则，零 core 修改。

## 已完成

### Phase 1: Vault — 加密凭证存储

`extensions/vault/` — Fernet 对称加密的 key-value 凭证库。

- **store.py**: PBKDF2-HMAC-SHA256 (600K iterations) 密钥派生 + Fernet 加解密 + 统一 lockfile（`secrets.lock`，LOCK_SH/LOCK_EX 读写互斥） + 原子写入 + 0700 目录权限 + 0600 文件权限
- **mcp_server.py**: `vault_store` / `vault_list` / `vault_retrieve` / `vault_delete` 四个 MCP 工具，通过 bridge RPC 调用主进程 VaultStore
- **extension.py**: 注册 `engine.services["vault"]` 供其他扩展程序内调用 + 注册 MCP server + bridge handler + key 命名校验（强制 `category/service/name` 格式）+ `_internal_prefixes` 访问控制机制 + 系统提示约束（不泄露密文）
- **安全设计**: passphrase 优先级：`CLAUDE_EXT_VAULT_PASSPHRASE` 环境变量 > `{vault_dir}/.passphrase` 文件 > 自动生成（`secrets.token_urlsafe(32)`, 0600 权限）。MCP server 进程不持有 passphrase，所有加解密通过 bridge RPC 在主进程完成。通过 `register_env_unset()` 确保 passphrase 不泄漏到 Claude session 环境。每次 bridge 调用携带 `session_id`，handler 记录审计日志
- **安全边界的诚实定位**: 加密是 defense-in-depth（防止密文文件被意外拷贝后直接可读），不是主安全边界。在 `bypassPermissions` 模式下 Claude 有完整文件系统访问权，真正的访问控制是 `_internal_prefixes`（控制 MCP 能读什么）和 OS 权限（控制谁能跑进程）
- **性能基线** (实测): raw socket echo 0.06ms, vault_retrieve 0.24ms, vault_store 0.57ms。瓶颈在 Fernet crypto + 磁盘 I/O，socket 开销可忽略

#### Vault Key 命名规范

所有 vault key 必须使用 `category/service/name` 的命名空间格式：

```
wallet/eth/privkey          # 钱包私钥
wallet/eth/0xABC.../privkey # 多钱包时按地址区分
email/smtp/password         # SMTP 密码
email/imap/password         # IMAP 密码
api/github/token            # GitHub API token
api/openai/key              # OpenAI API key
```

**为什么现在就定**：Phase 4 (Wallet) 需要添加 `internal_only` 前缀策略（如 `wallet/*` 前缀的 key 只能由 wallet bridge handler 内部读取，不返回给 LLM）。如果现在不按命名空间存储，到时候就需要数据迁移。按前缀匹配的访问控制不需要额外的 tag 字段，key 本身就携带了分类信息。

#### 访问控制机制（已就位）

`extension.py` 维护 `_internal_prefixes: list[str]`（当前为空列表）。`vault_retrieve` 的 bridge handler 检查 key 前缀，匹配时拒绝请求：

```python
# 当前代码（extension.py）
self._internal_prefixes: list[str] = []  # Phase 4+: ["wallet/"]

def _is_internal_key(self, key: str) -> bool:
    return any(key.startswith(p) for p in self._internal_prefixes)

# vault_retrieve 分支中：
if self._is_internal_key(key):
    return {"error": f"Key '{key}' is internal-only. Use the dedicated extension tools."}
```

Phase 4 Wallet 上线时只需 `self._internal_prefixes.append("wallet/")`，私钥即不可通过 MCP `vault_retrieve` 读取。其他扩展通过 `engine.services["vault"].get()` 程序内直接访问，不受前缀限制（已有测试覆盖 `TestInternalPrefixes`）。session_id 已在 bridge 协议中透传，需要时可进一步按 session context 做细粒度控制。

### Phase 2: Memory — 跨 session 记忆系统

`extensions/memory/` — Markdown-on-disk 持久记忆，Agent 自主维护。

- **store.py**: MemoryStore — 路径安全（拒绝绝对路径、`..` 遍历、非 `.md` 文件、symlink 逃逸）+ 统一 lockfile（`memory.lock`，LOCK_SH/LOCK_EX）+ 原子写入（write 用 temp+rename，append 在 LOCK_EX 下直接追加）+ 512 KB 读取上限 + 搜索结果上限 50 条
- **mcp_server.py**: `memory_read` / `memory_write` / `memory_append` / `memory_search` / `memory_list` 五个 MCP 工具，MCP 进程惰性初始化 MemoryStore 直接读写
- **extension.py**: 注册 `engine.services["memory"]` + 注册 MCP server（注入 `MEMORY_DIR` 环境变量）+ 系统提示注入（SESSION START PROTOCOL + CURATION 规则）+ 首次启动 seed `MEMORY.md` 模板
- **设计决策**: 直接文件 I/O，不走 bridge RPC。Memory 是明文 Markdown，无加密/访问控制需求。MCP server 进程持有自己的 MemoryStore 实例，省去 socket round-trip。审计需求可通过 MemoryStore 方法内 `log.info` 满足
- **与 Claude Code auto-memory 的区分**: CC 内置 auto-memory 存储在 `~/.claude/projects/<project>/memory/`（按项目隔离）。本扩展存储在 `~/.claude-ext/memory/`（全局共享）。系统提示显式声明两者独立，要求 Agent 仅通过 MCP 工具操作本扩展的记忆，不混用内置 Read/Write 工具
- **三层存储**: `MEMORY.md`（热索引，< 200 行）/ `topics/<name>.md`（深度知识）/ `daily/YYYY-MM-DD.md`（append-only 日志）
- **Phase 2b (延后)**: 本地嵌入模型向量语义搜索

### Pre-Heartbeat Architecture Improvements

Phase 3 前的架构加固。三项改进均为轻量级 core 增强。

#### P1: MCP 工具注册表内省

`register_mcp_server(name, config, tools=None)` 新增可选 `tools` 参数，声明 MCP server 提供的工具元数据。`list_mcp_tools()` 返回所有已注册 server 及其工具。四个 MCP 扩展（vault/memory/cron/ask_user）均已传入工具元数据。用于 `/status` 展示和调试定位。

#### P2: 结构化事件日志

新增 `core/events.py` — `EventLog` 类。JSONL 追加文件 `{state_dir}/events.jsonl`，每行 `{"ts", "type", "session_id", "detail"}`。Best-effort（不抛异常）、带锁（LOCK_SH/LOCK_EX）、10 MB 单代旋转。

事件点：SessionManager 6 处（created/destroyed/stopped/prompt/completed/dead）+ Registry 3 处（started/stopped/load_failed）+ Vault 3 处（store/retrieve/delete）+ Cron 1 处（triggered）。

#### P3: 健康检查注册表

`Extension` 基类新增 `health_check() -> dict`（非抽象，默认 `{"status": "ok"}`）。`Registry.health_check_all()` 聚合所有扩展健康状态（5 秒超时）。五个扩展各自覆写返回特有状态（secret 数、文件数、job 数、策略列表等）。Telegram `/status` 命令集成展示。

策略可见性：各扩展通过 health_check 返回 `policies` 字段自报当前策略（如 vault 的 `_internal_prefixes`、telegram 的 `allowed_users` 白名单人数），集中**可见性**而非集中**执行**。

### Phase 3: Heartbeat — 自主心跳

`extensions/heartbeat/` — 双通道调度 + 三层执行的自主周期 Agent。

- **store.py**: HeartbeatState（8 字段）+ HeartbeatStore — JSON + flock 状态持久化 + HEARTBEAT.md 指令文件 I/O + 原子写入 + 损坏文件回退
- **mcp_server.py**: `heartbeat_get_instructions` / `heartbeat_set_instructions` / `heartbeat_get_status` / `heartbeat_pause` / `heartbeat_resume` / `heartbeat_trigger` / `heartbeat_get_trigger_command` 七个 MCP 工具。前五个直接文件 I/O（同 memory 模式），`heartbeat_trigger` 通过 bridge RPC 调用主进程 `trigger()` 方法，`heartbeat_get_trigger_command` 返回外部脚本可用的 shell 触发命令
- **trigger_cli.py**: 纯 stdlib 独立 CLI，外部进程通过 bridge.sock 触发心跳。Agent 通过 `heartbeat_get_trigger_command` 获取完整命令（`shlex.quote()` 安全引用），嵌入后台任务或监控脚本
- **extension.py**: 双通道调度器（Timer + asyncio.Queue Trigger）+ 三层执行（Tier 0 门控 → Tier 1 预检 → Tier 2 LLM 决策 → Tier 3 完整 session）+ 利用率感知成本控制 + 自适应退避 + delivery callback 自动清理 + 恢复检查 + bridge handler（`heartbeat_trigger` RPC）
- **与 cron 的区别**: cron 是静态 prompt + 固定时间表；heartbeat 是动态读取指令 + Agent 自主决策是否行动 + 连续无事时自动退避
- **成本控制五道安全阀**: 日运行上限（默认 48）、利用率节流（80%: 仅 immediate 触发通过）、利用率暂停（95%: 全部暂停）、活跃时段窗口、自适应退避（1x→2x→4x→8x）
- **静默抑制**: Tier 2 使用 `engine.ask()` 轻量子进程，"NOTHING" 完全静默无通知。仅 Tier 3 创建的 session 有 `chat_id`，前端才投递
- **事件触发**: 三条路径——① 其他扩展通过 `engine.services["heartbeat"].trigger(source, event_type, urgency, payload)` 提交事件（Python API）；② Agent 在 session 中通过 MCP `heartbeat_trigger` 工具提交事件（bridge RPC → 主进程 `trigger()`）；③ 外部进程通过 `trigger_cli.py` 连接 bridge.sock 触发（session 外事件唤醒）。`immediate` 立即唤醒调度器；`normal` 积累到下次定时器到期。sync 方法，单事件循环内安全
- **`notify_context` 路由**: 配置中的 `notify_context` 原样透传到 `session.context`，heartbeat 不解读内容。前端扩展各自从 context 取所需字段（如 Telegram 取 `chat_id`、Discord 取 `channel_id`）。零前端耦合

---

## 待实现

### Phase 4: Wallet — Crypto 钱包管理

**目标**：让 Agent 能持有和管理链上资产。

**架构方向**：

```
extensions/wallet/
    chains/
        base.py        # ChainAdapter ABC（链抽象）
        evm.py         # EVM 实现 (web3.py)
    store.py           # 钱包元数据 + 消费限额追踪
    mcp_server.py      # wallet_generate / wallet_list / wallet_balance / wallet_send / wallet_contract_call / wallet_sign_message
    extension.py       # MCP + bridge 注册，vault 依赖检查
```

**关键设计**：
- **私钥隔离 (Cardinal Rule)**: 私钥存 Vault，MCP server 进程通过 bridge 调用 VaultStore 解密 → 内存中签名 → 清除。**私钥永远不进入 LLM 上下文**
- **消费限额**: 每钱包可配置每日 USD 上限，超限触发 ask_user 确认
- **Phase 4a**: 仅 EVM（Ethereum + Arbitrum + Optimism + Base），ChainAdapter ABC 预留其他链
- 硬依赖 Phase 1 (Vault)

---

### Phase 5a: Email — 独立邮箱

**目标**：Agent 拥有独立邮箱，能发送、接收、搜索邮件。

**架构方向**：

```
extensions/email/
    imap_listener.py   # IMAP IDLE 后台任务（新邮件 → delivery callback 通知）
    mcp_server.py      # email_send / email_check / email_read / email_search / email_reply
    extension.py       # IMAP IDLE 监听 + SMTP 发送 + delivery callback
```

**关键设计**：
- SMTP/IMAP 凭证存 Vault
- IMAP IDLE 实时监听新邮件，通过 delivery callback 通知活跃 session
- 发信限速（默认 10/小时），防止 Agent 失控
- 指数退避重连 + 轮询 fallback

---

### Phase 5b: Browser — Playwright 浏览器

**目标**：Agent 能浏览网页、截图、填表、执行 JS。

**架构方向**：

```
extensions/browser/
    mcp_server.py      # browser_navigate / browser_screenshot / browser_click / browser_type / browser_evaluate
    extension.py       # Playwright 生命周期 + bridge handler
```

**关键设计**：
- 单个 Chromium 实例，per-session browser context（隔离 cookies）
- MCP tool → bridge RPC → 主进程 Playwright 操作 → 返回结果（避免每个 MCP 进程启动独立浏览器）
- 截图保存到文件 → Claude 用 Read 工具查看（CC 原生支持图片）
- 最大同时打开页面数可配置，内存阈值重启

---

## 依赖关系

```
Phase 1: Vault  ←── 所有后续阶段的凭证基础 ✅ 已完成
    ↓
Phase 2: Memory ←── 自主行为的前提 ✅ 已完成
    ↓
Phase 3: Heartbeat ←── 从被动到主动的跃迁 ✅ 已完成
    ↓ (可与 4, 5 并行)
Phase 4: Wallet ←── 依赖 Vault（硬依赖）
Phase 5a: Email ←── 依赖 Vault（SMTP 凭证）
Phase 5b: Browser ←── 无硬依赖
```

## enabled 顺序建议

```yaml
enabled:
  - vault              # 必须最先（其他扩展依赖）
  - memory             # 早期加载（心跳受益）
  - ask_user           # 交互确认
  - heartbeat          # 自主行为
  - cron               # 定时任务
  - wallet             # 钱包（依赖 vault）
  - email              # 邮箱（依赖 vault）
  - browser            # 浏览器
  - telegram           # 前端（最后注册 delivery callback → flush pending）
```
