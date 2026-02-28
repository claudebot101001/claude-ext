[English](README.md) | 中文

# claude-ext

[![CI](https://github.com/claudebot101001/claude-ext/actions/workflows/ci.yml/badge.svg)](https://github.com/claudebot101001/claude-ext/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)

[Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) 的可扩展框架。Claude Code 本身已经是完整的 AI 编程 Agent——本框架仅做两件事：包装 CLI 调用和管理扩展生命周期。

## 为什么选择 claude-ext

### 订阅优势

claude-ext 底层调用官方 `claude` 二进制文件（`claude -p`），以第一方 Claude Code 会话运行，使用你现有的 Claude 订阅（Free、Pro 或 Max 计划 OAuth）进行认证。

其他方案——[Agent SDK](https://platform.claude.com/docs/en/agent-sdk/overview)、[OpenClaw](https://github.com/openclaw/openclaw) 或任何直接调用 API 的第三方工具——必须使用 API key 认证，按 token 付费。

对于高强度的 Agent 工作负载，这意味着显著的成本差异。Max 计划按月固定收费，而同等工作量的 API 使用费用可以轻松超过每月 $3,000。

| | claude-ext | Agent SDK | OpenClaw |
|---|---|---|---|
| **认证方式** | 订阅 OAuth（通过 CLI） | API key | API key |
| **计费模式** | 月度固定费用 | 按 token 付费 | 按 token 付费 |
| **运行方式** | 包装 `claude -p` 二进制 | 直接 API 调用 | 直接 API 调用 |

### 合规性

claude-ext 不提取、存储或代理 OAuth token。它将官方 `claude` CLI 二进制文件作为子进程启动——与你在终端中运行的完全相同。token 处理完全在 Anthropic 自己的代码内部完成。

Anthropic 的[法律与合规文档](https://code.claude.com/docs/en/legal-and-compliance)指出：

> **OAuth authentication** (used with Free, Pro, and Max plans) is intended exclusively for Claude Code and Claude.ai. Using OAuth tokens obtained through Claude Free, Pro, or Max accounts in any other product, tool, or service — including the Agent SDK — is not permitted and constitutes a violation of the Consumer Terms of Service.

翻译：OAuth 认证（用于 Free、Pro 和 Max 计划）专供 Claude Code 和 Claude.ai 使用。在任何其他产品、工具或服务（包括 Agent SDK）中使用通过 Claude Free、Pro 或 Max 账户获取的 OAuth token 是不被允许的，且违反消费者服务条款。

claude-ext 将工作委托给 Claude Code 本身，这正是 OAuth token 的预期用途。

> **免责声明**：Anthropic 的条款和政策可能随时变更。请始终查阅[最新条款](https://code.claude.com/docs/en/legal-and-compliance)以确认当前政策。

## 工作原理

```
用户 / 前端（如 Telegram）
        │
        ▼
    扩展层 ──── config.yaml
        │
        ▼
   ClaudeEngine
   SessionManager
   Bridge RPC
        │
        ▼
   tmux 会话 ──── MCP 服务器（每会话独立）
        │
        ▼
   claude -p ──── 文件 IPC（prompt → stream → result）
```

每个 Claude Code 会话运行在独立的 tmux 会话中，与主进程完全解耦。扩展通过基于文件的 IPC 与会话通信，通过异步回调接收结果。

**核心特性：**

- **崩溃恢复** — tmux 会话在主进程重启后存活，自动恢复
- **多用户** — 每用户独立会话槽位和队列
- **动态扩展** — 添加/移除扩展无需修改核心层或其他扩展
- **Bridge RPC** — MCP 子进程通过 Unix socket 调用主进程（行分隔 JSON）
- **结构化事件** — JSONL 事件日志，支持轮转，用于可观测性
- **用量感知** — 扩展可查询认证状态和 API 用量，用于成本控制

## 对比

| | claude-ext | Agent SDK | OpenClaw |
|---|---|---|---|
| **认证方式** | 订阅 OAuth（通过 CLI） | API key | API key |
| **计费** | 月度固定费用 | 按 token 付费 | 按 token 付费 |
| **运行时** | tmux + `claude -p` 子进程 | 进程内 API 调用 | 进程内 API 调用 |
| **语言** | Python | TypeScript / Python | Python |
| **多会话** | 支持（每用户槽位 + 队列） | 需手动管理 | 需手动管理 |
| **工具系统** | MCP 服务器（每会话独立） | 原生 tool_use | Skills（5700+） |
| **崩溃恢复** | 自动（tmux 存活） | 应用层处理 | 应用层处理 |
| **模型支持** | 仅 Claude（通过 CLI） | 仅 Claude（通过 API） | 12+ 供应商 |

**适用场景：**

- **claude-ext** — 希望以订阅价格运行自主 Claude Agent，具备多会话管理和 Claude Code 完整内置工具集（文件编辑、bash 等）
- **Agent SDK** — 正在构建基于 Claude 的产品，需要直接 API 控制、自定义工具定义和紧密集成
- **OpenClaw** — 需要多供应商支持、大型技能库或跨平台前端（Discord、Slack、Web 等）

这些是面向不同场景的互补工具。

## 扩展

| 扩展 | 描述 | 状态 |
|------|------|------|
| **telegram** | Telegram 机器人桥接。多会话管理、流式输出、内联命令 | 稳定 |
| **vault** | 加密凭证库（Fernet + PBKDF2）。Claude 通过 MCP 工具存取密钥 | 稳定 |
| **memory** | 跨会话持久记忆。Markdown 文件 + MCP 工具读写搜索 | 稳定 |
| **heartbeat** | 自主定期 Agent。双通道调度 + LLM 决策 + 用量感知成本控制 | 稳定 |
| **cron** | 定时任务执行。静态配置 + MCP 动态创建 | 稳定 |
| **ask_user** | 会话执行中 Claude 向用户提问的交互机制 | 稳定 |

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

- [技术参考](CLAUDE.md) — 开发指南与核心 API
- [架构详解](docs/ARCHITECTURE.md) — 完整实现细节
- [路线图](ROADMAP.md) — 已完成阶段与规划功能
- [贡献指南](CONTRIBUTING.md) — 开发环境、代码风格、PR 流程
- [安全](SECURITY.md) — 漏洞报告、安全模型

## 许可证

[MIT](LICENSE)
