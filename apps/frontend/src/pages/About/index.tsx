import * as React from "react"
import { Link } from "react-router-dom"
import { ArrowLeft, Play, Square, MousePointerClick } from "lucide-react"
import { TooltipProvider } from "@/components/ui/tooltip"
import { ModeToggle } from "@/components/mode-toggle"
import { Logo } from "@/components/brand/logo"
import { STAGES, LIFECYCLE_SCENARIOS } from "./content"
import { FlowChart } from "./FlowChart"
import { TRACE_EDGES, TRACE_NODES, type ChartNodeId } from "./layout"
import { StageDetail, RejectedDetail } from "./StageDetail"
import "./about.css"

const STEP_MS = 900

type Scenario = "approved" | "rejected"

export default function About() {
  const [selectedId, setSelectedId] = React.useState<ChartNodeId>("data")
  const [scenario, setScenario] = React.useState<Scenario>("approved")
  const [tracing, setTracing] = React.useState(false)
  const [step, setStep] = React.useState(0)

  const nodePath = TRACE_NODES[scenario]
  const edgePath = TRACE_EDGES[scenario]
  const total = nodePath.length

  React.useEffect(() => {
    if (!tracing) return
    if (step >= total) {
      const end = window.setTimeout(() => setTracing(false), 1400)
      return () => window.clearTimeout(end)
    }
    const t = window.setTimeout(() => setStep((s) => s + 1), STEP_MS)
    return () => window.clearTimeout(t)
  }, [tracing, step, total])

  // Stable identities: FlowChart takes these as props and they feed comparisons
  // on every render.
  const activeNodes = React.useMemo(
    () => (tracing ? nodePath.slice(0, step) : []),
    [tracing, nodePath, step]
  )
  const activeEdges = React.useMemo(
    () => (tracing ? edgePath.slice(0, Math.max(0, step - 1)) : []),
    [tracing, edgePath, step]
  )

  const runTrace = (next: Scenario) => {
    setScenario(next)
    setStep(0)
    setTracing(true)
  }

  const selectedStage = STAGES.find((s) => s.id === selectedId)
  const currentTraceStage = tracing && step > 0 ? nodePath[Math.min(step, total) - 1] : null
  const traceLabel = currentTraceStage
    ? (STAGES.find((s) => s.id === currentTraceStage)?.title ?? "Rejected")
    : null

  return (
    <TooltipProvider>
      <div className="min-h-screen bg-background text-foreground">
        <header className="sticky top-0 z-30 border-b border-border bg-background/90 backdrop-blur">
          <div className="mx-auto flex max-w-[1500px] items-center gap-4 px-4 py-3 sm:px-6">
            <Link
              to="/dashboard"
              className="inline-flex items-center gap-1.5 text-sm text-muted-foreground transition-colors hover:text-foreground"
            >
              <ArrowLeft className="h-4 w-4" />
              <span className="hidden sm:inline">Back</span>
            </Link>
            <div className="h-4 w-px bg-border" />
            <div className="flex items-center gap-3">
              <Logo size={28} />
              <span className="hidden text-[11px] uppercase tracking-[0.16em] text-muted-foreground sm:inline">
                AI-based trading system
              </span>
            </div>
            <div className="ml-auto">
              {/* This route renders outside the app layout, so it carries its own toggle. */}
              <ModeToggle />
            </div>
          </div>
        </header>

        <main className="mx-auto max-w-[1500px] px-4 pb-16 sm:px-6">
          {/* The chart is the page: no title block competing with it for the fold. */}
          <section className="pt-6 sm:pt-8">
            <h1 className="sr-only">TradeLense trade lifecycle</h1>

            <div className="mb-3 flex flex-wrap items-center justify-end gap-3">
              <div className="flex items-center gap-1.5">
                {tracing ? (
                  <>
                    <span className="mr-1 font-mono text-[11px] tabular-nums text-muted-foreground">
                      {String(Math.min(step, total)).padStart(2, "0")}/{total} ·{" "}
                      {LIFECYCLE_SCENARIOS.find((s) => s.id === scenario)?.asset}
                      {traceLabel ? ` · ${traceLabel}` : ""}
                    </span>
                    <button
                      type="button"
                      onClick={() => setTracing(false)}
                      className="inline-flex items-center gap-1.5 rounded-md border border-border px-2.5 py-1 text-xs font-medium text-muted-foreground transition-colors hover:text-foreground"
                    >
                      <Square className="h-3 w-3" /> Stop
                    </button>
                  </>
                ) : (
                  <>
                    <span className="mr-1 text-[11px] uppercase tracking-wide text-muted-foreground">
                      Trace
                    </span>
                    {(["approved", "rejected"] as Scenario[]).map((s) => (
                      <button
                        key={s}
                        type="button"
                        onClick={() => runTrace(s)}
                        className="inline-flex items-center gap-1.5 rounded-md border border-border px-2.5 py-1 text-xs font-medium text-muted-foreground transition-colors hover:border-foreground/30 hover:text-foreground"
                      >
                        <Play className="h-3 w-3" />
                        {s === "approved" ? "Order placed" : "Gate rejects"}
                      </button>
                    ))}
                  </>
                )}
              </div>
            </div>

            <div className="about-chart rounded-xl border border-border bg-card/30 p-4 sm:p-6">
              <FlowChart
                selectedId={selectedId}
                onSelect={setSelectedId}
                activeNodes={activeNodes}
                activeEdges={activeEdges}
                tracing={tracing}
              />
            </div>

            <p className="mt-2.5 flex items-center gap-1.5 text-[11.5px] text-muted-foreground">
              <MousePointerClick className="h-3.5 w-3.5" />
              Select any box to read what it does, what it receives, and how it fails.
            </p>
          </section>

          {/* Detail for the selected box — below the chart, full width */}
          <section className="mt-8 rounded-xl border border-border bg-card/30 p-5 sm:p-7">
            {selectedStage ? <StageDetail stage={selectedStage} /> : <RejectedDetail />}
          </section>

          <footer className="mt-10 flex flex-wrap items-center gap-3 text-sm">
            <Link to="/dashboard" className="text-primary hover:underline">
              Dashboard
            </Link>
            <span className="text-muted-foreground/40">·</span>
            <Link to="/backtests" className="text-primary hover:underline">
              Backtests
            </Link>
            <span className="text-muted-foreground/40">·</span>
            <Link to="/logs" className="text-primary hover:underline">
              Logs
            </Link>
          </footer>
        </main>
      </div>
    </TooltipProvider>
  )
}
