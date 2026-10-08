import { describe, expect, it, vi } from "vitest"
import { createJevClient, JevError, validateJevResponse } from "../src/client.ts"
import { permissionQuestion, planStrategyQuestion, outcomeQualityQuestion } from "../src/questions.ts"

function jsonResponse(status: number, body: unknown): Response {
	return new Response(JSON.stringify(body), {
		status,
		headers: { "content-type": "application/json" },
	})
}

const CLIENT_OPTIONS = {
	url: "https://decisions.example.com/api/alpha/decisions",
	apiKey: "sk-secret-key",
	model: "typesafe/jev-1.13",
	timeoutMs: 3000,
}

describe("validateJevResponse", () => {
	it("accepts a valid choice answer", () => {
		const validated = validateJevResponse(
			{ strategy: planStrategyQuestion },
			{
				answers: {
					strategy: {
						type: "choice",
						choice: "direct",
						confidence: 0.9,
						probabilities: { direct: 0.9, plan_only: 0.05, plan_then_execute: 0.05 },
					},
				},
				model: "typesafe/jev-1.13",
				usage: { cost: 0.001, input_tokens: 10, output_tokens: 5 },
			},
		)
		expect(validated.answers["strategy"]?.type).toBe("choice")
		expect(validated.usage?.cost).toBe(0.001)
	})

	it("rejects a choice outside the criteria", () => {
		expect(() =>
			validateJevResponse(
				{ strategy: planStrategyQuestion },
				{
					answers: {
						strategy: {
							type: "choice",
							choice: "yolo",
							confidence: 0.9,
							probabilities: { yolo: 1 },
						},
					},
				},
			),
		).toThrowError(new JevError("invalid_choice"))
	})

	it("rejects probabilities with missing or extra keys", () => {
		const base = {
			type: "choice",
			choice: "direct",
			confidence: 0.9,
		}
		expect(() =>
			validateJevResponse(
				{ strategy: planStrategyQuestion },
				{
					answers: {
						strategy: {
							...base,
							probabilities: { direct: 0.6, plan_only: 0.4 },
						},
					},
				},
			),
		).toThrowError(new JevError("invalid_distribution"))

		expect(() =>
			validateJevResponse(
				{ strategy: planStrategyQuestion },
				{
					answers: {
						strategy: {
							...base,
							probabilities: {
								direct: 0.5,
								plan_only: 0.25,
								plan_then_execute: 0.25,
								extra: 0,
							},
						},
					},
				},
			),
		).toThrowError(new JevError("invalid_distribution"))
	})

	it("rejects probabilities that do not sum to 1 within tolerance", () => {
		expect(() =>
			validateJevResponse(
				{ strategy: planStrategyQuestion },
				{
					answers: {
						strategy: {
							type: "choice",
							choice: "direct",
							confidence: 0.9,
							probabilities: { direct: 0.5, plan_only: 0.25, plan_then_execute: 0.1 },
						},
					},
				},
			),
		).toThrowError(new JevError("invalid_distribution"))
	})

	it("accepts a probability sum within the 0.02 tolerance", () => {
		expect(() =>
			validateJevResponse(
				{ strategy: planStrategyQuestion },
				{
					answers: {
						strategy: {
							type: "choice",
							choice: "direct",
							confidence: 0.9,
							probabilities: { direct: 0.5, plan_only: 0.3, plan_then_execute: 0.2 },
						},
					},
				},
			),
		).not.toThrow()
	})

	it("rejects score answers outside 0..criteria-1", () => {
		const scoreAnswer = (score: number) => ({
			quality: {
				type: "score",
				score,
				confidence: 0.9,
				probabilities: { 0: 0.1, 1: 0.1, 2: 0.2, 3: 0.3, 4: 0.3 },
			},
		})
		expect(() => validateJevResponse({ quality: outcomeQualityQuestion }, { answers: scoreAnswer(5) })).toThrowError(
			new JevError("invalid_score"),
		)
		expect(() => validateJevResponse({ quality: outcomeQualityQuestion }, { answers: scoreAnswer(-1) })).toThrowError(
			new JevError("invalid_score"),
		)
		expect(() => validateJevResponse({ quality: outcomeQualityQuestion }, { answers: scoreAnswer(4) })).not.toThrow()
	})

	it("requires score probabilities to cover all levels", () => {
		expect(() =>
			validateJevResponse(
				{ quality: outcomeQualityQuestion },
				{
					answers: {
						quality: {
							type: "score",
							score: 2,
							confidence: 0.9,
							probabilities: { 0: 0.3, 1: 0.3, 2: 0.4 },
						},
					},
				},
			),
		).toThrowError(new JevError("invalid_distribution"))
	})

	it("rejects a missing answer or a type mismatch", () => {
		expect(() =>
			validateJevResponse({ strategy: planStrategyQuestion }, { answers: { other: { type: "choice", choice: "direct", confidence: 1, probabilities: { direct: 1 } } } }),
		).toThrowError(new JevError("invalid_answer"))

		expect(() =>
			validateJevResponse(
				{ action: permissionQuestion },
				{
					answers: {
						action: {
							type: "score",
							score: 0,
							confidence: 1,
							probabilities: { 0: 1 },
						},
					},
				},
			),
		).toThrowError(new JevError("invalid_answer"))
	})

	it("rejects non-object bodies", () => {
		expect(() => validateJevResponse({ action: permissionQuestion }, "nope")).toThrowError(
			new JevError("transport_or_schema_error"),
		)
		expect(() => validateJevResponse({ action: permissionQuestion }, { answers: [] })).toThrowError(
			new JevError("transport_or_schema_error"),
		)
	})

	it("rejects negative usage values", () => {
		const answer = {
			type: "choice",
			choice: "allow",
			confidence: 1,
			probabilities: { allow: 1, ask: 0, deny: 0 },
		}
		expect(() =>
			validateJevResponse({ action: permissionQuestion }, { answers: { action: answer }, usage: { cost: -0.5 } }),
		).toThrowError(new JevError("transport_or_schema_error"))
		expect(() =>
			validateJevResponse({ action: permissionQuestion }, { answers: { action: answer }, usage: { input_tokens: -3 } }),
		).toThrowError(new JevError("transport_or_schema_error"))
	})
})

describe("createJevClient transport", () => {
	const REQUEST = {
		state: { task_prompt: "hello" },
		questions: { strategy: planStrategyQuestion },
	}

	it("sends the documented payload and returns latency", async () => {
		const fetchImpl = vi.fn(async (_input: string | URL | Request, _init?: RequestInit) =>
			jsonResponse(200, {
				answers: {
					strategy: {
						type: "choice",
						choice: "direct",
						confidence: 0.99,
						probabilities: { direct: 0.99, plan_only: 0.005, plan_then_execute: 0.005 },
					},
				},
				model: "typesafe/jev-1.13",
				usage: { cost: 0 },
			}),
		)
		const client = createJevClient({ ...CLIENT_OPTIONS, fetchImpl })
		const response = await client.decide(REQUEST)
		expect(response.latencyMs).toBeGreaterThanOrEqual(0)
		const [url, init] = fetchImpl.mock.calls[0]!
		expect(url).toBe(CLIENT_OPTIONS.url)
		expect(init?.method).toBe("POST")
		expect(init?.redirect).toBe("error")
		expect(init?.headers).toMatchObject({
			authorization: "Bearer sk-secret-key",
			"content-type": "application/json",
		})
		const body = JSON.parse(String(init?.body))
		expect(body.model).toBe("typesafe/jev-1.13")
		expect(body.state).toEqual({ task_prompt: "hello" })
		expect(body.questions.strategy.type).toBe("choice")
	})

	it("retries exactly once on retryable statuses, then succeeds", async () => {
		const fetchImpl = vi
			.fn(async (_input: string | URL | Request, _init?: RequestInit) => jsonResponse(200, {}))
			.mockResolvedValueOnce(jsonResponse(503, { error: "unavailable" }))
			.mockResolvedValueOnce(
				jsonResponse(200, {
					answers: {
						strategy: {
							type: "choice",
							choice: "direct",
							confidence: 1,
							probabilities: { direct: 1, plan_only: 0, plan_then_execute: 0 },
						},
					},
				}),
			)
		const client = createJevClient({ ...CLIENT_OPTIONS, fetchImpl })
		await expect(client.decide(REQUEST)).resolves.toBeTruthy()
		expect(fetchImpl).toHaveBeenCalledTimes(2)
	})

	it.each([429, 500, 502, 503, 524, 529])("gives up with http_%s after one retry", async (status) => {
		const fetchImpl = vi.fn(async (_input: string | URL | Request, _init?: RequestInit) => jsonResponse(status, { error: "boom" }))
		const client = createJevClient({ ...CLIENT_OPTIONS, fetchImpl })
		await expect(client.decide(REQUEST)).rejects.toThrowError(new JevError(`http_${status}`))
		expect(fetchImpl).toHaveBeenCalledTimes(2)
	})

	it.each([400, 401, 403, 404, 422])("does not retry http_%s", async (status) => {
		const fetchImpl = vi.fn(async (_input: string | URL | Request, _init?: RequestInit) => jsonResponse(status, { error: "bad" }))
		const client = createJevClient({ ...CLIENT_OPTIONS, fetchImpl })
		await expect(client.decide(REQUEST)).rejects.toThrowError(new JevError(`http_${status}`))
		expect(fetchImpl).toHaveBeenCalledTimes(1)
	})

	it("maps network failures to transport_or_schema_error without leaking details", async () => {
		const fetchImpl = vi.fn(async (_input: string | URL | Request, _init?: RequestInit) => {
			throw new TypeError("getaddrinfo ENOTFOUND decisions.example.com secret-token")
		})
		const client = createJevClient({ ...CLIENT_OPTIONS, fetchImpl })
		const promise = client.decide(REQUEST)
		await expect(promise).rejects.toThrowError(new JevError("transport_or_schema_error"))
		expect(fetchImpl).toHaveBeenCalledTimes(2)
		try {
			await client.decide(REQUEST)
		} catch (error) {
			const message = (error as Error).message
			expect(message).not.toContain("ENOTFOUND")
			expect(message).not.toContain("secret-token")
			expect(message).not.toContain(CLIENT_OPTIONS.url)
		}
	})

	it("maps timeouts to transport_or_schema_error", async () => {
		const fetchImpl = vi.fn((_input: string | URL | Request, init?: RequestInit) => {
			return new Promise<Response>((_resolve, reject) => {
				init?.signal?.addEventListener("abort", () => reject(new DOMException("aborted", "TimeoutError")))
			})
		})
		const client = createJevClient({ ...CLIENT_OPTIONS, timeoutMs: 5, fetchImpl })
		await expect(client.decide(REQUEST)).rejects.toThrowError(new JevError("transport_or_schema_error"))
	})

	it("honors an external abort signal without retrying", async () => {
		const controller = new AbortController()
		const fetchImpl = vi.fn((_input: string | URL | Request, init?: RequestInit) => {
			return new Promise<Response>((_resolve, reject) => {
				init?.signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")))
			})
		})
		const client = createJevClient({ ...CLIENT_OPTIONS, fetchImpl })
		const result = client.decide(REQUEST, controller.signal)
		controller.abort()
		await expect(result).rejects.toThrowError(new JevError("transport_or_schema_error"))
		expect(fetchImpl).toHaveBeenCalledTimes(1)
	})

	it("rejects non-JSON success bodies", async () => {
		const fetchImpl = vi.fn(async (_input: string | URL | Request, _init?: RequestInit) => new Response("<html>not json</html>", { status: 200 }))
		const client = createJevClient({ ...CLIENT_OPTIONS, fetchImpl })
		await expect(client.decide(REQUEST)).rejects.toThrowError(new JevError("transport_or_schema_error"))
	})
})
