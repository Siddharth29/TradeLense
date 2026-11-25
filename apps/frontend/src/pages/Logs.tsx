import { useEffect, useMemo, useRef, useState } from 'react'
import { useVirtualizer } from '@tanstack/react-virtual'
import { createEventClient } from '@/services/eventClient'
import { API_BASE_URL } from '@/services/apiClient'
import { formatISTDateTime } from '@/lib/dateUtils'

interface LogEntry {
  time?: string
  level?: string
  asset?: string
  msg?: string
  [key: string]: unknown
}

const LOGS_URL = `${API_BASE_URL}/logs/stream`
const logClient = createEventClient<LogEntry>(LOGS_URL)

const LEVEL_COLORS: Record<string, string> = {
  debug: 'text-gray-500',
  info: 'text-blue-600',
  warn: 'text-yellow-600',
  error: 'text-red-600',
}

export default function Logs() {
  const [logs, setLogs] = useState<LogEntry[]>([])
  const [paused, setPaused] = useState(false)
  const [autoScroll, setAutoScroll] = useState(true)
  const [isUserScrolling, setIsUserScrolling] = useState(false)
  const [level, setLevel] = useState('')
  const [asset, setAsset] = useState('')
  const [text, setText] = useState('')
  const [expanded, setExpanded] = useState<Set<number>>(new Set())
  const [connectionStatus, setConnectionStatus] = useState<'connected' | 'disconnected' | 'connecting'>('connecting')
  const connectionAttemptRef = useRef(0)
  const receivedLogRef = useRef(false)

  useEffect(() => {
    if (paused) return
    
    // Add a manual log entry to help debug
    setLogs(prev => [...prev, {
      time: new Date().toISOString(),
      level: 'debug',
      msg: `Connecting to log stream at ${LOGS_URL}`,
    }])
    
    setConnectionStatus('connecting')
    const connectionAttempt = ++connectionAttemptRef.current
    
    // Reset the received log flag
    receivedLogRef.current = false
    
    // Set a timeout to check if we've received any logs
    const timeoutId = setTimeout(() => {
      if (connectionAttempt === connectionAttemptRef.current && !receivedLogRef.current) {
        setConnectionStatus('disconnected')
        setLogs(prev => [...prev, {
          time: new Date().toISOString(),
          level: 'error',
          msg: 'No logs received. Connection may have failed.',
        }])
      }
    }, 5000)
    
    const unsub = logClient.subscribe((line) => {
      receivedLogRef.current = true
      setConnectionStatus('connected')
      setLogs((prev) => [...prev, line])
    })
    
    return () => {
      unsub()
      clearTimeout(timeoutId)
    }
  }, [paused])

  const clear = () => setLogs([])
  const copy = () => {
    const raw = filtered.map((l) => formatLine(l)).join('\n')
    void navigator.clipboard.writeText(raw)
  }
  
  const reconnect = () => {
    clear()
    setPaused(false)
  }

  const filtered = useMemo(() => {
    return logs.filter((l) => {
      return (
        (!level || l.level?.toLowerCase() === level.toLowerCase()) &&
        (!asset || l.asset?.toLowerCase().includes(asset.toLowerCase())) &&
        (!text || formatLine(l).toLowerCase().includes(text.toLowerCase()))
      )
    })
  }, [logs, level, asset, text])

  const parentRef = useRef<HTMLDivElement>(null)
  const rowVirtualizer = useVirtualizer({
    count: filtered.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => 20,
    overscan: 10,
  })

  // Handle scroll detection
  useEffect(() => {
    const scrollElement = parentRef.current
    if (!scrollElement) return

    let scrollTimeout: NodeJS.Timeout

    const handleScroll = () => {
      setIsUserScrolling(true)
      clearTimeout(scrollTimeout)
      scrollTimeout = setTimeout(() => {
        setIsUserScrolling(false)
      }, 1000) // Reset after 1 second of no scrolling
    }

    scrollElement.addEventListener('scroll', handleScroll)
    return () => {
      scrollElement.removeEventListener('scroll', handleScroll)
      clearTimeout(scrollTimeout)
    }
  }, [])

  // Auto-scroll when new logs arrive (only if user is not manually scrolling)
  useEffect(() => {
    if (autoScroll && !isUserScrolling && filtered.length > 0) {
      setTimeout(() => {
        rowVirtualizer.scrollToIndex(filtered.length - 1, { align: 'end' })
      }, 0)
    }
  }, [filtered.length, autoScroll, rowVirtualizer, isUserScrolling])

  // Auto-scroll when new logs arrive
  useEffect(() => {
    if (autoScroll && !isUserScrolling && logs.length > 0) {
      setTimeout(() => {
        rowVirtualizer.scrollToIndex(filtered.length - 1, { align: 'end' })
      }, 0)
    }
  }, [logs.length, autoScroll, rowVirtualizer, filtered.length, isUserScrolling])

  const togglePause = () => setPaused((p) => !p)
  const toggleScroll = () => {
    setAutoScroll((s) => !s)
    setIsUserScrolling(false) // Reset user scrolling state when toggling
  }
  const toggleExpand = (i: number) =>
    setExpanded((s) => {
      const next = new Set(s)
      if (next.has(i)) next.delete(i)
      else next.add(i)
      return next
    })
  const download = () => {
    const raw = filtered.map((l) => JSON.stringify(l)).join('\n')
    const blob = new Blob([raw], { type: 'application/json' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = 'logs.json'
    a.click()
    URL.revokeObjectURL(url)
  }

  // Connection status indicator styles
  const statusColors = {
    connected: 'bg-green-500',
    disconnected: 'bg-red-500',
    connecting: 'bg-yellow-500 animate-pulse',
  }

  return (
    <div className="flex flex-col h-full">
      <div className="mb-2 flex flex-wrap items-center gap-sm pb-2 flex-shrink-0">
        <div className="flex items-center">
          <span className={`inline-block h-2 w-2 rounded-full ${statusColors[connectionStatus]} mr-2`}></span>
          <span className="text-sm mr-4">
            {connectionStatus === 'connected' ? 'Connected' : 
             connectionStatus === 'disconnected' ? 'Disconnected' : 
             'Connecting...'}
          </span>
        </div>
        <select
          className="rounded border px-sm py-xs"
          value={level}
          onChange={(e) => setLevel(e.target.value)}
        >
          <option value="">All Levels</option>
          <option value="debug">Debug</option>
          <option value="info">Info</option>
          <option value="warn">Warn</option>
          <option value="error">Error</option>
        </select>
        <input
          className="rounded border px-sm py-xs"
          placeholder="Asset"
          value={asset}
          onChange={(e) => setAsset(e.target.value)}
        />
        <input
          className="flex-1 rounded border px-sm py-xs"
          placeholder="Filter text"
          value={text}
          onChange={(e) => setText(e.target.value)}
        />
        <button className="rounded border px-sm py-xs" onClick={togglePause}>
          {paused ? 'Resume' : 'Pause'}
        </button>
        <button 
          className={`rounded border px-sm py-xs ${autoScroll ? ' text-white' : ' text-white'}`}
          onClick={toggleScroll}
        >
          {autoScroll ? 'Auto Scroll ON' : 'Auto Scroll OFF'}
        </button>
        {connectionStatus === 'disconnected' && (
          <button className="rounded border bg-blue-500 text-white px-sm py-xs" onClick={reconnect}>
            Reconnect
          </button>
        )}
        <button className="rounded border px-sm py-xs" onClick={download}>
          Download
        </button>
        <button className="rounded border px-sm py-xs" onClick={copy}>
          Copy
        </button>
        <button className="rounded border px-sm py-xs" onClick={clear}>
          Clear
        </button>
      </div>
      <div 
        ref={parentRef} 
        className="flex-1 overflow-auto min-h-0 scrollbar-hide"
        style={{
          scrollbarWidth: 'none', /* Firefox */
          msOverflowStyle: 'none', /* Internet Explorer 10+ */
        }}
      >
        {filtered.length === 0 ? (
          <div className="flex h-full items-center justify-center text-gray-500">
            {connectionStatus === 'connecting' ? 'Connecting to log stream...' :
             connectionStatus === 'disconnected' ? 'Disconnected. Click "Reconnect" to try again.' :
             'No logs to display'}
          </div>
        ) : (
          <div
            style={{
              height: `${rowVirtualizer.getTotalSize()}px`,
              width: '100%',
              position: 'relative',
            }}
          >
            {rowVirtualizer.getVirtualItems().map((item) => {
              const log = filtered[item.index]
              const levelClass = LEVEL_COLORS[log.level?.toLowerCase() ?? '']
              const isExpanded = expanded.has(item.index)
              return (
                <div
                  key={item.key}
                  ref={rowVirtualizer.measureElement}
                  data-index={item.index}
                  className="font-mono text-sm"
                  style={{
                    position: 'absolute',
                    top: 0,
                    left: 0,
                    width: '100%',
                    transform: `translateY(${item.start}px)`,
                  }}
                >
                  <div
                    className="whitespace-pre cursor-pointer"
                    onClick={() => toggleExpand(item.index)}
                  >
                    {log.time && <span>[{formatISTDateTime(log.time)}] </span>}
                    {log.level && (
                      <span className={`${levelClass} font-bold`}>
                        {log.level.toUpperCase()}{' '}
                      </span>
                    )}
                    {log.asset && <span>{log.asset} </span>}
                    <span>{log.msg ?? JSON.stringify(log)}</span>
                  </div>
                  {isExpanded && (
                    <pre className="mt-xs whitespace-pre-wrap break-all text-xs">
                      {JSON.stringify(log, null, 2)}
                    </pre>
                  )}
                </div>
              )
            })}
          </div>
        )}
      </div>
    </div>
  )
}

function formatLine(l: LogEntry): string {
  const time = l.time ? `[${formatISTDateTime(l.time)}] ` : ''
  const lvl = l.level ? `${l.level.toUpperCase()} ` : ''
  const asset = l.asset ? `${l.asset} ` : ''
  const msg = l.msg ?? JSON.stringify(l)
  return `${time}${lvl}${asset}${msg}`
}

