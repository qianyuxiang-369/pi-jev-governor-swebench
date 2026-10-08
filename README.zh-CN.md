# pi-jev

[English](README.md) | 简体中文

一个 [pi](https://pi.dev) 包，把编码代理的四个决策点路由到 JEV Decisions API：

| 决策 | pi 事件 | 结果 |
|---|---|---|
| `plan_strategy` | `before_agent_start` | `direct`、`plan_only`，或只读的 `plan_then_execute` 阶段 |
| `tier` | `before_agent_start` | `small` / `normal` / `strong` 三档，带模型兼容性守卫 |
| `permission.action` | `tool_call` | 确定性规则、JEV 裁决、人工/fail-closed 兜底 |
| `outcome` | `agent_before_settle` | finish / retry / replan，任务级共享修正预算 |

`plan_strategy` 和 `tier` 合并在一次请求里。`plan_then_execute` 任务在执行前会额外发起一次 Outcome 请求校验计划。规划修订与执行纠正共享一个最多 2 次的硬上限修正预算。

本包由 pi 直接加载，无捆绑运行时依赖，也不修改 pi 本身。

## 安装

```bash
pi install git:github.com/<user>/pi-jev@v0.1.0
pi install npm:pi-jev@0.1.0
pi install ./pi-jev
```

不安装、直接试用本地检出：

```bash
pi -e ./pi-jev
```

需要 pi 0.87.1——这是已验证的基准版本：全部单元测试与 [`evidence/`](evidence) 中的验收证据均基于它产生。更高版本（含 pi 1.x）未经测试，扩展 API 可能有变动。`@earendil-works/pi-ai` 与 `@earendil-works/pi-coding-agent` 是 peer 依赖，由 pi 提供。

## 配置

最少只需配置 JEV 凭据和每档一个 pi 模型。本仓库把本地配置放在 git 忽略的 `.env.local`（模板见 [`.env.local.example`](.env.local.example)），并提供启动脚本 [`scripts/run.sh`](scripts/run.sh)：先 source 配置，再带着扩展启动 pi。

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
pi -e ./pi-jev
```

模型引用格式为 `provider:modelId`，按第一个冒号切分。

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `PI_JEV_ENABLED` | `true` | 设为 `false` 关闭全部四个决策 |
| `PI_JEV_URL` | `https://openrouter.ai/api/alpha/decisions` | JEV 端点 |
| `PI_JEV_API_KEY` | 必填 | API 凭据 |
| `PI_JEV_MODEL` | `typesafe/jev-1.13` | 决策模型 |
| `PI_JEV_TIMEOUT_MS` | `3000` | 整次请求（含可选重试）的总超时，1–60000 ms。OpenRouter 托管端点实测常见 2–4 s，建议设 `10000` |
| `PI_JEV_MODEL_SMALL` | 必填 | `provider:modelId` |
| `PI_JEV_MODEL_NORMAL` | 必填 | `provider:modelId` |
| `PI_JEV_MODEL_STRONG` | 必填 | `provider:modelId` |
| `PI_JEV_ROUTE_CONFIDENCE` | `0.8` | 低于该置信度回退 direct/当前模型 |
| `PI_JEV_PERMISSION_CONFIDENCE` | `0.9` | 低于该置信度转人工确认；无 UI 则阻断 |
| `PI_JEV_OUTCOME_CONFIDENCE` | `0.8` | 计划只有在明确且高置信的 finish 下才进入执行 |
| `PI_JEV_MAX_CORRECTIONS` | `2` | 任务级预算，硬上限 2 |
| `PI_JEV_OUTCOME_GIT_DIFF` | `false` | 显式开启才发送 `git diff --stat`；从不发送 diff 正文 |
| `PI_JEV_PLAN_AUTO_EXECUTE` | `true` | 设为 `false` 时，计划通过后需 UI 确认才执行 |
| `PI_JEV_LOG_DIR` | 平台状态目录 | 显式指定日志目录 |

tier 切换在以下情况保留当前模型：目标模型无法解析、不在 `--models` 作用域内、上下文窗口小于当前用量、无法接受分支中已有的图片、provider 未配置鉴权，或用户手动切换过模型。

### 开关与状态

- `pi --no-jev` 对本次调用禁用插件。
- `/jev` 显示当前阶段、tier、修正预算、决策计数和日志文件路径。
- `/jev off` 与 `/jev on` 切换会话级开关。
- `/jev on` 不能覆盖 `--no-jev` 或 `PI_JEV_ENABLED=false`。

远端配置缺失或无效时插件进入 `degraded` 模式而非完全旁路：路由使用 direct/当前模型兜底，Outcome 自动化停止，但本地权限门保持生效。需要远端裁决的调用在有 UI 时询问用户，无 UI 时 fail-closed 阻断。

## 生命周期

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

每次新的 `before_agent_start` 开启一个任务并清空其批准缓存。续跑、steering 和追问继承活动路由，不会重新路由。

状态以 `jev-state` 会话条目存储。会话恢复与分支导航通过 `getBranch()` 从活动分支还原，包括只读工具快照、修正预算和模型路由状态。`session_tree` 同时清空全部任务批准。

续跑在 settle 边界原子提交：状态条目与 `custom_message` 以 `continue: true` 一起返回，随后由 pi 重算最终上下文能否续跑。插件不以处理器执行前的 `context.canContinue` 预览为准，也不使用嵌套 `sendMessage` 兜底。

## 权限门

权限裁决分四层：

1. 规划阶段只允许内置读工具和白名单内的只读 shell 命令。
2. 确定性规则：拦截硬拒绝 shell 模式、放行内置读工具、遵循任务级批准。
3. 干净且已脱敏的工具输入才发送给 JEV。
4. `ask`、低置信、敏感输入或远端错误转给用户；无 UI 则阻断。

序列化、UI 或处理器的意外失败同样转换为脱敏的 fail-closed 阻断，原始异常文本不会返回给模型。计划校验对低置信、服务不可用、答案无效、证据过大分别报告不同的原因；任何一种都不会启动执行。

人工选择是任务级的：

- 仅允许一次（Allow once）
- 本任务内允许这一完全相同的调用
- 本任务内允许此工具
- 阻止（Block）

exact-call 与 tool 两级缓存在下一个任务和会话树导航时清空。敏感输入只提供"仅允许一次"或"阻止"。

pi 可能并发发出兄弟工具调用。pi-jev 将权限裁决与确认对话框串行化以避免 UI 和缓存竞争；批准之后的工具执行不串行化。`terminate` 结果不保证已派发的兄弟调用不会运行，因此每个调用必须凭自身裁决保证安全。

## 隐私与数据披露

插件为每个决策发送最少量的证据：

| 数据 | 远端行为 |
|---|---|
| 任务 prompt | 截断并擦除配置中的 secret 字面量 |
| 普通工具输入 | 敏感键脱敏并限长 |
| 干净 shell 命令 | 脱敏后发送，使 JEV 能理解操作语义 |
| 含凭据的命令 | 从不发送；本地确认或 fail-closed 处理 |
| 敏感文件证据 | 整条替换为省略标记 |
| 工具结果尾部 | 最多 10 条，截断并脱敏 |
| `git diff --stat` | 显式开启才发送；从不发送 diff 正文 |

凭据检测刻意保持窄范围：`Authorization`/`Bearer` 头、凭据形态的 `KEY=value` 上下文、私钥头、配置中的 secret 字面量。不使用广义高熵匹配，因为哈希常量和 git SHA 在合法命令中很常见。

工具输出中内嵌凭据上下文（`Bearer`/`Authorization` 头或私钥块）时整条省略——因此读取一个名字无害但内容含 token 的文件不会经由 outcome 证据泄露。纯高熵字符串（git SHA、哈希常量）保留。

敏感键下的值替换为 `[redacted]`；配置 secret 字面量在序列化后再次全文擦除。证据超过 96,000 字符时显式拒绝而非静默裁剪。客户端错误只含原因枚举，绝不包含 provider 响应体、URL 或凭据。

### 决策日志

每个会话有独立文件 `decisions-<session-id>.jsonl`。日志目录优先级：

1. `PI_JEV_LOG_DIR`
2. `$XDG_STATE_HOME/pi-jev`
3. macOS：`~/Library/Application Support/pi-jev`
4. Windows：`%LOCALAPPDATA%/pi-jev`
5. 兜底：`~/.local/state/pi-jev`

目录权限 `0700`，文件权限 `0600`。日志包含结构化答案、置信度、耗时、用量、实际生效动作、脱敏后的错误原因，以及 Outcome 决策的 `correctionPhase`。不包含原始 prompt、原始工具参数、provider 响应体或密钥。

## 安全边界

本包是工作流控制，不是操作系统级沙箱。它与 pi 运行在同一进程、拥有相同权限。不可信或无人值守的执行请使用容器化或其他系统安全边界。

## 开发

```bash
npm install
npm run typecheck
npm test
```

测试覆盖纯决策逻辑、扩展接线、并发权限确认、分支恢复、日志权限，以及基于 pi 公开 API 和 faux provider 的真实 agent 生命周期测试。生命周期回归验证：settle 预览可能报告 `canContinue=false`，但插件返回的 `custom_message + continue` 在 pi 重算最终上下文后仍会启动下一个 provider turn。

### 验收测试

单元测试之外还有自动化验收 harness 做端到端验证：真实的 pi、真实的 agent 模型，JEV 决策指向本地确定性 mock——可按决策类型编排任意答案（`ask`、顺序 `retry→finish`、预算耗尽）或注入故障（5xx、超时、垃圾响应）。每个用例留档四份证据（模型输出、决策日志、mock 实收请求）并生成 `RESULTS.md` 汇总：

```bash
./scripts/run-acceptance.sh    # 22 个用例；需要 .env.local 与 PATH 中的 pi
```

交互 TUI 行为（确认弹窗、状态栏、会话恢复）由 [docs/acceptance-tests.md](docs/acceptance-tests.md) 中的人工截图协议覆盖，其中还给出了与 [scripts/smoke.md](scripts/smoke.md) 清单的逐项映射。

## 文档

- [docs/design.md](docs/design.md) — v3 设计规范（实现的唯一依据）
- [docs/acceptance-tests.md](docs/acceptance-tests.md) — 验收测试协议（自动化 H 系列 + 人工截图 S 系列）
- [scripts/smoke.md](scripts/smoke.md) — 原始人工冒烟清单

## 许可证

MIT
