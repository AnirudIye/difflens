"use client";

import Link from "next/link";
import Mark from "@/components/Mark";
import ThemeToggle from "@/components/ThemeToggle";
import UserMenu from "@/components/UserMenu";
import type { MeState } from "@/lib/useMe";

export default function Header({
  me,
  onSignOut,
}: {
  me: MeState;
  onSignOut?: () => void;
}) {
  // Navigation disappears only when the user is definitively signed out.
  // Middleware already proved a session cookie exists to get here, so a
  // server we cannot reach must not cost the user their way around.
  const showNav = me.kind !== "anon";
  const user = me.kind === "authed" ? me.me : null;

  return (
    <header className="site-header">
      <Link className="brand" href={user ? "/dashboard" : "/"}>
        <Mark size={22} />
        <span className="brand-name">DiffLens</span>
      </Link>
      <div className="header-actions">
        {/* Theme is a device preference, not session state, so it stays
            put whether or not we know who is signed in */}
        <ThemeToggle />
        {showNav ? <UserMenu user={user} onSignOut={onSignOut} /> : null}
      </div>
    </header>
  );
}
