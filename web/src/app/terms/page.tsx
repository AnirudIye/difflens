import type { Metadata } from "next";
import Link from "next/link";
import ThemeToggle from "@/components/ThemeToggle";

export const metadata: Metadata = {
  title: "Terms - DiffLens",
  description:
    "The terms of service for DiffLens: a free, best-effort code review service.",
};

export default function TermsPage() {
  return (
    <>
      <div className="corner-actions">
        <ThemeToggle />
      </div>
      <main className="legal">
        <Link className="back-link" href="/">
          Back to DiffLens
        </Link>
        <h1 className="legal-title">Terms of service</h1>
        <p className="legal-updated">Last updated: 2026-08-24</p>

        <h2>1. Agreement</h2>
        <p>
          These terms are an agreement between you and the operator of
          DiffLens. By using DiffLens you accept them. If you do not accept
          them, do not use the service.
        </p>

        <h2>2. The service</h2>
        <p>
          DiffLens reviews code you choose to review from GitHub, using
          deterministic analyzers and an AI reviewer. It is free and provided
          on a best-effort basis with no service level agreement. It runs on
          free-tier infrastructure that sleeps when idle, so requests can be
          slow to wake, and rate limits apply. Features can change or be
          withdrawn.
        </p>

        <h2>3. Eligibility</h2>
        <p>You must be at least 13 years old to use DiffLens.</p>

        <h2>4. Accounts</h2>
        <p>
          You sign in with your GitHub account through OAuth. DiffLens never
          sees your GitHub password. You are responsible for your GitHub
          account and for what is done with DiffLens while signed in as you.
        </p>

        <h2>5. Acceptable use</h2>
        <ul>
          <li>
            Do not try to break the isolation of the review pipeline or to
            make reviewed code execute outside it.
          </li>
          <li>Do not place abusive load on the service. Rate limits are
            enforced.</li>
          <li>
            Do not ask DiffLens to review code you do not have the right to
            review.
          </li>
          <li>Do not use the service to break any law.</li>
        </ul>

        <h2>6. Your content</h2>
        <p>
          Your repositories remain yours. By starting a review you grant
          DiffLens a limited license to fetch and process the code you point
          it at, solely to produce your review. That license ends when the
          review finishes; the reviewed code itself is discarded, and only
          the findings are kept for you.
        </p>

        <h2>7. Output and the AI disclaimer</h2>
        <p>
          Review findings are produced automatically, partly by a
          probabilistic AI model. They can be wrong, incomplete, or
          misleading. AI-cited locations are validated against the reviewed
          code, but correctness of the findings themselves is not guaranteed.
          Findings are not professional advice, not security certification,
          and not a substitute for your own judgment. Verify before acting on
          anything DiffLens says.
        </p>

        <h2>8. Third-party services</h2>
        <p>
          DiffLens depends on GitHub and on the configured AI provider. Their
          terms apply to your use of them, alongside these terms.
        </p>

        <h2>9. Your own AI keys</h2>
        <p>
          If you add an AI provider key in Settings, it is your key, your
          cost, and your relationship with that provider. The key is stored
          encrypted and you can remove it at any time. Reviews you start with
          your key are billed by your provider to you.
        </p>

        <h2>10. Intellectual property</h2>
        <p>
          The DiffLens source code is open source under the MIT license. The
          DiffLens name and the hosted service are the operator&apos;s. These
          terms do not grant you rights in either beyond using the service.
        </p>

        <h2>11. Termination</h2>
        <p>
          You can stop using DiffLens at any time, and you can ask for your
          data to be deleted through the{" "}
          <Link href="/contact">contact form</Link>. The operator can suspend
          or end the service, or your access to it, at any time. On a
          deletion request your data is deleted as described in the{" "}
          <Link href="/privacy">privacy policy</Link>.
        </p>

        <h2>12. Warranties</h2>
        <p>
          THE SERVICE IS PROVIDED AS IS AND AS AVAILABLE, WITHOUT WARRANTY OF
          ANY KIND. TO THE MAXIMUM EXTENT PERMITTED BY LAW, THE OPERATOR
          DISCLAIMS ALL WARRANTIES, EXPRESS OR IMPLIED, INCLUDING
          MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE, NON-INFRINGEMENT,
          AND ANY WARRANTY THAT THE SERVICE WILL BE UNINTERRUPTED, SECURE, OR
          ERROR-FREE.
        </p>

        <h2>13. Limitation of liability</h2>
        <p>
          To the maximum extent permitted by law, the operator&apos;s total
          liability for all claims arising out of or relating to the service
          is capped at CAD $50 or the amount you paid for the service in the
          past 12 months, whichever is greater. Some jurisdictions do not
          allow certain limitations of liability, so parts of this section
          may not apply to you; in that case liability is limited to the
          smallest extent the law of your jurisdiction allows.
        </p>

        <h2>14. Indemnity</h2>
        <p>
          You will indemnify the operator against third-party claims that
          arise from your reviewing code you had no right to review, or from
          your breach of these terms.
        </p>

        <h2>15. Governing law</h2>
        <p>
          These terms are governed by the laws of the Province of Ontario and
          the federal laws of Canada applicable there. Disputes belong to the
          courts of Ontario.
        </p>

        <h2>16. Changes</h2>
        <p>
          Changes to these terms are posted on this page with a new date at
          the top. Continued use of DiffLens after a change means you accept
          the updated terms.
        </p>

        <h2>17. Complaints and contact</h2>
        <p>
          Questions about these terms, and complaints of any kind, including
          copyright complaints about code that appears in a review, go
          through the <Link href="/contact">contact form</Link>.
        </p>
      </main>
    </>
  );
}
