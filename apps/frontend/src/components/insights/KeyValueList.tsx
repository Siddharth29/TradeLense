import React from 'react'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'

interface KeyValueItem {
  key: string
  value: any
  type?: 'text' | 'number' | 'currency' | 'percentage' | 'badge' | 'code' | 'json'
  color?: 'default' | 'success' | 'warning' | 'danger'
  description?: string
}

interface KeyValueListProps {
  title: string
  items: KeyValueItem[]
  icon?: React.ReactNode
  className?: string
  columns?: 1 | 2 | 3
}

export function KeyValueList({ 
  title, 
  items, 
  icon,
  className = '',
  columns = 2
}: KeyValueListProps) {
  const formatValue = (item: KeyValueItem): React.ReactNode => {
    const { value, type = 'text', color = 'default' } = item

    if (value === null || value === undefined) {
      return <span className="text-muted-foreground italic">Not set</span>
    }

    switch (type) {
      case 'number':
        return (
          <span className="font-mono">
            {typeof value === 'number' ? value.toFixed(4) : value}
          </span>
        )
      case 'currency':
        return (
          <span className="font-mono">
            ${typeof value === 'number' ? value.toFixed(2) : value}
          </span>
        )
      case 'percentage':
        return (
          <span className="font-mono">
            {typeof value === 'number' ? (value * 100).toFixed(0) : value}%
          </span>
        )
      case 'badge':
        return (
          <Badge 
            variant={color === 'success' ? 'default' : 
                     color === 'warning' ? 'secondary' :
                     color === 'danger' ? 'destructive' : 'outline'}
            className={
              color === 'success' ? 'bg-green-100 text-green-800' :
              color === 'warning' ? 'bg-yellow-100 text-yellow-800' :
              color === 'danger' ? 'bg-red-100 text-red-800' : ''
            }
          >
            {String(value)}
          </Badge>
        )
      case 'code':
        return (
          <code className="bg-muted px-2 py-1 rounded text-sm font-mono">
            {String(value)}
          </code>
        )
      case 'json':
        return (
          <pre className="bg-muted p-2 rounded text-xs font-mono overflow-auto max-h-32">
            {JSON.stringify(value, null, 2)}
          </pre>
        )
      default:
        return <span>{String(value)}</span>
    }
  }

  const getGridCols = () => {
    switch (columns) {
      case 1: return 'grid-cols-1'
      case 2: return 'grid-cols-1 md:grid-cols-2'
      case 3: return 'grid-cols-1 md:grid-cols-2 lg:grid-cols-3'
      default: return 'grid-cols-1 md:grid-cols-2'
    }
  }

  return (
    <Card className={className}>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          {icon}
          {title}
        </CardTitle>
      </CardHeader>
      <CardContent>
        <div className={`grid ${getGridCols()} gap-4`}>
          {items.map((item, index) => (
            <div key={index} className="space-y-1">
              <div className="text-sm font-medium text-muted-foreground">
                {item.key}
              </div>
              <div className="text-sm">
                {formatValue(item)}
              </div>
              {item.description && (
                <div className="text-xs text-muted-foreground">
                  {item.description}
                </div>
              )}
            </div>
          ))}
        </div>
      </CardContent>
    </Card>
  )
}
