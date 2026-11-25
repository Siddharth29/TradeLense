import React from 'react'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { TrendingUp, TrendingDown, Minus } from 'lucide-react'

interface MetricCardProps {
  title: string
  value: string | number
  subtitle?: string
  trend?: 'up' | 'down' | 'neutral'
  trendValue?: string
  icon?: React.ReactNode
  className?: string
  color?: 'default' | 'success' | 'warning' | 'danger'
}

export function MetricCard({
  title,
  value,
  subtitle,
  trend,
  trendValue,
  icon,
  className = '',
  color = 'default'
}: MetricCardProps) {
  const getTrendIcon = () => {
    switch (trend) {
      case 'up': return <TrendingUp className="h-3 w-3 text-green-600" />
      case 'down': return <TrendingDown className="h-3 w-3 text-red-600" />
      case 'neutral': return <Minus className="h-3 w-3 text-gray-600" />
      default: return null
    }
  }

  const getColorClasses = () => {
    switch (color) {
      case 'success': return 'text-green-600'
      case 'warning': return 'text-yellow-600'
      case 'danger': return 'text-red-600'
      default: return 'text-foreground'
    }
  }

  return (
    <Card className={`${className}`}>
      <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
        <CardTitle className="text-sm font-medium text-muted-foreground">
          {title}
        </CardTitle>
        {icon}
      </CardHeader>
      <CardContent>
        <div className={`text-2xl font-bold ${getColorClasses()}`}>
          {value}
        </div>
        {(subtitle || trend) && (
          <div className="flex items-center space-x-2 text-xs text-muted-foreground mt-1">
            {subtitle && <span>{subtitle}</span>}
            {trend && (
              <div className="flex items-center space-x-1">
                {getTrendIcon()}
                {trendValue && <span>{trendValue}</span>}
              </div>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  )
}
