import type { Metadata } from "next";
import Mark from "@/components/Mark";
import ThemeToggle from "@/components/ThemeToggle";

export const metadata: Metadata = {
  title: "Sign in - DiffLens",
};

export default function LoginPage() {
  return (
    <>
      <div className="corner-actions">
        <ThemeToggle />
      </div>
      <main className="login-card">
        <Mark size={36} className="mark" />
        <h1 className="wordmark">DiffLens</h1>
        <a className="button" href="/api/backend/auth/github/login">
          Sign in with GitHub
        </a>
        <p className="trust">
          Read-only access to public repositories. DiffLens cannot write to your
          code.
        </p>
      </main>
    </>
  );
}
