"use client";

// Auth wrappers — password-only mode.
// Kept as re-exports with the same API surface so consumers don't need changes.

import type { ReactNode } from "react";

import { getPasswordAccessToken } from "@/auth/passwordAuth";

function hasToken(): boolean {
  return Boolean(getPasswordAccessToken());
}

export function SignedIn(props: { children: ReactNode }) {
  return hasToken() ? <>{props.children}</> : null;
}

export function SignedOut(props: { children: ReactNode }) {
  return hasToken() ? null : <>{props.children}</>;
}

// Renders children inside a link to /sign-in (replaces Clerk modal).
// Accepts the same props as the old Clerk component so consumers compile.
export function SignInButton(props: {
  children?: ReactNode;
  mode?: string;
  forceRedirectUrl?: string;
  signUpForceRedirectUrl?: string;
}) {
  return <>{props.children}</>;
}

export function SignOutButton() {
  return null;
}

export function useUser() {
  return {
    isLoaded: true,
    isSignedIn: hasToken(),
    user: null,
  } as const;
}

export function useAuth() {
  const token = getPasswordAccessToken();
  return {
    isLoaded: true,
    isSignedIn: Boolean(token),
    userId: token ? "password-user" : null,
    sessionId: token ? "password-session" : null,
    getToken: async () => token,
  } as const;
}
