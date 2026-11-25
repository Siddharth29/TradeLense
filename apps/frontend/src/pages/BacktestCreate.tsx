import { useState } from "react"
import { useNavigate } from "react-router-dom"
import { Card } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"

export default function BacktestCreate() {
  const navigate = useNavigate()
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const [formData, setFormData] = useState({
    test_name: "",
    asset: "BTCUSD",
    start_date: "",
    end_date: "",
    candle_count_5m: 100,
    candle_count_15m: 50,
    use_code_sniper: true,
    interval_hours: 1,
    lot_size: 1.0,
  })
  
  const ALLOWED_CANDLE_COUNTS = [30, 50, 80, 100]
  const SUPPORTED_ASSETS = ["BTCUSD", "GBPUSD", "EURUSD", "XAUUSD", "ETHUSD"]

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setLoading(true)
    setError(null)

    try {
      // Validate dates
      const startDate = new Date(formData.start_date)
      const endDate = new Date(formData.end_date)

      if (startDate >= endDate) {
        throw new Error("Start date must be before end date")
      }

      // Format dates for API
      const request = {
        ...formData,
        start_date: startDate.toISOString(),
        end_date: endDate.toISOString(),
      }

      const response = await fetch("/api/backtest/run", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify(request),
      })

      if (!response.ok) {
        const errorData = await response.json()
        throw new Error(errorData.detail || "Failed to create backtest")
      }

      const data = await response.json()

      // Navigate to backtest details
      navigate(`/backtests/${data.backtest_id}`)
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unknown error")
    } finally {
      setLoading(false)
    }
  }

  const handleChange = (field: string, value: any) => {
    setFormData((prev) => ({
      ...prev,
      [field]: value,
    }))
  }

  return (
    <div className="container mx-auto p-4 max-w-2xl">
      <div className="mb-6">
        <h1 className="text-3xl font-bold">Create New Backtest</h1>
        <p className="text-muted-foreground mt-2">
          Verify the trading lifecycle with an optional historical replay
        </p>
        <div className="mt-4 p-3 bg-blue-50 border border-blue-200 rounded">
          <p className="text-sm text-blue-800">
            Requires connected mode, a configured AI provider, and historical CSVs for the selected asset.
            Choose dates covered by your files and allow earlier candles for indicator warm-up.
          </p>
        </div>
      </div>

      <Card className="p-6">
        <form onSubmit={handleSubmit} className="space-y-6">
          {/* Test Name */}
          <div className="space-y-2">
            <Label htmlFor="test_name">Test Name *</Label>
            <Input
              id="test_name"
              value={formData.test_name}
              onChange={(e) => handleChange("test_name", e.target.value)}
              placeholder="e.g., BTCUSD engineering replay"
              required
            />
          </div>

          {/* Asset */}
          <div className="space-y-2">
            <Label htmlFor="asset">Asset Symbol *</Label>
            <Select
              value={formData.asset}
              onValueChange={(value) => handleChange("asset", value)}
            >
              <SelectTrigger id="asset">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {SUPPORTED_ASSETS.map((a) => (
                  <SelectItem key={a} value={a}>{a}</SelectItem>
                ))}
              </SelectContent>
            </Select>
            <p className="text-xs text-muted-foreground">
              GBPUSD / EURUSD use pip-based PnL. CSV data required for each asset.
            </p>
          </div>

          {/* Date Range */}
          <div className="grid grid-cols-2 gap-4">
            <div className="space-y-2">
              <Label htmlFor="start_date">Start Date *</Label>
              <Input
                id="start_date"
                type="datetime-local"
                value={formData.start_date}
                onChange={(e) => handleChange("start_date", e.target.value)}
                required
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="end_date">End Date *</Label>
              <Input
                id="end_date"
                type="datetime-local"
                value={formData.end_date}
                onChange={(e) => handleChange("end_date", e.target.value)}
                required
              />
            </div>
          </div>

          {/* Candle Counts - Dropdown Selection */}
          <div className="grid grid-cols-2 gap-4">
            <div className="space-y-2">
              <Label htmlFor="candle_count_5m">5m Candles *</Label>
              <Select
                value={formData.candle_count_5m.toString()}
                onValueChange={(value) =>
                  handleChange("candle_count_5m", parseInt(value))
                }
              >
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {ALLOWED_CANDLE_COUNTS.map((count) => (
                    <SelectItem key={count} value={count.toString()}>
                      {count} candles
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <p className="text-xs text-muted-foreground">
                Number of 5-minute candles for analysis
              </p>
            </div>
            <div className="space-y-2">
              <Label htmlFor="candle_count_15m">15m Candles *</Label>
              <Select
                value={formData.candle_count_15m.toString()}
                onValueChange={(value) =>
                  handleChange("candle_count_15m", parseInt(value))
                }
              >
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {ALLOWED_CANDLE_COUNTS.map((count) => (
                    <SelectItem key={count} value={count.toString()}>
                      {count} candles
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <p className="text-xs text-muted-foreground">
                Number of 15-minute candles for analysis
              </p>
            </div>
          </div>

          {/* Interval Hours */}
          <div className="space-y-2">
            <Label htmlFor="interval_hours">Iteration Interval (hours)</Label>
            <Input
              id="interval_hours"
              type="number"
              value={formData.interval_hours}
              onChange={(e) =>
                handleChange("interval_hours", parseInt(e.target.value))
              }
              min={1}
              max={24}
            />
            <p className="text-xs text-muted-foreground">
              Hours between backtest iterations (default: 1)
            </p>
          </div>

          {/* Lot Size */}
          <div className="space-y-2">
            <Label htmlFor="lot_size">Lot Size</Label>
            <Input
              id="lot_size"
              type="number"
              value={formData.lot_size}
              onChange={(e) =>
                handleChange("lot_size", parseFloat(e.target.value))
              }
              min={0.01}
              step={0.01}
            />
            <p className="text-xs text-muted-foreground">
              1.0 = 1 standard lot. Forex: $10/pip/lot. BTCUSD: $1/point/lot.
            </p>
          </div>

          {error && (
            <div className="p-4 bg-red-50 border border-red-200 rounded">
              <p className="text-red-600 text-sm">{error}</p>
            </div>
          )}

          <div className="flex gap-4">
            <Button type="submit" disabled={loading}>
              {loading ? "Creating..." : "Create Backtest"}
            </Button>
            <Button
              type="button"
              variant="outline"
              onClick={() => navigate("/backtests")}
            >
              Cancel
            </Button>
          </div>
        </form>
      </Card>
    </div>
  )
}

