"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import FindingCard from "@/components/FindingCard";
import Header from "@/components/Header";
import { ApiError, apiFetch } from "@/lib/api";
import { groupByFile, isReplayedAI, severitySummary } from "@/lib/findings";
import { relativeTime } from "@/lib/time";
import type { Review, ReviewStatus } from "@/lib/types";

const POLL_MS = 2500;
// Render's free tier wakes in 30-60s; past ~2.5 minutes something is wrong
const MAX_WAKE_POLLS = 60;

const STATUS_LABEL: Record<ReviewStatus, string> = {
  queued: "Queued",
  running: "Analyzing",
  completed: "Done",
  failed: "Failed",
  cancelled: "Cancelled",
  superseded: "Replaced by a newer run",
};

type LoadState =
  | { kind: "loading" }
  | { kind: "waking" }
  | { kind: "ready"; review: Review }
  | { kind: "unavailable" }
  | { kind: "error"; message: string };

function isActive(status: ReviewStatus): boolean {
  return status === "queued" || status === "running";
}

export default function DemoPage() {
  const [state, setState] = useState<LoadState>({ kind: "loading" });
  const [stalled, setStalled] = useState(false);
  const [rerunBusy, setRerunBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const failedPolls = useRef(0);
  const [pollEpoch, setPollEpoch] = useState(0);

  // There is no review id in the URL: /demo always shows whatever the
  // current demo review is, so a rerun cannot strand a visitor on the run it
  // replaced, and the link stays shareable.
  const load = useCallback(async (): Promise<Review | null> => {
    const review = await apiFetch<Review>("/demo/review");
    failedPolls.current = 0;
    setStalled(false);
    setState({ kind: "ready", review });
    return review;
  }, []);

  useEffect(() => {
    let stopped = false;
    let timer: number | undefined;

    async function poll() {
      if (stopped) {
        return;
      }
      try {
        const review = await load();
        if (review && isActive(review.status) && !stopped) {
          timer = window.setTimeout(poll, POLL_MS);
        }
      } catch (err) {
        if (stopped) {
          return;
        }
        // A 404 here means the demo is switched off or not seeded. That is
        // a settled answer, not a cold start, so it must not be retried for
        // fifty seconds behind a "waking up" message.
        if (err instanceof ApiError && err.status === 404) {
          setState({ kind: "unavailable" });
          return;
        }
        failedPolls.current += 1;
        if (failedPolls.current >= MAX_WAKE_POLLS) {
          setStalled(true);
          return;
        }
        setState((prev) => (prev.kind === "ready" ? prev : { kind: "waking" }));
        timer = window.setTimeout(poll, POLL_MS);
      }
    }

    void poll();
    return () => {
      stopped = true;
      window.clearTimeout(timer);
    };
  }, [load, pollEpoch]);

  async function rerun() {
    setRerunBusy(true);
    setNote(null);
    try {
      const review = await apiFetch<Review>("/demo/review/rerun", {
        method: "POST",
      });
      setState({ kind: "ready", review });
      failedPolls.current = 0;
      setPollEpoch((epoch) => epoch + 1); // restart polling for the new run
    } catch (err) {
      if (err instanceof ApiError) {
        if (err.code === "demo_review_running") {
          // Someone else got there first. Saying "a review is already
          // running" and then watching that very run finish leaves the
          // sentence sitting next to a completed review contradicting it,
          // so the restarted poll is left to explain itself: the status
          // line already reads Analyzing, then Done.
          setPollEpoch((epoch) => epoch + 1);
        } else {
          // The server's own sentence: a rate limit says how long to wait,
          // and "try again" is the one thing that will not work.
          setNote(err.message);
        }
      } else {
        setNote("Could not start the review. The server may be waking up.");
      }
    } finally {
      setRerunBusy(false);
    }
  }

  return (
    <div className="shell">
      <Header me={{ kind: "anon" }} />
      <main className="dash">
        <DemoIntro />
        {state.kind === "loading" ? (
          <p className="muted">Loading the demo review...</p>
        ) : null}
        {state.kind === "waking" && !stalled ? (
          // Suppressed once stalled: "waking up, hold on" underneath "we
          // stopped watching" tells the visitor two different things
          <p className="muted">
            Waking the review server. The free tier sleeps when idle, so the
            first request after a quiet spell takes up to a minute.
          </p>
        ) : null}
        {state.kind === "unavailable" ? (
          <div className="notice">
            <p>
              The demo is not available on this deployment. Everything else
              still works if you sign in with GitHub.
            </p>
            <Link className="button" href="/login">
              Sign in with GitHub
            </Link>
          </div>
        ) : null}
        {stalled ? (
          <div className="notice" role="alert">
            <p>
              Lost contact with the review server. This page has stopped
              watching it.
            </p>
            <button
              className="button button-quiet"
              type="button"
              onClick={() => {
                failedPolls.current = 0;
                setStalled(false);
                setPollEpoch((epoch) => epoch + 1);
              }}
            >
              Reconnect
            </button>
          </div>
        ) : null}
        {state.kind === "ready" ? (
          <DemoReview
            review={state.review}
            note={note}
            rerunBusy={rerunBusy}
            onRerun={() => void rerun()}
          />
        ) : null}
      </main>
    </div>
  );
}

function DemoIntro() {
  return (
    <div className="page-head demo-head">
      <div>
        <h1 className="page-title">A real review, no account needed</h1>
        <p className="muted demo-lede">
          This is one pull request with deliberate bugs in it, reviewed by the
          same pipeline that reviews yours: ruff, detect-secrets, ESLint, a
          missing-tests check, and an AI pass whose every cited line is
          re-validated against the diff before you see it. Run it again below
          and watch the job move through the queue.
        </p>
      </div>
    </div>
  );
}

function DemoReview({
  review,
  note,
  rerunBusy,
  onRerun,
}: {
  review: Review;
  note: string | null;
  rerunBusy: boolean;
  onRerun: () => void;
}) {
  const pull = review.pull_request;
  const active = isActive(review.status);
  const groups = groupByFile(review.findings);
  const counts = severitySummary(review);

  return (
    <>
      <div className="page-head demo-review-head">
        <h2 className="page-title demo-pr-title">
          <span className="mono">#{pull.number}</span> {pull.title}
        </h2>
        <button
          className="button"
          type="button"
          disabled={rerunBusy || active}
          onClick={onRerun}
        >
          {rerunBusy
            ? "Starting..."
            : active
              ? "Running..."
              : "Run this review again"}
        </button>
      </div>
      <p className="review-meta">
        <span className="mono">{pull.repository_full_name}</span>
        {" - started "}
        {relativeTime(review.created_at)}
        {review.completed_at
          ? ` - finished ${relativeTime(review.completed_at)}`
          : ""}
      </p>

      <div className="status-line" role="status">
        <span
          className={`status-dot${active ? " status-live" : ""}${
            review.status === "failed" ? " status-bad" : ""
          }${review.status === "completed" ? " status-done" : ""}`}
          aria-hidden="true"
        />
        <span>{STATUS_LABEL[review.status]}</span>
        {review.status === "queued" ? (
          <span className="muted">waiting for a worker to pick it up</span>
        ) : null}
        {review.status === "running" ? (
          <span className="muted">checking the diff line by line</span>
        ) : null}
      </div>

      {note ? (
        <p className="muted run-note" role="alert">
          {note}
        </p>
      ) : null}

      {review.status === "failed" ? (
        <div className="notice">
          <p>
            {review.error_user_message ??
              "This run failed before it could finish."}
          </p>
        </div>
      ) : null}

      {review.status === "completed" || review.status === "superseded" ? (
        <>
          {review.summary ? (
            <p className="review-summary">{review.summary}</p>
          ) : null}
          {isReplayedAI(review) ? (
            <div className="notice ai-notice" role="status">
              <p>
                The AI half of this demo replays a recorded reviewer response,
                so it is free to run and identical every time. Those findings
                are real bugs, and they go through the same validation and
                merging as a live model&apos;s output. Sign in with your own
                API key to point a live model at your own pull requests.
              </p>
              <Link className="button button-quiet" href="/login">
                Sign in with GitHub
              </Link>
            </div>
          ) : null}
          {counts.length > 0 ? (
            <div className="severity-row">
              {counts.map(([severity, count]) => (
                <span key={severity} className={`chip sev-${severity}`}>
                  {count} {severity}
                </span>
              ))}
            </div>
          ) : null}
          {groups.map(([filePath, findings]) => (
            <section className="file-group" key={filePath}>
              <h3 className="file-head">
                <span className="mono">{filePath}</span>
                <span className="muted">
                  {findings.length}{" "}
                  {findings.length === 1 ? "finding" : "findings"}
                </span>
              </h3>
              <ul className="finding-list">
                {findings.map((finding) => (
                  // No feedback controls: feedback is per account, and this
                  // page has no account behind it.
                  <FindingCard key={finding.id} finding={finding} />
                ))}
              </ul>
            </section>
          ))}
        </>
      ) : null}
    </>
  );
}
