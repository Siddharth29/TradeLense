/**
 * Format utilities for analyst-grade UI with strict validation
 * No fallbacks - explicit errors when data is invalid
 */

export class FormatError extends Error {
  public field: string
  public value: unknown
  
  constructor(message: string, field: string, value: unknown) {
    super(message)
    this.name = 'FormatError'
    this.field = field
    this.value = value
  }
}

/**
 * Format latency with 2 decimal places and color coding context
 */
export function formatLatency(latency: number | undefined): string {
  if (latency === undefined || latency === null) {
    throw new FormatError('Latency is required', 'latency', latency)
  }
  if (typeof latency !== 'number' || isNaN(latency)) {
    throw new FormatError('Latency must be a valid number', 'latency', latency)
  }
  if (latency < 0) {
    throw new FormatError('Latency cannot be negative', 'latency', latency)
  }
  return `${latency.toFixed(2)}ms`
}

/**
 * Get latency color class based on performance thresholds
 */
export function getLatencyColorClass(latency: number): string {
  if (latency < 1000) return 'text-green-600'
  if (latency < 3000) return 'text-yellow-600'
  if (latency < 5000) return 'text-orange-600'
  return 'text-red-600'
}

/**
 * Format price with appropriate decimal places
 */
export function formatPrice(price: number | undefined): string {
  if (price === undefined || price === null) {
    throw new FormatError('Price is required', 'price', price)
  }
  if (typeof price !== 'number' || isNaN(price)) {
    throw new FormatError('Price must be a valid number', 'price', price)
  }
  return price.toFixed(4)
}

/**
 * Format confidence as percentage
 */
export function formatConfidence(confidence: number | undefined): string {
  if (confidence === undefined || confidence === null) {
    throw new FormatError('Confidence is required', 'confidence', confidence)
  }
  if (typeof confidence !== 'number' || isNaN(confidence)) {
    throw new FormatError('Confidence must be a valid number', 'confidence', confidence)
  }
  if (confidence < 0 || confidence > 1) {
    throw new FormatError('Confidence must be between 0 and 1', 'confidence', confidence)
  }
  return `${(confidence * 100).toFixed(0)}%`
}

/**
 * Get confidence color class
 */
export function getConfidenceColorClass(confidence: number): string {
  if (confidence >= 0.8) return 'text-green-600 bg-green-100'
  if (confidence >= 0.6) return 'text-yellow-600 bg-yellow-100'
  return 'text-red-600 bg-red-100'
}

/**
 * Calculate Risk-Reward ratio
 */
export function calculateRR(entry: number | null, stop: number | null, target: number | null, _signal: string): number {
  if (entry === null || stop === null || target === null) {
    throw new FormatError('Entry, stop, and target are required for RR calculation', 'rr', { entry, stop, target })
  }
  if (!entry || !stop || !target) {
    throw new FormatError('Entry, stop, and target must be positive numbers for RR calculation', 'rr', { entry, stop, target })
  }
  
  const risk = Math.abs(entry - stop)
  const reward = Math.abs(target - entry)
  
  if (risk === 0) {
    throw new FormatError('Risk cannot be zero (entry and stop are the same)', 'rr', { entry, stop })
  }
  
  return reward / risk
}

/**
 * Validate RR ratio meets minimum threshold
 */
export function validateRR(rr: number, minRR: number = 2.5): { valid: boolean; message: string } {
  if (rr < minRR) {
    return { 
      valid: false, 
      message: `RR ${rr.toFixed(2)} below minimum ${minRR}` 
    }
  }
  return { 
    valid: true, 
    message: `RR ${rr.toFixed(2)} meets minimum ${minRR}` 
  }
}

/**
 * Validate TTL is in allowed range
 */
export function validateTTL(ttl: number | undefined): { valid: boolean; message: string } {
  const allowedValues = [45, 60, 90, 120]
  if (ttl === undefined || ttl === null) {
    return { valid: false, message: 'TTL not set' }
  }
  if (!allowedValues.includes(ttl)) {
    return { 
      valid: false, 
      message: `TTL ${ttl} not in allowed values [${allowedValues.join(', ')}]` 
    }
  }
  return { valid: true, message: `TTL ${ttl} is valid` }
}

/**
 * Validate active windows
 */
export function validateActiveWindows(c5?: number, c15?: number): { valid: boolean; message: string } {
  const allowedC5 = [40, 50, 60, 75, 90]
  const allowedC15 = [20, 30, 40]
  
  const c5Valid = c5 === undefined || allowedC5.includes(c5)
  const c15Valid = c15 === undefined || allowedC15.includes(c15)
  
  if (!c5Valid || !c15Valid) {
    const issues = []
    if (!c5Valid) issues.push(`C5: ${c5} not in [${allowedC5.join(', ')}]`)
    if (!c15Valid) issues.push(`C15: ${c15} not in [${allowedC15.join(', ')}]`)
    return { valid: false, message: issues.join(', ') }
  }
  
  return { valid: true, message: 'Active windows are valid' }
}

/**
 * Parse JSON response content safely
 */
export function parseResponseContent(content: string, responseType: string): any {
  if (responseType === 'json') {
    try {
      return JSON.parse(content)
    } catch (error) {
      throw new FormatError(
        `Failed to parse JSON response: ${error instanceof Error ? error.message : 'Unknown error'}`,
        'response_content',
        content
      )
    }
  }
  return null
}

/**
 * Calculate decision completeness score
 */
export function calculateDecisionCompleteness(decision: any): { score: number; missing: string[] } {
  const requiredFields = ['signal', 'entry', 'stop', 'target']
  const missing: string[] = []
  
  for (const field of requiredFields) {
    if (decision[field] === undefined || decision[field] === null) {
      missing.push(field)
    }
  }
  
  const score = (requiredFields.length - missing.length) / requiredFields.length
  return { score, missing }
}

/**
 * Format number with appropriate precision
 */
export function formatNumber(num: number | undefined, decimals: number = 2): string {
  if (num === undefined || num === null) {
    throw new FormatError('Number is required', 'number', num)
  }
  if (typeof num !== 'number' || isNaN(num)) {
    throw new FormatError('Number must be a valid number', 'number', num)
  }
  return num.toFixed(decimals)
}

/**
 * Format tokens used with locale
 */
export function formatTokens(tokens: number | undefined): string {
  if (tokens === undefined || tokens === null) {
    throw new FormatError('Tokens count is required', 'tokens', tokens)
  }
  if (typeof tokens !== 'number' || isNaN(tokens)) {
    throw new FormatError('Tokens must be a valid number', 'tokens', tokens)
  }
  return tokens.toLocaleString()
}
