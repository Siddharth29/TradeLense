import { useState, useEffect } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { 
  Loader2, 
  FileText, 
  ArrowLeft,
  AlertTriangle,
  TrendingUp,
  TrendingDown,
  Minus,
  CheckCircle,
  XCircle,
  Shield,
  Target,
  Brain,
  Zap
} from 'lucide-react'
import { getEnhancedGptResponses } from '@/services/apiClient'
import type { EnhancedGptResponse } from '@/types/api'
import { 
  formatLatency, 
  formatConfidence, 
  calculateRR,
  validateRR,
  validateTTL,
  validateActiveWindows,
  parseResponseContent,
  calculateDecisionCompleteness
} from '@/lib/format'

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
  return price.toFixed(4)
}

const getSignalIcon = (signal: string | null | undefined) => {
  if (!signal) return <Minus className="h-4 w-4" />
  if (signal.toLowerCase() === 'buy') return <TrendingUp className="h-4 w-4 text-green-600" />
  if (signal.toLowerCase() === 'sell') return <TrendingDown className="h-4 w-4 text-red-600" />
  return <Minus className="h-4 w-4 text-gray-600" />
}

const getValidationIcon = (status: 'valid' | 'invalid' | 'warning') => {
  switch (status) {
    case 'valid': return <CheckCircle className="h-4 w-4 text-green-500" />
    case 'invalid': return <XCircle className="h-4 w-4 text-red-500" />
    case 'warning': return <AlertTriangle className="h-4 w-4 text-yellow-500" />
  }
}

export default function AiDecisionDetails() {
  const { responseId } = useParams<{ responseId: string }>()
  const navigate = useNavigate()
  const [response, setResponse] = useState<EnhancedGptResponse | null>(null)

  // Fetch response data
  const { data: responsesData, isLoading, error } = useQuery({
    queryKey: ['enhanced-gpt-responses', responseId],
    queryFn: () => getEnhancedGptResponses('ALL', 100, 0), // Get more to find the specific response
    staleTime: 30000,
    refetchOnWindowFocus: false,
  })

  // Find the specific response by ID
  useEffect(() => {
    if (responsesData?.responses && responseId) {
      const foundResponse = responsesData.responses.find(r => r._id === responseId)
      if (foundResponse) {
        setResponse(foundResponse)
      }
    }
  }, [responsesData, responseId])

  // Enhanced validation functions
  const getResponseValidations = (response: EnhancedGptResponse) => {
    const validations: Array<{name: string, status: 'valid' | 'invalid' | 'warning', message: string}> = []
    
    try {
      if (!response.parsed_decision) {
        validations.push({
          name: 'Decision Parsing',
          status: 'invalid',
          message: 'No parsed decision available'
        })
        return validations
      }

      const decision = response.parsed_decision
      
      const completeness = calculateDecisionCompleteness(decision)
      if (completeness.score < 1) {
        validations.push({
          name: 'Decision Completeness',
          status: 'invalid',
          message: `Missing fields: ${completeness.missing.join(', ')}`
        })
      } else {
        validations.push({
          name: 'Decision Completeness',
          status: 'valid',
          message: 'All required fields present'
        })
      }

      if (decision.entry !== null && decision.stop !== null && decision.target !== null && decision.signal) {
        try {
          const rr = calculateRR(decision.entry, decision.stop, decision.target, decision.signal)
          const rrValidation = validateRR(rr)
          validations.push({
            name: 'Risk-Reward Ratio',
            status: rrValidation.valid ? 'valid' : 'invalid',
            message: rrValidation.message
          })
        } catch (error) {
          validations.push({
            name: 'Risk-Reward Ratio',
            status: 'warning',
            message: `RR calculation failed: ${error instanceof Error ? error.message : 'Unknown error'}`
          })
        }
      }

      const ttlValidation = validateTTL(decision.ttl_minutes)
      validations.push({
        name: 'TTL Validation',
        status: ttlValidation.valid ? 'valid' : 'invalid',
        message: ttlValidation.message
      })

      const windowValidation = validateActiveWindows(decision.active_window_c5, decision.active_window_c15)
      validations.push({
        name: 'Active Windows',
        status: windowValidation.valid ? 'valid' : 'invalid',
        message: windowValidation.message
      })

      if (response.response_type === 'json') {
        try {
          parseResponseContent(response.response_content, response.response_type)
          validations.push({
            name: 'JSON Parsing',
            status: 'valid',
            message: 'Response content parsed successfully'
          })
        } catch (error) {
          validations.push({
            name: 'JSON Parsing',
            status: 'invalid',
            message: `Parse error: ${error instanceof Error ? error.message : 'Unknown error'}`
          })
        }
      }

    } catch (error) {
      validations.push({
        name: 'Validation Error',
        status: 'warning',
        message: `Validation failed: ${error instanceof Error ? error.message : 'Unknown error'}`
      })
    }

    return validations
  }

  const getResponseValidity = (response: EnhancedGptResponse): 'valid' | 'invalid' | 'warning' => {
    const validations = getResponseValidations(response)
    const hasInvalid = validations.some(v => v.status === 'invalid')
    const hasWarning = validations.some(v => v.status === 'warning')
    
    if (hasInvalid) return 'invalid'
    if (hasWarning) return 'warning'
    return 'valid'
  }

  const getResponseRR = (response: EnhancedGptResponse): number | null => {
    try {
      if (!response.parsed_decision) return null
      const { entry, stop, target, signal } = response.parsed_decision
      if (entry === null || stop === null || target === null || !signal) return null
      return calculateRR(entry, stop, target, signal)
    } catch {
      return null
    }
  }

  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-64">
        <Loader2 className="h-8 w-8 animate-spin" />
        <span className="ml-2">Loading AI decision details...</span>
      </div>
    )
  }

  if (error || !response) {
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
            {error ? `Error loading response: ${error}` : `AI decision with ID ${responseId} not found`}
          </AlertDescription>
        </Alert>
      </div>
    )
  }

  const validity = getResponseValidity(response)
  const rr = getResponseRR(response)
  const validations = getResponseValidations(response)

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
            <h1 className="text-3xl font-bold text-foreground">AI Decision Details</h1>
            <p className="text-muted-foreground">
              GPT Response ID: {response._id}
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

      {/* Decision Overview */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-foreground">
            <Brain className="h-5 w-5" />
            Decision Overview
          </CardTitle>
          <CardDescription className="text-muted-foreground">
            Basic information about this AI trading decision
          </CardDescription>
        </CardHeader>
        <CardContent>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4 text-sm">
            <div className="text-foreground">
              <strong>Asset:</strong> 
              <Badge variant="outline" className="ml-2">{response.asset}</Badge>
            </div>
            <div className="text-foreground">
              <strong>Mode:</strong> 
              <Badge variant="secondary" className="ml-2">{response.gpt_mode || 'normal'}</Badge>
            </div>
            <div className="text-foreground">
              <strong>Model:</strong> 
              <Badge variant="outline" className="ml-2">{response.model}</Badge>
            </div>
            <div className="text-foreground">
              <strong>Status:</strong> 
              <Badge 
                variant={validity === 'valid' ? 'default' : 
                       validity === 'invalid' ? 'destructive' : 'secondary'}
                className="ml-2"
              >
                {getValidationIcon(validity)}
                <span className="ml-1">{validity}</span>
              </Badge>
            </div>
            <div className="text-foreground">
              <strong>Timestamp:</strong> {convertUTCtoIST(response.timestamp)}
            </div>
            <div className="text-foreground">
              <strong>Latency:</strong> 
              <Badge variant="outline" className="ml-2">
                <Zap className="h-3 w-3 mr-1" />
                {formatLatency(response.latency_ms)}
              </Badge>
            </div>
            <div className="text-foreground">
              <strong>Tokens:</strong> 
              <Badge variant="outline" className="ml-2">
                {response.tokens_used?.toLocaleString() || 'N/A'}
              </Badge>
            </div>
            <div className="text-foreground">
              <strong>Confidence:</strong> 
              <Badge variant="outline" className="ml-2">
                {formatConfidence(response.confidence || response.parsed_decision?.confidence || 0)}
              </Badge>
            </div>
          </div>
        </CardContent>
      </Card>

      {/* Trading Decision */}
      {response.parsed_decision && (
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-foreground">
            <Target className="h-5 w-5" />
            Trading Decision
          </CardTitle>
          <CardDescription className="text-muted-foreground">
            Parsed trading signal with entry, stop, and target levels
          </CardDescription>
        </CardHeader>
          <CardContent>
            <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
              <div className="text-center p-4 bg-blue-50 dark:bg-blue-950/20 rounded-lg">
                <div className="flex items-center justify-center gap-2 mb-2">
                  {getSignalIcon(response.parsed_decision.signal)}
                  <span className="font-semibold text-lg">{response.parsed_decision.signal || 'flat'}</span>
                </div>
                <div className="text-sm text-muted-foreground">Signal Direction</div>
              </div>
              <div className="text-center p-4 bg-green-50 dark:bg-green-950/20 rounded-lg">
                <div className="text-lg font-semibold">{formatPrice(response.parsed_decision.entry)}</div>
                <div className="text-sm text-muted-foreground">Entry Price</div>
              </div>
              <div className="text-center p-4 bg-purple-50 dark:bg-purple-950/20 rounded-lg">
                <div className="text-lg font-semibold">{formatPrice(response.parsed_decision.target)}</div>
                <div className="text-sm text-muted-foreground">Target Price</div>
              </div>
            </div>
            
            <div className="grid grid-cols-1 md:grid-cols-3 gap-4 mt-6">
              <div className="text-center p-3 bg-red-50 dark:bg-red-950/20 rounded-lg">
                <div className="text-lg font-semibold">{formatPrice(response.parsed_decision.stop)}</div>
                <div className="text-sm text-muted-foreground">Stop Loss</div>
              </div>
              <div className="text-center p-3 bg-yellow-50 dark:bg-yellow-950/20 rounded-lg">
                <div className="text-lg font-semibold">
                  {rr ? rr.toFixed(2) : 'N/A'}
                </div>
                <div className="text-sm text-muted-foreground">Risk/Reward Ratio</div>
              </div>
              <div className="text-center p-3 bg-gray-50 dark:bg-gray-950/20 rounded-lg">
                <div className="text-lg font-semibold">{response.parsed_decision.ttl_minutes || 'N/A'}</div>
                <div className="text-sm text-muted-foreground">TTL (minutes)</div>
              </div>
            </div>

            <div className="flex flex-wrap gap-2 mt-4">
              <Badge variant="outline">C5 Window: {response.parsed_decision.active_window_c5 || 'N/A'}</Badge>
              <Badge variant="outline">C15 Window: {response.parsed_decision.active_window_c15 || 'N/A'}</Badge>
            </div>

            {response.parsed_decision.rationale && (
              <div className="mt-6 p-4 bg-blue-50 dark:bg-blue-950/20 rounded-lg">
                <h5 className="font-semibold mb-2 text-blue-600 dark:text-blue-400">AI Rationale</h5>
                <p className="text-sm italic">"{response.parsed_decision.rationale}"</p>
              </div>
            )}
          </CardContent>
        </Card>
      )}

      {/* Validation Results */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-foreground">
            <Shield className="h-5 w-5" />
            Validation Results
          </CardTitle>
          <CardDescription className="text-muted-foreground">
            Comprehensive validation of the AI decision quality and completeness
          </CardDescription>
        </CardHeader>
        <CardContent>
          <div className="space-y-3">
            {validations.map((validation, index) => (
              <div key={index} className="flex items-center justify-between p-3 border rounded-lg">
                <div className="flex items-center gap-3">
                  {getValidationIcon(validation.status)}
                  <span className="font-medium">{validation.name}</span>
                </div>
                <div className="text-right">
                  <Badge 
                    variant={validation.status === 'valid' ? 'default' : 
                           validation.status === 'invalid' ? 'destructive' : 'secondary'}
                  >
                    {validation.status}
                  </Badge>
                  <p className="text-xs text-muted-foreground mt-1">{validation.message}</p>
                </div>
              </div>
            ))}
          </div>
        </CardContent>
      </Card>

      {/* Response Content - Only show if it's different from parsed decision */}
      {response.response_content && response.response_type !== 'json' && (
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-foreground">
              <FileText className="h-5 w-5" />
              Raw Response Content
            </CardTitle>
            <CardDescription className="text-muted-foreground">
              Original AI response content from the model
            </CardDescription>
          </CardHeader>
          <CardContent>
            <div className="bg-gray-50 dark:bg-gray-950/20 p-4 rounded-lg">
              <pre className="whitespace-pre-wrap text-sm font-mono overflow-auto max-h-96">
                {response.response_content}
              </pre>
            </div>
          </CardContent>
        </Card>
      )}

      {/* Error Information */}
      {response.error_message && (
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-red-600 dark:text-red-400">
              <AlertTriangle className="h-5 w-5" />
              Error Information
            </CardTitle>
            <CardDescription className="text-muted-foreground">
              Error details from the AI decision process
            </CardDescription>
          </CardHeader>
          <CardContent>
            <Alert variant="destructive">
              <AlertTriangle className="h-4 w-4" />
              <AlertDescription>
                <strong>Error:</strong> {response.error_message}
              </AlertDescription>
            </Alert>
          </CardContent>
        </Card>
      )}
    </div>
  )
}
