import { describe, expect, it } from "vitest"
import {
	consumeCorrection,
	correctionsRemaining,
	createTaskState,
	newTaskId,
	restoreState,
	toExecutingPhase,
	toPlanOnlyPhase,
	toPlanPhase,
	type JevState,
} from "../src/state.ts"

function sampleState(): JevState {
	return {
		schemaVersion: 1,
		taskId: "t-1",
		originalPrompt: "fix the login bug",
		phase: "direct",
		tier: "normal",
		correctionsUsed: 0,
	}
}

describe("state machine", () => {
	it("creates unique task ids", () => {
		const ids = new Set(Array.from({ length: 100 }, () => newTaskId()))
		expect(ids.size).toBe(100)
	})

	it("createTaskState starts in direct with the given tier and optional model ref", () => {
		const state = createTaskState("do it", "strong", "anthropic:claude-opus")
		expect(state.phase).toBe("direct")
		expect(state.tier).toBe("strong")
		expect(state.correctionsUsed).toBe(0)
		expect(state.lastSetModelId).toBe("anthropic:claude-opus")
		expect(createTaskState("x", "small").lastSetModelId).toBeUndefined()
	})

	it("transitions direct → planning/plan_only with a tool snapshot", () => {
		const base = sampleState()
		expect(toPlanPhase(base, "plan_then_execute", ["read", "edit"]).phase).toBe("planning")
		expect(toPlanPhase(base, "plan_only", ["read", "edit"]).phase).toBe("plan_only")
		expect(toPlanPhase(base, "plan_then_execute", ["read", "edit"]).toolsBeforePlan).toEqual(["read", "edit"])
	})

	it("transitions planning → executing and keeps the snapshot for resume", () => {
		const planning = toPlanPhase(sampleState(), "plan_then_execute", ["read", "edit"])
		const executing = toExecutingPhase(planning)
		expect(executing.phase).toBe("executing")
		expect(executing.toolsBeforePlan).toEqual(["read", "edit"])
		// original task identity is preserved across the transition
		expect(executing.taskId).toBe(planning.taskId)
	})

	it("degrades planning to plan_only", () => {
		const planning = toPlanPhase(sampleState(), "plan_then_execute", ["read"])
		expect(toPlanOnlyPhase(planning).phase).toBe("plan_only")
	})

	it("consumes corrections and reports the remaining budget", () => {
		let state = sampleState()
		expect(correctionsRemaining(state, 2)).toBe(2)
		state = consumeCorrection(state)
		expect(state.correctionsUsed).toBe(1)
		expect(correctionsRemaining(state, 2)).toBe(1)
		state = consumeCorrection(state)
		expect(correctionsRemaining(state, 2)).toBe(0)
	})

	it("restores valid persisted state", () => {
		const state = toPlanPhase({ ...sampleState(), correctionsUsed: 1 }, "plan_then_execute", ["read", "edit"])
		const restored = restoreState(JSON.parse(JSON.stringify(state)))
		expect(restored).toEqual(state)
	})

	it("rejects malformed persisted state", () => {
		expect(restoreState(undefined)).toBeUndefined()
		expect(restoreState(null)).toBeUndefined()
		expect(restoreState({ taskId: "t" })).toBeUndefined()
		expect(restoreState({ ...sampleState(), phase: "vibing" })).toBeUndefined()
		expect(restoreState({ ...sampleState(), tier: "ultra" })).toBeUndefined()
		expect(restoreState({ ...sampleState(), correctionsUsed: -1 })).toBeUndefined()
		expect(restoreState({ ...sampleState(), correctionsUsed: 1.5 })).toBeUndefined()
		expect(restoreState({ ...sampleState(), toolsBeforePlan: "read" })).toBeUndefined()
		expect(restoreState({ ...sampleState(), lastSetModelId: 42 })).toBeUndefined()
	})
})
