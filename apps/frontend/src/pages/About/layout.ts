import { STAGES, type Stage, type StageId } from "./content"

/** 288 fits the longest title ("Data Processing & Preparation", 191px) on one line. */
export const NODE_W = 288
export const NODE_H = 88

const COL_GAP = 52
const COL_PITCH = NODE_W + COL_GAP
const ROW_PITCH = 148
/** Equal gutters left and right, so the four columns read as centred in the canvas. */
const GUTTER = 40
const OX = GUTTER
const OY = 18

/** Full drawing surface, including the routing gutters on the left and bottom. */
export const CANVAS_W = OX + 3 * COL_PITCH + NODE_W + GUTTER
export const CANVAS_H = 500

export interface Pt {
  x: number
  y: number
}

export type ChartNodeId = StageId | "rejected"

export interface ChartNode {
  id: ChartNodeId
  col: number
  row: number
  x: number
  y: number
  stage?: Stage
  terminal?: { label: string; sublabel: string }
}

const place = (col: number, row: number): Pt => ({
  x: OX + col * COL_PITCH,
  y: OY + row * ROW_PITCH
})

const GRID: { id: ChartNodeId; col: number; row: number }[] = [
  // Row 0 — left to right
  { id: "data", col: 0, row: 0 },
  { id: "processing", col: 1, row: 0 },
  { id: "ai", col: 2, row: 0 },
  { id: "decision", col: 3, row: 0 },
  // Row 1 — right to left
  { id: "reconciliation", col: 0, row: 1 },
  { id: "monitoring", col: 1, row: 1 },
  { id: "execution", col: 2, row: 1 },
  { id: "risk", col: 3, row: 1 },
  // Row 2 — left to right, with the reject terminal under the gates
  { id: "audit", col: 0, row: 2 },
  { id: "improvement", col: 1, row: 2 },
  { id: "rejected", col: 3, row: 2 }
]

export const CHART_NODES: ChartNode[] = GRID.map(({ id, col, row }) => {
  const { x, y } = place(col, row)
  const stage = STAGES.find((s) => s.id === id)
  return {
    id,
    col,
    row,
    x,
    y,
    stage,
    terminal:
      id === "rejected"
        ? { label: "Rejected", sublabel: "Reason logged · no order sent" }
        : undefined
  }
})

const node = (id: ChartNodeId) => CHART_NODES.find((n) => n.id === id)!

const sideOf = (id: ChartNodeId) => {
  const n = node(id)
  return {
    left: { x: n.x, y: n.y + NODE_H / 2 },
    right: { x: n.x + NODE_W, y: n.y + NODE_H / 2 },
    top: { x: n.x + NODE_W / 2, y: n.y },
    bottom: { x: n.x + NODE_W / 2, y: n.y + NODE_H }
  }
}

/** Converts an orthogonal polyline into a path with rounded corners. */
export function roundedPath(points: Pt[], radius = 12): string {
  if (points.length < 2) return ""
  let d = `M ${points[0].x} ${points[0].y}`

  for (let i = 1; i < points.length - 1; i++) {
    const prev = points[i - 1]
    const cur = points[i]
    const next = points[i + 1]

    const inLen = Math.hypot(cur.x - prev.x, cur.y - prev.y)
    const outLen = Math.hypot(next.x - cur.x, next.y - cur.y)
    const r = Math.min(radius, inLen / 2, outLen / 2)

    const inUnit = { x: (cur.x - prev.x) / inLen, y: (cur.y - prev.y) / inLen }
    const outUnit = { x: (next.x - cur.x) / outLen, y: (next.y - cur.y) / outLen }

    const a = { x: cur.x - inUnit.x * r, y: cur.y - inUnit.y * r }
    const b = { x: cur.x + outUnit.x * r, y: cur.y + outUnit.y * r }

    d += ` L ${a.x} ${a.y} Q ${cur.x} ${cur.y} ${b.x} ${b.y}`
  }

  const last = points[points.length - 1]
  return `${d} L ${last.x} ${last.y}`
}

export type EdgeVariant = "main" | "reject" | "feedback"

export interface ChartEdge {
  id: string
  from: ChartNodeId
  to: ChartNodeId
  d: string
  variant: EdgeVariant
  label?: string
  /** Where the label sits, in canvas coordinates. */
  labelAt?: Pt
}

// Routing gutters kept clear of every node box.
const BOTTOM_LANE_REJECT = 442
const BOTTOM_LANE_FEEDBACK = 474
const LEFT_LANE = 16

function buildEdges(): ChartEdge[] {
  const s = sideOf

  const straight = (from: ChartNodeId, to: ChartNodeId, side: "row" | "down"): ChartEdge => {
    const a = side === "row" ? s(from) : s(from)
    const b = s(to)
    const points =
      side === "row"
        ? node(from).col < node(to).col
          ? [a.right, b.left]
          : [a.left, b.right]
        : [a.bottom, b.top]
    return { id: `${from}-${to}`, from, to, d: roundedPath(points), variant: "main" }
  }

  const edges: ChartEdge[] = [
    straight("data", "processing", "row"),
    straight("processing", "ai", "row"),
    straight("ai", "decision", "row"),
    straight("decision", "risk", "down"),
    straight("risk", "execution", "row"),
    straight("execution", "monitoring", "row"),
    straight("monitoring", "reconciliation", "row"),
    straight("reconciliation", "audit", "down"),
    straight("audit", "improvement", "row")
  ]

  // PASS label on the gate → execution hop.
  const passEdge = edges.find((e) => e.id === "risk-execution")!
  passEdge.label = "PASS"
  passEdge.labelAt = {
    x: (s("risk").left.x + s("execution").right.x) / 2,
    y: s("risk").left.y - 12
  }

  // Reject branch: straight drop out of the gates.
  edges.push({
    id: "risk-rejected",
    from: "risk",
    to: "rejected",
    d: roundedPath([s("risk").bottom, s("rejected").top]),
    variant: "reject",
    label: "REJECT",
    labelAt: { x: s("risk").bottom.x + 46, y: (s("risk").bottom.y + s("rejected").top.y) / 2 }
  })

  // Rejections still reach the audit — routed under row 2 to clear Improvement.
  edges.push({
    id: "rejected-audit",
    from: "rejected",
    to: "audit",
    d: roundedPath([
      s("rejected").bottom,
      { x: s("rejected").bottom.x, y: BOTTOM_LANE_REJECT },
      { x: s("audit").bottom.x, y: BOTTOM_LANE_REJECT },
      s("audit").bottom
    ]),
    variant: "reject",
    label: "still audited",
    labelAt: {
      x: (s("rejected").bottom.x + s("audit").bottom.x) / 2,
      y: BOTTOM_LANE_REJECT - 10
    }
  })

  // Closing the loop: down, around the left gutter, back into Market Data.
  edges.push({
    id: "improvement-data",
    from: "improvement",
    to: "data",
    d: roundedPath([
      s("improvement").bottom,
      { x: s("improvement").bottom.x, y: BOTTOM_LANE_FEEDBACK },
      { x: LEFT_LANE, y: BOTTOM_LANE_FEEDBACK },
      { x: LEFT_LANE, y: s("data").left.y },
      s("data").left
    ]),
    variant: "feedback",
    label: "validated change",
    labelAt: {
      x: (s("improvement").bottom.x + LEFT_LANE) / 2,
      y: BOTTOM_LANE_FEEDBACK + 15
    }
  })

  return edges
}

export const CHART_EDGES: ChartEdge[] = buildEdges()

/** Node order for each traced scenario. */
export const TRACE_NODES: Record<"approved" | "rejected", ChartNodeId[]> = {
  approved: [
    "data",
    "processing",
    "ai",
    "decision",
    "risk",
    "execution",
    "monitoring",
    "reconciliation",
    "audit",
    "improvement"
  ],
  rejected: ["data", "processing", "ai", "decision", "risk", "rejected", "audit"]
}

/** Edge order for each traced scenario, aligned to TRACE_NODES transitions. */
export const TRACE_EDGES: Record<"approved" | "rejected", string[]> = {
  approved: [
    "data-processing",
    "processing-ai",
    "ai-decision",
    "decision-risk",
    "risk-execution",
    "execution-monitoring",
    "monitoring-reconciliation",
    "reconciliation-audit",
    "audit-improvement",
    "improvement-data"
  ],
  rejected: [
    "data-processing",
    "processing-ai",
    "ai-decision",
    "decision-risk",
    "risk-rejected",
    "rejected-audit"
  ]
}
