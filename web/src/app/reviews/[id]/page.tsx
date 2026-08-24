"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import ConfirmDialog from "@/components/ConfirmDialog";
import FindingCard from "@/components/FindingCard";
import Header from "@/components/Header";
import { ApiError, apiFetch } from "@/lib/api";
import {
  groupByFile,
  hasRealAI,
  severitySummary,
  STATUS_LABEL,
} from "@/lib/findings";
import { relativeTime } from "@/lib/time";
import type {
  FeedbackVerdict,
  Finding,
  Review,
  ReviewStatus,
} from "@/lib/types";
import { useMe } from "@/lib/useMe";
import { useSignOut } from "@/lib/useSignOut";

const POLL_MS = 2500;
// Render's free tier wakes in 30-60s; past ~2.5 minutes something is wrong
const MAX_WAKE_POLLS = 60;

type LoadState =
  | { kind: "loading" }
  | { kind: "waking" }
  | { kind: "ready"; review: Review }
  | { kind: "error"; message: string };

function isActive(status: ReviewStatus): boolean {
  return status === "queued" || status === "running";
}

export default function ReviewPage() {
  const router = useRouter();
  const params = useParams<{ id: string }>();
  const me = useMe();
  const signOut = useSignOut();
  const [state, setState] = useState<LoadState>({ kind: "loading" });
  const [stalled, setStalled] = useState(false);
  const [cancelBusy, setCancelBusy] = useState(false);
  const [cancelSent, setCancelSent] = useState(false);
  const [rerunBusy, setRerunBusy] = useState(false);
  const [confirming, setConfirming] = useState<"cancel" | "rerun" | null>(null);
  const [busyFindings, setBusyFindings] = useState<ReadonlySet<string>>(
    new Set(),
  );
  const [note, setNote] = useState<string | null>(null);
  const failedPolls = useRef(0);
  const [pollEpoch, setPollEpoch] = useState(0);

  // A stale server snapshot (late cancel response, reordered poll) must
  // never overwrite a terminal state already on screen: findings and the
  // feedback given on them would vanish with polling already stopped
  const applyServerReview = useCallback((review: Review) => {
    setState((prev) => {
      if (
        prev.kind === "ready" &&
        prev.review.id === review.id &&
        !isActive(prev.review.status)
      ) {
        return prev;
      }
      return { kind: "ready", review };
    });
  }, []);

  useEffect(() => {
    let stopped = false;
    let timer: number | undefined;
    failedPolls.current = 0;
    setStalled(false);
    setState({ kind: "loading" });

    async function poll() {
      if (stopped) {
        return;
      }
      let keepPolling = true;
      try {
        const review = await apiFetch<Review>(`/reviews/${params.id}`);
        if (stopped) {
          return;
        }
        failedPolls.current = 0;
        setStalled(false);
        applyServerReview(review);
        keepPolling = isActive(review.status);
      } catch (err) {
        if (stopped) {
          return;
        }
        if (err instanceof ApiError && err.status === 401) {
          router.push("/login");
          return;
        }
        if (err instanceof ApiError && err.status === 404) {
          setState({
            kind: "error",
            message: "This review does not exist or belongs to another account.",
          });
          return;
        }
        if (
          err instanceof ApiError &&
          err.status < 500 &&
          err.code !== "unknown_error"
        ) {
          // The envelope came from an awake API: retrying will not help
          setState({ kind: "error", message: err.message });
          return;
        }
        failedPolls.current += 1;
        if (failedPolls.current > MAX_WAKE_POLLS) {
          // Giving up must be visible: a banner over live data, or the
          // error state when nothing has loaded yet
          setStalled(true);
          setState((prev) =>
            prev.kind === "ready"
              ? prev
              : {
                  kind: "error",
                  message:
                    "The review server did not answer. It may be down; try again in a minute.",
                },
          );
          return;
        }
        setState((prev) => (prev.kind === "ready" ? prev : { kind: "waking" }));
      }
      if (keepPolling && !stopped) {
        timer = window.setTimeout(() => void poll(), POLL_MS);
      }
    }

    void poll();
    return () => {
      stopped = true;
      window.clearTimeout(timer);
    };
  }, [params.id, router, pollEpoch, applyServerReview]);

  const mutateFinding = useCallback(
    (findingId: string, verdict: FeedbackVerdict | null) => {
      setState((prev) => {
        if (prev.kind !== "ready") {
          return prev;
        }
        return {
          kind: "ready",
          review: {
            ...prev.review,
            findings: prev.review.findings.map((finding) =>
              finding.id === findingId
                ? { ...finding, feedback: verdict }
                : finding,
            ),
          },
        };
      });
    },
    [],
  );

  async function giveFeedback(finding: Finding, verdict: FeedbackVerdict) {
    if (busyFindings.has(finding.id)) {
      return;
    }
    setBusyFindings((prev) => new Set(prev).add(finding.id));
    const next = finding.feedback === verdict ? null : verdict;
    const previous = finding.feedback;
    mutateFinding(finding.id, next);
    setNote(null);
    try {
      if (next === null) {
        await apiFetch<{ verdict: null }>(`/findings/${finding.id}/feedback`, {
          method: "DELETE",
        });
      } else {
        await apiFetch<{ verdict: FeedbackVerdict }>(
          `/findings/${finding.id}/feedback`,
          { method: "PUT", body: JSON.stringify({ verdict: next }) },
        );
      }
    } catch (err) {
      mutateFinding(finding.id, previous);
      if (err instanceof ApiError && err.status === 401) {
        router.push("/login");
        return;
      }
      setNote("Saving your feedback failed. Try again.");
    } finally {
      setBusyFindings((prev) => {
        const remaining = new Set(prev);
        remaining.delete(finding.id);
        return remaining;
      });
    }
  }

  async function rerun(review: Review) {
    setRerunBusy(true);
    setNote(null);
    try {
      const fresh = await apiFetch<Review>(`/reviews/${review.id}/rerun`, {
        method: "POST",
      });
      router.push(`/reviews/${fresh.id}`);
      return;
    } catch (err) {
      if (err instanceof ApiError) {
        if (err.status === 401) {
          router.push("/login");
          return;
        }
        if (err.code === "review_already_exists") {
          const existingId = err.details.review_id;
          if (typeof existingId === "string") {
            router.push(`/reviews/${existingId}`);
            return;
          }
        }
        if (err.code === "pull_request_closed") {
          setNote("That pull request has closed on GitHub, so it cannot be reviewed again.");
        } else if (err.code === "repository_empty") {
          setNote(err.message);
        } else if (err.code === "review_still_running") {
          setNote("This review is still going. Wait for it to finish first.");
        } else if (err.code === "rate_limited") {
          // The server's own sentence names the limit and the wait; "try
          // again" is the one instruction that is wrong here
          setNote(err.message);
        } else {
          setNote("Starting a new review failed. Try again.");
        }
      } else {
        setNote("Starting a new review failed. Try again.");
      }
    }
    setRerunBusy(false);
    setConfirming(null);
  }

  async function cancel(review: Review) {
    setCancelBusy(true);
    setNote(null);
    try {
      const updated = await apiFetch<Review>(`/reviews/${review.id}/cancel`, {
        method: "POST",
      });
      // Sticky until a poll confirms: a racing stale poll must not
      // re-enable the button mid-cancel
      setCancelSent(true);
      applyServerReview(updated);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        router.push("/login");
        return;
      }
      if (!(err instanceof ApiError && err.code === "review_finished")) {
        setNote("Cancelling failed. Try again.");
      }
      // review_finished: the next poll shows the final state
    } finally {
      setCancelBusy(false);
      setConfirming(null);
    }
  }

  return (
    <div className="shell">
      <Header me={me} onSignOut={signOut} />
      <main className="dash">
        {state.kind === "loading" ? (
          <p className="muted">Loading the review...</p>
        ) : state.kind === "waking" ? (
          <div className="status-line">
            <span className="status-dot status-live" aria-hidden="true" />
            <div>
              <p>Waking the review server</p>
              <p className="muted">
                The free tier sleeps when idle. This can take up to a minute;
                the page keeps trying on its own.
              </p>
            </div>
          </div>
        ) : state.kind === "error" ? (
          <>
            <p className="muted">{state.message}</p>
            <div className="review-actions">
              <button
                className="button button-quiet"
                type="button"
                onClick={() => {
                  setState({ kind: "loading" });
                  setPollEpoch((epoch) => epoch + 1);
                }}
              >
                Try again
              </button>
              <Link className="button button-quiet" href="/repositories">
                All repositories
              </Link>
            </div>
          </>
        ) : (
          <ReviewBody
            review={state.review}
            note={note}
            stalled={stalled}
            cancelBusy={cancelBusy}
            cancelSent={cancelSent}
            rerunBusy={rerunBusy}
            busyFindings={busyFindings}
            onRetry={() => setPollEpoch((epoch) => epoch + 1)}
            onCancel={() => setConfirming("cancel")}
            onRerun={() => setConfirming("rerun")}
            onFeedback={(finding, verdict) => void giveFeedback(finding, verdict)}
          />
        )}
      </main>

      <ConfirmDialog
        open={confirming === "cancel"}
        title="Stop this review?"
        body={
          "The worker stops as soon as it reaches its next checkpoint, and " +
          "no findings are kept. You can run the review again afterwards."
        }
        confirmLabel="Stop review"
        cancelLabel="Keep going"
        destructive
        busy={cancelBusy}
        onConfirm={() =>
          state.kind === "ready" ? void cancel(state.review) : undefined
        }
        onCancel={() => setConfirming(null)}
      />
      <ConfirmDialog
        open={confirming === "rerun"}
        title={
          state.kind === "ready" && state.review.repository
            ? "Review this repository again?"
            : "Review this commit again?"
        }
        body={
          state.kind === "ready" && state.review.repository
            ? "DiffLens reviews the current latest commit of " +
              `${state.review.repository.default_branch ?? "the default branch"}. ` +
              "If new commits have landed since, the new review covers them. " +
              "This review stays readable but stops being the current one."
            : "This review is replaced by a new one. Its findings stay readable, " +
              "but it stops being the current review for this pull request."
        }
        confirmLabel="Run again"
        busy={rerunBusy}
        onConfirm={() =>
          state.kind === "ready" ? void rerun(state.review) : undefined
        }
        onCancel={() => setConfirming(null)}
      />
    </div>
  );
}

function ReviewBody({
  review,
  note,
  stalled,
  cancelBusy,
  cancelSent,
  rerunBusy,
  busyFindings,
  onRetry,
  onCancel,
  onRerun,
  onFeedback,
}: {
  review: Review;
  note: string | null;
  stalled: boolean;
  cancelBusy: boolean;
  cancelSent: boolean;
  rerunBusy: boolean;
  busyFindings: ReadonlySet<string>;
  onRetry: () => void;
  onCancel: () => void;
  onRerun: () => void;
  onFeedback: (finding: Finding, verdict: FeedbackVerdict) => void;
}) {
  const pull = review.pull_request;
  const repo = review.repository;
  const active = isActive(review.status);
  const cancelling = review.cancel_requested || cancelSent;
  const groups = groupByFile(review.findings);
  const counts = severitySummary(review);
  const repoPageHref = pull
    ? `/repositories/${pull.repository_id}`
    : `/repositories/${review.repository_id}`;

  return (
    <>
      <Link className="back-link" href={repoPageHref}>
        &lsaquo; {pull ? pull.repository_full_name : repo?.full_name}
      </Link>
      <div className="page-head">
        <h1 className="page-title">
          Review of{" "}
          {pull ? (
            pull.html_url ? (
              <a
                className="pr-title"
                href={pull.html_url}
                target="_blank"
                rel="noopener noreferrer"
              >
                <span className="mono">#{pull.number}</span> {pull.title}
              </a>
            ) : (
              <>
                <span className="mono">#{pull.number}</span> {pull.title}
              </>
            )
          ) : repo?.html_url ? (
            <a
              className="pr-title"
              href={`${repo.html_url}/tree/${review.head_sha}`}
              target="_blank"
              rel="noopener noreferrer"
            >
              {repo.full_name}
            </a>
          ) : (
            repo?.full_name
          )}
        </h1>
        {active ? (
          <button
            className="button button-quiet"
            type="button"
            disabled={cancelBusy || cancelling}
            onClick={onCancel}
          >
            {cancelling ? "Cancelling..." : "Cancel review"}
          </button>
        ) : review.status === "superseded" ? null : (
          <button
            className="button button-quiet"
            type="button"
            disabled={rerunBusy}
            onClick={onRerun}
            title={
              pull
                ? "Review this commit again, for instance after changing your AI key"
                : "Review the default branch's current head commit again"
            }
          >
            {rerunBusy ? "Starting..." : "Run again"}
          </button>
        )}
      </div>
      <p className="review-meta">
        <span className="mono">{review.head_sha.slice(0, 7)}</span>
        {pull && review.base_sha ? (
          <>
            {" against "}
            <span className="mono">{review.base_sha.slice(0, 7)}</span>
          </>
        ) : repo?.default_branch ? (
          <>
            {" on "}
            <span className="mono">{repo.default_branch}</span>
          </>
        ) : null}
        {" - started "}
        {relativeTime(review.created_at)}
        {review.completed_at
          ? ` - finished ${relativeTime(review.completed_at)}`
          : ""}
        {hasRealAI(review) ? (
          <>
            {" - reviewed by "}
            <span className="mono">{review.ai_model}</span>
          </>
        ) : null}
      </p>

      <div className="status-line" role="status">
        <span
          className={`status-dot${active && !stalled ? " status-live" : ""}${
            review.status === "failed" ? " status-bad" : ""
          }${review.status === "completed" ? " status-done" : ""}`}
          aria-hidden="true"
        />
        <span>{STATUS_LABEL[review.status]}</span>
        {review.status === "queued" && !stalled ? (
          <span className="muted">
            waiting for a worker; a sleeping free tier can add a minute
          </span>
        ) : null}
        {review.status === "running" && !stalled ? (
          <span className="muted">
            {pull
              ? "checking the diff line by line"
              : "checking the repository file by file"}
          </span>
        ) : null}
      </div>

      {stalled ? (
        <div className="notice" role="alert">
          <p>
            Lost contact with the review server. The review may still be
            running; this page has stopped watching it.
          </p>
          <button className="button button-quiet" type="button" onClick={onRetry}>
            Reconnect
          </button>
        </div>
      ) : null}

      {note ? (
        <p className="muted run-note" role="alert">
          {note}
        </p>
      ) : null}

      {review.status === "failed" ? (
        <div className="notice">
          <p>
            {review.error_user_message ??
              "This review failed before it could finish."}
          </p>
          <Link className="button" href={repoPageHref}>
            {pull ? "Back to pull requests" : "Back to repository"}
          </Link>
        </div>
      ) : null}

      {review.status === "cancelled" ? (
        <p className="muted">
          This review was cancelled before it finished. Run it again from the{" "}
          {pull ? "pull request list" : "repository page"} whenever you like.
        </p>
      ) : null}

      {review.status === "superseded" ? (
        <p className="muted">
          A newer review of this {pull ? "pull request" : "repository"} has
          replaced this one. Its findings are kept below as they were.
        </p>
      ) : null}

      {review.status === "completed" || review.status === "superseded" ? (
        <>
          {review.summary ? (
            <p className="review-summary">{review.summary}</p>
          ) : null}
          {review.ai_failed === "user_key" ? (
            // Their key, their fix. Said first, because it explains every
            // other AI symptom on the page and sends them somewhere useful.
            <div className="notice ai-notice" role="status">
              <p>
                Your AI key was rejected, so these findings come from the
                deterministic analyzers alone. Check the key in Settings and
                run the review again.
              </p>
              <Link className="button button-quiet" href="/settings">
                Check your API key
              </Link>
            </div>
          ) : review.ai_failed === "server" ? (
            // Not their key and not their problem, so no Settings button
            <div className="notice ai-notice" role="status">
              <p>
                The AI reviewer is misconfigured, so these findings come from
                the deterministic analyzers alone. This one is for the
                operator to fix.
              </p>
            </div>
          ) : review.ai_skipped === "diff_too_large" ? (
            // A key would not have helped: the pipeline refused the diff, not
            // the provider. Offering Settings here is wrong advice.
            <div className="notice ai-notice" role="status">
              <p>
                This diff was too large to send to the AI reviewer, so these
                findings come from the deterministic analyzers alone. A
                smaller pull request gets the full review.
              </p>
            </div>
          ) : review.ai_chunks_failed > 0 ? (
            // Checked before the cap: an outage that stopped every pass would
            // otherwise be dressed up as the free tier's coverage limit, and
            // the page would sell an API key as the cure for a provider being
            // down.
            <div className="notice ai-notice" role="status">
              <p>
                The AI reviewer could not finish {review.ai_chunks_failed}{" "}
                {review.ai_chunks_failed === 1 ? "pass" : "passes"} over this
                repository, so its findings are incomplete. The deterministic
                analyzers checked every reviewable file. Running the review
                again usually clears it.
              </p>
            </div>
          ) : review.ai_capped === "keyless" && review.ai_coverage ? (
            <div className="notice ai-notice" role="status">
              <p>
                The AI reviewer read {review.ai_coverage.files_covered} of{" "}
                {review.ai_coverage.files_total} reviewable files. Without your
                own AI key, DiffLens runs on a shared free AI tier and caps how
                much of a repository the AI reads. The deterministic analyzers
                checked every reviewable file. Add your own AI key in Settings
                to get full AI coverage.
              </p>
              <Link className="button button-quiet" href="/settings">
                Add your API key
              </Link>
            </div>
          ) : !hasRealAI(review) ? (
            <div className="notice ai-notice" role="status">
              <p>
                No AI reviewer ran, so these findings come from the
                deterministic analyzers alone. Logic bugs with no lint
                signature would not appear here.
              </p>
              <Link className="button button-quiet" href="/settings">
                Add your API key
              </Link>
            </div>
          ) : review.ai_coverage &&
            review.ai_coverage.files_covered <
              review.ai_coverage.files_total ? (
            // A key would not help here either: the repository is bigger than
            // one review's batch ceiling, whoever pays for the calls
            <div className="notice ai-notice" role="status">
              <p>
                The AI reviewer read {review.ai_coverage.files_covered} of{" "}
                {review.ai_coverage.files_total} reviewable files; this
                repository is larger than one review can cover. The
                deterministic analyzers checked every reviewable file.
              </p>
            </div>
          ) : null}
          {review.findings_truncated ? (
            <div className="notice ai-notice" role="status">
              <p>
                This review found more than 100 findings. Only the 100 most
                severe are shown; fix some and run the review again to see the
                rest.
              </p>
            </div>
          ) : null}
          {review.analyzers_skipped && review.analyzers_skipped.length > 0 ? (
            <div className="notice ai-notice" role="status">
              <p>
                Some analyzers did not finish:{" "}
                <span className="mono">
                  {review.analyzers_skipped.join(", ")}
                </span>
                . Their findings are missing from this review.
              </p>
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
          {review.findings.length === 0 ? (
            <div className="clean-state">
              <svg
                width="18"
                height="18"
                viewBox="0 0 18 18"
                fill="none"
                aria-hidden="true"
              >
                <path
                  d="M3 9.5 7 13.5 15 4.5"
                  stroke="currentColor"
                  strokeWidth="2"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
              </svg>
              <p>
                {review.analyzers_skipped && review.analyzers_skipped.length > 0
                  ? "Nothing to show. Some checks did not finish, so this is not a clean result."
                  : "Nothing to report."}
              </p>
            </div>
          ) : (
            groups.map(([filePath, findings]) => (
              <section className="file-group" key={filePath}>
                <h2 className="file-head">
                  <span className="mono">{filePath}</span>
                  <span className="muted">
                    {findings.length}{" "}
                    {findings.length === 1 ? "finding" : "findings"}
                  </span>
                </h2>
                <ul className="finding-list">
                  {findings.map((finding) => (
                    <FindingCard
                      key={finding.id}
                      finding={finding}
                      busy={busyFindings.has(finding.id)}
                      onFeedback={onFeedback}
                    />
                  ))}
                </ul>
              </section>
            ))
          )}
        </>
      ) : null}
    </>
  );
}
