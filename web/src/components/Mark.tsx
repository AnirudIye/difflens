export default function Mark({
  size = 44,
  className,
}: {
  size?: number;
  className?: string;
}) {
  return (
    <svg
      className={className}
      width={size}
      height={size}
      viewBox="0 0 48 48"
      fill="none"
      aria-hidden="true"
    >
      <circle cx="20" cy="20" r="13" stroke="currentColor" strokeWidth="2.5" />
      <line
        x1="30.5"
        y1="30.5"
        x2="40"
        y2="40"
        stroke="currentColor"
        strokeWidth="2.5"
        strokeLinecap="round"
      />
      <line
        x1="14"
        y1="16.5"
        x2="26"
        y2="16.5"
        stroke="var(--accent)"
        strokeWidth="2.5"
        strokeLinecap="round"
      />
      <line
        x1="14"
        y1="23.5"
        x2="22"
        y2="23.5"
        stroke="currentColor"
        strokeWidth="2.5"
        strokeLinecap="round"
        opacity="0.45"
      />
    </svg>
  );
}
