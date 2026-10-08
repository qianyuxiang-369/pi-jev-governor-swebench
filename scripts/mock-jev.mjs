#!/usr/bin/env node
/**
 * Deterministic local JEV Decisions API for acceptance testing.
 *
 * Why: the real Decisions API is LLM-driven and non-deterministic — it cannot
 * reliably produce "ask", "deny", sequential "retry→finish" answers, or
 * 5xx/timeout/garbage conditions on demand. Pointing PI_JEV_URL at this
 * server makes every plugin branch reproducible and screenshottable.
 *
 * Behavior is scripted via a JSON control file that tests rewrite between
 * runs (no restart needed). Every received request body is appended to a
 * requests log — that log is the evidence for privacy assertions (M3):
 * tests grep it to prove what was and wasn't sent.
 *
 * Usage:
 *   node scripts/mock-jev.mjs [--port 8787] \
 *        [--control /tmp/jev-control.json] [--requests /tmp/jev-requests.jsonl]
 *
 * Control file format (all fields optional):
 * {
 *   "route":      { "plan": "direct|plan_only|plan_then_execute", "tier": "small|normal|strong",
 *                   "confidence": 0.95 },
 *   "permission": { "choice": "allow|ask|deny", "confidence": 0.9 },
 *   "outcome":    { "action": "finish|retry|replan", "issue": "none|incomplete|...",
 *                   "quality": 4, "confidence": 0.9 }
 *                 — or an ARRAY consumed in request order (last entry repeats),
 *                   for sequential scenarios like retry-then-finish,
 *   "error":      { "status": 500 } | { "delayMs": 15000 } | { "garbage": true }
 * }
 *
 * The plugin identifies a request kind by its question keys:
 *   plan_strategy+tier → route · action alone → permission · action+issue+quality → outcome.
 * Probabilities are derived from each question's own criteria so answers
 * always satisfy the plugin validator (keys exact, sum 1).
 */

import http from "node:http"
import { appendFileSync, readFileSync } from "node:fs"

const args = process.argv.slice(2)
const argOf = (name, dflt) => {
	const i = args.indexOf(`--${name}`)
	return i >= 0 ? args[i + 1] : dflt
}
const PORT = Number(argOf("port", "8787"))
const CONTROL_PATH = argOf("control", "/tmp/jev-control.json")
const REQUESTS_PATH = argOf("requests", "/tmp/jev-requests.jsonl")

const DEFAULTS = {
	route: { plan: "direct", tier: "small", confidence: 0.95 },
	permission: { choice: "allow", confidence: 0.95 },
	outcome: { action: "finish", issue: "none", quality: 4, confidence: 0.95 },
	error: null,
}

let outcomeSeq = { key: "", remaining: [] }

function loadControl() {
	let raw
	try {
		raw = JSON.parse(readFileSync(CONTROL_PATH, "utf8"))
	} catch {
		return structuredClone(DEFAULTS)
	}
	return { ...structuredClone(DEFAULTS), ...raw }
}

/**
 * Every pi run starts with a route request, so the route request is the
 * reliable "new run" signal: sequential outcome scripts restart there. This
 * prevents sequence state from leaking across pi processes when consecutive
 * runs reuse the same control file.
 */
function resetOutcomeSeq(control) {
	outcomeSeq = Array.isArray(control.outcome)
		? { key: JSON.stringify(control.outcome), remaining: [...control.outcome] }
		: { key: "", remaining: [] }
}

/** Consume the sequential outcome script; route requests already reset it. */
function outcomeAnswer(rawOutcome) {
	if (!Array.isArray(rawOutcome)) return rawOutcome
	if (outcomeSeq.remaining.length > 1) return outcomeSeq.remaining.shift()
	return outcomeSeq.remaining[0] ?? rawOutcome.at(-1)
}

function choiceAnswer(question, choice, confidence) {
	const keys = Object.keys(question.criteria)
	const probabilities = Object.fromEntries(keys.map((k) => [k, k === choice ? 1 : 0]))
	return { type: "choice", choice, confidence, probabilities }
}

function scoreAnswer(question, score, confidence) {
	const level = Math.max(0, Math.min(question.criteria.length - 1, Math.round(score)))
	const probabilities = Object.fromEntries(question.criteria.map((_, i) => [String(i), i === level ? 1 : 0]))
	return { type: "score", score, confidence, probabilities }
}

function requestKind(questions) {
	const keys = Object.keys(questions)
	if (keys.includes("plan_strategy") && keys.includes("tier")) return "route"
	if (keys.includes("action") && keys.includes("issue") && keys.includes("quality")) return "outcome"
	if (keys.includes("action")) return "permission"
	return "unknown"
}

function answersFor(questions, control) {
	const answers = {}
	if (requestKind(questions) === "route") {
		const r = control.route
		answers.plan_strategy = choiceAnswer(questions.plan_strategy, r.plan, r.confidence)
		answers.tier = choiceAnswer(questions.tier, r.tier, r.confidence)
	} else if (requestKind(questions) === "outcome") {
		const o = outcomeAnswer(control.outcome)
		answers.action = choiceAnswer(questions.action, o.action, o.confidence)
		answers.issue = choiceAnswer(questions.issue, o.issue ?? "none", o.confidence)
		answers.quality = scoreAnswer(questions.quality, o.quality ?? 4, o.confidence)
	} else if (requestKind(questions) === "permission") {
		const p = control.permission
		answers.action = choiceAnswer(questions.action, p.choice, p.confidence)
	}
	return answers
}

const server = http.createServer(async (req, res) => {
	if (req.method !== "POST") {
		res.writeHead(405).end()
		return
	}
	let body = ""
	for await (const chunk of req) body += chunk
	const control = loadControl()
	let parsed
	try {
		parsed = JSON.parse(body)
	} catch {
		parsed = null
	}
	const kind = parsed ? requestKind(parsed.questions ?? {}) : ""
	if (kind === "route") resetOutcomeSeq(control)
	appendFileSync(
		REQUESTS_PATH,
		JSON.stringify({
			ts: new Date().toISOString(),
			kind: kind || "invalid",
			error: control.error,
			body: parsed,
		}) + "\n",
	)

	// error.kind optionally targets one decision kind ("route"|"permission"|"outcome").
	const errorApplies = control.error && (!control.error.kind || control.error.kind === kind)
	if (errorApplies && control.error.delayMs) await new Promise((r) => setTimeout(r, control.error.delayMs))
	if (errorApplies && control.error.status) {
		res.writeHead(control.error.status, { "content-type": "application/json" })
		res.end("{}")
		return
	}
	if (errorApplies && control.error.garbage) {
		res.writeHead(200, { "content-type": "application/json" })
		res.end('{"answers": "garbage"}')
		return
	}

	const answers = parsed ? answersFor(parsed.questions ?? {}, control) : {}
	res.writeHead(200, { "content-type": "application/json" })
	res.end(
		JSON.stringify({
			model: "mock/jev-1",
			answers,
			usage: { cost: 0.000001, input_tokens: 10, output_tokens: 5 },
		}),
	)
})

server.listen(PORT, "127.0.0.1", () => {
	console.log(`mock-jev listening on http://127.0.0.1:${PORT}`)
	console.log(`  control:   ${CONTROL_PATH} (rewrite between runs, no restart)`)
	console.log(`  requests:  ${REQUESTS_PATH} (append-only; grep for privacy assertions)`)
})
