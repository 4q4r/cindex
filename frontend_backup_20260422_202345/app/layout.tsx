import type { Metadata } from "next";
import { PT_Sans, PT_Serif } from "next/font/google";
import "./globals.css";

const display = PT_Sans({ subsets: ["latin", "cyrillic"], weight: ["400", "700"], variable: "--font-display" });
const body = PT_Serif({ subsets: ["latin", "cyrillic"], weight: ["400", "700"], variable: "--font-body" });

export const metadata: Metadata = {
  title: "CIndex",
  description: "Citation-ready scholarly search",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className={`${display.variable} ${body.variable}`}>{children}</body>
    </html>
  );
}
