import * as React from "react"
import { CheckCircle2, XCircle, ChevronRight } from "lucide-react"
import { cn } from "@/lib/utils"
import { Button } from "@/components/ui/button"
import { RISK_GATE_SAMPLES } from "./content"
import { FAIL_TEXT, PASS_TEXT } from "./ui"

const HISTORY_LIMIT = 4
const AUTO_MS = 3200

export function RiskGateDemo() {
  const [step, setStep] = React.useState(0)
  const [auto, setAuto] = React.useState(true)

  React.useEffect(() => {
    if (!auto) return
    const t = setTimeout(() => setStep((s) => s + 1), AUTO_MS)
    return () => clearTimeout(t)
  }, [auto, step])

  const start = Math.max(0, step - HISTORY_LIMIT + 1)
  const visible = Array.from({ length: step - start + 1 }, (_, i) => start + i)

  return (
    <div className="rounded-xl border border-border bg-background/50 p-4">
      <div className="mb-3 flex items-center justify-between">
        <div className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
          Live gate verdicts — sample proposals
        </div>
        <Button
          type="button"
          variant="outline"
          size="sm"
          className="h-7 gap-1 text-xs"
          onClick={() => {
            setAuto(false)
            setStep((s) => s + 1)
          }}
        >
          Run another sample
          <ChevronRight className="h-3 w-3" />
        </Button>
      </div>

      <div className="space-y-1.5 font-mono text-xs">
        {visible.map((s) => {
          const sample = RISK_GATE_SAMPLES[s % RISK_GATE_SAMPLES.length]
          const isLast = s === step
          const pass = sample.verdict === "pass"
          return (
            <div
              key={s}
              className={cn(
                "flex items-start gap-2 rounded-md border px-2.5 py-1.5 transition-opacity",
                pass ? "border-emerald-500/25 bg-emerald-500/[0.06]" : "border-rose-500/25 bg-rose-500/[0.06]",
                isLast ? "opacity-100" : "opacity-45"
              )}
            >
              {pass ? (
                <CheckCircle2 className={cn("mt-0.5 h-3.5 w-3.5 shrink-0", PASS_TEXT)} />
              ) : (
                <XCircle className={cn("mt-0.5 h-3.5 w-3.5 shrink-0", FAIL_TEXT)} />
              )}
              <span>
                <span className={cn("font-semibold", pass ? PASS_TEXT : FAIL_TEXT)}>
                  {pass ? "PASS" : "REJECT"}
                </span>
                <span className="text-muted-foreground"> · {sample.gate} — {sample.reason}</span>
              </span>
            </div>
          )
        })}
      </div>

      <p className="mt-3 text-[11px] text-muted-foreground">
        Every verdict shown here — pass or reject — is written to the record with its gate and reason. Nothing is dropped silently.
      </p>
    </div>
  )
}
