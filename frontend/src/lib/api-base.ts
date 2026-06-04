type RuntimeConfig = { backendPort: string };

declare global {
  interface Window {
    __RUNTIME_CONFIG__?: RuntimeConfig;
  }
}

export function getApiBaseUrl(): string {
  const raw = process.env.NEXT_PUBLIC_API_URL?.trim();
  if (raw && raw.toLowerCase() !== "auto") {
    const normalized = raw.replace(/\/+$/, "");
    if (!normalized) {
      throw new Error("NEXT_PUBLIC_API_URL is invalid.");
    }
    return normalized;
  }

  // Server-side (SSR): use Docker internal networking.
  if (typeof window === "undefined") {
    return "http://backend:8000";
  }

  // Client-side: read port from runtime config injected by docker-entrypoint.sh.
  // The file public/runtime-config.js sets window.__RUNTIME_CONFIG__ before any
  // page JS runs, so the value is always available here.
  const port = window.__RUNTIME_CONFIG__?.backendPort || "8000";
  const protocol = window.location.protocol === "https:" ? "https" : "http";
  const host = window.location.hostname;
  if (host) {
    return `${protocol}://${host}:${port}`;
  }

  throw new Error(
    "NEXT_PUBLIC_API_URL is not set and cannot be auto-resolved outside the browser.",
  );
}
