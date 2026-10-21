/**
 * The FishSense mark: a stand-in monoline fish with a teal dot, loosely after
 * the current logo, which may be replaced (docs/design/README.md). It draws in
 * the current text colour, so it follows the theme.
 */
export function Mark({ className = "h-7 w-auto" }: { className?: string }) {
  return (
    <svg
      viewBox="0 0 48 30"
      fill="none"
      stroke="currentColor"
      strokeWidth={3}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden
      className={className}
    >
      <path d="M12 15c6-8 20-10 32 0c-12 10-26 8-32 0z" />
      <path d="M12 15c-3-3-6-6-9-5c1 3 2 4 4 5c-2 1-3 2-4 5c3 1 6-2 9-5" />
      <circle cx={38} cy={14} r={1.6} fill="currentColor" stroke="none" />
      <circle cx={24} cy={16} r={2.4} className="fill-teal" stroke="none" />
    </svg>
  );
}
