"use client";

import { lineRef, SOURCE_LABEL } from "@/lib/findings";
import type { FeedbackVerdict, Finding } from "@/lib/types";

// onFeedback is optional because the public demo has no signed-in user to
// attribute a verdict to. Omitting it renders the card without the feedback
// row rather than with buttons that would 401, which is the honest version
// of "you cannot do this here".
export default function FindingCard({
  finding,
  busy = false,
  onFeedback,
}: {
  finding: Finding;
  busy?: boolean;
  onFeedback?: (finding: Finding, verdict: FeedbackVerdict) => void;
}) {
  const lines = lineRef(finding);
  return (
    <li className="finding">
      <div className="finding-head">
        <span className={`chip sev-${finding.severity}`}>
          {finding.severity}
        </span>
        <span className="chip">{finding.category}</span>
        {finding.confidence ? (
          <span className="chip">{finding.confidence} confidence</span>
        ) : null}
        <span className="chip">{SOURCE_LABEL[finding.source]}</span>
        {lines ? <span className="mono finding-lines">{lines}</span> : null}
      </div>
      <p className="finding-title">{finding.title}</p>
      {finding.explanation && finding.explanation !== finding.title ? (
        <p className="finding-body">{finding.explanation}</p>
      ) : null}
      {finding.recommendation ? (
        <p className="finding-body finding-rec">
          <span className="rec-label">Fix</span>
          {finding.recommendation}
        </p>
      ) : null}
      {onFeedback ? (
        <div className="feedback-row">
          <button
            className="button button-quiet"
            type="button"
            disabled={busy}
            aria-pressed={finding.feedback === "useful"}
            onClick={() => onFeedback(finding, "useful")}
          >
            Useful
          </button>
          <button
            className="button button-quiet"
            type="button"
            disabled={busy}
            aria-pressed={finding.feedback === "not_useful"}
            onClick={() => onFeedback(finding, "not_useful")}
          >
            Not useful
          </button>
        </div>
      ) : null}
    </li>
  );
}
