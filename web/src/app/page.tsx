import Link from "next/link";
import Mark from "@/components/Mark";
import ThemeToggle from "@/components/ThemeToggle";

export default function Home() {
  return (
    <>
      <div className="corner-actions">
        <ThemeToggle />
      </div>
      <main className="landing">
        <Mark size={44} className="mark" />
        <h1 className="wordmark">DiffLens</h1>
        <p className="lede">
          DiffLens reviews GitHub pull requests with deterministic static
          analysis and AI reasoning, and reports findings tied to exact files
          and lines.
        </p>
        <p className="status">
          <Link href="/demo">See a review without signing in</Link>, or{" "}
          <Link href="/login">sign in with GitHub</Link> to review your own
          pull requests.
        </p>
      </main>
    </>
  );
}
