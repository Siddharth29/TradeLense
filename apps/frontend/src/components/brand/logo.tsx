import { useId } from "react"
import { cn } from "@/lib/utils"

/**
 * TradeLense logo primitives used throughout the public application.
 *
 * Colors come from the --tl-* CSS variables in styles/globals.css, so the mark
 * and the wordmark follow the light/dark theme without swapping files.
 * Minimum sizes: full lockup 120px wide, mark 24px, icon 16px.
 */

const MARK_ASPECT = 200 / 110

type MarkVariant = "auto" | "mark" | "icon"

/** Below this height the soft candles turn to mush, so the brand kit says use the icon. */
const ICON_SWITCH_HEIGHT = 28

export type BrandMarkProps = {
  /** Rendered height in px. */
  size?: number
  /** "auto" (default) drops to the simplified icon at small sizes. */
  variant?: MarkVariant
  className?: string
}

/** The focus-frame mark: two soft candles, one sharp centre candle, camera frame. */
export function BrandMark({ size = 28, variant = "auto", className }: BrandMarkProps) {
  const rawId = useId()
  const blurId = `tl-blur-${rawId.replace(/:/g, "")}`
  const asIcon = variant === "icon" || (variant === "auto" && size < ICON_SWITCH_HEIGHT)

  if (asIcon) {
    return (
      <svg
        viewBox="0 0 110 110"
        width={size}
        height={size}
        className={cn("shrink-0", className)}
        role="presentation"
        aria-hidden="true"
        focusable="false"
      >
        <line
          x1="55"
          y1="12"
          x2="55"
          y2="98"
          stroke="var(--tl-emerald)"
          strokeWidth="8.4"
          strokeLinecap="round"
        />
        <rect x="44" y="30" width="22" height="46" rx="4.8" fill="var(--tl-emerald)" />
        <path
          d="M 27 8 h -12 v 12 M 83 8 h 12 v 12 M 27 102 h -12 v -12 M 83 102 h 12 v -12"
          fill="none"
          stroke="var(--tl-mark-frame)"
          strokeWidth="5"
          strokeLinecap="round"
        />
      </svg>
    )
  }

  return (
    <svg
      viewBox="0 0 200 110"
      width={Math.round(size * MARK_ASPECT)}
      height={size}
      className={cn("shrink-0", className)}
      role="presentation"
      aria-hidden="true"
      focusable="false"
    >
      <defs>
        <filter id={blurId} x="-40%" y="-40%" width="180%" height="180%">
          <feGaussianBlur stdDeviation="3.2" />
        </filter>
      </defs>
      {/* the noise: out-of-focus candles */}
      <g filter={`url(#${blurId})`} opacity="0.45">
        <line
          x1="40"
          y1="28"
          x2="40"
          y2="88"
          stroke="var(--tl-mark-soft)"
          strokeWidth="6.1"
          strokeLinecap="round"
        />
        <rect x="32" y="42" width="16" height="32" rx="3.5" fill="var(--tl-mark-soft)" />
        <line
          x1="160"
          y1="22"
          x2="160"
          y2="82"
          stroke="var(--tl-mark-soft)"
          strokeWidth="6.1"
          strokeLinecap="round"
        />
        <rect x="152" y="34" width="16" height="34" rx="3.5" fill="var(--tl-mark-soft)" />
      </g>
      {/* the one trade in focus */}
      <line
        x1="100"
        y1="12"
        x2="100"
        y2="98"
        stroke="var(--tl-emerald)"
        strokeWidth="8.4"
        strokeLinecap="round"
      />
      <rect x="89" y="30" width="22" height="46" rx="4.8" fill="var(--tl-emerald)" />
      <path
        d="M 72 8 h -12 v 12 M 128 8 h 12 v 12 M 72 102 h -12 v -12 M 128 102 h 12 v -12"
        fill="none"
        stroke="var(--tl-mark-frame)"
        strokeWidth="4.5"
        strokeLinecap="round"
      />
    </svg>
  )
}

export type WordmarkProps = {
  /** Font size in px. */
  size?: number
  className?: string
}

/** The wordmark: always one word, lowercase — "trade" muted, "lense" emerald. */
export function Wordmark({ size = 20, className }: WordmarkProps) {
  return (
    <span className={cn("tl-wordmark", className)} style={{ fontSize: size }}>
      trade<span className="tl-lens">lense</span>
    </span>
  )
}

export type LogoProps = {
  /** Height of the mark in px; the wordmark is scaled off it. */
  size?: number
  /** Mark only, no wordmark (tight spaces, collapsed rails). */
  markOnly?: boolean
  variant?: MarkVariant
  /** Override the wordmark size — use when the mark is boxed to an icon column. */
  wordmarkSize?: number
  className?: string
  /** Extra classes for the wordmark — e.g. to hide it responsively. */
  wordmarkClassName?: string
}

/** Full lockup: mark + wordmark, spoken as "TradeLense" by screen readers. */
export function Logo({
  size = 28,
  markOnly = false,
  variant = "auto",
  wordmarkSize,
  className,
  wordmarkClassName
}: LogoProps) {
  const wordSize = wordmarkSize ?? Math.round(size * 0.78)
  return (
    <span
      className={cn("inline-flex items-center", className)}
      style={{ gap: Math.round(size * 0.22) }}
      role="img"
      aria-label="TradeLense"
    >
      <BrandMark size={size} variant={variant} />
      {!markOnly && <Wordmark size={wordSize} className={wordmarkClassName} />}
    </span>
  )
}
