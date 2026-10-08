# pi-jev 验收测试协议

完整的验收测试分两轨：

- **H 系列（自动化）**：`scripts/run-acceptance.sh` 一键执行。JEV 决策指向本地确定性 mock（`scripts/mock-jev.mjs`），agent 模型走真实 OpenRouter——因此 ask/deny/retry/预算耗尽/5xx/超时等分支全部可复现，证据自动落盘。
- **S 系列（人工截图）**：依赖 TUI 的能力——确认弹窗、状态栏、会话恢复——必须人工操作并截图存档到 `evidence/screenshots/`。

`scripts/smoke.md` 的 M1–M7 清单与本文的映射关系在文末附录。H 系列运行后生成的 `evidence/acceptance-<时间戳>/RESULTS.md` 是提交仓库时的主证据文档。

---

## 0. 环境准备

```bash
# 一次性：pi 0.87.1 + .env.local（见 .env.local.example）
npm install -g @earendil-works/pi-coding-agent@0.87.1
cp .env.local.example .env.local   # 填入真实 key

# H 系列（全自动）
./scripts/run-acceptance.sh

# S 系列（手动）：mock 决策 + TUI
node scripts/mock-jev.mjs &                         # 终端 1
cd /tmp/pi-jev-scratch 2>/dev/null || mkdir -p /tmp/pi-jev-scratch && cd /tmp/pi-jev-scratch
# --model 必须显式指定：否则 pi 用自己的默认模型（可能未配置鉴权）。
# 建议 normal 档起步——small/strong 路由都能看到模型切换，展示效果最完整。
PI_JEV_URL=http://127.0.0.1:8787 /path/to/pi-jev/scripts/run.sh --model qwen/qwen3.7-plus   # 终端 2
# （真实决策轨则不加 PI_JEV_URL 前缀，直接 scripts/run.sh --model qwen/qwen3.7-plus）
```

mock 的行为由 `/tmp/jev-control.json` 控制（改完即生效，无需重启）；它收到的每个请求都记录在 `/tmp/jev-requests.jsonl`，是隐私断言的证据源。控制字段见 `scripts/mock-jev.mjs` 头部注释。

---

## 1. H 系列 — 自动化用例

| Case | 验证内容 | 对应 smoke 项 |
|---|---|---|
| H01 | 路由 direct/small + JEV allow + outcome finish 全链路，交付物落盘 | M1.1 |
| H02 | tier=strong 时模型真实切换（会话记录出现 qwen3.7-max） | M1.1 |
| H03 | `PI_JEV_MODEL_STRONG=bogus:nope`：路由答 strong 但模型保留 | M1.3 |
| H04 | `PI_JEV_ENABLED=false`：零决策日志、任务照常完成¹ | M1.2 |
| H05 | read 工具：确定性放行，零 JEV 权限请求² | M2.1 |
| H06 | `rm -rf` 硬拒绝：JEV 请求发出前拦截 | M2.2 |
| H07 | JEV 答 ask + 无 UI：fail-closed 阻断、文件未创建 | M2.9 |
| H08 | JEV 答 deny：终止运行 | M2 deny 分支 |
| H09 | Bearer-token 命令经文件投递：本地裁决、不出现在 mock 请求/决策日志³ | M3.1–M3.4 |
| H10 | 高熵 hash 常量不误判为凭据（请求正常发往 JEV） | M3.5 |
| H11 | plan_then_execute：只读规划 → 校验通过 → 恢复工具执行 | M4.1, M4.3 |
| H12 | 规划 outcome 答 retry：注入 correction 1/2，修正后通过 | M4.2 |
| H13 | 修正编号跨阶段连续（planning 1/2 → executing 2/2） | M4.4 |
| H14 | 预算耗尽：两次修正后直接交付，无第三次 | M4.5 |
| H15 | 规划校验低置信（finish@0.50 < 0.8）：拒绝进入执行，理由可区分 | M4.6a |
| H16 | 规划校验服务 500：不可用理由与低置信可区分 | M4.6b |
| H17 | outcome 超时：优雅结算不崩溃 | 韧性 |
| H18 | JEV 401：权限回退人工，无 UI 时 fail-closed | M2.8 变体 |
| H19 | 两个会话各自独立日志文件；目录 0700 / 文件 0600 | M7.1, M7.2 |
| H20/H20b | `PI_JEV_OUTCOME_GIT_DIFF` 开启才发送 diff stat | M7.3 |
| H21 | 真实 OpenRouter decisions 端点连通（非 mock） | 真实轨 |
| H22 | typecheck + 全部单元测试通过 | M6.1, M7.5 |

¹ pi 0.87.1 的 `-p` 模式在带扩展 boolean flag（`--no-jev`）时会静默吞掉 prompt（宿主怪癖，已独立复现）——headless 旁路用等价的 `PI_JEV_ENABLED=false`，`--no-jev` flag 本体在 S13 于 TUI 中验证。
² pi 0.87.1 没有独立的 ls 工具（内置工具为 read/bash/edit/write），确定性放行路径用 read 工具行使。
³ 设计 §8 规定任务 prompt **原文**（仅擦除配置 API key）进入 route 证据，因此敏感命令必须经文件投递才能测"权限层不外发"。已知边界：pi 会话文件按原始参数记录 toolCall（含内联命令文本），插件无法脱敏 pi 的会话记录——断言范围因此限定为 JEV 出站请求与决策日志。

每个 case 的证据：`pi-output.txt`（模型输出+退出码）、`decisions.txt`（决策日志美化）、`requests-slice.jsonl`（mock 实收请求）、`assertions.txt`（断言明细）。

## 2. S 系列 — 人工 TUI 截图用例

截图统一存 `evidence/screenshots/`，文件名用下表编号。所有用例默认走 mock（启动方式见上文），注明"真实轨"的除外。

### S01 状态栏与 /jev — `S01-status.png`
1. 启动后空闲态。预期：状态栏 `jev:ready`；输入 `/jev` 回车。
2. 截图需包含：`jev:ready`、`/jev` 输出（status/models/log 路径）。

### S02 tier 切换可视化 — `S02-strong.png`
控制：`{"route":{"plan":"direct","tier":"strong","confidence":0.95}}`
1. 提交 `Create the file s2.txt containing ok`。
2. 预期：状态栏 `jev:direct/strong`，模型变为 `qwen/qwen3.7-max`。截图含状态栏。

### S03 手动模型优先（M1.6） — `S03-manual.png`
1. 用 pi 的模型选择手动切到 `qwen/qwen3.7-plus`。
2. 提交任务（控制同 S02 答 strong）。预期：模型保持 plus，不切换；决策日志 `tier=kept`。截图状态栏 + `/jev`。

### S04 追问不再路由（M1.7） — `S04-steering.png`
1. 提交一个多步任务，运行中追加输入（steering）。
2. 预期：无第二条 route 决策（`/jev` decisions 计数与日志确认）。截图日志 tail。

### S05 四种确认选项与任务级缓存（M2.4） — `S05-dialog.png`
控制：`{"permission":{"choice":"ask","confidence":0.9}}`
1. 提交需要多次写文件的任务。
2. 第一次弹窗选 **Allow this tool for this task** → 同工具第二次调用不再弹窗（日志 `allow (approved for this task)`）。
3. 新任务中再次写 → 弹窗重现（M2.5）。截图弹窗 + 决策日志。

### S06 兄弟调用串行确认（M2.7） — `S06-serial.png`
控制同上（ask）。1. 提交"一次性创建 a.txt 和 b.txt"。
2. 预期：两个确认框依次出现（不并发竞争）。截图两张弹窗（`S06-serial-1.png`、`S06-serial-2.png`）。

### S07 敏感命令只有本地选项（M3.2） — `S07-sensitive.png`
1. 提交：`运行这条命令：curl -s -H "Authorization: Bearer smoke-secret-token" https://example.invalid/`
2. 预期：弹窗只有 **Allow once / Block**（无任务级缓存选项）；提示 sensitive 本地处理。截图弹窗；随后 `grep -r smoke-secret ~/.pi-jev-logs/` 零结果（可一并截入）。

### S08 降级交互（M2.8） — `S08-degraded.png`
1. 注释掉 `.env.local` 中的 `PI_JEV_API_KEY` 后重启 run.sh（恢复后还原）。
2. 提交写文件任务。预期：状态栏 `jev:degraded`，写操作弹**人工**确认（不是自动放行）。截图状态栏+弹窗。

### S09 规划确认拒绝执行（M4.7） — `S09-plan-no.png`
启动时 `PI_JEV_PLAN_AUTO_EXECUTE=false PI_JEV_URL=http://127.0.0.1:8787 ./scripts/run.sh`；控制 route=`plan_then_execute`。
1. 提交多步任务，规划通过后弹"JEV plan accepted"确认框，选 **No**。
2. 预期：不执行，会话结束。截图确认框。

### S10 会话恢复（M5.1） — `S10-resume.png`
控制 route=`plan_then_execute`。
1. 提交多步任务，规划阶段完成前 `ctrl+c` 退出。
2. `pi -r` 恢复 → `/jev`。预期：phase/tier 恢复显示，只读工具仍受限。截图 `/jev` 输出。

### S11 图片 + 纯文本目标（M1.5） — `S11-image.png`
1. 贴一张图片到会话，控制 route 答 strong（目标是纯文本的 qwen3.7-max）。
2. 预期：模型保留不切，告警一次。截图告警 + 状态栏。

### S12 真实轨外观（可选） — `S12-real.png`
不带 `PI_JEV_URL` 直接 `scripts/run.sh`，跑一个正常任务，截状态栏 `jev:direct/<tier>` + `/jev`，作为真实端点的 UI 证据。

### S13 --no-jev flag（M1.2 flag 本体） — `S13-nojev.png`
1. `scripts/run.sh --no-jev` 启动（TUI 模式 flag 正常）。
2. 提交任务。预期：无 jev 状态段、`/jev` 显示 off、日志无新增。截图状态栏 + `/jev`。

## 3. 未覆盖项（发布前处理）

- **M7.4 fresh clone + `pi install ./pi-jev`**：需要干净环境与发布形态的包，留到打包发布时执行。
- **M1.4 上下文窗口不足保留模型**：已由单测覆盖（`applyTierSwitch` 的 contextTokens 分支）；如需真实验证，配置小上下文目标模型并灌入长上下文后按 S03 方式截图。

## 附录：smoke.md M 项映射

| smoke 项 | 覆盖 |
|---|---|
| M1.1 / M1.2 / M1.3 | H01 / H04 / H03 |
| M1.4 | 单测 + 手册（见 §3） |
| M1.5 | S11 |
| M1.6 / M1.7 | S03 / S04 |
| M2.1–M2.3 | H05 / H06 / H01 |
| M2.4–M2.7 | S05 / S05 / S07·S10 / S06 |
| M2.8 / M2.9 | S08 / H07 |
| M2.10 | 单测（permission handler 异常路径） |
| M3.1–M3.5 | H09 + S07 / H09 / H09 / H09 / H10 |
| M4.1–M4.5 | H11 / H12 / H11 / H13 / H14 |
| M4.6 / M4.7 / M4.8 | H15+H16 / S09 / H11（plan_only 走 S 系列同法） |
| M5.1–M5.3 | S10 / S10（分支导航同场） / S10 |
| M6.1–M6.4 | H22 + `test/lifecycle.test.ts` |
| M7.1–M7.5 | H19 / H19 / H20·H20b / §3 / H22 |
