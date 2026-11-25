import { useState } from 'react'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { ChevronDown, ChevronRight, Copy, FileText } from 'lucide-react'

interface JsonViewerProps {
  title: string
  data: any
  defaultCollapsed?: boolean
  className?: string
  showCopyButton?: boolean
}

export function JsonViewer({ 
  title, 
  data, 
  defaultCollapsed = true,
  className = '',
  showCopyButton = true
}: JsonViewerProps) {
  const [isCollapsed, setIsCollapsed] = useState(defaultCollapsed)

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(JSON.stringify(data, null, 2))
      // Could add a toast notification here
    } catch (error) {
      console.error('Failed to copy JSON:', error)
    }
  }

  const formatJsonSize = (jsonStr: string): string => {
    const bytes = new Blob([jsonStr]).size
    if (bytes < 1024) return `${bytes} B`
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
  }

  const jsonString = JSON.stringify(data, null, 2)
  const size = formatJsonSize(jsonString)

  return (
    <Card className={className}>
      <CardHeader className="pb-3">
        <div className="flex items-center justify-between">
          <CardTitle className="flex items-center gap-2">
            <FileText className="h-5 w-5" />
            {title}
            <Badge variant="outline" className="text-xs">
              {size}
            </Badge>
          </CardTitle>
          <div className="flex items-center gap-2">
            {showCopyButton && (
              <Button
                variant="outline"
                size="sm"
                onClick={handleCopy}
                className="h-8 px-2"
              >
                <Copy className="h-3 w-3" />
              </Button>
            )}
            <Button
              variant="ghost"
              size="sm"
              onClick={() => setIsCollapsed(!isCollapsed)}
              className="h-8 px-2"
            >
              {isCollapsed ? (
                <ChevronRight className="h-4 w-4" />
              ) : (
                <ChevronDown className="h-4 w-4" />
              )}
            </Button>
          </div>
        </div>
      </CardHeader>
      {!isCollapsed && (
        <CardContent className="pt-0">
          <div className="bg-muted rounded-md p-4 max-h-96 overflow-auto">
            <pre className="text-xs font-mono whitespace-pre-wrap break-words">
              {jsonString}
            </pre>
          </div>
        </CardContent>
      )}
    </Card>
  )
}
