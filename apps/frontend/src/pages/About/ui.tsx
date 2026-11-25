export interface AccentStyle {
  text: string
  bgSoft: string
  border: string
  /** Vertical accent bar on a canvas node. */
  spine: string
  /** Solid border used when a chart node is selected. */
  borderStrong: string
}

export const ACCENT_STYLES: Record<string, AccentStyle> = {
  sky: { text: "text-sky-700 dark:text-sky-400", bgSoft: "bg-sky-500/10", border: "border-sky-500/40 dark:border-sky-500/30", spine: "bg-sky-500", borderStrong: "border-sky-500" },
  cyan: { text: "text-cyan-700 dark:text-cyan-400", bgSoft: "bg-cyan-500/10", border: "border-cyan-500/40 dark:border-cyan-500/30", spine: "bg-cyan-500", borderStrong: "border-cyan-500" },
  violet: { text: "text-violet-700 dark:text-violet-400", bgSoft: "bg-violet-500/10", border: "border-violet-500/40 dark:border-violet-500/30", spine: "bg-violet-500", borderStrong: "border-violet-500" },
  fuchsia: { text: "text-fuchsia-700 dark:text-fuchsia-400", bgSoft: "bg-fuchsia-500/10", border: "border-fuchsia-500/40 dark:border-fuchsia-500/30", spine: "bg-fuchsia-500", borderStrong: "border-fuchsia-500" },
  amber: { text: "text-amber-700 dark:text-amber-400", bgSoft: "bg-amber-500/10", border: "border-amber-500/40 dark:border-amber-500/30", spine: "bg-amber-500", borderStrong: "border-amber-500" },
  emerald: { text: "text-emerald-700 dark:text-emerald-400", bgSoft: "bg-emerald-500/10", border: "border-emerald-500/40 dark:border-emerald-500/30", spine: "bg-emerald-500", borderStrong: "border-emerald-500" },
  blue: { text: "text-blue-700 dark:text-blue-400", bgSoft: "bg-blue-500/10", border: "border-blue-500/40 dark:border-blue-500/30", spine: "bg-blue-500", borderStrong: "border-blue-500" },
  orange: { text: "text-orange-700 dark:text-orange-400", bgSoft: "bg-orange-500/10", border: "border-orange-500/40 dark:border-orange-500/30", spine: "bg-orange-500", borderStrong: "border-orange-500" },
  rose: { text: "text-rose-700 dark:text-rose-400", bgSoft: "bg-rose-500/10", border: "border-rose-500/40 dark:border-rose-500/30", spine: "bg-rose-500", borderStrong: "border-rose-500" },
  lime: { text: "text-lime-700 dark:text-lime-400", bgSoft: "bg-lime-500/10", border: "border-lime-500/40 dark:border-lime-500/30", spine: "bg-lime-500", borderStrong: "border-lime-500" }
}

export const PASS_TEXT = "text-emerald-700 dark:text-emerald-400"
export const FAIL_TEXT = "text-rose-700 dark:text-rose-400"
export const WARN_TEXT = "text-amber-700 dark:text-amber-400"

export const PASS_PANEL =
  "border-emerald-500/40 bg-emerald-500/10 text-emerald-800 dark:border-emerald-500/30 dark:text-emerald-300"
export const FAIL_PANEL =
  "border-rose-500/40 bg-rose-500/10 text-rose-800 dark:border-rose-500/30 dark:text-rose-300"
export const WARN_PANEL =
  "border-amber-500/40 bg-amber-500/10 text-amber-800 dark:border-amber-500/30 dark:text-amber-300"
