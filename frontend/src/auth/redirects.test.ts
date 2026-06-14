import { afterEach, describe, expect, it, vi } from "vitest";

import { resolveSignInRedirectUrl } from "@/auth/redirects";

describe("resolveSignInRedirectUrl", () => {
  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it("defaults to /onboarding when redirect is missing", () => {
    expect(resolveSignInRedirectUrl(null)).toBe("/onboarding");
  });

  it("allows safe relative paths", () => {
    expect(resolveSignInRedirectUrl("/dashboard?tab=ops#queue")).toBe(
      "/dashboard?tab=ops#queue",
    );
  });

  it("rejects protocol-relative urls", () => {
    expect(resolveSignInRedirectUrl("//evil.example.com/path")).toBe(
      "/onboarding",
    );
  });

  it("rejects external absolute urls", () => {
    expect(resolveSignInRedirectUrl("https://evil.example.com/steal")).toBe(
      "/onboarding",
    );
  });

  it("accepts same-origin absolute urls and normalizes to path", () => {
    const url = `${window.location.origin}/projects/new?src=invite#top`;
    expect(resolveSignInRedirectUrl(url)).toBe("/projects/new?src=invite#top");
  });
});
