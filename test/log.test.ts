import { mkdtemp, readFile, rm, stat } from "node:fs/promises"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { afterEach, describe, expect, it } from "vitest"
import { appendDecisionLog, decisionLogFilename } from "../src/log.ts"

const tempDirs: string[] = []

afterEach(async () => {
	await Promise.all(tempDirs.map((dir) => rm(dir, { recursive: true, force: true })))
	tempDirs.length = 0
})

async function tempDir(): Promise<string> {
	const dir = await mkdtemp(join(tmpdir(), "pi-jev-log-"))
	tempDirs.push(dir)
	return dir
}

describe("appendDecisionLog", () => {
	it("appends one JSON line per decision and creates the directory", async () => {
		const logDir = join(await tempDir(), "nested", ".pi-jev")
		await appendDecisionLog(logDir, "session-1", {
			timestamp: "2026-10-07T00:00:00.000Z",
			decisionKind: "route",
			taskId: "t1",
			questions: ["plan_strategy", "tier"],
			effective: "phase=direct; tier=normal",
		})
		await appendDecisionLog(logDir, "session-1", {
			timestamp: "2026-10-07T00:00:01.000Z",
			decisionKind: "permission",
			taskId: "t1",
			effective: "allow (builtin read tool)",
		})
		const path = join(logDir, decisionLogFilename("session-1"))
		const content = await readFile(path, "utf8")
		const lines = content.trim().split("\n")
		expect(lines).toHaveLength(2)
		const first = JSON.parse(lines[0]!) as Record<string, unknown>
		expect(first["decisionKind"]).toBe("route")
		expect(first["effective"]).toBe("phase=direct; tier=normal")
		const second = JSON.parse(lines[1]!) as Record<string, unknown>
		expect(second["taskId"]).toBe("t1")
		expect((await stat(logDir)).mode & 0o777).toBe(0o700)
		expect((await stat(path)).mode & 0o777).toBe(0o600)
	})

	it("never embeds the API key when callers include it in error fields", async () => {
		const logDir = await tempDir()
		await appendDecisionLog(logDir, "session-2", {
			timestamp: "2026-10-07T00:00:00.000Z",
			decisionKind: "outcome",
			taskId: "t2",
			effective: "settle (outcome request failed: http_401)",
			error: "http_401",
		})
		const content = await readFile(join(logDir, decisionLogFilename("session-2")), "utf8")
		expect(content).not.toContain("Bearer")
		expect(content).not.toContain("authorization")
	})

	it("separates decisions by session", async () => {
		const logDir = await tempDir()
		const entry = {
			timestamp: "2026-10-07T00:00:00.000Z",
			decisionKind: "route" as const,
			taskId: "t1",
			effective: "direct",
		}
		await appendDecisionLog(logDir, "a", entry)
		await appendDecisionLog(logDir, "b", entry)
		expect(await readFile(join(logDir, decisionLogFilename("a")), "utf8")).toContain("t1")
		expect(await readFile(join(logDir, decisionLogFilename("b")), "utf8")).toContain("t1")
	})
})
