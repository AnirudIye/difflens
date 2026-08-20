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

    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [open]);

  return (
    <div className="menu" ref={wrapper}>
      <button
        className="menu-trigger"
        ref={trigger}
        type="button"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((was) => !was)}
      >
        {user ? (
          <>
            {/* eslint-disable-next-line @next/next/no-img-element -- avatars come from GitHub's CDN, no image loader configured */}
            <img
              className="avatar"
              src={user.avatar_url}
              alt=""
              width={24}
              height={24}
            />
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
        <div className="menu-list" role="menu">
          <Link
            className="menu-item"
            href="/repositories"
            role="menuitem"
            onClick={() => setOpen(false)}
          >
            Repositories
          </Link>
          <Link
            className="menu-item"
            href="/settings"
            role="menuitem"
            onClick={() => setOpen(false)}
          >
            Settings
          </Link>
          <button
            className="menu-item menu-item-danger"
            type="button"
            role="menuitem"
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
