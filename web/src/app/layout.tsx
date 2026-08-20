import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "DiffLens",
  description: "AI-assisted code review for GitHub pull requests",
};

/* Runs before the first paint, so a user who chose a theme never sees the
   other one flash first. It duplicates readChoice/applyChoice from
   lib/theme.ts on purpose: importing that module would mean waiting for the
   bundle, which is exactly the wait this avoids. Keep the two in step. */
const NO_FLASH_SCRIPT = `try{var c=localStorage.getItem("difflens-theme");if(c==="light"||c==="dark"){document.documentElement.dataset.theme=c}}catch(e){}`;

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    // The script above edits this element before React hydrates it
    <html lang="en" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: NO_FLASH_SCRIPT }} />
      </head>
      <body>{children}</body>
    </html>
  );
}
