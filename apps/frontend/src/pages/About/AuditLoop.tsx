import { CornerLeftUp } from "lucide-react"
import { cn } from "@/lib/utils"
import { IMPROVEMENT_LOOP } from "./content"

/**
 * Vertical stepper rather than a circle: this renders inside the ~400px
 * inspector, where a radial layout would either clip or shrink the labels
 * past readability.
 */
export function AuditLoop() {
  return (
    <div className="rounded-xl border border-border bg-background/50 p-4">
      <div className="mb-3 text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
        The loop
      </div>

      <ol className="relative">
        <span
          aria-hidden
          className="absolute bottom-3 left-[9px] top-3 w-px bg-gradient-to-b from-lime-500/60 via-border to-lime-500/60"
        />
        {IMPROVEMENT_LOOP.map((node, i) => (
          <li key={node.label} className="relative flex gap-3 pb-3 last:pb-0">
            <span
              className={cn(
                "relative z-10 mt-0.5 flex h-[19px] w-[19px] shrink-0 items-center justify-center rounded-full border bg-card font-mono text-[9px] tabular-nums",
                i === 0
                  ? "border-lime-500/60 text-lime-700 dark:text-lime-400"
                  : "border-border text-muted-foreground"
              )}
            >
              {i + 1}
            </span>
            <div className="min-w-0 pt-px">
              <div className="text-[12px] font-semibold leading-tight text-foreground">
                {node.label}
              </div>
              <p className="mt-0.5 text-[11.5px] leading-snug text-muted-foreground">{node.blurb}</p>
            </div>
          </li>
        ))}
      </ol>

      <div className="mt-2 flex items-center gap-1.5 pl-[3px] text-[11px] text-lime-700 dark:text-lime-400">
        <CornerLeftUp className="h-3.5 w-3.5" />
        Returns to production, and the loop runs again.
      </div>
    </div>
  )
}
