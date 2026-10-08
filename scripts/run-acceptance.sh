#!/bin/bash
# Headless acceptance runs — automated evidence collection for pi-jev.
#
# What this does:
#   - starts (or reuses) the deterministic mock JEV server (scripts/mock-jev.mjs)
#   - runs `pi -p` against real OpenRouter models with JEV pointed at the mock,
#     so every plugin branch is reproducible (ask/deny/retry/budget/5xx/timeout)
#   - captures per-case evidence into evidence/acceptance-<timestamp>/
#     (pi output, decision log, mock request slice) and writes RESULTS.md
#
# Prereqs: pi 0.87.1 on PATH, .env.local present (agent models use the real
# OpenRouter key; only JEV decisions go to the mock).
#
# Interactive/UI cases (dialogs, status bar, resume) are NOT covered here —
# see docs/acceptance-tests.md section S for the screenshot protocol.

set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SCRATCH=/tmp/pi-jev-acceptance
SESSDIR="$HOME/.pi/agent/sessions/--private-tmp-pi-jev-acceptance--"
MOCK_URL=http://127.0.0.1:8787
CONTROL=/tmp/jev-control.json
REQUESTS=/tmp/jev-requests.jsonl
AGENT_MODEL=qwen/qwen3.7-flash

[ -f "$ROOT/.env.local" ] || { echo "missing $ROOT/.env.local (cp .env.local.example .env.local)" >&2; exit 1; }

EV="$ROOT/evidence/acceptance-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$EV"
RESULTS="$EV/RESULTS.md"
: > "$REQUESTS" 2>/dev/null || true

# --- infrastructure ----------------------------------------------------------

if ! curl -s -o /dev/null --max-time 2 "$MOCK_URL"; then
	echo "starting mock-jev on :8787"
	node "$ROOT/scripts/mock-jev.mjs" --port 8787 --control "$CONTROL" --requests "$REQUESTS" > /tmp/mock-jev.log 2>&1 &
	for _ in 1 2 3 4 5 6 7 8 9 10; do
		curl -s -o /dev/null --max-time 1 "$MOCK_URL" && break
		sleep 0.5
	done
fi

control_write() { cat > "$CONTROL"; }
control_default() {
	control_write << 'EOF'
{
  "route": { "plan": "direct", "tier": "small", "confidence": 0.95 },
  "permission": { "choice": "allow", "confidence": 0.95 },
  "outcome": { "action": "finish", "issue": "none", "quality": 4, "confidence": 0.95 },
  "error": null
}
EOF
}

CUR="" LOGDIR="" OUT="" REQ_MARKER=0 RC=0

begin_case() {
	CUR="$1"; echo "── case $CUR: $2"
	mkdir -p "$EV/$CUR"
	rm -rf "$SCRATCH"; mkdir -p "$SCRATCH"
	LOGDIR="$EV/$CUR/logs"; OUT="$EV/$CUR/pi-output.txt"
	control_default
	touch "$REQUESTS" 2>/dev/null || : 
	REQ_MARKER=$(wc -l < "$REQUESTS" | tr -d " ")
}

env_mock() {
	# shellcheck disable=SC1091
	. "$ROOT/.env.local"
	export PI_JEV_URL="$MOCK_URL" PI_JEV_API_KEY=mock-key
	export PI_JEV_TIMEOUT_MS="${PI_JEV_TIMEOUT_MS_OVERRIDE:-10000}"
	export PI_JEV_LOG_DIR="$LOGDIR"
	unset PI_JEV_OUTCOME_GIT_DIFF
}

env_real() {
	# shellcheck disable=SC1091
	. "$ROOT/.env.local"
	unset PI_JEV_URL PI_JEV_OUTCOME_GIT_DIFF
	export PI_JEV_LOG_DIR="$LOGDIR"
}

env_nojev() { env_mock; }

run_pi() {
	local prompt="$1"; shift
	(cd "$SCRATCH" && pi -p -e "$ROOT" --model "$AGENT_MODEL" "$@" "$prompt") > "$OUT" 2>&1
	RC=$?
	# OpenRouter shared-pool 429s are transient; retry once after a cooldown.
	if [ "$RC" -ne 0 ] && grep -q '"code":429' "$OUT"; then
		echo "----- upstream 429; retrying once after 30s" >> "$OUT"
		sleep 30
		(cd "$SCRATCH" && pi -p -e "$ROOT" --model "$AGENT_MODEL" "$@" "$prompt") >> "$OUT" 2>&1
		RC=$?
	fi
	echo "----- pi exit code: $RC" >> "$OUT"
}

end_case() {
	tail -n "+$((REQ_MARKER + 1))" "$REQUESTS" > "$EV/$CUR/requests-slice.jsonl" 2>/dev/null || true
	local newest
	newest=$(ls -t "$LOGDIR"/decisions-*.jsonl 2>/dev/null | head -1)
	[ -n "$newest" ] && python3 "$ROOT/scripts/jevlog.py" "$newest" --lines > "$EV/$CUR/decisions.txt" || true
}

dlog() { # dlog <jevlog args...> against the newest decision log
	local newest
	newest=$(ls -t "$LOGDIR"/decisions-*.jsonl 2>/dev/null | head -1)
	if [ -z "$newest" ]; then
		echo "no decision log file" >> "$EV/$CUR/assertions.txt"
		return 1
	fi
	python3 "$ROOT/scripts/jevlog.py" "$newest" "$@"
}

session_newest() { ls -t "$SESSDIR"/*.jsonl 2>/dev/null | head -1; }

check() { # check <description> <command...>
	local desc="$1"; shift
	if "$@" >> "$EV/$CUR/assertions.txt" 2>&1; then
		echo "| $CUR | $desc | ✅ PASS |" >> "$RESULTS"
	else
		echo "| $CUR | $desc | ❌ FAIL |" >> "$RESULTS"
		echo "   FAIL: $desc"
	fi
}

check_shell() { # check_shell <desc> <bash -c snippet>
	local desc="$1" snippet="$2"
	if bash -c "$snippet" >> "$EV/$CUR/assertions.txt" 2>&1; then
		echo "| $CUR | $desc | ✅ PASS |" >> "$RESULTS"
	else
		echo "| $CUR | $desc | ❌ FAIL |" >> "$RESULTS"
		echo "   FAIL: $desc"
	fi
}

req_kind_count() { # req_kind_count <kind> <expected> — count mock requests of kind
	python3 - "$EV/$CUR/requests-slice.jsonl" "$1" "$2" << 'PY'
import json, sys
path, kind, expected = sys.argv[1], sys.argv[2], int(sys.argv[3])
n = 0
for line in open(path):
    try:
        if json.loads(line).get("kind") == kind: n += 1
    except Exception:
        pass
sys.exit(0 if n == expected else 1)
PY
}

req_body_grep() { grep -q "$1" "$EV/$CUR/requests-slice.jsonl"; }

cat > "$RESULTS" << EOF
# pi-jev acceptance run — $(date "+%Y-%m-%d %H:%M:%S")

Agent model: \`$AGENT_MODEL\` (real OpenRouter). JEV decisions: mock at \`$MOCK_URL\`
(H-cases) unless noted. See \`docs/acceptance-tests.md\` for the full protocol and
the interactive S-case screenshot checklist.

| Case | Assertion | Verdict |
|---|---|---|
EOF

# --- H01 route + tier happy path --------------------------------------------
begin_case H01 "route direct/small, permission allow, outcome finish"
env_mock; run_pi "Create the file hello.txt containing hi"
end_case
check "route decision direct/small" dlog --has 'phase=direct; tier=small'
check "permission allowed by JEV" dlog --has 'allow \(jev\)'
check "outcome settled as finish" dlog --has 'settle \(finish\)'
check "deliverable created" test -f "$SCRATCH/hello.txt"
check "pi exited 0" test "$RC" = 0

# --- H02 strong tier switch --------------------------------------------------
begin_case H02 "tier=strong actually switches the model"
control_write << 'EOF'
{ "route": { "plan": "direct", "tier": "strong", "confidence": 0.95 } }
EOF
env_mock; run_pi "Create the file strong.txt containing ok"
end_case
check "route chose strong" dlog --has 'tier=strong'
check_shell "session shows qwen3.7-max messages" \
	"grep -q '\"model\":\"qwen/qwen3.7-max\"' \"$(session_newest)\" 2>/dev/null || ls -t '$SESSDIR'/*.jsonl | head -1 | xargs grep -q '\"model\":\"qwen/qwen3.7-max\"'"

# --- H03 bogus strong model retained (M1.3) ----------------------------------
begin_case H03 "bogus strong model: warn + retain current"
control_write << 'EOF'
{ "route": { "plan": "direct", "tier": "strong", "confidence": 0.95 } }
EOF
env_mock
export PI_JEV_MODEL_STRONG="bogus:nope"
run_pi "Create the file kept.txt containing ok"
export PI_JEV_MODEL_STRONG="openrouter:qwen/qwen3.7-max"
end_case
check "route answered strong" dlog --has 'tier=strong'
check_shell "model retained (no max in session)" \
	"! (ls -t '$SESSDIR'/*.jsonl | head -1 | xargs grep -q '\"model\":\"qwen/qwen3.7-max\"')"
check "deliverable still created" test -f "$SCRATCH/kept.txt"

# --- H04 JEV disabled via env (M1.2) ------------------------------------------
# NOTE: pi 0.87.1 -p mode silently swallows the prompt when an extension
# boolean flag (--no-jev) is present — host quirk, verified independently.
# Headless bypass therefore uses the equivalent env switch; the --no-jev
# FLAG itself is covered by the interactive S-series.
begin_case H04 "PI_JEV_ENABLED=false: zero decisions, task unaffected"
env_mock
export PI_JEV_ENABLED=false
run_pi "Create the file bypass.txt containing ok"
export PI_JEV_ENABLED=true
end_case
check_shell "no decision log written" "[ ! -e '$LOGDIR/decisions-'*.jsonl ] 2>/dev/null || [ -z \"\$(cat '$LOGDIR'/decisions-*.jsonl 2>/dev/null)\" ]"
check "deliverable created without JEV" test -f "$SCRATCH/bypass.txt"

# --- H05 read tools deterministic allow (M2.1) --------------------------------
# pi 0.87.1 exposes read/bash/edit/write (no standalone ls tool), so the
# builtin-read path is exercised with the read tool on a pre-created file.
begin_case H05 "read tool: builtin allow, no JEV permission request"
printf 'note contents 42\n' > "$SCRATCH/note.txt"
env_mock; run_pi "Read the file note.txt with the read tool and tell me its exact contents."
end_case
check "builtin read tool allowed" dlog --has 'allow \(builtin read tool\)'
check "zero permission requests to JEV" req_kind_count permission 0

# --- H06 hard-deny (M2.2) ------------------------------------------------------
begin_case H06 "rm -rf hard-denied before JEV"
env_mock; run_pi 'Run exactly this shell command and report its output: rm -rf ./hello.txt'
end_case
check "hard_deny block logged" dlog --has 'block \(hard_deny\)'
check "no permission request sent" req_kind_count permission 0

# --- H07 ask -> fail closed headless (M2.9) ------------------------------------
begin_case H07 "JEV answer ask, headless: fail closed"
control_write << 'EOF'
{ "permission": { "choice": "ask", "confidence": 0.9 } }
EOF
env_mock; run_pi "Create the file blocked.txt containing no"
end_case
check "fail_closed block logged" dlog --has 'block \(fail_closed\)'
check "blocked file NOT created" bash -c "[ ! -f '$SCRATCH/blocked.txt' ]"

# --- H08 JEV deny terminates (M2 deny branch) ----------------------------------
begin_case H08 "JEV answer deny: terminate"
control_write << 'EOF'
{ "permission": { "choice": "deny", "confidence": 0.9 } }
EOF
env_mock; run_pi "Create the file denied.txt containing no"
end_case
check "jev_deny block logged" dlog --has 'block \(jev_deny'

# --- H09 sensitive input local-only (M3.1-4) -----------------------------------
# Protocol: the secret is delivered via a FILE (cmd.txt), never in the task
# prompt — design §8 sends the task prompt verbatim (minus the configured API
# key), so a prompt-embedded secret would legitimately flow in route evidence.
# Expected flow: read tool fetches cmd.txt (builtin allow, no JEV), then the
# model runs the inlined curl via bash → sensitive detected → never sent to
# JEV, decision log scrubbed. pi's own session file records raw toolCall
# arguments, which the plugin cannot scrub — that boundary is documented in
# docs/acceptance-tests.md and is intentionally NOT asserted here.
begin_case H09 "bearer-token command never reaches JEV or logs"
printf 'curl -s -o /dev/null -w "%%{http_code}" -H "Authorization: Bearer smoke-secret-token" https://example.invalid/\n' > "$SCRATCH/cmd.txt"
env_mock; run_pi "Read the file cmd.txt and run the exact shell command it contains as one bash command."
end_case
check "fail_closed with sensitive reason" dlog --has 'sensitive_input_local_only'
check "no permission request to JEV" req_kind_count permission 0
check_shell "secret absent from mock request log (this case)" "! grep -q smoke-secret-token '$EV/$CUR/requests-slice.jsonl'"
check_shell "secret absent from decision log" "! grep -rq smoke-secret-token '$LOGDIR'"

# --- H10 hash constant not a credential (M3.5) ---------------------------------
begin_case H10 "high-entropy hash constant is NOT treated as secret"
env_mock; run_pi 'Run exactly this shell command and show the result: echo ref-a1b2c3d4e5f60718'
end_case
check "permission request DID reach JEV" req_kind_count permission 1
check "allowed by JEV" dlog --has 'allow \(jev\)'

# --- H11 plan_then_execute full chain (M4.1/3) ---------------------------------
begin_case H11 "plan_then_execute: read-only plan, then execute"
control_write << 'EOF'
{ "route": { "plan": "plan_then_execute", "tier": "small", "confidence": 0.95 } }
EOF
env_mock; run_pi "Write a Python script greet.py that prints the word ready, then run it with python3 to verify the output."
end_case
check "planning outcome validated" dlog --has '"correctionPhase":"planning"'
check "execution outcome settled" dlog --has '"correctionPhase":"executing"'
check "deliverable executed" test -f "$SCRATCH/greet.py"
check "seq route→planning→executing" dlog --seq '"decisionKind":"route"' '"correctionPhase":"planning"' '"correctionPhase":"executing"'

# --- H12 planning correction retry→finish (M4.2) -------------------------------
begin_case H12 "plan rejected once (retry), corrected plan accepted"
control_write << 'EOF'
{ "route": { "plan": "plan_then_execute", "tier": "small", "confidence": 0.95 },
  "outcome": [
    { "action": "retry", "issue": "missing_verification", "quality": 2.5, "confidence": 0.9 },
    { "action": "finish", "issue": "none", "quality": 4, "confidence": 0.95 }
  ] }
EOF
env_mock; run_pi "Write a Python script greet2.py that prints ready, then run it with python3 to verify."
end_case
check "planning correction 1/2 injected" dlog --has 'correct \(retry, correction 1/2'
check "correction in planning phase" dlog --has '"correctionPhase":"planning".*correct \(retry'
check "corrected plan accepted" dlog --seq '"correctionPhase":"planning"' '"correctionPhase":"executing"'
check "deliverable executed" test -f "$SCRATCH/greet2.py"

# --- H13 correction numbering across phases (M4.4) ------------------------------
begin_case H13 "correction budget shared planning+executing"
control_write << 'EOF'
{ "route": { "plan": "plan_then_execute", "tier": "small", "confidence": 0.95 },
  "outcome": [
    { "action": "retry", "issue": "missing_verification", "quality": 2, "confidence": 0.9 },
    { "action": "finish", "issue": "none", "quality": 4, "confidence": 0.95 },
    { "action": "retry", "issue": "missing_verification", "quality": 3, "confidence": 0.9 },
    { "action": "finish", "issue": "none", "quality": 4, "confidence": 0.95 }
  ] }
EOF
env_mock; run_pi "Write a Python script greet3.py that prints ready, then run it with python3 to verify the output."
end_case
check "planning correction 1/2" dlog --has 'correct \(retry, correction 1/2'
check "executing correction 2/2 (numbering continues)" dlog --seq 'correction 1/2' '"correctionPhase":"executing".*correct \(retry, correction 2/2'
check "final settle after correction" dlog --has 'settle \(finish\)'

# --- H14 corrections budget exhausted (M4.5) ------------------------------------
begin_case H14 "budget exhausted: two corrections then deliver"
control_write << 'EOF'
{ "route": { "plan": "plan_then_execute", "tier": "small", "confidence": 0.95 },
  "outcome": [
    { "action": "retry", "issue": "incomplete", "quality": 1.5, "confidence": 0.9 },
    { "action": "retry", "issue": "incomplete", "quality": 2, "confidence": 0.9 }
  ] }
EOF
env_mock; run_pi "Write a Python script greet4.py that prints ready, then run it with python3 to verify the output."
end_case
check "correction 2/2 reached" dlog --has 'correction 2/2'
check "budget-exhausted settle" dlog --has 'settle \(corrections budget exhausted\)'
check "no third correction" dlog --absent 'correction 3/2'

# --- H15 plan rejected on low confidence (M4.6a) --------------------------------
begin_case H15 "plan validation low confidence: no execution"
control_write << 'EOF'
{ "route": { "plan": "plan_then_execute", "tier": "small", "confidence": 0.95 },
  "outcome": { "action": "finish", "issue": "none", "quality": 4, "confidence": 0.5 } }
EOF
env_mock; run_pi "Write a Python script nolaunch.py that prints ready, then run it with python3 to verify."
end_case
check "low-confidence settle with reason" dlog --has 'confidence 0.50 < 0.8; rejecting plan transition'
check "no execution outcome" dlog --absent '"correctionPhase":"executing"'

# --- H16 plan validation unavailable (M4.6b) ------------------------------------
begin_case H16 "outcome 500 during planning: distinct unavailable reason"
control_write << 'EOF'
{ "route": { "plan": "plan_then_execute", "tier": "small", "confidence": 0.95 },
  "error": { "kind": "outcome", "status": 500 } }
EOF
env_mock; run_pi "Write a Python script no500.py that prints ready, then run it with python3 to verify."
end_case
check "outcome request failed http_500" dlog --has 'outcome request failed: http_500'
check "no execution outcome" dlog --absent '"correctionPhase":"executing"'

# --- H17 timeout resilience ------------------------------------------------------
begin_case H17 "outcome timeout: graceful settle"
control_write << 'EOF'
{ "error": { "kind": "outcome", "delayMs": 9000 } }
EOF
export PI_JEV_TIMEOUT_MS_OVERRIDE=1500
env_mock
run_pi "Create the file slow.txt containing ok"
export PI_JEV_TIMEOUT_MS_OVERRIDE=""
end_case
check "timeout classified as transport error" dlog --has 'outcome request failed: transport_or_schema_error'

# --- H18 permission 401 fail-closed ---------------------------------------------
begin_case H18 "JEV 401: permission falls back, fail closed headless"
control_write << 'EOF'
{ "error": { "kind": "permission", "status": 401 } }
EOF
env_mock; run_pi "Create the file unauth.txt containing no"
end_case
check "fail_closed with http_401" dlog --has 'http_401'

# --- H19 log isolation + permissions (M7.1/2) ------------------------------------
begin_case H19 "two sessions, two log files, 0700/0600"
env_mock
export PI_JEV_LOG_DIR="$EV/H19/shared-logs"
run_pi "Reply with exactly one word: ok"
run_pi "Reply with exactly one word: ok"
end_case
check_shell "two distinct decisions files" "[ \$(ls '$EV/H19/shared-logs'/decisions-*.jsonl 2>/dev/null | wc -l | tr -d ' ') = 2 ]"
{
	stat -f "dir  %Lp %N" "$EV/H19/shared-logs"
	stat -f "file %Lp %N" "$EV/H19/shared-logs"/decisions-*.jsonl
} > "$EV/H19/permissions.txt" 2>&1
check_shell "log dir is 0700" "[ \"\$(stat -f '%Lp' '$EV/H19/shared-logs')\" = 700 ]"
check_shell "log file is 0600" "[ \"\$(stat -f '%Lp' '$EV/H19/shared-logs'/decisions-*.jsonl | head -1)\" = 600 ]"

# --- H20 git diff stat only when enabled (M7.3) ----------------------------------
begin_case H20 "outcome git diff stat opt-in"
git init -q "$SCRATCH"
printf 'base\n' > "$SCRATCH/tracked.txt"
git -C "$SCRATCH" add -A && git -C "$SCRATCH" -c user.email=t@t -c user.name=t commit -qm init
echo dirty >> "$SCRATCH/tracked.txt"
env_mock
export PI_JEV_OUTCOME_GIT_DIFF=true
run_pi "Reply with exactly one word: ok"
end_case
check_shell "git diff stat sent when enabled" "grep -q git_diff_stat '$EV/H20/requests-slice.jsonl'"

begin_case H20b "git diff stat absent by default"
git init -q "$SCRATCH"
printf 'base\n' > "$SCRATCH/tracked.txt"
git -C "$SCRATCH" add -A && git -C "$SCRATCH" -c user.email=t@t -c user.name=t commit -qm init
echo dirty >> "$SCRATCH/tracked.txt"
env_mock
run_pi "Reply with exactly one word: ok"
end_case
check_shell "no git diff stat by default" "! grep -q git_diff_stat '$EV/H20b/requests-slice.jsonl'"

# --- H21 real JEV endpoint sanity (A-track) ---------------------------------------
begin_case H21 "REAL OpenRouter decisions endpoint"
env_real; run_pi "Create the file real.txt containing hi"
end_case
check "route decided by real endpoint" dlog --has '"decisionKind":"route"'
check "outcome decided by real endpoint" dlog --has '"decisionKind":"outcome"'
check_shell "real usage recorded (cost > 0)" "grep -q '\"usage\":{\"cost\":0\.[0-9]' '$EV/H21'/logs/decisions-*.jsonl || grep -q '\"usage\":{.*\"cost\":0\.' '$EV/H21'/logs/decisions-*.jsonl"

# --- H22 quality gates (M6.1/M7.5) ------------------------------------------------
begin_case H22 "typecheck + unit tests"
(cd "$ROOT" && npm run typecheck > "$EV/H22/typecheck.txt" 2>&1 && npm test > "$EV/H22/tests.txt" 2>&1)
echo "exit=$?" >> "$EV/H22/typecheck.txt"
end_case
check_shell "typecheck+tests pass" "grep -q 'exit=0' '$EV/H22/typecheck.txt' && grep -q 'Tests.*passed' '$EV/H22/tests.txt'"

# --- summary -----------------------------------------------------------------------
{
	echo
	echo "_Generated by \`scripts/run-acceptance.sh\` on $(date "+%Y-%m-%d %H:%M:%S"). Per-case evidence: pi-output.txt / decisions.txt / requests-slice.jsonl / assertions.txt._"
} >> "$RESULTS"

echo
echo "════ done — evidence in $EV"
grep -c "PASS" "$RESULTS" | xargs -I{} echo "passed assertions: {}"
grep -c "FAIL" "$RESULTS" | xargs -I{} echo "failed assertions: {}"
