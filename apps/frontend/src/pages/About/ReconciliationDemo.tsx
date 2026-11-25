import * as React from "react"
import { CheckCircle2, AlertTriangle } from "lucide-react"
import { cn } from "@/lib/utils"
import { RECONCILIATION_SCENARIOS } from "./content"
import { PASS_PANEL, PASS_TEXT, WARN_PANEL, WARN_TEXT } from "./ui"

export function ReconciliationDemo() {
  const [idx, setIdx] = React.useState(0)
  const scenario = RECONCILIATION_SCENARIOS[idx]
  const mismatch = scenario.status === "mismatch"

  return (
    <div className="rounded-xl border border-border bg-background/50 p-4">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <div className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
          System record vs. broker reality
        </div>
        <div className="flex flex-wrap gap-1.5">
          {RECONCILIATION_SCENARIOS.map((s, i) => (
            <button
              key={s.id}
              type="button"
              onClick={() => setIdx(i)}
              className={cn(
                "rounded-full border px-2.5 py-1 text-[11px] font-medium transition-colors",
                i === idx
                  ? s.status === "match"
                    ? cn("border-emerald-500/50 bg-emerald-500/10", PASS_TEXT)
                    : cn("border-amber-500/50 bg-amber-500/10", WARN_TEXT)
                  : "border-border text-muted-foreground hover:text-foreground"
              )}
            >
              {s.label}
            </button>
          ))}
        </div>
      </div>

      <div className="overflow-x-auto">
        <table className="w-full min-w-[420px] border-collapse text-xs">
          <thead>
            <tr className="text-left text-[10px] uppercase tracking-wide text-muted-foreground">
              <th className="pb-1.5 pr-3 font-semibold">Field</th>
              <th className="pb-1.5 pr-3 font-semibold">System expects</th>
              <th className="pb-1.5 pr-3 font-semibold">Broker reports</th>
              <th className="pb-1.5 font-semibold">Status</th>
            </tr>
          </thead>
          <tbody>
            {scenario.rows.map((row) => (
              <tr key={row.field} className="border-t border-border/60">
                <td className="py-1.5 pr-3 text-muted-foreground">{row.field}</td>
                <td className="py-1.5 pr-3 font-mono text-foreground">{row.expected}</td>
                <td
                  className={cn(
                    "py-1.5 pr-3 font-mono",
                    row.match ? "text-foreground" : cn("font-semibold", WARN_TEXT)
                  )}
                >
                  {row.broker}
                </td>
                <td className="py-1.5">
                  {row.match ? (
                    <CheckCircle2 className={cn("h-3.5 w-3.5", PASS_TEXT)} />
                  ) : (
                    <AlertTriangle className={cn("h-3.5 w-3.5", WARN_TEXT)} />
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div
        className={cn(
          "mt-3 flex items-center gap-2 rounded-md border px-3 py-2 text-xs",
          mismatch ? WARN_PANEL : PASS_PANEL
        )}
      >
        {mismatch ? (
          <>
            <AlertTriangle className="h-3.5 w-3.5 shrink-0" />
            <span>
              Flagged: <span className="font-semibold">{scenario.category}</span> — recorded and surfaced, not silently corrected.
            </span>
          </>
        ) : (
          <>
            <CheckCircle2 className="h-3.5 w-3.5 shrink-0" />
            <span>Every field reconciles. The system's record can be trusted for this trade.</span>
          </>
        )}
      </div>
    </div>
  )
}
