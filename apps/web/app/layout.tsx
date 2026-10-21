// Ported from fishsense-lite@77e8f8e5 apps/fishsense-lite-web/app/layout.tsx.
import type { Metadata } from "next";
import { Inter, JetBrains_Mono } from "next/font/google";
import pkg from "../package.json";
import { AppHeader } from "./_shell/app-header";
import "./globals.css";

// The design's two families (docs/design/README.md): Inter for what a person
// reads, JetBrains Mono for what they copy. Self-hosted by next/font at build.
const inter = Inter({ subsets: ["latin"], variable: "--font-inter", display: "swap" });
const mono = JetBrains_Mono({
  subsets: ["latin"],
  variable: "--font-jetbrains-mono",
  display: "swap",
});

export const metadata: Metadata = {
  title: "FishSense",
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
    <html lang="en" className={`${inter.variable} ${mono.variable}`}>
      <body className="flex min-h-screen flex-col bg-ground font-sans text-fg antialiased">
        <AppHeader />
        <div className="flex-1">{children}</div>
        <footer className="border-t border-line px-6 py-3 text-xs text-fg-2">
          Engineers for Exploration, UC San Diego · fishsense-web v{pkg.version}
        </footer>
      </body>
    </html>
  );
}
