import { mkdtemp, rm } from "node:fs/promises"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { fauxAssistantMessage, fauxProvider, fauxToolCall } from "@earendil-works/pi-ai"
import {
	createAgentSession,
	DefaultResourceLoader,
	SessionManager,
	type ExtensionFactory,
} from "@earendil-works/pi-coding-agent"
import { afterEach, describe, expect, it, vi } from "vitest"
import jevExtension from "../extensions/jev.ts"

const ENV_KEYS = [
	"PI_JEV_API_KEY",
	"PI_JEV_LOG_DIR",
	"PI_JEV_MODEL_SMALL",
	"PI_JEV_MODEL_NORMAL",
	"PI_JEV_MODEL_STRONG",
] as const

const cleanups: Array<() => Promise<void> | void> = []

afterEach(async () => {
	while (cleanups.length > 0) await cleanups.pop()?.()
	for (const key of ENV_KEYS) delete process.env[key]
	vi.unstubAllGlobals()
})

function decisionResponse(questions: Record<string, { type: string }>, outcomeCalls: number): Response {
	const answers: Record<string, unknown> = {}
	for (const [id, question] of Object.entries(questions)) {
		if (id === "plan_strategy") {
			answers[id] = {
				type: "choice",
				choice: "direct",
				confidence: 1,
				probabilities: { direct: 1, plan_only: 0, plan_then_execute: 0 },
			}
			continue
		}
		if (id === "tier") {
			answers[id] = {
				type: "choice",
				choice: "normal",
				confidence: 1,
				probabilities: { small: 0, normal: 1, strong: 0 },
			}
			continue
		}
		if (id === "action") {
			const permission = questions["issue"] === undefined
			answers[id] = {
				type: "choice",
				choice: permission ? "allow" : outcomeCalls === 1 ? "retry" : "finish",
				confidence: 1,
				probabilities: permission
					? { allow: 1, ask: 0, deny: 0 }
					: outcomeCalls === 1
						? { finish: 0, retry: 1, replan: 0 }
						: { finish: 1, retry: 0, replan: 0 },
			}
			continue
		}
		if (id === "issue") {
			answers[id] = {
				type: "choice",
				choice: outcomeCalls === 1 ? "missing_verification" : "none",
				confidence: 1,
				probabilities: {
					none: outcomeCalls === 1 ? 0 : 1,
					incomplete: 0,
					test_failure: 0,
					missing_verification: outcomeCalls === 1 ? 1 : 0,
					wrong_approach: 0,
					insufficient_context: 0,
				},
			}
			continue
		}
		if (question.type === "score") {
			answers[id] = {
				type: "score",
				score: outcomeCalls === 1 ? 2 : 4,
				confidence: 1,
				probabilities:
					outcomeCalls === 1
						? { 0: 0, 1: 0, 2: 1, 3: 0, 4: 0 }
						: { 0: 0, 1: 0, 2: 0, 3: 0, 4: 1 },
			}
		}
	}
	return new Response(JSON.stringify({ answers, model: "jev-test" }), {
		status: 200,
		headers: { "content-type": "application/json" },
	})
}

describe("real pi lifecycle", () => {
	it("continues an outcome correction even though the settle preview cannot continue", async () => {
		const cwd = await mkdtemp(join(tmpdir(), "pi-jev-lifecycle-"))
		const agentDir = join(cwd, "agent")
		const faux = fauxProvider({ provider: "jev-test-provider", models: [{ id: "jev-test-model" }] })
		faux.setResponses([fauxAssistantMessage("first response"), fauxAssistantMessage("corrected response")])

		process.env["PI_JEV_API_KEY"] = "test-secret-key"
		process.env["PI_JEV_LOG_DIR"] = join(cwd, "logs")
		for (const key of ["PI_JEV_MODEL_SMALL", "PI_JEV_MODEL_NORMAL", "PI_JEV_MODEL_STRONG"] as const) {
			process.env[key] = `${faux.provider.id}:${faux.getModel().id}`
		}

		let outcomeCalls = 0
		vi.stubGlobal(
			"fetch",
			vi.fn(async (_input: string | URL | Request, init?: RequestInit) => {
				const body = JSON.parse(String(init?.body)) as { questions: Record<string, { type: string }> }
				if (body.questions["issue"] !== undefined) outcomeCalls++
				return decisionResponse(body.questions, outcomeCalls)
			}),
		)

		const providerExtension: ExtensionFactory = (pi) => {
			pi.registerProvider(faux.provider.id, {
				api: faux.api,
				apiKey: "test-provider-key",
				baseUrl: faux.getModel().baseUrl,
				models: faux.models,
				streamSimple: faux.provider.streamSimple,
			})
		}
		const resourceLoader = new DefaultResourceLoader({
			cwd,
			agentDir,
			extensionFactories: [providerExtension, jevExtension],
			noContextFiles: true,
			noSkills: true,
			noPromptTemplates: true,
			noThemes: true,
		})
		await resourceLoader.reload()
		const sessionManager = SessionManager.inMemory(cwd)
		const { session } = await createAgentSession({
			cwd,
			agentDir,
			model: faux.getModel(),
			resourceLoader,
			sessionManager,
			noTools: "all",
		})
		cleanups.push(async () => {
			session.dispose()
			await rm(cwd, { recursive: true, force: true })
		})

		await session.prompt("verify and finish the task")

		expect(faux.state.callCount).toBe(2)
		expect(outcomeCalls).toBe(2)
		expect(sessionManager.getBranch()).toContainEqual(
			expect.objectContaining({ type: "custom_message", customType: "jev-correction" }),
		)
	})

	it("keeps steering and follow-up messages on the active route", async () => {
		const cwd = await mkdtemp(join(tmpdir(), "pi-jev-queue-"))
		const agentDir = join(cwd, "agent")
		const faux = fauxProvider({ provider: "jev-queue-provider", models: [{ id: "jev-queue-model" }] })
		faux.setResponses([
			fauxAssistantMessage(fauxToolCall("bash", { command: 'node -e "setTimeout(() => {}, 100)"' }), {
				stopReason: "toolUse",
			}),
			fauxAssistantMessage("handled steering"),
			fauxAssistantMessage("handled follow-up"),
		])

		process.env["PI_JEV_API_KEY"] = "test-secret-key"
		process.env["PI_JEV_LOG_DIR"] = join(cwd, "logs")
		for (const key of ["PI_JEV_MODEL_SMALL", "PI_JEV_MODEL_NORMAL", "PI_JEV_MODEL_STRONG"] as const) {
			process.env[key] = `${faux.provider.id}:${faux.getModel().id}`
		}

		let outcomeCalls = 0
		let routeCalls = 0
		vi.stubGlobal(
			"fetch",
			vi.fn(async (_input: string | URL | Request, init?: RequestInit) => {
				const body = JSON.parse(String(init?.body)) as { questions: Record<string, { type: string }> }
				if (body.questions["plan_strategy"] !== undefined) routeCalls++
				if (body.questions["issue"] !== undefined) outcomeCalls++
				return decisionResponse(body.questions, body.questions["issue"] === undefined ? outcomeCalls : 2)
			}),
		)

		const providerExtension: ExtensionFactory = (pi) => {
			pi.registerProvider(faux.provider.id, {
				api: faux.api,
				apiKey: "test-provider-key",
				baseUrl: faux.getModel().baseUrl,
				models: faux.models,
				streamSimple: faux.provider.streamSimple,
			})
		}
		const resourceLoader = new DefaultResourceLoader({
			cwd,
			agentDir,
			extensionFactories: [providerExtension, jevExtension],
			noContextFiles: true,
			noSkills: true,
			noPromptTemplates: true,
			noThemes: true,
		})
		await resourceLoader.reload()
		const sessionManager = SessionManager.inMemory(cwd)
		const { session } = await createAgentSession({
			cwd,
			agentDir,
			model: faux.getModel(),
			resourceLoader,
			sessionManager,
		})
		cleanups.push(async () => {
			session.dispose()
			await rm(cwd, { recursive: true, force: true })
		})

		const toolStarted = new Promise<void>((resolve) => {
			const unsubscribe = session.subscribe((event) => {
				if (event.type !== "tool_execution_start" || event.toolName !== "bash") return
				unsubscribe()
				resolve()
			})
		})
		const prompt = session.prompt("run the queued-message test")
		await toolStarted
		await session.steer("steering update")
		await session.followUp("follow-up update")
		await prompt

		expect(routeCalls).toBe(1)
		expect(faux.state.callCount).toBe(3)
	})
})
