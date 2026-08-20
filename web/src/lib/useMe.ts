"use client";

import { useEffect, useState } from "react";
import { ApiError, apiFetch } from "./api";
import type { Me } from "./types";

/** Three answers, not two. "Could not ask" is not the same as "signed out",
 *  and collapsing them cost the header every navigation link it has. */
export type MeState =
  | { kind: "loading" }
  | { kind: "authed"; me: Me }
  | { kind: "anon" }
  | { kind: "unreachable" };

// Render's free tier wakes in 30-60s, so identity has to survive a cold start
// the way the review poll already does. Roughly 50s of attempts, then stop.
const RETRY_DELAYS_MS = [1000, 2000, 4000, 8000, 15000, 20000];

export function useMe(): MeState {
  const [state, setState] = useState<MeState>({ kind: "loading" });

  useEffect(() => {
    let stopped = false;
    let timer: number | undefined;

    async function attempt(index: number) {
      if (stopped) {
        return;
      }
      try {
        const me = await apiFetch<Me>("/auth/me");
        if (!stopped) {
          setState({ kind: "authed", me });
        }
      } catch (err) {
        if (stopped) {
          return;
        }
        if (err instanceof ApiError && err.status === 401) {
          setState({ kind: "anon" }); // the only definitive answer
          return;
        }
        setState({ kind: "unreachable" });
        const delay = RETRY_DELAYS_MS[index];
        if (delay !== undefined) {
          timer = window.setTimeout(() => void attempt(index + 1), delay);
        }
      }
    }

    void attempt(0);
    return () => {
      stopped = true;
      window.clearTimeout(timer);
    };
  }, []);

  return state;
}
