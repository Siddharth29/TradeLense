import type { Time } from "lightweight-charts"

/**
 * Convert various time formats to Lightweight Charts Time type.
 * Handles ISO strings, Date objects, Unix timestamps, and validates input.
 */
export function convertToChartTime(time: string | Date | number | null | undefined): Time | null {
  if (!time) return null
  
  try {
    let timestamp: number
    
    if (typeof time === "number") {
      // Already a timestamp - assume seconds if < year 2000 in ms, otherwise assume ms
      if (time < 946684800000) {
        // Less than year 2000 in ms, assume seconds
        timestamp = time
      } else {
        // Assume milliseconds, convert to seconds
        timestamp = Math.floor(time / 1000)
      }
    } else if (time instanceof Date) {
      // Date object - convert to Unix timestamp (seconds)
      timestamp = Math.floor(time.getTime() / 1000)
    } else if (typeof time === "string") {
      // String - parse as ISO date
      const date = new Date(time)
      if (isNaN(date.getTime())) {
        console.warn(`Invalid date string: ${time}`)
        return null
      }
      timestamp = Math.floor(date.getTime() / 1000)
    } else {
      console.warn(`Unsupported time type: ${typeof time}`, time)
      return null
    }
    
    // Validate timestamp is reasonable (between year 2000 and 2100)
    if (timestamp < 946684800 || timestamp > 4102444800) {
      console.warn(`Timestamp out of reasonable range: ${timestamp}`)
      return null
    }
    
    return timestamp as Time
  } catch (error) {
    console.error(`Error converting time: ${time}`, error)
    return null
  }
}

/**
 * Validate that a time value is a valid chart time.
 */
export function isValidChartTime(time: Time | null): time is Time {
  return time !== null && typeof time === "number" && time > 0
}

