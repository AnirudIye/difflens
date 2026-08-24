"use client";

import { FormEvent, useState } from "react";
import { ApiError, apiFetch } from "@/lib/api";

export default function ContactForm() {
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [subject, setSubject] = useState("");
  const [message, setMessage] = useState("");
  // The honeypot. People never see it; form bots fill every input they
  // find. The server answers success either way and stores nothing.
  const [website, setWebsite] = useState("");
  const [busy, setBusy] = useState(false);
  const [sent, setSent] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const empty = message.trim().length === 0;

  async function send(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await apiFetch<{ ok: boolean }>("/contact", {
        method: "POST",
        body: JSON.stringify({ name, email, subject, message, website }),
      });
      setSent(true);
    } catch (err) {
      // The server's own sentence: rate limits and validation both arrive
      // with a message written to be shown, so it is shown
      setError(
        err instanceof ApiError ? err.message : "Sending failed. Try again.",
      );
    } finally {
      setBusy(false);
    }
  }

  if (sent) {
    return (
      <p className="notice" role="status">
        Your message was sent.
      </p>
    );
  }

  return (
    <form className="contact-form" onSubmit={(e) => void send(e)}>
      <div className="field">
        <label className="field-label" htmlFor="contact-name">
          Name <span className="field-optional">optional</span>
        </label>
        <input
          id="contact-name"
          type="text"
          maxLength={200}
          autoComplete="name"
          value={name}
          onChange={(e) => setName(e.target.value)}
        />
      </div>
      <div className="field">
        <label className="field-label" htmlFor="contact-email">
          Email <span className="field-optional">optional</span>
        </label>
        <input
          id="contact-email"
          type="email"
          maxLength={200}
          autoComplete="email"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
        />
      </div>
      <div className="field">
        <label className="field-label" htmlFor="contact-subject">
          Subject <span className="field-optional">optional</span>
        </label>
        <input
          id="contact-subject"
          type="text"
          maxLength={200}
          value={subject}
          onChange={(e) => setSubject(e.target.value)}
        />
      </div>
      <div className="field">
        <label className="field-label" htmlFor="contact-message">
          Message
        </label>
        <textarea
          id="contact-message"
          maxLength={5000}
          required
          value={message}
          onChange={(e) => setMessage(e.target.value)}
        />
      </div>
      <div className="hp-field" aria-hidden="true">
        <label htmlFor="contact-website">Website</label>
        <input
          id="contact-website"
          name="website"
          type="text"
          tabIndex={-1}
          autoComplete="off"
          value={website}
          onChange={(e) => setWebsite(e.target.value)}
        />
      </div>
      <div className="form-row">
        <button className="button" type="submit" disabled={busy || empty}>
          Send message
        </button>
        {error ? (
          <p className="muted" role="alert">
            {error}
          </p>
        ) : null}
      </div>
    </form>
  );
}
