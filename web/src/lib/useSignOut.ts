"use client";

import { useCallback } from "react";
import { useRouter } from "next/navigation";
import { apiFetch } from "./api";

/** Sign out, and end up signed out either way.
 *
 * This was written out by hand on all five pages that render the header, and
 * every copy awaited the logout request with nothing around it. On a free
 * tier that sleeps, the request fails often enough to matter, and a failed
 * one left the person exactly where they were with no feedback at all: the
 * menu item was simply dead.
 *
 * The redirect happens regardless. The session cookie is HttpOnly, so the
 * browser cannot clear it here, but landing on the sign in page is honest
 * about what the user asked for, and the middleware sends them onward if the
 * session did survive.
 */
export function useSignOut(): () => Promise<void> {
  const router = useRouter();

  return useCallback(async () => {
    try {
      await apiFetch<undefined>("/auth/logout", { method: "POST" });
    } catch {
      // The server-side row may still exist. Nothing the user can do about
      // that from here, and stranding them on a page they asked to leave is
      // worse than sending them to sign in again.
    } finally {
      router.push("/login");
      router.refresh();
    }
  }, [router]);
}
