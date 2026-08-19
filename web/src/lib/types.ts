export type Me = {
  id: number;
  login: string;
  name: string | null;
  avatar_url: string;
  github_connected: boolean;
};

export type Repository = {
  id: number;
  full_name: string;
  private: boolean;
  default_branch: string;
  html_url: string;
  last_synced_at: string | null;
};

export type PullRequest = {
  id: number;
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

export type AIKeyStatus = {
  configured: boolean;
  provider: "anthropic" | "gemini" | null;
  model: string | null;
  key_hint: string | null;
  key_invalid: boolean;
};

export type ApiErrorPayload = {
  code: string;
  message: string;
};

export type ApiErrorEnvelope = {
  error: ApiErrorPayload;
};
