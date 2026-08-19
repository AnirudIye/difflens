"use client";

import Link from "next/link";
import Mark from "@/components/Mark";
import type { Me } from "@/lib/types";

export default function Header({
  user,
  onSignOut,
}: {
  user: Me | null;
  onSignOut?: () => void;
}) {
  return (
    <header className="site-header">
      <Link className="brand" href="/">
        <Mark size={22} />
        <span className="brand-name">DiffLens</span>
      </Link>
      {user ? (
        <div className="session">
          {/* eslint-disable-next-line @next/next/no-img-element -- avatars come from GitHub's CDN, no image loader configured */}
          <img
            className="avatar"
            src={user.avatar_url}
            alt=""
            width={24}
            height={24}
          />
          <span className="session-login">{user.login}</span>
          <Link className="button button-quiet" href="/settings">
            Settings
          </Link>
          <button className="button button-quiet" type="button" onClick={onSignOut}>
            Sign out
          </button>
        </div>
      ) : null}
    </header>
  );
}
