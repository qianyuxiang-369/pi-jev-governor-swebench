import { describe, expect, it } from "vitest"
import {
	buildOutcomeEvidence,
	buildToolEvidence,
	EvidenceTooLargeError,
	MAX_CONTEXT_CHARS,
	MAX_TOOL_EVIDENCE,
	redactSensitiveKeys,
	scrubSecretLiterals,
	serializeEvidence,
	SENSITIVE_KEY_PATTERN,
	SENSITIVE_TOOL_OMITTED,
	TEXT_TRUNCATE_CHARS,
	TOOL_JSON_TRUNCATE_CHARS,
	truncateText,
	type EvidenceContextMessage,
} from "../src/evidence.ts"

function toolMessage(tool: string, text: string, isError = false): EvidenceContextMessage {
	return {
		role: "toolResult",
		toolName: tool,
		isError,
		content: [{ type: "text", text }],
	}
}

function assistantToolCall(id: string, name: string, args: unknown): EvidenceContextMessage {
	return {
		role: "assistant",
		content: [{ type: "toolCall", id, name, arguments: args }],
	}
}

describe("redaction rules", () => {
	it("rule 1 (primary): omits entries whose tool ARGUMENTS reference sensitive paths, even when the output text is clean", () => {
		// A read of .env whose output is plain KEY=value text — no sensitive
		// marker in the output, and key redaction cannot touch plain strings.
		const evidence = buildToolEvidence([
			assistantToolCall("c1", "read", { path: "/app/.env" }),
			{
				role: "toolResult",
				toolCallId: "c1",
				toolName: "read",
				isError: false,
				content: [{ type: "text", text: "API_TOKEN=plain-secret-value\nDB_PASSWORD=hunter2" }],
			},
		])
		expect(evidence[0]?.evidence).toBe(SENSITIVE_TOOL_OMITTED)
		expect(JSON.stringify(evidence)).not.toContain("plain-secret-value")
		expect(JSON.stringify(evidence)).not.toContain("hunter2")
	})

	it("rule 1 (primary): matches credentials, .pem, .key and id_rsa in arguments", () => {
		for (const marker of ["credentials.json", "server.pem", "id_rsa", "private.key"]) {
			const evidence = buildToolEvidence([
				assistantToolCall("c1", "read", { path: `/etc/${marker}` }),
				{ role: "toolResult", toolCallId: "c1", toolName: "read", isError: false, content: [] },
			])
			expect(evidence[0]?.evidence).toBe(SENSITIVE_TOOL_OMITTED)
		}
	})

	it("rule 1 (second layer): omits entries whose OUTPUT text itself looks sensitive", () => {
		const evidence = buildToolEvidence([toolMessage("read", "/home/user/.env contents")])
		expect(evidence[0]?.evidence).toBe(SENSITIVE_TOOL_OMITTED)
		expect(JSON.stringify(evidence)).not.toContain(".env contents")
	})

	it("rule 1 (second layer): omits output embedding credential contexts (benign file, bearer content)", () => {
		for (const text of [
			'curl -H "Authorization: Bearer smoke-secret-token" https://example.invalid/',
			"token: Bearer abcdef123456",
			"-----BEGIN RSA PRIVATE KEY-----",
		]) {
			const evidence = buildToolEvidence([toolMessage("read", text)])
			expect(evidence[0]?.evidence).toBe(SENSITIVE_TOOL_OMITTED)
			expect(JSON.stringify(evidence)).not.toContain("smoke-secret-token")
		}
	})

	it("rule 1 (second layer): keeps hash constants and git SHAs (no high-entropy false positives)", () => {
		const evidence = buildToolEvidence([toolMessage("bash", "ref-a1b2c3d4e5f60718 commit 9f2b6c1")])
		expect(evidence[0]?.evidence).toContain("ref-a1b2c3d4e5f60718")
	})

	it("includes redacted tool arguments for non-sensitive calls", () => {
		const evidence = buildToolEvidence([
			assistantToolCall("c2", "bash", { command: "npm test", apiKey: "sk-live" }),
			{ role: "toolResult", toolCallId: "c2", toolName: "bash", isError: true, content: [{ type: "text", text: "2 failed" }] },
		])
		expect(evidence[0]?.evidence).toContain("npm test")
		expect(evidence[0]?.evidence).toContain("[redacted]")
		expect(evidence[0]?.evidence).not.toContain("sk-live")
		expect(evidence[0]?.status).toBe("failed")
	})

	it("rule 2a: redacts values under sensitive keys at any depth", () => {
		const redacted = redactSensitiveKeys({
			apiKey: "sk-abc",
			nested: { authorization: "Bearer x", keep: "visible" },
			list: [{ password: "hunter2" }, { name: "ok" }],
		}) as Record<string, unknown>
		expect(redacted["apiKey"]).toBe("[redacted]")
		expect((redacted["nested"] as Record<string, unknown>)["authorization"]).toBe("[redacted]")
		expect((redacted["nested"] as Record<string, unknown>)["keep"]).toBe("visible")
		const list = redacted["list"] as Array<Record<string, unknown>>
		expect(list[0]!["password"]).toBe("[redacted]")
		expect(list[1]!["name"]).toBe("ok")
	})

	it("rule 2a: matches api key variants in key names", () => {
		for (const key of ["apiKey", "api_key", "X-Api-Key", "access_token", "clientSecret", "PASSWORD"]) {
			expect(SENSITIVE_KEY_PATTERN.test(key)).toBe(true)
		}
		expect(SENSITIVE_KEY_PATTERN.test("keyboard")).toBe(false)
	})

	it("rule 2b: scrubs configured secret literals from serialized text", () => {
		const text = 'the key sk-live-abcdefghijklmnop appears here and again sk-live-abcdefghijklmnop'
		const scrubbed = scrubSecretLiterals(text, ["sk-live-abcdefghijklmnop"])
		expect(scrubbed).not.toContain("sk-live-abcdefghijklmnop")
		expect(scrubbed.split("[redacted]")).toHaveLength(3)
		// short literals are not scrubbed (avoid destroying ordinary words)
		expect(scrubSecretLiterals("token", ["token"])).toBe("token")
	})

	it("rule 3: rejects oversized serialized contexts explicitly", () => {
		// The per-part truncation limits normally keep evidence small; this cap
		// is the backstop for unbounded parts (e.g. many approved operations).
		const evidence = { blob: "x".repeat(MAX_CONTEXT_CHARS + 10) }
		expect(() => serializeEvidence(evidence, [])).toThrowError(EvidenceTooLargeError)
		expect(() => serializeEvidence(evidence, [])).toThrowError("Decision context too large")
		expect(() => serializeEvidence({ small: "ok" }, [])).not.toThrow()
	})
})

describe("buildToolEvidence", () => {
	it("takes the tail 10 tool results only", () => {
		const messages = Array.from({ length: 15 }, (_, i) => toolMessage("read", `file-${i}`))
		const evidence = buildToolEvidence(messages)
		expect(evidence).toHaveLength(MAX_TOOL_EVIDENCE)
		expect(evidence[0]?.evidence).toContain("file-5")
		expect(evidence[9]?.evidence).toContain("file-14")
	})

	it("ignores non-tool messages", () => {
		const messages: EvidenceContextMessage[] = [
			{ role: "user", content: "hello" },
			{ role: "assistant", content: [{ type: "text", text: "thinking" }] },
			toolMessage("bash", "ls"),
		]
		expect(buildToolEvidence(messages)).toHaveLength(1)
	})

	it("marks failed tools and truncates tool json to the limit", () => {
		const long = "a".repeat(TOOL_JSON_TRUNCATE_CHARS + 100)
		const evidence = buildToolEvidence([toolMessage("bash", long, true)])
		expect(evidence[0]?.status).toBe("failed")
		expect(evidence[0]!.evidence.length).toBeLessThanOrEqual(TOOL_JSON_TRUNCATE_CHARS + 20)
		expect(evidence[0]!.evidence.endsWith("…[truncated]")).toBe(true)
	})
})

describe("buildOutcomeEvidence", () => {
	it("truncates the task prompt to the text limit and carries optional parts", () => {
		const evidence = buildOutcomeEvidence({
			originalPrompt: "p".repeat(TEXT_TRUNCATE_CHARS + 50),
			phase: "executing",
			contextMessages: [toolMessage("read", "ok")],
			gitDiffStat: " file | 1 +",
			approvedActions: ["bash ls"],
		})
		expect(evidence.task_prompt.length).toBeLessThanOrEqual(TEXT_TRUNCATE_CHARS + 20)
		expect(evidence.git_diff_stat).toBe(" file | 1 +")
		expect(evidence.approved_operations).toEqual(["bash ls"])
		expect(evidence.tool_evidence).toHaveLength(1)
	})

	it("omits empty optional parts", () => {
		const evidence = buildOutcomeEvidence({
			originalPrompt: "task",
			phase: "direct",
			contextMessages: [],
		})
		expect(evidence).not.toHaveProperty("approved_operations")
		expect(evidence).not.toHaveProperty("git_diff_stat")
	})
})

describe("truncateText", () => {
	it("keeps short text and marks truncated text", () => {
		expect(truncateText("short", 10)).toBe("short")
		expect(truncateText("0123456789A", 10)).toBe("0123456789…[truncated]")
	})
})
