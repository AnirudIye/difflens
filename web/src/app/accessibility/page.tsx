import type { Metadata } from "next";
import Link from "next/link";
import ThemeToggle from "@/components/ThemeToggle";

export const metadata: Metadata = {
  title: "Accessibility - DiffLens",
  description:
    "The accessibility target DiffLens aims for, the measures taken, and the honest gaps.",
};

export default function AccessibilityPage() {
  return (
    <>
      <div className="corner-actions">
        <ThemeToggle />
      </div>
      <main className="legal">
        <Link className="back-link" href="/">
          Back to DiffLens
        </Link>
        <h1 className="legal-title">Accessibility</h1>
        <p className="legal-updated">Last updated: 2026-08-24</p>

        <h2>Our commitment</h2>
        <p>
          DiffLens should be usable by everyone, including people who use
          screen readers, keyboards without a mouse, or high-contrast and
          reduced-motion settings. The conformance target is WCAG 2.2 level
          AA.
        </p>

        <h2>Measures taken</h2>
        <p>
          These are the measures actually built into the product today, not
          aspirations:
        </p>
        <ul>
          <li>
            Color contrast is measured to the AA thresholds in both the light
            and the dark theme.
          </li>
          <li>
            Everything is operable by keyboard. The account menu closes on
            Escape and returns focus to the control that opened it, and its
            state is exposed with aria-expanded.
          </li>
          <li>
            Confirmation dialogs use the native dialog element, so focus is
            trapped inside them and restored when they close.
          </li>
          <li>
            Your chosen theme is applied before the first paint, so there is
            no flash of the wrong theme.
          </li>
          <li>
            The prefers-reduced-motion setting is respected; the one looping
            animation in the interface stops for users who ask for reduced
            motion.
          </li>
          <li>
            Pages use semantic landmarks and labels: headings, lists, form
            labels tied to their inputs, and named navigation.
          </li>
        </ul>

        <h2>Known limitations</h2>
        <p>Stated honestly, because a policy that hides them helps no one:</p>
        <ul>
          <li>
            No formal third-party accessibility audit has been done yet. The
            measures above are self-tested.
          </li>
          <li>
            Review findings include code snippets, and code read aloud by a
            screen reader can be hard to follow. There is no plain-language
            alternative for snippets yet.
          </li>
        </ul>

        <h2>Feedback</h2>
        <p>
          If any part of DiffLens is hard for you to use, say so through the{" "}
          <Link href="/contact">contact form</Link>. You will get a response
          within 30 days, and reports of barriers are treated as bugs, not
          suggestions.
        </p>
      </main>
    </>
  );
}
