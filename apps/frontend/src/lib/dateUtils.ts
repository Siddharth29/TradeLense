/**
 * Date and time utilities for the application
 */

/**
 * Convert UTC timestamp to IST (Indian Standard Time)
 * Uses proper timezone conversion instead of manual offset
 */
export function convertToIST(utcTimestamp: string | Date): string {
  // Handle invalid inputs that cause "undefined" in logs
  if (!utcTimestamp || utcTimestamp === 'undefined' || utcTimestamp === 'null') {
    return 'Invalid Date';
  }
  
  const date = typeof utcTimestamp === 'string' ? new Date(utcTimestamp) : utcTimestamp;
  
  // Check if date is valid
  if (isNaN(date.getTime())) {
    return 'Invalid Date';
  }
  
  // Use proper timezone conversion to IST
  const istDate = new Date(date.toLocaleString('en-US', { timeZone: 'Asia/Kolkata' }));
  
  return istDate.toISOString().replace('T', ' ').replace('Z', ' IST');
}

/**
 * Format timestamp for display in IST
 * Returns format: YYYY-MM-DD HH:MM:SS IST
 */
export function formatISTDateTime(utcTimestamp: string | Date): string {
  // Handle invalid inputs that cause "undefined" in logs
  if (!utcTimestamp || utcTimestamp === 'undefined' || utcTimestamp === 'null') {
    return 'Invalid Date';
  }
  
  const date = typeof utcTimestamp === 'string' ? new Date(utcTimestamp) : utcTimestamp;
  
  // Check if date is valid
  if (isNaN(date.getTime())) {
    return 'Invalid Date';
  }
  
  // Use proper timezone conversion to IST
  const istString = date.toLocaleString('sv-SE', {
    timeZone: 'Asia/Kolkata',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false
  });
  
  return `${istString} IST`;
}

/**
 * Format relative time in IST context
 * Shows relative time but with IST timezone indicator
 */
export function formatRelativeIST(utcTimestamp: string | Date): string {
  const date = typeof utcTimestamp === 'string' ? new Date(utcTimestamp) : utcTimestamp;
  const now = new Date();
  const diff = now.getTime() - date.getTime();
  
  const seconds = Math.floor(diff / 1000);
  const minutes = Math.floor(seconds / 60);
  const hours = Math.floor(minutes / 60);
  const days = Math.floor(hours / 24);
  
  if (seconds < 60) return `${seconds}s ago (IST)`;
  if (minutes < 60) return `${minutes}m ago (IST)`;
  if (hours < 24) return `${hours}h ago (IST)`;
  return `${days}d ago (IST)`;
}

/**
 * Get current time in IST format
 */
export function getCurrentIST(): string {
  return formatISTDateTime(new Date());
}
