import { useEffect, useMemo, useRef, useState } from "react"
import { List } from "react-window"
import type { RowComponentProps } from "react-window"
import { Button } from "@/components/ui/button"
import { Badge } from "@/components/ui/badge"
import { useAgentEvents, type AgentEvent } from "@/stores/events"

const MAX_EVENTS = 100

const TYPE_META: Record<string, { icon: string; className: string }> = {
  buy: { icon: "⬆️", className: "bg-green-100 text-green-800" },
  sell: { icon: "⬇️", className: "bg-red-100 text-red-800" },
  error: { icon: "⚠️", className: "bg-yellow-100 text-yellow-800" },
  info: { icon: "ℹ️", className: "bg-blue-100 text-blue-800" },
}

const getMeta = (type: string) =>
  TYPE_META[type.toLowerCase()] ?? {
    icon: "•",
    className: "bg-gray-100 text-muted-foreground",
  }

const formatRelative = (ts: string, now: number) => {
  const diff = now - new Date(ts).getTime()
  const sec = Math.round(diff / 1000)
  if (sec < 60) return `${sec}s ago (IST)`
  const min = Math.round(sec / 60)
  if (min < 60) return `${min}m ago (IST)`
  const hr = Math.round(min / 60)
  if (hr < 24) return `${hr}h ago (IST)`
  const day = Math.round(hr / 24)
  return `${day}d ago (IST)`
}

interface EventTimelineProps {
  events?: AgentEvent[]
}

export function EventTimeline({ events: propEvents }: EventTimelineProps = {}) {
  const [paused, setPaused] = useState(false)
  const [assetFilter, setAssetFilter] = useState("")
  const [typeFilter, setTypeFilter] = useState("")

  const [pinned, setPinned] = useState<Set<AgentEvent>>(new Set())
  const pinnedRef = useRef(pinned)
  useEffect(() => {
    pinnedRef.current = pinned
  }, [pinned])

  const [now, setNow] = useState(Date.now())
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 60000)
    return () => clearInterval(id)
  }, [])

  const storeEvents = useAgentEvents(assetFilter || undefined)
  const rawEvents = propEvents ?? storeEvents
  const [events, setEvents] = useState<AgentEvent[]>(propEvents ?? [])
  const buffer = useRef<AgentEvent[]>([])
  const seen = useRef(0)

  useEffect(() => {
    if (propEvents) {
      setEvents(propEvents)
      return
    }
    if (paused) return
    const newEvents = rawEvents.slice(seen.current)
    if (newEvents.length) {
      buffer.current.push(...newEvents)
      // Flush immediately so UI updates without waiting for interval
      setEvents((prev) => {
        const next = [...prev, ...buffer.current].slice(-MAX_EVENTS)
        buffer.current = []
        return next
      })
    }
    seen.current = rawEvents.length
  }, [rawEvents, paused, propEvents])

  useEffect(() => {
    if (propEvents || paused) return
    const id = setInterval(() => {
      if (buffer.current.length) {
        setEvents((prev) => {
          const next = [...prev, ...buffer.current]
          buffer.current = []
          const pinnedEvents = next.filter((e) => pinnedRef.current.has(e))
          const others = next
            .filter((e) => !pinnedRef.current.has(e))
            .slice(-MAX_EVENTS)
          return [...pinnedEvents, ...others]
        })
      }
    }, 200)
    return () => clearInterval(id)
  }, [paused, propEvents])

  const filtered = useMemo(
    () =>
      events.filter(
        (e) =>
          (!assetFilter || e.asset === assetFilter) &&
          (!typeFilter || e.type.toLowerCase() === typeFilter.toLowerCase())
      ),
    [events, assetFilter, typeFilter]
  )

  const Row = ({ index, style }: RowComponentProps) => {
    const evt = filtered[index]
    const meta = getMeta(evt.type)
    const isPinned = pinned.has(evt)
    const togglePin = () =>
      setPinned((p) => {
        const next = new Set(p)
        if (next.has(evt)) next.delete(evt)
        else next.add(evt)
        return next
      })
    const relative = evt.timestamp ? formatRelative(evt.timestamp as string, now) : ""
    return (
      <div
        style={style}
        className={`flex items-center gap-sm px-sm text-sm ${
          isPinned ? "bg-yellow-50" : ""
        }`}
      >
        <Badge className={meta.className}>{meta.icon}</Badge>
        <span className="font-mono">[{evt.asset}] {evt.type}</span>
        {relative && (
          <span className="ml-auto text-xs text-muted-foreground">{relative}</span>
        )}
        <button
          aria-label={isPinned ? "Unpin event" : "Pin event"}
          className="ml-sm"
          onClick={togglePin}
        >
          {isPinned ? "📌" : "📍"}
        </button>
      </div>
    )
  }

  return (
    <div className="flex h-full flex-col">
      <div className="mb-2 flex items-center gap-sm">
        <Button variant="outline" onClick={() => setPaused((p) => !p)}>
          {paused ? "Resume" : "Pause"}
        </Button>
        <input
          className="h-8 w-24 rounded border px-sm"
          placeholder="Asset"
          value={assetFilter}
          onChange={(e) => setAssetFilter(e.target.value)}
        />
        <input
          className="h-8 w-32 rounded border px-sm"
          placeholder="Event type"
          value={typeFilter}
          onChange={(e) => setTypeFilter(e.target.value)}
        />
      </div>
      <List
        rowProps={{}}
        rowCount={filtered.length}
        rowHeight={32}
        rowComponent={Row}
        style={{ height: 400, width: "100%" }}
      />
    </div>
  )
}

