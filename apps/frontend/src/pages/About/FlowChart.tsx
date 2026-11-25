import { ArrowDown, Ban } from "lucide-react"
import { cn } from "@/lib/utils"
import { ACCENT_STYLES } from "./ui"
import {
  CANVAS_H,
  CANVAS_W,
  CHART_EDGES,
  CHART_NODES,
  NODE_H,
  NODE_W,
  type ChartNode,
  type ChartNodeId,
  type EdgeVariant
} from "./layout"

const EDGE_COLOR: Record<EdgeVariant, string> = {
  main: "var(--chart-line)",
  reject: "var(--chart-reject)",
  feedback: "var(--chart-feedback)"
}

interface FlowChartProps {
  selectedId: ChartNodeId | null
  onSelect: (id: ChartNodeId) => void
  activeNodes: ChartNodeId[]
  activeEdges: string[]
  tracing: boolean
}

function NodeCard({
  node,
  selected,
  dimmed,
  lit,
  onSelect
}: {
  node: ChartNode
  selected: boolean
  dimmed: boolean
  lit: boolean
  onSelect: (id: ChartNodeId) => void
}) {
  const isTerminal = !!node.terminal
  const styles = node.stage ? ACCENT_STYLES[node.stage.accent] : null
  const Icon = node.stage?.icon ?? Ban

  return (
    <button
      type="button"
      onClick={() => onSelect(node.id)}
      aria-pressed={selected}
      className={cn(
        "group relative flex w-full items-start gap-2.5 rounded-xl border bg-card px-3.5 py-3 text-left transition-all duration-200",
        "hover:-translate-y-0.5 hover:shadow-lg focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
        isTerminal ? "border-dashed border-rose-500/50" : "border-border",
        selected && !isTerminal && cn("shadow-md", styles?.borderStrong),
        selected && isTerminal && "border-rose-500 shadow-md",
        dimmed && "opacity-30",
        lit && "shadow-lg"
      )}
      style={{ height: NODE_H }}
    >
      {/* Accent spine keeps ten cards reading as one system without ten loud fills. */}
      <span
        className={cn(
          "absolute inset-y-2.5 left-0 w-[3px] rounded-full transition-opacity",
          isTerminal ? "bg-rose-500" : styles?.spine,
          selected || lit ? "opacity-100" : "opacity-50 group-hover:opacity-100"
        )}
      />

      <span
        className={cn(
          "mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-lg border",
          isTerminal ? "border-rose-500/40 bg-rose-500/10" : cn(styles?.bgSoft, styles?.border)
        )}
      >
        <Icon
          className={cn("h-4 w-4", isTerminal ? "text-rose-700 dark:text-rose-400" : styles?.text)}
        />
      </span>

      <span className="min-w-0 flex-1">
        <span className="flex items-baseline gap-1.5">
          {node.stage && (
            <span className="font-mono text-[10px] tabular-nums text-muted-foreground">
              {String(node.stage.index).padStart(2, "0")}
            </span>
          )}
          <span
            className={cn(
              // No truncate: the card is wide enough for every title, and a long one
              // wraps to a second line rather than losing its tail to an ellipsis.
              "text-[13px] font-semibold leading-tight",
              isTerminal ? "text-rose-700 dark:text-rose-400" : "text-foreground"
            )}
          >
            {node.stage?.title ?? node.terminal?.label}
          </span>
        </span>
        <span className="mt-0.5 block truncate text-[11px] leading-tight text-muted-foreground">
          {node.stage?.role ?? node.terminal?.sublabel}
        </span>
      </span>
    </button>
  )
}

export function FlowChart({
  selectedId,
  onSelect,
  activeNodes,
  activeEdges,
  tracing
}: FlowChartProps) {
  return (
    <>
      {/* Wide layout: absolutely positioned cards over a fixed SVG connector plane.
          Scrolls horizontally on narrow desktops rather than scaling — text stays crisp. */}
      <div className="hidden overflow-x-auto md:block">
        <div
          className="relative mx-auto"
          style={{ width: CANVAS_W, height: CANVAS_H, minWidth: CANVAS_W }}
        >
          <svg
            className="absolute inset-0 h-full w-full"
            viewBox={`0 0 ${CANVAS_W} ${CANVAS_H}`}
            fill="none"
            aria-hidden
          >
            <defs>
              {(["main", "reject", "feedback"] as EdgeVariant[]).map((v) => (
                <marker
                  key={v}
                  id={`arrow-${v}`}
                  viewBox="0 0 10 10"
                  refX="9"
                  refY="5"
                  markerWidth="6"
                  markerHeight="6"
                  orient="auto-start-reverse"
                >
                  <path d="M 0 0 L 10 5 L 0 10 z" fill={EDGE_COLOR[v]} />
                </marker>
              ))}
            </defs>

            {CHART_EDGES.map((edge) => {
              const isActive = activeEdges.includes(edge.id)
              const faded = tracing && !isActive
              return (
                <g key={edge.id} className={cn(faded && "opacity-15")}>
                  <path
                    d={edge.d}
                    stroke={EDGE_COLOR[edge.variant]}
                    strokeWidth={isActive ? 2.5 : 1.5}
                    strokeDasharray={edge.variant === "feedback" ? "7 5" : undefined}
                    markerEnd={`url(#arrow-${edge.variant})`}
                    className={cn(
                      "transition-[stroke-width] duration-200",
                      isActive && "about-edge-flow"
                    )}
                  />
                  {edge.label && edge.labelAt && (
                    <text
                      x={edge.labelAt.x}
                      y={edge.labelAt.y}
                      textAnchor="middle"
                      dominantBaseline="middle"
                      className="fill-current font-mono"
                      style={{
                        fontSize: 10,
                        letterSpacing: "0.04em",
                        color: EDGE_COLOR[edge.variant]
                      }}
                    >
                      {edge.label}
                    </text>
                  )}
                </g>
              )
            })}
          </svg>

          {CHART_NODES.map((node) => (
            <div
              key={node.id}
              className="absolute"
              style={{ left: node.x, top: node.y, width: NODE_W }}
            >
              <NodeCard
                node={node}
                selected={selectedId === node.id}
                dimmed={tracing && !activeNodes.includes(node.id)}
                lit={tracing && activeNodes.includes(node.id)}
                onSelect={onSelect}
              />
            </div>
          ))}
        </div>
      </div>

      {/* Narrow layout: the same sequence, stacked. */}
      <div className="space-y-1.5 md:hidden">
        {CHART_NODES.map((node, i) => (
          <div key={node.id}>
            <NodeCard
              node={node}
              selected={selectedId === node.id}
              dimmed={tracing && !activeNodes.includes(node.id)}
              lit={tracing && activeNodes.includes(node.id)}
              onSelect={onSelect}
            />
            {i < CHART_NODES.length - 1 && (
              <div className="flex justify-center py-0.5">
                <ArrowDown className="h-3.5 w-3.5 text-muted-foreground/50" />
              </div>
            )}
          </div>
        ))}
      </div>
    </>
  )
}
