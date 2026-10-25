import type { Metadata } from "next";
import "dockview-react/dist/styles/dockview.css";
import "./globals.css";

export const metadata: Metadata = {
  title: "Potbot — Orb Workspace",
  description: "Workspace-oriented shell for Potbot reasoning, chat and tools",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="en"><body>{children}</body></html>;
}
