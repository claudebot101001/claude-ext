# claude-ext Autonomous Agent Roadmap

从被动助手到自主 Agent 个体的演进路线。核心理念不变：**Claude Code 是运行时，我们只封装 CLI 调用和管理扩展生命周期**。所有新能力作为独立扩展实现，遵守解耦原则，零 core 修改。

## 已完成

### Phase 1: Vault — 加密凭证存储

`extensions/vault/` — Fernet 对称加密的 key-value 凭证库。

- **store.py**: PBKDF2-HMAC-SHA256 (600K iterations) 密钥派生 + Fernet 加解密 + flock 文件锁 + 原子写入 + 0600 权限
- **mcp_server.py**: `vault_store` / `vault_list` / `vault_retrieve` / `vault_delete` 四个 MCP 工具，通过 bridge RPC 调用主进程 VaultStore
- **extension.py**: 注册 `engine.services["vault"]` 供其他扩展程序内调用 + 注册 MCP server + bridge handler + 系统提示约束（不泄露密文）
- **安全设计**: passphrase 从环境变量 `CLAUDE_EXT_VAULT_PASSPHRASE` 读取，不进配置文件。MCP server 进程不持有 passphrase，所有加解密通过 bridge RPC 在主进程完成。每次 bridge 调用携带 `session_id`，handler 记录审计日志
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

#### 未来访问控制方向 (Phase 4+)

当前 vault 是全局可读的（受信任 Agent 场景）。Phase 4 引入 wallet 后，bridge handler 层面会添加前缀策略：

```python
# Phase 4 的 bridge handler 增强（方向，非最终设计）
INTERNAL_ONLY_PREFIXES = ["wallet/"]  # 这些前缀的 key 只能由 bridge handler 内部读取

if method == "vault_retrieve":
    key = params["key"]
    if any(key.startswith(p) for p in INTERNAL_ONLY_PREFIXES):
        return {"error": "This key is internal-only. Use the dedicated wallet tools."}
```

这个改动仅涉及 vault extension.py 的 `_bridge_handler` 方法（~5 行），不影响 store.py、MCP server 或其他扩展。session_id 已在 bridge 协议中透传，需要时可进一步按 session context 做细粒度控制。

---

## 待实现

### Phase 2: Memory — 跨 session 记忆系统

**目标**：让 Agent 拥有跨 session 的持久记忆，区别于 `CLAUDE.md`（项目指令）和 `--resume`（单 session 连续上下文）。

**架构方向**：

```
extensions/memory/
    store.py           # MemoryStore: 文件 I/O + 每日日志轮转 + 关键词搜索
    mcp_server.py      # memory_read / memory_write / memory_append / memory_search / memory_list
    extension.py       # 注册 MCP server + 系统提示注入
```

**关键设计**：
- **磁盘布局**: `~/.claude-ext/memory/` 下 `MEMORY.md`（核心记忆）+ `daily/YYYY-MM-DD.md`（每日日志）+ `topics/`（主题文件）
- 参考 OpenClaw 的 Markdown-on-disk 模式，Agent 自己维护和精炼记忆
- **Phase 2a**: grep 关键词搜索作为 MVP
- **Phase 2b (延后)**: 本地嵌入模型向量语义搜索
- 系统提示引导 Agent 在 session 开始时读取 MEMORY.md，任务完成后追加每日日志

**实现要点**：
- `memory_read(path)`: 读取指定记忆文件
- `memory_write(path, content)`: 覆写文件（用于 MEMORY.md 精炼）
- `memory_append(path, content)`: 追加内容（用于每日日志，自动加时间戳）
- `memory_search(query)`: 全目录 grep 搜索
- `memory_list()`: 列出所有记忆文件
- 路径限制在 memory 目录内，防止路径遍历

---

### Phase 3: Heartbeat — 心跳驱动的自主模式

**目标**：从"被动响应"到"主动行动"的关键转变。周期性唤醒 Agent，读取 HEARTBEAT.md 中的常驻指令，自主判断是否需要行动。

**架构方向**：

```
extensions/heartbeat/
    store.py           # HeartbeatState: 间隔/每日成本/per-user 状态持久化
    mcp_server.py      # heartbeat_status / heartbeat_update_orders / heartbeat_set_interval
    extension.py       # 心跳循环 + 专属 session 管理 + 成本追踪
```

**关键设计**：
- **HEARTBEAT.md**: Agent 的"常驻指令"文件。包含监控项、每日任务、抑制条件等
- **与 cron 的区别**: cron 是静态 prompt + 固定时间表；heartbeat 是动态读取指令 + Agent 自主决策是否行动
- **成本控制**: 最小间隔 5 分钟，默认 30 分钟。连续 HEARTBEAT_OK 时间隔翻倍（最大 4 小时）。每日 USD 预算上限
- 每个用户一个专属持久 session（不是每次创建新 session）
- 依赖 Phase 1 (Vault) 和 Phase 2 (Memory)

---

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
Phase 2: Memory ←── 自主行为的前提
    ↓
Phase 3: Heartbeat ←── 从被动到主动的跃迁
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
