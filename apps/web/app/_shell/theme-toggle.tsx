"use client";

import { useState } from "react";
import { applyTheme, type Theme } from "@/lib/theme";

const CHOICES: { theme: Theme; label: string; icon: React.ReactNode }[] = [
  {
    theme: "system",
    label: "Match the system",
    icon: (
      <>
        <rect x="3" y="4" width="18" height="12" rx="2" />
        <path d="M8 20h8M12 16v4" />
      </>
    ),
  },
  {
    theme: "light",
    label: "Light",
    icon: (
      <>
        <circle cx="12" cy="12" r="4" />
        <path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" />
      </>
    ),
  },
  {
    theme: "dark",
    label: "Dark",
    icon: <path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z" />,
  },
];

/**
 * System, light or dark, for this browser (lib/theme.ts). The header renders
 * it with the cookie's pick, so the server's page and the switch agree; a
 * click switches the open page at once and remembers it.
 */
export function ThemeToggle({ initial }: { initial: Theme }) {
  const [theme, setTheme] = useState<Theme>(initial);
  return (
    <div role="radiogroup" aria-label="Theme" className="inline-flex rounded-control border border-line p-0.5">
      {CHOICES.map((c) => (
        <button
          key={c.theme}
          type="button"
          role="radio"
          aria-checked={theme === c.theme}
          aria-label={c.label}
          title={c.label}
          onClick={() => {
            applyTheme(c.theme, document);
            setTheme(c.theme);
          }}
          className="inline-flex size-9 items-center justify-center rounded-[8px] text-fg-2 hover:text-fg aria-checked:bg-raised aria-checked:text-fg"
        >
          <svg
            viewBox="0 0 24 24"
            className="size-[18px]"
            fill="none"
            stroke="currentColor"
            strokeWidth={1.8}
            strokeLinecap="round"
            strokeLinejoin="round"
            aria-hidden
          >
            {c.icon}
          </svg>
        </button>
      ))}
    </div>
  );
}
