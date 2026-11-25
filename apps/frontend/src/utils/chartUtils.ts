import type { CandlestickData } from "lightweight-charts"
import type { BacktestCandle } from "@/types/api"
import { convertToChartTime } from "./timeUtils"

/**
 * Convert BacktestCandle array to Lightweight Charts CandlestickData format.
 * Validates and filters out invalid candles.
 */
export function convertCandlesToChartData(
  candles: BacktestCandle[]
): CandlestickData[] {
  if (!candles || !Array.isArray(candles)) {
    console.warn("Invalid candles input - not an array")
    return []
  }
  
  const validCandles: CandlestickData[] = []
  
  for (const candle of candles) {
    // Validate required fields
    if (!candle || typeof candle !== "object") {
      console.warn("Invalid candle object:", candle)
      continue
    }
    
    const { time, open, high, low, close } = candle
    
    // Convert time
    const chartTime = convertToChartTime(time)
    if (!chartTime) {
      console.warn(`Invalid time for candle: ${time}`)
      continue
    }
    
    // Validate numeric fields
    const o = typeof open === "number" ? open : parseFloat(String(open))
    const h = typeof high === "number" ? high : parseFloat(String(high))
    const l = typeof low === "number" ? low : parseFloat(String(low))
    const c = typeof close === "number" ? close : parseFloat(String(close))
    
    if (isNaN(o) || isNaN(h) || isNaN(l) || isNaN(c)) {
      console.warn("Invalid numeric values in candle:", candle)
      continue
    }
    
    // Validate OHLC logic (high >= low, high >= open/close, low <= open/close)
    if (h < l || h < Math.max(o, c) || l > Math.min(o, c)) {
      console.warn("Invalid OHLC values (high/low/open/close mismatch):", candle)
      continue
    }
    
    validCandles.push({
      time: chartTime,
      open: o,
      high: h,
      low: l,
      close: c
    })
  }
  
  // Sort by time to ensure chronological order
  validCandles.sort((a, b) => (a.time as number) - (b.time as number))
  
  console.log(`Converted ${validCandles.length} valid candles from ${candles.length} input candles`)
  
  return validCandles
}

/**
 * Get container dimensions, ensuring valid width and height.
 */
export function getContainerDimensions(
  container: HTMLElement | null
): { width: number; height: number } | null {
  if (!container) return null
  
  const width = container.clientWidth || container.offsetWidth
  const height = container.clientHeight || container.offsetHeight
  
  // Ensure minimum dimensions
  if (width <= 0 || height <= 0) {
    console.warn(`Invalid container dimensions: ${width}x${height}`)
    return null
  }
  
  return { width, height }
}

/**
 * Wait for container to have valid dimensions.
 */
export function waitForContainerDimensions(
  container: HTMLElement | null,
  timeoutMs: number = 5000
): Promise<{ width: number; height: number }> {
  return new Promise((resolve, reject) => {
    if (!container) {
      reject(new Error("Container is null"))
      return
    }
    
    // Check immediately
    const dims = getContainerDimensions(container)
    if (dims) {
      resolve(dims)
      return
    }
    
    // Use ResizeObserver to wait for dimensions
    const observer = new ResizeObserver((entries) => {
      for (const entry of entries) {
        const { width, height } = entry.contentRect
        if (width > 0 && height > 0) {
          observer.disconnect()
          clearTimeout(timeoutId)
          resolve({ width, height })
          return
        }
      }
    })
    
    observer.observe(container)
    
    // Timeout fallback
    const timeoutId = setTimeout(() => {
      observer.disconnect()
      reject(new Error(`Container dimensions not available after ${timeoutMs}ms`))
    }, timeoutMs)
  })
}

