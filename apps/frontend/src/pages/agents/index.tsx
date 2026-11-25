import {
  useQuery,
  useMutation,
  useQueryClient,
} from '@tanstack/react-query'
import {
  listAgents,
  getAgentMetrics,
  startAgent,
  stopAgent,
  runAgentNow,
  saveAgent,
  deleteAgent,
  listPrompts,
} from '@/services/apiClient'
import { toast } from 'sonner'
import { useMt5Status } from '@/services/hooks'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Card, CardContent, CardFooter, CardHeader } from '@/components/ui/card'
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
  TooltipProvider,
} from '@/components/ui/tooltip'
import {
  ArrowUpDown,
  Loader2,
  Pencil,
  Play,
  Plus,
  Search,
  Square,
  Trash2,
  Bot,
} from 'lucide-react'
import {
  useAgentsStore,
  useAgentList,
  type Agent,
} from '@/stores/agents'
import React from 'react'
import Mt5WalletPanel from '@/components/Mt5WalletPanel'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Modal } from '@/components/ui/modal'
import type { AgentInfo, Metrics } from '@/types/api'
import { Asset, assetOptions } from '@/constants/assets'
import { cn } from '@/lib/utils'

type StatusFilter = 'all' | 'running' | 'stopped'
type SortKey = 'pnl' | 'name' | 'status'

const fmtMoney = (n: number) => `${n >= 0 ? '+' : ''}${n.toFixed(2)}`

const fmtTime = (iso?: string | null) =>
  iso
    ? new Date(iso).toLocaleTimeString('en-IN', {
        timeZone: 'Asia/Kolkata',
        hour: '2-digit',
        minute: '2-digit',
      })
    : '—'

export default function Agents() {
  const setAgents = useAgentsStore((s) => s.setAgents)
  const setMetrics = useAgentsStore((s) => s.setMetrics)
  const metrics = useAgentsStore((s) => s.metrics)
  const queryClient = useQueryClient()

  const [search, setSearch] = React.useState('')
  const [status, setStatus] = React.useState<StatusFilter>('all')
  const [sortKey, setSortKey] = React.useState<SortKey>('pnl')
  const [sortDir, setSortDir] = React.useState<'asc' | 'desc'>('desc')
  const [adding, setAdding] = React.useState(false)
  const [stoppingAll, setStoppingAll] = React.useState(false)

  const agentsQuery = useQuery({
    queryKey: ['agents'],
    queryFn: listAgents,
    refetchInterval: 2000,
  })

  const metricsQuery = useQuery({
    queryKey: ['agentMetrics'],
    queryFn: getAgentMetrics,
    refetchInterval: 2000,
  })

  React.useEffect(() => {
    if (agentsQuery.data) {
      setAgents(
        agentsQuery.data.agents.map((a: any) => ({
          id: a.id,
          symbol: a.symbol,
          candleCount5m: a.candle_count_5m,
          candleCount15m: a.candle_count_15m,
          broker: a.broker || 'mt5',
          riskPercent: a.risk_percent,
          runInterval: a.run_interval,
          promptId: a.prompt_id,
        })),
      )
    }
  }, [agentsQuery.data, setAgents])

  React.useEffect(() => {
    if (metricsQuery.data) {
      setMetrics(metricsQuery.data)
    }
  }, [metricsQuery.data, setMetrics])

  const agents = useAgentList()

  // Metrics may be keyed by agent id or symbol depending on the backend path;
  // resolve defensively so status/pnl are always found.
  const metricFor = React.useCallback(
    (a: Agent): Metrics | undefined => metrics[a.id] ?? metrics[a.symbol],
    [metrics],
  )

  const summary = React.useMemo(() => {
    let running = 0
    let pnl = 0
    let open = 0
    for (const a of agents) {
      const m = metricFor(a)
      if (m?.running) running += 1
      pnl += m?.pnl ?? 0
      open += m?.open_trades ?? 0
    }
    return { running, pnl, open, total: agents.length }
  }, [agents, metricFor])

  const filtered = React.useMemo(() => {
    return agents
      .filter(
        (a) =>
          a.symbol.toLowerCase().includes(search.toLowerCase()) ||
          a.id.toLowerCase().includes(search.toLowerCase()),
      )
      .filter((a) => {
        const run = metricFor(a)?.running
        return status === 'all' ? true : status === 'running' ? run : !run
      })
      .sort((a, b) => {
        const mA = metricFor(a)
        const mB = metricFor(b)
        let cmp = 0
        if (sortKey === 'pnl') cmp = (mA?.pnl ?? 0) - (mB?.pnl ?? 0)
        else if (sortKey === 'name') cmp = a.symbol.localeCompare(b.symbol)
        else cmp = Number(mA?.running ?? false) - Number(mB?.running ?? false)
        return sortDir === 'asc' ? cmp : -cmp
      })
  }, [agents, metricFor, search, status, sortKey, sortDir])

  const runningAgents = React.useMemo(
    () => agents.filter((a) => metricFor(a)?.running),
    [agents, metricFor],
  )

  const handleStopAll = async () => {
    if (!runningAgents.length) return
    if (!window.confirm(`Stop all ${runningAgents.length} running agent(s)?`)) return
    setStoppingAll(true)
    const results = await Promise.allSettled(
      runningAgents.map((a) => stopAgent(a.symbol)),
    )
    const failed = results.filter((r) => r.status === 'rejected').length
    setStoppingAll(false)
    queryClient.invalidateQueries({ queryKey: ['agentMetrics'] })
    if (failed) {
      toast.error(`Stopped ${runningAgents.length - failed}, ${failed} failed`)
    } else {
      toast.success(`Stopped ${runningAgents.length} agent(s)`)
    }
  }

  const loading = agentsQuery.isLoading

  return (
    <TooltipProvider>
      <div className="flex flex-col gap-3">
        {/* MT5 wallet + connection + agent rollup (single compact bar) */}
        <Mt5WalletPanel summary={summary} />

        {/* Toolbar */}
        <div className="flex flex-wrap items-center gap-1.5">
          <div className="relative">
            <Search className="pointer-events-none absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
            <Input
              className="h-8 w-40 pl-7 text-xs shadow-sm"
              placeholder="Search agents…"
              aria-label="Search agents"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
            />
          </div>
          <select
            className="h-8 rounded-md bg-input px-2 text-xs shadow-sm border-0"
            aria-label="Agent status"
            value={status}
            onChange={(e) => setStatus(e.target.value as StatusFilter)}
          >
            <option value="all">All status</option>
            <option value="running">Running</option>
            <option value="stopped">Stopped</option>
          </select>
          <select
            className="h-8 rounded-md bg-input px-2 text-xs shadow-sm border-0"
            aria-label="Sort agents by"
            value={sortKey}
            onChange={(e) => setSortKey(e.target.value as SortKey)}
          >
            <option value="pnl">Sort: P&L</option>
            <option value="name">Sort: Name</option>
            <option value="status">Sort: Status</option>
          </select>
          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                variant="outline"
                size="sm"
                className="h-8 w-8 p-0 shadow-sm"
                aria-label={sortDir === 'asc' ? 'Sort descending' : 'Sort ascending'}
                onClick={() => setSortDir((d) => (d === 'asc' ? 'desc' : 'asc'))}
              >
                <ArrowUpDown className="h-3.5 w-3.5" />
              </Button>
            </TooltipTrigger>
            <TooltipContent>{sortDir === 'asc' ? 'Ascending' : 'Descending'}</TooltipContent>
          </Tooltip>

          <div className="ml-auto flex items-center gap-1.5">
            <Button
              variant="destructive"
              size="sm"
              className="h-8 gap-1 px-2 text-xs"
              onClick={handleStopAll}
              disabled={stoppingAll || runningAgents.length === 0}
            >
              {stoppingAll ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
              ) : (
                <Square className="h-3.5 w-3.5" />
              )}
              Stop All{runningAgents.length ? ` (${runningAgents.length})` : ''}
            </Button>
            <Button
              size="sm"
              variant="secondary"
              className="h-8 gap-1 px-2 text-xs"
              onClick={() => setAdding(true)}
            >
              <Plus className="h-3.5 w-3.5" />
              Add Agent
            </Button>
          </div>
        </div>

        {/* Agent grid */}
        {loading ? (
          <div className="grid gap-md sm:grid-cols-2 xl:grid-cols-3">
            {Array.from({ length: 3 }).map((_, i) => (
              <div key={i} className="h-64 animate-pulse rounded-xl bg-card shadow-md" />
            ))}
          </div>
        ) : filtered.length === 0 ? (
          <EmptyState hasAgents={agents.length > 0} onAdd={() => setAdding(true)} />
        ) : (
          <div className="grid gap-md sm:grid-cols-2 xl:grid-cols-3">
            {filtered.map((agent) => (
              <AgentCard key={agent.id} agent={agent} />
            ))}
          </div>
        )}

        {adding && <AgentModal onClose={() => setAdding(false)} />}
      </div>
    </TooltipProvider>
  )
}

function EmptyState({ hasAgents, onAdd }: { hasAgents: boolean; onAdd: () => void }) {
  return (
    <div className="flex flex-col items-center justify-center gap-3 rounded-xl bg-card py-16 text-center shadow-md">
      <div className="flex h-12 w-12 items-center justify-center rounded-full bg-primary/10 text-primary">
        <Bot className="h-6 w-6" />
      </div>
      <div>
        <p className="font-medium">{hasAgents ? 'No agents match your filters' : 'No agents yet'}</p>
        <p className="text-sm text-muted-foreground">
          {hasAgents
            ? 'Try adjusting your search or status filter.'
            : 'Create your first trading agent to get started.'}
        </p>
      </div>
      {!hasAgents && (
        <Button size="sm" variant="secondary" className="gap-1.5" onClick={onAdd}>
          <Plus className="h-4 w-4" />
          Add Agent
        </Button>
      )}
    </div>
  )
}

interface AgentCardProps {
  agent: Agent
}

export const AgentCard = React.memo(({ agent }: AgentCardProps) => {
  const metrics = useAgentsStore((s) => s.metrics[agent.symbol] ?? s.metrics[agent.id])
  const queryClient = useQueryClient()
  const [editing, setEditing] = React.useState(false)

  const invalidateMetrics = () =>
    queryClient.invalidateQueries({ queryKey: ['agentMetrics'] })

  const startMutation = useMutation({
    mutationFn: () => startAgent(agent.symbol, {} as any),
    onSettled: invalidateMetrics,
  })
  const stopMutation = useMutation({
    mutationFn: () => stopAgent(agent.symbol),
    onSettled: invalidateMetrics,
  })
  const runMutation = useMutation({
    mutationFn: () => runAgentNow(agent.symbol),
    onSettled: invalidateMetrics,
  })
  const deleteMutation = useMutation({
    mutationFn: () => deleteAgent(agent.id),
    onSuccess: () => toast.success('Agent deleted'),
    onError: (err: Error) => toast.error(err.message),
    onSettled: () => queryClient.invalidateQueries({ queryKey: ['agents'] }),
  })
  const { data: mt5Status } = useMt5Status()

  const running = metrics?.running
  const {
    last_run,
    eta,
    wins = 0,
    losses = 0,
    pnl = 0,
    realized_pnl = 0,
    unrealized_pnl = 0,
    open_trades = 0,
    orders = 0,
    runs = 0,
  } = metrics ?? {}

  const handleStart = async () => {
    if (!mt5Status?.connected) {
      toast.error('Connect broker before starting')
      return
    }
    try {
      await startMutation.mutateAsync()
      toast.success('Agent started')
    } catch (err) {
      toast.error((err as Error).message)
    }
  }

  const handleStop = async () => {
    try {
      await stopMutation.mutateAsync()
      toast.success('Agent stopped')
    } catch (err) {
      toast.error((err as Error).message)
    }
  }

  const handleRunNow = async () => {
    try {
      await runMutation.mutateAsync()
      toast.success('Run complete')
    } catch (err) {
      toast.error((err as Error).message)
    }
  }

  return (
    <Card className="group relative flex flex-col overflow-hidden rounded-xl transition-shadow hover:shadow-lg">
      {/* Status accent rail */}
      <span
        className={cn(
          'absolute inset-y-0 left-0 w-1',
          running ? 'bg-emerald-500' : 'bg-muted-foreground/25',
        )}
      />

      <CardHeader className="flex-row items-start justify-between space-y-0 pb-3 pl-5">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <span
              className={cn(
                'h-2 w-2 shrink-0 rounded-full',
                running ? 'bg-emerald-500 animate-pulse' : 'bg-muted-foreground/50',
              )}
            />
            <h3 className="truncate text-base font-semibold">{agent.symbol}</h3>
          </div>
          <p className="mt-0.5 truncate text-xs text-muted-foreground">
            {agent.broker} · {agent.riskPercent}% risk · {agent.runInterval} ·{' '}
            {agent.candleCount5m}/{agent.candleCount15m}
          </p>
        </div>
        <Badge variant={running ? 'success' : 'secondary'} className="shrink-0">
          {running ? 'Running' : 'Stopped'}
        </Badge>
      </CardHeader>

      <CardContent className="flex-1 space-y-3 pl-5">
        {/* Stat tiles */}
        <div className="grid grid-cols-4 gap-2">
          <StatTile label="Trades" value={orders} />
          <StatTile label="Wins" value={wins} tone="positive" />
          <StatTile label="Losses" value={losses} tone="negative" />
          <StatTile
            label="Net P&L"
            value={fmtMoney(pnl)}
            tone={pnl >= 0 ? 'positive' : 'negative'}
          />
        </div>

        {/* Realized / unrealized */}
        <div className="grid grid-cols-2 gap-3 text-xs">
          <PnlRow label="Realized" value={realized_pnl} />
          <PnlRow label="Unrealized" value={unrealized_pnl} />
        </div>

        {open_trades > 0 && (
          <div className="inline-flex items-center gap-1.5 rounded-md bg-blue-500/10 px-2 py-0.5 text-xs font-medium text-blue-500">
            <span className="h-1.5 w-1.5 rounded-full bg-blue-500" />
            {open_trades} open trade{open_trades > 1 ? 's' : ''}
          </div>
        )}

        {/* Timing */}
        <div className="flex items-center justify-between border-t border-border/60 pt-2 text-xs text-muted-foreground">
          <span>Runs · {runs}</span>
          <span title={last_run ?? undefined}>Last · {fmtTime(last_run)}</span>
          <span title={eta ?? undefined}>Next · {fmtTime(eta)}</span>
        </div>
      </CardContent>

      <CardFooter className="gap-2 pl-5">
        {running ? (
          <Button
            size="sm"
            variant="secondary"
            className="flex-1 gap-1.5"
            onClick={handleStop}
            disabled={stopMutation.isPending}
          >
            {stopMutation.isPending ? (
              <Loader2 className="h-4 w-4 animate-spin" />
            ) : (
              <Square className="h-4 w-4" />
            )}
            Stop
          </Button>
        ) : (
          <Button
            size="sm"
            variant="secondary"
            className="flex-1 gap-1.5"
            onClick={handleStart}
            disabled={startMutation.isPending}
          >
            {startMutation.isPending ? (
              <Loader2 className="h-4 w-4 animate-spin" />
            ) : (
              <Play className="h-4 w-4" />
            )}
            Start
          </Button>
        )}

        <IconAction
          label="Run now"
          onClick={handleRunNow}
          disabled={runMutation.isPending}
          loading={runMutation.isPending}
          icon={<Play className="h-4 w-4" />}
        />
        <IconAction
          label="Edit"
          onClick={() => setEditing(true)}
          icon={<Pencil className="h-4 w-4" />}
        />
        <IconAction
          label="Delete"
          onClick={() => deleteMutation.mutate()}
          disabled={deleteMutation.isPending}
          loading={deleteMutation.isPending}
          danger
          icon={<Trash2 className="h-4 w-4" />}
        />
      </CardFooter>

      {editing && <AgentModal agent={agent} onClose={() => setEditing(false)} />}
    </Card>
  )
})
AgentCard.displayName = 'AgentCard'

function StatTile({
  label,
  value,
  tone = 'default',
}: {
  label: string
  value: string | number
  tone?: 'default' | 'positive' | 'negative'
}) {
  return (
    <div className="rounded-lg bg-secondary/40 px-2 py-1.5 text-center">
      <div className="text-[10px] uppercase tracking-wide text-muted-foreground">{label}</div>
      <div
        className={cn(
          'truncate font-mono text-sm font-semibold tabular-nums',
          tone === 'positive' && 'text-emerald-500',
          tone === 'negative' && 'text-red-500',
        )}
      >
        {value}
      </div>
    </div>
  )
}

function PnlRow({ label, value }: { label: string; value: number }) {
  return (
    <div className="flex items-center justify-between">
      <span className="text-muted-foreground">{label}</span>
      <span
        className={cn(
          'font-mono tabular-nums',
          value >= 0 ? 'text-emerald-500' : 'text-red-500',
        )}
      >
        {fmtMoney(value)}
      </span>
    </div>
  )
}

function IconAction({
  label,
  icon,
  onClick,
  disabled,
  loading,
  danger,
}: {
  label: string
  icon: React.ReactNode
  onClick: () => void
  disabled?: boolean
  loading?: boolean
  danger?: boolean
}) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button
          size="icon"
          variant="ghost"
          className={cn('h-9 w-9', danger && 'hover:bg-destructive/10 hover:text-destructive')}
          onClick={onClick}
          disabled={disabled}
          aria-label={label}
        >
          {loading ? <Loader2 className="h-4 w-4 animate-spin" /> : icon}
        </Button>
      </TooltipTrigger>
      <TooltipContent>{label}</TooltipContent>
    </Tooltip>
  )
}

function AgentModal({ agent, onClose }: { agent?: Agent; onClose: () => void }) {
  const queryClient = useQueryClient()
  const [symbol, setSymbol] = React.useState(agent?.symbol ?? Asset.XAUUSD)
  const [candleCount5m, setCandleCount5m] = React.useState(agent?.candleCount5m ?? 0)
  const [candleCount15m, setCandleCount15m] = React.useState(agent?.candleCount15m ?? 0)
  const [riskPercent, setRiskPercent] = React.useState(agent?.riskPercent ?? 1)
  const [runInterval, setRunInterval] = React.useState(agent?.runInterval ?? 'hourly')
  const [broker, setBroker] = React.useState(agent?.broker ?? 'mt5')
  const [promptId, setPromptId] = React.useState(agent?.promptId ?? '')

  const promptsQuery = useQuery({
    queryKey: ['prompts'],
    queryFn: listPrompts,
  })

  const mutation = useMutation({
    mutationFn: async () => {
      const agentId =
        agent?.id || `${symbol.toLowerCase().replace(/[^a-z0-9]/g, '')}_${Date.now()}`

      const info: AgentInfo = {
        id: agentId,
        symbol,
        candle_count_5m: candleCount5m,
        candle_count_15m: candleCount15m,
        broker,
        risk_percent: riskPercent,
        run_interval: runInterval,
        prompt_id: promptId,
      }
      await saveAgent(info)
    },
    onSuccess: () => {
      toast.success('Agent saved')
      onClose()
    },
    onError: (err: Error) => toast.error(err.message),
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ['agents'] })
      queryClient.invalidateQueries({ queryKey: ['agentMetrics'] })
    },
  })

  const submit = (e: React.FormEvent) => {
    e.preventDefault()
    if (!symbol || !promptId) {
      toast.error('Symbol and Prompt are required')
      return
    }
    mutation.mutate()
  }

  return (
    <Modal
      open
      onClose={onClose}
      title={agent ? 'Edit Agent' : 'Add Agent'}
      size="lg"
      className="max-h-[90vh]"
    >
      <div className="flex max-h-[70vh] flex-col">
        <div className="flex-1 overflow-y-auto px-1">
          <form onSubmit={submit} className="grid gap-4 py-1">
            {/* Symbol */}
            <div className="space-y-1">
              <Label htmlFor="symbol">Symbol *</Label>
              <select
                id="symbol"
                className="w-full rounded-md bg-input px-3 py-2 shadow-sm focus:outline-none focus:ring-2 focus:ring-ring border-0"
                value={symbol}
                onChange={(e) => setSymbol(e.target.value)}
              >
                {assetOptions.map((a) => (
                  <option key={a} value={a}>
                    {a}
                  </option>
                ))}
              </select>
            </div>

            {/* Candle counts */}
            <div className="grid grid-cols-2 gap-4">
              <div className="space-y-1">
                <Label htmlFor="candles5m">5m Candle Count *</Label>
                <Input
                  id="candles5m"
                  type="number"
                  min="0"
                  value={candleCount5m}
                  onChange={(e) => setCandleCount5m(Number(e.target.value))}
                  placeholder="0"
                />
              </div>
              <div className="space-y-1">
                <Label htmlFor="candles15m">15m Candle Count *</Label>
                <Input
                  id="candles15m"
                  type="number"
                  min="0"
                  value={candleCount15m}
                  onChange={(e) => setCandleCount15m(Number(e.target.value))}
                  placeholder="0"
                />
              </div>
            </div>

            {/* Broker + risk */}
            <div className="grid grid-cols-2 gap-4">
              <div className="space-y-1">
                <Label htmlFor="broker">Broker *</Label>
                <select
                  id="broker"
                  className="w-full rounded-md bg-input px-3 py-2 shadow-sm focus:outline-none focus:ring-2 focus:ring-ring border-0"
                  value={broker}
                  onChange={(e) => setBroker(e.target.value)}
                >
                  <option value="mt5">MetaTrader5</option>
                </select>
              </div>
              <div className="space-y-1">
                <Label htmlFor="riskPercent">Risk % *</Label>
                <Input
                  id="riskPercent"
                  type="number"
                  min="0.1"
                  max="100"
                  step="0.1"
                  value={riskPercent}
                  onChange={(e) => setRiskPercent(Number(e.target.value))}
                  placeholder="1.0"
                />
              </div>
            </div>

            {/* Prompt */}
            <div className="space-y-1">
              <Label htmlFor="promptId">Prompt *</Label>
              <select
                id="promptId"
                className="w-full rounded-md bg-input px-3 py-2 shadow-sm focus:outline-none focus:ring-2 focus:ring-ring border-0"
                value={promptId}
                onChange={(e) => setPromptId(e.target.value)}
                required
              >
                <option value="">Select a prompt…</option>
                {promptsQuery.data?.map((prompt: any) => (
                  <option key={prompt.id} value={prompt.id}>
                    {prompt.name} ({prompt.gpt_config?.model || 'Unknown Model'})
                  </option>
                ))}
              </select>
              <p className="text-xs text-muted-foreground">
                The prompt supplies the GPT configuration (model, temperature, etc.)
              </p>
            </div>

            {/* Run interval */}
            <div className="space-y-1">
              <Label htmlFor="runInterval">Run Interval *</Label>
              <select
                id="runInterval"
                className="w-full rounded-md bg-input px-3 py-2 shadow-sm focus:outline-none focus:ring-2 focus:ring-ring border-0"
                value={runInterval}
                onChange={(e) => setRunInterval(e.target.value)}
              >
                <option value="hourly">Hourly (1h)</option>
                <option value="30min">30 Minutes</option>
              </select>
            </div>
          </form>
        </div>

        <div className="flex justify-end gap-3 border-t border-border/60 bg-background px-1 pt-4">
          <Button type="button" variant="outline" onClick={onClose}>
            Cancel
          </Button>
          <Button
            type="submit"
            onClick={submit}
            variant="secondary"
            disabled={mutation.isPending || !symbol || !promptId}
            className="gap-1.5"
          >
            {mutation.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : null}
            {agent ? 'Update Agent' : 'Create Agent'}
          </Button>
        </div>
      </div>
    </Modal>
  )
}
