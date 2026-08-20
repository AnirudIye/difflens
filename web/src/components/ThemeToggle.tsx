"use client";

import { useSyncExternalStore } from "react";
import {
  nextChoice,
  readChoice,
  setChoice,
  subscribe,
  THEME_LABELS,
  type ThemeChoice,
} from "@/lib/theme";

/** The server cannot know what is in localStorage, so it renders the default
 *  and useSyncExternalStore swaps in the real value after hydration without
 *  the mismatch a useState initializer reading storage would cause. The
 *  no-flash script in layout.tsx has already painted the right colours by
 *  then; only this button's label moves. */
function serverChoice(): ThemeChoice {
  return "system";
}

function ThemeIcon({ choice }: { choice: ThemeChoice }) {
  const common = {
    width: 15,
    height: 15,
    viewBox: "0 0 24 24",
    fill: "none",
    stroke: "currentColor",
    strokeWidth: 1.8,
    strokeLinecap: "round" as const,
    strokeLinejoin: "round" as const,
    "aria-hidden": true,
  };

  if (choice === "light") {
    return (
      <svg {...common}>
        <circle cx="12" cy="12" r="4" />
        <path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" />
      </svg>
    );
  }
  if (choice === "dark") {
    return (
      <svg {...common}>
        <path d="M20 14.5A8.5 8.5 0 0 1 9.5 4a8.5 8.5 0 1 0 10.5 10.5z" />
      </svg>
    );
  }
  return (
    <svg {...common}>
      <rect x="2.5" y="4" width="19" height="13" rx="2" />
      <path d="M9 20.5h6M12 17v3.5" />
    </svg>
  );
}

/** One button that advances System -> Light -> Dark -> System.
 *
 *  The current mode is spelled out next to the icon rather than left to an
 *  icon alone, because a cycling control whose icon shows the current state
 *  is otherwise indistinguishable from one whose icon shows the next state.
 */
export default function ThemeToggle() {
  const choice = useSyncExternalStore(subscribe, readChoice, serverChoice);
  const following = nextChoice(choice);

  return (
    <button
      className="theme-toggle"
      type="button"
      onClick={() => setChoice(following)}
      aria-label={`Theme: ${THEME_LABELS[choice].toLowerCase()}. Switch to ${THEME_LABELS[following].toLowerCase()}.`}
      title={`Switch to ${THEME_LABELS[following].toLowerCase()} theme`}
    >
      <ThemeIcon choice={choice} />
      <span className="theme-toggle-label">{THEME_LABELS[choice]}</span>
    </button>
  );
}
