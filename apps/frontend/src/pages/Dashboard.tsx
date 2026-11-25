import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  ResponsiveContainer,
  AreaChart,
  Area,
  BarChart,
  Bar,
  XAxis,
  YAxis,
  Tooltip,
  CartesianGrid,
  Cell,
  LabelList,
  ReferenceLine,
} from "recharts";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { MetricCard } from "@/components/insights/MetricCard";
import { Wallet, TrendingUp, Coins, Target, Bot, BarChart3 } from "lucide-react";
import { getSystemMetrics, getAgentMetrics } from "@/services/apiClient";
import type { BookedPnl, SystemMetrics, Metrics as AgentMetrics } from "@/types/api";

const POS = "#10b981"; // emerald-500
const NEG = "#ef4444"; // red-500
const ACCENT = "#6366f1"; // indigo-500

// Compact USD formatter, sign-aware.
const money = (n: number): string =>
  `${n < 0 ? "-" : ""}$${Math.abs(n).toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;

// HH:MM (IST) tick formatter for intraday time axes.
const fmtTime = (t: string): string => {
  const d = new Date(t);
  if (isNaN(d.getTime())) return "";
  return d.toLocaleTimeString("en-IN", {
    timeZone: "Asia/Kolkata",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
};

export default function Dashboard() {
  const { data: systemMetrics } = useQuery<SystemMetrics>({
    queryKey: ["systemMetrics"],
    queryFn: getSystemMetrics,
    refetchInterval: 15000,
  });
  const { data: agentMetrics } = useQuery<Record<string, AgentMetrics>>({
    queryKey: ["agentMetrics"],
    queryFn: getAgentMetrics,
    refetchInterval: 15000,
  });

  const stats = useMemo(() => {
    const ms = agentMetrics ? Object.values(agentMetrics) : [];
    const sum = (f: (m: AgentMetrics) => number) => ms.reduce((s, m) => s + (f(m) || 0), 0);
    const totalPnl = sum((m) => m.pnl);
    const wins = sum((m) => m.wins);
    const losses = sum((m) => m.losses);
    const closed = sum((m) => m.closed_trades ?? (m.wins + m.losses));
    const winRate = wins + losses ? (wins / (wins + losses)) * 100 : 0;
    const open = sum((m) => m.open_trades ?? 0);
    const running = ms.filter((m) => m.running).length;
    return { totalPnl, wins, losses, closed, winRate, open, running, total: ms.length };
  }, [agentMetrics]);

  const equityData = systemMetrics?.equity_curve ?? [];
  const account = systemMetrics?.account_pnl;
  const dailyPnlData = account?.daily_curve ?? [];
  const accountMoney = (n: number) => n.toLocaleString("en-US", {
    style: "currency", currency: account!.currency, minimumFractionDigits: 2, maximumFractionDigits: 2,
  });
  const breakdown = (p: BookedPnl) => `Gross ${accountMoney(p.gross)} · Charges ${accountMoney(p.charges)}`;
  const total = account ? account.all_time.net + account.floating : null;
  const dailyUp = dailyPnlData.length ? dailyPnlData[dailyPnlData.length - 1].pnl >= 0 : true;
  const dailyColor = dailyUp ? POS : NEG;

  const agentPnlData = useMemo(
    () =>
      agentMetrics
        ? Object.entries(agentMetrics)
            .map(([name, m]) => ({
              name,
              pnl: m.pnl || 0,
              realized: m.realized_pnl ?? 0,
              unrealized: m.unrealized_pnl ?? 0,
            }))
            .sort((a, b) => b.pnl - a.pnl)
        : [],
    [agentMetrics]
  );

  return (
    <div className="space-y-4">
      {/* Header */}
      <div className="flex items-center justify-between gap-2">
        <h1 className="text-xl font-bold">Dashboard</h1>
        <div
          className="flex shrink-0 items-center gap-1 rounded-full bg-secondary/50 px-2 py-0.5 text-[11px] text-muted-foreground"
          title="Live · refreshes every 15s"
          aria-label="Live · refreshes every 15 seconds"
        >
          <span className="h-1.5 w-1.5 rounded-full bg-green-500" aria-hidden="true" />
          Live · 15s
        </div>
      </div>

      {/* KPI cards */}
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-6">
        <MetricCard
          title="Total account P&L"
          value={total === null ? "—" : accountMoney(total)}
          subtitle={account ? `${breakdown(account.all_time)} · Booked ${accountMoney(account.all_time.net)}` : "MT5 history unavailable"}
          color={total === null ? "default" : total >= 0 ? "success" : "danger"}
          icon={<Wallet className="h-4 w-4 text-muted-foreground" />}
        />
        <MetricCard
          title="Today’s booked P&L · IST"
          value={account ? accountMoney(account.today.net) : "—"}
          subtitle={account ? breakdown(account.today) : "MT5 history unavailable"}
          color={!account ? "default" : account.today.net >= 0 ? "success" : "danger"}
          icon={<TrendingUp className="h-4 w-4 text-muted-foreground" />}
        />
        <MetricCard
          title="Unrealized P&L"
          value={account ? accountMoney(account.floating) : "—"}
          subtitle="Included in total account P&L"
          color={!account ? "default" : account.floating >= 0 ? "success" : "danger"}
          icon={<Coins className="h-4 w-4 text-muted-foreground" />}
        />
        <MetricCard
          title="Win Rate"
          value={`${stats.winRate.toFixed(1)}%`}
          subtitle={`Agent trades · ${stats.wins}W / ${stats.losses}L`}
          icon={<Target className="h-4 w-4 text-muted-foreground" />}
        />
        <MetricCard
          title="Running Agents"
          value={`${stats.running} / ${stats.total}`}
          icon={<Bot className="h-4 w-4 text-muted-foreground" />}
        />
        <MetricCard
          title="Closed agent trades"
          value={stats.closed}
          subtitle={`${stats.open} open`}
          icon={<BarChart3 className="h-4 w-4 text-muted-foreground" />}
        />
      </div>

      {/* Equity + Daily P&L */}
      <div className="grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium">
              Equity Curve <span className="text-xs font-normal text-muted-foreground">· today (IST)</span>
            </CardTitle>
          </CardHeader>
          <CardContent>
            <div className="h-56">
              {equityData.length ? (
                <ResponsiveContainer width="100%" height="100%">
                  <AreaChart data={equityData} margin={{ left: 4, right: 8, top: 4, bottom: 0 }}>
                    <defs>
                      <linearGradient id="grad-equity" x1="0" y1="0" x2="0" y2="1">
                        <stop offset="0%" stopColor={ACCENT} stopOpacity={0.35} />
                        <stop offset="100%" stopColor={ACCENT} stopOpacity={0} />
                      </linearGradient>
                    </defs>
                    <CartesianGrid strokeDasharray="3 3" opacity={0.12} />
                    <XAxis dataKey="time" tickFormatter={fmtTime} minTickGap={44} fontSize={11} tickLine={false} axisLine={false} />
                    <YAxis width={64} fontSize={11} tickLine={false} axisLine={false} domain={["auto", "auto"]} />
                    <Tooltip content={(p) => <TsTooltip {...p} label2="Equity" />} />
                    <Area type="monotone" dataKey="equity" stroke={ACCENT} strokeWidth={2} fill="url(#grad-equity)" isAnimationActive={false} />
                  </AreaChart>
                </ResponsiveContainer>
              ) : (
                <EmptyChart label="No equity samples yet today" />
              )}
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium">
              Cumulative P&L <span className="text-xs font-normal text-muted-foreground">· today (realized)</span>
            </CardTitle>
          </CardHeader>
          <CardContent>
            <div className="h-56">
              {dailyPnlData.length ? (
                <ResponsiveContainer width="100%" height="100%">
                  <AreaChart data={dailyPnlData} margin={{ left: 4, right: 8, top: 4, bottom: 0 }}>
                    <defs>
                      <linearGradient id="grad-pnl" x1="0" y1="0" x2="0" y2="1">
                        <stop offset="0%" stopColor={dailyColor} stopOpacity={0.35} />
                        <stop offset="100%" stopColor={dailyColor} stopOpacity={0} />
                      </linearGradient>
                    </defs>
                    <CartesianGrid strokeDasharray="3 3" opacity={0.12} />
                    <XAxis dataKey="time" tickFormatter={fmtTime} minTickGap={44} fontSize={11} tickLine={false} axisLine={false} />
                    <YAxis width={64} fontSize={11} tickLine={false} axisLine={false} />
                    <Tooltip content={(p) => <TsTooltip {...p} label2="Booked P&L" format={accountMoney} />} />
                    <ReferenceLine y={0} stroke="currentColor" opacity={0.25} />
                    <Area type="monotone" dataKey="pnl" stroke={dailyColor} strokeWidth={2} fill="url(#grad-pnl)" isAnimationActive={false} />
                  </AreaChart>
                </ResponsiveContainer>
              ) : (
                <EmptyChart label={account ? "No trading deals posted today" : "MT5 history unavailable"} />
              )}
            </div>
          </CardContent>
        </Card>
      </div>

      {/* Agent performance */}
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-sm font-medium">
            Agent Performance <span className="text-xs font-normal text-muted-foreground">· net P&L by asset</span>
            {agentMetrics && <span className="float-right">{money(stats.totalPnl)}</span>}
          </CardTitle>
        </CardHeader>
        <CardContent>
          <div style={{ height: Math.max(180, agentPnlData.length * 46) }}>
            {agentPnlData.length ? (
              <ResponsiveContainer width="100%" height="100%">
                <BarChart layout="vertical" data={agentPnlData} margin={{ left: 8, right: 56, top: 4, bottom: 4 }}>
                  <CartesianGrid horizontal={false} strokeDasharray="3 3" opacity={0.12} />
                  <XAxis type="number" fontSize={11} tickLine={false} axisLine={false} tickFormatter={(v) => money(v)} />
                  <YAxis type="category" dataKey="name" width={88} fontSize={12} tickLine={false} axisLine={false} />
                  <Tooltip cursor={{ fill: "currentColor", opacity: 0.05 }} content={(p) => <AgentTooltip {...p} />} />
                  <ReferenceLine x={0} stroke="currentColor" opacity={0.3} />
                  <Bar dataKey="pnl" radius={[0, 4, 4, 0]} isAnimationActive={false} barSize={22}>
                    {agentPnlData.map((d, i) => (
                      <Cell key={i} fill={d.pnl >= 0 ? POS : NEG} />
                    ))}
                    <LabelList dataKey="pnl" position="right" formatter={(v: number) => money(v)} fontSize={11} />
                  </Bar>
                </BarChart>
              </ResponsiveContainer>
            ) : (
              <EmptyChart label="No agent activity yet" />
            )}
          </div>
        </CardContent>
      </Card>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Tooltips + empty state
// ---------------------------------------------------------------------------

function EmptyChart({ label }: { label: string }) {
  return (
    <div className="flex h-full items-center justify-center text-sm text-muted-foreground">{label}</div>
  );
}

function TsTooltip({ active, payload, label, label2, format = money }: any) {
  if (!active || !payload?.length) return null;
  const v = payload[0].value as number;
  return (
    <div className="rounded-md border bg-popover px-3 py-2 text-xs shadow-md">
      <div className="mb-0.5 text-muted-foreground">{fmtTime(label)}</div>
      <div className="font-semibold">
        {label2}: {format(v)}
      </div>
    </div>
  );
}

function AgentTooltip({ active, payload }: any) {
  if (!active || !payload?.length) return null;
  const d = payload[0].payload as { name: string; pnl: number; realized: number; unrealized: number };
  const row = (k: string, val: number, colored = false) => (
    <div className="flex justify-between gap-6">
      <span className="text-muted-foreground">{k}</span>
      <span className={colored ? (val >= 0 ? "text-green-600" : "text-red-600") : ""}>{money(val)}</span>
    </div>
  );
  return (
    <div className="rounded-md border bg-popover px-3 py-2 text-xs shadow-md">
      <div className="mb-1 font-semibold">{d.name}</div>
      {row("Net P&L", d.pnl, true)}
      {row("Realized", d.realized)}
      {row("Unrealized", d.unrealized)}
    </div>
  );
}
