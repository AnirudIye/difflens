import type { ApiErrorEnvelope, Me } from "./types";

export class ApiError extends Error {
  status: number;
  code: string;
  details: Record<string, unknown>;

  constructor(
    status: number,
    code: string,
    message: string,
    details: Record<string, unknown> = {},
  ) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

export async function apiFetch<T>(
  path: string,
  init?: RequestInit,
): Promise<T> {
  const res = await fetch(`/api/backend${path}`, {
    credentials: "same-origin",
    ...init,
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
      ...init?.headers,
    },
  });

  if (!res.ok) {
    let code = "unknown_error";
    let message = `Request failed with status ${res.status}`;
    let details: Record<string, unknown> = {};
    try {
      const body = (await res.json()) as ApiErrorEnvelope;
      if (body.error) {
        code = body.error.code ?? code;
        message = body.error.message ?? message;
        details = body.error;
      }
    } catch {
      // Body was not the JSON envelope (proxy error, empty body): keep defaults.
    }
    throw new ApiError(res.status, code, message, details);
  }

  if (res.status === 204) {
    return undefined as T;
  }
  return res.json() as Promise<T>;
}

export async function getMe(): Promise<Me | null> {
  try {
    return await apiFetch<Me>("/auth/me");
  } catch (err) {
    if (err instanceof ApiError && err.status === 401) {
      return null;
    }
    throw err;
  }
}
