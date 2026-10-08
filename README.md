# pi-jev-governor

English | [简体中文](README.zh-CN.md)

A governor for the [pi](https://pi.dev) coding agent: it routes four coding-agent decisions through the JEV Decisions API.

| Decision | pi event | Result |
|---|---|---|
| `plan_strategy` | `before_agent_start` | `direct`, `plan_only`, or a read-only `plan_then_execute` phase |
| `tier` | `before_agent_start` | `small`, `normal`, or `strong`, with model compatibility guards |
| `permission.action` | `tool_call` | deterministic rules, JEV adjudication, and human/fail-closed fallback |
| `outcome` | `agent_before_settle` | finish, retry, or replan with a task-wide correction budget |

`plan_strategy` and `tier` share one request. A `plan_then_execute` task makes an additional Outcome request to validate the plan before execution. Planning revisions and execution corrections share a hard maximum of two automatic corrections.

The package is loaded directly by pi. It has no bundled runtime dependencies and does not modify pi itself.

## Install

```bash
pi install git:github.com/qianyuxiang-369/pi-jev-governor@v0.1.0
pi install npm:pi-jev-governor@0.1.0
pi install ./pi-jev-governor
```

Try a local checkout without installing:

```bash
pi -e ./pi-jev-governor
```

Requires pi 0.87.1 — the validated anchor: all unit tests and the acceptance evidence in [`evidence/`](evidence) were produced against it. Later releases, including pi 1.x, are untested and may change the extension API. `@earendil-works/pi-ai` and `@earendil-works/pi-coding-agent` are peer dependencies supplied by pi.

## Configure

At minimum, configure the JEV credential and one pi model for each tier. The repo keeps local configuration in a git-ignored `.env.local` (template: [`.env.local.example`](.env.local.example)) and provides [`scripts/run.sh`](scripts/run.sh), which sources it and launches pi with the extension:

```bash
cp .env.local.example .env.local   # fill in your keys and models
./scripts/run.sh                   # from any scratch project directory
```

Manual equivalent:

```bash
export PI_JEV_API_KEY="$OPENROUTER_API_KEY"
export PI_JEV_MODEL_SMALL="openrouter:qwen/qwen3.7-flash"
export PI_JEV_MODEL_NORMAL="openrouter:qwen/qwen3.7-plus"
export PI_JEV_MODEL_STRONG="openrouter:qwen/qwen3.7-max"
pi -e ./pi-jev-governor
```

Model references use `provider:modelId` and split on the first colon.

| Variable | Default | Notes |
|---|---|---|
| `PI_JEV_ENABLED` | `true` | Set to `false` to disable all four decisions |
| `PI_JEV_URL` | `https://openrouter.ai/api/alpha/decisions` | JEV endpoint |
| `PI_JEV_API_KEY` | required | API credential |
| `PI_JEV_MODEL` | `typesafe/jev-1.13` | Decision model |
| `PI_JEV_TIMEOUT_MS` | `3000` | Total deadline across an optional retry, 1–60000 ms. The hosted OpenRouter endpoint commonly answers in 2–4 s; `10000` is a practical value there |
| `PI_JEV_MODEL_SMALL` | required | `provider:modelId` |
| `PI_JEV_MODEL_NORMAL` | required | `provider:modelId` |
| `PI_JEV_MODEL_STRONG` | required | `provider:modelId` |
| `PI_JEV_ROUTE_CONFIDENCE` | `0.8` | Lower confidence uses direct/current-model fallback |
| `PI_JEV_PERMISSION_CONFIDENCE` | `0.9` | Lower confidence requires human approval or blocks headless |
| `PI_JEV_OUTCOME_CONFIDENCE` | `0.8` | A plan only advances on an explicit high-confidence finish |
| `PI_JEV_MAX_CORRECTIONS` | `2` | Task-wide budget, hard-capped at 2 |
| `PI_JEV_OUTCOME_GIT_DIFF` | `false` | Opt in to sending `git diff --stat`; never sends the diff body |
| `PI_JEV_PLAN_AUTO_EXECUTE` | `true` | When false, accepted plans require UI confirmation |
| `PI_JEV_LOG_DIR` | platform state directory | Explicit log directory override |

Tier switching keeps the current model when the target cannot be resolved, is outside `--models` scope, has too small a context window, cannot accept images already in the branch, lacks provider authentication, or the user manually changed models.

### Switches and status

- `pi --no-jev` disables the plugin for that invocation.
- `/jev` shows the current phase, tier, correction budget, decision count, and log file.
- `/jev off` and `/jev on` change the session switch.
- `/jev on` does not override `--no-jev` or `PI_JEV_ENABLED=false`.

Missing or invalid remote configuration puts the plugin in `degraded` mode, not full bypass. Routing uses direct/current-model defaults and Outcome automation stops, while the local permission gate remains active. Calls needing remote judgment ask the user when UI is available and fail closed when it is not.

## Lifecycle

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

Every new `before_agent_start` begins a task and clears its approval cache. Continuations, steering, and follow-up messages inherit the active route rather than re-running it.

State is stored as `jev-state` session entries. Session resume and tree navigation restore from the active branch via `getBranch()`, including the read-only tool snapshot, correction budget, and model-routing state. `session_tree` also clears all task approvals.

Continuation is committed atomically at the settle boundary: the state entry and a `custom_message` are returned with `continue: true`, then pi recomputes whether the final context can continue. The plugin does not gate this on the pre-handler `context.canContinue` preview and does not use a nested `sendMessage` fallback.

## Permission gate

Permission decisions run in four layers:

1. Planning phases allow only built-in read tools and allowlisted read-only shell commands.
2. Deterministic rules block hard-deny shell patterns, allow built-in read tools, and honor task approvals.
3. Clean, sanitized tool input is sent to JEV.
4. `ask`, low confidence, sensitive input, or a remote error goes to the user; without UI it blocks.

An unexpected serialization, UI, or handler failure is also converted into a sanitized fail-closed block. Raw exception text is not returned to the model. Plan validation reports low confidence, service unavailability, invalid answers, and oversized evidence as distinct reasons; none of them starts execution.

Human choices are task-scoped:

- Allow once
- Allow this exact call for this task
- Allow this tool for this task
- Block

The exact-call and tool caches are cleared on the next task and on session-tree navigation. Sensitive inputs only offer allow-once or block.

pi may issue sibling tool calls concurrently. pi-jev-governor serializes permission decisions and confirmation dialogs to avoid UI and cache races; it does not serialize the tools after approval. A `terminate` result is not treated as a guarantee that already-dispatched siblings cannot run, so each call must be safe on its own verdict.

## Privacy and data disclosure

The plugin sends the minimum evidence needed for each decision:

| Data | Remote behavior |
|---|---|
| Task prompt | Truncated and scrubbed of configured secret literals |
| Ordinary tool input | Sensitive keys redacted and length limited |
| Clean shell command | Sent after sanitization so JEV can understand the operation |
| Command containing credentials | Never sent; handled by local confirmation or fail-closed |
| Sensitive-file evidence | Replaced wholesale with an omission marker |
| Tool-result tail | At most 10 entries, truncated and redacted |
| `git diff --stat` | Sent only when explicitly enabled; full diff is never sent |

Credential detection is intentionally narrow: Authorization/Bearer headers, credential-shaped `KEY=value` contexts, private-key headers, and configured secret literals. Broad entropy matching is not used because hashes and git SHAs are common in legitimate commands.

Tool output that embeds a credential context (a `Bearer`/`Authorization` header or a private-key block) is omitted wholesale, so a benign read of an innocently-named file cannot leak a token through outcome evidence. Purely high-entropy strings — git SHAs, hash constants — are kept.

Values under sensitive keys are replaced with `[redacted]`; configured secret literals are scrubbed again after serialization. Evidence over 96,000 characters is rejected instead of silently trimmed. Client errors contain only reason enums and never include provider response bodies, URLs, or credentials.

### Decision logs

Each session has its own file named `decisions-<session-id>.jsonl`. Log directory precedence is:

1. `PI_JEV_LOG_DIR`
2. `$XDG_STATE_HOME/pi-jev`
3. macOS: `~/Library/Application Support/pi-jev`
4. Windows: `%LOCALAPPDATA%/pi-jev`
5. fallback: `~/.local/state/pi-jev`

Directories are mode `0700`; files are mode `0600`. Logs contain structured answers, confidence, timing, usage, effective action, sanitized error reasons, and `correctionPhase` for Outcome decisions. They do not contain raw prompts, raw tool arguments, provider bodies, or keys.

## Security boundary

This package is workflow control, not an operating-system sandbox. It runs inside pi with the same process privileges. Use containerization or another system security boundary for untrusted or unattended execution.

## Development

```bash
npm install
npm run typecheck
npm test
```

Tests include pure decision logic, extension wiring, parallel permission confirmations, branch recovery, log permissions, and a real public-API agent lifecycle using pi's faux provider. The lifecycle regression verifies that an assistant-ending settle preview may report `canContinue=false` while a returned `custom_message + continue` still starts the next provider turn after pi recomputes the final context.

### Acceptance testing

Beyond unit tests, an automated acceptance harness exercises the plugin end-to-end: real pi, real agent models, and a deterministic local JEV mock that can script any answer (`ask`, sequential `retry→finish`, budget exhaustion) or inject faults (5xx, timeouts, garbage) per decision kind. Each case leaves per-case evidence (model output, decision log, the exact requests the mock received) and a summary `RESULTS.md`:

```bash
./scripts/run-acceptance.sh    # 22 cases; requires .env.local and pi on PATH
```

Interactive TUI behavior (confirmation dialogs, status bar, session resume) is covered by the manual screenshot protocol in [docs/acceptance-tests.md](docs/acceptance-tests.md), which also maps every case to the [scripts/smoke.md](scripts/smoke.md) checklist.

See [docs/design.md](docs/design.md) for the v3 specification, [docs/acceptance-tests.md](docs/acceptance-tests.md) for the acceptance protocol, and [scripts/smoke.md](scripts/smoke.md) for the original manual checklist.

## License

MIT
