# pi-jev 设计方案 v3

状态：已批准并实现  
基准：pi 0.87.1  
日期：2026-10-08

## 1. 目标与边界

`pi-jev` 是一个可独立安装的 pi package，在 pi 的扩展事件上实现四个决策点：

1. `plan_strategy`：当前任务是否需要只读规划阶段；
2. `tier`：当前任务应使用哪个模型档位；
3. `permission.action`：当前工具调用应放行、询问还是阻止；
4. `outcome`：当前交付物应结束、纠正还是重新规划。

插件不修改 pi 源码，不启动守护进程，也不建立 IPC。TypeScript 源码由 pi 直接加载。运行时只使用 Node 标准能力和 pi 注入的 `@earendil-works/pi-ai`、`@earendil-works/pi-coding-agent`；二者必须以 `"*"` 声明为 peer dependency，开发依赖固定到实际验证版本。

项目结构：

```text
pi-jev/
├── extensions/jev.ts       # 扩展入口与生命周期编排
├── src/
│   ├── client.ts           # JEV HTTP 客户端与响应校验
│   ├── config.ts           # 环境变量、开关与日志路径
│   ├── evidence.ts         # 证据构造、截断和脱敏
│   ├── log.ts              # 每会话 JSONL 日志
│   ├── outcome.ts          # 计划/执行结果校验与纠正预算
│   ├── permission.ts       # 工具权限裁决
│   ├── questions.ts        # 四类 JEV 问题
│   ├── route.ts            # 规划策略与模型路由
│   └── state.ts            # 可持久化任务状态
├── test/                   # 单元、扩展和公开 API 生命周期测试
├── scripts/smoke.md        # 人工验收清单
└── docs/design.md          # 本设计
```

## 2. 决策点与 pi 生命周期

| 决策 | pi 事件 | 行为 |
|---|---|---|
| `plan_strategy` + `tier` | `before_agent_start` | 一次 JEV 请求同时回答两题；设置任务阶段、工具集和候选模型 |
| `permission.action` | `tool_call` | 阶段门、确定性规则、JEV 裁决、人工兜底 |
| `outcome` | `agent_before_settle` | 校验计划或最终交付物；结束或注入纠正并续跑 |
| 分支恢复 | `session_start`、`session_tree` | 从当前活动分支恢复状态和工具集，清空任务批准缓存 |

核心生命周期事实：

- 正常 run 到达 `agent_before_settle` 时，最后一条上下文通常是 assistant，因此事件预览中的 `context.canContinue` 通常为 `false`。它描述的是 handler 草案提交前的上下文，不能用于提前否决续跑。
- 需要续跑时，handler 必须在同一个返回值里提交一个改变尾随角色的 `custom_message` 草案和 `continue: true`。pi 提交草案后会重算最终上下文，再做 continuation 校验。
- settle handler 不提前 `appendEntry`，状态 `custom` 条目和续跑 `custom_message` 必须原子地放进 boundary `entries`。这样后续 handler 替换草案或 pi 拒绝续跑时，不会先扣预算、先推进阶段。
- `before_agent_start` 的返回类型没有通用 `entries`，因此该阶段的路由状态仍通过 `pi.appendEntry` 持久化。这是上一条规则的唯一正常例外。
- outcome continuation 不重新触发 `before_agent_start`。steering/follow-up 消息也继承当前任务路由，不重新判定 tier 或 plan strategy。
- settle 路径不使用 `pi.sendMessage` 作为 continuation 兜底。

## 3. 状态机与持久化

完整状态 schema：

```ts
interface JevState {
  schemaVersion: 1
  taskId: string
  originalPrompt: string
  phase: "direct" | "planning" | "executing" | "plan_only"
  tier: "small" | "normal" | "strong"
  correctionsUsed: number
  toolsBeforePlan?: string[]
  lastSetModelId?: string
}
```

- `toolsBeforePlan` 用于恢复只读阶段前的工具集，也必须随状态持久化以支持 resume。
- `lastSetModelId` 用于区分插件路由和用户手动换模。
- 恢复器接受缺少 `schemaVersion` 的 v2 兼容条目，但拒绝未知版本和字段类型错误。
- `correctionsUsed` 是 planning 与 executing 共享的任务级预算，默认和硬上限均为 2。

状态流：

```text
before_agent_start
  ├─ direct ───────────────────────────────┐
  ├─ plan_only（只读）─────────────────────┤
  └─ planning（只读）                      │
         │ agent_before_settle             │
         ▼                                 │
       Outcome Judge 校验计划              │
         ├─ retry/replan → 纠正规划 ───────┤（共享预算）
         ├─ finish → executing + 续跑      │
         └─ 失败/低置信/预算耗尽 → 结束     │
                                           ▼
                                  Outcome Judge 校验交付物
                                    ├─ finish → 结束
                                    ├─ retry/replan → 纠正并续跑
                                    └─ 预算耗尽 → 附说明结束
```

`plan_then_execute` 的计划校验会额外产生一次 JEV Outcome 请求。计划修订和执行纠正共用同一个预算。只有明确且高置信的 `finish` 才能从 planning 进入 executing；请求失败、答案不完整、低置信或上下文过大都停止自动执行。

`plan_only` 也运行 Outcome Judge，但永远不会转换为 executing。

abort、error、配置不可用等不能安全判定的 settle 路径恢复工具集、写入空状态并自然结束。

## 4. 路由：plan strategy 与 tier

### 4.1 一次请求回答两题

`before_agent_start` 把原始任务 prompt 截断和脱敏后发给 JEV：

- `plan_strategy`：`direct`、`plan_only`、`plan_then_execute`；
- `tier`：`small`、`normal`、`strong`。

低于 `PI_JEV_ROUTE_CONFIDENCE` 时：

- plan strategy 降为 `direct`；
- tier 保持当前模型；
- 降级原因写入决策日志。

请求失败也按相同规则安全降级。路由是优化项，不是安全边界。

### 4.2 模型切换保护

模型配置使用 `provider:modelId`，只在第一个冒号处分割。路由切换前依次检查：

1. 目标存在于当前可用模型目录；
2. 目标属于 `scopedModels`（若 pi 启用了模型范围）；
3. 当前上下文 token 数不超过候选模型的 `contextWindow`；
4. 当前分支含图片时，候选模型的 `input` 包含 `image`；
5. 用户没有在插件上次设置模型后手动接管；
6. `pi.setModel` 成功，即 provider 鉴权可用。

任一保护失败都保持当前模型并最多提示一次，不中断任务。

### 4.3 只读计划阶段

进入 `planning` 或 `plan_only` 时：

- 保存当前工具列表到 `toolsBeforePlan`；
- 仅保留内置读取工具和受 allowlist 约束的 shell；
- 移除 edit、write 和默认未知的 custom/MCP 工具；
- 注入不可见的只读阶段指令；
- `tool_call` 的 phase gate 再做一次强制校验，防止工具集过滤遗漏。

计划被 Outcome Judge 接受后先恢复完整工具集，再通过 boundary entries 写入 executing 状态和不可见执行消息。

## 5. Outcome Judge 与 continuation

Outcome 请求包含三题：

- `action`：`finish`、`retry`、`replan`；
- `issue`：未完成、测试失败、缺少验证、方法错误、上下文不足等；
- `quality`：质量分数。

证据以原始任务为基准，包括当前阶段、尾部工具证据、该任务已批准操作摘要，以及用户显式启用时的 `git diff --stat`。

纠正规则：

- `finish`：清空任务状态并自然 settle；
- `retry` / `replan` 且有预算：返回 `custom` 状态草案、可见的 `custom_message` 纠正草案和 `continue: true`；
- 预算耗尽：返回空状态草案和可见说明，不续跑；
- planning 的 `finish`：恢复工具，返回 executing 状态草案、不可见执行消息和 `continue: true`；
- planning 的非明确成功：恢复工具、清空状态并说明未开始执行。

`settle` 使用结构化原因 `finish | low_confidence | unavailable | invalid_answers | context_too_large`，不得用面向用户的字符串作为状态判断。planning 根据原因分别说明低置信、服务不可用、答案无效或上下文过大；这些情况都不自动执行计划。

每个 outcome 日志记录 `correctionPhase: "planning" | "executing"`。`direct` 和 `plan_only` 记为 executing 类交付阶段，以保持日志字段只有这两个批准值。

## 6. 权限裁决

### 6.1 分层策略

每个 `tool_call` 按以下顺序裁决：

1. **阶段门**：planning/plan_only 仅允许内置读工具和 allowlist 中的只读 shell；其他调用直接阻止。
2. **确定性规则**：高危 shell 模式直接阻止；内置读工具直接放行；命中当前任务批准缓存则放行。
3. **JEV 裁决**：干净且已脱敏的工具输入可发送给 JEV。
4. **人工/fail-closed**：`ask`、低置信、敏感输入或远端失败时，有 UI 则询问用户；无 UI 则阻止。

配置缺失或无效不会关闭权限层。此时路由使用 direct/当前模型、outcome 自然结束，但工具权限仍执行确定性规则并在需要时人工确认；无 UI 时 fail-closed。

整个权限 handler 还有最终异常边界。序列化、UI 或其他未预期异常统一转换为脱敏的 fail-closed block，并以 `permission_handler_error`（abort 时为 `permission_aborted`）写入日志；原始异常文本不进入模型上下文或用户提示。

### 6.2 并发与 terminate

pi 可能并发执行同一 assistant message 内的多个工具调用。插件只串行化权限判定和 UI 询问，避免多个确认框及缓存竞争；批准后的工具执行仍由 pi 并行调度。

`terminate: true` 不是对整个并发批次的绝对阻止保证。安全性必须来自每个调用自身的 block 结果，不能依赖 terminate 停止兄弟调用。

### 6.3 任务级批准

v3 只提供四个用户选择：

- `allow_once`；
- `allow_exact_for_task`；
- `allow_tool_for_task`；
- `block`。

缓存不持久化，且在每次 `before_agent_start` 和 `session_tree` 清空。敏感输入只允许一次或阻止，不提供缓存选项。

## 7. 分支、resume 与用户接管

`session_start` 和 `session_tree` 必须从 `ctx.sessionManager.getBranch()` 找当前活动分支最后一个 `jev-state` 条目，不能使用全会话 `getEntries()`。

恢复后：

- planning/plan_only 重建只读工具集；
- executing 恢复完整工具集快照；
- 空状态恢复先前工具并清除当前任务；
- 同时恢复 corrections budget 和 `lastSetModelId`；
- 清空全部任务批准缓存。

当前模型与插件记录的上次设置不一致时，视为用户手动接管；当前任务不再强制 tier 切换。

## 8. 数据披露与脱敏

### 8.1 外发规则

| 数据 | 是否发送给 JEV | 处理 |
|---|---|---|
| 任务 prompt | 是 | 截断并擦除配置 secret 字面量 |
| 普通工具名和输入 | 是 | 敏感键脱敏，长度受限 |
| 干净 shell 命令 | 是 | 脱敏后用于权限语义判断 |
| 命中凭据模式的命令 | 否 | 仅本地人工确认或 fail-closed |
| 敏感文件工具证据 | 否 | 整条替换为省略标记 |
| 尾部工具结果 | 是 | 最多 10 条，截断和脱敏 |
| `git diff --stat` | 仅显式启用 | 默认关闭，不发送 diff 内容 |
| API key | 否 | 只进入 Authorization header；正文和日志擦除 |

敏感命令检测从窄规则开始：`Authorization`/`Bearer`、常见 `KEY=value` 凭据上下文、私钥头、已知配置 secret 字面量。不得用无上下文的广义高熵规则，以免把 git SHA、哈希常量或普通 base64 误判为凭据。

证据保护：

1. 路径/参数命中 `.env`、credentials、私钥等模式时整条工具证据省略；
2. key 命中 api key、authorization、password、secret、access token 等模式时值替换为 `[redacted]`；
3. 配置 secret 字面量在序列化文本中再次全文擦除；
4. 序列化后超过 96,000 字符时拒绝该次判断，不静默截断到上限；
5. JEV 错误只保留枚举原因，不记录响应体、URL 或 provider 文本。

## 9. 开关与降级模型

只有明确关闭才完全旁路所有四个决策点：

- `--no-jev`；
- `PI_JEV_ENABLED=false`；
- `/jev off`。

`/jev on` 只能打开会话开关，不能覆盖启动级 `--no-jev` 或环境变量关闭。

远端配置缺失、无效、超时或服务错误采用分层降级：

- route：`direct`，保持当前模型；
- outcome：不自动纠正或执行计划，自然结束；
- permission：保留 phase gate 和确定性规则，需要远端判断时转人工，无 UI 则阻止；
- 日志：仅在配置能够确定安全日志目录时写入，写入失败不影响 agent loop。

这一区分保证“优化功能可降级，权限边界不因配置错误而打开”。

## 10. JEV 客户端

客户端使用原生 `fetch` 和显式 type guard：

- 校验每个问题都有匹配类型的答案；
- choice 必须属于 criteria；score 必须是合法整数；
- confidence 必须在 `[0,1]`；概率键必须完全匹配且总和误差不超过 0.02；
- 禁止 HTTP redirect，避免 Authorization header 泄漏；
- 429、500、502、503、524、529 和一次瞬态 transport failure 最多重试一次；
- 两次尝试共享同一个总超时，不把最坏等待翻倍；
- auth/其他 4xx 不重试；
- pi 的 abort signal 与超时 signal 合并，显式取消不重试。

## 11. 日志

日志目录优先级：

1. `PI_JEV_LOG_DIR`；
2. `$XDG_STATE_HOME/pi-jev`；
3. macOS：`~/Library/Application Support/pi-jev`；
4. Windows：`%LOCALAPPDATA%/pi-jev`；
5. 其他/回退：`~/.local/state/pi-jev`。

目录权限强制为 `0700`，日志文件强制为 `0600`。每个 session 使用独立文件：

```text
decisions-<sanitized-session-id>.jsonl
```

日志只记录决策种类、task id、问题、结构化答案/置信度、延迟、usage、最终生效动作、可选错误枚举和 outcome 的 `correctionPhase`。不记录原始 prompt、原始工具参数、响应体或密钥。

## 12. 配置

| 变量 | 默认值 | 说明 |
|---|---:|---|
| `PI_JEV_ENABLED` | `true` | `false` 明确关闭 |
| `PI_JEV_URL` | `https://openrouter.ai/api/alpha/decisions` | JEV endpoint |
| `PI_JEV_API_KEY` | 必填 | 决策 API 凭据 |
| `PI_JEV_MODEL` | `typesafe/jev-1.13` | 决策模型 |
| `PI_JEV_TIMEOUT_MS` | `3000` | 整次请求总超时，1–60000 ms |
| `PI_JEV_MODEL_SMALL` | 必填 | `provider:modelId` |
| `PI_JEV_MODEL_NORMAL` | 必填 | `provider:modelId` |
| `PI_JEV_MODEL_STRONG` | 必填 | `provider:modelId` |
| `PI_JEV_ROUTE_CONFIDENCE` | `0.8` | route/tier 置信阈值 |
| `PI_JEV_PERMISSION_CONFIDENCE` | `0.9` | permission 置信阈值 |
| `PI_JEV_OUTCOME_CONFIDENCE` | `0.8` | outcome 置信阈值 |
| `PI_JEV_MAX_CORRECTIONS` | `2` | 任务共享预算，硬上限 2 |
| `PI_JEV_OUTCOME_GIT_DIFF` | `false` | 是否发送 `git diff --stat` |
| `PI_JEV_PLAN_AUTO_EXECUTE` | `true` | 计划通过后是否自动执行 |
| `PI_JEV_LOG_DIR` | 平台状态目录 | 覆盖日志目录 |

命令与界面：

- `pi --no-jev`：本次启动明确关闭；
- `/jev`：显示状态、阶段、tier、预算、决策数和日志文件；
- `/jev off`、`/jev on`：修改当前会话开关；
- 状态栏显示 ready、degraded、off 或活动任务阶段。

## 13. 测试策略

### 13.1 纯单元测试

覆盖响应 schema、路由降级、模型窗口/图片保护、状态恢复、证据脱敏、敏感命令检测、权限规则、预算、客户端超时/abort/重试和日志权限。

已有手工传入 hook 上下文的单元测试保留用于局部语义，不把其中人为构造的 `canContinue` 当作真实边界证明。

### 13.2 扩展 wiring 测试

用测试 ExtensionAPI 注册并触发事件，验证：

- 配置无效时权限仍 fail-closed；
- permission UI 串行；
- task approval 在新任务和 `session_tree` 清空；
- 活动分支状态恢复；
- planning outcome 的纠正、接受和不执行路径；
- steering/follow-up 不重走 route。

### 13.3 公开 API 生命周期测试

使用包根公开的 `createAgentSession`、`DefaultResourceLoader`、`SessionManager` 和 faux provider 运行真实 agent loop。关键回归用例必须验证：正常 assistant 结尾的 boundary preview 即使 `canContinue=false`，插件返回 `custom_message + continue` 后 pi 仍会重算并开始第二个 provider turn。

完成标准：类型检查通过；全部测试通过；真实生命周期测试能在修复前复现续跑缺陷、修复后通过；人工 smoke 覆盖四个决策点、分支、降级、并行确认和敏感命令不外发。

## 14. 非目标与安全边界

- 插件不是操作系统沙箱，也不能降低 pi 进程自身权限；不可信执行仍需容器或其他系统级隔离。
- v3 不提供 session 级或跨分支批准缓存。
- v3 不保证仅靠 `terminate` 中止同批并发工具。
- v3 不对配置损坏时的 route/outcome 可用性作强保证，但权限不得 fail-open。
- v3 不自动上传完整 diff、文件内容、凭据命令或原始决策响应。
