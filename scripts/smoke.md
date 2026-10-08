# Manual acceptance checklist

> The fuller two-track protocol lives in [docs/acceptance-tests.md](../docs/acceptance-tests.md):
> an automated harness (`scripts/run-acceptance.sh`, 22 headless cases against a
> deterministic mock JEV) plus a TUI screenshot series. This file remains the
> original quick manual checklist; case numbering below is referenced from there.

Run this checklist in a scratch project after `pi install ./pi-jev`. Use a dedicated log directory so the files are easy to inspect:

```bash
export PI_JEV_API_KEY=...
export PI_JEV_MODEL_SMALL="provider:small-id"
export PI_JEV_MODEL_NORMAL="provider:normal-id"
export PI_JEV_MODEL_STRONG="provider:strong-id"
export PI_JEV_LOG_DIR=/tmp/pi-jev-smoke-logs
pi -e ./pi-jev
```

`/jev` prints the exact `decisions-<session-id>.jsonl` path.

## M1 — route and model compatibility

1. Submit a multi-module, risky implementation task. Expect one route decision and an appropriate plan strategy/tier.
2. Run the same prompt with `pi --no-jev`. Expect no status, route request, model switch, or log entry.
3. Set `PI_JEV_MODEL_STRONG=bogus:nope`; trigger strong. Expect one warning and the current model retained.
4. Configure a target whose context window is below current usage. Expect the current model retained.
5. Add an image to the branch and configure a text-only target. Expect the current model retained.
6. Let the plugin route, manually change models, then submit a task. Expect user choice to win and tier switching to skip.
7. Send steering/follow-up input during an active run. Expect no second route decision; it inherits the active task.

## M2 — permission gate and concurrency

1. Read a file or run `ls`. Expect deterministic allow with no permission JEV request.
2. Attempt a hard-denied shell command. Expect immediate block with no JEV request.
3. Ask for a normal edit. Expect JEV allow or a human prompt depending on the returned answer.
4. At a prompt choose, in turn: allow once, allow exact call for this task, allow tool for this task, and block. Verify only the two task approvals are cached.
5. Start a new task. Verify prior exact/tool approvals no longer apply.
6. Navigate to another session-tree leaf. Verify approvals clear and permissions are asked again.
7. Trigger two ambiguous sibling tool calls from one assistant message. Verify confirmation dialogs appear serially rather than racing; approved tools may execute concurrently afterward.
8. Remove `PI_JEV_API_KEY` and run an ambiguous write interactively. Expect `jev:degraded` and a local prompt, not an automatic allow.
9. Repeat the previous case headless. Expect fail-closed.
10. Force the confirmation UI callback to fail in a test harness. Expect a sanitized fail-closed block and no raw exception text in the tool result.

## M3 — sensitive command disclosure

1. Write a shell command resembling `curl -H "Authorization: Bearer smoke-secret-token" https://example.invalid` into a file (e.g. `cmd.txt`), then ask the agent to read the file and run the exact command it contains. Deliver the token via the file, not the prompt: the task prompt itself is legitimate route evidence and is scrubbed only of configured secret literals.
2. Expect only local allow-once/block choices; no task-cache options.
3. Verify the permission request is not sent to the JEV endpoint.
4. Search the plugin's decision logs (and the mock request journal when running against `scripts/mock-jev.mjs`) for `smoke-secret-token`. It must not appear. (pi's own session JSONL records raw tool arguments and is outside the plugin's control.)
5. Try a harmless command containing a git SHA or hash constant. Verify it is not classified as a credential solely because it is high entropy.

## M4 — plan strategy and shared correction budget

1. Trigger `plan_then_execute`. During planning, verify edit/write and non-allowlisted shell commands block.
2. Make the first planning Outcome answer `retry` or `replan`. Expect a visible correction, a second provider turn, and `correctionPhase: "planning"` in the log.
3. Make the corrected plan answer `finish`. Expect tools restored, an execution instruction, and execution in the same task.
4. Make execution Outcome request a correction. Verify its correction number continues from the planning count rather than resetting.
5. Exhaust two corrections across planning/execution. Expect an explanatory message and natural settle, with no third correction.
6. Return low confidence or make plan validation unavailable. Expect execution not to start and the visible message to distinguish the two reasons.
7. With `PI_JEV_PLAN_AUTO_EXECUTE=false`, accept a plan. Choose No and verify it ends without execution; in headless mode it also ends.
8. Trigger `plan_only`. Expect read-only operation throughout and Outcome validation without transition to executing.

## M5 — resume and branch recovery

1. Stop during planning and resume the session. Expect the read-only tools and remaining correction budget restored from the active branch.
2. Navigate between leaves holding different `jev-state` entries. Expect phase/tools to follow the selected leaf, never a state from another branch.
3. Navigate to a leaf with no active state. Expect prior plan tools restored and status returned to ready.

## M6 — boundary continuation regression

1. Run the public-API lifecycle regression test in `test/lifecycle.test.ts`.
2. Verify the first completed provider turn ends in assistant output and the pre-handler boundary preview is not used to veto continuation.
3. Verify the boundary returns state plus `custom_message` with `continue: true` and a second provider turn occurs.
4. Verify no settle path uses a `sendMessage` fallback and no state/budget is appended before the boundary result is committed.

## M7 — logs and packaging

1. Start two sessions. Verify each writes a distinct `decisions-<session-id>.jsonl` file.
2. Verify the log directory mode is `0700` and each file mode is `0600` on POSIX.
3. Enable `PI_JEV_OUTCOME_GIT_DIFF=true`; verify only a diff stat is sent. Leave it unset and verify no git stat is collected.
4. Fresh clone and run `pi install ./pi-jev`; verify the extension loads.
5. Run `npm run typecheck` and `npm test`; both must pass.
