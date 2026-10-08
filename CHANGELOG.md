# Changelog

## 0.1.0 (2026-10-08)

Initial release.

- Decision 1 `plan_strategy` + Decision 2 `tier`: one JEV request at
  `before_agent_start`; model switching with manual-override detection,
  scoped-model, context-window, and image-input guards; read-only plan phase
  for `plan_only` / `plan_then_execute`.
- Decision 3 permission gate at `tool_call`: phase gate, deterministic
  allow/deny rules, JEV adjudication with human confirmation fallback
  (fail-closed without UI). Concurrent confirmations are serialized; approval
  caches are task-scoped (`allow_once` / `allow_exact_for_task` /
  `allow_tool_for_task`); credential-bearing commands stay local while clean
  commands are sanitized before being sent; permissions stay enforced in
  degraded mode; unexpected handler failures become sanitized fail-closed
  blocks.
- Decision 4 outcome judge at `agent_before_settle`: finish / retry / replan
  with a bounded corrections budget shared across planning and execution,
  phase-tagged logs, structured settle reasons, evidence redaction, and
  opt-in `git diff --stat` evidence. Plans are validated by the outcome judge
  before execution starts.
- Settle continuation returns state and `custom_message` drafts atomically;
  state is restored from the active branch on session start and tree
  navigation.
- `pi --no-jev` disables the plugin; `/jev on|off` controls the session.
- JSONL decision logs: per-session files with mode 0600 under a mode-0700
  platform state directory (`PI_JEV_LOG_DIR` overrides).
- Unit tests plus real-lifecycle integration tests against pi's public APIs.
