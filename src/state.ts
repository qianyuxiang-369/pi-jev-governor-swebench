/**
 * Task state machine (docs/design.md §3).
 *
 * One task spans one or more agent runs:
 *
 *   before_agent_start ── route ──▶ direct | plan_only | planning
 *   planning settle ── JEV outcome ──▶ revise | executing | finish
 *   direct/executing/plan_only settle ── JEV outcome ──▶ finish | retry | replan
 *
 * Route state is persisted via `pi.appendEntry("jev-state", ...)`; settle
 * state is returned as boundary entries. The active branch is restored on
 * session_start/session_tree, so correctionsUsed survives resume/navigation.
 */

import type { Tier } from "./questions.ts"

export type JevPhase = "direct" | "planning" | "executing" | "plan_only"

/** Canonical tier union lives in questions.ts; re-exported for state consumers. */
export type JevTier = Tier

export interface JevState {
	schemaVersion: 1
	taskId: string
	originalPrompt: string
	phase: JevPhase
	tier: JevTier
	correctionsUsed: number
	/** Active-tool snapshot taken when entering a read-only plan phase. */
	toolsBeforePlan?: string[]
	/** `${provider}:${id}` of the model the router last set, for manual-override detection. */
	lastSetModelId?: string
}

export const JEV_STATE_CUSTOM_TYPE = "jev-state"

const PHASES: readonly JevPhase[] = ["direct", "planning", "executing", "plan_only"]
const TIERS: readonly JevTier[] = ["small", "normal", "strong"]

export function newTaskId(): string {
	const random = Math.random().toString(36).slice(2, 8)
	return `t${Date.now().toString(36)}-${random}`
}

export function createTaskState(originalPrompt: string, tier: JevTier, lastSetModelId?: string): JevState {
	return {
		schemaVersion: 1,
		taskId: newTaskId(),
		originalPrompt,
		phase: "direct",
		tier,
		correctionsUsed: 0,
		...(lastSetModelId !== undefined ? { lastSetModelId } : {}),
	}
}

/** Enter a read-only plan phase; snapshot the tools to restore afterwards. */
export function toPlanPhase(
	state: JevState,
	strategy: "plan_only" | "plan_then_execute",
	toolsBeforePlan: string[],
): JevState {
	return { ...state, phase: strategy === "plan_only" ? "plan_only" : "planning", toolsBeforePlan }
}

/** Accepted planning → executing transition. Keep the tool snapshot for resume. */
export function toExecutingPhase(state: JevState): JevState {
	return { ...state, phase: "executing" }
}

/** Degrade to plan_only (used when auto-execution is unavailable). */
export function toPlanOnlyPhase(state: JevState): JevState {
	return { ...state, phase: "plan_only" }
}

export function consumeCorrection(state: JevState): JevState {
	return { ...state, correctionsUsed: state.correctionsUsed + 1 }
}

export function correctionsRemaining(state: JevState, maxCorrections: number): number {
	return Math.max(0, maxCorrections - state.correctionsUsed)
}

function isStringArray(value: unknown): value is string[] {
	return Array.isArray(value) && value.every((item) => typeof item === "string")
}

/** Type guard for state restored from a persisted `jev-state` entry. */
export function restoreState(data: unknown): JevState | undefined {
	if (typeof data !== "object" || data === null) return undefined
	const raw = data as Record<string, unknown>
	if (raw["schemaVersion"] !== undefined && raw["schemaVersion"] !== 1) return undefined
	if (typeof raw["taskId"] !== "string" || raw["taskId"] === "") return undefined
	if (typeof raw["originalPrompt"] !== "string") return undefined
	if (typeof raw["phase"] !== "string" || !PHASES.includes(raw["phase"] as JevPhase)) return undefined
	if (typeof raw["tier"] !== "string" || !TIERS.includes(raw["tier"] as JevTier)) return undefined
	if (typeof raw["correctionsUsed"] !== "number" || !Number.isInteger(raw["correctionsUsed"]) || raw["correctionsUsed"] < 0) {
		return undefined
	}
	if (raw["toolsBeforePlan"] !== undefined && !isStringArray(raw["toolsBeforePlan"])) return undefined
	if (raw["lastSetModelId"] !== undefined && typeof raw["lastSetModelId"] !== "string") return undefined
	return {
		schemaVersion: 1,
		taskId: raw["taskId"],
		originalPrompt: raw["originalPrompt"],
		phase: raw["phase"] as JevPhase,
		tier: raw["tier"] as JevTier,
		correctionsUsed: raw["correctionsUsed"],
		...(raw["toolsBeforePlan"] !== undefined ? { toolsBeforePlan: raw["toolsBeforePlan"] as string[] } : {}),
		...(raw["lastSetModelId"] !== undefined ? { lastSetModelId: raw["lastSetModelId"] as string } : {}),
	}
}
