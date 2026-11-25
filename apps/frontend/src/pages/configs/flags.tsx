import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Button } from '@/components/ui/button'
import { getFeatureFlagsConfig, putFeatureFlagsConfig } from '@/services/apiClient'
import type { FeatureFlagsConfig } from '@/types/api'
import ConfigHistory from '@/components/ConfigHistory'
import { toast } from 'sonner'

export default function FlagsConfigs() {
  const qc = useQueryClient()
  const { data, isError, error } = useQuery({ queryKey: ['flagsConfig'], queryFn: getFeatureFlagsConfig })
  const [enableBacktest, setEnableBacktest] = useState(false)
  const [enableStream, setEnableStream] = useState(false)
  const [showHistory, setShowHistory] = useState(false)
  const env = import.meta.env.VITE_ENV || ''

  useEffect(() => {
    if (data) {
      setEnableBacktest(data.enable_backtest)
      setEnableStream(data.enable_stream)
    }
  }, [data])

  const mutation = useMutation({
    mutationFn: (cfg: FeatureFlagsConfig) => putFeatureFlagsConfig(cfg),
    retry: 2,
    onMutate: async (cfg) => {
      await qc.cancelQueries({ queryKey: ['flagsConfig'] })
      const prev = qc.getQueryData<FeatureFlagsConfig>(['flagsConfig'])
      qc.setQueryData<FeatureFlagsConfig>(['flagsConfig'], cfg)
      return { prev }
    },
    onError: (err: Error, _vars, ctx) => {
      if (ctx?.prev) qc.setQueryData(['flagsConfig'], ctx.prev)
      toast.error(err.message)
    },
    onSuccess: () => toast.success('Flags saved'),
    onSettled: () => qc.invalidateQueries({ queryKey: ['flagsConfig'] }),
  })

  const onSave = () => {
    mutation.mutate({ enable_backtest: enableBacktest, enable_stream: enableStream })
  }

  return (
    <div className="space-y-4">
      <div className="flex justify-end">
        <Button variant="outline" size="sm" onClick={() => setShowHistory(true)}>
          View history
        </Button>
      </div>
      {isError && (
        <div className="rounded border border-destructive/50 bg-destructive/10 p-sm text-sm text-destructive mb-sm">
          {(error as Error)?.message || 'Failed to load flags config'}
        </div>
      )}
      <ul className="space-y-2">
        <li className="flex items-center gap-sm">
          <input
            type="checkbox"
            checked={enableBacktest}
            onChange={(e) => setEnableBacktest(e.target.checked)}
          />
          <span className="uppercase text-sm">enable_backtest</span>
        </li>
        <li className="flex items-center gap-sm">
          <input
            type="checkbox"
            checked={enableStream}
            onChange={(e) => setEnableStream(e.target.checked)}
          />
          <span className="uppercase text-sm">enable_stream</span>
        </li>
      </ul>
      <Button type="button" onClick={onSave} disabled={mutation.isPending}>
        Save
      </Button>
      {showHistory && (
        <ConfigHistory
          domain="flags"
          env={env}
          open={showHistory}
          onClose={() => setShowHistory(false)}
          queryKey={['flagsConfig']}
        />
      )}
    </div>
  )
}
