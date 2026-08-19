"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import Header from "@/components/Header";
import { ApiError, apiFetch, getMe } from "@/lib/api";
import { relativeTime } from "@/lib/time";
import type { Me, PullRequest, Review } from "@/lib/types";

type LoadState =
  | { kind: "loading" }
  | { kind: "ready"; pulls: PullRequest[] }
  | { kind: "reconnect" }
  | { kind: "error"; message: string };

export default function RepositoryPullsPage() {
  const router = useRouter();
  const params = useParams<{ id: string }>();
  const [me, setMe] = useState<Me | null>(null);
  const [state, setState] = useState<LoadState>({ kind: "loading" });
  const [startingId, setStartingId] = useState<string | null>(null);
  const [runNote, setRunNote] = useState<string | null>(null);

  const load = useCallback(async () => {
    setState({ kind: "loading" });
    try {
      const data = await apiFetch<{ items: PullRequest[] }>(
        `/repositories/${params.id}/pull-requests`,
      );
      setState({ kind: "ready", pulls: data.items });
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
              "GitHub is not answering right now. Give it a minute and try again.",
          });
          return;
        }
      }
      setState({
        kind: "error",
        message: "Something went wrong loading the pull requests.",
      });
    }
  }, [params.id, router]);

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

  async function runReview(pr: PullRequest) {
    setStartingId(pr.id);
    setRunNote(null);
    try {
      const review = await apiFetch<Review>("/reviews", {
        method: "POST",
        body: JSON.stringify({ pull_request_id: pr.id }),
      });
      router.push(`/reviews/${review.id}`);
      return;
    } catch (err) {
      if (err instanceof ApiError) {
        // Reconnect arrives as a 401 too, so the code check must come first
        if (err.code === "github_reconnect_required") {
          setState({ kind: "reconnect" });
        } else if (err.status === 401) {
          router.push("/login");
          return;
        } else if (err.code === "review_already_exists") {
          const existingId = err.details.review_id;
          if (typeof existingId === "string") {
            router.push(`/reviews/${existingId}`);
            return;
          }
          setRunNote(
            "A review by another user already covers this pull request at this commit.",
          );
        } else if (err.code === "pull_request_closed") {
          setRunNote(
            "That pull request has closed on GitHub since this list loaded.",
          );
          void load();
        } else if (err.status === 404) {
          setRunNote(
            "That pull request is not available anymore. The list has been refreshed.",
          );
          void load();
        } else if (
          err.code === "github_rate_limited" ||
          err.code === "github_unavailable"
        ) {
          setRunNote(
            "GitHub is not answering right now. Give it a minute and try again.",
          );
        } else {
          setRunNote("Starting the review failed. Try again.");
        }
      } else {
        setRunNote("Starting the review failed. Try again.");
      }
    }
    setStartingId(null);
  }

  return (
    <div className="shell">
      <Header user={me} onSignOut={signOut} />
      <main className="dash">
        <Link className="back-link" href="/repositories">
          &lsaquo; All repositories
        </Link>
        <div className="page-head">
          <h1 className="page-title">Pull requests</h1>
        </div>

        {runNote ? (
          <p className="muted run-note" role="alert">
            {runNote}
          </p>
        ) : null}

        {state.kind === "loading" ? (
          <p className="muted">Loading pull requests...</p>
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
        ) : state.pulls.length === 0 ? (
          <p className="muted">No open pull requests.</p>
        ) : (
          <ul className="row-list">
            {state.pulls.map((pr) => (
              <li className="row" key={pr.id}>
                <div className="pr-main">
                  <a
                    className="pr-title"
                    href={pr.html_url}
                    target="_blank"
                    rel="noopener noreferrer"
                  >
                    <span className="mono">#{pr.number}</span> {pr.title}
                  </a>
                  <p className="time-muted">
                    {pr.author_login}
                    {" - "}
                    <span className="mono">
                      {`${pr.head_ref} -> ${pr.base_ref}`}
                    </span>
                    {" - "}
                    <span className="mono">{pr.head_sha.slice(0, 7)}</span>
                    {" - updated "}
                    {relativeTime(pr.github_updated_at)}
                  </p>
                </div>
                <button
                  className="button button-quiet row-end"
                  type="button"
                  disabled={startingId !== null}
                  onClick={() => void runReview(pr)}
                >
                  {startingId === pr.id ? "Starting..." : "Run review"}
                </button>
              </li>
            ))}
          </ul>
        )}
      </main>
    </div>
  );
}
