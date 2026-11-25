#!/usr/bin/env python3
"""
Export raw GPT responses to CSV with IST timings.

Usage:
    python scripts/export_gpt_responses.py --output gpt_responses.csv
"""

import argparse
import asyncio
import csv
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Add the project root to the Python path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from packages.db.repositories.gpt_responses import get_all_gpt_responses
import json


def convert_utc_to_ist(utc_datetime):
    """Convert UTC datetime to IST (UTC+5:30)."""
    if isinstance(utc_datetime, str):
        utc_datetime = datetime.fromisoformat(utc_datetime.replace('Z', '+00:00'))
    
    # Add 5 hours 30 minutes to convert to IST
    ist_datetime = utc_datetime + timedelta(hours=5, minutes=30)
    return ist_datetime.strftime('%Y-%m-%d %H:%M:%S IST')


def parse_gpt_response_content(response_content):
    """Parse GPT response content to extract trading parameters."""
    try:
        if isinstance(response_content, str):
            data = json.loads(response_content)
        else:
            data = response_content
        
        return {
            'level': data.get('level', ''),
            'sl': data.get('stop', data.get('stoploss', data.get('stop_loss', ''))),
            'tp': data.get('target', data.get('take_profit', '')),
            'pattern': data.get('pattern', data.get('condition', ''))
        }
    except (json.JSONDecodeError, TypeError):
        return {
            'level': '',
            'sl': '',
            'tp': '',
            'pattern': ''
        }


async def export_gpt_responses_to_csv(output_file: str, hours: int = 12):
    """Export GPT responses to CSV with IST timings."""
    
    # Calculate time range (past 12 hours)
    end_time = datetime.now(timezone.utc)
    start_time = end_time - timedelta(hours=hours)
    
    print(f"🕐 Fetching GPT responses from {start_time.strftime('%Y-%m-%d %H:%M:%S UTC')} to {end_time.strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"🕐 IST Time Range: {convert_utc_to_ist(start_time)} to {convert_utc_to_ist(end_time)}")
    
    try:
        # Get GPT responses from database
        responses = await get_all_gpt_responses(
            limit=1000,  # Get more responses
            skip=0,
            start_time=start_time,
            end_time=end_time
        )
        
        print(f"📊 Found {len(responses)} GPT responses")
        
        if not responses:
            print("❌ No GPT responses found in the specified time range")
            return
        
        # Prepare CSV data
        csv_data = []
        
        for response in responses:
            # Parse response content
            trading_params = parse_gpt_response_content(response.get('response_content', ''))
            
            # Convert timestamp to IST
            received_time = response.get('timestamp')
            if received_time:
                ist_time = convert_utc_to_ist(received_time)
            else:
                ist_time = ''
            
            csv_data.append({
                'Asset': response.get('asset', ''),
                'Level': trading_params['level'],
                'SL': trading_params['sl'],
                'TP': trading_params['tp'],
                'Pattern': trading_params['pattern'],
                'Received_At_IST': ist_time,
                'Direction': response.get('direction', ''),
                'Confidence': response.get('confidence', ''),
                'Agent_ID': response.get('agent_id', ''),
                'Response_ID': str(response.get('_id', ''))
            })
        
        # Sort by received time (newest first)
        csv_data.sort(key=lambda x: x['Received_At_IST'], reverse=True)
        
        # Write to CSV
        fieldnames = [
            'Asset', 'Level', 'SL', 'TP', 'Pattern', 'Received_At_IST',
            'Direction', 'Confidence', 'Agent_ID', 'Response_ID'
        ]
        
        with open(output_file, 'w', newline='', encoding='utf-8') as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(csv_data)
        
        print(f"✅ Exported {len(csv_data)} GPT responses to {output_file}")
        
        # Show summary by asset
        asset_counts = {}
        for row in csv_data:
            asset = row['Asset']
            asset_counts[asset] = asset_counts.get(asset, 0) + 1
        
        print("\n📈 Summary by Asset:")
        for asset, count in sorted(asset_counts.items()):
            print(f"   {asset}: {count} responses")
        
        return len(csv_data)
        
    except Exception as e:
        print(f"❌ Error exporting GPT responses: {e}")
        return 0


def parse_args():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Export raw GPT responses to CSV with IST timings",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    # Export last 12 hours
    python scripts/export_gpt_responses.py --output gpt_responses.csv
    
    # Export last 24 hours
    python scripts/export_gpt_responses.py --output gpt_responses.csv --hours 24
        """
    )
    
    parser.add_argument("--output", default="gpt_responses.csv",
                       help="Output CSV file path (default: gpt_responses.csv)")
    parser.add_argument("--hours", type=int, default=12,
                       help="Hours of data to export (default: 12)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable verbose logging")
    
    return parser.parse_args()


async def main():
    """Main function."""
    args = parse_args()
    
    # Configure logging
    if args.verbose:
        import logging
        logging.basicConfig(level=logging.INFO)
    
    print("🚀 Exporting GPT Responses to CSV")
    print(f"📁 Output file: {args.output}")
    print(f"⏰ Time range: Past {args.hours} hours")
    print()
    
    try:
        count = await export_gpt_responses_to_csv(args.output, args.hours)
        
        if count > 0:
            print(f"\n🎉 Successfully exported {count} GPT responses!")
            print(f"📄 File saved as: {args.output}")
        else:
            print("\n⚠️  No data exported")
        
        return 0
        
    except Exception as e:
        print(f"❌ Export failed: {e}")
        if args.verbose:
            import traceback
            traceback.print_exc()
        return 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
