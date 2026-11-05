import { describe, expect, it } from "vitest";
import { ConfigError, loadConfig } from "../src/config.js";

describe("loadConfig", () => {
  it("returns defaults when the environment is empty", () => {
    expect(loadConfig({})).toEqual({
      baseUrl: "https://swapi.dev/api/",
      timeoutMs: 30_000,
    });
  });

  it("normalizes a custom base URL with trailing slashes", () => {
    const config = loadConfig({ SWAPI_BASE_URL: "https://example.com/swapi///" });
    expect(config.baseUrl).toBe("https://example.com/swapi/");
  });

  it("accepts a custom timeout", () => {
    expect(loadConfig({ SWAPI_TIMEOUT_MS: "15000" }).timeoutMs).toBe(15_000);
  });

  it("rejects an invalid base URL", () => {
    expect(() => loadConfig({ SWAPI_BASE_URL: "not-a-url" })).toThrow(ConfigError);
  });

  it("rejects a non-http(s) base URL", () => {
    expect(() => loadConfig({ SWAPI_BASE_URL: "ftp://example.com/api/" })).toThrow(
      ConfigError,
    );
  });

  it("rejects invalid timeouts", () => {
    for (const value of ["abc", "0", "-5", "10.5", ""]) {
      // Empty string falls back to the default, everything else must throw.
      if (value === "") {
        expect(loadConfig({ SWAPI_TIMEOUT_MS: value }).timeoutMs).toBe(30_000);
      } else {
        expect(() => loadConfig({ SWAPI_TIMEOUT_MS: value })).toThrow(ConfigError);
      }
    }
  });
});
