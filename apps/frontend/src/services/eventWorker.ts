// Web Worker for handling agent event streams via SSE with WS fallback
interface StartMessage {
  type: 'start'
  url: string
}

interface StopMessage {
  type: 'stop'
}

type IncomingMessage = StartMessage | StopMessage

type Connection = EventSource | WebSocket | null

let url = ''
let retry = 0
let conn: Connection = null
let heartbeatAt = Date.now()
let heartbeatTimer: number | undefined
const HEARTBEAT_MS = 30_000

const log = (message: string, data?: any) => {
  self.postMessage({
    type: 'log',
    payload: { message, data, timestamp: new Date().toISOString() },
  })
}

const scheduleReconnect = () => {
  cleanup()
  const delay = Math.min(30_000, 1000 * 2 ** retry++)
  log(`Scheduling reconnect in ${delay}ms (retry attempt: ${retry})`)
  setTimeout(connect, delay)
}

const cleanup = () => {
  if (conn instanceof EventSource) {
    conn.close()
  } else if (conn instanceof WebSocket) {
    conn.close()
  }
  conn = null
  if (heartbeatTimer) {
    clearInterval(heartbeatTimer)
    heartbeatTimer = undefined
  }
}

const handleData = (data: string) => {
  try {
    const msg = JSON.parse(data)
    if (msg.type === 'heartbeat') {
      heartbeatAt = Date.now()
      return
    }
    self.postMessage({
      type: 'event',
      payload: msg,
    })
  } catch (err) {
    // Log malformed messages for debugging
    log('Error parsing event data', { error: String(err), data })
  }
}

const startHeartbeat = () => {
  if (heartbeatTimer) clearInterval(heartbeatTimer)
  heartbeatTimer = setInterval(() => {
    const elapsed = Date.now() - heartbeatAt
    if (elapsed > HEARTBEAT_MS * 2) {
      log(`Heartbeat timeout: ${elapsed}ms since last heartbeat`)
      scheduleReconnect()
    }
  }, HEARTBEAT_MS) as unknown as number
}

const connect = () => {
  heartbeatAt = Date.now()
  log(`Connecting to ${url}`)
  try {
    conn = new EventSource(url)
    conn.onopen = () => {
      log('EventSource connection opened')
      retry = 0
      startHeartbeat()
    }
    conn.onmessage = (ev: MessageEvent) => handleData(ev.data)
    conn.onerror = (err) => {
      log('EventSource error', err)
      conn?.close()
      conn = null
      tryWebSocket()
    }
  } catch (err) {
    log('EventSource connection failed', String(err))
    tryWebSocket()
  }
}

const tryWebSocket = () => {
  try {
    const wsUrl = url.replace(/^http/, 'ws')
    log(`Trying WebSocket fallback at ${wsUrl}`)
    const ws = new WebSocket(wsUrl)
    conn = ws
    ws.onopen = () => {
      log('WebSocket connection opened')
      retry = 0
      startHeartbeat()
    }
    ws.onmessage = (ev: MessageEvent) => handleData(ev.data as string)
    ws.onerror = (err) => {
      log('WebSocket error', err)
      scheduleReconnect()
    }
    ws.onclose = (ev) => {
      log('WebSocket closed', { code: ev.code, reason: ev.reason })
      scheduleReconnect()
    }
  } catch (err) {
    log('WebSocket connection failed', String(err))
    scheduleReconnect()
  }
}

self.onmessage = (e: MessageEvent<IncomingMessage>) => {
  const msg = e.data
  if (msg.type === 'start') {
    url = msg.url
    retry = 0
    log(`Starting connection to ${url}`)
    connect()
  } else if (msg.type === 'stop') {
    log('Stopping connection')
    cleanup()
  }
}
