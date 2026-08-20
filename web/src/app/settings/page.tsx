"use client";

import { FormEvent, useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import Header from "@/components/Header";
import { ApiError, apiFetch } from "@/lib/api";
import type { AIKeyStatus } from "@/lib/types";
import { useMe } from "@/lib/useMe";

const DEFAULT_MODELS: Record<"gemini" | "anthropic", string> = {
  gemini: "gemini-3.6-flash",
  anthropic: "claude-opus-5",
};

export default function SettingsPage() {
  const router = useRouter();
  const me = useMe();
  const [status, setStatus] = useState<AIKeyStatus | null>(null);
  const [provider, setProvider] = useState<"gemini" | "anthropic">("gemini");
  const [apiKey, setApiKey] = useState("");
  const [model, setModel] = useState("");
  const [busy, setBusy] = useState(false);
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

  async function signOut() {
    await apiFetch<undefined>("/auth/logout", { method: "POST" });
    router.push("/login");
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
      setMessage(
        err instanceof ApiError && err.status === 422
          ? "That key looks too short. Paste the full key."
          : "Saving failed. Try again.",
      );
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
    } catch {
      setMessage("Removing failed. Try again.");
    } finally {
      setBusy(false);
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
                onClick={() => void remove()}
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
                onChange={(e) =>
                  setProvider(e.target.value as "gemini" | "anthropic")
                }
              >
                <option value="gemini">Gemini</option>
                <option value="anthropic">Anthropic</option>
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
                placeholder={`Provider default (${DEFAULT_MODELS[provider]})`}
                value={model}
                onChange={(e) => setModel(e.target.value)}
              />
            </div>
            <div className="form-row">
              <button
                className="button"
                type="submit"
                disabled={busy || apiKey.trim().length < 10}
              >
                Save key
              </button>
              {message ? <p className="muted">{message}</p> : null}
            </div>
          </form>
        </section>
      </main>
    </div>
  );
}
