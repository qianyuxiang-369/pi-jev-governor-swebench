# eval/swebench — Pi SWE-bench Runner（P4 完成；P5–P13 修复、加固与链路 smoke 完成）

将 SWE-bench Verified 的「解题者」从 mini-swe-agent 替换为 Pi（可选挂 `pi-jev-governor` 扩展），
判分完全复用 `test_swe/` 下原有的 SWE-bench evaluator（本地经 grade.py 适配 docker-rt，见 §5.11）。

> 设计依据：plan.md（路线 1）；P2 已端到端验证容器内 Pi + Governor 全链路（见 memory_doc §5.5）。

## 1. 实验组（同 prompt、同模型参数，唯一变量 = 是否挂 governor）

| 组 | 启动差异 | 模型 |
|---|---|---|
| A. Pi + fixed small | 无 `-e`（vanilla） | PI_JEV_MODEL_SMALL 的同一模型 |
| B. Pi + fixed normal | 无 `-e`（vanilla） | PI_JEV_MODEL_NORMAL 的同一模型 |
| C. Pi + fixed strong | 无 `-e`（vanilla） | PI_JEV_MODEL_STRONG 的同一模型 |
| D. Pi + JEV Governor | `-e <repo>/dist 或 src 入口` | 初始 PI_JEV_MODEL_NORMAL（P13 起，run.py 默认），由 governor 路由升/降档 |

最小验证集：A（任选一个固定模型）+ D。
所有组必须使用：完全相同的 prompt 文件、相同 thinking level（runner 以 `--thinking` 显式钉死，默认
medium）、相同 `--mode json --no-session`、相同 timeout、相同镜像。Vanilla 组的模型 ID 从 governor 的
SMALL/NORMAL/STRONG 配置中取值，保证可比。非 dry-run 的 governor 组要求三档互不相同（相同档位只能
测到 router 开销、测不到路由收益，runner 直接拒绝启动）。

## 2. 运行形态（两层）

### 2.1 宿主层（run.py，P4 实现）

每个 task 的流程：

1. 从 HF 缓存数据集读 instance（`princeton-nlp/SWE-bench_Verified`，已缓存于 `~/.cache/huggingface/datasets/`），取 `instance_id / repo / base_commit / problem_statement`。
2. `docker create` 对应镜像 `swebench/sweb.eval.x86_64.<instance_id>`（`-w /testbed`，`--env-file container.env`）。
   镜像内 /testbed 的 HEAD 是 "SWE-bench" commit（= base_commit + 镜像环境设置，见 memory_doc §6.16）；setup 硬校验：
   ①数据集 base_commit 是 HEAD 的祖先（`git merge-base --is-ancestor`，防镜像错配）；②worktree 干净。
   image_id / image_head 记入 metadata 与 manifest。
3. 注入资产（P2 验证过的唯一可靠通道，docker-rt 下 bind mount / docker cp 均不可用，见 memory_doc §6.12）：
   所有文件一律 `docker exec -i <c> bash -c 'cat > <容器路径>' < <宿主文件>`，写入后 md5sum 校验。
   - `inject.tar.gz`（node + pi bundle + 嵌套 node_modules + governor）
   - prompt 文件：`task.md`（instance-prompt 替换 `{{problem_statement}}`）、`append-system-prompt.md`
   - `container.env`（API key 注入；注意权限 600，不入库）
4. 容器内准备：
   - 解压 inject 资产到 `/root/agent/`（P2 已验证的目录布局）。
   - **`export BASH_ENV=/root/.bashrc`（关键）**：pi 的 bash 工具用 `/bin/bash -c`（非登录 shell，已核实
     pi 1.1.0 bundle 的 getBashShellConfig），不会自动加载 conda；`BASH_ENV` 让每条被执行的命令先
     source 镜像的 .bashrc（内含 `conda activate testbed`），与 mini-swe-agent 的验证机制一致。
   - 容器内 wrapper `/root/pi.sh`：`export BASH_ENV=/root/.bashrc; exec /root/agent/node .../cli.js "$@"`
5. 启动 Pi（命令形态与 P2 一致 + prompt 参数）：

   ```bash
   # Governor 组（D）：--models 传三档全体（scope），--model 选初始档（strong）
   /root/pi.sh -e /root/agent/governor \
     --mode json --no-session --no-approve \
     --models "qwen/qwen3.7-flash,google/gemini-flash-latest,deepseek/deepseek-flash-latest" \
     --model "deepseek/deepseek-flash-latest" \
     --append-system-prompt /root/agent/prompts/append-system-prompt.md \
     -p "$(cat /root/agent/prompts/task.md)" > /root/run.log 2>&1

   # Vanilla 组（A/B/C）：去掉 -e /root/agent/governor，--models 与 --model 都换成该档模型
   ```

   **`--models` 是 scope 语义（P5 修复的关键）**：pi 的 `--models`（复数）声明「可用模型范围」，
   `--model`（单数）只挑初始模型。governor 的档位切换被 `scopedModels` 门控——目标档模型不在
   scope 内会被 `not_scoped` 静默拒绝（一次警告后保持当前模型，见 src/route.ts 与 memory_doc §6.17）。所以
   单模型 scope 会让 governor 完全无法切换，**governor 组必须把三档全体（去重）传给 `--models`**；
   vanilla 组保持 `--models=--model=<该档模型>` 的单模型基线。（当前 `.env.local` 三档相同、scope
   去重成单模型；pilot 前须配置三个不同档位，否则测不到路由收益。）

   `--no-approve` 说明（P4 核实）：它是 **project trust** 决策（是否加载 /testbed 的项目级资源），
   不是工具审批。headless 下无 flag 时默认 `ask` → 静默跳过；显式 `--no-approve` 把行为钉死。
   /testbed 无 `.pi/`、`.agents/skills`、`AGENTS.md` 等资源，默认与实际行为等价。
   pi 核心不逐工具审批（安全边界靠容器），vanilla 组 headless 下工具直接执行。

6. 结束后收集 patch（P5 修复后的命令）：
   - 容器内 `git add -A` 后 `git diff --cached --binary --full-index <image_head>`：
     - **基线必须是镜像原始 HEAD（image_head），不是数据集 base_commit**——官方镜像 HEAD 含自身
       环境设置变更（本机实测 1862 个 mode 变更 + 1 行 pyproject 改动）。判分链路源码实证（swebench
       5.0.2，见 memory_doc §6.20）：evaluator 从同一镜像新建容器后**直接在镜像 HEAD 的工作树上
       `git apply`**，没有 checkout base_commit 这一步（重试间以 `git checkout -- . ; git clean -fd`
       复位）。因此 diff base_commit 会把镜像自带变更污染进 patch，必须用 image_head 固定 sha 作基线
       （顺带防 agent 违约 commit 导致 `diff HEAD` 变空，memory_doc §6.16）。
     - `--binary --full-index`：二进制与完整 blob hash 入 patch，`git apply --check` 兼容性已实测。
   - 运行时垃圾不入 patch：`.git/info/exclude` 追加 `__pycache__/ *.pyc .pytest_cache/ .mypy_cache/ .ruff_cache/`。
   - patch > 5MB 打 `patch_size_warn`；落盘 `patch.diff` 经 exec stdout 回收；空 patch 也照常记录。
7. 写官方 predictions 格式（本机判分环境 swebench 5.0.2 已核实）：
   `{"instance_id": ..., "model_name_or_path": "<run-id>-<variant>", "model_patch": "<diff>"}`，jsonl 逐行或 json 列表皆可。
   **P7 起 preds.jsonl 不逐行 append**：实例目录的 metadata.json + patch.diff（均原子写：tmp + os.replace）是唯一
   事实源，preds 由 `rebuild_preds` 从实例目录全量原子重建（幂等）——run 结束 / resume 启动 / finalize 三处调用；
   崩溃或中途被杀只会「少最后一行」，下次重建自动补齐，不会出现半行/重复行。
8. `docker rm -f` 清理容器；证据归档 `<output>/<run-id>/<variant>/<instance_id>/`（run.log、patch.diff、metadata.json、jev/decisions.jsonl），variant 级 manifest.json + preds.jsonl。

超时：宿主 side 用 subprocess/exec wall-clock timeout（默认 1800s）。超时后 **`docker stop -t 5` 停整个容器**
（namespace 拆除，全进程树连带死亡——含 pytest/build 子进程），再 `docker start` 恢复 writable layer 后收稳定
patch；状态记 `timeout_partial`。实测：stop 0.03s + start ~4s，总开销约 5s。

#### 2.1.1 状态机与 preds 收录策略（P6 修复：分母 = 计划集）

每个 instance 结束必落入 8 种状态之一（写入 metadata.json 的 `status` / `status_notes`）：

| status | 触发条件 | 进 preds.jsonl | 重试 |
|---|---|---|---|
| `success` | 有 agent_settled、至少一个 stop/toolUse、末次响应 stop、pi exit 0 | 是 | — |
| `timeout_partial` | 超时（停容器后收稳定 patch；空 patch 也收录并注明） | 是 | — |
| `agent_error` | 无 agent_settled / aborted / 零响应 / 末次响应 error/aborted / pi 非零退出 | 是 | — |
| `extension_bypass` | governor 组但无决策日志 / 有日志但零 route 记录（扩展被静默旁路或降级，memory_doc §6.11） | 否 | 是（≤2 次） |
| `setup_error` | 容器创建 / 资产注入 / base 校验失败 | 否 | 是（≤2 次） |
| `collection_error` | 采集类异常：patch 收集、容器生命周期（InfraError）、决策日志读取、解析错误（run.log / 决策日志）、pi 事件为 0 | 否 | 是（≤2 次） |
| `dry_run_ok` / `dry_run_probe_failed` | `--dry-run`（probe 组合判据 v3 见 §5.5，rc 收紧见 §5.6） | 否 | — |

原则（P6 修正）：**分母 = manifest 计划集**。模型自身的失败（空 patch、末次响应 error、非零退出）
本身就是结果，全部写入 preds——evaluator 对空 patch 记为 `empty_patch` 类别（而非 unresolved），
若把失败样本排除会造成幸存者偏差（只剩跑得顺的样本，resolved 率虚高）。只有基础设施状态
（setup_error / collection_error / extension_bypass）不进 preds：一次没有 governor 的 governor 运行
不是可测量的数据点；基础设施状态对称重试（最多 2 次），仍失败则 manifest `infra_failed` 记录并 exit 1。
preds 的 `model_name_or_path = <run-id>-<variant>`。

分类判据（P7 起，固定顺序，详见 §5.5；P8 补 run.log 解析错误）：**有效性门先于结果门**——扩展有效性（extension_errors / governor 零 route
记录 → `extension_bypass`）→ 采集完整性（决策日志解析/读取错误、pi 事件为 0 → `collection_error`；事件流都没有 =
采集失败，不是模型失败）→ 结果判定（timeout → 非零退出 → settled/末次响应 → success）。Docker 生命周期异常
（launch_pi 异常、collect rc≠0、stop/start 稳定性校验失败）统一抛 `InfraError`，agent 阶段发生时归
`collection_error` 并对称重试——基础设施故障不能伪装成模型失败写进 preds。run.log 出现解析错误时 success 门关闭
（截断可能吞掉末次调用与成本），timeout / 非零退出等更早的保守判定不受影响（超时截断是常态，不降级重试）。

### 2.2 判分层（复用 test_swe 流程）

```bash
export DOCKER_HOST=unix:///tmp/docker-rt.sock   # docker-rt 必设，Python docker SDK 不读 context

# 0) 判分前必须校验镜像 digest + run 结构（evaluator 直接在镜像 HEAD 树上 apply，镜像变了 = 测错树）。
#    任一命中即 exit 1：digest 漂移；同一 tag 在一次 run 内混用多个 digest（MIXED，不同树的 patch 无法整体判分）；
#    结构不完整（manifest 缺失/为空、计划实例缺 metadata、计划实例无 recorded outcome（NO-OUTCOME）、recorded outcome
#    缺 image_id 或缺 patch.diff、存在未列入计划的 metadata）；判分输入不一致（preds.jsonl 缺失/空白行/坏行/
#    缺 instance_id、model_name_or_path、model_patch（evaluator 逐行 json.loads 且直接下标读这些字段）/重复/缺行/
#    多余行/与磁盘 patch.diff 不符——evaluator 读的是 preds.jsonl，不是实例目录）
python check_images.py results/<run-id>

# 1) 判分（-i 逐个实例限定，与 preds 收录集一致；本机 swebench 5.0.2 实证 CLI）
swebench eval verified -p <variant>/preds.jsonl -i <instance_id> [...] --run-id <run-id> --report-dir <eval_dir>
```

说明（swebench 5.0.2 源码实证，memory_doc §6.20）：
- `run_instance` 从数据集镜像新建容器后**直接 `git apply`** prediction（GIT_APPLY_CMDS 链：
  `git apply --verbose` → `--3way` → `--reject`；重试间 `git checkout -- . ; git clean -fd` 复位），
  **不存在 checkout base_commit 这一步**——所以判分镜像必须与解题时同一 digest（check_images.py 校验），
  否则 agent 解题的树与判分的树不是同一棵。
- 无 prediction 的实例被 evaluator 记为 `incomplete_ids`（不是 unresolved）、空 patch 记为
  `empty_patch_ids`（单列类别）、report 缺失记为 `error_ids`——这正是「分母 = 计划集」要把全部结果写进
  preds 的原因：不写 = 从统计里消失。
- `-i/--instance-id` 可重复；限定子集后 `Total` 即为该子集（`full_dataset = load_instances(..., instance_ids)`）。

## 3. Prompt 设计（P3 产出，本目录 prompts/）

**策略：不覆盖 pi 默认 system prompt，用 `--append-system-prompt` 追加 benchmark 约束段。**
理由：pi 原生 system prompt（preamble/tools/rules 自动生成，cwd 自动注入）是「Pi 能力」的组成部分，
覆盖它会改变基线；append 只增加任务上下文（addendum 独立段），Vanilla 与 Governor 两组完全一致。
`--append-system-prompt` 接受 text 或**已存在文件的路径**（docs/cli.md L231-232 已核实），runner 直接传文件路径。

| 文件 | 用途 | 要点 |
|---|---|---|
| `prompts/append-system-prompt.md` | 追加 system 约束 | 无人值守不问问题；只改源码不改测试；不 commit（工作树是交付物）；单命令执行（对齐 governor 白名单，避免 `&&`/管道/重定向/未引用 glob 被 `bash_not_allowed` 拦截）；用仓库自带测试验证；完成即停 |
| `prompts/instance-prompt.md` | user prompt 模板 | 极简：PR description 注入 + 「实现修复、保持未提交、用测试验证」。避免过度 prompt engineering |

设计取舍：
- 第 4 条「单命令」对两组同等生效（公平），且显著降低 governor 组的无谓 block 率；vanilla 组按要求
  拆命令可能略慢，但这是两组同规则的代价，接受。
- 「不 commit」是硬约束：runner 用工作树 diff 收 patch，commit 会导致空 patch。
- 「不建测试/复现脚本文件」：避免污染 patch（evaluator 判分前会 apply 官方 test patch，测试文件被改动易冲突）。

## 4. 已核实的关键事实（P3 侦察）

- pi 1.1.0 bash 工具：spawn `/bin/bash -c`（非登录、非交互 shell），不读 `.bashrc`，除非设置 `BASH_ENV`。
- `--append-system-prompt <text|path>` 支持文件路径，可重复；内容渲染为 system prompt 的 addendum 段。
- pi 不设 `--system-prompt` 时自动生成 preamble/tools/rules/cwd 等段；`--no-context-files` 可禁 AGENTS.md 加载（P4 决策点）。
- swebench 预测文件（5.0.2 已核实）：`.json`（list 或 dict[instance_id→pred]）或 `.jsonl`；每条至少含 `instance_id`；标准字段 `model_patch`、`model_name_or_path`。
- 数据集已缓存：`~/.cache/huggingface/datasets/princeton-nlp___swe-bench_verified`。
- 现有 test_swe 命令参考：`test_swe/scripts/run.sh`（mini-extra 生成 `output/runs_output/<name>/preds.json` → `swebench eval verified`）。

## 5. 工具与完成状态（2026-10-14：P4 完成 + P5–P13 修复轮与链路 smoke）

### 5.1 使用方式

```bash
# 1) 构建容器注入资产（约 1 分钟，产物在 container-assets/，已 .gitignore）
bash eval/swebench/build-assets.sh

# 2) 干跑（零成本 probe）：容器创建/资产注入/base 校验 + 假 key 跑 pi，验证
#    扩展真实加载 + 模型 scope 解析（死在 API 认证步，不产生任何调用）
python run.py --variant governor --instance-id astropy__astropy-14182 --dry-run

# 3) 单任务真跑（默认 governor，timeout 1800s）
python run.py --variant governor --instance-id astropy__astropy-14182

# 4) 批量配对跑（AB/BA 交替，对抗 provider 负载/限流/模型版本漂移）
python run.py --variant vanilla --variant governor --sample 30 --seed 7

# 5) 正式 pilot：显式 run-id（结果在 results/<run-id>/<variant>/，重名拒绝）
python run.py --variant vanilla --variant governor --sample 30 --seed 7 --run-id pilot-01

# 6) 中断恢复：跳过已终结实例（success/timeout_partial/agent_error/dry_run_ok），基础设施状态重跑；
#    preds.jsonl 从实例 metadata 全量原子重建（自动补齐任何丢失行）；run spec 全量 config_hash 校验——
#    dataset revision pin + 三档模型/scope + thinking/timeout + prompt/资产哈希 + JEV env + runner 自身 SHA256，任一字段变化即拒绝（防混组）
python run.py --variant vanilla --variant governor --run-id pilot-01 --resume
```

要点：
- 结果目录 `<output>/<run-id>/<variant>/`：variant 间物理隔离；**同名 run dir 非空且无 `--resume` 即拒绝**（防混组/重复 prediction）。
- **不可变 run spec（P7；P8 补 runner 哈希）**：启动时冻结 dataset revision + fingerprint、模型/tier、thinking/timeout、prompt/资产 SHA、JEV env（剔除 API key）与 runner 自身 SHA256 为 16 键 run spec，`config_hash`（sha256 64 位）写入 manifest；`--resume` 全量比对，差异列 `changed field(s)` 拒绝。eval/ 在 git 中 untracked，改 run.py 后 resume 会被 runner 哈希拒绝（防混两套执行逻辑）；resume 重写 manifest 时首次运行的 git 状态保留在 `original_governor_git`。
- **revision 钉死（P7）**：新建 run 无法 pin dataset revision 时默认拒绝启动（防数据集漂移）；`--allow-unpinned-dataset` 显式豁免（逃生阀）；`--resume` 一律用旧 manifest 的 pinned revision 加载。
- `--sample N --seed S`：固定随机抽样（同 seed 可复现）；`--limit N` 取数据集前 N 条，仅供 smoke、不代表分布。
- `--run-id` 仅允许 `[A-Za-z0-9._-]`（目录逃逸防护）。
- `--thinking {off,minimal,low,medium,high,xhigh,max}` 默认 medium（pi 自身默认也是 medium；显式传入保证批次间固定）。
- **退出码**：非 dry-run 存在基础设施失败、recorded outcome 缺 `patch.diff`（缺失 patch 是结构异常，非合法空 patch；行仍写入以保分母，manifest `missing_patch_files` 记录）或零 recorded outcomes → exit 1；dry-run 存在非 `dry_run_ok` → exit 1。CI/脚本按此判定批次是否可用。
- `.env.local` 仅白名单键进入容器：`PI_JEV_*` 前缀 + `OPENROUTER_API_KEY`；其余键记入 manifest 后丢弃。
- python 解释器用 `/workspace/.conda/bin/python`（有 datasets 4.8.5）。

### 5.2 完成项（含零成本验证证据）

- [x] **conda 激活验证**——astropy-14182 容器实测：无 `BASH_ENV` → `No module named pytest`（base）；`BASH_ENV=/root/.bashrc` → `pytest 7.4.0`（testbed）。
- [x] **patch 收集**：`git add -A` + `git diff --cached`（P5 修复轮升级为固定基线 + binary/full-index + exclude，见 §2.1 步骤 6；是否过滤测试路径留 pilot 统计决定）。
- [x] **容错路径**：空 patch / 超时（P5 起改为停容器收稳定 patch，见 §2.1）/ 异常 → 写 error 元数据 + 状态机分类，整批继续。
- [x] **证据归档**：`results/<run-id>/<variant>/<instance_id>/{patch.diff, run.log, metadata.json, jev/decisions.jsonl}` + variant 级 `manifest.json` + `preds.jsonl`（P5 起 variant 物理隔离）。
- [x] **`--approve` 语义核实**（2026-10-14）：它是 project trust（加载 /testbed 项目级资源）而非工具审批；headless 默认 ask→skip；run.py 显式 `--no-approve` 钉死；/testbed 无 `.pi/`/`.agents`/`AGENTS.md` 资源，两组行为一致。pi 核心不逐工具审批，vanilla headless 工具直接执行。
- [x] **container.env**：临时目录生成、chmod 600、用后即删、不入库。
- [x] **metadata 指标**：model_calls / cost_usd / tokens / decisions（route/permission/outcome + allow/block）。
- [x] **零成本验证**：dry-run ×2（8.7s 全链路通过：node v22.21.0 + pi 1.1.0 容器内可运行）+ keep-container 深度检查（wrapper 内容 / BASH_ENV→pytest / testbed git 干净 / prompts / 扩展文件）+ **扩展加载对照实验**（坏路径/坏 TS 均先于 model 解析报错 → 真扩展无错 = governor 加载成功且 jiti 依赖注入正确）。

### 5.3 P5 修复轮完成项（2026-10-14，外部审查 6 项 + pilot 加固）

**审查 6 项修复**（每项均有验证证据，详见 memory_doc §5.8）：

1. **模型 scope**（最致命：单模型 scope 使 governor 无法切换）→ governor 组 `--models` 传三档全体（`--model` 选初始 strong），vanilla 组单模型。验证：三档不同模型的实际 dry-run 中 `model_scope` = 三模型逗号串、probe 通过、`jev-state` 日志出现。
2. **状态机**（原实现超时/失败也写 status=ok）→ 8 状态分类；仅 `success`/`timeout_partial`（且 patch 非空）进 preds（**P6 起修正为分母 = 计划集，见 §5.4**）。验证：10 组分类分支 + 6 组收录真值表单元测试全过。
3. **JEV 成本漏统计** → `parse_decisions` 统计 `usage.cost/input_tokens/output_tokens/latencyMs`；`parse_pi_log` 额外统计实际 `provider:model` 分布与 `stopReason` 分布。验证：单元测试精确匹配（成本/tokens/延迟）。
4. **超时 patch 不稳定** → `docker stop -t 5`（杀全进程树）→ `docker start` 后收 patch。验证：容器内挂 `sleep 300` 子进程 → stop/start 后消失；writable layer（/testbed HEAD、/tmp 文件）保留；stop 实测 0.03s、start 4.1s。
5. **run-id 混组/重复** → `<output>/<run-id>/<variant>/` 物理隔离 + run dir 非空拒绝（exit 1）+ run-id 字符白名单。验证：重复 run-id 被拒；AB/BA 交替日志（14182: G→V，14508: V→G）。
6. **dry-run 没加载 Governor** → 改为**零成本 probe**：真实 scope + 真实 `-e governor`，但注入假 key 跑 pi；扩展加载/模型解析失败标记（`Failed to load extension`/`No models match pattern`/`Model not found`）都出现在 API 调用之前。验证：真配置 probe 通过且输出含 `jev-state` entry（扩展真实运行）+ 401（死在认证步）；假模型名必触发 `No models match pattern`（判据灵敏度，见 memory_doc §6.18）。

**pilot 加固**：

- base 校验：`git merge-base --is-ancestor <base_commit> HEAD`（dry-run 首跑即抓到镜像 HEAD ≠ base_commit，见 memory_doc §6.16）。
- patch 命令：`git diff --cached --binary --full-index <image_head>` + `.git/info/exclude` 排除运行时垃圾 + 5MB size warn。验证：制造的变更（跟踪文件/新文件）正确入 patch、`__pycache__` 零泄漏、`git apply --check` 通过。
- AB/BA 配对交替（奇数 idx 正序、偶数反序）。
- `--sample N --seed S`：固定 seed 抽样，验证可复现（seed 7 两次一致、seed 8 不同）。
- manifest 增强：tier_models / model_scope / dataset_fingerprint / inject_tar_sha256 / prompt SHA / governor git head+dirty / ab_ba_pairing。
- preflight 硬校验：governor 组要求三档齐全 + `PI_JEV_UNATTENDED_POLICY=benchmark` + API key 存在 + 模型 id 正则。

**验证总览**：py_compile / 6 轮 dry-run（含三档不同模型、2 实例 AB/BA）/ 单元测试（classify_status 10 例、preds 真值表 6 例、parse_decisions 9 项、parse_pi_log 8 项）/ patch apply 兼容性 / 重复 run-id 拒绝 —— 全部通过。测试容器与 dry-run 残留目录已清理。

### 5.4 P6 第二轮修复轮（2026-10-14，外部审查 4×P0 + P1 清单）

**背景**：P5 交付后第二轮外部审查判定「大部分修复到位，但还有 4 个影响评测结论的 P0」；逐条实测（mock 复现脚本 T1–T3）全部成立，随后完成全部修复并逐项验证。

**4 个 P0 与修复**：

1. **失败样本被排除 → 幸存者偏差**（原策略：空 patch 的 timeout / agent_error 不进 preds，两组分母不同）→ **分母 = 计划集**：RESULT_STATUSES（success/timeout_partial/agent_error）全部写 preds（空 patch 也写），仅基础设施状态不写且对称重试（≤2 次），manifest `infra_failed` 记录残留失败并 exit 1。实证：swebench 5.0.2 `get_dataset_from_preds` 把无 prediction 记为 `incomplete_ids`（不是 unresolved）。
2. **dry-run probe 严重假阳性**（原判据只检查 3 个错误字符串、忽略退出码：rc=127 + 无关致命错误仍返回 `ok:true`）→ 组合判据：反面标记 + rc∈{126,127,137} + 至少 1 个可解析 JSON 事件 + governor 要求 `jev-state` + 认证失败证据（401/unauthorized/…，证明真实走到 provider auth 步）。验证：故障注入 rc=127 垃圾输出 → fail；真配置 → 通过（14 JSON 事件 + 401 + jev-state×2）。
3. **Governor timeout 绕过扩展有效性检查**（原分类顺序先判 timeout 后查决策日志，无决策日志的超时被记 `timeout_partial` 进 preds）→ 决策日志校验提到所有分支之前（governor + 0 决策日志 → `extension_bypass`，永不再进 preds）。另：`docker stop` 原 `check=False` 不验证停止成功 → 改用 stop 重试 + `docker inspect {{.State.Running}}` 校验，失败则 `collection_error` 不收 patch（防停止未生效时收 patch 被仍在运行的进程污染）。
4. **最终模型调用失败仍判 success**（原判据只看是否有 stop——`{"toolUse":3,"error":1}` 判成功）→ `final_stop_reason ∈ {error, aborted}` → `agent_error`；pi 非零退出码 → `agent_error`。

**P1 加固（全部完成）**：非 dry-run governor 三档相同直接拒绝；`total_cost_usd`（pi+jev）入 metadata 与 summary；cache_read/write tokens、route 档位分布（requested vs effective）、skip 原因、degradation、parse_errors 全量入 metadata；finalized manifest（expected/completed/status_counts/recorded_outcomes/infra_failed/preds_count）+ 退出码（非 dry-run 有 infra 失败或零 outcomes → 1）；`--resume`（跳过已终结、重跑基础设施、补写 preds、manifest 一致性拒绝，见 §5.1）；manifest 补 9 项（dataset_revision（HfApi sha pin）/ thinking_level / evaluator 版本 / docker server 版本 / jev_thresholds / env_allowlist / pi_deps sha 等）；env allowlist（`PI_JEV_*` + `OPENROUTER_API_KEY`）；setup 失败自清理容器；`--thinking` 显式固定；`test_run.py` 47 项单元测试落地；`check_images.py` 判分镜像 digest 校验。

**验证证据**：47 单测全过；单档/三档 dry-run 全链路（含三档不同模型 scope = 三模型逗号串）；resume（skip 已终结 + preds 补写 + 篡改实例集拒绝）；check_images（OK + MISSING/MISMATCH 故障注入均 exit 1）；重复 run-id 拒绝；三档相同非 dry-run 拒绝。

**重要事实纠错**：此前记录的「evaluator 会 checkout base_commit 再 apply patch」经 swebench 5.0.2 源码核实为**错误**——evaluator 直接在镜像 HEAD 工作树上 `git apply`，无 checkout。image_head 基线结论不变，理由修正为「镜像自带变更会污染 patch + 防 agent 违约 commit」（memory_doc §6.20）。

### 5.5 P7 第三轮修复轮（2026-10-14，外部审查 6×P0 + 2 组 P1）

**背景**：P6 交付后第三轮外部审查指出「核心链路已正确，仍有崩溃一致性 / 配置冻结 / 证据链三类系统性缺口」，6 个 P0 + 2 组 P1 逐条核实后完成全部修复并验证。

**6 个 P0 与修复**：

1. **preds 崩溃一致性**（原 append 逐行写：崩溃截断半行、resume 重跑重复行）→ metadata.json / patch.diff / probe 输出全部原子写（tmp + os.replace）；metadata 为唯一事实源，preds.jsonl 由 `rebuild_preds` 从实例目录**全量原子重建**（幂等，run 后 / resume 启动 / finalize 三处调用）。验证：6 项 rebuild 单测（空 patch / 去重崩溃残留 / 排除 infra / 幂等 / 无 tmp 残留）。
2. **run spec 不可变 + revision pin**（原 resume 只比对 instances/model/scope，改 thinking/timeout/prompt/JEV env 后 resume 静默混组）→ `build_run_spec` 冻结 15 键（dataset revision + fingerprint、模型、thinking/timeout、prompt/资产 SHA、dry_run、JEV env（剔 API key）），`config_hash`（64 位）入 manifest；resume 全量比对、差异列 `changed field(s)` 拒绝；新建 run 无法 pin revision 时 fail-closed（`--allow-unpinned-dataset` 显式豁免）；resume 用旧 pinned revision 加载。验证：8 项单测 + 实测混配置 resume 拒绝（`changed field(s): thinking_level`）。
3. **probe 判据 v3**（P6 判据依赖 `jev-state` 子串——实测真实 probe 的 11 个结构化事件（7 种类型）里**没有 custom_entry**，判据依赖的输出形状与真实不符；合并输出子串匹配混淆字段边界）→ 结构化组合证据链：pi 事件（白名单类型 + `*_start/*_end/*_error` 后缀家族）≥1 → `errorMessage` 形状字段内 401/unauthorized（`\b401\b` 词边界，仅结构化字段）→ 容器 route 决策记录 ≥1（degraded 配置只写 jev-state、无决策记录）→ rc 一致性（rc∈{126,127,137} 直接失败；其他非零 rc 需完整链才豁免；**P8 收紧为任何非零 rc 直接失败，见 §5.6 第 7 条**）。实测校准：真实 dry-run rc=0、11 事件（7 类事件全覆盖）、401 从结构化字段提取。
4. **classify_status 用解析后证据**（原凭原始字符串计数：degraded governor（route=0）漏判；pi 事件为 0 仍可能落 agent_error 进 preds）→ 固定顺序：扩展有效性（extension_errors / governor 零 route → `extension_bypass`）→ 采集完整性（解析错误、日志读取错误、pi 事件 = 0 → `collection_error`）→ 结果判定（timeout → exit_code → settled / 末次响应 → success）。
5. **Docker 生命周期异常专属类型**（原 agent 阶段 docker exec 异常被 `except Exception` 吞成 agent_error 写进 preds）→ 新增 `InfraError`：launch_pi 异常（OSError/SubprocessError）、`collect_run_log` rc≠0、stop/start 稳定性校验失败统一抛出，agent 阶段映射 `collection_error`（对称重试、不进 preds）。
6. **check_images 三缺口**（同 tag 多 digest 集合判据静默通过；计划实例缺 metadata 不报；recorded outcome 缺 image_id 不报）→ `analyze_run` 纯函数重写（inspect 可注入）：MIXED 直接 FAIL（一次 run 内同 tag 多 digest，即使本地命中其一）、计划集完整性（每 planned instance 有 metadata；RESULT_STATUS 必带 image_id）、UNREADABLE 检测。

**P1（2 组）**：①重试成本双统计——`attempt_history` 逐次记录 cost、`total_cost_usd_all_attempts` 入 metadata 与 summary（原 `total_cost_usd` 只含最终尝试，重试成本从账本消失）；②杂项——`--variant` 去重、revision fail-closed、manifest 记录 JEV env 与全部哈希 64 位。

**验证证据（全部实测）**：89 单测全过（test_run 79 + check_images 10，较 P6 的 47 新增 42）；dry-run 冒烟（probe rc=0 + route 记录 + 结构化 401；run_spec 15 键 / config_hash 与 fingerprint 64 位 / revision pinned / finalized 字段 / preds.jsonl 空）；resume 混配置拒绝（rc=1）+ 正常 resume（原子重建 + skipped + rc=0）；`check_images.py` 实跑 OK + exit 0。经验教训见 memory_doc §6.24–§6.27。

### 5.6 P8 第四轮修复轮（2026-10-14，外部审查 2×P0 + 1×P1 + 4 项次级）

**背景**：P7 交付后第四轮外部审查判定「修复方向正确、原审查 6 项大部分落地，但仍有 2 个会直接污染评测结果的 P0 与若干持久化/校验缺口」，无法进入付费 pilot。逐条核实全部成立（均有代码证据链），随后完成全部修复并验证。

**2 个 P0 与修复**：

1. **重试复用旧 decision 证据**（`collect_decisions` 只在文本非空时写文件，而 `inst_dir` 跨 attempt 复用：attempt 2 静默旁路/降级、没有决策日志时不覆盖旧文件 → `parse_decisions` 读到 attempt 1 的旧 route 记录 → route≥1 有效性门被旧证据通过 → 无 governor 参与的数据点可能判 success 进 preds）→ 改为**永远原子写**（空输出也写空文件）：空 → route=0 → 正确判 `extension_bypass`。验证：stale 文件清空单测（旧 route 文件 + 空读取 → 文件清空 → parse_decisions route=0）+ 既有写入测试。
2. **restart 异常写成 agent_error**（`launch_pi` 的 TimeoutExpired 处理块内调用 `restart_container`，其 stop/verify-inspect 无保护：docker stop 卡死或 CLI 故障时异常从 except 块体外抛 → 绕过 InfraError 通道 → 落入 `run_one_attempt` 的 `except Exception` → phase="agent" → 幻影 `agent_error` 进 preds）→ `restart_container` 全函数包裹 try（契约改为「永不抛出」）：任何故障 log + return False → 走既有 `container_stable=False → InfraError → collection_error` 路径。验证：单元级（docker 抛 TimeoutExpired → False）+ 全链路（exec 超时 + restart 故障 → `collection_error`，timed_out/container_stable 字段正确）。

**1 个 P1 与修复**（check_images 结构完整性 fail-closed；复核实为 4 个放行缺口，比审查多 1 个）：

3. manifest 缺失静默跳过该变体完整性检查 → `MISSING manifest` FAIL；`instances` 缺键/为空放行 → `EMPTY` FAIL；recorded outcome 缺 `patch.diff` 文件不检查（评测会把缺文件当空 patch 提交）→ `NO-PATCH` FAIL（仅判文件存在性，空内容文件是合法 outcome）；补充：records 中未列入任何计划的实例不报 → `UNPLANNED` FAIL（仅对 manifest 可读的变体判定，避免与 MISSING manifest 双报）。汇总行加 structural 计数。验证：5 项新单测（含「空 patch 文件通过」反例）。

**4 项次级与修复**：

4. **parse_errors 断链**（`parse_pi_log` 统计了 run.log 解析错误但 `classify_status` 不读：日志尾部损坏仍可能判 success，且被吞掉的最后 message_end 造成成本低估）→ success 门前插入 `parse_errors → collection_error`（timeout / exit_code 等更早的保守判定不受影响，避免把超时截断误降级为可重试错误）。
5. **成本不完整未标记**（`collect_run_log` 失败时 attempt 无 cost_usd 记 0，`total_cost_usd_all_attempts` 被当作精确值）→ `attempt_history` 每项加 `cost_collected`（dry run / 容器未创建 = 精确 0；有容器但日志缺失或含解析错误 = 不完整）、run 级 `cost_complete` 入 metadata 与 manifest（`cost_incomplete` 列表）、summary 标注 `cost_complete=false` 且 TOTAL 注明为下界。
6. **resume 对 runner 自身变更不可感知**（run spec 无 runner 哈希；eval/ 整体 untracked，改 run.py 后 resume 静默混两套执行逻辑；manifest 重写还会覆盖原 git 状态）→ spec 加第 16 键 `runner_sha256`（run.py 文件 SHA256，进 config_hash → 改动即拒绝 resume）；manifest 顶层同步记录；resume 保留首次运行的 git 状态到 `original_governor_git`。
7. **probe rc 豁免与实测不符**（`full_chain` 允许 rc=1 + 完整证据链通过，而真实 probe 实测 rc=0）→ 收紧为**任何非零 rc 直接失败**（126/127/137 报 shell level，其余报 non-zero probe exit）；对应测试改为失败断言。

**验证证据**：102 单测全过（test_run 87 + check_images 15，较 P7 的 89 新增 13）；py_compile ×4、git diff --check、results/ 空目录确认。经验教训见 memory_doc §6.28–§6.31。

### 5.7 P9 run.py 质量小修（2026-10-14，内部静态审查）

P8 交付后对 run.py 全文静态审查（无新外部审查），落地 8 处质量修复：①`cleanup_container` 永不抛出契约（finally 路径：docker rm 故障只 log + timeout，不再可能中止整个 batch 或吞掉 attempt 结果）；setup 的 except 清理块改复用该 helper（rm 失败不再替换原始 setup 错误）；②`collect_run_log` 补原子写（`atomic_write_bytes`；`atomic_write_text` 委托之）——此前是唯一非原子产物；③`validate_run_id` 拒绝 `.`/`..`（会把 run root 重定向到 output 目录之外）；`--limit/--sample` 拒绝 0；`load_instances` 改 `is not None`（0 不再静默回落到 1 条默认）；④docker 命令失败保留 stderr（`describe_exception`：missing image 的 "No such image" 进 metadata）；⑤主循环 try/finally 保证 Ctrl-C 不泄漏 tmp_root（含 API key 的 container.env 临时目录）；⑥风格：`is`→`==`、`not in (0,)`→`!= 0`。验证：108 单测全过（test_run 93 + check_images 15，较 P8 新增 6）；py_compile ×4、diff-check 干净。

### 5.8 P10 第五轮修复轮（2026-10-14，外部审查 P0×1 + P1×2）

**背景**：P9 交付后第五轮外部审查判定「8 项修复基本落地，但仍有 3 个影响评测可信度的问题，其中一个能直接把 runner 故障写成模型失败」。逐条核实（代码证据 + 测试证据）全部成立，完成修复并验证。审查另指出：eval/ 整体 untracked，`git diff --check` 对关键 runner 文件无覆盖力（本 README 中既往轮次该项证据价值应降级）。

**3 项与修复**：

1. **P0 未知 runner 异常仍记 agent_error**——`run_one_attempt` 的 `except Exception` 分支直接 `PHASE_STATUS.get(phase)`：agent 阶段任何非 InfraError 异常（runner bug、MemoryError 等）→ `agent_error`（RESULT 状态）→ 无证据链地进 preds 记为模型失败、不重试、run 仍 exit 0。InfraError 通道（§5.5 第 5 条）只钉死了已知基础设施故障，兜底映射是同一泄漏点的第二层。修复：状态映射集中为 `phase_status(phase)`（agent → `collection_error`），两个 except 分支共用，杜绝再次漂移；新测试：agent 阶段 RuntimeError → `collection_error` + `phase_status` 全相位断言。
2. **P1 缺失 patch 生成“合法空预测”**——`rebuild_preds` 把 `patch.diff` 缺失/不可读与“空文件”合并（`""`）静默写进 preds：损坏状态下 run 伪装成“模型产出空 patch”（评测判 unresolved），与 §5.6 第 3 条 check_images 的 NO-PATCH fail-closed 不一致。修复：区分 missing 与 empty——缺失仍写行（分母 = 计划集，不能把实例从统计里剔除）但返回 missing 列表；resume 时 warn、finalize 记入 `missing_patch_files` 并 **exit 1**；新增「缺失上报」单测。
3. **P1 infra 失败的计划实例通过 check_images**——`analyze_run` 只检查 RESULT 状态实例：计划集内 `setup_error`/`collection_error` 等（有 metadata、无结果、不进 preds）整体放行 → 判分时从分母静默消失。修复：非 RESULT 状态报 `NO-OUTCOME` FAIL（infra 状态无 digest 是正常的，故不报 NO-IMAGE）；原「infra 状态不报 NO-IMAGE 即通过」测试改为「必须 FAIL 且不报 NO-IMAGE」。

**验证证据**：111 单测全过（test_run 96 + check_images 15，较 P9 新增 3）；py_compile ×4、results/ 空确认。经验教训见 memory_doc §6.27/§6.30 的 P10 补强。

### 5.9 P11 第六轮修复轮（2026-10-14，外部审查 P1×2）

**背景**：P10 交付后第六轮外部审查判定「P10 三项修复真实且基本正确、无同等级评分污染，但仍有 2 个 P1 与 eval/ 未入库风险，正式 pilot 前应处理」。逐条核实全部成立（含审查给出的最小复现：decision 读取失败时 `cost_was_collected=True`），完成修复并验证。

**2 项与修复**：

1. **P1 成本判据漏 decision 读取失败**——`cost_was_collected` 枚举了「run.log 未采集 / run.log 解析错误 / decisions 解析错误」，但漏了 decision 文件**读取失败**：cat 失败文件的 JEV 成本丢失，剩余记录解析干净 → `cost_complete=True`，成本下界冒充精确值。修复：判据补 `decision_read_errors`（timeout 诊断路径只设该字段、不设 `collection_errors`，故选它覆盖最广）；docstring 同步为「run.log 未采集/不可解析、decision 文件读取失败/解析失败」全覆盖。验证：审查最小复现脚本由 True 变 False + 单元级（判据）/端到端（`run_one` → `cost_complete=False`）回归测试。
2. **P1 checker 不验证判分输入**——`check_images.analyze_run` 校验了 manifest/metadata/patch.diff/digest，却从不读 **preds.jsonl**——而 evaluator 判分消费的正是它：preds 缺失（预测整体消失）、坏行、重复行、缺行、多余行、stale（patch.diff 被重建而 preds 未重建 → 上传旧 patch）全部放行。修复：新增 (2) Grading input 检查块（原 (2) digest 顺延为 (3)）——`MISSING / UNREADABLE / BAD-PREDS / STALE-PREDS / DUP-PREDS / NO-PRED / EXTRA-PRED` 七类检查；expected 集合只含 RESULT 状态实例（与 NO-OUTCOME 不双报），置于 (1) 之后、digest 之前（不被 by_tag 空的提前 return 截断）。验证：5 项新单测（缺文件 / stale / 缺行 / 重复+多余 / 坏行）。

**验证证据**：118 单测全过（test_run 98 + check_images 20，较 P10 新增 7）；py_compile ×4、build-assets.sh bash -n、results/ 空。经验教训见 memory_doc §6.31 P11 补强与 §6.32。

### 5.10 P12 第七轮修复轮（2026-10-15，外部审查 P1×1）

**背景**：P11 交付后第七轮外部审查判定「P11 修复真实正确、118 测试通过，但 preds schema 检查仍比 evaluator 宽松，会放过 evaluator 实际无法完整消费的文件」。审查独立复现 `missing_model_name_ok = True`、`blank_line_ok = True`；对本机 swebench 5.0.2 源码核实消费约束均成立：harness/utils.py 对 jsonl 逐行 `json.loads`（无 strip 无跳过——单个空白行让整个文件加载失败）；harness/reporting.py 与 run_evaluation.py 直接下标读 `model_name_or_path` 并 `.replace()`（字段缺失在**全部实例跑完后**的报告阶段才 KeyError，最贵失败点）；grading.py 直接下标读 `instance_id`/`model_patch`（None 有专门桶可消费）。

**修复**：check_images (2) Grading input 块收紧：①空白行由跳过改为 `BAD-PREDS ... blank line`；②行 schema 逐字段报告缺失——`instance_id`（非空 str）/ `model_name_or_path`（非空 str）/ `model_patch`（键存在；expected 实例的 None 因 ≠ 磁盘 patch.diff 已由 STALE-PREDS 捕获）。docstring 同步。

**验证证据**：120 单测全过（test_run 98 + check_images 22，较 P11 新增 2：空白行 / 缺 model_name_or_path）；py_compile ×4、results/ 空。教训见 memory_doc §6.32 P12 补强。

### 5.11 P13 三档配置 + 真实单题链路 smoke（2026-10-15）

三档落地（SMALL=flash / NORMAL=plus / STRONG=max，与 .env.local.example 推荐一致）后，链路首次完整实跑：

> **收尾变更（用户指示）**：governor 初始档由默认 strong 改为 normal（run.py `tiers.get("normal")`，D 组初始=B 组固定，对照差异纯净为路由本身）；120 单测复跑全过 + dry-run probe 复验通过。下文「34 次全 max」是旧默认下的行为，改后需新 run 才能观察初始 normal 的表现。

1. **零成本 dry-run**：probe `dry_run_ok`（扩展真实加载 + 三档 scope 全部可解析 + 假 key 死在认证步），11.2s。
2. **真实单题**（governor × astropy__astropy-14182，run-id smoke-01）：success，wall 165s，pi=$0.3432 + JEV=$0.0011，`cost_complete=true`、parse_errors=0、attempts=1；决策日志完整（route 1 / permission 33（27 allow·6 block）/ outcome 1）。34 次调用全部落在 strong 档（max）——唯一 route 决策建议 normal 但置信度 0.49 < 0.8，按设计降级保持当前模型。
3. **check_images**：镜像 digest 一致 + preds 判分输入七类校验首次在真实产物上通过。
4. **判分打通**：新增 **grade.py**（swebench 本地判分的 docker-rt 适配 wrapper，参数与 `swebench eval verified` 对齐）。首次实跑暴露两个 docker-rt 差异：镜像列表含 inspect 404 幽灵条目（list_images 崩）、镜像 WorkingDir 不被保留（apply 在 `/` 找不到文件）——两个进程内 monkeypatch 修复后全链路正常。结果 resolved=0 / unresolved=1（9 passed / 1 failed：F2P `test_rst_with_header_rows`，agent 修复不完整）——模型表现问题，非管道问题。

判分用法：

```bash
DOCKER_HOST=unix:///tmp/docker-rt.sock python grade.py \
    --predictions results/<run-id>/<variant>/preds.jsonl \
    --instance-ids <instance_id> \
    --run-id <id> --report-dir results/<run-id>/eval
```

数据质量观察：agent 创建了空 test_fix.py（违反 prompt 约束「不建测试文件」）被 `git add -A` 带进 patch——对判分无害，但 patch 纯净度靠 prompt 自觉执行不可靠（记录观察，暂不加固）。

**P14 续跑（smoke-02，2 题 × vanilla+governor，2026-10-15）**：django-10999 + xarray-3677，4/4 success、check_images 与判分零错误。**路由首次真实切换**：governor django 题初始 plus → route 成功切 flash（15 次全 flash，$0.0015 vs vanilla $0.0045，省 67%）；xarray 题 route 建议 0.56 < 0.8 被拦、保持 plus（$0.0312 vs $0.0245，JEV 决策 30 次开销）。判分两组完全一致（xarray resolved、django unresolved 各 5 failures）——路由/拦截未影响解题能力。初始 normal 改动后单题成本从 $0.344 降至 $0.0015–0.031。代码已入库推送（commit daf2ca8 → qianyuxiang-369/pi-jev-governor-swebench，SSH key 见 memory_doc §5.17）。

**P15（smoke-03，6 题 × vanilla+governor，2026-10-15）**：sphinx-9591 / pytest-6202 / sympy-17139（easy）+ django-12262 / django-14122 / sklearn-14894（medium），12/12 success、判分零错误。vanilla 6/6 resolved，governor 5/6（sphinx-9591 unresolved）。成本 vanilla $0.228 vs governor $0.826（3.6×）。归因三项：① `SENSITIVE_TOOL_PATTERN` 的 `\.env\b` 误伤代码内 `self.env`——sphinx-9591 被误拦 65 次（edit 34 / write 17 / bash 14），编辑无法落地 → 成本 4.6× 且未解出；已修复（`(?<!\w)\.env\b` + 回归测试，见 src/evidence.ts）。② django-14122 36 次 `bash_not_allowed`（`cd /testbed && …` 组合命令与白名单不匹配的重试税，设计内行为，未改）。③ 路由 6 题全部未触发切换（tier 置信度 0.11–0.38 < 0.8），JEV 决策开销 ≈$0.010。check_images 判据升级：docker-rt 下同 tag 多副本 image_id 属正常（12 条记录 6 tag 互不相同），判定改用 image_head 内容指纹（同 tag 唯一 + `docker run --rm --entrypoint git <tag> git -C /testbed rev-parse HEAD` 实时探针），老 metadata 无 image_head 时回退 image_id 比较。单测 124（py）+ 192（TS）。

### 5.12 留待 pilot 的决策点

- [ ] 并发度：当前单容器串行；docker-rt 单 daemon 下 `-w N` 的资源限制留 pilot 实测。
- [ ] patch 是否过滤测试路径（按 pilot 统计再定；P13 已观察到 agent 会创建额外的空测试文件进 patch）。
- [ ] pilot 规模与预算：三档已配置为 flash/plus/max（P13，非 dry-run 的三档 distinct 校验已通过）。

### 5.13 build-assets.sh 的 JuiceFS 坑（已内置重试）

JuiceFS 上刚 copy 的大文件（node 二进制 123MB）元数据滞后几秒，tar 会误报 "file changed as we read it"：
进程退出码 1，而 `--warning=no-file-changed` 只抑制警告文本、**不改变退出码**，`set -e` 下脚本中止。
脚本内置重试（失败后 sleep 5 重跑一次，第二次文件状态已稳定必然成功，tar 内容完整）。
