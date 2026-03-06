[English](README.md) | 中文

# claude-ext

[![CI](https://github.com/claudebot101001/claude-ext/actions/workflows/ci.yml/badge.svg)](https://github.com/claudebot101001/claude-ext/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)

将 [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) 转变为可编程、常驻运行、多会话 Agent 平台的服务端框架。仅做两件事：包装官方 `claude` 二进制调用，管理独立扩展的生命周期。

> **27k 行代码** | **11 个扩展** | **782 项测试** | **纯 Python，零框架依赖**

## 为什么选择 claude-ext

### 基于订阅的 Agent 平台

claude-ext 底层调用 `claude -p`，以第一方 Claude Code 会话运行，使用现有 Claude 订阅（Free、Pro 或 Max 计划 OAuth）认证。其他方案——[Agent SDK](https://platform.claude.com/docs/en/agent-sdk/overview)、自定义 API 集成或第三方封装——必须使用 API key 认证，按 token 付费。

对于高强度 Agent 工作负载（自主心跳、多 Agent 协作、定时任务），这意味着显著的成本差异。Max 计划按月固定收费；同等 API 用量可轻松超过每月 $3,000。

### 集成式基础设施，不只是封装

大多数 Claude Code 扩展项目只聚焦单一关注点——会话管理、Telegram 桥接或定时调度。claude-ext 提供**完整的服务端基础设施层**：

| 能力 | 作用 |
|---|---|
| **多会话管理** | tmux 会话 + 每用户槽位 + 提示队列 + 崩溃恢复 |
| **MCP 服务器注入** | 每会话独立 MCP 服务器 + 动态工具注册 + 会话定制器 |
| **Bridge RPC** | MCP 子进程与主进程之间的双向 Unix socket IPC |
| **网关模式** | 多工具 MCP 服务器合并为 1 个网关工具（token 减少 98%） |
| **扩展生命周期** | 动态发现、热重载（SIGHUP）、健康检查、严格解耦 |
| **交付回调** | 实时流式传输 + 结构化元数据（费用、状态、工具调用） |
| **Pending Store** | 通用异步请求/响应注册表，用于跨进程协调 |

这些核心原语驱动 11 个独立扩展，可通过配置文件自由启用/禁用。

### 合规性

claude-ext 不提取、存储或代理 OAuth token。它将官方 `claude` CLI 二进制文件作为子进程启动——与你在终端中运行的完全相同。token 处理完全在 Anthropic 自己的代码内部。

> **免责声明**：Anthropic 的条款和政策可能随时变更。请查阅[最新条款](https://code.claude.com/docs/en/legal-and-compliance)确认当前政策。

## 架构

```
用户 / 前端（Telegram、CLI、...）
        │
        ▼
    扩展层 ──── config.yaml
        │
        ▼
   ClaudeEngine ─── Bridge RPC（Unix socket）
   SessionManager   PendingStore
   EventLog         Service Registry
        │
        ▼
   tmux 会话 ──── MCP 服务器（每会话独立，网关模式）
        │
        ▼
   claude -p ──── 文件 IPC（prompt → stream.jsonl → result）
```

**核心特性：**

- **崩溃恢复** — tmux 会话在主进程重启后存活，启动时自动恢复
- **多用户** — 每用户独立会话槽位和提示队列
- **动态扩展** — 通过 `config.yaml` 增删；无需修改核心层或其他扩展
- **会话定制器** — 通过 `SessionOverrides` 实现每次提示的 MCP/系统提示/工具动态修改
- **SIGHUP 热重载** — 无需重启即可更新配置；扩展收到 `reconfigure()` 回调
- **用量感知** — 查询认证状态和 API 用量配额，用于成本控制
- **结构化事件** — JSONL 事件日志 + 轮转，用于可观测性

## 扩展

| 扩展 | MCP 工具数 | 描述 |
|------|:---------:|------|
| **vault** | 4 | 加密凭证库（Fernet + PBKDF2）。密码短语永不离开主进程 |
| **memory** | 8 | 三层身份体系（宪法 + 加密人格 + 每用户档案）+ FTS5 全文搜索知识库 |
| **heartbeat** | 7 | 自主定期 Agent。三层执行（门控 → LLM 决策 → 会话）+ 五重安全阀 |
| **cron** | 3 | 定时任务。Cron 表达式 + 一次性延迟，支持静态配置和 MCP 动态创建 |
| **ask_user** | 1 | Claude 向用户提交互式问题，支持选项按钮 |
| **subagent** | 10 | 多 Agent 协作。PM 派生 worker 会话，支持角色范式 + git worktree 隔离 |
| **session_ask** | 3 | 跨会话 RPC。会话间互相提问并等待回复 |
| **context** | 3 | 上下文窗口监控 + 自动压缩。通过交付回调实现每会话 token 跟踪 |
| **browser** | 3+25 | 网页自动化（agent-browser CLI）+ 反爬虫抓取（Scrapling）+ 隐身浏览（Patchright 反检测 + 验证码求解） |
| **crypto** | 11 | 链上钱包管理。多链 EVM、代币转账、合约部署/调用、EIP-191 签名、x402 支付协议 |
| **telegram** | 0 | Telegram 机器人前端。多会话、流式输出、内联命令 |

所有扩展完全独立——删除目录 + 从 `enabled` 移除 = 零影响。

## 对比

### vs. OpenClaw / NanoClaw

这是两个主要的开源「Claw」框架。claude-ext 采用了根本不同的架构方案：

| | claude-ext | [OpenClaw](https://github.com/openclaw/openclaw)（257k stars） | [NanoClaw](https://github.com/qwibitai/nanoclaw)（18k stars） |
|---|---|---|---|
| **代码量** | ~27k 行（Python） | ~800k 行（TypeScript） | ~3.9k 行（TypeScript） |
| **架构** | 插件化内核 + 扩展 | 单体 Gateway | 极简宿主 + 容器 |
| **CLI 封装** | tmux 中的 `claude -p` | SDK 原生（无 CLI） | 容器中的 Claude Agent SDK |
| **IPC** | Unix socket Bridge RPC（0.06ms） | WebSocket RPC | 文件系统轮询（1s 间隔） |
| **MCP 集成** | 每会话注入 + 网关模式 | MCP Registry + Skill 注入 | 每容器单个 MCP 服务器 |
| **会话定制** | 每次提示的 `SessionOverrides` | 每 Agent 配置文件 | 每组 CLAUDE.md |
| **记忆** | 三层加密 + FTS5 | 文件型（MEMORY.md） | 两级 CLAUDE.md |
| **密钥管理** | Fernet 保险库 + bridge 隔离 | 配置中的 SecretRef | 挂载验证过滤 |
| **扩展模型** | `start()/stop()` 生命周期 + engine services | Skills + Channel + Provider 插件 | Skills（Markdown）+ 频道注册 |
| **消息平台** | Telegram（扩展方式） | 22+ 平台（内置） | 5 个平台（内置） |
| **隔离** | tmux + env unset + 工具禁用列表 | 回环绑定 + 可选 Docker | 容器优先（Docker） |

**核心差异化**：

- **CLI 封装 vs SDK 集成**：claude-ext 封装 `claude -p`，继承 Claude Code 完整特性集（权限模式、内置工具、MCP 客户端）。OpenClaw 和 NanoClaw 使用 SDK 原生集成，需自行重新实现 Claude Code 免费提供的功能。
- **每会话 MCP 注入**：claude-ext 为每个会话注册不同的 MCP 服务器配置，定制器可按提示动态包含/排除服务器。两个 Claw 项目均不具备此粒度。
- **网关模式**：多工具 MCP 服务器 token 减少 98%（如 Scrapling：5,640 → 120 tokens）。两个 Claw 项目无对应机制。
- **Bridge RPC**：Unix socket，0.06ms 延迟，专为 MCP↔主进程通信设计。OpenClaw 使用通用 WebSocket；NanoClaw 以 1s 间隔轮询文件系统。
- **带进程隔离的加密保险库**：密码短语仅保存在主进程内存中；MCP 服务器通过 bridge RPC 访问。两个 Claw 项目均无可比拟的凭证隔离。

**权衡**：OpenClaw 拥有远更多的消息平台覆盖（22+ vs 1）和社区 Skill 市场（13k+ Skills）。NanoClaw 提供更强的容器级隔离。claude-ext 追求基础设施深度，而非集成广度。

### vs. Agent SDK / API 类工具

| | claude-ext | Agent SDK | API 封装 |
|---|---|---|---|
| **计费** | 月度固定订阅 | 按 token 付费 | 按 token 付费 |
| **运行时** | tmux + `claude -p` | 进程内 API | 进程内 API |
| **多会话** | 内置（每用户槽位 + 队列） | 需手动管理 | 需手动管理 |
| **工具系统** | MCP 服务器（每会话注入） | 原生 tool_use | 各异 |
| **崩溃恢复** | 自动（tmux 存活） | 应用层处理 | 应用层处理 |
| **模型支持** | 仅 Claude（通过 CLI） | 仅 Claude（通过 API） | 通常多供应商 |

### vs. 多 Agent 编排器

[CrewAI](https://github.com/crewAIInc/crewAI)（45k stars）、[AutoGen](https://github.com/microsoft/autogen)（55k stars）、[LangGraph](https://github.com/langchain-ai/langgraph)（25k stars）等编排器在 API/SDK 层面协调 Agent。claude-ext 提供编排之下的**基础设施层**：

| 能力 | claude-ext | 典型编排器 |
|---|---|---|
| **会话生命周期** | 管理型（创建 → 队列 → 执行 → 恢复） | 启动即放 |
| **MCP 注入** | 每会话、动态、支持网关合并 | 无或静态 |
| **Bridge RPC** | 双向（MCP ↔ 主进程） | 无 |
| **凭证保险库** | 内置（加密 + bridge 隔离） | 无 |
| **自主心跳** | 三层 + 用量节流 | 最多基础 cron |
| **扩展体系** | 解耦生命周期 + 健康检查 | 通常为单体 |

### vs. 官方 Claude Code 插件

[Claude Code 插件](https://docs.anthropic.com/en/docs/claude-code)在单个会话内扩展功能。claude-ext 管理**多个并发会话**及服务端基础设施（持久状态、凭证库、自主调度、跨会话协调）。两者互补：claude-ext 会话可以使用 Claude Code 插件。

**适用场景：**

- **claude-ext** — 需要基于订阅价格的常驻自主 Agent，具备多会话管理和服务端基础设施
- **Agent SDK** — 正在构建基于 Claude 的产品，需要直接 API 控制和紧密集成
- **CC 插件** — 想在单个 Claude Code 会话内添加自定义工具和技能

## 快速开始

### 前置条件

- Python 3.12+
- tmux 3.x+
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) 已安装并认证（`claude auth login`）

### 安装

```bash
git clone https://github.com/claudebot101001/claude-ext.git
cd claude-ext
python -m venv .venv
source .venv/bin/activate
pip install -e ".[all]"
```

### 配置

```bash
cp config.yaml.example config.yaml
# 编辑 config.yaml，填入你的设置（bot token、用户 ID 等）
```

### 运行

```bash
python main.py
```

## 添加扩展

在 `extensions/` 下创建新目录，包含一个 `extension.py`：

```python
from core.extension import Extension

class ExtensionImpl(Extension):
    name = "my_extension"

    async def start(self) -> None:
        self.engine.session_manager.add_delivery_callback(self._deliver)

    async def stop(self) -> None:
        pass
```

然后在 `config.yaml` 的 `enabled` 列表中添加扩展名。无需修改核心层或其他扩展。

## 文档

- [架构详解](docs/ARCHITECTURE.md) — 完整实现细节、核心 API、扩展参考
- [技术参考](CLAUDE.md) — 快速开发指南
- [路线图](ROADMAP.md) — 已完成阶段与规划功能
- [贡献指南](CONTRIBUTING.md) — 开发环境、代码风格、PR 流程
- [安全](SECURITY.md) — 漏洞报告、安全模型

## 许可证

[MIT](LICENSE)
