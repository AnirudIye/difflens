/** Theme choice: a tiny store outside React, because the theme has to be on
 *  the document before React exists.
 *
 *  Three choices, two rendered themes. "system" is the absence of a stored
 *  preference, not a third palette: it lets the OS decide and keeps deciding
 *  when the OS changes. Storing "system" explicitly would be the same thing
 *  with an extra key to keep in sync, so it is stored by removing the key.
 */

export type ThemeChoice = "system" | "light" | "dark";

/** Also hardcoded in the no-flash script in layout.tsx. Keep them together. */
export const THEME_STORAGE_KEY = "difflens-theme";

export const THEME_CHOICES: readonly ThemeChoice[] = ["system", "light", "dark"];

const listeners = new Set<() => void>();

function isChoice(value: unknown): value is ThemeChoice {
  return value === "light" || value === "dark";
}

export function readChoice(): ThemeChoice {
  try {
    const stored = window.localStorage.getItem(THEME_STORAGE_KEY);
    return isChoice(stored) ? stored : "system";
  } catch {
    // Private mode and blocked storage both throw here. The theme is a
    // preference, not a feature, so losing it is not worth an error path.
    return "system";
  }
}

/** Stamp the document. "system" removes the attribute so the
 *  prefers-color-scheme rules in globals.css take over again. */
export function applyChoice(choice: ThemeChoice): void {
  const root = document.documentElement;
  if (choice === "system") {
    delete root.dataset.theme;
  } else {
    root.dataset.theme = choice;
  }
}

export function setChoice(choice: ThemeChoice): void {
  try {
    if (choice === "system") {
      window.localStorage.removeItem(THEME_STORAGE_KEY);
    } else {
      window.localStorage.setItem(THEME_STORAGE_KEY, choice);
    }
  } catch {
    // Unstorable is still switchable for this tab.
  }
  applyChoice(choice);
  for (const listener of listeners) {
    listener();
  }
}

export function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  // A second tab changing the theme fires storage here, never in the tab
  // that wrote it, so both paths are needed to keep tabs agreeing.
  const onStorage = (event: StorageEvent) => {
    if (event.key === THEME_STORAGE_KEY || event.key === null) {
      applyChoice(readChoice());
      listener();
    }
  };
  window.addEventListener("storage", onStorage);
  return () => {
    listeners.delete(listener);
    window.removeEventListener("storage", onStorage);
  };
}

export function nextChoice(choice: ThemeChoice): ThemeChoice {
  const index = THEME_CHOICES.indexOf(choice);
  return THEME_CHOICES[(index + 1) % THEME_CHOICES.length];
}

export const THEME_LABELS: Record<ThemeChoice, string> = {
  system: "System",
  light: "Light",
  dark: "Dark",
};
