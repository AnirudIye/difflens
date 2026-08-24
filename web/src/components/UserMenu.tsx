"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import type { Me } from "@/lib/types";

/** The account menu in the header.
 *
 * Collapsing the account actions behind one control is what keeps the
 * header from spilling off a narrow screen, which is how the link to
 * Settings became unreachable in the first place.
 */
export default function UserMenu({
  user,
  onSignOut,
}: {
  user: Me | null;
  onSignOut?: () => void;
}) {
  const [open, setOpen] = useState(false);
  const wrapper = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (!open) {
      return;
    }

    function onPointerDown(event: MouseEvent) {
      if (!wrapper.current?.contains(event.target as Node)) {
        setOpen(false);
      }
    }
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        setOpen(false);
        trigger.current?.focus(); // Escape must not strand focus in a closed menu
      }
    }

    function onFocusIn(event: FocusEvent) {
      // Tabbing past the last item moves focus out of the menu but left it
      // open, and an open menu covers whatever comes next in the tab order.
      // A keyboard user then tabs into something they cannot see.
      if (!wrapper.current?.contains(event.target as Node)) {
        setOpen(false);
      }
    }

    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    document.addEventListener("focusin", onFocusIn);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
      document.removeEventListener("focusin", onFocusIn);
    };
  }, [open]);

  return (
    <div className="menu" ref={wrapper}>
      <button
        className="menu-trigger"
        ref={trigger}
        type="button"
        aria-expanded={open}
        onClick={() => setOpen((was) => !was)}
      >
        {user ? (
          <>
            {user.avatar_url ? (
              /* eslint-disable-next-line @next/next/no-img-element -- avatars come from GitHub's CDN, no image loader configured */
              <img
                className="avatar"
                src={user.avatar_url}
                alt=""
                width={24}
                height={24}
              />
            ) : null}
            <span className="menu-label">{user.login}</span>
          </>
        ) : (
          <span className="menu-label">Account</span>
        )}
        <span aria-hidden="true" className="menu-caret">
          &#9662;
        </span>
      </button>

      {open ? (
        <div className="menu-list">
          <Link
            className="menu-item"
            href="/repositories"
            onClick={() => setOpen(false)}
          >
            Repositories
          </Link>
          <Link
            className="menu-item"
            href="/settings"
            onClick={() => setOpen(false)}
          >
            Settings
          </Link>
          <button
            className="menu-item menu-item-danger"
            type="button"
            onClick={() => {
              setOpen(false);
              onSignOut?.();
            }}
          >
            Sign out
          </button>
        </div>
      ) : null}
    </div>
  );
}
