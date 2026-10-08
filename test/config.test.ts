import { describe, expect, it } from "vitest"
import {
	loadConfig,
	MAX_CORRECTIONS_HARD_CAP,
	parseModelRef,
	DEFAULT_JEV_URL,
	DEFAULT_JEV_MODEL,
	defaultLogDir,
} from "../src/config.ts"

const VALID_ENV = {
	PI_JEV_API_KEY: "sk-test-key-0123456789",
	PI_JEV_MODEL_SMALL: "anthropic:claude-haiku",
	PI_JEV_MODEL_NORMAL: "anthropic:claude-sonnet",
	PI_JEV_MODEL_STRONG: "openrouter:google/gemini-2.5-pro",
}

function load(env: Record<string, string | undefined>, flagEnabled = true) {
	return loadConfig({ env, flagEnabled, cwd: "/tmp/project", platform: "linux", homeDir: "/home/test" })
}

describe("parseModelRef", () => {
	it("splits on the first colon", () => {
		expect(parseModelRef("openrouter:google/gemini-2.5-pro")).toEqual({
			provider: "openrouter",
			modelId: "google/gemini-2.5-pro",
		})
	})

	it("keeps colons inside the model id", () => {
		expect(parseModelRef("provider:model:exacto")).toEqual({ provider: "provider", modelId: "model:exacto" })
	})

	it("rejects refs without a colon, empty parts, or whitespace", () => {
		expect(parseModelRef("claude-sonnet")).toBeUndefined()
		expect(parseModelRef(":model")).toBeUndefined()
		expect(parseModelRef("provider:")).toBeUndefined()
		expect(parseModelRef("pro vider:model")).toBeUndefined()
	})
})

describe("loadConfig", () => {
	it("fills defaults for a valid environment", () => {
		const { config, unavailableReason } = load(VALID_ENV)
		expect(unavailableReason).toBeUndefined()
		expect(config).toMatchObject({
			url: DEFAULT_JEV_URL,
			model: DEFAULT_JEV_MODEL,
			timeoutMs: 3000,
			routeConfidence: 0.8,
			permissionConfidence: 0.9,
			outcomeConfidence: 0.8,
			maxCorrections: 2,
			outcomeGitDiff: false,
			planAutoExecute: true,
			logDir: "/home/test/.local/state/pi-jev",
			apiKey: "sk-test-key-0123456789",
		})
		expect(config?.models).toEqual({
			small: "anthropic:claude-haiku",
			normal: "anthropic:claude-sonnet",
			strong: "openrouter:google/gemini-2.5-pro",
		})
	})

	it("disables remote decisions when the flag is disabled", () => {
		const { config, unavailableReason } = load(VALID_ENV, false)
		expect(config).toBeUndefined()
		expect(unavailableReason).toContain("flag")
	})

	it("disables remote decisions when PI_JEV_ENABLED is false", () => {
		const { config } = load({ ...VALID_ENV, PI_JEV_ENABLED: "false" })
		expect(config).toBeUndefined()
	})

	it("reports remote decisions unavailable when the API key is missing", () => {
		const { config, unavailableReason } = load({})
		expect(config).toBeUndefined()
		expect(unavailableReason).toContain("PI_JEV_API_KEY")
	})

	it("rejects an invalid URL", () => {
		const { config } = load({ ...VALID_ENV, PI_JEV_URL: "not a url" })
		expect(config).toBeUndefined()
	})

	it("rejects non-http(s) URLs", () => {
		const { config } = load({ ...VALID_ENV, PI_JEV_URL: "ftp://example.com" })
		expect(config).toBeUndefined()
	})

	it("accepts valid overrides", () => {
		const { config } = load({
			...VALID_ENV,
			PI_JEV_URL: "https://decisions.example.com/v1",
			PI_JEV_MODEL: "custom/jev-2",
			PI_JEV_TIMEOUT_MS: "5000",
			PI_JEV_ROUTE_CONFIDENCE: "0.7",
			PI_JEV_PERMISSION_CONFIDENCE: "0.95",
			PI_JEV_OUTCOME_CONFIDENCE: "0.6",
			PI_JEV_MAX_CORRECTIONS: "1",
			PI_JEV_OUTCOME_GIT_DIFF: "false",
			PI_JEV_PLAN_AUTO_EXECUTE: "false",
			PI_JEV_LOG_DIR: "/var/log/jev",
		})
		expect(config).toMatchObject({
			url: "https://decisions.example.com/v1",
			model: "custom/jev-2",
			timeoutMs: 5000,
			routeConfidence: 0.7,
			permissionConfidence: 0.95,
			outcomeConfidence: 0.6,
			maxCorrections: 1,
			outcomeGitDiff: false,
			planAutoExecute: false,
			logDir: "/var/log/jev",
		})
	})

	it("rejects timeout values out of bounds", () => {
		expect(load({ ...VALID_ENV, PI_JEV_TIMEOUT_MS: "0" }).config).toBeUndefined()
		expect(load({ ...VALID_ENV, PI_JEV_TIMEOUT_MS: "60001" }).config).toBeUndefined()
		expect(load({ ...VALID_ENV, PI_JEV_TIMEOUT_MS: "abc" }).config).toBeUndefined()
	})

	it("rejects confidences out of bounds", () => {
		expect(load({ ...VALID_ENV, PI_JEV_ROUTE_CONFIDENCE: "1.5" }).config).toBeUndefined()
		expect(load({ ...VALID_ENV, PI_JEV_PERMISSION_CONFIDENCE: "-0.1" }).config).toBeUndefined()
	})

	it("rejects missing or malformed tier models", () => {
		expect(load({ ...VALID_ENV, PI_JEV_MODEL_SMALL: undefined }).config).toBeUndefined()
		expect(load({ ...VALID_ENV, PI_JEV_MODEL_STRONG: "no-colon" }).config).toBeUndefined()
	})

	it("hard-caps max corrections at 2 but rejects negatives", () => {
		expect(load({ ...VALID_ENV, PI_JEV_MAX_CORRECTIONS: "9" }).config?.maxCorrections).toBe(MAX_CORRECTIONS_HARD_CAP)
		expect(load({ ...VALID_ENV, PI_JEV_MAX_CORRECTIONS: "-1" }).config).toBeUndefined()
		expect(load({ ...VALID_ENV, PI_JEV_MAX_CORRECTIONS: "0" }).config?.maxCorrections).toBe(0)
	})

	it("uses the documented cross-platform log directory precedence", () => {
		expect(defaultLogDir({ PI_JEV_LOG_DIR: "/explicit", XDG_STATE_HOME: "/xdg" }, "linux", "/home/u")).toBe("/explicit")
		expect(defaultLogDir({ XDG_STATE_HOME: "/xdg" }, "darwin", "/Users/u")).toBe("/xdg/pi-jev")
		expect(defaultLogDir({}, "darwin", "/Users/u")).toBe("/Users/u/Library/Application Support/pi-jev")
		expect(defaultLogDir({ LOCALAPPDATA: "C:\\Users\\u\\AppData\\Local" }, "win32", "C:\\Users\\u")).toContain("pi-jev")
	})
})
