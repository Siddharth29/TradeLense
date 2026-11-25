import React, { useState } from 'react'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { 
  Brain, 
  GitBranch, 
  CheckCircle, 
  Scale, 
  ArrowRight, 
  Clock,
  ChevronDown,
  ChevronUp,
  Code
} from 'lucide-react'
import { ProposalCard } from './ProposalCard'
import type { MultiExpertCycle } from '@/types/api'

interface CycleFlowDiagramProps {
  cycle: MultiExpertCycle
}

export const CycleFlowDiagram: React.FC<CycleFlowDiagramProps> = ({ cycle }) => {
  const [expandedExpert, setExpandedExpert] = useState(false)
  const [expandedProposals, setExpandedProposals] = useState(false)
  const [expandedJudge, setExpandedJudge] = useState(false)
  const [selectedProposal, setSelectedProposal] = useState<number | null>(null)

  // Expert data is no longer stored - only judge output is available
  const proposals: any[] = []  // No expert proposals stored
  const candelCount = 20  // Default value

  // Calculate durations
  const totalDuration = cycle.completed_at && cycle.started_at
    ? Math.round((new Date(cycle.completed_at).getTime() - new Date(cycle.started_at).getTime()) / 1000)
    : null

  // Expert duration not available (experts not stored)
  const expertDuration = null
  const judgeDuration = cycle.judge?.validation_summary?.judge_ms 
    ? Math.round(cycle.judge.validation_summary.judge_ms)
    : null

  const isCodeSniper = cycle.mode === 'multi_expert_code_sniper'

  return (
    <div className="space-y-4">
      {/* Timeline Header */}
      <div className="flex items-center justify-center gap-2 text-sm text-muted-foreground">
        <Badge variant="outline" className="gap-1">
          <Clock className="h-3 w-3" />
          Total: {totalDuration ? `${totalDuration}s` : 'N/A'}
        </Badge>
        <ArrowRight className="h-4 w-4" />
        <Badge variant="outline">2 API Calls</Badge>
      </div>

      {/* Flow Steps */}
      <div className="grid grid-cols-1 md:grid-cols-5 gap-4">
        {/* Step 1: Unified Expert */}
        <Card className="md:col-span-1">
          <CardHeader className="pb-3">
            <CardTitle className="text-sm flex items-center gap-2">
              <Brain className="h-4 w-4 text-purple-600" />
              Unified Expert
            </CardTitle>
            <Badge variant="secondary" className="w-fit text-xs">API Call 1/2</Badge>
          </CardHeader>
          <CardContent className="space-y-2">
            <div className="text-xs space-y-1">
              <div className="flex justify-between">
                <span className="text-muted-foreground">Duration:</span>
                <span className="font-mono">{expertDuration ? `${expertDuration}ms` : 'N/A'}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-muted-foreground">Proposals:</span>
                <span className="font-semibold">{proposals.length}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-muted-foreground">Candles:</span>
                <span className="font-mono">{candelCount}</span>
              </div>
            </div>
            
            <Button
              variant="ghost"
              size="sm"
              className="w-full justify-between"
              onClick={() => setExpandedExpert(!expandedExpert)}
            >
              Market Analysis
              {expandedExpert ? <ChevronUp className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />}
            </Button>

            {expandedExpert && (
              <div className="mt-2 p-2 bg-muted rounded-md text-xs">
                <div className="text-muted-foreground italic">
                  Expert analysis not stored (data passed to judge only)
                </div>
              </div>
            )}
          </CardContent>
        </Card>

        <div className="hidden md:flex items-center justify-center">
          <ArrowRight className="h-6 w-6 text-muted-foreground" />
        </div>

        {/* Step 2: Proposals */}
        <Card className="md:col-span-1">
          <CardHeader className="pb-3">
            <CardTitle className="text-sm flex items-center gap-2">
              <GitBranch className="h-4 w-4 text-blue-600" />
              Proposals
            </CardTitle>
            <Badge variant="secondary" className="w-fit text-xs">{proposals.length} Generated</Badge>
          </CardHeader>
          <CardContent className="space-y-2">
            <div className="text-xs space-y-1">
              {proposals.length > 0 ? (
                proposals.map((proposal: any, idx: number) => (
                  <div key={idx} className="flex items-center justify-between p-1 rounded bg-muted/50">
                    <span>{idx + 1}. {proposal.signal?.toUpperCase() || 'N/A'}</span>
                    <Badge variant="outline" className="text-xs">
                      {proposal.confidence ? `${(proposal.confidence * 100).toFixed(0)}%` : 'N/A'}
                    </Badge>
                  </div>
                ))
              ) : (
                <div className="text-xs text-muted-foreground italic">Not stored (passed to judge)</div>
              )}
            </div>

            <Button
              variant="ghost"
              size="sm"
              className="w-full justify-between"
              onClick={() => setExpandedProposals(!expandedProposals)}
            >
              View Details
              {expandedProposals ? <ChevronUp className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />}
            </Button>
          </CardContent>
        </Card>

        <div className="hidden md:flex items-center justify-center">
          <ArrowRight className="h-6 w-6 text-muted-foreground" />
        </div>

        {/* Step 3: Validation */}
        <Card className="md:col-span-1">
          <CardHeader className="pb-3">
            <CardTitle className="text-sm flex items-center gap-2">
              <CheckCircle className="h-4 w-4 text-green-600" />
              Validation
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-2">
            <div className="text-xs space-y-1">
              {cycle.judge?.validation_summary?.total_proposals !== undefined && (
                <div className="flex justify-between">
                  <span className="text-muted-foreground">Total:</span>
                  <span>{cycle.judge.validation_summary.total_proposals}</span>
                </div>
              )}
              {cycle.judge?.validation_summary?.valid_proposals !== undefined && (
                <div className="flex justify-between">
                  <span className="text-muted-foreground">Passed:</span>
                  <span className="font-semibold text-green-600">
                    {cycle.judge.validation_summary.valid_proposals}
                  </span>
                </div>
              )}
              {cycle.judge?.validation_summary?.failed_validation !== undefined && (
                <div className="flex justify-between">
                  <span className="text-muted-foreground">Failed:</span>
                  <span className="font-semibold text-red-600">
                    {cycle.judge.validation_summary.failed_validation}
                  </span>
                </div>
              )}
            </div>
          </CardContent>
        </Card>

        <div className="hidden md:flex items-center justify-center">
          <ArrowRight className="h-6 w-6 text-muted-foreground" />
        </div>

        {/* Step 4: Judge Decision */}
        <Card className="md:col-span-1">
          <CardHeader className="pb-3">
            <CardTitle className="text-sm flex items-center gap-2">
              {isCodeSniper ? <Code className="h-4 w-4 text-purple-600" /> : <Scale className="h-4 w-4 text-orange-600" />}
              {isCodeSniper ? 'Code Gen' : 'Judge'}
            </CardTitle>
            <Badge variant="secondary" className="w-fit text-xs">API Call 2/2</Badge>
          </CardHeader>
          <CardContent className="space-y-2">
            <div className="text-xs space-y-1">
              <div className="flex justify-between">
                <span className="text-muted-foreground">Duration:</span>
                <span className="font-mono">{judgeDuration ? `${judgeDuration}ms` : 'N/A'}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-muted-foreground">Decision:</span>
                <Badge variant={cycle.judge?.selected ? 'default' : 'secondary'} className="text-xs">
                  {cycle.judge?.selected || 'Flat/Skip'}
                </Badge>
              </div>
            </div>

            {cycle.judge?.rationale && (
              <Button
                variant="ghost"
                size="sm"
                className="w-full justify-between"
                onClick={() => setExpandedJudge(!expandedJudge)}
              >
                Rationale
                {expandedJudge ? <ChevronUp className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />}
              </Button>
            )}

            {expandedJudge && cycle.judge?.rationale && (
              <div className="mt-2 p-2 bg-muted rounded-md text-xs">
                {cycle.judge.rationale}
              </div>
            )}
          </CardContent>
        </Card>
      </div>

      {/* Expanded Proposals View */}
      {expandedProposals && proposals.length > 0 && (
        <Card className="mt-4">
          <CardHeader>
            <CardTitle className="text-sm">Proposal Details</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              {proposals.length > 0 ? (
                proposals.map((proposal: any, idx: number) => (
                  <ProposalCard
                    key={idx}
                    proposal={proposal}
                    validation={undefined}
                    index={idx}
                    isSelected={cycle.judge?.selected === 'trend' && idx === 0}
                    onClick={() => setSelectedProposal(selectedProposal === idx ? null : idx)}
                  />
                ))
              ) : (
                <div className="text-center text-muted-foreground py-4">
                  Expert proposals not stored (passed to judge only)
                </div>
              )}
            </div>

            {selectedProposal !== null && proposals.length > 0 && (
              <div className="mt-4">
                <div className="text-sm text-muted-foreground">
                  Validation metrics not available (expert data not stored)
                </div>
              </div>
            )}
          </CardContent>
        </Card>
      )}

      {/* Final Decision Summary */}
      {cycle.judge?.final_signal && (
        <Card>
          <CardHeader>
            <CardTitle className="text-sm">Final Decision</CardTitle>
          </CardHeader>
          <CardContent>
            <pre className="text-xs bg-muted p-3 rounded-md overflow-auto">
              {JSON.stringify(cycle.judge.final_signal, null, 2)}
            </pre>
          </CardContent>
        </Card>
      )}
    </div>
  )
}

