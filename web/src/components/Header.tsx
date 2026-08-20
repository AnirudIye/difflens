"use client";

import Link from "next/link";
import Mark from "@/components/Mark";
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
      {showNav ? <UserMenu user={user} onSignOut={onSignOut} /> : null}
    </header>
  );
}
