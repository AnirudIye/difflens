"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import Header from "@/components/Header";
import { ApiError, apiFetch } from "@/lib/api";
import { relativeTime } from "@/lib/time";
import type { PullRequest, RepositoryDetail, Review } from "@/lib/types";
import { useMe } from "@/lib/useMe";
import { useSignOut } from "@/lib/useSignOut";

type LoadState =
  | { kind: "loading" }
  | { kind: "ready"; repo: RepositoryDetail; pulls: PullRequest[] }
  | { kind: "reconnect" }
  | { kind: "error"; message: string };

export default function RepositoryPullsPage() {
  const router = useRouter();
  const params = useParams<{ id: string }>();
  const me = useMe();
  const signOut = useSignOut();
  const [state, setState] = useState<LoadState>({ kind: "loading" });
  const [startingId, setStartingId] = useState<string | null>(null);
  const [repoStarting, setRepoStarting] = useState(false);
  const [runNote, setRunNote] = useState<string | null>(null);

  const load = useCallback(async () => {
    setState({ kind: "loading" });
    try {
      const [repo, data] = await Promise.all([
        apiFetch<RepositoryDetail>(`/repositories/${params.id}`),
        apiFetch<{ items: PullRequest[] }>(
          `/repositories/${params.id}/pull-requests`,
        ),
      ]);
      setState({ kind: "ready", repo, pulls: data.items });
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
    void load();
  }, [load]);

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
        } else if (err.code === "rate_limited") {
          // The server's own sentence carries the limit and the wait, and
          // "Try again" is the one thing that will not work here
          setRunNote(err.message);
        } else {
          setRunNote("Starting the review failed. Try again.");
        }
      } else {
        setRunNote("Starting the review failed. Try again.");
      }
    }
    setStartingId(null);
  }

  async function runRepoReview() {
    setRepoStarting(true);
    setRunNote(null);
    try {
      const review = await apiFetch<Review>("/reviews", {
        method: "POST",
        body: JSON.stringify({ repository_id: params.id }),
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
            "A review by another user already covers this repository at this commit.",
          );
        } else if (err.code === "repository_empty") {
          setRunNote(err.message);
        } else if (err.status === 404) {
          setRunNote("This repository is not available anymore.");
        } else if (
          err.code === "github_rate_limited" ||
          err.code === "github_unavailable"
        ) {
          setRunNote(
            "GitHub is not answering right now. Give it a minute and try again.",
          );
        } else if (err.code === "rate_limited") {
          setRunNote(err.message);
        } else {
          setRunNote("Starting the review failed. Try again.");
        }
      } else {
        setRunNote("Starting the review failed. Try again.");
      }
    }
    setRepoStarting(false);
  }

  return (
    <div className="shell">
      <Header me={me} onSignOut={signOut} />
      <main className="dash">
        <Link className="back-link" href="/repositories">
          &lsaquo; All repositories
        </Link>
        <div className="page-head">
          <h1 className="page-title">
            {state.kind === "ready" ? state.repo.full_name : "Repository"}
          </h1>
          {state.kind === "ready" ? (
            <button
              className="button row-end"
              type="button"
              disabled={repoStarting || startingId !== null}
              onClick={() => void runRepoReview()}
            >
              {repoStarting ? "Starting..." : "Review repository"}
            </button>
          ) : null}
        </div>
        {state.kind === "ready" ? (
          <p className="muted">
            Reviews a snapshot of{" "}
            <span className="mono">{state.repo.default_branch}</span> at its
            latest commit. Deterministic checks cover every reviewable file.
          </p>
        ) : null}
        {state.kind === "ready" && state.repo.latest_repo_review ? (
          <p className="time-muted">
            Latest repository review:{" "}
            <Link
              className="pr-title"
              href={`/reviews/${state.repo.latest_repo_review.id}`}
            >
              {state.repo.latest_repo_review.status} at{" "}
              <span className="mono">
                {state.repo.latest_repo_review.head_sha.slice(0, 7)}
              </span>
            </Link>
          </p>
        ) : null}

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
          <>
            <h2 className="file-head">Pull requests</h2>
            <p className="muted">No open pull requests.</p>
          </>
        ) : (
          <>
            <h2 className="file-head">Pull requests</h2>
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
                  disabled={startingId !== null || repoStarting}
                  onClick={() => void runReview(pr)}
                >
                  {startingId === pr.id ? "Starting..." : "Run review"}
                </button>
              </li>
            ))}
            </ul>
          </>
        )}
      </main>
    </div>
  );
}
