import React from 'react'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { CheckCircle, XCircle, AlertTriangle } from 'lucide-react'

interface ValidationMetrics {
  rr?: number
  anti_scalp?: boolean
  inside_window?: boolean
  is_valid?: boolean
  errors?: string[]
  metrics?: Record<string, any>
}

interface ValidationMetricsProps {
  validation: ValidationMetrics
  proposalIndex?: number
}

export const ValidationMetrics: React.FC<ValidationMetricsProps> = ({
  validation,
  proposalIndex
}) => {
  const getStatusIcon = (passed: boolean) => {
    return passed ? (
      <CheckCircle className="h-4 w-4 text-green-600" />
    ) : (
      <XCircle className="h-4 w-4 text-red-600" />
    )
  }

  const hasValidation = validation.rr !== undefined || 
                        validation.anti_scalp !== undefined || 
                        validation.inside_window !== undefined

  if (!hasValidation && !validation.errors?.length) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="text-sm flex items-center gap-2">
            <AlertTriangle className="h-4 w-4 text-yellow-600" />
            {proposalIndex !== undefined ? `Proposal ${proposalIndex + 1} ` : ''}Validation
          </CardTitle>
        </CardHeader>
        <CardContent>
          <p className="text-sm text-muted-foreground">No validation data available</p>
        </CardContent>
      </Card>
    )
  }

  const overallPassed = validation.is_valid !== false && 
                        (validation.rr === undefined || validation.rr >= 2) &&
                        (validation.anti_scalp === undefined || validation.anti_scalp === true) &&
                        (validation.inside_window === undefined || validation.inside_window === true)

  return (
    <Card className={overallPassed ? 'border-green-200' : 'border-red-200'}>
      <CardHeader>
        <div className="flex items-center justify-between">
          <CardTitle className="text-sm flex items-center gap-2">
            {getStatusIcon(overallPassed)}
            {proposalIndex !== undefined ? `Proposal ${proposalIndex + 1} ` : ''}Validation
          </CardTitle>
          <Badge variant={overallPassed ? 'default' : 'destructive'}>
            {overallPassed ? 'Passed' : 'Failed'}
          </Badge>
        </div>
      </CardHeader>
      <CardContent className="space-y-3">
        {/* Validation Checks */}
        <div className="space-y-2">
          {validation.rr !== undefined && (
            <div className="flex items-center justify-between p-2 rounded-md bg-muted/50">
              <div className="flex items-center gap-2">
                {getStatusIcon(validation.rr >= 2)}
                <span className="text-sm font-medium">Risk-Reward Ratio</span>
              </div>
              <span className={`text-sm font-mono ${validation.rr >= 2 ? 'text-green-600' : 'text-red-600'}`}>
                {validation.rr.toFixed(2)}
              </span>
            </div>
          )}

          {validation.anti_scalp !== undefined && (
            <div className="flex items-center justify-between p-2 rounded-md bg-muted/50">
              <div className="flex items-center gap-2">
                {getStatusIcon(validation.anti_scalp)}
                <span className="text-sm font-medium">Anti-Scalp Check</span>
              </div>
              <span className={`text-sm ${validation.anti_scalp ? 'text-green-600' : 'text-red-600'}`}>
                {validation.anti_scalp ? 'Passed' : 'Failed'}
              </span>
            </div>
          )}

          {validation.inside_window !== undefined && (
            <div className="flex items-center justify-between p-2 rounded-md bg-muted/50">
              <div className="flex items-center gap-2">
                {getStatusIcon(validation.inside_window)}
                <span className="text-sm font-medium">Window Validation</span>
              </div>
              <span className={`text-sm ${validation.inside_window ? 'text-green-600' : 'text-red-600'}`}>
                {validation.inside_window ? 'Inside' : 'Outside'}
              </span>
            </div>
          )}
        </div>

        {/* Error Messages */}
        {validation.errors && validation.errors.length > 0 && (
          <div className="pt-2 border-t">
            <div className="text-xs font-semibold text-red-600 mb-1">Validation Errors:</div>
            <ul className="space-y-1">
              {validation.errors.map((error, idx) => (
                <li key={idx} className="text-xs text-red-600 flex items-start gap-1">
                  <span>•</span>
                  <span>{error}</span>
                </li>
              ))}
            </ul>
          </div>
        )}

        {/* Additional Metrics */}
        {validation.metrics && Object.keys(validation.metrics).length > 0 && (
          <div className="pt-2 border-t">
            <div className="text-xs font-semibold text-muted-foreground mb-1">Additional Metrics:</div>
            <div className="grid grid-cols-2 gap-2">
              {Object.entries(validation.metrics).map(([key, value]) => (
                <div key={key} className="text-xs">
                  <span className="text-muted-foreground">{key}:</span>{' '}
                  <span className="font-mono">{JSON.stringify(value)}</span>
                </div>
              ))}
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  )
}

