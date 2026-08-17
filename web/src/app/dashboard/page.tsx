"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import Header from "@/components/Header";
import { apiFetch, getMe } from "@/lib/api";
import type { Me } from "@/lib/types";

export default function DashboardPage() {
  const router = useRouter();
  const [me, setMe] = useState<Me | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    getMe()
      .then(setMe)
      .catch(() => setMe(null))
      .finally(() => setLoading(false));
  }, []);

  async function signOut() {
    await apiFetch<undefined>("/auth/logout", { method: "POST" });
    router.push("/login");
  }

  return (
    <div className="shell">
      <Header user={me} onSignOut={signOut} />
      <main className="dash">
        {loading ? (
          <p className="muted">Checking your session...</p>
        ) : me ? (
          <>
            <p className="signed-in">
              {/* eslint-disable-next-line @next/next/no-img-element -- avatars come from GitHub's CDN, no image loader configured */}
              <img
                className="avatar"
                src={me.avatar_url}
                alt=""
                width={40}
                height={40}
              />
              Signed in as {me.login}
            </p>
            <Link className="button" href="/repositories">
              Browse your repositories
            </Link>
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
