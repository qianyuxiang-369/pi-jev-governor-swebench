<div align="center">

# pi-jev-governor

**[English](#en)** | **[简体中文](#zh)**

</div>

---

<div align="center">

<a id="en"></a>

## English

</div>

A decision governor for the [pi](https://pi.dev) coding agent — routes four critical agent decisions through the JEV Decisions API to add intelligent planning, model routing, permission control, and outcome validation.

### What it does

| Decision | pi event | Result |
|---|---|---|
| `plan_strategy` | `before_agent_start` | `direct`, `plan_only`, or a read-only `plan_then_execute` phase |
| `tier` | `before_agent_start` | `small`, `normal`, or `strong`, with model compatibility guards |
| `permission.action` | `tool_call` | Deterministic rules → JEV adjudication → human/fail-closed fallback |
| `outcome` | `agent_before_settle` | Finish, retry, or replan with a shared correction budget |

- `plan_strategy` and `tier` share a single request. A `plan_then_execute` task makes an additional Outcome request to validate the plan before execution.
- Planning revisions and execution corrections share a hard maximum of **2** automatic corrections.
- Loaded directly by pi. **No bundled runtime dependencies. Does not modify pi itself.**

### Install

```bash
# From Git
pi install git:github.com/qianyuxiang-369/pi-jev-governor@v0.1.0

# From npm
pi install npm:pi-jev-governor@0.1.0

# From local checkout
pi install ./pi-jev-governor
```

Try without installing:

```bash
pi -e ./pi-jev-governor
```

> **Requires pi 0.87.1+** — both 0.87.1 and 1.1.0 pass the full acceptance suite (54 assertions each; evidence in [`evidence/acceptance-20261008-163809`](evidence/acceptance-20261008-163809) for 0.87.1 and [`evidence/acceptance-20261008-180941`](evidence/acceptance-20261008-180941) for 1.1.0). `@earendil-works/pi-ai` and `@earendil-works/pi-coding-agent` are peer dependencies supplied by pi.

### Configure

At minimum, set the JEV credential and one pi model per tier. The repo provides a `.env.local` template and a launch script:

```bash
cp .env.local.example .env.local   # fill in your keys and models
./scripts/run.sh                   # run from any scratch project directory
```

Manual equivalent:

```bash
export PI_JEV_API_KEY="$OPENROUTER_API_KEY"
export PI_JEV_MODEL_SMALL="openrouter:qwen/qwen3.7-flash"
export PI_JEV_MODEL_NORMAL="openrouter:qwen/qwen3.7-plus"
export PI_JEV_MODEL_STRONG="openrouter:qwen/qwen3.7-max"
pi -e ./pi-jev-governor
```

Model references use `provider:modelId` format (split on the first colon).

<details>
<summary><strong>Environment variables reference</strong></summary>

| Variable | Default | Notes |
|---|---|---|
| `PI_JEV_ENABLED` | `true` | Set `false` to disable all four decisions |
| `PI_JEV_URL` | `https://openrouter.ai/api/alpha/decisions` | JEV endpoint |
| `PI_JEV_API_KEY` | *required* | API credential |
| `PI_JEV_MODEL` | `typesafe/jev-1.13` | Decision model |
| `PI_JEV_TIMEOUT_MS` | `3000` | Total deadline per request (1–60000 ms). OpenRouter typically answers in 2–4 s; `10000` is practical |
| `PI_JEV_MODEL_SMALL` | *required* | `provider:modelId` |
| `PI_JEV_MODEL_NORMAL` | *required* | `provider:modelId` |
| `PI_JEV_MODEL_STRONG` | *required* | `provider:modelId` |
| `PI_JEV_ROUTE_CONFIDENCE` | `0.8` | Below → direct/current-model fallback |
| `PI_JEV_PERMISSION_CONFIDENCE` | `0.9` | Below → human approval or fail-closed |
| `PI_JEV_OUTCOME_CONFIDENCE` | `0.8` | Plan only advances on high-confidence finish |
| `PI_JEV_MAX_CORRECTIONS` | `2` | Task-wide budget, hard-capped at 2 |
| `PI_JEV_OUTCOME_GIT_DIFF` | `false` | Opt in to `git diff --stat`; full diff is never sent |
| `PI_JEV_PLAN_AUTO_EXECUTE` | `true` | `false` → plans require UI confirmation |
| `PI_JEV_LOG_DIR` | platform state dir | Explicit log directory override |

</details>

**Tier switching** keeps the current model when the target cannot be resolved, is outside `--models` scope, has insufficient context window, cannot accept images already in the branch, lacks provider auth, or the user manually changed models.

#### Switches & status

- `pi --no-jev` — disable plugin for this invocation
- `/jev` — show current phase, tier, correction budget, decision count, and log file
- `/jev off` / `/jev on` — session-level toggle (`on` cannot override `--no-jev` or `PI_JEV_ENABLED=false`)

**Degraded mode**: missing/invalid remote config enters `degraded` (not full bypass). Routing falls back to direct/current-model, Outcome automation stops, but the local permission gate stays active. Calls needing remote judgment ask the user when UI is available and fail-closed when it is not.

### Lifecycle

```text
user prompt
   │
   ▼
route: plan_strategy + tier
   ├─ direct ────────────────────────────────┐
   ├─ plan_only (read-only) ─────────────────┤
   └─ planning (read-only)                   │
        │ Outcome validates the plan         │
        ├─ retry/replan → revise plan        │
        ├─ finish → restore tools, execute   │
        └─ unavailable/low confidence → stop │
                                              ▼
                                     Outcome validates delivery
                                        ├─ finish → settle
                                        └─ retry/replan → correct and continue
```

- Each new `before_agent_start` begins a task and clears its approval cache. Continuations, steering, and follow-ups inherit the active route.
- State is stored as `jev-state` session entries. Resume and tree navigation restore from the active branch via `getBranch()`, including the read-only tool snapshot, correction budget, and model-routing state.
- Continuation is committed atomically at the settle boundary: state entry + `custom_message` with `continue: true`, then pi recomputes whether the final context can continue.

### Permission gate

Four-layer permission pipeline:

1. **Phase gate** — planning phases allow only built-in read tools and allowlisted read-only shell commands
2. **Deterministic rules** — block hard-deny shell patterns, allow built-in read tools, honor task approvals
3. **JEV adjudication** — clean, sanitized tool input sent to JEV
4. **Human / fail-closed** — `ask`, low confidence, sensitive input, or remote error → user prompt; no UI → block

Human choices are task-scoped: allow once · allow exact call for task · allow tool for task · block. Caches clear on next task and on session-tree navigation.

### Privacy & data disclosure

The plugin sends the **minimum evidence** needed for each decision:

| Data | Remote behavior |
|---|---|
| Task prompt | Truncated, secret literals scrubbed |
| Ordinary tool input | Sensitive keys redacted, length limited |
| Clean shell command | Sanitized then sent |
| Command with credentials | **Never sent** — local confirmation or fail-closed |
| Sensitive-file evidence | Replaced with omission marker |
| Tool-result tail | ≤ 10 entries, truncated and redacted |
| `git diff --stat` | Only when explicitly enabled; full diff never sent |

Credential detection is intentionally narrow (Authorization/Bearer headers, `KEY=value` credential contexts, private-key headers, configured secrets). Broad entropy matching is not used — hashes and git SHAs are common in legitimate commands.

<details>
<summary><strong>Decision logs</strong></summary>

Each session gets its own `decisions-<session-id>.jsonl` file. Log directory priority:

1. `PI_JEV_LOG_DIR`
2. `$XDG_STATE_HOME/pi-jev`
3. macOS: `~/Library/Application Support/pi-jev`
4. Windows: `%LOCALAPPDATA%/pi-jev`
5. Fallback: `~/.local/state/pi-jev`

Directories: mode `0700`; files: mode `0600`. Logs contain structured answers, confidence, timing, usage, effective action, and sanitized error reasons — never raw prompts, tool arguments, provider bodies, or keys.

</details>

### Security boundary

This package is **workflow control, not an OS sandbox**. It runs inside pi with the same process privileges. Use containerization or another system security boundary for untrusted or unattended execution.

### Development

```bash
npm install
npm run typecheck
npm test
```

Tests cover decision logic, extension wiring, parallel permission confirmations, branch recovery, log permissions, and a real public-API agent lifecycle using pi's faux provider.

#### Acceptance testing

An automated acceptance harness exercises the plugin end-to-end: real pi, real agent models, and a deterministic local JEV mock. Each case leaves per-case evidence and a summary `RESULTS.md`:

```bash
./scripts/run-acceptance.sh    # 22 cases; requires .env.local and pi on PATH
```

Interactive TUI behavior is covered by the manual screenshot protocol in [docs/acceptance-tests.md](docs/acceptance-tests.md).

### Documentation

- [docs/design.md](docs/design.md) — v3 design specification
- [docs/acceptance-tests.md](docs/acceptance-tests.md) — acceptance test protocol
- [scripts/smoke.md](scripts/smoke.md) — manual smoke checklist

### License

MIT

---

<div align="center">

<a id="zh"></a>

## 简体中文

</div>

[pi](https://pi.dev) 编码代理的「治理器」（governor）——通过 JEV Decisions API 路由四个关键决策点，为编码代理增加智能规划、模型路由、权限控制和结果校验能力。

### 功能概览

| 决策 | pi 事件 | 结果 |
|---|---|---|
| `plan_strategy` | `before_agent_start` | `direct`、`plan_only`，或只读的 `plan_then_execute` 阶段 |
| `tier` | `before_agent_start` | `small` / `normal` / `strong` 三档，带模型兼容性守卫 |
| `permission.action` | `tool_call` | 确定性规则 → JEV 裁决 → 人工/fail-closed 兜底 |
| `outcome` | `agent_before_settle` | finish / retry / replan，任务级共享修正预算 |

- `plan_strategy` 和 `tier` 合并在一次请求中。`plan_then_execute` 任务在执行前会额外发起一次 Outcome 请求校验计划。
- 规划修订与执行纠正共享最多 **2 次**的硬上限修正预算。
- 由 pi 直接加载，**无捆绑运行时依赖，不修改 pi 本身**。

### 安装

```bash
# 从 Git 安装
pi install git:github.com/qianyuxiang-369/pi-jev-governor@v0.1.0

# 从 npm 安装
pi install npm:pi-jev-governor@0.1.0

# 从本地目录安装
pi install ./pi-jev-governor
```

不安装、直接试用本地检出：

```bash
pi -e ./pi-jev-governor
```

> **需要 pi 0.87.1+** ——0.87.1 与 1.1.0 均通过完整验收（各 54 项断言；证据分别在 0.87.1 的 [`evidence/acceptance-20261008-163809`](evidence/acceptance-20261008-163809) 与 1.1.0 的 [`evidence/acceptance-20261008-180941`](evidence/acceptance-20261008-180941)）。`@earendil-works/pi-ai` 与 `@earendil-works/pi-coding-agent` 是 peer 依赖，由 pi 提供。

### 配置

最少只需配置 JEV 凭据和每档一个 pi 模型。仓库提供 `.env.local` 模板和启动脚本：

```bash
cp .env.local.example .env.local   # 填入 key 和模型
./scripts/run.sh                   # 在任意草稿项目目录下执行
```

等价的手动方式：

```bash
export PI_JEV_API_KEY="$OPENROUTER_API_KEY"
export PI_JEV_MODEL_SMALL="openrouter:qwen/qwen3.7-flash"
export PI_JEV_MODEL_NORMAL="openrouter:qwen/qwen3.7-plus"
export PI_JEV_MODEL_STRONG="openrouter:qwen/qwen3.7-max"
pi -e ./pi-jev-governor
```

模型引用格式为 `provider:modelId`，按第一个冒号切分。

<details>
<summary><strong>环境变量参考</strong></summary>

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `PI_JEV_ENABLED` | `true` | 设为 `false` 关闭全部四个决策 |
| `PI_JEV_URL` | `https://openrouter.ai/api/alpha/decisions` | JEV 端点 |
| `PI_JEV_API_KEY` | *必填* | API 凭据 |
| `PI_JEV_MODEL` | `typesafe/jev-1.13` | 决策模型 |
| `PI_JEV_TIMEOUT_MS` | `3000` | 整次请求总超时（1–60000 ms）。OpenRouter 实测常见 2–4 s，建议设 `10000` |
| `PI_JEV_MODEL_SMALL` | *必填* | `provider:modelId` |
| `PI_JEV_MODEL_NORMAL` | *必填* | `provider:modelId` |
| `PI_JEV_MODEL_STRONG` | *必填* | `provider:modelId` |
| `PI_JEV_ROUTE_CONFIDENCE` | `0.8` | 低于该置信度回退 direct/当前模型 |
| `PI_JEV_PERMISSION_CONFIDENCE` | `0.9` | 低于该置信度转人工确认；无 UI 则阻断 |
| `PI_JEV_OUTCOME_CONFIDENCE` | `0.8` | 计划只有在明确且高置信的 finish 下才进入执行 |
| `PI_JEV_MAX_CORRECTIONS` | `2` | 任务级预算，硬上限 2 |
| `PI_JEV_OUTCOME_GIT_DIFF` | `false` | 显式开启才发送 `git diff --stat`；从不发送 diff 正文 |
| `PI_JEV_PLAN_AUTO_EXECUTE` | `true` | 设为 `false` 时，计划通过后需 UI 确认才执行 |
| `PI_JEV_LOG_DIR` | 平台状态目录 | 显式指定日志目录 |

</details>

**tier 切换**在以下情况保留当前模型：目标模型无法解析、不在 `--models` 作用域内、上下文窗口不足、无法接受分支中已有的图片、provider 未配置鉴权，或用户手动切换过模型。

#### 开关与状态

- `pi --no-jev` — 对本次调用禁用插件
- `/jev` — 显示当前阶段、tier、修正预算、决策计数和日志文件
- `/jev off` / `/jev on` — 会话级切换（`on` 不能覆盖 `--no-jev` 或 `PI_JEV_ENABLED=false`）

**降级模式**：远端配置缺失或无效时进入 `degraded` 模式（非完全旁路）。路由使用 direct/当前模型兜底，Outcome 自动化停止，但本地权限门保持生效。需要远端裁决的调用在有 UI 时询问用户，无 UI 时 fail-closed 阻断。

### 生命周期

```text
用户 prompt
   │
   ▼
路由：plan_strategy + tier
   ├─ direct ────────────────────────────────┐
   ├─ plan_only（只读）──────────────────────┤
   └─ planning（只读）                        │
        │ Outcome 校验计划                    │
        ├─ retry/replan → 修订规划            │
        ├─ finish → 恢复工具，开始执行         │
        └─ 不可用/低置信 → 停止                │
                                              ▼
                                     Outcome 校验交付物
                                        ├─ finish → 结束
                                        └─ retry/replan → 纠正并续跑
```

- 每次新的 `before_agent_start` 开启一个任务并清空其批准缓存。续跑、steering 和追问继承活动路由，不会重新路由。
- 状态以 `jev-state` 会话条目存储。会话恢复与分支导航通过 `getBranch()` 从活动分支还原，包括只读工具快照、修正预算和模型路由状态。
- 续跑在 settle 边界原子提交：状态条目与 `custom_message` 以 `continue: true` 一起返回，随后由 pi 重算最终上下文能否续跑。

### 权限门

四层权限管线：

1. **阶段门** — 规划阶段仅允许内置读工具和白名单内的只读 shell 命令
2. **确定性规则** — 拦截高危 shell 模式、放行内置读工具、遵循任务级批准
3. **JEV 裁决** — 干净且已脱敏的工具输入才发送给 JEV
4. **人工 / fail-closed** — `ask`、低置信、敏感输入或远端错误 → 转给用户；无 UI 则阻断

人工选择是任务级的：仅允许一次 · 允许本任务此调用 · 允许本任务此工具 · 阻止。缓存在下一个任务和会话树导航时清空。

### 隐私与数据披露

插件为每个决策发送**最少量的证据**：

| 数据 | 远端行为 |
|---|---|
| 任务 prompt | 截断并擦除 secret 字面量 |
| 普通工具输入 | 敏感键脱敏，长度受限 |
| 干净 shell 命令 | 脱敏后发送 |
| 含凭据的命令 | **从不发送** — 本地确认或 fail-closed |
| 敏感文件证据 | 替换为省略标记 |
| 工具结果尾部 | ≤ 10 条，截断并脱敏 |
| `git diff --stat` | 仅显式开启时发送；从不发送 diff 正文 |

凭据检测刻意保持窄范围（Authorization/Bearer 头、`KEY=value` 凭据上下文、私钥头、配置 secret 字面量）。不使用广义高熵匹配——哈希常量和 git SHA 在合法命令中很常见。

<details>
<summary><strong>决策日志</strong></summary>

每个会话独立文件 `decisions-<session-id>.jsonl`。日志目录优先级：

1. `PI_JEV_LOG_DIR`
2. `$XDG_STATE_HOME/pi-jev`
3. macOS：`~/Library/Application Support/pi-jev`
4. Windows：`%LOCALAPPDATA%/pi-jev`
5. 兜底：`~/.local/state/pi-jev`

目录权限 `0700`，文件权限 `0600`。日志包含结构化答案、置信度、耗时、用量、实际生效动作和脱敏错误原因——不包含原始 prompt、工具参数、provider 响应体或密钥。

</details>

### 安全边界

本包是**工作流控制，不是操作系统级沙箱**。它与 pi 运行在同一进程、拥有相同权限。不可信或无人值守的执行请使用容器化或其他系统安全边界。

### 开发

```bash
npm install
npm run typecheck
npm test
```

测试覆盖纯决策逻辑、扩展接线、并发权限确认、分支恢复、日志权限，以及基于 pi 公开 API 和 faux provider 的真实 agent 生命周期测试。

#### 验收测试

自动化验收 harness 做端到端验证：真实的 pi、真实的 agent 模型，JEV 决策指向本地确定性 mock。每个用例留档四份证据并生成 `RESULTS.md` 汇总：

```bash
./scripts/run-acceptance.sh    # 22 个用例；需要 .env.local 与 PATH 中的 pi
```

交互 TUI 行为由 [docs/acceptance-tests.md](docs/acceptance-tests.md) 中的人工截图协议覆盖。

### 文档

- [docs/design.md](docs/design.md) — v3 设计规范
- [docs/acceptance-tests.md](docs/acceptance-tests.md) — 验收测试协议
- [scripts/smoke.md](scripts/smoke.md) — 人工冒烟清单

### 许可证

MIT
