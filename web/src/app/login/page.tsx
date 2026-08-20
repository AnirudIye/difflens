import type { Metadata } from "next";
import Link from "next/link";
import Mark from "@/components/Mark";
import ThemeToggle from "@/components/ThemeToggle";

export const metadata: Metadata = {
  title: "Sign in - DiffLens",
};

/* Reasons the API sends people back here. Cancelling is not an error, so it
   does not read like one; the other two say what to do next, because there
   is nothing else the person can do about either. */
const REASONS: Record<string, string> = {
  cancelled:
    "You cancelled on GitHub, so nothing was shared and no account was created.",
  expired:
    "That sign in attempt expired before it finished. Starting again should work.",
  github:
    "GitHub did not finish the sign in. That is usually temporary, so try again.",
};

export default async function LoginPage({
  searchParams,
}: {
  searchParams: Promise<{ error?: string }>;
}) {
  const { error } = await searchParams;
  const reason = error ? REASONS[error] : undefined;

  return (
    <>
      <div className="corner-actions">
        <ThemeToggle />
      </div>
      <main className="login-card">
        <Mark size={36} className="mark" />
        <h1 className="wordmark">DiffLens</h1>
        {reason ? (
          <p className="login-note" role="status">
            {reason}
          </p>
        ) : null}
        <a className="button" href="/api/backend/auth/github/login">
          Sign in with GitHub
        </a>
        <p className="trust">
          Read-only access to public repositories. DiffLens cannot write to your
          code.
        </p>
        <p className="login-note">
          Not ready to connect an account?{" "}
          <Link href="/demo">Look at a finished review first</Link>.
        </p>
      </main>
    </>
  );
}
