import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { GitCompare } from 'lucide-react'

interface DiffViewProps {
  title: string
  original: any
  modified: any
  className?: string
}

export function DiffView({ title, original, modified, className = '' }: DiffViewProps) {
  const hasDifferences = JSON.stringify(original) !== JSON.stringify(modified)
  
  const getDifferences = () => {
    const differences: Array<{
      path: string
      type: 'added' | 'removed' | 'changed'
      original?: any
      modified?: any
    }> = []

    const compareObjects = (obj1: any, obj2: any, path: string = '') => {
      const allKeys = new Set([...Object.keys(obj1 || {}), ...Object.keys(obj2 || {})])
      
      for (const key of allKeys) {
        const currentPath = path ? `${path}.${key}` : key
        const val1 = obj1?.[key]
        const val2 = obj2?.[key]
        
        if (val1 === undefined && val2 !== undefined) {
          differences.push({ path: currentPath, type: 'added', modified: val2 })
        } else if (val1 !== undefined && val2 === undefined) {
          differences.push({ path: currentPath, type: 'removed', original: val1 })
        } else if (val1 !== val2) {
          differences.push({ path: currentPath, type: 'changed', original: val1, modified: val2 })
        } else if (typeof val1 === 'object' && val1 !== null && typeof val2 === 'object' && val2 !== null) {
          compareObjects(val1, val2, currentPath)
        }
      }
    }

    compareObjects(original, modified)
    return differences
  }

  const differences = getDifferences()

  const getTypeColor = (type: string) => {
    switch (type) {
      case 'added': return 'bg-green-100 text-green-800 border-green-200'
      case 'removed': return 'bg-red-100 text-red-800 border-red-200'
      case 'changed': return 'bg-yellow-100 text-yellow-800 border-yellow-200'
      default: return 'bg-gray-100 text-gray-800 border-gray-200'
    }
  }

  const formatValue = (value: any): string => {
    if (value === null) return 'null'
    if (value === undefined) return 'undefined'
    if (typeof value === 'object') return JSON.stringify(value, null, 2)
    return String(value)
  }

  return (
    <Card className={className}>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <GitCompare className="h-5 w-5" />
          {title}
          <Badge variant={hasDifferences ? 'destructive' : 'default'}>
            {hasDifferences ? `${differences.length} differences` : 'No differences'}
          </Badge>
        </CardTitle>
      </CardHeader>
      <CardContent>
        {hasDifferences ? (
          <div className="space-y-3">
            {differences.map((diff, index) => (
              <div key={index} className="border rounded-lg p-3">
                <div className="flex items-center justify-between mb-2">
                  <code className="text-sm font-mono bg-muted px-2 py-1 rounded">
                    {diff.path}
                  </code>
                  <Badge variant="outline" className={getTypeColor(diff.type)}>
                    {diff.type.toUpperCase()}
                  </Badge>
                </div>
                
                <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                  {diff.original !== undefined && (
                    <div>
                      <div className="text-xs font-medium text-muted-foreground mb-1">
                        Original:
                      </div>
                      <div className="text-sm font-mono bg-red-50 p-2 rounded border-l-2 border-red-300">
                        {formatValue(diff.original)}
                      </div>
                    </div>
                  )}
                  
                  {diff.modified !== undefined && (
                    <div>
                      <div className="text-xs font-medium text-muted-foreground mb-1">
                        Modified:
                      </div>
                      <div className="text-sm font-mono bg-green-50 p-2 rounded border-l-2 border-green-300">
                        {formatValue(diff.modified)}
                      </div>
                    </div>
                  )}
                </div>
              </div>
            ))}
          </div>
        ) : (
          <div className="text-center py-4 text-muted-foreground">
            <GitCompare className="h-8 w-8 mx-auto mb-2 opacity-50" />
            <p>No differences found</p>
          </div>
        )}
      </CardContent>
    </Card>
  )
}
