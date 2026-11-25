
interface SparklineProps {
  data: number[]
  width?: number
  height?: number
  color?: string
  className?: string
}

export function Sparkline({ 
  data, 
  width = 60, 
  height = 20, 
  color = '#3b82f6',
  className = ''
}: SparklineProps) {
  if (!data || data.length === 0) {
    return (
      <div 
        className={`flex items-center justify-center text-xs text-muted-foreground ${className}`}
        style={{ width, height }}
      >
        No data
      </div>
    )
  }

  const min = Math.min(...data)
  const max = Math.max(...data)
  const range = max - min || 1

  // Normalize data to 0-1 range
  const normalizedData = data.map(value => (value - min) / range)
  
  // Create SVG path
  const points = normalizedData.map((value, index) => {
    const x = (index / (normalizedData.length - 1)) * width
    const y = height - (value * height)
    return `${x},${y}`
  }).join(' ')

  const pathData = `M ${points}`

  return (
    <div className={`inline-block ${className}`}>
      <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`}>
        <path
          d={pathData}
          fill="none"
          stroke={color}
          strokeWidth="1.5"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
        {/* Optional: Add area fill */}
        <path
          d={`M 0,${height} L ${points} L ${width},${height} Z`}
          fill={color}
          fillOpacity="0.1"
        />
      </svg>
    </div>
  )
}

interface SparklineWithValueProps extends SparklineProps {
  currentValue: number
  unit?: string
  showValue?: boolean
}

export function SparklineWithValue({ 
  currentValue, 
  unit = '', 
  showValue = true,
  ...sparklineProps 
}: SparklineWithValueProps) {
  return (
    <div className="flex items-center gap-2">
      <Sparkline {...sparklineProps} />
      {showValue && (
        <span className="text-xs font-mono">
          {currentValue.toFixed(2)}{unit}
        </span>
      )}
    </div>
  )
}
