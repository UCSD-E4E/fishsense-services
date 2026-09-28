// Ported from fishsense-lite@77e8f8e5 apps/fishsense-lite-web/app/layout.tsx.
import type { Metadata } from "next";
import pkg from "../package.json";
import "./globals.css";

export const metadata: Metadata = {
  title: "E4E FishSense",
  description: "FishSense dashboard",
  // SVG first (modern browsers prefer the vector); 64x64 PNG fallback
  // for clients that don't render SVG favicons (older Safari + niche
  // legacy clients). Browsers walk the array and pick the first
  // type they support, so order matters.
  icons: {
    icon: [
      { url: "/logo.svg", type: "image/svg+xml" },
      { url: "/favicon.png", type: "image/png", sizes: "64x64" },
    ],
  },
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="flex min-h-screen flex-col bg-slate-50 text-slate-900 antialiased dark:bg-slate-950 dark:text-slate-100">
        <div className="flex-1">{children}</div>
        <footer className="border-t border-slate-200 px-6 py-3 text-xs text-slate-500 dark:border-slate-800 dark:text-slate-400">
          fishsense-web v{pkg.version}
        </footer>
      </body>
    </html>
  );
}
