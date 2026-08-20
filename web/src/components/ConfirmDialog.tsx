"use client";

import { useEffect, useId, useRef } from "react";

/** Confirmation built on the native <dialog>.
 *
 * showModal() already gives focus trapping, Escape, inertness of the page
 * behind it, and a styleable ::backdrop, so there is no reason to
 * reimplement any of that in JavaScript.
 */
export default function ConfirmDialog({
  open,
  title,
  body,
  confirmLabel,
  cancelLabel = "Never mind",
  destructive = false,
  busy = false,
  onConfirm,
  onCancel,
}: {
  open: boolean;
  title: string;
  body: string;
  confirmLabel: string;
  cancelLabel?: string;
  destructive?: boolean;
  busy?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  // Per instance, because a page can hold more than one of these. Both
  // dialogs on the review page shared a hardcoded id, so whichever one
  // mounted first named them both: "Run again" was announced as "Stop
  // this review?"
  const titleId = useId();

  useEffect(() => {
    const el = ref.current;
    if (!el) {
      return;
    }
    if (open && !el.open) {
      el.showModal();
    } else if (!open && el.open) {
      el.close();
    }
  }, [open]);

  return (
    <dialog
      className="confirm"
      ref={ref}
      aria-labelledby={titleId}
      onCancel={(event) => {
        // Escape: let React own the open state rather than the DOM
        event.preventDefault();
        if (!busy) {
          onCancel();
        }
      }}
      onClick={(event) => {
        // A click landing on the dialog itself is the backdrop; the panel
        // inside stops its own clicks from reaching here
        if (event.target === ref.current && !busy) {
          onCancel();
        }
      }}
    >
      <div className="confirm-panel" onClick={(event) => event.stopPropagation()}>
        <h2 className="confirm-title" id={titleId}>
          {title}
        </h2>
        <p className="confirm-body">{body}</p>
        <div className="confirm-actions">
          <button
            className="button button-quiet"
            type="button"
            onClick={onCancel}
            disabled={busy}
          >
            {cancelLabel}
          </button>
          <button
            className={destructive ? "button button-danger" : "button"}
            type="button"
            onClick={onConfirm}
            disabled={busy}
            autoFocus
          >
            {busy ? "Working..." : confirmLabel}
          </button>
        </div>
      </div>
    </dialog>
  );
}
