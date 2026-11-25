import { useMemo } from 'react'
import {
  useInfiniteQuery,
  useMutation,
  useQueryClient,
} from '@tanstack/react-query'
import { Modal } from '@/components/ui/modal'
import { Button } from '@/components/ui/button'
import {
  listConfigVersions,
  rollbackConfig,
  type ConfigVersion,
} from '@/services/apiClient'
import { toast } from 'sonner'
import { Light as SyntaxHighlighter } from 'react-syntax-highlighter'
import diff from 'react-syntax-highlighter/dist/esm/languages/hljs/diff'
import github from 'react-syntax-highlighter/dist/esm/styles/hljs/github'

SyntaxHighlighter.registerLanguage('diff', diff)

interface ConfigHistoryProps {
  domain: string
  env: string
  open: boolean
  onClose: () => void
  /** Query key to invalidate on rollback */
  queryKey: unknown[]
}

export default function ConfigHistory({
  domain,
  env,
  open,
  onClose,
  queryKey,
}: ConfigHistoryProps) {
  const qc = useQueryClient()

  const { data, fetchNextPage, hasNextPage, isLoading } = useInfiniteQuery({
    queryKey: ['configHistory', domain, env],
    enabled: open,
    initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam }) =>
      listConfigVersions(domain, env, pageParam),
    getNextPageParam: (last: any) => last.nextCursor,
  })

  const rollback = useMutation({
    mutationFn: (v: number) => rollbackConfig(domain, env, v),
    onSuccess: () => {
      toast.success('Config rolled back')
      qc.invalidateQueries({ queryKey })
      qc.invalidateQueries({ queryKey: ['configHistory', domain, env] })
      onClose()
    },
    onError: (err: Error & { status?: number }) => {
      if (err.status === 403) toast.error('Forbidden')
      else toast.error(err.message)
    },
  })

  const versions = useMemo(
    () => data?.pages.flatMap((p) => p.versions) ?? [],
    [data]
  )

  const items = useMemo(() => {
    const out: (ConfigVersion & { diff: string })[] = []
    for (let i = 0; i < versions.length; i++) {
      const cur = versions[i]
      const prev = versions[i + 1]
      const curStr = JSON.stringify(cur.doc, null, 2)
      const prevStr = JSON.stringify(prev?.doc ?? {}, null, 2)
      const diffStr = createDiff(prevStr, curStr)
      out.push({ ...cur, diff: diffStr })
    }
    return out
  }, [versions])

  return (
    <Modal
      open={open}
      onClose={onClose}
      className="max-h-[90vh] overflow-y-auto"
      title="Config History"
    >
      {isLoading && <div>Loading...</div>}
      {!isLoading && items.length === 0 && <div>No history</div>}
      {!isLoading &&
        items.map((v) => (
          <div key={v.version} className="mb-md">
            <div className="mb-sm flex items-center justify-between">
              <span className="font-mono text-sm">Version {v.version}</span>
              <Button size="sm" onClick={() => rollback.mutate(v.version)}>
                Rollback
              </Button>
            </div>
            <SyntaxHighlighter
              language="diff"
              style={github}
              customStyle={{ margin: 0 }}
            >
              {v.diff}
            </SyntaxHighlighter>
          </div>
        ))}
      {hasNextPage && (
        <div className="mt-md flex justify-center">
          <Button
            variant="secondary"
            onClick={() => fetchNextPage()}
            disabled={isLoading}
          >
            Load more
          </Button>
        </div>
      )}
    </Modal>
  )
}

function createDiff(oldStr: string, newStr: string): string {
  const oldLines = oldStr.split('\n')
  const newLines = newStr.split('\n')
  const m = oldLines.length
  const n = newLines.length
  const dp = Array.from({ length: m + 1 }, () => Array(n + 1).fill(0))
  for (let i = m - 1; i >= 0; i--) {
    for (let j = n - 1; j >= 0; j--) {
      dp[i][j] =
        oldLines[i] === newLines[j]
          ? dp[i + 1][j + 1] + 1
          : Math.max(dp[i + 1][j], dp[i][j + 1])
    }
  }
  const res: string[] = []
  let i = 0,
    j = 0
  while (i < m && j < n) {
    if (oldLines[i] === newLines[j]) {
      res.push('  ' + oldLines[i])
      i++
      j++
    } else if (dp[i + 1][j] >= dp[i][j + 1]) {
      res.push('- ' + oldLines[i])
      i++
    } else {
      res.push('+ ' + newLines[j])
      j++
    }
  }
  while (i < m) res.push('- ' + oldLines[i++])
  while (j < n) res.push('+ ' + newLines[j++])
  return res.join('\n')
}

