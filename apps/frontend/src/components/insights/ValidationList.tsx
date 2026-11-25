import React from 'react'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { StatusBadge } from './StatusBadge'
import { CheckCircle, XCircle, AlertTriangle } from 'lucide-react'

interface ValidationItem {
  name: string
  status: 'valid' | 'invalid' | 'warning'
  message: string
  details?: string
}

interface ValidationListProps {
  title: string
  validations: ValidationItem[]
  icon?: React.ReactNode
  className?: string
}

export function ValidationList({ 
  title, 
  validations, 
  icon,
  className = '' 
}: ValidationListProps) {
  const validCount = validations.filter(v => v.status === 'valid').length
  const invalidCount = validations.filter(v => v.status === 'invalid').length
  const warningCount = validations.filter(v => v.status === 'warning').length

  const getOverallStatus = (): 'valid' | 'invalid' | 'warning' | 'neutral' => {
    if (invalidCount > 0) return 'invalid'
    if (warningCount > 0) return 'warning'
    if (validCount > 0) return 'valid'
    return 'neutral'
  }

  const getStatusIcon = () => {
    switch (getOverallStatus()) {
      case 'valid': return <CheckCircle className="h-5 w-5 text-green-600" />
      case 'invalid': return <XCircle className="h-5 w-5 text-red-600" />
      case 'warning': return <AlertTriangle className="h-5 w-5 text-yellow-600" />
      default: return null
    }
  }

  return (
    <Card className={className}>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          {icon || getStatusIcon()}
          {title}
          <span className="text-sm font-normal text-muted-foreground">
            ({validCount} valid, {invalidCount} invalid, {warningCount} warnings)
          </span>
        </CardTitle>
      </CardHeader>
      <CardContent>
        <div className="space-y-3">
          {validations.map((validation, index) => (
            <div key={index} className="flex items-start justify-between gap-3">
              <div className="flex-1 min-w-0">
                <div className="font-medium text-sm">{validation.name}</div>
                <div className="text-sm text-muted-foreground">
                  {validation.message}
                </div>
                {validation.details && (
                  <div className="text-xs text-muted-foreground mt-1 font-mono bg-muted px-2 py-1 rounded">
                    {validation.details}
                  </div>
                )}
              </div>
              <StatusBadge 
                status={validation.status} 
                label={validation.status.toUpperCase()}
                size="sm"
              />
            </div>
          ))}
        </div>
      </CardContent>
    </Card>
  )
}
