export type Me = {
  id: string;
  login: string;
  name: string | null;
  avatar_url: string;
  github_connected: boolean;
};

export type Repository = {
  id: string;
  full_name: string;
  private: boolean;
  default_branch: string;
  html_url: string;
  last_synced_at: string | null;
};

export type PullRequest = {
  id: string;
  number: number;
  title: string;
  author_login: string;
  state: string;
  base_ref: string;
  head_ref: string;
  head_sha: string;
  html_url: string;
  github_updated_at: string;
};

export type ReviewStatus =
  | "queued"
  | "running"
  | "completed"
  | "failed"
  | "cancelled"
  | "superseded";

export type Severity = "critical" | "high" | "medium" | "low" | "info";

export type FeedbackVerdict = "useful" | "not_useful" | "dismissed";

export type Finding = {
  id: string;
  file_path: string;
  start_line: number | null;
  end_line: number | null;
  severity: Severity;
  category: string;
  confidence: "high" | "medium" | "low" | null;
  source: "deterministic" | "ai" | "hybrid";
  status: string;
  title: string;
  explanation: string | null;
  recommendation: string | null;
  feedback: FeedbackVerdict | null;
};

export type ReviewPullRequest = {
  id: string;
  number: number;
  title: string;
  html_url: string | null;
  repository_id: string;
  repository_full_name: string;
};

export type Review = {
  id: string;
  pull_request_id: string;
  pull_request: ReviewPullRequest;
  // null means no AI ran; "mock" means the offline stub did; "demo" means
  // the public demo replayed its recorded response. None of the three is a
  // clean AI pass, and the page has to say which it was.
  ai_model: string | null;
  // Why the AI stage did not run, when the pipeline decided that (today
  // only "diff_too_large"). Null covers both a real AI pass and no AI at
  // all, which ai_model tells apart.
  ai_skipped: string | null;
  status: ReviewStatus;
  head_sha: string;
  base_sha: string;
  summary: string | null;
  findings_count: number | null;
  severity_counts: Partial<Record<Severity, number>> | null;
  error_user_message: string | null;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
  cancel_requested: boolean;
  findings: Finding[];
};

export type AIKeyStatus = {
  configured: boolean;
  provider: "anthropic" | "gemini" | "openai" | null;
  model: string | null;
  key_hint: string | null;
  key_invalid: boolean;
};

export type ApiErrorPayload = {
  code: string;
  message: string;
} & Record<string, unknown>;

export type ApiErrorEnvelope = {
  error: ApiErrorPayload;
};
