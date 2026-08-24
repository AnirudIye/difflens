import type { Metadata } from "next";
import Link from "next/link";
import ThemeToggle from "@/components/ThemeToggle";

export const metadata: Metadata = {
  title: "Privacy - DiffLens",
  description:
    "What DiffLens collects, what it deliberately does not, and how to exercise your rights.",
};

const PROCESSORS: { name: string; role: string; url: string }[] = [
  {
    name: "Vercel",
    role: "Hosts the web frontend you are reading now",
    url: "https://vercel.com/legal/privacy-policy",
  },
  {
    name: "Render",
    role: "Hosts the API and the review worker (Oregon, United States)",
    url: "https://render.com/privacy",
  },
  {
    name: "Neon",
    role: "The Postgres database holding accounts, reviews, and findings",
    url: "https://neon.tech/privacy-policy",
  },
  {
    name: "Upstash",
    role: "Redis, used for job dispatch and rate limiting",
    url: "https://upstash.com/trust/privacy.pdf",
  },
  {
    name: "GitHub",
    role: "Sign-in, and the source of the code you choose to review",
    url: "https://docs.github.com/en/site-policy/privacy-policies/github-general-privacy-statement",
  },
  {
    name: "Google",
    role: "Gemini, the default AI reviewer",
    url: "https://policies.google.com/privacy",
  },
  {
    name: "Anthropic",
    role: "AI reviewer, only if you add your own Anthropic key",
    url: "https://www.anthropic.com/legal/privacy",
  },
  {
    name: "OpenAI",
    role: "AI reviewer, only if you add your own OpenAI key",
    url: "https://openai.com/policies/privacy-policy/",
  },
  {
    name: "Resend",
    role: "Delivers contact form messages to the operator by email",
    url: "https://resend.com/legal/privacy-policy",
  },
];

export default function PrivacyPage() {
  return (
    <>
      <div className="corner-actions">
        <ThemeToggle />
      </div>
      <main className="legal">
        <Link className="back-link" href="/">
          Back to DiffLens
        </Link>
        <h1 className="legal-title">Privacy policy</h1>
        <p className="legal-updated">Last updated: 2026-08-24</p>

        <h2>1. Who we are</h2>
        <p>
          DiffLens is a code review service operated by Anirud, an individual
          developer based in Canada. There is no company behind it. The way to
          reach the operator, including for anything in this policy, is the{" "}
          <Link href="/contact">contact form</Link>.
        </p>

        <h2>2. What DiffLens does</h2>
        <p>
          DiffLens reviews code you choose to review from GitHub. It runs
          deterministic analyzers and an AI reviewer over that code and shows
          you findings tied to files and lines. This policy describes what
          information that involves.
        </p>

        <h2>3. Information we collect</h2>
        <h3>What we do not collect</h3>
        <ul>
          <li>No analytics or tracking cookies. None.</li>
          <li>No advertising and no advertising identifiers.</li>
          <li>No payment information; the service is free.</li>
          <li>No email address at sign-up.</li>
          <li>
            No private repositories. The GitHub access DiffLens requests is
            read-only and limited to public code.
          </li>
          <li>No precise location data.</li>
        </ul>
        <p>
          One thing that is easy to miss: DiffLens does handle your IP address,
          because it has to. It is used to rate limit the pages anyone can use
          without an account, and it appears in ordinary server logs. It is
          never linked to your account, never used to profile you, and never
          sold or shared. The detail is in the next list.
        </p>
        <h3>What we do collect</h3>
        <ul>
          <li>
            Your public GitHub profile via OAuth: login, display name, and
            avatar. GitHub sends these when you sign in.
          </li>
          <li>
            A GitHub access token, stored encrypted. It is requested with a
            deliberately empty scope, so it can read public repositories and
            nothing else, and it cannot write anywhere.
          </li>
          <li>
            An AI provider API key, only if you add one in Settings. It is
            stored encrypted and shown afterwards only as its last four
            characters.
          </li>
          <li>
            Review results: findings with file paths, line numbers, and short
            code snippets from the public code you asked DiffLens to review,
            plus summaries.
          </li>
          <li>Your feedback on findings (useful, not useful, dismissed).</li>
          <li>
            Contact form submissions: the message, and a name, email address,
            and subject only if you choose to give them. The message is stored
            in our database and, when email forwarding is configured, sent on
            to the operator through Resend, listed in the processor table
            below. Your IP address is deliberately not stored with it.
          </li>
          <li>
            Your IP address, for rate limiting and in server logs. Rate limit
            counters are held in Redis under a key containing the address and
            expire within an hour. Server logs record request addresses in the
            ordinary way and are short lived. Neither is stored in the database
            and neither is attached to your account.
          </li>
        </ul>

        <h2>4. How we use information</h2>
        <p>
          To provide the service you asked for, to keep it secure, and to
          prevent abuse. Nothing else. DiffLens does not sell personal
          information, does not advertise, and does not profile you.
        </p>
        <h3>What the AI reviewer sees</h3>
        <p>
          The code under review is sent to the configured AI provider to be
          reviewed. If you have not added your own AI key, that provider is
          Google Gemini on a free API tier, and Google&apos;s terms for that
          free tier allow Google to use submitted content to improve its
          products. DiffLens only reviews public code, but you should know
          this before reviewing anything.
        </p>
        <p>
          If you add your own key in Settings, your reviews run under your own
          provider&apos;s API terms instead. Paid API tiers generally do not
          train on API data; check your provider&apos;s terms. DiffLens itself
          never trains models on your data.
        </p>

        <h2>5. Cookies</h2>
        <p>
          DiffLens sets two cookies, both strictly necessary for signing in.
          There are no advertising or analytics cookies and no cross-site
          tracking. Your theme preference is stored in your browser&apos;s
          localStorage and never leaves your device.
        </p>
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>Cookie</th>
                <th>Purpose</th>
                <th>Lifetime</th>
              </tr>
            </thead>
            <tbody>
              <tr>
                <td>session</td>
                <td>Keeps you signed in. Removed when you sign out.</td>
                <td>7 days</td>
              </tr>
              <tr>
                <td>oauth_state</td>
                <td>Protects the GitHub sign-in flow against forgery.</td>
                <td>10 minutes</td>
              </tr>
            </tbody>
          </table>
        </div>

        <h2>6. Third-party processors</h2>
        <p>
          DiffLens runs on a small set of infrastructure providers. Each one
          processes data only to run the service, under its own privacy
          policy:
        </p>
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>Provider</th>
                <th>What it does for DiffLens</th>
                <th>Privacy policy</th>
              </tr>
            </thead>
            <tbody>
              {PROCESSORS.map((processor) => (
                <tr key={processor.name}>
                  <td>{processor.name}</td>
                  <td>{processor.role}</td>
                  <td>
                    <a href={processor.url} target="_blank" rel="noreferrer">
                      {processor.name} privacy policy
                    </a>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        <h2>7. How long we keep things</h2>
        <ul>
          <li>
            Reviewed code: processed in a temporary workspace and discarded
            when the review finishes. It is not stored.
          </li>
          <li>
            Findings and summaries: kept so you can revisit your reviews,
            until you ask for deletion.
          </li>
          <li>
            Sessions: stop working after 7 days. Signing out deletes the
            session row immediately; a session simply left to expire stops
            being usable at 7 days and its row is removed when you ask for
            deletion.
          </li>
          <li>
            Encrypted GitHub token and AI key: kept until you remove them in
            Settings or request deletion.
          </li>
          <li>Contact messages: kept until your request is handled.</li>
          <li>
            Rate limit counters: held in Redis under a key containing your IP
            address, and expire automatically within an hour.
          </li>
          <li>
            Server logs: our application logs are redacted of tokens and
            secrets before they are written. Logs record request IP addresses
            in the ordinary way, and everything is short-lived on our hosting
            provider.
          </li>
        </ul>

        <h2>8. Your rights</h2>
        <p>
          Whatever your location, you can ask what DiffLens holds about you,
          ask for it to be corrected, or ask for it to be deleted. Send the
          request through the <Link href="/contact">contact form</Link>; you
          do not need to be signed in. Deletion is currently a manual process
          handled by the operator, and requests are completed within 30 days.
          There is no self-serve delete button yet, and this policy will not
          pretend otherwise.
        </p>

        <h2>9. International transfers</h2>
        <p>
          The service is operated from Canada and the data is processed in the
          United States by the providers listed above. Where a transfer
          mechanism is required, it is provided by those processors&apos; own
          frameworks, including standard contractual clauses.
        </p>

        <h2>10. Children</h2>
        <p>
          DiffLens is not directed to children under 13, or under 16 in the
          EEA and UK. If you believe a child has created an account, use the{" "}
          <Link href="/contact">contact form</Link> and it will be removed.
        </p>

        <h2>11. Security</h2>
        <ul>
          <li>All traffic to the hosted service uses HTTPS.</li>
          <li>
            GitHub tokens and AI keys are encrypted at rest, not merely
            hashed or hidden.
          </li>
          <li>
            The GitHub OAuth scope is empty by design: read-only, public code
            only, no write access to anything.
          </li>
          <li>Logs are redacted of tokens and secrets.</li>
          <li>There is no payment data anywhere in the system.</li>
        </ul>
        <p>
          If a breach affects your personal information, a notice will be
          posted on this page and on the project&apos;s GitHub repository
          without undue delay, and regulators will be notified where the law
          requires it. Being direct about the limit: DiffLens holds no email
          address for you, so it cannot contact you individually. If you want
          to be told directly, leave an address through the contact form and
          it will be used for that and nothing else.
        </p>

        <h2>12. Changes to this policy</h2>
        <p>
          Changes are posted on this page with a new date at the top.
          Continued use of DiffLens after a change means the updated policy
          applies.
        </p>

        <h2>13. Contact</h2>
        <p>
          For anything in this policy, use the{" "}
          <Link href="/contact">contact form</Link>. It works without an
          account.
        </p>

        <h2>14. Regional supplements</h2>
        <h3>EEA and UK (GDPR)</h3>
        <p>
          The legal bases for processing are: performance of a contract, for
          providing the service you signed up for; and legitimate interest,
          for security and abuse prevention. You have the rights of access,
          rectification, erasure, restriction, portability, and objection
          under Articles 15 to 22. Requests go through the{" "}
          <Link href="/contact">contact form</Link> and are answered within 30
          days. You also have the right to lodge a complaint with your
          supervisory authority. Transfers to the United States rely on the
          processors&apos; own safeguards, including standard contractual
          clauses.
        </p>
        <h3>California (CCPA)</h3>
        <p>
          DiffLens does not sell or share personal information, and has not in
          the preceding 12 months. You have the right to know, delete, and
          correct. Requests go through the{" "}
          <Link href="/contact">contact form</Link> and will not result in
          different treatment. Categories collected in the last 12 months:
        </p>
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>Category</th>
                <th>Collected</th>
                <th>Examples</th>
              </tr>
            </thead>
            <tbody>
              <tr>
                <td>Identifiers</td>
                <td>Yes</td>
                <td>
                  GitHub login, display name, avatar, and IP address for rate
                  limiting and server logs
                </td>
              </tr>
              <tr>
                <td>Customer records (payment, government ID)</td>
                <td>No</td>
                <td>None held</td>
              </tr>
              <tr>
                <td>Commercial information</td>
                <td>No</td>
                <td>None held</td>
              </tr>
              <tr>
                <td>Internet activity</td>
                <td>Yes, limited</td>
                <td>Reviews you start and feedback you give inside DiffLens</td>
              </tr>
              <tr>
                <td>Geolocation</td>
                <td>No</td>
                <td>No precise location is collected</td>
              </tr>
              <tr>
                <td>Sensitive personal information</td>
                <td>No</td>
                <td>None held</td>
              </tr>
            </tbody>
          </table>
        </div>
      </main>
    </>
  );
}
