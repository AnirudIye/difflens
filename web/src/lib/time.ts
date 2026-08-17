export function relativeTime(iso: string | null | undefined): string {
  if (!iso) {
    return "never";
  }
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) {
    return "never";
  }

  const seconds = Math.floor((Date.now() - then) / 1000);
  if (seconds < 45) {
    return "just now";
  }
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) {
    return `${Math.max(minutes, 1)}m ago`;
  }
  const hours = Math.floor(minutes / 60);
  if (hours < 24) {
    return `${hours}h ago`;
  }
  const days = Math.floor(hours / 24);
  if (days < 30) {
    return `${days}d ago`;
  }
  // ponytail: calendar-exact months/years are overkill for a "synced 2mo ago"
  // label, 30-day months are close enough until someone complains
  const months = Math.floor(days / 30);
  if (months < 12) {
    return `${months}mo ago`;
  }
  return `${Math.floor(months / 12)}y ago`;
}
