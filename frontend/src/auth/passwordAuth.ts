"use client";

import { AuthMode } from "@/auth/mode";

// ---------------------------------------------------------------------------
// Storage keys
// ---------------------------------------------------------------------------

const ACCESS_KEY = "mc_pw_access_token";
const REFRESH_KEY = "mc_pw_refresh_token";
const USER_KEY = "mc_pw_user";

// ---------------------------------------------------------------------------
// Module-level cache (avoids localStorage reads on every API call)
// ---------------------------------------------------------------------------

let cachedAccessToken: string | null = null;
let refreshTimer: ReturnType<typeof setTimeout> | null = null;

// ---------------------------------------------------------------------------
// Mode check
// ---------------------------------------------------------------------------

export function isPasswordAuthMode(): boolean {
  return process.env.NEXT_PUBLIC_AUTH_MODE === AuthMode.Password;
}

// ---------------------------------------------------------------------------
// Token accessors
// ---------------------------------------------------------------------------

export function getPasswordAccessToken(): string | null {
  if (cachedAccessToken) return cachedAccessToken;
  if (typeof window === "undefined") return null;
  try {
    const stored = window.localStorage.getItem(ACCESS_KEY);
    if (stored) {
      cachedAccessToken = stored;
      return stored;
    }
  } catch {
    // Ignore storage failures.
  }
  return null;
}

export function getPasswordRefreshToken(): string | null {
  if (typeof window === "undefined") return null;
  try {
    return window.localStorage.getItem(REFRESH_KEY);
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------------------
// Token persistence
// ---------------------------------------------------------------------------

export function setPasswordTokens(access: string, refresh: string): void {
  cachedAccessToken = access;
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(ACCESS_KEY, access);
    window.localStorage.setItem(REFRESH_KEY, refresh);
  } catch {
    // Ignore storage failures.
  }
}

// ---------------------------------------------------------------------------
// User profile persistence (for display in UserMenu without extra API call)
// ---------------------------------------------------------------------------

export type PasswordUser = {
  email: string;
  name: string | null;
};

export function setPasswordUser(user: PasswordUser): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(USER_KEY, JSON.stringify(user));
  } catch {
    // Ignore storage failures.
  }
}

export function getPasswordUser(): PasswordUser | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = window.localStorage.getItem(USER_KEY);
    if (!raw) return null;
    return JSON.parse(raw) as PasswordUser;
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------------------
// Clear all
// ---------------------------------------------------------------------------

export function clearPasswordTokens(): void {
  cachedAccessToken = null;
  if (refreshTimer !== null) {
    clearTimeout(refreshTimer);
    refreshTimer = null;
  }
  if (typeof window === "undefined") return;
  try {
    window.localStorage.removeItem(ACCESS_KEY);
    window.localStorage.removeItem(REFRESH_KEY);
    window.localStorage.removeItem(USER_KEY);
  } catch {
    // Ignore storage failures.
  }
}

// ---------------------------------------------------------------------------
// Auto-refresh: decode JWT exp and schedule refresh 60s before expiry
// ---------------------------------------------------------------------------

function decodeJwtExp(token: string): number | null {
  try {
    const parts = token.split(".");
    if (parts.length !== 3) return null;
    // base64url → base64
    let payload = parts[1]!;
    payload = payload.replace(/-/g, "+").replace(/_/g, "/");
    // Pad to multiple of 4
    const pad = payload.length % 4;
    if (pad) payload += "=".repeat(4 - pad);
    const decoded = JSON.parse(atob(payload)) as { exp?: number };
    return typeof decoded.exp === "number" ? decoded.exp : null;
  } catch {
    return null;
  }
}

export function scheduleTokenRefresh(baseUrl: string): void {
  // Cancel any existing timer
  if (refreshTimer !== null) {
    clearTimeout(refreshTimer);
    refreshTimer = null;
  }

  const token = getPasswordAccessToken();
  if (!token) return;

  const exp = decodeJwtExp(token);
  if (!exp) return;

  const nowSec = Math.floor(Date.now() / 1000);
  const ttlSec = exp - nowSec;

  // Refresh 60s before expiry, but at least 5s from now
  const delayMs = Math.max((ttlSec - 60) * 1000, 5000);

  refreshTimer = setTimeout(async () => {
    const refreshToken = getPasswordRefreshToken();
    if (!refreshToken) {
      clearPasswordTokens();
      window.location.reload();
      return;
    }

    try {
      const response = await fetch(`${baseUrl}/api/v1/auth/refresh`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ refresh_token: refreshToken }),
      });

      if (!response.ok) {
        clearPasswordTokens();
        window.location.reload();
        return;
      }

      const data = (await response.json()) as {
        access_token: string;
        refresh_token: string;
      };
      setPasswordTokens(data.access_token, data.refresh_token);
      // Reschedule with new token
      scheduleTokenRefresh(baseUrl);
    } catch {
      clearPasswordTokens();
      window.location.reload();
    }
  }, delayMs);
}
