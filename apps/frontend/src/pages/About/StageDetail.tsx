import * as React from "react"
import {
  Target,
  Database,
  Cog,
  ArrowRightCircle,
  ShieldAlert,
  OctagonX,
  Ban
} from "lucide-react"
import { cn } from "@/lib/utils"
import { Tooltip, TooltipTrigger, TooltipContent } from "@/components/ui/tooltip"
import type { InfoBlock, Stage } from "./content"
import { ACCENT_STYLES } from "./ui"
import { RiskGateDemo } from "./RiskGateDemo"
import { ReconciliationDemo } from "./ReconciliationDemo"
import { AuditLoop } from "./AuditLoop"
import { BacktestPanel } from "./BacktestPanel"

const FIELDS: { key: keyof InfoBlock; label: string; icon: React.ElementType }[] = [
  { key: "purpose", label: "Purpose", icon: Target },
  { key: "input", label: "Input", icon: Database },
  { key: "processing", label: "Processing", icon: Cog },
  { key: "output", label: "Output", icon: ArrowRightCircle },
  { key: "safety", label: "Safety", icon: ShieldAlert },
  { key: "failure", label: "On failure", icon: OctagonX }
]

function SectionLabel({ children }: { children: React.ReactNode }) {
  return (
    <div className="mb-2 text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
      {children}
    </div>
  )
}

export function StageDetail({ stage }: { stage: Stage }) {
  const styles = ACCENT_STYLES[stage.accent]
  const Icon = stage.icon
  const hasDemo = stage.id === "risk" || stage.id === "reconciliation" || stage.id === "improvement"

  return (
    <div key={stage.id} className="about-fade-up">
      <div className="flex items-start gap-3 border-b border-border pb-4">
        <div
          className={cn(
            "flex h-11 w-11 shrink-0 items-center justify-center rounded-xl border",
            styles.bgSoft,
            styles.border
          )}
        >
          <Icon className={cn("h-5 w-5", styles.text)} />
        </div>
        <div className="min-w-0">
          <div className="flex items-baseline gap-2">
            <span className={cn("font-mono text-xs tabular-nums", styles.text)}>
              {String(stage.index).padStart(2, "0")}
            </span>
            <h2 className="text-lg font-semibold leading-tight text-foreground sm:text-xl">
              {stage.title}
            </h2>
          </div>
          <p className="mt-1 max-w-3xl text-sm leading-snug text-muted-foreground">
            {stage.tagline}
          </p>
        </div>
      </div>

      {/* Six-field contract, three across on wide screens now that this is full width. */}
      <div className="grid grid-cols-1 gap-x-8 gap-y-5 py-5 sm:grid-cols-2 lg:grid-cols-3">
        {FIELDS.map(({ key, label, icon: FieldIcon }) => (
          <div key={key}>
            <div className="mb-1 flex items-center gap-1.5">
              <FieldIcon className={cn("h-3.5 w-3.5 shrink-0", styles.text)} />
              <span className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
                {label}
              </span>
            </div>
            <p className="text-[13.5px] leading-relaxed text-foreground/90">{stage.info[key]}</p>
          </div>
        ))}
      </div>

      <div className={cn("grid grid-cols-1 gap-6 border-t border-border pt-5", hasDemo && "lg:grid-cols-2")}>
        <div>
          <SectionLabel>Components</SectionLabel>
          <div className="flex flex-wrap gap-1.5">
            {stage.subNodes.map((sub) => (
              <Tooltip key={sub.label} delayDuration={100}>
                <TooltipTrigger asChild>
                  <button
                    type="button"
                    className={cn(
                      "rounded-md border bg-card px-2.5 py-1 text-[11.5px] font-medium transition-colors hover:bg-accent/10",
                      styles.border,
                      styles.text
                    )}
                  >
                    {sub.label}
                  </button>
                </TooltipTrigger>
                <TooltipContent side="top" className="max-w-[280px] border bg-card text-card-foreground">
                  {sub.blurb}
                </TooltipContent>
              </Tooltip>
            ))}
          </div>

          <div className="mt-5 space-y-2.5 border-l-2 border-border pl-4">
            {stage.detail.map((p, i) => (
              <p key={i} className="max-w-2xl text-[13px] leading-relaxed text-muted-foreground">
                {p}
              </p>
            ))}
          </div>
        </div>

        {hasDemo && (
          <div className="space-y-4">
            {stage.id === "risk" && <RiskGateDemo />}
            {stage.id === "reconciliation" && <ReconciliationDemo />}
            {stage.id === "improvement" && (
              <>
                <AuditLoop />
                <BacktestPanel />
              </>
            )}
          </div>
        )}
      </div>
    </div>
  )
}

export function RejectedDetail() {
  return (
    <div className="about-fade-up">
      <div className="flex items-start gap-3 border-b border-border pb-4">
        <div className="flex h-11 w-11 shrink-0 items-center justify-center rounded-xl border border-rose-500/40 bg-rose-500/10">
          <Ban className="h-5 w-5 text-rose-700 dark:text-rose-400" />
        </div>
        <div>
          <h2 className="text-lg font-semibold leading-tight text-foreground sm:text-xl">Rejected</h2>
          <p className="mt-1 text-sm leading-snug text-muted-foreground">
            Where a proposal goes when it fails a gate.
          </p>
        </div>
      </div>

      <div className="grid grid-cols-1 gap-6 py-5 lg:grid-cols-3">
        <p className="text-[13.5px] leading-relaxed text-foreground/90">
          A rejected proposal is not discarded. It is written to the record with the gate that
          failed, the reason, the market context it came from, and the candle that produced it.
        </p>
        <p className="text-[13.5px] leading-relaxed text-muted-foreground">
          That record flows into the same daily audit as executed trades — which is what makes it
          possible to ask, later, whether a rejection was correct. A gate that rejects good trades is
          a defect, and it can only be found if the rejections were kept.
        </p>
        <div className="rounded-lg border border-rose-500/30 bg-rose-500/[0.06] p-3">
          <div className="text-[11px] font-medium uppercase tracking-wide text-rose-700 dark:text-rose-400">
            Most cycles end here
          </div>
          <p className="mt-1 text-[12.5px] leading-relaxed text-muted-foreground">
            Rejection is the common path, not the exception. The system is built to pass on trades
            far more often than it takes them.
          </p>
        </div>
      </div>
    </div>
  )
}
