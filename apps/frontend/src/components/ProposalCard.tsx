import React from 'react'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { CheckCircle, XCircle, TrendingUp, TrendingDown, Minus } from 'lucide-react'

interface Proposal {
  signal: 'buy' | 'sell' | 'flat'
  entry: number | null
  stop: number | null
  target: number | null
  confidence: number
  ttl_minutes: number
  active_window_c5: number
  active_window_c15: number
  rationale?: string
}

interface ValidationMetrics {
  rr?: number
  anti_scalp?: boolean
  inside_window?: boolean
  is_valid?: boolean
  errors?: string[]
}

interface ProposalCardProps {
  proposal: Proposal
  validation?: ValidationMetrics
  index: number
  isSelected?: boolean
  onClick?: () => void
}

export const ProposalCard: React.FC<ProposalCardProps> = ({
  proposal,
  validation,
  index,
  isSelected = false,
  onClick
}) => {
  const getSignalIcon = (signal: string) => {
    switch (signal) {
      case 'buy':
        return <TrendingUp className="h-4 w-4 text-green-600" />
      case 'sell':
        return <TrendingDown className="h-4 w-4 text-red-600" />
      default:
        return <Minus className="h-4 w-4 text-gray-400" />
    }
  }

  const getSignalBadgeVariant = (signal: string) => {
    switch (signal) {
      case 'buy':
        return 'default'
      case 'sell':
        return 'destructive'
      default:
        return 'secondary'
    }
  }

  const formatPrice = (price: number | null) => {
    return price !== null ? price.toFixed(2) : 'N/A'
  }

  const calculateRR = (): string | null => {
    if (!proposal.entry || !proposal.stop || !proposal.target) return null
    const risk = Math.abs(proposal.entry - proposal.stop)
    const reward = Math.abs(proposal.target - proposal.entry)
    return risk > 0 ? (reward / risk).toFixed(2) : null
  }

  const rr = validation?.rr !== undefined ? String(validation.rr.toFixed(2)) : calculateRR()

  return (
    <Card 
      className={`cursor-pointer transition-all ${
        isSelected 
          ? 'ring-2 ring-primary shadow-lg' 
          : 'hover:shadow-md'
      } ${validation?.is_valid === false ? 'border-red-300' : ''}`}
      onClick={onClick}
    >
      <CardHeader className="pb-3">
        <div className="flex items-center justify-between">
          <CardTitle className="text-sm font-medium flex items-center gap-2">
            {getSignalIcon(proposal.signal)}
            Proposal {index + 1}
            {isSelected && (
              <Badge variant="default" className="ml-2">Selected</Badge>
            )}
          </CardTitle>
          <Badge variant={getSignalBadgeVariant(proposal.signal)}>
            {proposal.signal.toUpperCase()}
          </Badge>
        </div>
      </CardHeader>
      <CardContent className="space-y-3">
        {/* Price Levels */}
        {proposal.signal !== 'flat' && (
          <div className="grid grid-cols-3 gap-2 text-sm">
            <div>
              <div className="text-muted-foreground text-xs">Entry</div>
              <div className="font-mono font-semibold">{formatPrice(proposal.entry)}</div>
            </div>
            <div>
              <div className="text-muted-foreground text-xs">Stop</div>
              <div className="font-mono font-semibold text-red-600">{formatPrice(proposal.stop)}</div>
            </div>
            <div>
              <div className="text-muted-foreground text-xs">Target</div>
              <div className="font-mono font-semibold text-green-600">{formatPrice(proposal.target)}</div>
            </div>
          </div>
        )}

        {/* Metrics */}
        <div className="grid grid-cols-2 gap-2 text-sm">
          <div>
            <div className="text-muted-foreground text-xs">Confidence</div>
            <div className="font-semibold">{(Number(proposal.confidence) * 100).toFixed(0)}%</div>
          </div>
          {rr && (
            <div>
              <div className="text-muted-foreground text-xs">RR Ratio</div>
              <div className={`font-semibold ${Number(rr) >= 2 ? 'text-green-600' : 'text-red-600'}`}>
                {rr}
              </div>
            </div>
          )}
        </div>

        {/* Validation Status */}
        {validation && (
          <div className="space-y-1 pt-2 border-t">
            <div className="text-xs font-semibold text-muted-foreground mb-1">Validation</div>
            <div className="flex flex-wrap gap-2">
              {validation.rr !== undefined && (
                <div className="flex items-center gap-1 text-xs">
                  {validation.rr >= 2 ? (
                    <CheckCircle className="h-3 w-3 text-green-600" />
                  ) : (
                    <XCircle className="h-3 w-3 text-red-600" />
                  )}
                  <span>RR: {validation.rr.toFixed(2)}</span>
                </div>
              )}
              {validation.anti_scalp !== undefined && (
                <div className="flex items-center gap-1 text-xs">
                  {validation.anti_scalp ? (
                    <CheckCircle className="h-3 w-3 text-green-600" />
                  ) : (
                    <XCircle className="h-3 w-3 text-red-600" />
                  )}
                  <span>Anti-scalp</span>
                </div>
              )}
              {validation.inside_window !== undefined && (
                <div className="flex items-center gap-1 text-xs">
                  {validation.inside_window ? (
                    <CheckCircle className="h-3 w-3 text-green-600" />
                  ) : (
                    <XCircle className="h-3 w-3 text-red-600" />
                  )}
                  <span>Window</span>
                </div>
              )}
            </div>
            {validation.errors && validation.errors.length > 0 && (
              <div className="text-xs text-red-600 mt-1">
                {validation.errors.join(', ')}
              </div>
            )}
          </div>
        )}

        {/* Rationale */}
        {proposal.rationale && (
          <div className="pt-2 border-t">
            <div className="text-xs text-muted-foreground line-clamp-2">
              {proposal.rationale}
            </div>
          </div>
        )}

        {/* Windows Info */}
        <div className="flex gap-2 text-xs text-muted-foreground pt-1">
          <span>C5: {proposal.active_window_c5}</span>
          <span>C15: {proposal.active_window_c15}</span>
          <span>TTL: {proposal.ttl_minutes}m</span>
        </div>
      </CardContent>
    </Card>
  )
}

