import Link from "next/link";

/* The quiet last row of every page, rendered once from the root layout. The
   body grid gives it its own auto-sized row under a 1fr content row, so the
   pages that center themselves (landing, sign in, 404) still center in the
   space above it rather than pushing it around. */
export default function Footer() {
  return (
    <footer className="site-footer">
      <nav aria-label="Policies and contact">
        <ul className="footer-links">
          <li>
            <Link href="/privacy">Privacy</Link>
          </li>
          <li>
            <Link href="/terms">Terms</Link>
          </li>
          <li>
            <Link href="/accessibility">Accessibility</Link>
          </li>
          <li>
            <Link href="/contact">Contact</Link>
          </li>
          <li>
            <a
              href="https://github.com/AnirudIye/difflens"
              target="_blank"
              rel="noreferrer"
            >
              GitHub
            </a>
          </li>
        </ul>
      </nav>
    </footer>
  );
}
