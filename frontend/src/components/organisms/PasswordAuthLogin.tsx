"use client";

import { useEffect, useState } from "react";
import { Lock, Mail, User } from "lucide-react";

import {
  clearPasswordTokens,
  isPasswordAuthMode,
  setPasswordTokens,
  setPasswordUser,
  scheduleTokenRefresh,
} from "@/auth/passwordAuth";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { getApiBaseUrl } from "@/lib/api-base";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

type FormMode = "loading" | "register" | "login";

type PasswordAuthLoginProps = {
  onAuthenticated?: () => void;
};

const defaultOnAuthenticated = () => window.location.reload();

// ---------------------------------------------------------------------------
// API helpers
// ---------------------------------------------------------------------------

async function fetchSetupStatus(baseUrl: string): Promise<boolean> {
  const response = await fetch(`${baseUrl}/api/v1/auth/setup-status`);
  if (!response.ok) return false;
  const data = (await response.json()) as { needs_setup: boolean };
  return data.needs_setup;
}

async function submitRegister(
  baseUrl: string,
  email: string,
  password: string,
  name: string,
): Promise<{ ok: true; data: AuthResponse } | { ok: false; error: string }> {
  try {
    const response = await fetch(`${baseUrl}/api/v1/auth/register`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, password, name: name || undefined }),
    });
    if (response.ok) {
      const data = (await response.json()) as AuthResponse;
      return { ok: true, data };
    }
    const body = (await response.json().catch(() => null)) as {
      detail?: string;
    } | null;
    if (response.status === 409) {
      return { ok: false, error: "An account with this email already exists." };
    }
    return {
      ok: false,
      error: body?.detail ?? `Registration failed (HTTP ${response.status}).`,
    };
  } catch {
    return { ok: false, error: "Unable to reach the server." };
  }
}

async function submitLogin(
  baseUrl: string,
  email: string,
  password: string,
): Promise<{ ok: true; data: AuthResponse } | { ok: false; error: string }> {
  try {
    const response = await fetch(`${baseUrl}/api/v1/auth/login`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, password }),
    });
    if (response.ok) {
      const data = (await response.json()) as AuthResponse;
      return { ok: true, data };
    }
    const body = (await response.json().catch(() => null)) as {
      detail?: string;
    } | null;
    if (response.status === 401) {
      return { ok: false, error: "Invalid email or password." };
    }
    return {
      ok: false,
      error: body?.detail ?? `Login failed (HTTP ${response.status}).`,
    };
  } catch {
    return { ok: false, error: "Unable to reach the server." };
  }
}

// ---------------------------------------------------------------------------
// Response types (mirrors backend AuthResponse)
// ---------------------------------------------------------------------------

type AuthResponse = {
  user: { email: string; name: string | null };
  tokens: { access_token: string; refresh_token: string };
};

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function PasswordAuthLogin({
  onAuthenticated,
}: PasswordAuthLoginProps) {
  const [mode, setMode] = useState<FormMode>("loading");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [name, setName] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);

  // Determine register vs login based on setup-status
  useEffect(() => {
    if (!isPasswordAuthMode()) return;
    let cancelled = false;

    (async () => {
      let baseUrl: string;
      try {
        baseUrl = getApiBaseUrl();
      } catch {
        if (!cancelled) setMode("login");
        return;
      }

      try {
        const needsSetup = await fetchSetupStatus(baseUrl);
        if (!cancelled) setMode(needsSetup ? "register" : "login");
      } catch {
        if (!cancelled) setMode("login");
      }
    })();

    return () => {
      cancelled = true;
    };
  }, []);

  const handleSubmit = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setError(null);

    const cleanedEmail = email.trim();
    if (!cleanedEmail) {
      setError("Email is required.");
      return;
    }
    if (!password) {
      setError("Password is required.");
      return;
    }
    if (password.length < 8) {
      setError("Password must be at least 8 characters.");
      return;
    }

    let baseUrl: string;
    try {
      baseUrl = getApiBaseUrl();
    } catch {
      setError("Unable to resolve backend URL.");
      return;
    }

    setIsSubmitting(true);

    const result =
      mode === "register"
        ? await submitRegister(baseUrl, cleanedEmail, password, name.trim())
        : await submitLogin(baseUrl, cleanedEmail, password);

    setIsSubmitting(false);

    if (!result.ok) {
      setError(result.error);
      return;
    }

    // Store tokens + user profile
    setPasswordTokens(result.data.tokens.access_token, result.data.tokens.refresh_token);
    setPasswordUser({
      email: result.data.user.email,
      name: result.data.user.name,
    });
    scheduleTokenRefresh(baseUrl);
    (onAuthenticated ?? defaultOnAuthenticated)();
  };

  const isRegister = mode === "register";
  const title = isRegister ? "Create Admin Account" : "Sign In";
  const subtitle = isRegister
    ? "Set up your admin account to get started."
    : "Sign in to your account.";
  const badge = isRegister ? "First-time setup" : "Password mode";

  return (
    <div className="relative flex min-h-screen items-center justify-center overflow-hidden bg-app px-4 py-10">
      <div className="pointer-events-none absolute inset-0">
        <div className="absolute -top-28 -left-24 h-72 w-72 rounded-full bg-[color:var(--accent-soft)] blur-3xl" />
        <div className="absolute -right-28 -bottom-24 h-80 w-80 rounded-full bg-[rgba(14,165,233,0.12)] blur-3xl" />
      </div>

      <Card className="relative w-full max-w-lg animate-fade-in-up">
        <CardHeader className="space-y-5 border-b border-[color:var(--border)] pb-5">
          <div className="flex items-center justify-between">
            <span className="rounded-full border border-[color:var(--border)] bg-[color:var(--surface-muted)] px-3 py-1 text-xs font-semibold uppercase tracking-[0.08em] text-muted">
              {badge}
            </span>
            <div className="rounded-xl bg-[color:var(--accent-soft)] p-2 text-[color:var(--accent)]">
              <Lock className="h-5 w-5" />
            </div>
          </div>
          <div className="space-y-1">
            <h1 className="text-2xl font-semibold tracking-tight text-strong">
              {title}
            </h1>
            <p className="text-sm text-muted">{subtitle}</p>
          </div>
        </CardHeader>
        <CardContent className="pt-5">
          {mode === "loading" ? (
            <div className="flex items-center justify-center py-8">
              <div className="h-6 w-6 animate-spin rounded-full border-2 border-[color:var(--accent)] border-t-transparent" />
            </div>
          ) : (
            <form onSubmit={handleSubmit} className="space-y-4">
              {isRegister && (
                <div className="space-y-2">
                  <label
                    htmlFor="pw-name"
                    className="text-xs font-semibold uppercase tracking-[0.08em] text-muted"
                  >
                    Name
                  </label>
                  <div className="relative">
                    <User className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted" />
                    <Input
                      id="pw-name"
                      type="text"
                      value={name}
                      onChange={(e) => setName(e.target.value)}
                      placeholder="Your name"
                      disabled={isSubmitting}
                      className="pl-9"
                    />
                  </div>
                </div>
              )}

              <div className="space-y-2">
                <label
                  htmlFor="pw-email"
                  className="text-xs font-semibold uppercase tracking-[0.08em] text-muted"
                >
                  Email
                </label>
                <div className="relative">
                  <Mail className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted" />
                  <Input
                    id="pw-email"
                    type="email"
                    value={email}
                    onChange={(e) => setEmail(e.target.value)}
                    placeholder="you@example.com"
                    autoFocus
                    disabled={isSubmitting}
                    className="pl-9"
                  />
                </div>
              </div>

              <div className="space-y-2">
                <label
                  htmlFor="pw-password"
                  className="text-xs font-semibold uppercase tracking-[0.08em] text-muted"
                >
                  Password
                </label>
                <div className="relative">
                  <Lock className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted" />
                  <Input
                    id="pw-password"
                    type="password"
                    value={password}
                    onChange={(e) => setPassword(e.target.value)}
                    placeholder="••••••••"
                    disabled={isSubmitting}
                    className="pl-9"
                  />
                </div>
              </div>

              {error ? (
                <p className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">
                  {error}
                </p>
              ) : (
                <p className="text-xs text-muted">
                  Password must be at least 8 characters.
                </p>
              )}

              <Button
                type="submit"
                className="w-full"
                size="lg"
                disabled={isSubmitting}
              >
                {isSubmitting
                  ? isRegister
                    ? "Creating account..."
                    : "Signing in..."
                  : isRegister
                    ? "Create account"
                    : "Sign in"}
              </Button>
            </form>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
