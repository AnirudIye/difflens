import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "DiffLens",
  description: "AI-assisted code review for GitHub pull requests",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
