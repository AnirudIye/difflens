"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import Header from "@/components/Header";
import { ApiError, apiFetch, getMe } from "@/lib/api";
import { relativeTime } from "@/lib/time";
import type { Me, Repository } from "@/lib/types";

type LoadState =
  | { kind: "loading" }
  | { kind: "ready"; repos: Repository[] }
  | { kind: "reconnect" }
  | { kind: "error"; message: string };

export default function RepositoriesPage() {
  const router = useRouter();
  const [me, setMe] = useState<Me | null>(null);
  const [state, setState] = useState<LoadState>({ kind: "loading" });

  const load = useCallback(async () => {
    setState({ kind: "loading" });
    try {
      const data = await apiFetch<{ items: Repository[] }>(
        "/repositories?sync=true",
      );
      setState({ kind: "ready", repos: data.items });
    } catch (err) {
      if (err instanceof ApiError) {
        if (err.code === "github_reconnect_required") {
          setState({ kind: "reconnect" });
          return;
        }
        if (err.status === 401) {
          router.push("/login");
          return;
        }
        if (
          err.code === "github_rate_limited" ||
          err.code === "github_unavailable"
        ) {
          setState({
            kind: "error",
            message:
              "GitHub is not answering right now. Give it a minute and refresh.",
          });
          return;
        }
      }
      setState({
        kind: "error",
        message: "Something went wrong loading your repositories.",
      });
    }
  }, [router]);

  useEffect(() => {
    getMe()
      .then(setMe)
      .catch(() => setMe(null));
    void load();
  }, [load]);

  async function signOut() {
    await apiFetch<undefined>("/auth/logout", { method: "POST" });
    router.push("/login");
  }

  return (
    <div className="shell">
      <Header user={me} onSignOut={signOut} />
      <main className="dash">
        <div className="page-head">
          <h1 className="page-title">Repositories</h1>
          <button
            className="button button-quiet"
            type="button"
            onClick={() => void load()}
            disabled={state.kind === "loading"}
          >
            Refresh from GitHub
          </button>
        </div>

        {state.kind === "loading" ? (
          <p className="muted">Syncing with GitHub...</p>
        ) : state.kind === "reconnect" ? (
          <div className="notice">
            <p>
              DiffLens lost access to your GitHub account. Reconnect to keep
              your repositories in sync.
            </p>
            <a className="button" href="/api/backend/auth/github/login">
              Reconnect GitHub
            </a>
          </div>
        ) : state.kind === "error" ? (
          <p className="muted">{state.message}</p>
        ) : state.repos.length === 0 ? (
          <p className="muted">No public repositories on your account yet.</p>
        ) : (
          <ul className="row-list">
            {state.repos.map((repo) => (
              <li key={repo.id}>
                <Link className="row" href={`/repositories/${repo.id}`}>
                  <span className="mono">{repo.full_name}</span>
                  <span className="chip">{repo.default_branch}</span>
                  <span className="time-muted row-end">
                    synced {relativeTime(repo.last_synced_at)}
                  </span>
                  <span className="chevron" aria-hidden="true">
                    &rsaquo;
                  </span>
                </Link>
              </li>
            ))}
          </ul>
        )}
      </main>
    </div>
  );
}
