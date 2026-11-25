import { useState, useEffect } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { 
  Loader2, 
  Brain, 
  Target,
  AlertTriangle,
  ArrowLeft,
  TrendingUp,
  TrendingDown,
  Minus,
  CheckCircle,
  XCircle,
  Timer
} from 'lucide-react'
import { getMultiExpertCycles } from '@/services/apiClient'
import type { MultiExpertCycle } from '@/types/api'

// Utility function to convert UTC timestamp to IST
const convertUTCtoIST = (timestamp: string): string => {
  try {
    if (!timestamp || timestamp === 'undefined' || timestamp === 'null') {
      return 'Invalid Date'
    }
    
    const date = new Date(timestamp)
    
    if (isNaN(date.getTime())) {
      return 'Invalid Date'
    }
    
    return date.toLocaleString('en-IN', {
      timeZone: 'Asia/Kolkata',
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
      hour12: true
    })
  } catch (error) {
    console.error('Error converting timestamp:', error)
    return 'Invalid Date'
  }
}

const formatPrice = (price: number | null | undefined): string => {
  if (price === null || price === undefined) return 'N/A'
  return price.toFixed(2)
}

const formatConfidence = (confidence: number | undefined): string => {
  if (confidence === undefined) return 'N/A'
  return `${(confidence * 100).toFixed(1)}%`
}

const getSignalIcon = (signal: string | null | undefined) => {
  if (!signal) return <Minus className="h-4 w-4" />
  if (signal.toLowerCase() === 'buy') return <TrendingUp className="h-4 w-4 text-green-600" />
  if (signal.toLowerCase() === 'sell') return <TrendingDown className="h-4 w-4 text-red-600" />
  return <Minus className="h-4 w-4 text-gray-600" />
}

export default function CycleDetails() {
  const { cycleId } = useParams<{ cycleId: string }>()
  const navigate = useNavigate()
  const [cycle, setCycle] = useState<MultiExpertCycle | null>(null)

  // Fetch cycle data
  const { data: cyclesData, isLoading, error } = useQuery({
    queryKey: ['multi-expert-cycles', cycleId],
    queryFn: () => getMultiExpertCycles(
      undefined, // agent_id
      undefined, // asset
      undefined, // mode
      undefined, // status
      undefined, // start_time
      undefined, // end_time
      100, // limit - get more to find the specific cycle
      0 // skip
    ),
    staleTime: 30000,
    refetchOnWindowFocus: false,
  })

  // Find the specific cycle by ID
  useEffect(() => {
    if (cyclesData?.cycles && cycleId) {
      const foundCycle = cyclesData.cycles.find(c => c.cycle_id === cycleId)
      if (foundCycle) {
        setCycle(foundCycle)
      }
    }
  }, [cyclesData, cycleId])

  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-64">
        <Loader2 className="h-8 w-8 animate-spin" />
        <span className="ml-2">Loading cycle details...</span>
      </div>
    )
  }

  if (error || !cycle) {
    return (
      <div className="space-y-4">
        <div className="flex items-center gap-4">
          <Button variant="outline" onClick={() => navigate('/unified-analysis')}>
            <ArrowLeft className="h-4 w-4 mr-2" />
            Back to Analysis
          </Button>
        </div>
        <Alert>
          <AlertTriangle className="h-4 w-4" />
          <AlertDescription>
            {error ? `Error loading cycle: ${error}` : `Cycle with ID ${cycleId} not found`}
          </AlertDescription>
        </Alert>
      </div>
    )
  }

  const calculateDuration = (): string => {
    if (!cycle.completed_at || !cycle.started_at) return 'N/A'
    const duration = Math.round(
      (new Date(cycle.completed_at).getTime() - new Date(cycle.started_at).getTime()) / 1000
    )
    return `${duration}s`
  }

  const getStatusIcon = (status: string) => {
    switch (status) {
      case 'success': return <CheckCircle className="h-4 w-4 text-green-500" />
      case 'failed': return <XCircle className="h-4 w-4 text-red-500" />
      default: return <AlertTriangle className="h-4 w-4 text-yellow-500" />
    }
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-4">
          <Button variant="outline" onClick={() => navigate('/unified-analysis')}>
            <ArrowLeft className="h-4 w-4 mr-2" />
            Back to Analysis
          </Button>
          <div>
            <h1 className="text-3xl font-bold text-foreground">Multi-Expert Cycle Details</h1>
            <p className="text-muted-foreground">
              Cycle ID: {cycle.cycle_id}
            </p>
          </div>
        </div>
      </div>

      {/* Static Back Button */}
      <div className="fixed bottom-6 right-6 z-50">
        <Button 
          variant="outline" 
          size="lg"
          onClick={() => navigate('/unified-analysis')}
          className="shadow-lg bg-background/95 backdrop-blur supports-[backdrop-filter]:bg-background/60 border"
        >
          <ArrowLeft className="h-4 w-4 mr-2" />
          Back to Analysis
        </Button>
      </div>

      {/* Cycle Overview */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-foreground">
            <Brain className="h-5 w-5" />
            Cycle Overview
          </CardTitle>
          <CardDescription className="text-muted-foreground">
            Basic information about this multi-expert decision cycle
          </CardDescription>
        </CardHeader>
        <CardContent>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4 text-sm">
            <div className="text-foreground">
              <strong>Asset:</strong> 
              <Badge variant="outline" className="ml-2">{cycle.asset}</Badge>
            </div>
            <div className="text-foreground">
              <strong>Mode:</strong> 
              <Badge variant="secondary" className="ml-2">{cycle.mode}</Badge>
            </div>
            <div className="text-foreground">
              <strong>Status:</strong> 
              <Badge 
                variant={cycle.status === 'success' ? 'default' : 
                       cycle.status === 'failed' ? 'destructive' : 'secondary'}
                className="ml-2"
              >
                {getStatusIcon(cycle.status)}
                <span className="ml-1">{cycle.status}</span>
              </Badge>
            </div>
            <div className="text-foreground">
              <strong>Version:</strong> {cycle.version}
            </div>
            <div className="text-foreground">
              <strong>Started:</strong> {convertUTCtoIST(cycle.started_at)}
            </div>
            <div className="text-foreground">
              <strong>Completed:</strong> {cycle.completed_at ? convertUTCtoIST(cycle.completed_at) : 'N/A'}
            </div>
            <div className="text-foreground">
              <strong>Duration:</strong> 
              <Badge variant="outline" className="ml-2">
                <Timer className="h-3 w-3 mr-1" />
                {calculateDuration()}
              </Badge>
            </div>
            <div className="text-foreground">
              <strong>Error:</strong>
              {cycle.error ? (
                <div className="mt-2 p-3 bg-red-50 dark:bg-red-950/20 rounded-lg border border-red-200 dark:border-red-800">
                  <p className="text-sm text-red-700 dark:text-red-300 whitespace-pre-wrap">{cycle.error}</p>
                </div>
              ) : (
                <span className="ml-2">None</span>
              )}
            </div>
          </div>
        </CardContent>
      </Card>

      {/* Expert analysis removed: only final judge output is displayed below */}

      {/* Judge Decision */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-foreground">
            <Target className="h-5 w-5" />
            Judge Decision {cycle.judge ? '(Called)' : '(Skipped)'}
          </CardTitle>
          <CardDescription className="text-muted-foreground">
            Final decision maker that coordinates expert proposals and selects the best strategy
          </CardDescription>
        </CardHeader>
        <CardContent>
          {cycle.judge ? (
            <div className="space-y-6">
              <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                <div>
                  <strong>Selected Expert:</strong>
                  <Badge variant="default" className="ml-2">
                    {cycle.judge.selected || 'No Selection'}
                  </Badge>
                </div>
                <div>
                  <strong>Status:</strong> 
                  <Badge variant="default" className="ml-2">
                    Judge Called
                  </Badge>
                </div>
              </div>
              
              {cycle.judge.rationale && (
                <div>
                  <h5 className="font-semibold mb-3 text-green-600 dark:text-green-400">Judge Rationale</h5>
                  <div className="p-4 bg-green-50 dark:bg-green-950/20 rounded-lg">
                    <p className="text-sm">"{cycle.judge.rationale}"</p>
                  </div>
                </div>
              )}

              {cycle.judge.final_signal && (
                <div>
                  <h5 className="font-semibold mb-3 text-blue-600 dark:text-blue-400">Final Signal</h5>
                  <div className="grid grid-cols-1 md:grid-cols-3 gap-4 p-4 bg-blue-50 dark:bg-blue-950/20 rounded-lg">
                    <div className="text-center">
                      <div className="flex items-center justify-center gap-2 mb-2">
                        {getSignalIcon(cycle.judge.final_signal.signal)}
                        <span className="font-semibold text-lg">{cycle.judge.final_signal.signal || 'flat'}</span>
                      </div>
                      <div className="text-sm text-muted-foreground">Final Signal</div>
                    </div>
                    <div className="text-center">
                      <div className="text-lg font-semibold">{formatPrice(cycle.judge.final_signal.entry)}</div>
                      <div className="text-sm text-muted-foreground">Entry Price</div>
                    </div>
                    <div className="text-center">
                      <div className="text-lg font-semibold">{formatPrice(cycle.judge.final_signal.target)}</div>
                      <div className="text-sm text-muted-foreground">Target Price</div>
                    </div>
                  </div>
                  <div className="grid grid-cols-1 md:grid-cols-3 gap-4 mt-4">
                    <div className="text-center">
                      <div className="text-lg font-semibold">{formatPrice(cycle.judge.final_signal.stop)}</div>
                      <div className="text-sm text-muted-foreground">Stop Loss</div>
                    </div>
                    <div className="text-center">
                      <div className="text-lg font-semibold">{formatConfidence(cycle.judge.final_signal.confidence)}</div>
                      <div className="text-sm text-muted-foreground">Confidence</div>
                    </div>
                    <div className="text-center">
                      <div className="text-lg font-semibold">{cycle.judge.final_signal.ttl_minutes || 'N/A'} min</div>
                      <div className="text-sm text-muted-foreground">Time to Live</div>
                    </div>
                  </div>
                  <div className="flex flex-wrap gap-2 mt-4">
                    <Badge variant="outline">C5 Window: {cycle.judge.final_signal.active_window_c5 || 'N/A'}</Badge>
                    <Badge variant="outline">C15 Window: {cycle.judge.final_signal.active_window_c15 || 'N/A'}</Badge>
                  </div>
                  {cycle.judge.final_signal.rationale && (
                    <div className="mt-4 p-3 bg-blue-50 dark:bg-blue-950/20 rounded-lg">
                      <p className="text-sm italic">"{cycle.judge.final_signal.rationale}"</p>
                    </div>
                  )}
                </div>
              )}

              {cycle.judge.validation_summary && Object.keys(cycle.judge.validation_summary).length > 0 && (
                <div>
                  <h5 className="font-semibold mb-3 text-gray-600 dark:text-gray-400">Validation Summary</h5>
                  <div className="overflow-x-auto">
                    <table className="w-full text-sm">
                      <thead>
                        <tr className="border-b">
                          <th className="text-left p-2">Expert</th>
                          <th className="text-left p-2">Signal</th>
                          <th className="text-left p-2">Valid</th>
                          <th className="text-left p-2">Confidence</th>
                          <th className="text-left p-2">RR</th>
                          <th className="text-left p-2">Anti-Scalp</th>
                          <th className="text-left p-2">Window</th>
                        </tr>
                      </thead>
                      <tbody>
                        {Object.entries(cycle.judge.validation_summary).map(([key, proposal]: [string, any]) => (
                          <tr key={key} className="border-b hover:bg-gray-50">
                            <td className="p-2">
                              <Badge variant="outline">{proposal.expert_kind}</Badge>
                            </td>
                            <td className="p-2">
                              <div className="flex items-center gap-1">
                                {getSignalIcon(proposal.signal)}
                                <span>{proposal.signal || 'flat'}</span>
                              </div>
                            </td>
                            <td className="p-2">
                              <Badge variant={proposal.is_valid ? 'default' : 'destructive'}>
                                {proposal.is_valid ? 'Yes' : 'No'}
                              </Badge>
                            </td>
                            <td className="p-2">{formatConfidence(proposal.confidence)}</td>
                            <td className="p-2">
                              {proposal.metrics?.rr ? (
                                <Badge variant={proposal.metrics.rr >= 2 ? 'default' : 'secondary'}>
                                  {proposal.metrics.rr.toFixed(2)}
                                </Badge>
                              ) : 'N/A'}
                            </td>
                            <td className="p-2">
                              {proposal.metrics?.anti_scalp !== undefined ? (
                                <Badge variant={proposal.metrics.anti_scalp ? 'default' : 'destructive'}>
                                  {proposal.metrics.anti_scalp ? 'Yes' : 'No'}
                                </Badge>
                              ) : 'N/A'}
                            </td>
                            <td className="p-2">
                              {proposal.metrics?.inside_window !== undefined ? (
                                <Badge variant={proposal.metrics.inside_window ? 'default' : 'secondary'}>
                                  {proposal.metrics.inside_window ? 'Yes' : 'No'}
                                </Badge>
                              ) : 'N/A'}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>
              )}
            </div>
          ) : (
            <div className="text-center py-12">
              <AlertTriangle className="h-16 w-16 text-yellow-500 mx-auto mb-4" />
              <h3 className="text-xl font-semibold mb-2">Judge Skipped</h3>
              <p className="text-muted-foreground max-w-md mx-auto">
                Judge was not called for this cycle. This may happen based on specific conditions or expert validation results.
              </p>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  )
}
