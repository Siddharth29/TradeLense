# Candlestick Chart Analyzer Guide

> **Status:** living  
> Candle validation rules, what each failure means, and how to fix it.


## Overview

The **Candlestick Chart Analyzer** is a powerful tool for validating and visualizing 5-minute candle data with automatic UTC to IST (Indian Standard Time) conversion. This feature provides real-time validation, comprehensive statistics, and professional-grade candlestick charts.

## Features

### ✨ Key Capabilities

1. **Real-time Candle Validation**
   - Validates JSON format
   - Checks candle data structure
   - Detects sequence gaps in 5-minute intervals
   - Provides detailed error messages

2. **UTC to IST Conversion**
   - Automatic timezone conversion (UTC → IST)
   - Display times in Indian Standard Time
   - Maintains UTC timestamps for accuracy

3. **Comprehensive Statistics**
   - Total candle count
   - First candle time (IST)
   - Last candle time (IST)
   - Total time range

4. **Professional Candlestick Charts**
   - Powered by TradingView's `lightweight-charts`
   - Interactive zoom and pan
   - Detailed tooltips with OHLC data
   - Color-coded bullish (green) and bearish (red) candles

5. **Sample Data Loading**
   - Quick load perfect sequence
   - Load data with gaps for testing
   - Easy testing without manual data entry

## How to Access

1. **Start the development server:**
   ```bash
   cd apps/frontend
   npm run dev
   ```

2. **Navigate to the page:**
   - Click on **"Candle Validator"** in the sidebar navigation (candlestick icon)
   - Or go directly to: `http://localhost:5173/candle-validator`

## How to Use

### Method 1: Load Sample Data (Quick Test)

1. Click the **"Load Sample"** button to load perfect 5-minute candle data
2. Click the **"With Gaps"** button to test gap detection
3. The chart will render automatically

### Method 2: Paste Your Own Data

1. **Prepare your candle data** in the following JSON format:
   ```json
   [
     ["2024-10-09T03:45:00Z", 24800, 24850, 24790, 24830, 12500],
     ["2024-10-09T03:50:00Z", 24830, 24880, 24820, 24860, 13200],
     ["2024-10-09T03:55:00Z", 24860, 24900, 24850, 24890, 14100]
   ]
   ```
   
   **Format:** `[UTC Timestamp, Open, High, Low, Close, Volume]`

2. **Paste the data** into the text area

3. **Click "🚀 Render Chart"**

4. **View results:**
   - ✅ Success: Perfect 5-minute intervals
   - ⚠️ Warning: Data loaded but has gaps
   - ❌ Error: Invalid format or data

## Data Format Specification

### Required Format
Each candle must be an array with 5 or 6 elements:
```javascript
[timestamp, open, high, low, close, volume?]
```

### Field Descriptions

| Index | Field | Type | Required | Description |
|-------|-------|------|----------|-------------|
| 0 | Timestamp | ISO 8601 String | Yes | UTC timestamp (e.g., "2024-10-09T03:45:00Z") |
| 1 | Open | Number | Yes | Opening price |
| 2 | High | Number | Yes | Highest price |
| 3 | Low | Number | Yes | Lowest price |
| 4 | Close | Number | Yes | Closing price |
| 5 | Volume | Number | Optional | Trading volume |

### Valid Examples

**Perfect 5-minute sequence:**
```json
[
  ["2024-10-09T03:45:00Z", 24800, 24850, 24790, 24830, 12500],
  ["2024-10-09T03:50:00Z", 24830, 24880, 24820, 24860, 13200],
  ["2024-10-09T03:55:00Z", 24860, 24900, 24850, 24890, 14100]
]
```

**Without volume:**
```json
[
  ["2024-10-09T03:45:00Z", 24800, 24850, 24790, 24830],
  ["2024-10-09T03:50:00Z", 24830, 24880, 24820, 24860]
]
```

### Invalid Examples

**❌ Missing required fields:**
```json
[
  ["2024-10-09T03:45:00Z", 24800, 24850, 24790]  // Missing close
]
```

**❌ Invalid timestamp:**
```json
[
  ["invalid-date", 24800, 24850, 24790, 24830]
]
```

**❌ Non-numeric values:**
```json
[
  ["2024-10-09T03:45:00Z", "24800", 24850, 24790, 24830]  // String instead of number
]
```

## Validation Rules

### 1. Format Validation
- Data must be valid JSON
- Must be a non-empty array
- Each candle must have 5-6 elements

### 2. Sequence Validation (5-minute intervals)
The validator checks that consecutive candles are exactly **5 minutes apart** in IST:

- ✅ **Valid:** Each candle is 5 minutes after the previous one
- ⚠️ **Warning:** Gaps detected in the sequence

**Example of valid sequence:**
- Candle 1: 09:15 IST
- Candle 2: 09:20 IST (5 min later) ✅
- Candle 3: 09:25 IST (5 min later) ✅

**Example with gaps:**
- Candle 1: 09:15 IST
- Candle 2: 09:25 IST (10 min later) ⚠️ Gap detected!
- Candle 3: 09:30 IST (5 min later) ✅

### 3. Data Type Validation
- Timestamp: Must be valid ISO 8601 string
- OHLC values: Must be valid numbers
- Volume: Must be a number (if provided)

## Chart Interactions

### Mouse Controls
- **Zoom:** Mouse wheel or pinch gesture
- **Pan:** Click and drag
- **Crosshair:** Hover over chart

### Time Scale
- Times are displayed in **IST (Indian Standard Time)**
- Format: HH:MM (24-hour format)
- Automatic time interval detection

### Price Scale
- Auto-scales based on price range
- Right-side price axis
- Grid lines for easy reading

### Tooltips
Hover over any candle to see:
- Exact timestamp (IST)
- Open price
- High price
- Low price
- Close price

## Statistics Panel

The stats panel shows four key metrics:

1. **Total Candles:** Count of all candles in the dataset
2. **First Candle (IST):** Start time of the data range
3. **Last Candle (IST):** End time of the data range
4. **Time Range:** Total duration covered (hours and minutes)

## Technical Details

### Libraries Used
- **Chart Library:** `lightweight-charts` v4.2.3 (TradingView)
- **UI Components:** Radix UI with Tailwind CSS
- **Icons:** Lucide React

### Component Location
- **Page:** `apps/frontend/src/pages/CandleValidator.tsx`
- **Route:** `/candle-validator`
- **Sample Data:** `apps/frontend/src/data/sampleCandleData.ts`

### Timezone Handling
- Input: UTC timestamps
- Internal: UTC timestamps (for accuracy)
- Display: IST (UTC + 5:30)
- Chart: IST with timezone adapter

## Troubleshooting

### Chart Not Rendering
**Solution:** Check browser console for errors, ensure data format is correct

### "Invalid JSON format" Error
**Solution:** Validate your JSON using a JSON validator or use the sample data

### "Sequence has gaps" Warning
**Solution:** This is informational - your data has missing 5-minute intervals. Verify if this is expected.

### Times Don't Match Expected IST
**Solution:** Ensure input timestamps are in UTC format (ending with 'Z')

## API Integration Example

If you want to fetch candle data from your backend:

```typescript
import { useState } from 'react'
import axios from 'axios'

const fetchCandleData = async () => {
  try {
    const response = await axios.get('/api/candles', {
      params: {
        symbol: 'NIFTY',
        interval: '5m',
        from: '2024-10-09T00:00:00Z',
        to: '2024-10-09T10:00:00Z'
      }
    })
    
    // Format: [[timestamp, open, high, low, close, volume], ...]
    return JSON.stringify(response.data, null, 2)
  } catch (error) {
    console.error('Error fetching candle data:', error)
    return null
  }
}
```

## Best Practices

1. **Always use UTC timestamps** in input data
2. **Validate data** before pasting large datasets
3. **Check sequence warnings** - they indicate data quality issues
4. **Use sample data** for testing features
5. **Monitor the statistics panel** for data overview

## Future Enhancements

Potential features for future versions:
- [ ] Export chart as image
- [ ] Multiple timeframe support (1m, 15m, 1h, etc.)
- [ ] Technical indicators overlay
- [ ] Compare multiple datasets
- [ ] Custom color schemes
- [ ] Data export functionality
- [ ] Real-time WebSocket integration

## Support

For issues or questions:
1. Check this documentation
2. Review sample data in `apps/frontend/src/data/sampleCandleData.ts`
3. Check browser console for detailed error messages
4. Verify data format matches specification

---

**Last Updated:** October 2024  
**Version:** 1.0.0










