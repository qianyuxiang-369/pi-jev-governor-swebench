import { mkdtemp, rm } from "node:fs/promises"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

/**
 * Wiring tests: the extension entry against a minimal ExtensionAPI stub.
 * The JEV endpoint is a stubbed global fetch; everything else is real code.
 */

type AnyRecord = Record<string, unknown>

interface CallOptions {
	plan?: string
	planConfidence?: number
	tier?: string
	tierConfidence?: number
	action?: string
	actionConfidence?: number
	issue?: string
	quality?: number
}

const ENV_KEYS = [
	"PI_JEV_API_KEY",
	"PI_JEV_MODEL_SMALL",
	"PI_JEV_MODEL_NORMAL",
	"PI_JEV_MODEL_STRONG",
	"PI_JEV_LOG_DIR",
	"PI_JEV_PLAN_AUTO_EXECUTE",
	"PI_JEV_ENABLED",
] as const

class MockApi {
	handlers = new Map<string, Array<(event: AnyRecord, ctx: AnyRecord) => Promise<unknown>>>()
	commands = new Map<string, { description?: string; handler: (args: string, ctx: AnyRecord) => Promise<void> }>()
	flagDefaults = new Map<string, boolean | string>()
	flagValues = new Map<string, boolean | string>()
	entries: AnyRecord[] = []
	sent: Array<{ message: AnyRecord; options: AnyRecord | undefined }> = []
	activeTools = ["read", "bash", "edit", "write", "grep", "find", "ls", "mcp_tool"]
	model: { provider: string; id: string } | undefined = { provider: "acme", id: "normal-model" }
	available: Array<{ provider: string; id: string }> = [
		{ provider: "acme", id: "small-model" },
		{ provider: "acme", id: "normal-model" },
		{ provider: "acme", id: "strong-model" },
	]
	scopedModels: Array<{ model: { provider: string; id: string } }> = []
	statuses = new Map<string, string | undefined>()
	notifications: Array<{ message: string; type?: string }> = []
	setModelImpl: (model: AnyRecord) => Promise<boolean> = vi.fn(async (_model: AnyRecord) => true)
	execImpl: (command: string, args: string[], options?: AnyRecord) => Promise<{ stdout: string; stderr: string; code: number; killed: boolean }> =
		vi.fn(async (_command: string, _args: string[], _options?: AnyRecord) => ({ stdout: "", stderr: "", code: 1, killed: false }))
	confirmImpl: (title: string, message: string) => Promise<boolean> = vi.fn(async (_title: string, _message: string) => true)
	selectImpl: (title: string, options: string[]) => Promise<string | undefined> = vi.fn(async (_title: string, _options: string[]) => undefined)

	on(event: string, handler: (event: AnyRecord, ctx: AnyRecord) => Promise<unknown>): () => void {
		const list = this.handlers.get(event) ?? []
		list.push(handler)
		this.handlers.set(event, list)
		return () => {}
	}

	registerFlag(name: string, options: { default?: boolean | string }): void {
		this.flagDefaults.set(name, options.default ?? true)
	}

	getFlag(name: string): boolean | string | undefined {
		if (this.flagValues.has(name)) return this.flagValues.get(name)
		return this.flagDefaults.get(name)
	}

	registerCommand(name: string, options: { description?: string; handler: (args: string, ctx: AnyRecord) => Promise<void> }): void {
		this.commands.set(name, options)
	}

	sendMessage(message: AnyRecord, options?: AnyRecord): void {
		this.sent.push({ message, options })
	}

	appendEntry(customType: string, data?: unknown): void {
		this.entries.push({ type: "custom", customType, data })
	}

	getActiveTools(): string[] {
		return [...this.activeTools]
	}

	setActiveTools(tools: string[]): void {
		this.activeTools = [...tools]
	}

	async setModel(model: { provider: string; id: string }): Promise<boolean> {
		return this.setModelImpl(model)
	}

	async exec(command: string, args: string[], options?: AnyRecord) {
		return this.execImpl(command, args, options)
	}

	makeCtx(overrides: AnyRecord = {}): AnyRecord {
		const self = this
		return {
			ui: {
				notify: (message: string, type?: string) => self.notifications.push({ message, type }),
				setStatus: (key: string, text: string | undefined) => self.statuses.set(key, text),
				select: self.selectImpl,
				confirm: self.confirmImpl,
			},
			hasUI: true,
			mode: "tui",
			cwd: "/tmp/project",
			get model() {
				return self.model
			},
			modelRegistry: { getAvailable: () => self.available },
			scopedModels: self.scopedModels,
			getContextUsage: () => undefined,
			sessionManager: {
				getEntries: () => self.entries,
				getBranch: () => self.entries,
				getSessionId: () => "mock-session",
				buildSessionProjection: () => ({ entries: [], messages: [] }),
			},
			...overrides,
		}
	}

	async emit(event: string, payload: AnyRecord, ctxOverrides: AnyRecord = {}): Promise<unknown> {
		let result: unknown
		for (const handler of this.handlers.get(event) ?? []) {
			const value = await handler({ type: event, ...payload }, this.makeCtx(ctxOverrides))
			if (value !== undefined) {
				result = value
				if (event === "agent_before_settle") {
					const entries = (value as { entries?: AnyRecord[] }).entries ?? []
					this.entries.push(...entries)
				}
			}
		}
		return result
	}

	async runCommand(name: string, args: string): Promise<void> {
		const command = this.commands.get(name)
		if (command === undefined) throw new Error(`command ${name} not registered`)
		await command.handler(args, this.makeCtx())
	}
}

function jsonResponse(body: unknown): Response {
	return new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } })
}

/** Stub fetch that answers route / permission / outcome decisions per script. */
function stubJeVFetch(options: CallOptions): ReturnType<typeof vi.fn> {
	const plan = options.plan ?? "direct"
	const tier = options.tier ?? "normal"
	const action = options.action ?? "finish"
	const permissionAction = "allow"
	const issue = options.issue ?? "none"
	const quality = options.quality ?? 4
	return vi.fn(async (_url: unknown, init?: RequestInit) => {
		const body = JSON.parse(String(init?.body)) as {
			questions: Record<string, { type: string }>
		}
		const isOutcomeRequest = body.questions["issue"] !== undefined
		const answers: AnyRecord = {}
		for (const [id, question] of Object.entries(body.questions)) {
			if (question.type === "score") {
				answers[id] = {
					type: "score",
					score: quality,
					confidence: 0.95,
					probabilities: { 0: 0, 1: 0, 2: 0, 3: 0, 4: 1 },
				}
			} else if (id === "plan_strategy") {
				answers[id] = {
					type: "choice",
					choice: plan,
					confidence: options.planConfidence ?? 0.95,
					probabilities: { direct: 0, plan_only: 0, plan_then_execute: 1 },
				}
			} else if (id === "tier") {
				answers[id] = {
					type: "choice",
					choice: tier,
					confidence: options.tierConfidence ?? 0.95,
					probabilities: { small: 0, normal: 1, strong: 0 },
				}
			} else if (id === "action" && isOutcomeRequest) {
				answers[id] = {
					type: "choice",
					choice: action,
					confidence: options.actionConfidence ?? 0.95,
					probabilities: { finish: 0, retry: 1, replan: 0 },
				}
			} else if (id === "action") {
				answers[id] = {
					type: "choice",
					choice: permissionAction,
					confidence: options.actionConfidence ?? 0.95,
					probabilities: { allow: 1, ask: 0, deny: 0 },
				}
			} else {
				answers[id] = {
					type: "choice",
					choice: issue,
					confidence: 0.95,
					probabilities: {
						none: 1,
						incomplete: 0,
						test_failure: 0,
						missing_verification: 0,
						wrong_approach: 0,
						insufficient_context: 0,
					},
				}
			}
		}
		return jsonResponse({ answers, model: "typesafe/jev-1.13", usage: { cost: 0.0001 } })
	})
}

function settleEvent(overrides: AnyRecord = {}): AnyRecord {
	return {
		outcome: "completed",
		entries: [],
		continue: false,
		context: {
			contextEntries: [],
			contextMessages: [
				{ role: "user", content: "do the thing" },
				{ role: "toolResult", toolName: "bash", isError: false, content: [{ type: "text", text: "ok" }] },
			],
			llmMessages: [],
			pendingMessages: [],
			canContinue: false,
		},
		...overrides,
	}
}

let tempLogDir: string

type ExtensionFactory = (pi: never) => void

async function importExtension(): Promise<ExtensionFactory> {
	const module = await import("../extensions/jev.ts")
	return module.default as unknown as ExtensionFactory
}

/** Load the extension into a MockApi; cast keeps the stub out of pi's types. */
async function loadExtension(): Promise<MockApi> {
	const factory = await importExtension()
	const api = new MockApi()
	factory(api as never)
	return api
}

beforeEach(async () => {
	vi.resetModules()
	tempLogDir = await mkdtemp(join(tmpdir(), "pi-jev-ext-"))
	process.env["PI_JEV_API_KEY"] = "sk-test-0123456789"
	process.env["PI_JEV_MODEL_SMALL"] = "acme:small-model"
	process.env["PI_JEV_MODEL_NORMAL"] = "acme:normal-model"
	process.env["PI_JEV_MODEL_STRONG"] = "acme:strong-model"
	process.env["PI_JEV_LOG_DIR"] = tempLogDir
	delete process.env["PI_JEV_PLAN_AUTO_EXECUTE"]
	delete process.env["PI_JEV_ENABLED"]
})

afterEach(async () => {
	vi.unstubAllGlobals()
	for (const key of ENV_KEYS) {
		delete process.env[key]
	}
	await rm(tempLogDir, { recursive: true, force: true })
})

describe("extension wiring", () => {
	it("registers the flag, the /jev command, and all four hooks", async () => {
		const api = await loadExtension()
		expect(api.flagDefaults.has("no-jev")).toBe(true)
		expect(api.commands.has("jev")).toBe(true)
		for (const event of ["session_start", "session_tree", "before_agent_start", "tool_call", "agent_before_settle"]) {
			expect(api.handlers.get(event)).toHaveLength(1)
		}
	})

	it("bypasses every hook when the flag is false", async () => {
		const api = await loadExtension()
		api.flagValues.set("no-jev", true)
		const fetchMock = stubJeVFetch({ plan: "plan_then_execute", tier: "strong" })
		vi.stubGlobal("fetch", fetchMock)

		await api.emit("session_start", { reason: "startup" })
		const result = await api.emit("before_agent_start", { prompt: "big refactor" })
		expect(result).toBeUndefined()
		expect(fetchMock).not.toHaveBeenCalled()
		expect(api.activeTools).toContain("edit")
	})

	it("notifies once and keeps permissions fail-closed when config is invalid", async () => {
		delete process.env["PI_JEV_API_KEY"]
		const api = await loadExtension()

		await api.emit("session_start", { reason: "startup" })
		expect(api.notifications).toHaveLength(1)
		expect(api.notifications[0]?.message).toContain("PI_JEV_API_KEY")

		const result = await api.emit("before_agent_start", { prompt: "task" })
		expect(result).toBeUndefined()
		const blocked = (await api.emit(
			"tool_call",
			{ toolName: "edit", toolCallId: "c1", input: { path: "a.ts" } },
			{ hasUI: false },
		)) as AnyRecord
		expect(blocked).toMatchObject({ block: true })
	})

	it("routes plan_then_execute: read-only tools, plan instruction, tier switch", async () => {
		const api = await loadExtension()
		vi.stubGlobal("fetch", stubJeVFetch({ plan: "plan_then_execute", tier: "strong" }))

		const result = (await api.emit("before_agent_start", { prompt: "migrate the build system" })) as AnyRecord
		expect(result.message as AnyRecord).toMatchObject({ customType: "jev-plan-context", display: false })
		expect(String((result.message as AnyRecord)["content"])).toContain("READ-ONLY")

		// edit/write and custom tools removed; read-only builtins kept
		expect(api.activeTools).toEqual(["read", "bash", "grep", "find", "ls"])

		// tier switched to strong
		const setModelCalls = vi.mocked(api.setModelImpl).mock.calls
		expect(setModelCalls).toHaveLength(1)
		expect(setModelCalls[0]![0]).toMatchObject({ provider: "acme", id: "strong-model" })

		// state persisted in planning phase with a tool snapshot
		const stateEntry = api.entries.findLast((entry) => entry["customType"] === "jev-state")
		expect((stateEntry?.["data"] as AnyRecord)["phase"]).toBe("planning")
		expect((stateEntry?.["data"] as AnyRecord)["toolsBeforePlan"]).toContain("edit")
	})

	it("routes direct: no message, no tool changes, same tier skips setModel", async () => {
		const api = await loadExtension()
		vi.stubGlobal("fetch", stubJeVFetch({ plan: "direct", tier: "normal" }))

		const result = await api.emit("before_agent_start", { prompt: "typo fix" })
		expect(result).toBeUndefined()
		expect(api.activeTools).toContain("edit")
		expect(api.setModelImpl).not.toHaveBeenCalled()
	})

	it("warns once and keeps the model when the tier model cannot be resolved", async () => {
		process.env["PI_JEV_MODEL_STRONG"] = "acme:unknown-model"
		const api = await loadExtension()
		vi.stubGlobal("fetch", stubJeVFetch({ plan: "direct", tier: "strong" }))

		await api.emit("before_agent_start", { prompt: "task one" })
		await api.emit("before_agent_start", { prompt: "task two" })
		const warnings = api.notifications.filter((n) => n.type === "warning")
		expect(warnings).toHaveLength(1)
		expect(warnings[0]?.message).toContain("catalogue")
		expect(api.setModelImpl).not.toHaveBeenCalled()
	})

	it("respects a manual model override: skips tier switching for the next task", async () => {
		const api = await loadExtension()
		vi.stubGlobal("fetch", stubJeVFetch({ plan: "direct", tier: "strong" }))

		// task 1: router switches to strong
		await api.emit("before_agent_start", { prompt: "first" })
		expect(api.setModelImpl).toHaveBeenCalledTimes(1)

		// user manually switches models
		api.model = { provider: "acme", id: "small-model" }
		await api.emit("before_agent_start", { prompt: "second" })
		expect(api.setModelImpl).toHaveBeenCalledTimes(1)
	})

	it("planning settle restores tools, injects execution context and continues", async () => {
		const api = await loadExtension()
		vi.stubGlobal("fetch", stubJeVFetch({ plan: "plan_then_execute", tier: "normal" }))
		await api.emit("before_agent_start", { prompt: "big task" })
		expect(api.activeTools).not.toContain("edit")

		const result = (await api.emit("agent_before_settle", settleEvent())) as AnyRecord
		expect(vi.mocked(fetch).mock.calls).toHaveLength(2)
		expect(result).toMatchObject({ continue: true })
		const entry = (result.entries as AnyRecord[])[1]
		expect(entry).toMatchObject({ type: "custom_message", customType: "jev-execution-context", display: false })
		expect(api.activeTools).toContain("edit")
		const stateEntry = api.entries.findLast((e) => e["customType"] === "jev-state")
		expect((stateEntry?.["data"] as AnyRecord)["phase"]).toBe("executing")
	})

	it("planning uses Outcome Judge and stays read-only while correcting the plan", async () => {
		const api = await loadExtension()
		vi.stubGlobal(
			"fetch",
			stubJeVFetch({ plan: "plan_then_execute", tier: "normal", action: "retry", issue: "incomplete", quality: 2 }),
		)
		await api.emit("before_agent_start", { prompt: "big task" })

		const result = (await api.emit("agent_before_settle", settleEvent())) as AnyRecord
		expect(result).toMatchObject({ continue: true })
		expect((result.entries as AnyRecord[])).toEqual(
			expect.arrayContaining([
				expect.objectContaining({ customType: "jev-state", data: expect.objectContaining({ phase: "planning", correctionsUsed: 1 }) }),
				expect.objectContaining({ customType: "jev-correction" }),
			]),
		)
		expect(api.activeTools).not.toContain("edit")
	})

	it("does not execute a plan when the Outcome answer is below the confidence threshold", async () => {
		const api = await loadExtension()
		vi.stubGlobal(
			"fetch",
			stubJeVFetch({ plan: "plan_then_execute", tier: "normal", action: "retry", actionConfidence: 0.5 }),
		)
		await api.emit("before_agent_start", { prompt: "big task" })

		const result = (await api.emit("agent_before_settle", settleEvent())) as AnyRecord
		expect(result["continue"]).toBeUndefined()
		expect(result["entries"]).toEqual(
			expect.arrayContaining([
				expect.objectContaining({ customType: "jev-state", data: null }),
				expect.objectContaining({
					customType: "jev-correction",
					content: expect.stringContaining("confidence was below the required threshold"),
				}),
			]),
		)
		expect(String((result["entries"] as AnyRecord[])[1]?.["content"])).not.toContain("unavailable")
		expect(api.activeTools).toContain("edit")
	})

	it("returns boundary entries and continuation even when the initial preview cannot continue", async () => {
		const api = await loadExtension()
		vi.stubGlobal("fetch", stubJeVFetch({ plan: "plan_then_execute", tier: "normal" }))
		await api.emit("before_agent_start", { prompt: "big task" })

		const base = settleEvent()
		const event = settleEvent({ context: { ...(base["context"] as AnyRecord), canContinue: false } })
		const result = await api.emit("agent_before_settle", event as AnyRecord)
		expect(result).toMatchObject({ continue: true })
		expect((result as AnyRecord).entries).toEqual(
			expect.arrayContaining([expect.objectContaining({ customType: "jev-execution-context" })]),
		)
		expect(api.sent).toHaveLength(0)
		expect(api.activeTools).toContain("edit")
	})

	it("outcome finish settles and clears the task", async () => {
		const api = await loadExtension()
		vi.stubGlobal("fetch", stubJeVFetch({ plan: "direct", tier: "normal", action: "finish" }))
		await api.emit("before_agent_start", { prompt: "small task" })

		const result = await api.emit("agent_before_settle", settleEvent())
		expect(result).toMatchObject({ entries: [expect.objectContaining({ customType: "jev-state", data: null })] })
		// cleared marker persisted so resume does not resurrect the task
		expect(api.entries.findLast((e) => e["customType"] === "jev-state")?.["data"]).toBeNull()
	})

	it("outcome retry injects a visible correction and continues; budget then exhausts", async () => {
		const api = await loadExtension()
		vi.stubGlobal(
			"fetch",
			stubJeVFetch({ plan: "direct", tier: "normal", action: "retry", issue: "test_failure", quality: 2 }),
		)
		await api.emit("before_agent_start", { prompt: "make tests pass" })

		const first = (await api.emit("agent_before_settle", settleEvent())) as AnyRecord
		expect(first).toMatchObject({ continue: true })
		const correction = (first.entries as AnyRecord[]).find((entry) => entry["customType"] === "jev-correction")!
		expect(correction).toMatchObject({ customType: "jev-correction", display: true })
		expect(String(correction.content)).toContain("policy selected retry; issue: test_failure")
		expect(api.entries.findLast((e) => e["customType"] === "jev-state")?.["data"]).toMatchObject({
			correctionsUsed: 1,
		})

		// continued run settles again: second correction
		const second = (await api.emit("agent_before_settle", settleEvent())) as AnyRecord
		expect(second).toMatchObject({ continue: true })
		expect(api.entries.findLast((e) => e["customType"] === "jev-state")?.["data"]).toMatchObject({
			correctionsUsed: 2,
		})

		// third settle: budget exhausted, natural finish
		const third = (await api.emit("agent_before_settle", settleEvent())) as AnyRecord
		expect(third).not.toHaveProperty("continue")
		const exhausted = (third.entries as AnyRecord[]).find((entry) => entry["customType"] === "jev-correction")
		expect(String(exhausted?.content)).toContain("budget exhausted")
	})

	it("aborted runs reset without judging", async () => {
		const api = await loadExtension()
		const fetchMock = stubJeVFetch({ plan: "direct", tier: "normal" })
		vi.stubGlobal("fetch", fetchMock)
		await api.emit("before_agent_start", { prompt: "task" })

		fetchMock.mockClear()
		const result = await api.emit("agent_before_settle", settleEvent({ outcome: "aborted" }))
		expect(result).toMatchObject({ entries: [expect.objectContaining({ customType: "jev-state", data: null })] })
		expect(fetchMock).not.toHaveBeenCalled()
		expect(api.entries.findLast((e) => e["customType"] === "jev-state")?.["data"]).toBeNull()
	})

	it("tool_call: read tools pass; hard-denied bash is blocked without JEV", async () => {
		const api = await loadExtension()
		const fetchMock = stubJeVFetch({})
		vi.stubGlobal("fetch", fetchMock)
		await api.emit("before_agent_start", { prompt: "task" })
		fetchMock.mockClear()

		const read = await api.emit("tool_call", { toolName: "read", toolCallId: "c1", input: { path: "a" } })
		expect(read).toBeUndefined()

		const blocked = (await api.emit("tool_call", {
			toolName: "bash",
			toolCallId: "c2",
			input: { command: "sudo rm -rf /" },
		})) as AnyRecord
		expect(blocked).toMatchObject({ block: true })
		expect(String(blocked.reason)).toContain("hard-deny")
		expect(fetchMock).not.toHaveBeenCalled()
	})

	it("tool_call: JEV-adjudicated writes are allowed and task approval sticks", async () => {
		const api = await loadExtension()
		const fetchMock = stubJeVFetch({})
		vi.stubGlobal("fetch", fetchMock)
		await api.emit("before_agent_start", { prompt: "add a test" })
		fetchMock.mockClear()

		const edit = await api.emit("tool_call", { toolName: "edit", toolCallId: "c1", input: { path: "a.ts" } })
		expect(edit).toBeUndefined()
		expect(fetchMock).toHaveBeenCalledTimes(1)
		const permissionBody = fetchMock.mock.calls[0]![1] as RequestInit
		const payload = JSON.parse(String(permissionBody.body)) as AnyRecord
		expect(Object.keys(payload["questions"] as object)).toEqual(["action"])

		// JEV goes down; human approves this exact call for this task.
		fetchMock.mockClear()
		api.selectImpl = vi.fn(async (_title: string, _options: string[]) => "Allow this exact call for this task")
		fetchMock.mockImplementation(async () => {
			throw new Error("JEV down")
		})
		const write = await api.emit("tool_call", { toolName: "write", toolCallId: "c2", input: { path: "b.ts" } })
		expect(write).toBeUndefined()
		expect(fetchMock).toHaveBeenCalledTimes(2)
		const again = await api.emit("tool_call", { toolName: "write", toolCallId: "c3", input: { path: "b.ts" } })
		expect(again).toBeUndefined()
		expect(fetchMock).toHaveBeenCalledTimes(2)
	})

	it("tool_call fails closed when JEV is down and no UI exists", async () => {
		const api = await loadExtension()
		// The client captures fetch at first use, so the failing endpoint must
		// be in place before the first request (routing degrades gracefully).
		const fetchMock = vi.fn(async (_input: unknown, _init?: RequestInit) => new Response("{}", { status: 500 }))
		vi.stubGlobal("fetch", fetchMock)
		await api.emit("before_agent_start", { prompt: "task" })

		const blocked = (await api.emit(
			"tool_call",
			{ toolName: "edit", toolCallId: "c1", input: { path: "a" } },
			{ hasUI: false },
		)) as AnyRecord
		expect(blocked).toMatchObject({ block: true })
		expect(String(blocked.reason)).toContain("fail closed")
	})

	it("tool_call converts an unexpected permission handler error into a sanitized fail-closed block", async () => {
		const api = await loadExtension()
		const fetchMock = stubJeVFetch({})
		vi.stubGlobal("fetch", fetchMock)
		await api.emit("before_agent_start", { prompt: "task" })

		fetchMock.mockImplementation(async () => {
			throw new Error("provider internal secret")
		})
		api.selectImpl = vi.fn(async () => {
			throw new Error("ui internal secret")
		})
		const blocked = (await api.emit("tool_call", {
			toolName: "edit",
			toolCallId: "c1",
			input: { path: "a.ts" },
		})) as AnyRecord

		expect(blocked).toMatchObject({ block: true })
		expect(String(blocked["reason"])).toContain("fail closed")
		expect(String(blocked["reason"])).not.toContain("internal secret")
	})

	it("serializes parallel permission confirmations", async () => {
		const api = await loadExtension()
		const fetchMock = stubJeVFetch({})
		vi.stubGlobal("fetch", fetchMock)
		await api.emit("before_agent_start", { prompt: "task" })
		fetchMock.mockImplementation(async () => {
			throw new Error("offline")
		})
		let active = 0
		let maxActive = 0
		api.selectImpl = vi.fn(async () => {
			active++
			maxActive = Math.max(maxActive, active)
			await new Promise((resolve) => setTimeout(resolve, 5))
			active--
			return "Block"
		})

		await Promise.all([
			api.emit("tool_call", { toolName: "write", toolCallId: "c1", input: { path: "a.ts" } }),
			api.emit("tool_call", { toolName: "edit", toolCallId: "c2", input: { path: "b.ts" } }),
		])
		expect(maxActive).toBe(1)
		expect(api.selectImpl).toHaveBeenCalledTimes(2)
	})

	it("session_tree restores branch state and clears task approvals", async () => {
		const api = await loadExtension()
		const fetchMock = stubJeVFetch({ plan: "direct" })
		vi.stubGlobal("fetch", fetchMock)
		await api.emit("before_agent_start", { prompt: "task" })
		fetchMock.mockClear()
		api.selectImpl = vi.fn(async () => "Allow this exact call for this task")
		fetchMock.mockImplementation(async () => {
			throw new Error("offline")
		})
		const call = { toolName: "write", toolCallId: "c1", input: { path: "a.ts" } }
		await api.emit("tool_call", call)
		expect(fetchMock).toHaveBeenCalledTimes(2)
		await api.emit("tool_call", { ...call, toolCallId: "c2" })
		expect(fetchMock).toHaveBeenCalledTimes(2)

		await api.emit("session_tree", { newLeafId: "leaf", oldLeafId: "old" })
		await api.emit("tool_call", { ...call, toolCallId: "c3" })
		expect(fetchMock).toHaveBeenCalledTimes(4)
	})

	it("session_start restores a persisted planning phase and rebuilds read-only tools", async () => {
		const api = await loadExtension()
		vi.stubGlobal("fetch", stubJeVFetch({ plan: "plan_then_execute", tier: "normal" }))
		await api.emit("before_agent_start", { prompt: "plan it" })
		expect(api.activeTools).not.toContain("edit")

		// simulate restart: fresh extension instance, tools back to defaults
		const restarted = new MockApi()
		restarted.entries = [...api.entries]
		;(await importExtension())(restarted as never)
		await restarted.emit("session_start", { reason: "resume" })
		expect(restarted.activeTools).toEqual(["read", "bash", "grep", "find", "ls"])

		// corrections budget and task survive restore
		const state = restarted.entries.findLast((e) => e["customType"] === "jev-state")?.["data"] as AnyRecord
		expect(state["phase"]).toBe("planning")
	})

	it("/jev off bypasses hooks; /jev on re-enables; /jev shows status", async () => {
		const api = await loadExtension()
		const fetchMock = stubJeVFetch({ plan: "direct", tier: "normal" })
		vi.stubGlobal("fetch", fetchMock)

		await api.runCommand("jev", "off")
		const result = await api.emit("before_agent_start", { prompt: "task" })
		expect(result).toBeUndefined()
		expect(fetchMock).not.toHaveBeenCalled()

		await api.runCommand("jev", "on")
		await api.emit("before_agent_start", { prompt: "task" })
		expect(fetchMock).toHaveBeenCalledTimes(1)

		await api.runCommand("jev", "")
		const status = api.notifications.findLast((n) => n.message.includes("phase:"))
		expect(status?.message).toContain("phase: direct")
		expect(status?.message).toContain("tier: normal")
	})

	it("status bar reflects the current phase and tier", async () => {
		const api = await loadExtension()
		vi.stubGlobal("fetch", stubJeVFetch({ plan: "plan_then_execute", tier: "strong" }))
		await api.emit("before_agent_start", { prompt: "task" })
		expect(api.statuses.get("jev")).toBe("jev:planning/strong")

		await api.emit("agent_before_settle", settleEvent())
		expect(api.statuses.get("jev")).toBe("jev:executing/strong")
	})
})
