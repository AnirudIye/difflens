// Presentation shared by the signed-in review page and the public demo.
// Both render the same review object, so they must group, order, and label
// findings identically; two copies of these rules would drift and the demo
// would quietly stop being a picture of the real thing.

import type { Finding, Review, Severity } from "./types";

export const SOURCE_LABEL: Record<Finding["source"], string> = {
  deterministic: "analyzer",
  ai: "ai",
  hybrid: "analyzer + ai",
};

export const SEVERITY_ORDER: Severity[] = [
  "critical",
  "high",
  "medium",
  "low",
  "info",
];

export function lineRef(finding: Finding): string | null {
  if (finding.start_line === null) {
    return null;
  }
  if (finding.end_line === null || finding.end_line === finding.start_line) {
    return `L${finding.start_line}`;
  }
  return `L${finding.start_line}-L${finding.end_line}`;
}

export function groupByFile(findings: Finding[]): Array<[string, Finding[]]> {
  const groups = new Map<string, Finding[]>();
  for (const finding of findings) {
    const list = groups.get(finding.file_path);
    if (list) {
      list.push(finding);
    } else {
      groups.set(finding.file_path, [finding]);
    }
  }
  return [...groups.entries()];
}

export function severitySummary(review: Review): Array<[Severity, number]> {
  const counts = review.severity_counts ?? {};
  return SEVERITY_ORDER.flatMap((severity) => {
    const count = counts[severity];
    return count ? [[severity, count] as [Severity, number]] : [];
  });
}

// The offline stub answers with no findings and no error, so a review it
// produced is indistinguishable from a clean AI pass unless we say so. The
// demo's recorded reviewer is not a live model either, and says so in its
// own words rather than borrowing the stub's.
export function hasRealAI(review: Review): boolean {
  return (
    review.ai_model !== null &&
    review.ai_model !== "mock" &&
    review.ai_model !== "demo"
  );
}

export function isReplayedAI(review: Review): boolean {
  return review.ai_model === "demo";
}
