export type Me = {
  id: number;
  login: string;
  name: string | null;
  avatar_url: string;
  github_connected: boolean;
};

export type ApiErrorPayload = {
  code: string;
  message: string;
};

export type ApiErrorEnvelope = {
  error: ApiErrorPayload;
};
