import { useMemo, useRef } from "react"
import { useParams, Link } from "react-router-dom"
import { toast } from "sonner"
import { Card } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { Badge } from "@/components/ui/badge"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import {
  useBacktestDetails,
  useBacktestIterations,
  useStopBacktest,
  useRestartBacktest,
} from "@/services/hooks"
import { BacktestDetailsSkeleton } from "@/components/BacktestDetailsSkeleton"
import { BacktestIterationsSkeleton } from "@/components/BacktestIterationsSkeleton"

// ─── Types ────────────────────────────────────────────────────────────────────

import type { Iteration } from "@/types/backtest"

// ─── Helpers ──────────────────────────────────────────────────────────────────

function fmtDate(dateStr: string | null | undefined): string {
  if (!dateStr) return "—"
  try { return new Date(dateStr).toLocaleString() } catch { return dateStr }
}

function fmtShortDate(dateStr: string | null | undefined): string {
  if (!dateStr) return "—"
  try {
    return new Date(dateStr).toLocaleString(undefined, {
      month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
    })
  } catch { return dateStr }
}

function fmtPnL(pnl: number | null | undefined): string {
  if (pnl == null || isNaN(pnl)) return "0.00"
  const s = Math.abs(pnl).toFixed(2)
  return pnl >= 0 ? `+${s}` : `-${s}`
}

function fmtPrice(v: number | null | undefined, decimals = 5): string {
  if (v == null) return "—"
  return v.toFixed(decimals)
}

function getStatusColor(status: string): string {
  switch (status) {
    case "completed": return "bg-green-500"
    case "running":   return "bg-blue-500"
    case "failed":    return "bg-red-500"
    case "pending":   return "bg-yellow-500"
    default:          return "bg-gray-500"
  }
}

function getResultBadgeClass(result: string | null | undefined): string {
  switch ((result ?? "").toUpperCase()) {
    case "TP":
    case "WIN":      return "bg-green-600 text-white"
    case "SL":
    case "LOSS":     return "bg-red-600 text-white"
    case "BE":       return "bg-yellow-600 text-white"
    case "TIMEOUT":  return "bg-slate-500 text-white"
    case "FILTERED": return "bg-amber-500 text-white"
    case "OPEN":     return "bg-blue-500 text-white"
    case "NO SIGNAL":
    case "NO ENTRY": return "bg-gray-400 text-white"
    default:         return "bg-gray-300 text-gray-800"
  }
}

// ─── Iteration display extractor ─────────────────────────────────────────────

function extractIterationDisplay(iteration: Iteration) {
  const iterNum   = iteration.iteration ?? iteration.iteration_number ?? 0
  const timestamp = iteration.iteration_timestamp ?? iteration.timestamp ?? ""
  const pred      = iteration.prediction ?? {}
  const outcome   = iteration.trade_outcome ?? {}

  // Canonical field names. Prefer trade_outcome (actual fill) over prediction (intended).
  const direction  = outcome.direction  ?? pred.direction  ?? null
  const entry      = outcome.entry      ?? pred.entry      ?? null
  const stop       = outcome.stop       ?? outcome.initial_stop ?? pred.stop   ?? null
  const target     = outcome.target     ?? pred.target     ?? null
  const exitPrice  = outcome.exit_price ?? null
  const riskReward = outcome.risk_reward ?? null
  const entryTime  = outcome.entry_time ?? outcome.signal_time ?? null
  const exitTime   = outcome.exit_time  ?? outcome.trade_closed_at ?? null

  // Duration
  let durationMins: number | null = null
  if (entryTime && exitTime) {
    try {
      const ms = new Date(exitTime).getTime() - new Date(entryTime).getTime()
      if (ms > 0) durationMins = Math.round(ms / 60000)
    } catch { /* ignore */ }
  }

  const predStatus = pred.status ?? ""
  const isFlat     = predStatus === "flat" || predStatus === "monitoring" || direction === null
  const isFiltered = predStatus === "rejected_guard" || predStatus === "rejected_levels"
  const isSkipped  = iteration.status === "skipped"
  const isError    = iteration.status === "error"
  const triggered  = entryTime != null || entry != null

  let tradeResult = outcome.result ?? ""
  if (!tradeResult) {
    if (isFiltered)      tradeResult = "Filtered"
    else if (isFlat)     tradeResult = "No Signal"
    else if (isSkipped)  tradeResult = "Skipped"
    else if (isError)    tradeResult = "Error"
    else if (triggered)  tradeResult = "Pending"
    else                 tradeResult = "No Entry"
  }

  let pnl = 0
  if (outcome.profit != null && tradeResult !== "Open") {
    pnl = outcome.profit
  } else if (tradeResult === "Open" && entry != null && exitPrice != null && direction) {
    pnl = direction === "long" ? exitPrice - entry : entry - exitPrice
  }

  const pnlPct        = entry && entry > 0 ? (pnl / entry) * 100 : 0
  const guardRejection = pred.guard_rejection ?? null
  const pattern        = pred.pattern ?? pred.status ?? "—"
  const confidence     = pred.confidence

  return {
    iterNum, timestamp, direction, entry, stop, target,
    exitPrice, riskReward, entryTime, exitTime, durationMins,
    tradeResult, pnl, pnlPct,
    triggered, isFlat, isFiltered, isSkipped, isError,
    guardRejection, pattern, confidence,
    brokerCost: outcome.broker_cost,
    aiCost: outcome.ai_cost ?? pred.ai_cost ?? iteration.ai_cost,
    tradeReason: outcome.trade_reason ?? pred.trade_reason ?? iteration.trade_reason,
    triggerReason: outcome.trigger_reason ?? pred.trigger_reason,
    status: iteration.status, error: iteration.error,
  }
}

// ─── Component ───────────────────────────────────────────────────────────────

export default function BacktestDetails() {
  const { backtestId } = useParams<{ backtestId: string }>()

  const {
    data: backtest,
    isLoading: isLoadingDetails,
    isFetching: isFetchingDetails,
    error: detailsError,
    refetch: refetchDetails,
  } = useBacktestDetails(backtestId)

  const shouldPoll = useMemo(
    () => backtest?.status === "running" || backtest?.status === "pending",
    [backtest?.status]
  )

  const {
    data: iterations,
    isLoading: isLoadingIterations,
    isFetching: isFetchingIterations,
    error: iterationsError,
    refetch: refetchIterations,
  } = useBacktestIterations(backtestId, 500, {
    refetchInterval: shouldPoll ? 3000 : false,
  })

  const stopMutation    = useStopBacktest()
  const restartMutation = useRestartBacktest()

  // Running cumulative — reset on each render (must be declared before any early returns)
  const cumRef = useRef(0)

  const handleRefresh = () => { refetchDetails(); refetchIterations() }

  const handleStop = async () => {
    if (!confirm("Stop this backtest?")) return
    try {
      await stopMutation.mutateAsync(backtestId!)
      toast.success("Backtest stopped")
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Failed to stop")
    }
  }

  const handleRestart = async () => {
    if (!confirm("Restart this backtest?")) return
    try {
      await restartMutation.mutateAsync(backtestId!)
      toast.success("Backtest restarted")
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Failed to restart")
    }
  }

  // ── Analytics ────────────────────────────────────────────────────────────
  const analytics = useMemo(() => {
    if (!iterations?.length) return null
    const all = iterations.map(extractIterationDisplay)

    const trades = all.filter(
      d => d.triggered && !d.isFlat && !d.isFiltered && !d.isSkipped && !d.isError
    )
    const closedTrades = trades.filter(d =>
      ["TP", "SL", "BE", "Trail", "Timeout"].includes(d.tradeResult)
    )
    const openTrades = trades.filter(d => d.tradeResult === "Open")
    const wins       = closedTrades.filter(d => d.pnl > 0)
    const losses     = closedTrades.filter(d => d.pnl < 0)
    const filtered   = all.filter(d => d.isFiltered)
    const skipped    = all.filter(d => d.isSkipped || d.isError)
    const flat       = all.filter(d => d.isFlat && !d.isFiltered && !d.isSkipped)

    const realizedPnl = closedTrades.reduce((s, d) => s + (d.pnl || 0), 0)
    const avgPnl      = closedTrades.length > 0 ? realizedPnl / closedTrades.length : 0

    const withDur  = closedTrades.filter(d => d.durationMins != null)
    const avgDuration = withDur.length > 0
      ? withDur.reduce((s, d) => s + (d.durationMins ?? 0), 0) / withDur.length
      : null

    const withRR = closedTrades.filter(d => d.riskReward != null)
    const avgRR  = withRR.length > 0
      ? withRR.reduce((s, d) => s + (d.riskReward ?? 0), 0) / withRR.length
      : null

    return { trades, closedTrades, openTrades, wins, losses, filtered, skipped, flat, realizedPnl, avgPnl, avgDuration, avgRR }
  }, [iterations])

  // ── CSV export ───────────────────────────────────────────────────────────
  const handleExportCSV = async () => {
    if (!backtest) { toast.error("No backtest data"); return }
    try {
      const { unparse } = await import("papaparse")
      const summary = [{
        "Backtest ID": backtest.backtest_id,
        "Test Name":   backtest.test_name,
        "Asset":       backtest.asset,
        "Start Date":  backtest.start_date,
        "End Date":    backtest.end_date,
        "Status":      backtest.status,
        "Iterations":  backtest.iterations,
        "Total Signals": backtest.total_signals,
        "Wins":        backtest.wins ?? 0,
        "Losses":      backtest.losses ?? 0,
        "Total P&L":   backtest.total_pnl ?? 0,
        "Max Drawdown": backtest.max_drawdown ?? 0,
        "Win Rate":    backtest.win_rate != null ? `${(backtest.win_rate * 100).toFixed(2)}%` : "0.00%",
        "Avg R:R":     backtest.avg_rr ?? 0,
        "Created At":  backtest.created_at,
        "Completed At": backtest.completed_at ?? "",
        "Error":       backtest.error_message ?? "",
      }]

      let cum = 0
      const rows = (iterations ?? []).map(it => {
        const d = extractIterationDisplay(it)
        cum += d.pnl
        return {
          "#":            d.iterNum,
          "Signal Time":  d.timestamp,
          "Direction":    d.direction ?? "—",
          "Pattern":      d.pattern,
          "Confidence":   d.confidence ?? "—",
          "Entry":        d.entry   != null ? d.entry.toFixed(5)   : "—",
          "Stop":         d.stop    != null ? d.stop.toFixed(5)    : "—",
          "Target":       d.target  != null ? d.target.toFixed(5)  : "—",
          "Entry Time":   d.entryTime ?? "—",
          "Exit Time":    d.exitTime  ?? "—",
          "Duration (m)": d.durationMins ?? "—",
          "Exit Price":   d.exitPrice != null ? d.exitPrice.toFixed(5) : "—",
          "R:R":          d.riskReward != null ? d.riskReward.toFixed(2) : "—",
          "Result":       d.tradeResult,
          "P&L":          d.pnl.toFixed(5),
          "Broker Charges (simulated USD)": d.brokerCost?.total ?? "",
          "AI Cost (USD)": d.aiCost?.total_usd ?? "",
          "AI Cost Status": d.aiCost?.status ?? "unavailable",
          "Trade Reason": d.tradeReason ?? "",
          "Entry Trigger": d.triggerReason ?? "",
          "Cum P&L":      cum.toFixed(5),
          "Guard Reject": d.guardRejection ?? "—",
          "Iter Status":  d.status ?? "—",
          "Error":        d.error ?? "",
        }
      })

      const csv  = unparse([...summary, {}, { "=== Iterations ===": "" }, ...rows])
      const blob = new Blob([csv], { type: "text/csv;charset=utf-8;" })
      const url  = URL.createObjectURL(blob)
      const a    = document.createElement("a")
      a.href     = url
      a.download = `backtest_${backtest.test_name}_${new Date().toISOString().slice(0, 10)}.csv`
      a.click()
      URL.revokeObjectURL(url)
      toast.success("CSV exported")
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Export failed")
    }
  }

  // ── Loading / Error ───────────────────────────────────────────────────────
  if (isLoadingDetails) return <BacktestDetailsSkeleton />

  if (detailsError || !backtest) {
    return (
      <div className="container mx-auto p-4">
        <Card className="p-4">
          <p className="text-red-500">
            {detailsError?.message ?? "Backtest not found"}
          </p>
          <Link to="/backtests">
            <Button className="mt-4">← Back to List</Button>
          </Link>
        </Card>
      </div>
    )
  }

  const isRefreshing = isFetchingDetails || isFetchingIterations

  // Reset cumulative counter each render
  cumRef.current = 0

  // ── Render ────────────────────────────────────────────────────────────────
  return (
    <div className="container mx-auto p-4 space-y-6">

      {/* Header */}
      <div className="flex flex-wrap justify-between items-start gap-4">
        <div>
          <h1 className="text-3xl font-bold">{backtest.test_name}</h1>
          <p className="text-muted-foreground text-sm">
            {backtest.asset} &nbsp;|&nbsp; {fmtDate(backtest.start_date)} – {fmtDate(backtest.end_date)}
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button variant="outline" onClick={handleRefresh} disabled={isRefreshing}>
            {isRefreshing ? "Refreshing…" : "🔄 Refresh"}
          </Button>
          <Button
            variant="outline"
            onClick={handleExportCSV}
            disabled={isLoadingDetails || isLoadingIterations}
          >
            📥 Export CSV
          </Button>
          {(backtest.status === "running" || backtest.status === "pending") && (
            <Button
              variant="outline"
              className="text-orange-600 hover:text-orange-700"
              onClick={handleStop}
              disabled={stopMutation.isPending}
            >
              {stopMutation.isPending ? "Stopping…" : "⏹ Stop"}
            </Button>
          )}
          {(backtest.status === "failed" || backtest.status === "completed") && (
            <Button
              variant="outline"
              className="text-blue-600 hover:text-blue-700"
              onClick={handleRestart}
              disabled={restartMutation.isPending}
            >
              {restartMutation.isPending ? "Restarting…" : "🔁 Restart"}
            </Button>
          )}
          <Link to="/backtests">
            <Button variant="outline">← Back</Button>
          </Link>
        </div>
      </div>

      {/* Status bar */}
      <Card className="p-4">
        <div className="flex flex-wrap items-center gap-4">
          <span className="font-medium">Status:</span>
          <Badge className={getStatusColor(backtest.status)}>{backtest.status}</Badge>
          {shouldPoll && (
            <Badge variant="outline" className="bg-blue-100 text-blue-800">
              🔴 Live – auto-refreshing every 3 s
            </Badge>
          )}
          {backtest.error_message && (
            <span className="text-red-500 text-sm">{backtest.error_message}</span>
          )}
        </div>
      </Card>

      {/* Summary row 1 */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <Card className="p-4">
          <p className="text-sm text-muted-foreground">Iterations</p>
          <p className="text-2xl font-bold">{backtest.iterations}</p>
        </Card>
        <Card className="p-4">
          <p className="text-sm text-muted-foreground">Total Signals</p>
          <p className="text-2xl font-bold">{backtest.total_signals}</p>
        </Card>
        <Card className="p-4">
          <p className="text-sm text-muted-foreground">Win Rate</p>
          <p className="text-2xl font-bold">
            {backtest.win_rate != null && !isNaN(backtest.win_rate)
              ? `${(backtest.win_rate * 100).toFixed(1)}%`
              : "0.0%"}
          </p>
        </Card>
        <Card className="p-4">
          <p className="text-sm text-muted-foreground">Total P&amp;L</p>
          <p className={`text-2xl font-bold font-mono ${(backtest.total_pnl ?? 0) >= 0 ? "text-green-600" : "text-red-600"}`}>
            {fmtPnL(backtest.total_pnl)}
          </p>
        </Card>
      </div>

      {/* Summary row 2 */}
      <div className="grid grid-cols-2 md:grid-cols-5 gap-4">
        <Card className="p-4">
          <p className="text-sm text-muted-foreground">Wins</p>
          <p className="text-xl font-semibold text-green-600">{backtest.wins}</p>
        </Card>
        <Card className="p-4">
          <p className="text-sm text-muted-foreground">Losses</p>
          <p className="text-xl font-semibold text-red-600">{backtest.losses}</p>
        </Card>
        <Card className="p-4">
          <p className="text-sm text-muted-foreground">Max Drawdown</p>
          <p className="text-xl font-semibold text-red-600">
            {backtest.max_drawdown != null ? backtest.max_drawdown.toFixed(2) : "0.00"}
          </p>
        </Card>
        <Card className="p-4">
          <p className="text-sm text-muted-foreground">Avg R:R</p>
          <p className="text-xl font-semibold">
            {backtest.avg_rr != null && !isNaN(backtest.avg_rr)
              ? backtest.avg_rr.toFixed(2)
              : "—"}
          </p>
        </Card>
        {analytics && (
          <Card className="p-4">
            <p className="text-sm text-muted-foreground">Filtered / Skipped</p>
            <p className="text-xl font-semibold text-amber-600">
              {analytics.filtered.length} / {analytics.skipped.length}
            </p>
          </Card>
        )}
      </div>

      {/* Iterations table */}
      <Card>
        <div className="p-4 border-b flex items-center justify-between">
          <h2 className="text-xl font-semibold">
            Iterations ({iterations?.length ?? 0})
          </h2>
          <p className="text-xs text-muted-foreground hidden md:block">
            Fields from trade_outcome (canonical: entry / stop / target / risk_reward)
          </p>
        </div>

        {isLoadingIterations ? (
          <div className="p-4"><BacktestIterationsSkeleton rows={5} /></div>
        ) : iterationsError ? (
          <div className="p-8 text-center text-red-500">
            Error loading iterations: {iterationsError.message}
          </div>
        ) : !iterations?.length ? (
          <div className="p-8 text-center text-muted-foreground">No iterations yet</div>
        ) : (
          <div className="overflow-x-auto">
            <Table>
              <TableHeader>
                <TableRow className="text-xs">
                  <TableHead className="w-8">#</TableHead>
                  <TableHead>Signal Time</TableHead>
                  <TableHead>Dir</TableHead>
                  <TableHead>Pattern</TableHead>
                  <TableHead className="text-right">Conf</TableHead>
                  <TableHead className="text-right">Entry</TableHead>
                  <TableHead className="text-right">Stop</TableHead>
                  <TableHead className="text-right">Target</TableHead>
                  <TableHead>Entry Time</TableHead>
                  <TableHead>Exit Time</TableHead>
                  <TableHead className="text-right">Exit Px</TableHead>
                  <TableHead className="text-right">R:R</TableHead>
                  <TableHead className="text-right">Dur</TableHead>
                  <TableHead>Result</TableHead>
                  <TableHead className="text-right">P&amp;L</TableHead>
                  <TableHead className="text-right">Charges (sim USD)</TableHead>
                  <TableHead className="text-right">AI cost (USD)</TableHead>
                  <TableHead>Why this trade?</TableHead>
                  <TableHead className="text-right">Cum P&amp;L</TableHead>
                  <TableHead>Notes</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {iterations.map((iteration, idx) => {
                  const d = extractIterationDisplay(iteration)
                  cumRef.current += d.pnl
                  const cumNow = cumRef.current
                  const key = iteration._id ?? `iter-${idx}`

                  const rowClass =
                    d.isError    ? "bg-red-500/10"    :
                    d.isFiltered ? "bg-amber-500/10"  :
                    d.isSkipped  ? "bg-gray-500/10"   :
                    d.isFlat     ? "bg-slate-500/5"   : ""

                  return (
                    <TableRow key={key} className={rowClass}>
                      <TableCell className="text-xs font-medium">{d.iterNum}</TableCell>
                      <TableCell className="text-xs whitespace-nowrap">{fmtShortDate(d.timestamp)}</TableCell>
                      <TableCell>
                        {d.direction ? (
                          <Badge className={d.direction === "long" ? "bg-green-600" : "bg-red-600"}>
                            {d.direction === "long" ? "▲ L" : "▼ S"}
                          </Badge>
                        ) : (
                          <span className="text-muted-foreground text-xs">—</span>
                        )}
                      </TableCell>
                      <TableCell className="text-xs max-w-[80px] truncate" title={d.pattern}>
                        {d.pattern}
                      </TableCell>
                      <TableCell className="text-xs font-mono text-right">
                        {d.confidence != null ? String(d.confidence) : "—"}
                      </TableCell>
                      <TableCell className="font-mono text-xs text-right">
                        {fmtPrice(d.entry)}
                      </TableCell>
                      <TableCell className="font-mono text-xs text-right text-red-700">
                        {fmtPrice(d.stop)}
                      </TableCell>
                      <TableCell className="font-mono text-xs text-right text-green-700">
                        {fmtPrice(d.target)}
                      </TableCell>
                      <TableCell className="text-xs whitespace-nowrap">
                        {fmtShortDate(d.entryTime)}
                      </TableCell>
                      <TableCell className="text-xs whitespace-nowrap">
                        {fmtShortDate(d.exitTime)}
                      </TableCell>
                      <TableCell className="font-mono text-xs text-right">
                        {fmtPrice(d.exitPrice)}
                      </TableCell>
                      <TableCell className="font-mono text-xs text-right">
                        {d.riskReward != null ? d.riskReward.toFixed(2) : "—"}
                      </TableCell>
                      <TableCell className="text-xs text-right">
                        {d.durationMins != null ? `${d.durationMins}m` : "—"}
                      </TableCell>
                      <TableCell>
                        <Badge className={getResultBadgeClass(d.tradeResult)}>
                          {d.tradeResult}
                        </Badge>
                      </TableCell>
                      <TableCell
                        className={`text-right font-mono text-xs font-semibold ${
                          d.pnl > 0 ? "text-green-600" :
                          d.pnl < 0 ? "text-red-600" :
                          "text-muted-foreground"
                        }`}
                      >
                        {d.pnl !== 0 ? fmtPnL(d.pnl) : "—"}
                      </TableCell>
                      <TableCell className="text-right font-mono text-xs">
                        {d.brokerCost?.total != null ? d.brokerCost.total.toFixed(4) : '—'}
                      </TableCell>
                      <TableCell className="text-right font-mono text-xs" title="Actual API usage at saved rates for the generating decision; includes expert and judge">
                        {d.aiCost?.total_usd != null ? `$${d.aiCost.total_usd.toLocaleString('en-US', { minimumFractionDigits: 6, maximumFractionDigits: 12 })}` : 'Unavailable'}
                      </TableCell>
                      <TableCell className="text-xs min-w-[200px] max-w-[320px]">
                        {d.tradeReason || d.triggerReason ? <details>
                          <summary className="cursor-pointer line-clamp-2">{d.tradeReason || d.triggerReason}</summary>
                          <p className="mt-2 whitespace-pre-wrap">{d.tradeReason}</p>
                          {d.triggerReason && <p className="mt-2 whitespace-pre-wrap">Entry trigger: {d.triggerReason}</p>}
                        </details> : '—'}
                      </TableCell>
                      <TableCell
                        className={`text-right font-mono text-xs ${
                          cumNow >= 0 ? "text-green-600" : "text-red-600"
                        }`}
                      >
                        {fmtPnL(cumNow)}
                      </TableCell>
                      <TableCell className="text-xs max-w-[140px]">
                        {d.isError && d.error && (
                          <span className="text-red-600" title={d.error}>
                            ⚠ {d.error.substring(0, 50)}
                          </span>
                        )}
                        {d.isFiltered && d.guardRejection && (
                          <span className="text-amber-700" title={d.guardRejection}>
                            🛡 {d.guardRejection.substring(0, 50)}
                          </span>
                        )}
                        {d.isSkipped && iteration.error && (
                          <span className="text-gray-500" title={iteration.error}>
                            ⏭ {iteration.error.substring(0, 50)}
                          </span>
                        )}
                      </TableCell>
                    </TableRow>
                  )
                })}
              </TableBody>
            </Table>
          </div>
        )}
      </Card>

      {/* Trade Analytics */}
      {analytics && (
        <Card>
          <div className="p-4 border-b">
            <h2 className="text-xl font-semibold">Trade Analytics</h2>
          </div>
          <div className="p-4 grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-6">
            <div>
              <p className="text-sm text-muted-foreground">Total Trades</p>
              <p className="text-2xl font-bold">{analytics.trades.length}</p>
              <p className="text-xs text-muted-foreground">
                {analytics.closedTrades.length} closed · {analytics.openTrades.length} open
              </p>
            </div>
            <div>
              <p className="text-sm text-muted-foreground">Win Rate</p>
              <p className="text-2xl font-bold">
                {analytics.closedTrades.length > 0
                  ? `${((analytics.wins.length / analytics.closedTrades.length) * 100).toFixed(1)}%`
                  : "—"}
              </p>
              <p className="text-xs text-muted-foreground">
                {analytics.wins.length}W / {analytics.losses.length}L
              </p>
            </div>
            <div>
              <p className="text-sm text-muted-foreground">Realized P&amp;L</p>
              <p className={`text-2xl font-bold font-mono ${analytics.realizedPnl >= 0 ? "text-green-600" : "text-red-600"}`}>
                {fmtPnL(analytics.realizedPnl)}
              </p>
            </div>
            <div>
              <p className="text-sm text-muted-foreground">Avg P&amp;L / Trade</p>
              <p className={`text-xl font-semibold font-mono ${analytics.avgPnl >= 0 ? "text-green-600" : "text-red-600"}`}>
                {fmtPnL(analytics.avgPnl)}
              </p>
            </div>
            <div>
              <p className="text-sm text-muted-foreground">Avg R:R</p>
              <p className="text-xl font-semibold">
                {analytics.avgRR != null ? analytics.avgRR.toFixed(2) : "—"}
              </p>
            </div>
            <div>
              <p className="text-sm text-muted-foreground">Avg Duration</p>
              <p className="text-xl font-semibold">
                {analytics.avgDuration != null ? `${Math.round(analytics.avgDuration)}m` : "—"}
              </p>
            </div>
            <div>
              <p className="text-sm text-muted-foreground">No Signal / Flat</p>
              <p className="text-xl font-semibold text-slate-500">{analytics.flat.length}</p>
            </div>
            <div>
              <p className="text-sm text-muted-foreground">Guard Filtered</p>
              <p className="text-xl font-semibold text-amber-600">{analytics.filtered.length}</p>
            </div>
            <div>
              <p className="text-sm text-muted-foreground">Skipped / Error</p>
              <p className="text-xl font-semibold text-gray-500">{analytics.skipped.length}</p>
            </div>
          </div>
        </Card>
      )}
    </div>
  )
}
