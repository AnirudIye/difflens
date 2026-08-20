"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import Header from "@/components/Header";
import { apiFetch } from "@/lib/api";
import { useMe } from "@/lib/useMe";

export default function DashboardPage() {
  const router = useRouter();
  const me = useMe();

  async function signOut() {
    await apiFetch<undefined>("/auth/logout", { method: "POST" });
    router.push("/login");
  }

  return (
    <div className="shell">
      <Header me={me} onSignOut={signOut} />
      <main className="dash">
        {me.kind === "loading" ? (
          <p className="muted">Checking your session...</p>
        ) : me.kind === "authed" ? (
          <>
            <p className="signed-in">
              {/* eslint-disable-next-line @next/next/no-img-element -- avatars come from GitHub's CDN, no image loader configured */}
              <img
                className="avatar"
                src={me.me.avatar_url}
                alt=""
                width={40}
                height={40}
              />
              Signed in as {me.me.login}
            </p>
            <Link className="button" href="/repositories">
              Browse your repositories
            </Link>
          </>
        ) : me.kind === "unreachable" ? (
          <>
            <p className="muted">
              Cannot reach the review server right now, so your account details
              are missing. The free tier sleeps when idle; this page keeps
              retrying. Everything below still works.
            </p>
            <div className="review-actions">
              <Link className="button" href="/repositories">
                Browse your repositories
              </Link>
              <Link className="button button-quiet" href="/settings">
                Settings
              </Link>
            </div>
          </>
        ) : (
          <p className="muted">
            You are not signed in. <Link href="/login">Go to sign in</Link>.
          </p>
        )}
      </main>
    </div>
  );
}
