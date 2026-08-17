import Link from "next/link";
import Mark from "@/components/Mark";

export default function Home() {
  return (
    <main className="landing">
      <Mark size={44} className="mark" />
      <h1 className="wordmark">DiffLens</h1>
      <p className="lede">
        DiffLens reviews GitHub pull requests with deterministic static
        analysis and AI reasoning, and reports findings tied to exact files
        and lines.
      </p>
      <p className="status">
        The platform is under active construction.{" "}
        <Link href="/login">Sign in with GitHub to get started</Link>.
      </p>
    </main>
  );
}
