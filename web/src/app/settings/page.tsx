"use client";

import { FormEvent, useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import ConfirmDialog from "@/components/ConfirmDialog";
import Header from "@/components/Header";
import { ApiError, apiFetch } from "@/lib/api";
import type { AIKeyStatus } from "@/lib/types";
import { useMe } from "@/lib/useMe";
import { useSignOut } from "@/lib/useSignOut";

type Provider = "gemini" | "anthropic" | "openai";

const DEFAULT_MODELS: Record<Provider, string> = {
  gemini: "gemini-3.6-flash",
  anthropic: "claude-opus-5",
  openai: "gpt-5.6-terra",
};

export default function SettingsPage() {
  const router = useRouter();
  const me = useMe();
  const signOut = useSignOut();
  const [status, setStatus] = useState<AIKeyStatus | null>(null);
  const [provider, setProvider] = useState<Provider>("gemini");
  const [apiKey, setApiKey] = useState("");
  const [model, setModel] = useState("");
  const [busy, setBusy] = useState(false);
  const [confirmRemove, setConfirmRemove] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const data = await apiFetch<AIKeyStatus>("/settings/ai-key");
      setStatus(data);
      if (data.provider) {
        setProvider(data.provider);
      }
      setModel(data.model ?? "");
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        router.push("/login");
        return;
      }
      setMessage("Could not load your settings. Refresh to try again.");
    }
  }, [router]);

  useEffect(() => {
    void load();
  }, [load]);

  // 10 characters is the API's own floor, so the form refuses what the
  // server would refuse rather than making a round trip to hear it
  const typed = apiKey.trim().length > 0;
  const tooShort = apiKey.trim().length < 10;

  /** A session that ran out mid-edit is not a server failure; go and sign in. */
  function expired(err: unknown): boolean {
    if (err instanceof ApiError && err.status === 401) {
      router.push("/login");
      return true;
    }
    return false;
  }

  /** Say which field the API rejected instead of guessing from the status.
   *
   * The form already refuses a short key, so a 422 reaching here is almost
   * always about a different field. Blaming the key for a model name that is
   * too long sends the user to re-paste a key that was never the problem.
   */
  function rejectionMessage(err: unknown): string {
    if (!(err instanceof ApiError) || err.status !== 422) {
      return "Saving failed. Try again.";
    }
    const fields = err.details.fields;
    const named = Array.isArray(fields)
      ? (fields as { field?: string }[]).map((item) => item.field)
      : [];
    if (named.includes("model")) {
      return "That model name is too long. Leave it blank for the provider default.";
    }
    if (named.includes("provider")) {
      return "Pick one of the providers listed above.";
    }
    return "That key looks too short. Paste the full key.";
  }

  async function save(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setMessage(null);
    try {
      const data = await apiFetch<AIKeyStatus>("/settings/ai-key", {
        method: "PUT",
        body: JSON.stringify({
          provider,
          api_key: apiKey,
          model: model.trim() || null,
        }),
      });
      setStatus(data);
      setApiKey("");
      setMessage("Saved. Reviews you start now run on your key.");
    } catch (err) {
      if (expired(err)) {
        return;
      }
      setMessage(rejectionMessage(err));
    } finally {
      setBusy(false);
    }
  }

  async function remove() {
    setBusy(true);
    setMessage(null);
    try {
      const data = await apiFetch<AIKeyStatus>("/settings/ai-key", {
        method: "DELETE",
      });
      setStatus(data);
      setModel("");
      setMessage("Removed. Reviews fall back to the server's reviewer.");
    } catch (err) {
      if (expired(err)) {
        return;
      }
      setMessage("Removing failed. Try again.");
    } finally {
      setBusy(false);
      setConfirmRemove(false);
    }
  }

  return (
    <div className="shell">
      <Header me={me} onSignOut={signOut} />
      <main className="dash">
        <div className="page-head">
          <h1 className="page-title">Settings</h1>
        </div>

        <section className="settings-section">
          <h2 className="section-title">AI reviewer key</h2>
          <p className="muted section-lede">
            Bring your own API key and reviews you start will use it instead of
            the server&apos;s reviewer. The key is encrypted at rest and never
            shown again after saving. Gemini keys have a free tier at{" "}
            <a
              href="https://aistudio.google.com/apikey"
              target="_blank"
              rel="noreferrer"
            >
              aistudio.google.com
            </a>
            .
          </p>

          {status?.configured ? (
            <div className="notice key-notice">
              {status.key_invalid ? (
                <p>
                  Your saved {status.provider} key can no longer be read.
                  Reviews you start will fail until you paste it again below,
                  or remove it to use the server&apos;s reviewer.
                </p>
              ) : (
                <p>
                  Using your {status.provider} key
                  {status.key_hint ? (
                    <>
                      {" "}
                      ending in <span className="mono">{status.key_hint}</span>
                    </>
                  ) : null}
                  {status.model ? (
                    <>
                      , model <span className="mono">{status.model}</span>
                    </>
                  ) : null}
                  .
                </p>
              )}
              <button
                className="button button-quiet"
                type="button"
                onClick={() => setConfirmRemove(true)}
                disabled={busy}
              >
                Remove key
              </button>
            </div>
          ) : null}

          <form className="key-form" onSubmit={(e) => void save(e)}>
            <div className="field">
              <label className="field-label" htmlFor="ai-provider">
                Provider
              </label>
              <select
                id="ai-provider"
                value={provider}
                onChange={(e) => setProvider(e.target.value as Provider)}
              >
                <option value="gemini">Gemini</option>
                <option value="anthropic">Anthropic</option>
                <option value="openai">OpenAI</option>
              </select>
            </div>
            <div className="field">
              <label className="field-label" htmlFor="ai-key">
                API key
              </label>
              <input
                id="ai-key"
                type="password"
                autoComplete="off"
                placeholder={
                  status?.configured
                    ? "Paste a new key to replace the saved one"
                    : "Paste your API key"
                }
                value={apiKey}
                onChange={(e) => setApiKey(e.target.value)}
              />
            </div>
            <div className="field">
              <label className="field-label" htmlFor="ai-model">
                Model <span className="field-optional">optional</span>
              </label>
              <input
                id="ai-model"
                type="text"
                // The API's own ceiling, enforced here so a paste that is
                // obviously wrong never becomes a round trip
                maxLength={100}
                placeholder={`Provider default (${DEFAULT_MODELS[provider]})`}
                value={model}
                onChange={(e) => setModel(e.target.value)}
              />
            </div>
            <div className="form-row">
              <button className="button" type="submit" disabled={busy || tooShort}>
                Save key
              </button>
              {message ? (
                <p className="muted">{message}</p>
              ) : typed && tooShort ? (
                // Without this the button is simply dead and never says why,
                // which is exactly what a half-pasted key looks like
                <p className="muted">
                  That is shorter than any provider&apos;s key. Paste the whole
                  thing.
                </p>
              ) : null}
            </div>
          </form>
        </section>
      </main>

      <ConfirmDialog
        open={confirmRemove}
        title="Remove your API key?"
        body={
          "The stored key is deleted and cannot be recovered. Reviews you " +
          "start will fall back to the server's reviewer until you paste a " +
          "key again."
        }
        confirmLabel="Remove key"
        destructive
        busy={busy}
        onConfirm={() => void remove()}
        onCancel={() => setConfirmRemove(false)}
      />
    </div>
  );
}
