import React, { useState, useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Card } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import {
  Table,
  TableHeader,
  TableBody,
  TableHead,
  TableRow,
  TableCell,
} from '@/components/ui/table'
import { Loader2, RefreshCw, Activity, TrendingUp, TrendingDown, Trash2, AlertTriangle } from 'lucide-react'
import { toast } from 'sonner'
import {
  getTradingAnalysisTrades,
  getAvailableAssets,
  deleteTradingAnalysisTrade,
  deleteAllTradingAnalysisTrades,
} from '@/services/apiClient'
import type { TradingAnalysisTrade } from '@/services/apiClient'

// IST date (YYYY-MM-DD) → UTC ISO instant for the start of that IST day.
const istDateToUtcStart = (d: string): string | undefined =>
  d ? new Date(`${d}T00:00:00+05:30`).toISOString() : undefined

// IST date → UTC ISO instant for the *next* IST midnight (exclusive end bound).
const istDateToUtcEndExclusive = (d: string): string | undefined => {
  if (!d) return undefined
  const e = new Date(`${d}T00:00:00+05:30`)
  e.setDate(e.getDate() + 1)
  return e.toISOString()
}

// Human-readable elapsed time between entry and exit.
const fmtDuration = (start: string | null, end: string | null): string => {
  if (!start || !end) return '—'
  const s = new Date(start).getTime()
  const e = new Date(end).getTime()
  if (isNaN(s) || isNaN(e) || e < s) return '—'
  const totalSec = Math.floor((e - s) / 1000)
  const h = Math.floor(totalSec / 3600)
  const m = Math.floor((totalSec % 3600) / 60)
  if (h > 0) return `${h}h ${m}m`
  if (m > 0) return `${m}m`
  return `${totalSec}s`
}

// Lenient price/number formatter — trims trailing zeros, never throws.
const fmt = (n: number | null | undefined): string => {
  if (n === null || n === undefined || Number.isNaN(n)) return '—'
  return n.toLocaleString('en-US', { maximumFractionDigits: 5 })
}

// UTC → IST short timestamp for the "Opened" column.
const toIST = (timestamp: string | null): string => {
  if (!timestamp) return '—'
  const date = new Date(timestamp)
  if (isNaN(date.getTime())) return '—'
  return date.toLocaleString('en-IN', {
    timeZone: 'Asia/Kolkata',
    day: '2-digit',
    month: 'short',
    hour: '2-digit',
    minute: '2-digit',
    hour12: true,
  })
}

const isPartialTrade = (trade: TradingAnalysisTrade) =>
  Boolean(trade.parent_trade_id) || ['TP1_PARTIAL', 'TP2_PARTIAL'].includes(trade.exit_reason || '')

export default function UnifiedAnalysis() {
  const [selectedAsset, setSelectedAsset] = useState('ALL')
  const [selectedStatus, setSelectedStatus] = useState('ALL')
  const [selectedSide, setSelectedSide] = useState('ALL')
  const [startDate, setStartDate] = useState('')
  const [endDate, setEndDate] = useState('')
  const [limit, setLimit] = useState(50)

  const { data, isLoading, refetch, isRefetching } = useQuery({
    queryKey: ['trading-analysis', selectedAsset, selectedStatus, selectedSide, startDate, endDate, limit],
    queryFn: () =>
      getTradingAnalysisTrades(
        selectedAsset === 'ALL' ? undefined : selectedAsset,
        selectedStatus === 'ALL' ? undefined : selectedStatus,
        selectedSide === 'ALL' ? undefined : selectedSide,
        limit,
        istDateToUtcStart(startDate),
        istDateToUtcEndExclusive(endDate),
      ),
    staleTime: 15000,
    refetchOnWindowFocus: false,
  })

  const { data: assetsData } = useQuery({
    queryKey: ['available-assets'],
    queryFn: getAvailableAssets,
    staleTime: 300000,
  })

  const assets = useMemo(() => ['ALL', ...(assetsData?.assets || [])], [assetsData])
  const trades: TradingAnalysisTrade[] = data?.trades || []

  const stats = useMemo(() => {
    const positions = trades.filter((t) => !isPartialTrade(t))
    const total = positions.length
    const open = positions.filter((t) => t.trade_status === 'open').length
    const closedPositions = positions.filter((t) => t.trade_status === 'closed')
    const closed = closedPositions.length
    const wins = closedPositions.filter((t) => t.pnl !== null && t.pnl > 0).length
    const losses = closedPositions.filter((t) => t.pnl !== null && t.pnl < 0).length
    const totalPnl = trades.reduce((sum, t) => sum + (t.pnl_in_parent ? 0 : t.pnl || 0), 0)
    return { total, open, closed, wins, losses, totalPnl }
  }, [trades])

  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-64">
        <Loader2 className="h-8 w-8 animate-spin" />
        <span className="ml-2">Loading trades...</span>
      </div>
    )
  }

  return (
    <div className="space-y-3 min-h-0">
      {/* Header */}
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h1 className="text-xl font-bold">Trading Analysis</h1>
        <div className="flex items-center gap-1.5">
          <Button onClick={() => refetch()} variant="outline" size="sm" className="h-7 gap-1 px-2 text-xs shadow-sm" disabled={isRefetching}>
            <RefreshCw className={`h-3.5 w-3.5 ${isRefetching ? 'animate-spin' : ''}`} />
            Refresh
          </Button>
          <DeleteAllButton onDeleted={() => refetch()} />
        </div>
      </div>

      {/* Quick Stats */}
      <div className="grid grid-cols-2 md:grid-cols-6 gap-2">
        <Card className="px-3 py-2">
          <div className="text-xs text-muted-foreground">Total Trades</div>
          <div className="text-xl font-bold">{stats.total}</div>
        </Card>
        <Card className="px-3 py-2">
          <div className="text-xs text-muted-foreground">Open</div>
          <div className="text-xl font-bold text-blue-600">{stats.open}</div>
        </Card>
        <Card className="px-3 py-2">
          <div className="text-xs text-muted-foreground">Closed</div>
          <div className="text-xl font-bold">{stats.closed}</div>
        </Card>
        <Card className="px-3 py-2">
          <div className="text-xs text-muted-foreground">Wins</div>
          <div className="text-xl font-bold text-green-600">{stats.wins}</div>
        </Card>
        <Card className="px-3 py-2">
          <div className="text-xs text-muted-foreground">Losses</div>
          <div className="text-xl font-bold text-red-600">{stats.losses}</div>
        </Card>
        <Card className="px-3 py-2">
          <div className="text-xs text-muted-foreground">Filtered trade P&amp;L</div>
          <div className={`text-xl font-bold ${stats.totalPnl >= 0 ? 'text-green-600' : 'text-red-600'}`}>
            {stats.totalPnl >= 0 ? '+' : ''}
            {stats.totalPnl.toFixed(2)}
          </div>
        </Card>
      </div>

      {/* Filters */}
      <div className="flex flex-wrap items-end gap-2">
        <div>
          <Label className="text-xs">Asset</Label>
          <Select value={selectedAsset} onValueChange={setSelectedAsset}>
            <SelectTrigger className="w-28 h-8 px-2 py-1 text-xs shadow-sm" aria-label="Asset">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {assets.map((a) => (
                <SelectItem key={a} value={a}>
                  {a === 'ALL' ? 'All' : a}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <div>
          <Label className="text-xs">Status</Label>
          <Select value={selectedStatus} onValueChange={setSelectedStatus}>
            <SelectTrigger className="w-24 h-8 px-2 py-1 text-xs shadow-sm" aria-label="Status">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="ALL">All</SelectItem>
              <SelectItem value="open">Open</SelectItem>
              <SelectItem value="closed">Closed</SelectItem>
            </SelectContent>
          </Select>
        </div>
        <div>
          <Label className="text-xs">Side</Label>
          <Select value={selectedSide} onValueChange={setSelectedSide}>
            <SelectTrigger className="w-20 h-8 px-2 py-1 text-xs shadow-sm" aria-label="Side">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="ALL">All</SelectItem>
              <SelectItem value="buy">Buy</SelectItem>
              <SelectItem value="sell">Sell</SelectItem>
            </SelectContent>
          </Select>
        </div>
        <div>
          <Label className="text-xs">Start date</Label>
          <Input
            type="date"
            value={startDate}
            max={endDate || undefined}
            onChange={(e) => setStartDate(e.target.value)}
            className="w-36 h-8 px-2 text-xs shadow-sm"
            aria-label="Start date"
          />
        </div>
        <div>
          <Label className="text-xs">End date</Label>
          <Input
            type="date"
            value={endDate}
            min={startDate || undefined}
            onChange={(e) => setEndDate(e.target.value)}
            className="w-36 h-8 px-2 text-xs shadow-sm"
            aria-label="End date"
          />
        </div>
        <div>
          <Label className="text-xs">Limit</Label>
          <Input
            type="number"
            value={limit}
            onChange={(e) => setLimit(parseInt(e.target.value) || 50)}
            className="w-16 h-8 px-2 text-xs shadow-sm"
            aria-label="Limit"
            min={1}
            max={500}
          />
        </div>
        {(startDate || endDate) && (
          <Button
            variant="ghost"
            size="sm"
            className="h-8 text-xs"
            onClick={() => {
              setStartDate('')
              setEndDate('')
            }}
          >
            Clear dates
          </Button>
        )}
      </div>

      {/* Trades Table */}
      {trades.length === 0 ? (
        <Card className="p-12 text-center">
          <Activity className="h-12 w-12 mx-auto mb-4 text-muted-foreground opacity-50" />
          <p className="text-muted-foreground">No trades found</p>
          <p className="text-sm text-muted-foreground mt-1">
            Trades will appear here once the agent executes them
          </p>
        </Card>
      ) : (
        <Card className="overflow-x-auto">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Symbol</TableHead>
                <TableHead className="text-right">Qty</TableHead>
                <TableHead className="text-right">Entry</TableHead>
                <TableHead className="text-right">Init SL</TableHead>
                <TableHead className="text-right">SL&nbsp;&rarr;&nbsp;1</TableHead>
                <TableHead className="text-right">SL&nbsp;&rarr;&nbsp;2</TableHead>
                <TableHead className="text-right">SL&nbsp;&rarr;&nbsp;3</TableHead>
                <TableHead className="text-right">Target</TableHead>
                <TableHead className="text-right">Exit</TableHead>
                <TableHead>Result</TableHead>
                <TableHead className="text-right">P&amp;L</TableHead>
                <TableHead className="text-right">Broker charges</TableHead>
                <TableHead className="text-right">AI cost (USD)</TableHead>
                <TableHead>Why this trade?</TableHead>
                <TableHead>Entry Time</TableHead>
                <TableHead>Exit Time</TableHead>
                <TableHead className="text-right">Duration</TableHead>
                <TableHead className="text-right"></TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {trades.map((trade) => (
                <TradeRow key={trade.id} trade={trade} onDeleted={() => refetch()} />
              ))}
            </TableBody>
          </Table>
        </Card>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Trade Row
// ---------------------------------------------------------------------------

function TradeRow({ trade, onDeleted }: { trade: TradingAnalysisTrade; onDeleted: () => void }) {
  const [deleting, setDeleting] = useState(false)
  const [confirmDelete, setConfirmDelete] = useState(false)

  const isBuy = trade.side?.toLowerCase() === 'buy'
  const isOpen = trade.trade_status === 'open'
  const isPartial = isPartialTrade(trade)
  const trail = trade.sl_trail || []
  const t1 = trail[0]?.sl ?? null
  const t2 = trail[1]?.sl ?? null
  const t3 = trail[2]?.sl ?? null
  const more = Math.max(0, trail.length - 3)
  const pnl = trade.pnl

  const handleDelete = async (e: React.MouseEvent) => {
    e.stopPropagation()
    if (!confirmDelete) {
      setConfirmDelete(true)
      return
    }
    setDeleting(true)
    try {
      await deleteTradingAnalysisTrade(trade.id)
      onDeleted()
    } catch {
      setDeleting(false)
      setConfirmDelete(false)
    }
  }

  return (
    <TableRow>
      {/* Symbol + side */}
      <TableCell>
        <div className="flex items-center gap-2">
          {isBuy ? (
            <TrendingUp className="h-4 w-4 text-green-600 shrink-0" />
          ) : (
            <TrendingDown className="h-4 w-4 text-red-600 shrink-0" />
          )}
          <div className="leading-tight">
            <div className="font-semibold text-sm">{trade.asset}</div>
            <div className="text-[11px] text-muted-foreground uppercase">{trade.side || '—'}</div>
          </div>
        </div>
      </TableCell>

      <TableCell className="text-right font-mono text-xs">{fmt(trade.quantity)}</TableCell>
      <TableCell className="text-right font-mono text-xs">{fmt(trade.entry_price)}</TableCell>
      <TableCell className="text-right font-mono text-xs">{fmt(trade.stop_loss)}</TableCell>

      {/* Trailed stops */}
      <TableCell className="text-right font-mono text-xs">{fmt(t1)}</TableCell>
      <TableCell className="text-right font-mono text-xs">{fmt(t2)}</TableCell>
      <TableCell className="text-right font-mono text-xs">
        {fmt(t3)}
        {more > 0 && (
          <span className="ml-1 text-[10px] text-muted-foreground" title={`${more} more trail step(s)`}>
            +{more}
          </span>
        )}
      </TableCell>

      <TableCell className="text-right font-mono text-xs">{fmt(trade.take_profit)}</TableCell>
      <TableCell className="text-right font-mono text-xs">{fmt(trade.exit_price)}</TableCell>

      {/* Result / target taken */}
      <TableCell>
        <Badge
          variant={isOpen ? 'default' : 'secondary'}
          className="text-[10px] font-medium whitespace-nowrap"
        >
          {isOpen ? 'OPEN' : trade.exit_reason || 'CLOSED'}
        </Badge>
      </TableCell>

      {/* P&L */}
      <TableCell className="text-right">
        {pnl !== null ? (
          <span className={`font-mono font-semibold text-xs ${pnl >= 0 ? 'text-green-600' : 'text-red-600'}`}>
            {pnl >= 0 ? '+' : ''}
            {pnl.toFixed(2)}
            {trade.pnl_in_parent && <small className="block text-muted-foreground">In position total</small>}
          </span>
        ) : (
          <span className="text-xs text-muted-foreground">—</span>
        )}
      </TableCell>

      {/* Posted costs and entry evidence */}
      <TableCell className="text-right font-mono text-xs whitespace-nowrap">
        {trade.broker_cost?.total != null ? (
          <span title="Posted commission, fees and swap across all position deals; positive is a charge, negative is a credit">
            {trade.broker_cost.total.toFixed(4)} {trade.broker_cost.currency}
            {trade.broker_cost.status === 'accrued' && <span className="block text-muted-foreground">so far</span>}
          </span>
        ) : <span className="text-muted-foreground">{isPartial ? 'In parent' : 'Pending'}</span>}
      </TableCell>
      <TableCell className="text-right font-mono text-xs whitespace-nowrap">
        <span title="Total provider usage cost for the decision cycle, including expert and judge; excludes invoice taxes/credits">
          {trade.ai_cost?.total_usd != null ? `$${trade.ai_cost.total_usd.toLocaleString('en-US', { minimumFractionDigits: 6, maximumFractionDigits: 12 })}` : isPartial ? 'In parent' : 'Unavailable'}
        </span>
      </TableCell>
      <TableCell className="min-w-[220px] max-w-[360px] text-xs">
        {trade.trade_reason || trade.trigger_reason ? (
          <details>
            <summary className="cursor-pointer line-clamp-2">{trade.trade_reason || trade.trigger_reason}</summary>
            <p className="mt-2 whitespace-pre-wrap">{trade.trade_reason}</p>
            {trade.trigger_reason && <p className="mt-2 whitespace-pre-wrap">Entry trigger: {trade.trigger_reason}</p>}
          </details>
        ) : <span className="text-muted-foreground">Unavailable</span>}
      </TableCell>
      <TableCell className="text-xs text-muted-foreground whitespace-nowrap">
        {toIST(trade.trade_executed_at)}
      </TableCell>
      <TableCell className="text-xs text-muted-foreground whitespace-nowrap">
        {toIST(trade.trade_closed_at)}
      </TableCell>
      <TableCell className="text-right font-mono text-xs whitespace-nowrap">
        {fmtDuration(trade.trade_executed_at, trade.trade_closed_at)}
      </TableCell>

      {/* Delete */}
      <TableCell className="text-right">
        <Button
          variant={confirmDelete ? 'destructive' : 'ghost'}
          size="sm"
          disabled={deleting}
          onClick={handleDelete}
          onBlur={() => setConfirmDelete(false)}
          className="h-7 px-2 text-[11px] gap-1"
          title="Delete trade"
        >
          {deleting ? <Loader2 className="h-3 w-3 animate-spin" /> : <Trash2 className="h-3 w-3" />}
          {confirmDelete && 'Confirm?'}
        </Button>
      </TableCell>
    </TableRow>
  )
}

// ---------------------------------------------------------------------------
// Delete-all button (destructive, two-step confirm)
// ---------------------------------------------------------------------------

function DeleteAllButton({ onDeleted }: { onDeleted: () => void }) {
  const [confirming, setConfirming] = useState(false)
  const [deleting, setDeleting] = useState(false)

  const handleClick = async () => {
    if (!confirming) {
      setConfirming(true)
      return
    }
    setDeleting(true)
    try {
      const res = await deleteAllTradingAnalysisTrades()
      toast.success(`Deleted all trade data (${res.deleted_count} records)`)
      onDeleted()
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Failed to delete trade data')
    } finally {
      setDeleting(false)
      setConfirming(false)
    }
  }

  return (
    <Button
      variant="destructive"
      size="sm"
      className="h-7 gap-1 px-2 text-xs"
      disabled={deleting}
      onClick={handleClick}
      onBlur={() => setConfirming(false)}
      title="Permanently delete ALL trade data (trades, decision cycles, active-trade rows). Does not touch open broker positions."
    >
      {deleting ? (
        <Loader2 className="h-3.5 w-3.5 animate-spin" />
      ) : confirming ? (
        <AlertTriangle className="h-3.5 w-3.5" />
      ) : (
        <Trash2 className="h-3.5 w-3.5" />
      )}
      {confirming ? 'Click again to delete ALL' : 'Delete All Trades'}
    </Button>
  )
}
