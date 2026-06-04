import { afterEach, describe, expect, it, vi } from "vitest";

import { getApiBaseUrl } from "./api-base";

describe("getApiBaseUrl", () => {
  afterEach(() => {
    vi.unstubAllEnvs();
    // Clean up runtime config between tests
    if (typeof window !== "undefined") {
      delete (window as Record<string, unknown>).__RUNTIME_CONFIG__;
    }
  });

  it("returns normalized explicit URL", () => {
    vi.stubEnv("NEXT_PUBLIC_API_URL", "https://api.example.com///");

    expect(getApiBaseUrl()).toBe("https://api.example.com");
  });

  it("auto-resolves from browser host using runtime config port", () => {
    vi.stubEnv("NEXT_PUBLIC_API_URL", "auto");
    (window as Record<string, unknown>).__RUNTIME_CONFIG__ = {
      backendPort: "8887",
    };

    expect(getApiBaseUrl()).toBe("http://localhost:8887");
  });

  it("falls back to port 8000 when runtime config is absent", () => {
    vi.stubEnv("NEXT_PUBLIC_API_URL", "auto");

    expect(getApiBaseUrl()).toBe("http://localhost:8000");
  });

  it("auto-resolves from browser host when unset", () => {
    vi.stubEnv("NEXT_PUBLIC_API_URL", "");
    (window as Record<string, unknown>).__RUNTIME_CONFIG__ = {
      backendPort: "9999",
    };

    expect(getApiBaseUrl()).toBe("http://localhost:9999");
  });
});
