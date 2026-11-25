import * as React from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Loader2, Plug, Unplug, RefreshCw, Wallet } from 'lucide-react'
import { toast } from 'sonner'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Modal } from '@/components/ui/modal'
import { useMt5Wallet, useMt5Status, useAgentMetrics } from '@/services/hooks'
import { connectMt5, disconnectMt5, getBrokerConfig } from '@/services/apiClient'
import type { Mt5Config } from '@/types/api'
import { cn } from '@/lib/utils'

export interface AgentSummary {
  running: number
  total: number
  pnl: number
  open: number
}

interface Mt5WalletPanelProps {
  /** Optional agent rollup rendered alongside the wallet to save vertical space. */
  summary?: AgentSummary
}

/**
 * Format a monetary amount with the account currency when available.
 * Falls back to a plain grouped number, and an em-dash for missing values.
 */
function money(value?: number | null, currency?: string | null): string {
  if (value == null || Number.isNaN(value)) return '—'
  if (currency) {
    try {
      return new Intl.NumberFormat('en-US', {
        style: 'currency',
        currency,
        minimumFractionDigits: 2,
        maximumFractionDigits: 2,
      }).format(value)
    } catch {
      /* unknown currency code — fall through to plain formatting */
    }
  }
  return value.toLocaleString('en-US', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })
}

const signed = (n: number) => `${n >= 0 ? '+' : ''}${n.toFixed(2)}`

function Stat({
  label,
  value,
  emphasis = false,
  tone = 'default',
  className,
}: {
  label: string
  value: string
  emphasis?: boolean
  tone?: 'default' | 'positive' | 'negative'
  className?: string
}) {
  return (
    <div className={cn('min-w-0', className)}>
      <div className="text-[10px] uppercase tracking-wide text-muted-foreground">{label}</div>
      <div
        className={cn(
          'truncate font-mono tabular-nums',
          emphasis ? 'text-sm font-semibold' : 'text-sm font-medium',
          tone === 'positive' && 'text-emerald-500',
          tone === 'negative' && 'text-red-500',
        )}
        title={value}
      >
        {value}
      </div>
    </div>
  )
}

/**
 * MT5 account + connection control surface.
 *
 * Combines live wallet balances, connection actions, and an optional agent
 * rollup so the broker can be managed from the Agents page (no separate Broker
 * page) while keeping the layout compact.
 */
export default function Mt5WalletPanel({ summary }: Mt5WalletPanelProps) {
  const { data: mt5Status, refetch: refetchMt5Status } = useMt5Status()
  const { data: wallet, error } = useMt5Wallet(mt5Status?.connected === true)
  const agentMetrics = useAgentMetrics()
  const qc = useQueryClient()

  const { data: brokerConfig } = useQuery({
    queryKey: ['brokerConfig'],
    queryFn: getBrokerConfig,
  })

  const [showModal, setShowModal] = React.useState(false)
  const [connecting, setConnecting] = React.useState(false)
  const [disconnecting, setDisconnecting] = React.useState(false)
  const [server, setServer] = React.useState('')
  const [loginId, setLoginId] = React.useState('')
  const [password, setPassword] = React.useState('')

  const connected = !!mt5Status?.connected
  const offline = mt5Status?.offline === true
  const currency = wallet?.currency
  const hasSavedCreds = !!(
    brokerConfig?.mt5?.server &&
    brokerConfig.mt5.loginId &&
    brokerConfig.mt5.password
  )

  const openModal = () => {
    // Never pre-fill: a real-money account must be typed in deliberately.
    setServer('')
    setLoginId('')
    setPassword('')
    setShowModal(true)
  }

  const doConnect = async (cfg?: Mt5Config) => {
    const target = cfg?.server ?? brokerConfig?.mt5?.server ?? ''
    if (
      /real/i.test(target) &&
      !window.confirm(`"${target}" looks like a REAL-money account. Connect and risk real funds?`)
    ) {
      return
    }
    try {
      setConnecting(true)
      await connectMt5(cfg)
      if (cfg) await qc.invalidateQueries({ queryKey: ['brokerConfig'] })
      await refetchMt5Status()
      await qc.invalidateQueries({ queryKey: ['mt5-wallet'] })
      toast.success('Connected to MT5')
      setShowModal(false)
    } catch (err) {
      toast.error((err as Error).message || 'Failed to connect')
    } finally {
      setConnecting(false)
    }
  }

  const handleConnectClick = () => {
    if (hasSavedCreds) {
      doConnect()
    } else {
      openModal()
    }
  }

  const handleDisconnect = async () => {
    const anyRunning =
      agentMetrics.data && Object.values(agentMetrics.data).some((m) => m.running)
    if (anyRunning) {
      toast.error('Stop all agents before disconnecting broker')
      return
    }
    try {
      setDisconnecting(true)
      await disconnectMt5()
      await refetchMt5Status()
      toast.success('Disconnected from MT5')
    } catch (err) {
      toast.error((err as Error).message || 'Failed to disconnect')
    } finally {
      setDisconnecting(false)
    }
  }

  const walletError = !!error
  const bal = walletError ? undefined : wallet?.balance
  const eq = walletError ? undefined : wallet?.equity
  const margin = walletError ? undefined : wallet?.margin
  const freeMargin = walletError ? undefined : wallet?.free_margin

  // Equity below balance signals an open drawdown — surface it subtly.
  const equityTone =
    eq != null && bal != null ? (eq >= bal ? 'positive' : 'negative') : 'default'

  return (
    <section className="rounded-xl bg-card text-card-foreground shadow-md">
      {/* Identity + actions */}
      <div className="flex flex-wrap items-center justify-between gap-3 px-md py-3">
        <div className="flex items-center gap-3">
          <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-secondary text-foreground">
            <Wallet className="h-4 w-4" />
          </div>
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <h2 className="text-sm font-semibold leading-none">MT5 Account</h2>
              <span
                className={cn(
                  'inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-[10px] font-medium',
                  connected
                    ? 'bg-emerald-500/15 text-emerald-500'
                    : 'bg-muted text-muted-foreground',
                )}
              >
                <span
                  className={cn(
                    'h-1.5 w-1.5 rounded-full',
                    connected ? 'bg-emerald-500 animate-pulse' : 'bg-muted-foreground/60',
                  )}
                />
                {offline ? 'Offline mode' : connected ? 'Connected' : 'Disconnected'}
              </span>
            </div>
            <p className="mt-1 truncate text-xs text-muted-foreground">
              {connected
                ? [wallet?.name || (wallet?.login ? `#${wallet.login}` : null), wallet?.server]
                    .filter(Boolean)
                    .join(' · ') || 'Live account'
                : offline ? 'Connected mode is required for backtests and MT5 access' : 'Connect to load balances and trade'}
            </p>
          </div>
        </div>

        {/* Compact connection actions */}
        <div className="flex items-center gap-2">
          {connected ? (
            <>
              <Button
                variant="outline"
                size="icon"
                onClick={handleConnectClick}
                disabled={connecting}
                className="h-8 w-8"
                aria-label="Reconnect"
                title="Reconnect"
              >
                {connecting ? (
                  <Loader2 className="h-4 w-4 animate-spin" />
                ) : (
                  <RefreshCw className="h-4 w-4" />
                )}
              </Button>
              <Button
                variant="destructive"
                size="icon"
                onClick={handleDisconnect}
                disabled={disconnecting}
                className="h-8 w-8"
                aria-label="Disconnect"
                title="Disconnect"
              >
                {disconnecting ? (
                  <Loader2 className="h-4 w-4 animate-spin" />
                ) : (
                  <Unplug className="h-4 w-4" />
                )}
              </Button>
            </>
          ) : (
            <Button
              variant="secondary"
              onClick={handleConnectClick}
              disabled={connecting || offline}
              className="h-8 gap-1.5 px-3 text-xs"
            >
              {connecting ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
              ) : (
                <Plug className="h-3.5 w-3.5" />
              )}
              {connecting ? 'Connecting…' : 'Connect'}
            </Button>
          )}
        </div>
      </div>

      {/* Wallet balances + agent rollup */}
      <div className="grid grid-cols-2 gap-x-md gap-y-3 border-t border-border/60 px-md py-3 sm:grid-cols-4 lg:grid-cols-7">
        <Stat label="Balance" value={money(bal, currency)} emphasis />
        <Stat label="Equity" value={money(eq, currency)} emphasis tone={equityTone} />
        <Stat label="Margin" value={money(margin, currency)} />
        <Stat label="Free Margin" value={money(freeMargin, currency)} />
        {summary && (
          <>
            <Stat
              label="Active"
              value={`${summary.running}/${summary.total}`}
              className="lg:border-l lg:border-border/60 lg:pl-md"
            />
            <Stat
              label="Net P&L"
              value={signed(summary.pnl)}
              tone={summary.pnl >= 0 ? 'positive' : 'negative'}
            />
            <Stat label="Open" value={String(summary.open)} />
          </>
        )}
      </div>

      {/* Credentials modal */}
      <Modal open={showModal} onClose={() => setShowModal(false)} title="Connect MT5">
        <div className="flex flex-col gap-md">
          <div className="flex flex-col gap-xs">
            <Label>Server</Label>
            <Input
              value={server}
              onChange={(e) => setServer(e.target.value)}
              placeholder="e.g. Example-Demo"
            />
          </div>
          <div className="flex flex-col gap-xs">
            <Label>Login ID</Label>
            <Input
              value={loginId}
              onChange={(e) => setLoginId(e.target.value)}
              placeholder="e.g. 12345678"
            />
          </div>
          <div className="flex flex-col gap-xs">
            <Label>Password</Label>
            <Input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="••••••••"
            />
          </div>
          <div className="flex items-center justify-end gap-sm pt-1">
            <Button variant="outline" onClick={() => setShowModal(false)}>
              Cancel
            </Button>
            <Button
              variant="secondary"
              onClick={() => doConnect({ server, loginId, password })}
              disabled={connecting || !server || !loginId || !password}
              className="gap-1.5"
            >
              {connecting ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : (
                <Plug className="h-4 w-4" />
              )}
              {connecting ? 'Connecting…' : 'Connect'}
            </Button>
          </div>
        </div>
      </Modal>
    </section>
  )
}
