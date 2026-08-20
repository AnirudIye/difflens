import Link from "next/link";
import Mark from "@/components/Mark";

export const metadata = { title: "Not found - DiffLens" };

export default function NotFound() {
  return (
    <main className="landing notfound">
      <Mark size={28} />
      <p className="notfound-code">404</p>
      <h1 className="lede">This page does not exist.</h1>
      <p className="status">
        The link may be wrong, or the thing it pointed at may have been a
        review that belongs to another account. Reviews are private to whoever
        started them.
      </p>
      <div className="review-actions">
        <Link className="button" href="/repositories">
          Your repositories
        </Link>
        <Link className="button button-quiet" href="/settings">
          Settings
        </Link>
      </div>
    </main>
  );
}
