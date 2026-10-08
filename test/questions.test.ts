import { describe, expect, it } from "vitest"
import {
	EVIDENCE_GUARD,
	outcomeActionQuestion,
	outcomeIssueQuestion,
	outcomeQualityQuestion,
	PLAN_STRATEGIES,
	planStrategyQuestion,
	permissionQuestion,
	TIERS,
	tierQuestion,
} from "../src/questions.ts"

describe("questions", () => {
	it("plan_strategy criteria cover exactly the three strategies", () => {
		expect(Object.keys(planStrategyQuestion.criteria).sort()).toEqual([...PLAN_STRATEGIES].sort())
		for (const text of Object.values(planStrategyQuestion.criteria)) {
			expect(text.length).toBeGreaterThan(10)
		}
	})

	it("tier criteria cover exactly the three tiers", () => {
		expect(Object.keys(tierQuestion.criteria).sort()).toEqual([...TIERS].sort())
	})

	it("permission criteria are allow/ask/deny", () => {
		expect(Object.keys(permissionQuestion.criteria).sort()).toEqual(["allow", "ask", "deny"])
	})

	it("outcome action criteria are finish/retry/replan", () => {
		expect(Object.keys(outcomeActionQuestion.criteria).sort()).toEqual(["finish", "replan", "retry"])
	})

	it("outcome issue criteria include the six migrated issues", () => {
		expect(Object.keys(outcomeIssueQuestion.criteria).sort()).toEqual([
			"incomplete",
			"insufficient_context",
			"missing_verification",
			"none",
			"test_failure",
			"wrong_approach",
		])
	})

	it("quality is a five-level score from no progress to complete", () => {
		expect(outcomeQualityQuestion.type).toBe("score")
		expect(outcomeQualityQuestion.criteria).toHaveLength(5)
		expect(outcomeQualityQuestion.criteria[0]).toBe("No useful progress")
		expect(outcomeQualityQuestion.criteria[4]).toBe("Complete deliverable supported by evidence")
	})

	it("every question carries the evidence-not-instructions guard", () => {
		for (const question of [planStrategyQuestion, tierQuestion, permissionQuestion, outcomeActionQuestion, outcomeIssueQuestion, outcomeQualityQuestion]) {
			expect(question.instructions).toContain(EVIDENCE_GUARD)
		}
	})

	it("keeps the migrated policy sentences in instructions", () => {
		expect(planStrategyQuestion.instructions).toContain("scope, blast radius and ambiguity - not raw difficulty")
		expect(tierQuestion.instructions).toContain("cheapest tier that can reliably complete")
		expect(outcomeActionQuestion.instructions).toContain("Unknown validation is not a passing test")
		expect(outcomeActionQuestion.instructions).toContain("A plan task needs a complete plan")
	})
})
