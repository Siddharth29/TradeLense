import { useMemo } from "react"
import { Link } from "react-router-dom"
import { toast } from "sonner"
import { Card } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { Badge } from "@/components/ui/badge"
import {
  useBacktestList,
  useStopBacktest,
  useRestartBacktest,
  useDeleteBacktest,
} from "@/services/hooks"
import { BacktestListSkeleton } from "@/components/BacktestListSkeleton"

export default function BacktestList() {
  // Smart polling: refetchInterval function checks data for running backtests
  const {
    data: backtests,
    isLoading,
    isFetching,
    error,
    refetch,
  } = useBacktestList({
    refetchInterval: (query) => {
      const data = query.state.data
      const hasRunning = data?.some(
        (bt) => bt.status === "running" || bt.status === "pending"
      )
      return hasRunning ? 5000 : false
    },
  })

  // Smart polling indicator: only when running/pending backtests exist
  const hasRunningBacktests = useMemo(() => {
    return backtests?.some(
      (bt) => bt.status === "running" || bt.status === "pending"
    ) ?? false
  }, [backtests])

  const stopMutation = useStopBacktest()
  const restartMutation = useRestartBacktest()
  const deleteMutation = useDeleteBacktest()

  const handleStop = async (backtestId: string) => {
    if (!confirm("Are you sure you want to stop this backtest?")) {
      return
    }

    try {
      await stopMutation.mutateAsync(backtestId)
      toast.success("Backtest stopped successfully")
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Failed to stop backtest")
    }
  }

  const handleRestart = async (backtestId: string) => {
    if (!confirm("Are you sure you want to restart this backtest?")) {
      return
    }

    try {
      await restartMutation.mutateAsync(backtestId)
      toast.success("Backtest restarted successfully")
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Failed to restart backtest")
    }
  }

  const handleDelete = async (backtestId: string) => {
    if (!confirm("Are you sure you want to delete this backtest?")) {
      return
    }

    try {
      await deleteMutation.mutateAsync(backtestId)
      toast.success("Backtest deleted successfully")
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Failed to delete backtest")
    }
  }

  const getStatusColor = (status: string) => {
    switch (status) {
      case "completed":
        return "bg-green-500"
      case "running":
        return "bg-blue-500"
      case "failed":
        return "bg-red-500"
      case "pending":
        return "bg-yellow-500"
      default:
        return "bg-gray-500"
    }
  }

  const formatDate = (dateStr: string) => {
    try {
      return new Date(dateStr).toLocaleDateString()
    } catch {
      return dateStr
    }
  }

  const formatPnL = (pnl: number | null | undefined) => {
    if (pnl == null || isNaN(pnl)) {
      return "0.00"
    }
    const formatted = pnl.toFixed(2)
    return pnl >= 0 ? `+${formatted}` : formatted
  }

  const handleExportCSV = async () => {
    if (!backtests || backtests.length === 0) {
      toast.error("No backtests to export")
      return
    }

    try {
      const { unparse } = await import('papaparse')
      
      const csvData = backtests.map((bt) => ({
        'Backtest ID': bt.backtest_id,
        'Test Name': bt.test_name,
        'Asset': bt.asset,
        'Start Date': bt.start_date,
        'End Date': bt.end_date,
        'Status': bt.status,
        'Iterations': bt.iterations,
        'Total Signals': bt.total_signals,
        'Wins': bt.wins ?? 0,
        'Losses': bt.losses ?? 0,
        'Total P&L': bt.total_pnl ?? 0,
        'Max Drawdown': bt.max_drawdown ?? 0,
        'Win Rate': bt.win_rate != null && !isNaN(bt.win_rate) ? `${(bt.win_rate * 100).toFixed(2)}%` : '0.00%',
        'Avg R:R': bt.avg_rr ?? 0,
        'Created At': bt.created_at,
        'Completed At': bt.completed_at ?? '',
        'Error Message': bt.error_message ?? '',
      }))

      const csv = unparse(csvData)
      const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `backtests_${new Date().toISOString().split('T')[0]}.csv`
      a.click()
      URL.revokeObjectURL(url)
      toast.success("CSV exported successfully")
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Failed to export CSV")
    }
  }

  if (isLoading) {
    return <BacktestListSkeleton />
  }

  if (error) {
    return (
      <div className="container mx-auto p-4">
        <Card className="p-4">
          <p className="text-red-500">Error: {error.message}</p>
          <Button onClick={() => refetch()} className="mt-4">
            Retry
          </Button>
        </Card>
      </div>
    )
  }

  return (
    <div className="container mx-auto p-4 space-y-4">
      <div className="flex justify-between items-center">
        <h1 className="text-3xl font-bold">Backtests</h1>
        <div className="flex gap-2">
          <Button
            variant="outline"
            onClick={() => refetch()}
            disabled={isFetching}
          >
            {isFetching ? "Refreshing..." : "🔄 Refresh"}
          </Button>
          {backtests && backtests.length > 0 && (
            <Button
              variant="outline"
              onClick={handleExportCSV}
              disabled={isLoading}
            >
              📥 Export CSV
            </Button>
          )}
          {hasRunningBacktests && (
            <Badge variant="outline" className="bg-blue-100 text-blue-800">
              🔴 Live - Auto-refreshing every 5 seconds
            </Badge>
          )}
          <Link to="/backtests/create">
            <Button>Create New Backtest</Button>
          </Link>
        </div>
      </div>

      {!backtests || backtests.length === 0 ? (
        <Card className="p-8 text-center">
          <p className="text-muted-foreground mb-4">
            No backtests found. Create your first backtest to get started.
          </p>
          <Link to="/backtests/create">
            <Button>Create Backtest</Button>
          </Link>
        </Card>
      ) : (
        <Card>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Test Name</TableHead>
                <TableHead>Asset</TableHead>
                <TableHead>Date Range</TableHead>
                <TableHead>Status</TableHead>
                <TableHead className="text-right">Iterations</TableHead>
                <TableHead className="text-right">Signals</TableHead>
                <TableHead className="text-right">Win Rate</TableHead>
                <TableHead className="text-right">Total P&L</TableHead>
                <TableHead className="text-right">Actions</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {backtests.map((backtest) => (
                <TableRow key={backtest.backtest_id}>
                  <TableCell className="font-medium">
                    {backtest.test_name}
                  </TableCell>
                  <TableCell>{backtest.asset}</TableCell>
                  <TableCell>
                    {formatDate(backtest.start_date)} -{" "}
                    {formatDate(backtest.end_date)}
                  </TableCell>
                  <TableCell>
                    <Badge className={getStatusColor(backtest.status)}>
                      {backtest.status}
                    </Badge>
                  </TableCell>
                  <TableCell className="text-right">
                    {backtest.iterations}
                  </TableCell>
                  <TableCell className="text-right">
                    {backtest.total_signals}
                  </TableCell>
                  <TableCell className="text-right">
                    {backtest.win_rate != null && !isNaN(backtest.win_rate)
                      ? `${(backtest.win_rate * 100).toFixed(1)}%`
                      : "0.0%"}
                  </TableCell>
                  <TableCell
                    className={`text-right font-mono ${
                      (backtest.total_pnl ?? 0) >= 0
                        ? "text-green-600"
                        : "text-red-600"
                    }`}
                  >
                    {formatPnL(backtest.total_pnl)}
                  </TableCell>
                  <TableCell className="text-right">
                    <div className="flex justify-end gap-2">
                      <Link to={`/backtests/${backtest.backtest_id}`}>
                        <Button size="sm" variant="outline">
                          View
                        </Button>
                      </Link>
                      
                      {/* Stop button - only for running/pending */}
                      {(backtest.status === "running" || backtest.status === "pending") && (
                        <Button
                          size="sm"
                          variant="outline"
                          className="text-orange-600 hover:text-orange-700"
                          onClick={() => handleStop(backtest.backtest_id)}
                          disabled={stopMutation.isPending}
                        >
                          {stopMutation.isPending ? "Stopping..." : "Stop"}
                        </Button>
                      )}
                      
                      {/* Restart button - only for failed/completed */}
                      {(backtest.status === "failed" || backtest.status === "completed") && (
                        <Button
                          size="sm"
                          variant="outline"
                          className="text-blue-600 hover:text-blue-700"
                          onClick={() => handleRestart(backtest.backtest_id)}
                          disabled={restartMutation.isPending}
                        >
                          {restartMutation.isPending ? "Restarting..." : "Restart"}
                        </Button>
                      )}
                      
                      <Button
                        size="sm"
                        variant="destructive"
                        onClick={() => handleDelete(backtest.backtest_id)}
                        disabled={deleteMutation.isPending}
                      >
                        {deleteMutation.isPending ? "Deleting..." : "Delete"}
                      </Button>
                    </div>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </Card>
      )}
    </div>
  )
}
