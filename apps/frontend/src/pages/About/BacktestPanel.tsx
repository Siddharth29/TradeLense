import { ArrowUpRight, TrendingDown, TrendingUp } from "lucide-react"
import { Link } from "react-router-dom"
import { cn } from "@/lib/utils"
import { BACKTEST_METRICS } from "./content"
import { FAIL_TEXT, PASS_TEXT } from "./ui"

export function BacktestPanel() {
  return (
    <div className="rounded-xl border border-border bg-background/50 p-4">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <div className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
          Baseline vs. candidate — illustrative example
        </div>
        <Link
          to="/backtests"
          className="inline-flex items-center gap-1 text-[11px] font-medium text-primary hover:underline"
        >
          See real backtest runs
          <ArrowUpRight className="h-3 w-3" />
        </Link>
      </div>

      <div className="overflow-x-auto">
        <table className="w-full min-w-[380px] border-collapse text-xs">
          <thead>
            <tr className="text-left text-[10px] uppercase tracking-wide text-muted-foreground">
              <th className="pb-1.5 pr-3 font-semibold">Metric</th>
              <th className="pb-1.5 pr-3 font-semibold">Baseline</th>
              <th className="pb-1.5 pr-3 font-semibold">Candidate</th>
              <th className="pb-1.5 font-semibold">Δ</th>
            </tr>
          </thead>
          <tbody>
            {BACKTEST_METRICS.map((m) => (
              <tr key={m.label} className="border-t border-border/60">
                <td className="py-1.5 pr-3 text-muted-foreground">{m.label}</td>
                <td className="py-1.5 pr-3 font-mono text-foreground/70">{m.baseline}</td>
                <td className="py-1.5 pr-3 font-mono font-semibold text-foreground">{m.candidate}</td>
                <td className="py-1.5">
                  <span
                    className={cn(
                      "inline-flex items-center gap-1",
                      m.improved ? PASS_TEXT : FAIL_TEXT
                    )}
                  >
                    {m.improved ? (
                      <TrendingUp className="h-3.5 w-3.5" />
                    ) : (
                      <TrendingDown className="h-3.5 w-3.5" />
                    )}
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <p className="mt-3 text-[11px] text-muted-foreground">
        Figures above illustrate the comparison mechanism only. A candidate has to win on the same historical
        data as its predecessor — not just post one good number — before it's promoted.
      </p>
    </div>
  )
}
