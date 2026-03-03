# claude-ext Autonomous Agent Roadmap

Evolution from passive assistant to autonomous agent. Core philosophy unchanged: **Claude Code is the runtime; we only wrap CLI calls and manage extension lifecycles**. All new capabilities are implemented as independent extensions, following decoupling principles, with zero core modifications.

## Completed

### Phase 1: Vault — Encrypted Credential Store

`extensions/vault/` — Fernet symmetric encryption key-value credential store.

- **store.py**: PBKDF2-HMAC-SHA256 (600K iterations) key derivation + Fernet encrypt/decrypt + unified lockfile (`secrets.lock`, LOCK_SH/LOCK_EX read/write mutual exclusion) + atomic writes + 0700 directory permissions + 0600 file permissions
- **mcp_server.py**: `vault_store` / `vault_list` / `vault_retrieve` / `vault_delete` four MCP tools, calling main process VaultStore via bridge RPC
- **extension.py**: Registers `engine.services["vault"]` for programmatic access by other extensions + registers MCP server + bridge handler + key naming validation (enforces `category/service/name` format) + `_internal_prefixes` access control mechanism + system prompt constraints (no secret leakage)
- **Security design**: Passphrase priority: `CLAUDE_EXT_VAULT_PASSPHRASE` env var > `{vault_dir}/.passphrase` file > auto-generated (`secrets.token_urlsafe(32)`, 0600 permissions). MCP server process does not hold the passphrase; all encrypt/decrypt goes through bridge RPC in the main process. Uses `register_env_unset()` to ensure passphrase doesn't leak into Claude session environment. Every bridge call carries `session_id`; handler logs audit events
- **Honest positioning of security boundary**: Encryption is defense-in-depth (prevents ciphertext files from being directly readable if accidentally copied), not the primary security boundary. In `bypassPermissions` mode Claude has full filesystem access; the real access controls are `_internal_prefixes` (controls what MCP can read) and OS permissions (controls who can run the process)
- **Performance baseline** (measured): raw socket echo 0.06ms, vault_retrieve 0.24ms, vault_store 0.57ms. Bottleneck is Fernet crypto + disk I/O; socket overhead is negligible

#### Vault Key Naming Convention

All vault keys must use the `category/service/name` namespace format:

```
wallet/eth/privkey          # Wallet private key
wallet/eth/0xABC.../privkey # Multi-wallet: differentiate by address
email/smtp/password         # SMTP password
email/imap/password         # IMAP password
api/github/token            # GitHub API token
api/openai/key              # OpenAI API key
```

**Why define this now**: Phase 4 (Wallet) needs `internal_only` prefix policies (e.g. keys with `wallet/*` prefix can only be read by the wallet bridge handler internally, not returned to the LLM). If we don't store by namespace now, a data migration would be needed later. Prefix-based access control doesn't require an extra tag field — the key itself carries the classification.

#### Access Control Mechanism (Already in Place)

`extension.py` maintains `_internal_prefixes: list[str]` (currently empty list). The `vault_retrieve` bridge handler checks key prefixes; matching requests are rejected:

```python
# Current code (extension.py)
self._internal_prefixes: list[str] = []  # Phase 4+: ["wallet/"]

def _is_internal_key(self, key: str) -> bool:
    return any(key.startswith(p) for p in self._internal_prefixes)

# In vault_retrieve branch:
if self._is_internal_key(key):
    return {"error": f"Key '{key}' is internal-only. Use the dedicated extension tools."}
```

When Phase 4 Wallet launches, just `self._internal_prefixes.append("wallet/")` — private keys become unreadable via MCP `vault_retrieve`. Other extensions access programmatically via `engine.services["vault"].get()`, bypassing prefix restrictions (test coverage in `TestInternalPrefixes`). session_id is already passed through the bridge protocol; finer-grained control by session context can be added when needed.

### Phase 2: Memory — Cross-Session Memory System

`extensions/memory/` — Markdown-on-disk persistent memory, agent self-maintained.

- **store.py**: MemoryStore — path safety (rejects absolute paths, `..` traversal, non-`.md` files, symlink escapes) + unified lockfile (`memory.lock`, LOCK_SH/LOCK_EX) + atomic writes (write uses temp+rename, append directly under LOCK_EX) + 512 KB read limit + 50 result search limit
- **mcp_server.py**: `memory_read` / `memory_write` / `memory_append` / `memory_search` / `memory_list` five MCP tools, MCP process lazily initializes MemoryStore for direct read/write
- **extension.py**: Registers `engine.services["memory"]` + registers MCP server (injects `MEMORY_DIR` env var) + system prompt injection (personality_read at session start + on-demand memory_search) + seeds `TOPICS_INDEX.md` on first startup
- **Design decision**: Direct file I/O, no bridge RPC. Memory is plaintext Markdown with no encryption/access control needs. MCP server process holds its own MemoryStore instance, eliminating socket round-trips. Auditing can be met via `log.info` in MemoryStore methods
- **Distinction from Claude Code auto-memory**: CC's built-in auto-memory is stored in `~/.claude/projects/<project>/memory/` (per-project isolation). This extension stores in `~/.claude-ext/memory/` (globally shared). System prompt explicitly declares both are independent, requiring the Agent to only operate this extension's memory via MCP tools, not mixing with built-in Read/Write tools
- **Storage**: `TOPICS_INDEX.md` (topic catalog) / `topics/<name>.md` (deep knowledge) — searched on-demand via FTS5
- **Phase 2b (deferred)**: Local embedding model vector semantic search

### Pre-Heartbeat Architecture Improvements

Architecture hardening before Phase 3. Three lightweight core enhancements.

#### P1: MCP Tool Registry Introspection

`register_mcp_server(name, config, tools=None)` adds optional `tools` parameter declaring MCP server tool metadata. `list_mcp_tools()` returns all registered servers and their tools. All four MCP extensions (vault/memory/cron/ask_user) now pass tool metadata. Used for `/status` display and debugging.

#### P2: Structured Event Log

Added `core/events.py` — `EventLog` class. JSONL append file `{state_dir}/events.jsonl`, each line `{"ts", "type", "session_id", "detail"}`. Best-effort (no exceptions), locked (LOCK_SH/LOCK_EX), 10 MB single-generation rotation.

Event points: SessionManager 6 (created/destroyed/stopped/prompt/completed/dead) + Registry 3 (started/stopped/load_failed) + Vault 3 (store/retrieve/delete) + Cron 1 (triggered).

#### P3: Health Check Registry

`Extension` base class adds `health_check() -> dict` (non-abstract, defaults to `{"status": "ok"}`). `Registry.health_check_all()` aggregates all extension health states (5-second timeout). Five extensions each override to return extension-specific status (secret count, file count, job count, policy lists, etc.). Telegram `/status` command integrates display.

Policy visibility: Extensions self-report current policies via the `policies` field in health_check (e.g. vault's `_internal_prefixes`, telegram's `allowed_users` whitelist count), centralizing **visibility** rather than **enforcement**.

### Phase 3: Heartbeat — Autonomous Heartbeat

`extensions/heartbeat/` — Dual-channel scheduler + three-tier execution autonomous periodic agent.

- **store.py**: HeartbeatState (8 fields) + HeartbeatStore — JSON + flock state persistence + HEARTBEAT.md instruction file I/O + atomic writes + corrupt file fallback
- **mcp_server.py**: `heartbeat_instructions` / `heartbeat_status` / `heartbeat_trigger` / `heartbeat_get_trigger_command` four MCP tools. `heartbeat_instructions` reads/writes HEARTBEAT.md (getter/setter pattern), `heartbeat_status` queries scheduler state and optionally pauses/resumes (via `enabled` param), `heartbeat_trigger` uses bridge RPC to call main process `trigger()` method, `heartbeat_get_trigger_command` returns a shell command usable by external scripts
- **trigger_cli.py**: Pure-stdlib standalone CLI, external processes trigger heartbeat via bridge.sock. Agent obtains complete command via `heartbeat_get_trigger_command` (safely quoted with `shlex.quote()`), for embedding in background tasks or monitoring scripts
- **extension.py**: Dual-channel scheduler (Timer + asyncio.Queue Trigger) + three-tier execution (Tier 0 gate → Tier 1 pre-check → Tier 2 LLM decision → Tier 3 full session) + usage-aware cost control + adaptive backoff + delivery callback auto-cleanup + recovery check + bridge handler (`heartbeat_trigger` RPC)
- **Difference from cron**: Cron uses static prompts + fixed schedules; heartbeat dynamically reads instructions + Agent autonomously decides whether to act + auto-backs off on consecutive idle
- **Five safety valves for cost control**: Daily run limit (default 48), utilization throttle (80%: only immediate triggers pass), utilization pause (95%: all paused), active hours window, adaptive backoff (1x→2x→4x→8x)
- **Silent suppression**: Tier 2 uses `engine.ask()` lightweight subprocess; "NOTHING" is completely silent with no notifications. Only Tier 3 sessions have `chat_id`, so the frontend only delivers for those
- **Event triggering**: Three paths — ① Other extensions via `engine.services["heartbeat"].trigger(source, event_type, urgency, payload)` (Python API); ② Agent in session via MCP `heartbeat_trigger` tool (bridge RPC → main process `trigger()`); ③ External processes via `trigger_cli.py` connecting to bridge.sock (out-of-session event wakeup). `immediate` wakes scheduler immediately; `normal` accumulates until next timer expiry. Sync method, safe within single event loop
- **`notify_context` routing**: Config's `notify_context` is passed through to `session.context` as-is; heartbeat does not interpret its contents. Frontend extensions each extract what they need from context (e.g. Telegram reads `chat_id`, Discord reads `channel_id`). Zero frontend coupling

---

## Explored / Deferred

### System Prompt Replacement via `--system-prompt-file`

**Date**: 2026-03-03
**Status**: Reverted (commit `6fc4235`, reverted in `0ccfe6b`)
**Goal**: Replace Claude Code's built-in ~4500 token behavioral system prompt with a compact ~500 token version, saving ~4000 tokens per API call.

**What was built**:
- Config option `system_prompt_file` with three modes: `null` (default), `"compact"` (bundled `core/compact_prompt.md`), or custom file path
- `--system-prompt-file` CLI flag passed to `claude -p` invocations
- Extension-injected prompts still appended via `--append-system-prompt-file` (unaffected)
- Tests covering all three modes + path validation

**Why reverted**:
- `--system-prompt-file` **replaces** the entire built-in system prompt, including critical behavioral instructions (tool usage patterns, safety guardrails, output formatting). Writing a reliable compact replacement that preserves all necessary behaviors is fragile and hard to validate.
- The token savings (~4000/call) are modest relative to the risk of subtle behavioral regressions in agent sessions.
- Claude Code's built-in prompt evolves across CLI versions; maintaining a parallel compact version creates an ongoing maintenance burden.
- For our use case (headless `-p` mode), the built-in prompt's overhead is acceptable — the real cost drivers are conversation context and tool schemas, not the system prompt.

**Lessons learned**:
- `--system-prompt-file` is a valid CLI option (confirmed working), useful if Claude Code ever offers a more surgical prompt override mechanism (e.g., section-level replacement).
- `--append-system-prompt-file` (already used by extensions) is the safe path for injecting additional instructions without disrupting built-in behavior.
- Token optimization efforts are better directed at conversation compression and MCP tool schema pruning.

---

## Planned

### Phase 4: Wallet — Crypto Wallet Management

**Goal**: Let the Agent hold and manage on-chain assets.

**Architecture direction**:

```
extensions/wallet/
    chains/
        base.py        # ChainAdapter ABC (chain abstraction)
        evm.py         # EVM implementation (web3.py)
    store.py           # Wallet metadata + spending limit tracking
    mcp_server.py      # wallet_generate / wallet_list / wallet_balance / wallet_send / wallet_contract_call / wallet_sign_message
    extension.py       # MCP + bridge registration, vault dependency check
```

**Key design**:
- **Private key isolation (Cardinal Rule)**: Private keys stored in Vault, MCP server process calls VaultStore via bridge to decrypt → signs in memory → clears. **Private keys never enter the LLM context**
- **Spending limits**: Per-wallet configurable daily USD cap; exceeding triggers ask_user confirmation
- **Phase 4a**: EVM only (Ethereum + Arbitrum + Optimism + Base), ChainAdapter ABC reserves other chains
- Hard dependency on Phase 1 (Vault)

---

### Phase 5a: Email — Independent Mailbox

**Goal**: Agent has its own mailbox, can send, receive, and search emails.

**Architecture direction**:

```
extensions/email/
    imap_listener.py   # IMAP IDLE background task (new mail → delivery callback notification)
    mcp_server.py      # email_send / email_check / email_read / email_search / email_reply
    extension.py       # IMAP IDLE listener + SMTP sending + delivery callback
```

**Key design**:
- SMTP/IMAP credentials stored in Vault
- IMAP IDLE real-time monitoring for new emails, notifies active session via delivery callback
- Send rate limiting (default 10/hour), prevents Agent runaway
- Exponential backoff reconnect + polling fallback

---

### Phase 5b: Browser — Playwright Browser

**Goal**: Agent can browse web pages, take screenshots, fill forms, execute JS.

**Architecture direction**:

```
extensions/browser/
    mcp_server.py      # browser_navigate / browser_screenshot / browser_click / browser_type / browser_evaluate
    extension.py       # Playwright lifecycle + bridge handler
```

**Key design**:
- Single Chromium instance, per-session browser context (isolates cookies)
- MCP tool → bridge RPC → main process Playwright operations → return results (avoids each MCP process launching an independent browser)
- Screenshots saved to file → Claude views with Read tool (CC natively supports images)
- Max simultaneous open pages configurable, memory threshold restart

---

## Dependency Graph

```
Phase 1: Vault  ←── Credential foundation for all subsequent phases ✅ Completed
    ↓
Phase 2: Memory ←── Prerequisite for autonomous behavior ✅ Completed
    ↓
Phase 3: Heartbeat ←── The leap from passive to proactive ✅ Completed
    ↓ (can parallelize with 4, 5)
Phase 4: Wallet ←── Depends on Vault (hard dependency)
Phase 5a: Email ←── Depends on Vault (SMTP credentials)
Phase 5b: Browser ←── No hard dependencies
```

## Recommended enabled Order

```yaml
enabled:
  - vault              # Must be first (other extensions depend on it)
  - memory             # Load early (heartbeat benefits)
  - ask_user           # Interactive confirmation
  - heartbeat          # Autonomous behavior
  - cron               # Scheduled tasks
  - wallet             # Wallet (depends on vault)
  - email              # Email (depends on vault)
  - browser            # Browser
  - telegram           # Frontend (last to register delivery callback → flush pending)
```
