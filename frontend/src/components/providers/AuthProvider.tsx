"use client";

import { useEffect, type ReactNode } from "react";

import {
  getPasswordAccessToken,
  scheduleTokenRefresh,
} from "@/auth/passwordAuth";
import { PasswordAuthLogin } from "@/components/organisms/PasswordAuthLogin";
import { getApiBaseUrl } from "@/lib/api-base";

export function AuthProvider({ children }: { children: ReactNode }) {
  // Kick off token auto-refresh when we have a token.
  useEffect(() => {
    if (getPasswordAccessToken()) {
      try {
        scheduleTokenRefresh(getApiBaseUrl());
      } catch {
        // baseUrl not resolvable — skip scheduling.
      }
    }
  }, []);

  if (!getPasswordAccessToken()) {
    return <PasswordAuthLogin />;
  }

  return <>{children}</>;
}
