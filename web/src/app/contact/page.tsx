import type { Metadata } from "next";
import Link from "next/link";
import ThemeToggle from "@/components/ThemeToggle";
import ContactForm from "./ContactForm";

export const metadata: Metadata = {
  title: "Contact - DiffLens",
  description: "Write to the operator of DiffLens. No account needed.",
};

export default function ContactPage() {
  return (
    <>
      <div className="corner-actions">
        <ThemeToggle />
      </div>
      <main className="legal">
        <Link className="back-link" href="/">
          Back to DiffLens
        </Link>
        <h1 className="legal-title">Contact</h1>
        <p className="legal-updated">
          The one channel for everything: questions, bug reports,
          accessibility barriers, privacy requests, and complaints.
        </p>
        <p>
          You do not need an account to write here. If you want a reply,
          include an email address; without one your message is still read,
          but there is no way to answer it.
        </p>
        <ContactForm />
      </main>
    </>
  );
}
