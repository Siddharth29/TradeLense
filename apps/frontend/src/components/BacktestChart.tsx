import { useEffect, useRef, useState } from "react"
import {
  createChart,
  type CandlestickData,
  type IChartApi,
  type ISeriesApi,
  type SeriesMarker,
  type Time,
  CrosshairMode,
} from "lightweight-charts"
import { getContainerDimensions } from "@/utils/chartUtils"

interface BacktestChartProps {
  candles: CandlestickData[]
  markers?: SeriesMarker<Time>[]
  entryPrice?: number | null
  stopLoss?: number | null
  targetPrice?: number | null
  height?: number
  onChartReady?: (chart: IChartApi, series: ISeriesApi<"Candlestick">) => void
}

/**
 * Reusable backtest chart component with proper error handling and lifecycle management.
 */
export function BacktestChart({
  candles,
  markers = [],
  entryPrice,
  stopLoss,
  targetPrice,
  height = 600,
  onChartReady,
}: BacktestChartProps) {
  const containerRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<IChartApi | null>(null)
  const seriesRef = useRef<ISeriesApi<"Candlestick"> | null>(null)
  const entryPriceLineRef = useRef<any>(null)
  const stopLossLineRef = useRef<any>(null)
  const targetPriceLineRef = useRef<any>(null)
  
  const [chartError, setChartError] = useState<string | null>(null)
  const [containerDimensions, setContainerDimensions] = useState<{ width: number; height: number } | null>(null)
  const [isInitialized, setIsInitialized] = useState(false)

  // Wait for container dimensions with immediate check
  useEffect(() => {
    if (!containerRef.current) {
      return
    }

    // Try immediate dimension check first
    const immediateDims = getContainerDimensions(containerRef.current)
    if (immediateDims) {
      setContainerDimensions(immediateDims)
      setChartError(null)
      return
    }

    // If not available, use ResizeObserver without timeout
    const observer = new ResizeObserver((entries) => {
      for (const entry of entries) {
        const { width, height } = entry.contentRect
        if (width > 0 && height > 0) {
          setContainerDimensions({ width, height })
          setChartError(null)
          observer.disconnect()
          return
        }
      }
    })

    observer.observe(containerRef.current)

    return () => {
      observer.disconnect()
    }
  }, [])

  // Initialize chart once container has dimensions
  useEffect(() => {
    if (!containerRef.current || !containerDimensions) {
      return
    }
    
    // Don't reinitialize if already initialized
    if (isInitialized && chartRef.current) {
      return
    }
    
    try {
      const chart = createChart(containerRef.current, {
        width: containerDimensions.width,
        height: containerDimensions.height,
        layout: {
          background: { color: "#ffffff" },
          textColor: "#191919",
        },
        crosshair: { mode: CrosshairMode.Magnet },
        handleScroll: {
          mouseWheel: true,
          pressedMouseMove: true,
          horzTouchDrag: true,
          vertTouchDrag: true,
        },
        handleScale: {
          axisPressedMouseMove: true,
          mouseWheel: true,
          pinch: true,
        },
      })
      
      const candleSeries = chart.addCandlestickSeries({
        upColor: "#22c55e",
        downColor: "#ef4444",
        borderVisible: false,
        wickUpColor: "#22c55e",
        wickDownColor: "#ef4444"
      })
      
      chartRef.current = chart
      seriesRef.current = candleSeries
      setIsInitialized(true)
      setChartError(null)
      
      // Callback if provided
      if (onChartReady) {
        onChartReady(chart, candleSeries)
      }
      
      return () => {
        if (chartRef.current) {
          chartRef.current.remove()
          chartRef.current = null
        }
        seriesRef.current = null
        setIsInitialized(false)
      }
    } catch (err) {
      console.error("Failed to initialize chart:", err)
      setChartError(err instanceof Error ? err.message : "Failed to initialize chart")
    }
  }, [containerDimensions, isInitialized, onChartReady])

  // Update chart data when candles change
  useEffect(() => {
    if (!seriesRef.current || !candles.length || !isInitialized) {
      return
    }
    
    try {
      seriesRef.current.setData(candles)
    } catch (err) {
      console.error("Failed to set chart data:", err)
    }
  }, [candles, isInitialized])

  // Update markers when they change
  useEffect(() => {
    if (!seriesRef.current || !isInitialized) {
      return
    }
    
    try {
      seriesRef.current.setMarkers(markers)
    } catch (err) {
      console.error("Failed to set markers:", err)
    }
  }, [markers, isInitialized])

  // Add price lines for entry, stop loss, and target
  useEffect(() => {
    if (!chartRef.current || !seriesRef.current || !isInitialized) {
      return
    }
    
    try {
      // Remove existing price lines
      if (entryPriceLineRef.current) {
        seriesRef.current.removePriceLine(entryPriceLineRef.current)
        entryPriceLineRef.current = null
      }
      if (stopLossLineRef.current) {
        seriesRef.current.removePriceLine(stopLossLineRef.current)
        stopLossLineRef.current = null
      }
      if (targetPriceLineRef.current) {
        seriesRef.current.removePriceLine(targetPriceLineRef.current)
        targetPriceLineRef.current = null
      }
      
      // Add entry price line
      if (entryPrice != null && entryPrice > 0) {
        entryPriceLineRef.current = seriesRef.current.createPriceLine({
          price: entryPrice,
          color: "#3b82f6",
          lineWidth: 2,
          lineStyle: 2, // Dashed
          axisLabelVisible: true,
          title: "Entry",
        })
      }
      
      // Add stop loss line
      if (stopLoss != null && stopLoss > 0) {
        stopLossLineRef.current = seriesRef.current.createPriceLine({
          price: stopLoss,
          color: "#ef4444",
          lineWidth: 2,
          lineStyle: 0, // Solid
          axisLabelVisible: true,
          title: "Stop Loss",
        })
      }
      
      // Add target price line
      if (targetPrice != null && targetPrice > 0) {
        targetPriceLineRef.current = seriesRef.current.createPriceLine({
          price: targetPrice,
          color: "#22c55e",
          lineWidth: 2,
          lineStyle: 2, // Dashed
          axisLabelVisible: true,
          title: "Target",
        })
      }
    } catch (err) {
      console.error("Failed to update price lines:", err)
    }
  }, [entryPrice, stopLoss, targetPrice, isInitialized])

  // Handle resize
  useEffect(() => {
    if (!chartRef.current || !containerRef.current || !isInitialized) {
      return
    }
    
    const resizeObserver = new ResizeObserver((entries) => {
      for (const entry of entries) {
        const { width, height } = entry.contentRect
        if (width > 0 && height > 0 && chartRef.current) {
          chartRef.current.applyOptions({ width, height })
          setContainerDimensions({ width, height })
        }
      }
    })
    
    resizeObserver.observe(containerRef.current)
    
    return () => {
      resizeObserver.disconnect()
    }
  }, [isInitialized])

  return (
    <div className="relative w-full" style={{ height: `${height}px`, minHeight: "400px" }}>
      {chartError && (
        <div className="absolute inset-0 flex items-center justify-center bg-red-50 border border-red-200 rounded z-10">
          <div className="text-center">
            <p className="text-red-600 font-semibold">Chart Error</p>
            <p className="text-red-500 text-sm mt-1">{chartError}</p>
          </div>
        </div>
      )}
      {!candles.length && !chartError && isInitialized && (
        <div className="absolute inset-0 flex items-center justify-center bg-gray-50 border border-gray-200 rounded z-10">
          <p className="text-muted-foreground">No candle data available</p>
        </div>
      )}
      {!isInitialized && !chartError && (
        <div className="absolute inset-0 flex items-center justify-center bg-gray-50 border border-gray-200 rounded z-10">
          <p className="text-muted-foreground">Initializing chart...</p>
        </div>
      )}
      <div
        ref={containerRef}
        className="w-full h-full"
        style={{
          width: "100%",
          height: "100%",
          minWidth: "300px",
          minHeight: "400px",
        }}
      />
    </div>
  )
}

