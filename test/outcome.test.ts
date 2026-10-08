import { describe, expect, it, vi } from "vitest"
import type { JevConfig } from "../src/config.ts"
import type { JevResponse } from "../src/client.ts"
import { JevError } from "../src/client.ts"
import type { DecisionLogEntry } from "../src/log.ts"
import { correctionMessage, judgeOutcome, type OutcomeTaskContext } from "../src/outcome.ts"
import type { EvidenceContextMessage } from "../src/evidence.ts"

const CONFIG: JevConfig = {
	url: "https://decisions.example.com",
	apiKey: "sk-secret-0123456789",
	model: "typesafe/jev-1.13",
	timeoutMs: 3000,
	models: { small: "a:s", normal: "a:n", strong: "a:g" },
	routeConfidence: 0.8,
	permissionConfidence: 0.9,
	outcomeConfidence: 0.8,
	maxCorrections: 2,
	outcomeGitDiff: false,
	planAutoExecute: true,
	logDir: "/tmp/.pi-jev",
}

const MESSAGES: EvidenceContextMessage[] = [
	{ role: "user", content: "make the tests pass" },
	{
		role: "toolResult",
		toolName: "bash",
		isError: true,
		content: [{ type: "text", text: "2 tests failed" }],
	},
]

function taskContext(overrides: Partial<OutcomeTaskContext> = {}): OutcomeTaskContext {
	return {
		taskId: "t-42",
		originalPrompt: "make the tests pass",
		phase: "executing",
		correctionsUsed: 0,
		contextMessages: MESSAGES,
		approvedActions: [],
		...overrides,
	}
}

function outcomeResponse(action: string, confidence: number, issue = "test_failure", quality = 2): JevResponse {
	return {
		answers: {
			action: { type: "choice", choice: action, confidence, probabilities: { finish: 0, retry: 1, replan: 0 } },
			issue: {
				type: "choice",
				choice: issue,
				confidence: 0.9,
				probabilities: Object.fromEntries(
					["none", "incomplete", "test_failure", "missing_verification", "wrong_approach", "insufficient_context"].map((k) => [k, 0]),
				),
			},
			quality: {
				type: "score",
				score: quality,
				confidence: 0.9,
				probabilities: { 0: 0, 1: 0, 2: 1, 3: 0, 4: 0 },
			},
		},
		model: "typesafe/jev-1.13",
		usage: { cost: 0.001 },
		latencyMs: 20,
	}
}

function deps(overrides: Partial<Parameters<typeof judgeOutcome>[1]> = {}) {
	return {
		config: CONFIG,
		decide: vi.fn(async (_request: unknown) => outcomeResponse("finish", 0.95)),
		getGitDiffStat: vi.fn(async () => undefined),
		onLog: vi.fn(),
		...overrides,
	}
}

describe("judgeOutcome", () => {
	it("settles naturally on finish", async () => {
		const d = deps()
		const verdict = await judgeOutcome(taskContext(), d)
		expect(verdict).toEqual({ kind: "settle", reason: "finish" })
	})

	it("returns a distinct low-confidence reason and rejects a planning transition", async () => {
		const d = deps({ decide: vi.fn(async () => outcomeResponse("retry", 0.5)) })
		const verdict = await judgeOutcome(taskContext({ phase: "planning" }), d)
		expect(verdict).toEqual({ kind: "settle", reason: "low_confidence" })
		expect(d.onLog).toHaveBeenCalledWith(
			expect.objectContaining({ effective: expect.stringContaining("rejecting plan transition") }),
		)
	})

	it("corrects and continues on retry within budget", async () => {
		const d = deps({ decide: vi.fn(async (_request: unknown) => outcomeResponse("retry", 0.9)) })
		const verdict = await judgeOutcome(taskContext(), d)
		expect(verdict.kind).toBe("correct")
		if (verdict.kind === "correct") {
			expect(verdict.correctionsUsed).toBe(1)
			expect(verdict.message).toContain("[jev correction 1/2; task t-42]")
			expect(verdict.message).toContain("policy selected retry; issue: test_failure")
			expect(verdict.message).toContain("quality: 2/4")
			// failed verification summary from the tool evidence
			expect(verdict.message).toContain("failing checks:")
			expect(verdict.message).toContain("- bash:")
			expect(verdict.message).toContain("2 tests failed")
		}
	})

	it("asks all three questions with redacted evidence", async () => {
		const decide = vi.fn(async (_request: unknown) => outcomeResponse("finish", 0.9))
		const d = deps({ decide, config: { ...CONFIG, outcomeGitDiff: true } })
		await judgeOutcome(taskContext(), d)
		const request = decide.mock.calls[0]![0] as { questions: Record<string, unknown>; state: unknown }
		expect(Object.keys(request.questions)).toEqual(["action", "issue", "quality"])
		const state = JSON.stringify(request.state)
		expect(state).toContain("make the tests pass")
		expect(state).toContain("failed")
	})

	it("stops correcting when the budget is exhausted", async () => {
		const d = deps({ decide: vi.fn(async () => outcomeResponse("replan", 0.9)) })
		const verdict = await judgeOutcome(taskContext({ correctionsUsed: 2 }), d)
		expect(verdict.kind).toBe("budget_exhausted")
		if (verdict.kind === "budget_exhausted") {
			expect(verdict.message).toContain("budget exhausted (2/2)")
			expect(verdict.message).toContain("policy selected replan")
		}
	})

	it("honors a zero-corrections budget", async () => {
		const d = deps({
			config: { ...CONFIG, maxCorrections: 0 },
			decide: vi.fn(async () => outcomeResponse("retry", 0.9)),
		})
		const verdict = await judgeOutcome(taskContext(), d)
		expect(verdict.kind).toBe("budget_exhausted")
	})

	it("settles when the JEV request fails", async () => {
		const d = deps({
			decide: vi.fn(async () => {
				throw new JevError("http_503")
			}),
		})
		const verdict = await judgeOutcome(taskContext(), d)
		expect(verdict).toEqual({ kind: "settle", reason: "unavailable" })
		expect(d.onLog).toHaveBeenCalledWith(expect.objectContaining({ error: "http_503" }))
	})

	it("returns a distinct reason for incomplete answers", async () => {
		const incomplete = outcomeResponse("finish", 0.9)
		delete incomplete.answers["quality"]
		const verdict = await judgeOutcome(taskContext(), deps({ decide: vi.fn(async () => incomplete) }))
		expect(verdict).toEqual({ kind: "settle", reason: "invalid_answers" })
	})

	it("refuses judgment when evidence exceeds the context cap", async () => {
		const d = deps()
		// approved operations are the unbounded part (each capped at 200 chars,
		// but not count-limited) — enough of them cross the 96000-char cap.
		const manyApprovals = Array.from({ length: 700 }, () => `bash ${"x".repeat(190)}`)
		const verdict = await judgeOutcome(taskContext({ approvedActions: manyApprovals }), d)
		expect(verdict).toEqual({ kind: "settle", reason: "context_too_large" })
		expect(d.decide).not.toHaveBeenCalled()
		expect(d.onLog).toHaveBeenCalledWith(expect.objectContaining({ error: "context_too_large" }))
	})

	it("attaches git diff evidence when enabled and available", async () => {
		const decide = vi.fn(async (_request: unknown) => outcomeResponse("finish", 0.9))
		const d = deps({ decide, config: { ...CONFIG, outcomeGitDiff: true }, getGitDiffStat: vi.fn(async () => " src/a.ts | 2 +-") })
		await judgeOutcome(taskContext(), d)
		const request = decide.mock.calls[0]![0] as { state: unknown }
		expect(JSON.stringify(request.state)).toContain("src/a.ts")
	})

	it("tolerates a failing git diff", async () => {
		const decide = vi.fn(async (_request: unknown) => outcomeResponse("finish", 0.9))
		const d = deps({
			decide,
			config: { ...CONFIG, outcomeGitDiff: true },
			getGitDiffStat: vi.fn(async () => {
				throw new Error("no git")
			}),
		})
		const verdict = await judgeOutcome(taskContext(), d)
		expect(verdict.kind).toBe("settle")
		const request = decide.mock.calls[0]![0] as { state: unknown }
		expect(JSON.stringify(request.state)).not.toContain("git_diff_stat")
	})

	it("logs answers, latency and usage for a judged outcome", async () => {
		const onLog = vi.fn()
		const d = deps({ onLog })
		await judgeOutcome(taskContext(), d)
		const entry = onLog.mock.calls[0]![0] as Omit<DecisionLogEntry, "timestamp">
		expect(entry.decisionKind).toBe("outcome")
		expect(entry.correctionPhase).toBe("executing")
		expect(entry.answers).toMatchObject({
			action: { value: "finish" },
			quality: { value: 2 },
		})
		expect(entry.usage).toEqual({ cost: 0.001 })
	})
})

describe("correctionMessage", () => {
	it("follows the migrated wording structure", () => {
		const message = correctionMessage("t-9", 1, 2, {
			action: "replan",
			issue: "wrong_approach",
			quality: 1,
			confidence: 0.9,
		})
		expect(message).toContain("[jev correction 1/2; task t-9] policy selected replan; issue: wrong_approach")
		expect(message).toContain("fundamentally wrong")
		expect(message).toContain("quality: 1/4")
		expect(message).not.toContain("failing checks:")
	})

	it("appends the failed-verification summary when checks exist", () => {
		const message = correctionMessage(
			"t-9",
			2,
			2,
			{ action: "retry", issue: "test_failure", quality: 2, confidence: 0.9 },
			["bash: 2 tests failed", "npm test: exit 1"],
		)
		expect(message).toContain("failing checks:")
		expect(message).toContain("- bash: 2 tests failed")
		expect(message).toContain("- npm test: exit 1")
	})
})
