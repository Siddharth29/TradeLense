import type { LucideIcon } from "lucide-react"
import {
  Radio,
  Filter,
  Brain,
  FileCheck2,
  ShieldCheck,
  Send,
  Activity,
  GitCompare,
  ClipboardList,
  RefreshCcw
} from "lucide-react"

export type StageId =
  | "data"
  | "processing"
  | "ai"
  | "decision"
  | "risk"
  | "execution"
  | "monitoring"
  | "reconciliation"
  | "audit"
  | "improvement"

export interface InfoBlock {
  purpose: string
  input: string
  processing: string
  output: string
  safety: string
  failure: string
}

export interface SubNode {
  label: string
  blurb: string
}

export interface Stage {
  id: StageId
  index: number
  title: string
  /** Short label rendered on the canvas node — keep under ~28 chars. */
  role: string
  tagline: string
  icon: LucideIcon
  accent: string
  info: InfoBlock
  subNodes: SubNode[]
  detail: string[]
}

export const STAGES: Stage[] = [
  {
    id: "data",
    index: 1,
    title: "Market Data",
    role: "Broker feed ingestion",
    tagline: "Continuous multi-timeframe candles, pulled straight from the broker feed.",
    icon: Radio,
    accent: "sky",
    info: {
      purpose:
        "Give every downstream stage a synchronized, multi-timeframe view of price — never a single snapshot.",
      input:
        "Live tick and candle stream from the MetaTrader 5 broker connection, for each of the six tracked instruments (XAUUSD, XAGUSD, EURUSD, GBPUSD, BTCUSD, USTEC).",
      processing:
        "Candles are pulled, timestamped, and cached per asset as each timeframe closes. Open (still-forming) candles are tracked separately from closed ones.",
      output:
        "A rolling window of closed 5-minute and 15-minute candles, plus 1-hour trend context, tagged with the current live price and prior-candle state.",
      safety:
        "Every candle is asset- and timeframe-scoped — a stale or partial candle for one instrument can never leak into another instrument's window.",
      failure:
        "If the feed goes stale (broker maintenance, disconnect, frozen ticks) the cycle is skipped for that asset rather than fed synthetic or repeated data."
    },
    subNodes: [
      { label: "5-minute candles", blurb: "Trigger timeframe. Reacts fast enough to catch an entry, slow enough to filter tick noise." },
      { label: "15-minute candles", blurb: "Structure timeframe. Confirms the setup seen on the 5-minute chart isn't a false break." },
      { label: "1-hour context", blurb: "Trend filter. Keeps the system from fading a strong higher-timeframe move on a short-term signal." },
      { label: "OHLCV", blurb: "Open / High / Low / Close / Volume — the full shape of every candle, not just its close." },
      { label: "Current market state", blurb: "Live bid/ask and the still-forming candle, tracked separately so it's never mistaken for a confirmed bar." },
      { label: "Previous closed candles", blurb: "The most recent fully-closed bar on each timeframe — the only bars the AI is ever allowed to reason about." }
    ],
    detail: [
      "The system does not wait for a human to click refresh. Each of the six tracked assets runs its own independent monitoring loop, watching for the next candle close on its configured timeframes.",
      "Closed candles only reach the AI — a candle that is still forming can repaint (its high, low, or close can still change), so trading off it would mean reasoning about a price bar that doesn't exist yet.",
      "Multiple timeframes exist because no single one tells the whole story: the 5-minute chart finds the moment, the 15-minute chart confirms the structure, and the 1-hour chart confirms the surrounding trend hasn't turned against the trade."
    ]
  },
  {
    id: "processing",
    index: 2,
    title: "Data Processing & Preparation",
    role: "Validation & context build",
    tagline: "Raw candles are validated, windowed, and turned into structured context — never handed to the AI as-is.",
    icon: Filter,
    accent: "cyan",
    info: {
      purpose:
        "Turn a stream of candles into a bounded, relevant, machine-readable snapshot the AI can reason about consistently.",
      input:
        "Raw closed candles across all tracked timeframes for one asset, plus that asset's current risk configuration.",
      processing:
        "Validate candle integrity → confirm closure → select the relevant historical window → derive market structure → compute volatility and risk-unit sizing → normalize into one structured payload.",
      output:
        "A single structured market-context object: recent price action, structure levels, a volatility read, and a risk unit — ready to be reasoned about.",
      safety:
        "Missing, malformed, or out-of-sequence candles are caught here and the cycle is aborted before it ever reaches the AI.",
      failure:
        "Invalid or incomplete data → cycle skipped for that asset, logged, and retried on the next candle close. No trade is ever proposed from a broken input."
    },
    subNodes: [
      { label: "Data validation", blurb: "Checks each candle for sane OHLC ordering, non-zero range, and continuity with the prior bar before it's trusted." },
      { label: "Closed-candle confirmation", blurb: "Cross-checks candle timestamps against the exchange clock so a forming bar can never pass as a closed one." },
      { label: "Historical window selection", blurb: "Picks a bounded, relevant slice of recent candles — enough for context, not so much that stale structure dominates." },
      { label: "Market structure / context", blurb: "Derives swing highs/lows and recent structure the AI references by name, rather than re-deriving from raw numbers." },
      { label: "Volatility calculation", blurb: "Measures current volatility against its own historical distribution, so 'normal' is asset-specific, not a fixed number." },
      { label: "Risk-unit calculation", blurb: "Converts volatility into a concrete price distance — the basis every stop-loss and target is measured against." },
      { label: "Input normalization", blurb: "Formats the payload identically every cycle, so the AI reasons about structure, not inconsistent formatting." },
      { label: "Missing/invalid data detection", blurb: "The gate every other step in this stage feeds into — if anything upstream looks wrong, the cycle stops here." }
    ],
    detail: [
      "This is the layer most people assume doesn't exist: an LLM that 'just looks at charts.' In production it never does — it receives a pre-validated, pre-structured context object, not a raw price feed.",
      "Volatility and risk-unit calculations happen before the AI is ever called, so every stop-loss and target the AI proposes is anchored to the instrument's own recent behavior, not a fixed pip or point value.",
      "If validation fails at any point in this stage, the cycle terminates here — the AI layer is never invoked for that candle close, and no proposal is generated from questionable data."
    ]
  },
  {
    id: "ai",
    index: 3,
    title: "AI Decision Layer",
    role: "Setup reasoning",
    tagline: "A trading-focused reasoning pass over the structured context — proposing a setup, not placing an order.",
    icon: Brain,
    accent: "violet",
    info: {
      purpose:
        "Evaluate the prepared market context for a valid, well-defined trade setup — or conclude that none exists.",
      input:
        "The structured market-context object from the processing stage: recent structure, volatility, risk unit, and multi-timeframe candle summaries.",
      processing:
        "The model reasons over price action and structure using established trading concepts — trend, structure breaks, retests, order blocks — to identify whether a valid setup is present, then derives a complete trade plan from it.",
      output:
        "A trade plan: direction, entry, stop-loss, target(s), expected risk/reward, a time horizon, and the model's own stated confidence — or an explicit no-valid-setup result.",
      safety:
        "The model only ever sees the current asset's own validated context; it cannot see or influence other assets' cycles or the account's live positions.",
      failure:
        "No qualifying setup → the model returns flat/no-trade. This is a normal, expected outcome, not an error — most cycles end here."
    },
    subNodes: [
      { label: "Setup identification", blurb: "Looks for a specific, nameable pattern in the structure — not a vague 'price might go up.'" },
      { label: "Entry", blurb: "The precise price at which the plan would trigger." },
      { label: "Stop-loss", blurb: "Derived from the risk unit computed upstream, anchored to invalidation structure, not an arbitrary distance." },
      { label: "Target", blurb: "One or more take-profit levels, checked against the same structure that produced the entry." },
      { label: "Direction", blurb: "Long or short — explicit, never inferred downstream." },
      { label: "Risk/reward", blurb: "The ratio between stop distance and target distance, computed before any gate sees the plan." },
      { label: "Time horizon", blurb: "How long the setup is expected to remain valid before it should be considered stale." },
      { label: "Confidence / validity", blurb: "The model's confidence in the setup — informative, but not a bypass for the gates that follow." }
    ],
    detail: [
      "A proposal from this layer is a hypothesis, not an instruction. Nothing downstream treats 'the AI said buy' as a reason to buy — it's the input to a separate validation stage.",
      "The reasoning is scoped deliberately: one asset, one candle close, one context object. There is no shared memory of the AI's own past proposals influencing the current one, which keeps each cycle independently auditable.",
      "Because 'no trade' is a legitimate, expected output — not a failure — the system is not structurally biased toward always finding a setup just because it was asked to look for one."
    ]
  },
  {
    id: "decision",
    index: 4,
    title: "Decision",
    role: "Proposal schema",
    tagline: "The AI's output becomes a formal trade proposal — a candidate, not a command.",
    icon: FileCheck2,
    accent: "fuchsia",
    info: {
      purpose:
        "Convert the model's reasoning into a structured, machine-checkable proposal, and draw a hard line between 'proposed' and 'ordered.'",
      input:
        "The AI's trade plan (direction, entry, stop, target, R:R, horizon, confidence) or its flat/no-trade result.",
      processing:
        "The proposal is parsed into a strict schema, tagged with the asset, the candle it was generated from, and a timestamp. A flat result ends the cycle here.",
      output:
        "One structured trade proposal, queued for risk evaluation — or nothing, if the model found no valid setup.",
      safety:
        "A proposal has no execution privileges of its own. It is inert until — and unless — it clears every gate in the next stage.",
      failure:
        "A proposal that fails schema validation (missing field, malformed price, inconsistent direction/stop) is rejected here, before it ever reaches a risk check, and logged as a schema failure."
    },
    subNodes: [
      { label: "Structured proposal", blurb: "Direction, entry, stop, target(s), R:R, horizon, and confidence, packaged into one auditable object." },
      { label: "Candle + timestamp tag", blurb: "Every proposal is permanently linked to the exact candle close that produced it, for later replay and audit." },
      { label: "Flat / no-trade path", blurb: "The majority outcome. Logged the same way a proposal is, so a pass is just as visible as a proposal." },
      { label: "Schema validation", blurb: "A malformed proposal is caught and discarded before risk evaluation ever sees it." }
    ],
    detail: [
      "This is the stage that makes the single most important distinction on this page: proposing a trade and executing a trade are two different systems, connected by a gate the AI does not control.",
      "Every proposal — accepted or not — is written to the record at this point, with the exact candle and timestamp that produced it. Nothing about a trade idea is ever silently discarded.",
      "A flat result is recorded with the same rigor as a proposal. That symmetry matters: it's what lets an audit later ask 'was the AI right to pass on this move?' as easily as 'was the AI right to enter?'"
    ]
  },
  {
    id: "risk",
    index: 5,
    title: "Risk Gates",
    role: "Deterministic checks",
    tagline: "Deterministic, non-negotiable checks. A proposal only becomes an order if every gate passes.",
    icon: ShieldCheck,
    accent: "amber",
    info: {
      purpose:
        "Stand between the AI and the broker as a fixed, deterministic control layer — the same checks, every time, regardless of how confident the proposal looked.",
      input:
        "One structured trade proposal from the Decision stage, plus live account state — open positions, exposure, recent signals.",
      processing:
        "Runs the proposal through an ordered checklist: data validity, entry/stop/target sanity, risk/reward floor, position and exposure limits, session and duplicate checks, execution feasibility.",
      output:
        "A binary verdict — PASS with a sized, executable order, or REJECT with a specific, logged reason.",
      safety:
        "Every gate is deterministic code, not a model call — the same input always produces the same verdict, and the checklist is fixed independent of the AI's stated confidence.",
      failure:
        "REJECT is not a dead end — it's a recorded outcome. The proposal, the failing gate, and the reason are written to the record exactly like an accepted trade would be."
    },
    subNodes: [
      { label: "Invalid market data", blurb: "Stale or inconsistent price data at decision time blocks the order outright." },
      { label: "Invalid entry", blurb: "Entry price that doesn't reconcile with current market price is rejected." },
      { label: "Stop-loss too close / too far", blurb: "Stops inside broker minimum distance, or so wide they'd blow past the risk unit, are both rejected." },
      { label: "Target outside allowed range", blurb: "Take-profit levels inconsistent with the stop distance and structure are rejected." },
      { label: "Poor risk/reward", blurb: "Below the minimum reward-to-risk floor, the trade is rejected regardless of confidence." },
      { label: "Position already active", blurb: "An open position on the same asset blocks a new one from stacking on top of it." },
      { label: "Risk limit exceeded", blurb: "Account-level exposure and per-trade risk percentage are checked before sizing, not after." },
      { label: "Session restriction", blurb: "Assets with defined trading sessions are blocked outside them, or during broker maintenance windows." },
      { label: "Duplicate signal", blurb: "The same setup re-proposed on a candle that already produced a decision is blocked from double-firing." },
      { label: "Market conditions unsuitable", blurb: "Choppy, low-structure, or otherwise unfavorable conditions can block entry independent of the setup's own quality." },
      { label: "Execution constraints", blurb: "Broker-side minimums — lot size, margin, symbol trading state — are checked before an order is ever sent." },
      { label: "Other safety checks", blurb: "Additional deterministic guards, layered in as new failure modes are discovered in audits." }
    ],
    detail: [
      "This is deliberately boring, deterministic code — not another model call. Boring is the point: the same proposal, run through this stage twice, produces the same verdict twice.",
      "Every one of these checks exists because a specific failure mode was observed and closed. The gate list grows over time; it never shrinks without a documented reason.",
      "A rejection is a first-class outcome, not a silent drop. It's written to the record with the specific gate that failed and why, which is what makes the system's 'no' decisions just as auditable as its 'yes' decisions."
    ]
  },
  {
    id: "execution",
    index: 6,
    title: "Order Execution",
    role: "Sizing & broker API",
    tagline: "An approved plan is sized against the live account and sent to the broker as a real order.",
    icon: Send,
    accent: "emerald",
    info: {
      purpose:
        "Turn an approved, gate-cleared trade plan into an actual broker order — and confirm the broker agrees before calling it a position.",
      input:
        "The gate-approved trade plan (direction, entry, stop, target) plus live account balance and the account's configured risk percentage.",
      processing:
        "Position size is calculated from account risk and the stop distance, checked against broker lot-size and margin constraints, then submitted as an order through the broker API.",
      output:
        "A broker order response — accepted, filled, or rejected — with the broker's own fill price, not the price the plan assumed.",
      safety:
        "Every order carries a unique position tag written by the broker at the moment of fill, so the system can always tell its own positions apart from anything else on the account, including manual trades on the same account.",
      failure:
        "A broker-side rejection (margin, symbol trading state, requote) is caught, logged with the broker's own error, and does not silently retry into a duplicate order."
    },
    subNodes: [
      { label: "Account balance", blurb: "Pulled live from the broker at sizing time, not cached from an earlier check." },
      { label: "Risk percentage", blurb: "The fraction of account equity the plan is allowed to risk on this single trade." },
      { label: "Position / lot sizing", blurb: "Derived from risk percentage and stop distance, then rounded to the broker's tradeable lot increments." },
      { label: "Entry / stop / target", blurb: "Carried forward from the approved plan, translated into broker order fields." },
      { label: "Broker constraints", blurb: "Minimum lot, margin requirement, and symbol trading state, checked before submission." },
      { label: "Order submission", blurb: "Sent to the broker through its trading API — the same interface a human places manual orders through." },
      { label: "Broker response", blurb: "The broker's own acknowledgement — accepted, rejected, or requoted — is what the system trusts, not its own request." },
      { label: "Fill confirmation", blurb: "The broker-reported fill price and time become the position's true entry, replacing the plan's assumed entry." }
    ],
    detail: [
      "The plan the AI proposed and the order the broker confirms are treated as two different objects on purpose. The system never assumes its own request executed exactly as sent.",
      "Position sizing happens here, against the live account, not earlier in the pipeline — so a plan sized for one account state can't get executed against a stale one.",
      "Because every position is tagged at the moment of fill, the system can share an account with manual trading activity and still know, unambiguously, which positions are its own."
    ]
  },
  {
    id: "monitoring",
    index: 7,
    title: "Live Trade Monitoring",
    role: "Position lifecycle",
    tagline: "Execution isn't the finish line. Every open position is watched, tick by tick, until it closes.",
    icon: Activity,
    accent: "blue",
    info: {
      purpose:
        "Keep the system's understanding of a trade synchronized with reality for its entire lifetime, not just at the moment of entry.",
      input:
        "The broker-confirmed position (entry, stop, target, fill time) plus the live market feed for that asset.",
      processing:
        "Continuously polls order and position status, current market price, and broker-side position state; compares each against what the system expects.",
      output:
        "A live position record — current status, unrealized P&L, and any detected mismatch — updated as market and broker state change.",
      safety:
        "Monitoring is asset-scoped and independent per position, so a stall or error watching one trade can't blind the system to another.",
      failure:
        "A detected mismatch (stop not where expected, position missing, broker error) isn't silently absorbed — it's flagged and handed to reconciliation."
    },
    subNodes: [
      { label: "Order status", blurb: "Whether the originating order is still working, filled, or was cancelled/rejected." },
      { label: "Position status", blurb: "Open, partially closed, or closed — tracked independently of what the system last logged." },
      { label: "Entry price", blurb: "The broker-confirmed fill, checked against what the plan assumed." },
      { label: "Stop-loss / target", blurb: "Verified to still be attached at the broker, not just recorded locally." },
      { label: "Current market state", blurb: "Live price against entry, stop, and target, for real-time unrealized P&L." },
      { label: "Broker-side position", blurb: "The account's actual open position, pulled directly from the broker — never assumed from local memory." },
      { label: "Execution / fill info", blurb: "Partial fills, modifications, and closes, as the broker reports them." },
      { label: "Errors or mismatches", blurb: "Any divergence between expected and broker-reported state, surfaced rather than overwritten." }
    ],
    detail: [
      "A filled order is the start of a position's monitored lifetime, not the end of the pipeline's involvement with it.",
      "The system re-checks broker state on an ongoing basis rather than trusting its own last-known record — the broker is always the source of truth for what's actually open.",
      "This is also where a ghost position — anything on the account not tagged as the system's own, including manual trades — gets recognized and left alone rather than mistakenly managed."
    ]
  },
  {
    id: "reconciliation",
    index: 8,
    title: "Reconciliation",
    role: "Broker truth check",
    tagline: "The system's internal record is checked against broker reality — never assumed to match it.",
    icon: GitCompare,
    accent: "orange",
    info: {
      purpose:
        "Verify, explicitly, that what the system believes happened is what actually happened at the broker.",
      input:
        "The system's own record of the trade lifecycle (plan → order → expected position) and the broker's authoritative account state.",
      processing:
        "Compares each stage's expected outcome against the broker's reported outcome: order status, fill price, position existence, and current stop/target.",
      output:
        "A confirmed match, or a flagged mismatch with its category and the specific discrepancy.",
      safety:
        "Reconciliation runs independently of execution and monitoring, so a bug in either of those layers can't also hide its own mismatch from being caught.",
      failure:
        "A mismatch is never auto-resolved by assumption. It's flagged, logged, and — for anything affecting a live position — surfaced for the appropriate handling."
    },
    subNodes: [
      { label: "Order rejected", blurb: "System expected a fill; broker reports the order never went through." },
      { label: "Partial fill", blurb: "Broker filled less size than the order requested." },
      { label: "Different execution price", blurb: "Fill price differs from the requested entry — normal in fast markets, but always recorded, never ignored." },
      { label: "Missing position", blurb: "System expects an open position the broker doesn't show — closed, stopped out, or never opened." },
      { label: "Unexpected position", blurb: "A position exists at the broker the system didn't expect — flagged for identification, never silently adopted." },
      { label: "Stop/target mismatch", blurb: "Broker-side stop or target differs from what the system last set." },
      { label: "Broker/system state mismatch", blurb: "Any other divergence between the two records of truth." }
    ],
    detail: [
      "This stage exists because 'the system logged it' and 'the broker executed it' are two different claims, and only one of them is authoritative.",
      "It runs on a schedule independent of any single trade's lifecycle, so positions opened by the system, closed by the system, or touched manually on the same account all get checked the same way.",
      "A clean reconciliation isn't just a status — it's the evidence, checked continuously, that the rest of the pipeline's internal bookkeeping can be trusted."
    ]
  },
  {
    id: "audit",
    index: 9,
    title: "Daily Audit",
    role: "Daily reconstruction",
    tagline: "Every trading day is reconstructed and reviewed — proposals, rejections, fills, and misses alike.",
    icon: ClipboardList,
    accent: "rose",
    info: {
      purpose:
        "Turn a day of raw activity into an explicit, evidence-based review of how the system actually performed — not just what it earned.",
      input:
        "Every event the day produced: candles, AI proposals, gate verdicts, orders, fills, monitored positions, reconciliation results, and errors.",
      processing:
        "Trades are reconstructed on real market data, matched against the broker's own deal history, and checked against a running library of previously-identified failure patterns.",
      output:
        "A written daily record: what happened, why, which known patterns recurred, and what — if anything — should change.",
      safety:
        "The audit is scoped to the system's own tagged activity, so it never conflates the system's performance with unrelated manual activity on the same account.",
      failure:
        "An audit that can't reconcile a day's data (missing logs, broker gaps) is flagged as incomplete rather than silently producing a partial, misleadingly clean report."
    },
    subNodes: [
      { label: "Accepted signals", blurb: "Every proposal that cleared the risk gates and became an order." },
      { label: "Rejected signals", blurb: "Every proposal that didn't — with the specific gate and reason." },
      { label: "Orders & fills", blurb: "What was actually sent to the broker, and what the broker actually did with it." },
      { label: "P&L and execution quality", blurb: "Outcome, but also how cleanly the plan was executed — slippage, partial fills, timing." },
      { label: "Missed opportunities", blurb: "Moves the system didn't act on — whether correctly filtered or a genuine gap." },
      { label: "Failed trades", blurb: "Losses, examined for whether the loss was the system working as designed or a failure mode." }
    ],
    detail: [
      "The audit's job is not to report profit and loss — that's an input to it, not the output. The output is a diagnosis: did the system behave the way it was designed to, and is a failure mode repeating.",
      "Every day's audit asks the same standing questions: was the AI's read of the setup correct, was the entry timed well, was the target realistic, did the risk gates behave correctly, did execution match the plan.",
      "Findings are written into a standing pattern library — a running, cited record of every identified behavior, good or bad — so the next audit builds on the last one instead of starting from zero."
    ]
  },
  {
    id: "improvement",
    index: 10,
    title: "Continuous Improvement",
    role: "Validated change loop",
    tagline: "Findings become hypotheses, hypotheses become tested changes, and only validated changes reach production.",
    icon: RefreshCcw,
    accent: "lime",
    info: {
      purpose:
        "Close the loop from observed failure to a measurably better system, without ever skipping the evidence step.",
      input:
        "Patterns and failure modes identified by the daily audit, cited against specific trades and dated evidence.",
      processing:
        "Observation → hypothesis → a scoped change to code, prompt, or risk configuration → backtest against historical data → compare against the previous version → validate → deploy.",
      output:
        "A deployed change with a documented reason, a before/after comparison, and a record of what it was expected to fix.",
      safety:
        "Nothing reaches production on the strength of a single number or a hunch. Every change is compared against its predecessor on the same historical data before it ships.",
      failure:
        "A change that doesn't clear its backtest comparison isn't deployed — it's revised or discarded, and that outcome is recorded too, so the same idea isn't silently retried later."
    },
    subNodes: [
      { label: "Observation", blurb: "A pattern the daily audit flagged, cited to specific trades and dates." },
      { label: "Hypothesis", blurb: "A specific, falsifiable explanation for the pattern — not a vague 'improve entries.'" },
      { label: "Change", blurb: "A scoped edit to code, a prompt, or a risk parameter — never a broad rewrite justified by one incident." },
      { label: "Backtest", blurb: "The change is run against historical data before it ever touches a live account." },
      { label: "Compare", blurb: "Backtest results are measured against the previous version's results on the same data, not in isolation." },
      { label: "Validate", blurb: "A change that improves the target pattern without degrading others clears validation." },
      { label: "Deploy", blurb: "Ships with its own record: what changed, why, and what evidence supported it." }
    ],
    detail: [
      "This loop is what keeps the system from being a fixed strategy frozen at whatever state it launched in — and equally, from being a system that changes on vibes.",
      "Every shipped change traces back to a specific, dated piece of audit evidence. If asked why the system behaves a certain way, the answer is always a documented incident, not an assumption.",
      "Backtesting here is a gate, not a formality: a change has to beat its predecessor on historical data before it's trusted with real capital again."
    ]
  }
]

export interface Invariant {
  key: string
  statement: string
}

/** Hard guarantees, stated once. Per-stage detail lives in each stage's `info.safety`. */
export const INVARIANTS: Invariant[] = [
  { key: "no-direct-broker", statement: "The model proposes. Deterministic code disposes." },
  { key: "recorded", statement: "Accepted and rejected decisions are both written to the record." },
  { key: "broker-truth", statement: "Broker state is the source of truth, verified continuously." },
  { key: "gated-change", statement: "No change ships without a backtest comparison against the version it replaces." }
]

export interface LifecycleFrame {
  stageId: StageId
  headline: string
  detail: string
}

export interface LifecycleScenario {
  id: "approved" | "rejected"
  label: string
  asset: string
  frames: LifecycleFrame[]
  outcome: string
  reachedStageCount: number
}

export const LIFECYCLE_SCENARIOS: LifecycleScenario[] = [
  {
    id: "approved",
    label: "Trade approved",
    asset: "XAUUSD",
    outcome: "Order filled, monitored, reconciled, and fed into tonight's audit.",
    reachedStageCount: 10,
    frames: [
      { stageId: "data", headline: "5m & 15m candles close on XAUUSD", detail: "1h context confirms the higher-timeframe trend is still intact." },
      { stageId: "processing", headline: "Context validated and structured", detail: "Closed-candle check passes; volatility and risk unit computed." },
      { stageId: "ai", headline: "Setup identified", detail: "Bullish order-block retest, entry 2,412.40, target R:R 2.1." },
      { stageId: "decision", headline: "Trade plan drafted", detail: "Structured proposal queued for risk evaluation — not yet an order." },
      { stageId: "risk", headline: "7 gates checked — PASS", detail: "R:R, exposure, session, and duplicate checks all clear." },
      { stageId: "execution", headline: "Order sent to broker", detail: "0.02 lot sized from account risk % and stop distance." },
      { stageId: "monitoring", headline: "Fill confirmed @ 2,412.55", detail: "Stop and target tracked tick by tick against live price." },
      { stageId: "reconciliation", headline: "Broker matches system record", detail: "No mismatch — order, fill, and position all reconcile cleanly." },
      { stageId: "audit", headline: "Logged for tonight's audit", detail: "Entry timing, R-multiple, and execution quality recorded." },
      { stageId: "improvement", headline: "Pattern tagged in the library", detail: "Feeds the next review cycle alongside every other trade." }
    ]
  },
  {
    id: "rejected",
    label: "Trade rejected",
    asset: "GBPUSD",
    outcome: "Proposal never became an order. Rejection logged with its exact reason.",
    reachedStageCount: 5,
    frames: [
      { stageId: "data", headline: "5m & 15m candles close on GBPUSD", detail: "1h context shows a weaker, less committed trend." },
      { stageId: "processing", headline: "Context validated and structured", detail: "Volatility read is elevated; risk unit computed accordingly." },
      { stageId: "ai", headline: "Setup proposed", detail: "Breakout continuation, entry 1.2634 — a lower-conviction read." },
      { stageId: "decision", headline: "Trade plan drafted", detail: "Structured proposal queued for risk evaluation — not yet an order." },
      { stageId: "risk", headline: "7 gates checked — 1 FAIL", detail: "REJECTED: stop_loss_too_close — 0.6R measured against a 1.2R minimum." }
    ]
  }
]

export interface ReconciliationScenario {
  id: string
  label: string
  status: "match" | "mismatch"
  category?: string
  rows: { field: string; expected: string; broker: string; match: boolean }[]
}

export const RECONCILIATION_SCENARIOS: ReconciliationScenario[] = [
  {
    id: "clean",
    label: "Clean match",
    status: "match",
    rows: [
      { field: "Order status", expected: "Filled", broker: "Filled", match: true },
      { field: "Fill price", expected: "2,412.40", broker: "2,412.55", match: true },
      { field: "Position size", expected: "0.02 lot", broker: "0.02 lot", match: true },
      { field: "Stop-loss", expected: "2,406.10", broker: "2,406.10", match: true },
      { field: "Take-profit", expected: "2,425.00", broker: "2,425.00", match: true }
    ]
  },
  {
    id: "partial-fill",
    label: "Partial fill",
    status: "mismatch",
    category: "Partial fill",
    rows: [
      { field: "Order status", expected: "Filled", broker: "Partially filled", match: false },
      { field: "Fill price", expected: "1.2634", broker: "1.2634", match: true },
      { field: "Position size", expected: "0.05 lot", broker: "0.03 lot", match: false },
      { field: "Stop-loss", expected: "1.2601", broker: "1.2601", match: true },
      { field: "Take-profit", expected: "1.2700", broker: "1.2700", match: true }
    ]
  },
  {
    id: "slippage",
    label: "Execution slippage",
    status: "mismatch",
    category: "Different execution price",
    rows: [
      { field: "Order status", expected: "Filled", broker: "Filled", match: true },
      { field: "Fill price", expected: "62,450.00", broker: "62,481.50", match: false },
      { field: "Position size", expected: "0.01 lot", broker: "0.01 lot", match: true },
      { field: "Stop-loss", expected: "62,180.00", broker: "62,180.00", match: true },
      { field: "Take-profit", expected: "63,050.00", broker: "63,050.00", match: true }
    ]
  },
  {
    id: "missing-position",
    label: "Missing position",
    status: "mismatch",
    category: "Missing position",
    rows: [
      { field: "Order status", expected: "Filled", broker: "Filled", match: true },
      { field: "Position open", expected: "Open", broker: "Not found", match: false },
      { field: "Position size", expected: "0.10 lot", broker: "—", match: false },
      { field: "Stop-loss", expected: "18,540.00", broker: "—", match: false },
      { field: "Take-profit", expected: "18,720.00", broker: "—", match: false }
    ]
  }
]

export interface RiskGateSample {
  id: string
  verdict: "pass" | "reject"
  gate: string
  reason: string
}

export const RISK_GATE_SAMPLES: RiskGateSample[] = [
  { id: "pass-1", verdict: "pass", gate: "All 7 gates", reason: "R:R 2.1, exposure within limit, no duplicate, session open." },
  { id: "reject-rr", verdict: "reject", gate: "Risk/reward", reason: "0.9R measured — below the minimum reward-to-risk floor." },
  { id: "reject-sl", verdict: "reject", gate: "Stop-loss too close", reason: "0.6R measured against a 1.2R minimum stop distance." },
  { id: "reject-dup", verdict: "reject", gate: "Duplicate signal", reason: "Same setup already proposed on this candle close." },
  { id: "reject-active", verdict: "reject", gate: "Position already active", reason: "An open position on this asset already exists." },
  { id: "reject-session", verdict: "reject", gate: "Session restriction", reason: "Outside this asset's configured trading session." },
  { id: "pass-2", verdict: "pass", gate: "All 7 gates", reason: "R:R 1.8, flat account exposure, fresh signal, session open." }
]

export interface BacktestMetric {
  label: string
  baseline: string
  candidate: string
  improved: boolean
}

export const BACKTEST_METRICS: BacktestMetric[] = [
  { label: "Trades", baseline: "148", candidate: "112", improved: true },
  { label: "Win rate", baseline: "41%", candidate: "47%", improved: true },
  { label: "Avg. risk/reward", baseline: "1.4R", candidate: "1.8R", improved: true },
  { label: "Max drawdown", baseline: "9.6%", candidate: "6.1%", improved: true },
  { label: "Rejection rate", baseline: "62%", candidate: "71%", improved: true }
]

export interface LoopNode {
  label: string
  blurb: string
}

export const IMPROVEMENT_LOOP: LoopNode[] = [
  { label: "Production", blurb: "The system runs live, generating the only kind of evidence that counts — real cycles, real gate verdicts, real fills." },
  { label: "Audit", blurb: "Every trading day is reconstructed and reviewed against the standing pattern library." },
  { label: "Failure analysis", blurb: "Recurring patterns are named, cited to specific trades, and turned into a falsifiable hypothesis." },
  { label: "Improvement", blurb: "A scoped change to code, a prompt, or a risk parameter — sized to the evidence, not a rewrite." },
  { label: "Backtest", blurb: "The change is run against historical data before it's trusted with a live account again." },
  { label: "Validate", blurb: "Compared against the previous version on the same data — it has to win, not just look different." },
  { label: "Deploy", blurb: "Ships with its reason and its evidence on record, and the loop starts again." }
]
