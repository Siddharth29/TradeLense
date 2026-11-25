import { useParams, Navigate } from "react-router-dom"

// Visual chart view has been removed.
// Redirect to the backtest details page.
export default function BacktestVisual() {
  const { backtestId } = useParams<{ backtestId: string }>()
  return <Navigate to={`/backtests/${backtestId}`} replace />
}
