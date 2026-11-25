"""Export service for GPT responses and trading reports."""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Union
from pathlib import Path
import asyncio

from packages.core.logger import get_logger
from packages.db.repositories import gpt_responses as gpt_repo
from packages.db.repositories import comprehensive_logging as comp_logging

logger = get_logger(__name__)


class ExportService:
    """Service for exporting GPT responses and trading reports."""
    
    def __init__(self, export_dir: Optional[str] = None):
        self._logger = get_logger(__name__)
        self._export_dir = Path(export_dir) if export_dir else Path("exports")
        self._export_dir.mkdir(exist_ok=True)
    
    async def export_gpt_responses(
        self,
        asset: Optional[str] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        limit: int = 1000,
        format: str = "json",
        filename: Optional[str] = None,
    ) -> str:
        """Export GPT responses to file."""
        
        try:
            # Get GPT responses
            if asset and asset != "ALL":
                responses = await gpt_repo.get_gpt_responses_by_asset(
                    asset=asset,
                    start_time=start_time,
                    end_time=end_time,
                    limit=limit,
                )
            else:
                # Get all responses - use a general query
                responses = await gpt_repo.get_all_gpt_responses(
                    start_time=start_time,
                    end_time=end_time,
                    limit=limit,
                )
            
            if not responses:
                self._logger.warning("No GPT responses found for export")
                return ""
            
            # Generate filename if not provided
            if not filename:
                timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
                asset_suffix = f"_{asset}" if asset and asset != "ALL" else "_all"
                filename = f"gpt_responses{asset_suffix}_{timestamp}.{format}"
            
            filepath = self._export_dir / filename
            
            if format.lower() == "json":
                await self._export_to_json(responses, filepath)
            elif format.lower() == "csv":
                await self._export_to_csv(responses, filepath, "gpt_responses")
            else:
                raise ValueError(f"Unsupported format: {format}")
            
            self._logger.info(
                f"Exported GPT responses: asset={asset}, count={len(responses)}, filepath={filepath}, format={format}"
            )
            
            return str(filepath)
            
        except Exception as e:
            self._logger.error(f"Failed to export GPT responses: {str(e)}")
            raise
    
    async def export_comprehensive_trades(
        self,
        asset: Optional[str] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        limit: int = 1000,
        format: str = "json",
        filename: Optional[str] = None,
    ) -> str:
        """Export comprehensive trades to file."""
        
        try:
            # Get comprehensive trades
            if asset and asset != "ALL":
                trades = await comp_logging.get_comprehensive_trades(
                    asset=asset,
                    start_time=start_time,
                    end_time=end_time,
                    limit=limit,
                )
            else:
                # Get all trades - use a general query
                trades = await comp_logging.get_all_comprehensive_trades(
                    start_time=start_time,
                    end_time=end_time,
                    limit=limit,
                )
            
            if not trades:
                self._logger.warning("No comprehensive trades found for export")
                return ""
            
            # Generate filename if not provided
            if not filename:
                timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
                asset_suffix = f"_{asset}" if asset and asset != "ALL" else "_all"
                filename = f"comprehensive_trades{asset_suffix}_{timestamp}.{format}"
            
            filepath = self._export_dir / filename
            
            if format.lower() == "json":
                await self._export_to_json(trades, filepath)
            elif format.lower() == "csv":
                await self._export_to_csv(trades, filepath, "comprehensive_trades")
            else:
                raise ValueError(f"Unsupported format: {format}")
            
            self._logger.info(
                f"Exported comprehensive trades: asset={asset}, count={len(trades)}, filepath={filepath}, format={format}"
            )
            
            return str(filepath)
            
        except Exception as e:
            self._logger.error(f"Failed to export comprehensive trades: {str(e)}")
            raise
    
    async def export_slot_candle_matches(
        self,
        asset: Optional[str] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        limit: int = 1000,
        format: str = "json",
        filename: Optional[str] = None,
    ) -> str:
        """Export slot-candle matches to file."""
        
        try:
            # Get slot-candle matches
            if asset and asset != "ALL":
                matches = await comp_logging.get_slot_candle_matches(
                    asset=asset,
                    start_time=start_time,
                    end_time=end_time,
                    limit=limit,
                )
            else:
                # Get all matches - use a general query
                matches = await comp_logging.get_all_slot_candle_matches(
                    start_time=start_time,
                    end_time=end_time,
                    limit=limit,
                )
            
            if not matches:
                self._logger.warning("No slot-candle matches found for export")
                return ""
            
            # Generate filename if not provided
            if not filename:
                timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
                asset_suffix = f"_{asset}" if asset and asset != "ALL" else "_all"
                filename = f"slot_candle_matches{asset_suffix}_{timestamp}.{format}"
            
            filepath = self._export_dir / filename
            
            if format.lower() == "json":
                await self._export_to_json(matches, filepath)
            elif format.lower() == "csv":
                await self._export_to_csv(matches, filepath, "slot_candle_matches")
            else:
                raise ValueError(f"Unsupported format: {format}")
            
            self._logger.info(
                f"Exported slot-candle matches: asset={asset}, count={len(matches)}, filepath={filepath}, format={format}"
            )
            
            return str(filepath)
            
        except Exception as e:
            self._logger.error(f"Failed to export slot-candle matches: {str(e)}")
            raise
    
    async def export_hourly_candles(
        self,
        asset: Optional[str] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        limit: int = 1000,
        format: str = "json",
        filename: Optional[str] = None,
    ) -> str:
        """Export hourly candles to file."""
        
        try:
            # Get hourly candles
            if asset:
                candles = await comp_logging.get_hourly_candles(
                    asset=asset,
                    start_time=start_time,
                    end_time=end_time,
                    limit=limit,
                )
            else:
                candles = []
            
            if not candles:
                self._logger.warning("No hourly candles found for export")
                return ""
            
            # Generate filename if not provided
            if not filename:
                timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
                asset_suffix = f"_{asset}" if asset and asset != "ALL" else "_all"
                filename = f"hourly_candles{asset_suffix}_{timestamp}.{format}"
            
            filepath = self._export_dir / filename
            
            if format.lower() == "json":
                await self._export_to_json(candles, filepath)
            elif format.lower() == "csv":
                await self._export_to_csv(candles, filepath, "hourly_candles")
            else:
                raise ValueError(f"Unsupported format: {format}")
            
            self._logger.info(
                f"Exported hourly candles: asset={asset}, count={len(candles)}, filepath={filepath}, format={format}"
            )
            
            return str(filepath)
            
        except Exception as e:
            self._logger.error(f"Failed to export hourly candles: {str(e)}")
            raise
    
    async def export_gpt_sniper_levels(
        self,
        asset: Optional[str] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        limit: int = 1000,
        format: str = "json",
        filename: Optional[str] = None,
    ) -> str:
        """Export GPT sniper asset levels to file."""
        
        try:
            # Get GPT responses with level information
            if asset and asset != "ALL":
                responses = await gpt_repo.get_gpt_responses_by_asset(
                    asset=asset,
                    start_time=start_time,
                    end_time=end_time,
                    limit=limit,
                )
            else:
                responses = await gpt_repo.get_all_gpt_responses(
                    start_time=start_time,
                    end_time=end_time,
                    limit=limit,
                )
            
            if not responses:
                self._logger.warning("No GPT responses found for sniper levels export")
                return ""
            
            # Filter and format sniper levels data
            sniper_levels = []
            for response in responses:
                if response.get('level') and response.get('direction'):
                    sniper_level = {
                        'timestamp': response.get('timestamp'),
                        'asset': response.get('asset'),
                        'direction': response.get('direction'),
                        'level': response.get('level'),
                        'pattern': response.get('pattern'),
                        'confidence': response.get('confidence'),
                        'stop_loss': response.get('stop_loss'),
                        'take_profit': response.get('take_profit'),
                        'quantity': response.get('quantity'),
                        'agent_id': response.get('agent_id'),
                        'gpt_mode': response.get('gpt_mode'),
                        'broker_type': response.get('broker_type'),
                    }
                    sniper_levels.append(sniper_level)
            
            if not sniper_levels:
                self._logger.warning("No sniper levels found in GPT responses")
                return ""
            
            # Generate filename if not provided
            if not filename:
                timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
                asset_suffix = f"_{asset}" if asset and asset != "ALL" else "_all"
                filename = f"gpt_sniper_levels{asset_suffix}_{timestamp}.{format}"
            
            filepath = self._export_dir / filename
            
            if format.lower() == "json":
                await self._export_to_json(sniper_levels, filepath)
            elif format.lower() == "csv":
                await self._export_to_csv(sniper_levels, filepath, "gpt_sniper_levels")
            else:
                raise ValueError(f"Unsupported format: {format}")
            
            self._logger.info(
                f"Exported GPT sniper levels: asset={asset}, count={len(sniper_levels)}, filepath={filepath}, format={format}"
            )
            
            return str(filepath)
            
        except Exception as e:
            self._logger.error(f"Failed to export GPT sniper levels: {str(e)}")
            raise

    async def export_trading_report(
        self,
        asset: Optional[str] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        format: str = "json",
        filename: Optional[str] = None,
    ) -> str:
        """Export a comprehensive trading report combining all data."""
        
        try:
            # Get all data types
            gpt_responses = []
            trades = []
            matches = []
            candles = []
            
            if asset and asset != "ALL":
                gpt_responses = await gpt_repo.get_gpt_responses_by_asset(
                    asset=asset,
                    start_time=start_time,
                    end_time=end_time,
                    limit=1000,
                )
                trades = await comp_logging.get_comprehensive_trades(
                    asset=asset,
                    start_time=start_time,
                    end_time=end_time,
                    limit=1000,
                )
                matches = await comp_logging.get_slot_candle_matches(
                    asset=asset,
                    start_time=start_time,
                    end_time=end_time,
                    limit=1000,
                )
                candles = await comp_logging.get_hourly_candles(
                    asset=asset,
                    start_time=start_time,
                    end_time=end_time,
                    limit=1000,
                )
            else:
                # Get all data for "ALL" or no asset specified
                gpt_responses = await gpt_repo.get_all_gpt_responses(
                    start_time=start_time,
                    end_time=end_time,
                    limit=1000,
                )
                trades = await comp_logging.get_all_comprehensive_trades(
                    start_time=start_time,
                    end_time=end_time,
                    limit=1000,
                )
                matches = await comp_logging.get_all_slot_candle_matches(
                    start_time=start_time,
                    end_time=end_time,
                    limit=1000,
                )
                candles = await comp_logging.get_all_hourly_candles(
                    start_time=start_time,
                    end_time=end_time,
                    limit=1000,
                )
            
            # Create comprehensive report
            report = {
                "export_info": {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "asset": asset,
                    "start_time": start_time.isoformat() if start_time else None,
                    "end_time": end_time.isoformat() if end_time else None,
                    "format": format,
                },
                "summary": {
                    "gpt_responses_count": len(gpt_responses),
                    "trades_count": len(trades),
                    "slot_candle_matches_count": len(matches),
                    "hourly_candles_count": len(candles),
                },
                "data": {
                    "gpt_responses": gpt_responses,
                    "comprehensive_trades": trades,
                    "slot_candle_matches": matches,
                    "hourly_candles": candles,
                },
            }
            
            # Generate filename if not provided
            if not filename:
                timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
                asset_suffix = f"_{asset}" if asset and asset != "ALL" else "_all"
                filename = f"trading_report{asset_suffix}_{timestamp}.{format}"
            
            filepath = self._export_dir / filename
            
            if format.lower() == "json":
                await self._export_to_json(report, filepath)
            elif format.lower() == "csv":
                # For CSV, export each data type separately
                base_name = filepath.stem
                csv_files = []
                
                if gpt_responses:
                    csv_file = self._export_dir / f"{base_name}_gpt_responses.csv"
                    await self._export_to_csv(gpt_responses, csv_file, "gpt_responses")
                    csv_files.append(str(csv_file))
                
                if trades:
                    csv_file = self._export_dir / f"{base_name}_trades.csv"
                    await self._export_to_csv(trades, csv_file, "comprehensive_trades")
                    csv_files.append(str(csv_file))
                
                if matches:
                    csv_file = self._export_dir / f"{base_name}_matches.csv"
                    await self._export_to_csv(matches, csv_file, "slot_candle_matches")
                    csv_files.append(str(csv_file))
                
                if candles:
                    csv_file = self._export_dir / f"{base_name}_candles.csv"
                    await self._export_to_csv(candles, csv_file, "hourly_candles")
                    csv_files.append(str(csv_file))
                
                # Create a summary file
                summary_file = self._export_dir / f"{base_name}_summary.json"
                await self._export_to_json(report["summary"], summary_file)
                csv_files.append(str(summary_file))
                
                return "; ".join(csv_files)
            else:
                raise ValueError(f"Unsupported format: {format}")
            
            self._logger.info(
                f"Exported trading report: asset={asset}, filepath={filepath}, format={format}, summary={report['summary']}"
            )
            
            return str(filepath)
            
        except Exception as e:
            self._logger.error(f"Failed to export trading report: {str(e)}")
            raise
    
    async def _export_to_json(self, data: List[Dict[str, Any]], filepath: Path) -> None:
        """Export data to JSON file."""
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False, default=str)
    
    async def _export_to_csv(self, data: List[Dict[str, Any]], filepath: Path, data_type: str) -> None:
        """Export data to CSV file."""
        if not data:
            return
        
        # Get all unique keys from all records
        all_keys = set()
        for record in data:
            all_keys.update(record.keys())
        
        # Sort keys for consistent column order
        fieldnames = sorted(all_keys)
        
        with open(filepath, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            
            for record in data:
                # Convert any non-serializable values to strings
                csv_record = {}
                for key, value in record.items():
                    if isinstance(value, (dict, list)):
                        csv_record[key] = json.dumps(value, default=str)
                    elif value is None:
                        csv_record[key] = ""
                    else:
                        csv_record[key] = str(value)
                writer.writerow(csv_record)
    
    def get_export_dir(self) -> Path:
        """Get the export directory path."""
        return self._export_dir
    
    def list_exports(self) -> List[Dict[str, Any]]:
        """List all exported files."""
        exports = []
        
        for file_path in self._export_dir.glob("*"):
            if file_path.is_file():
                stat = file_path.stat()
                exports.append({
                    "filename": file_path.name,
                    "filepath": str(file_path),
                    "size_bytes": stat.st_size,
                    "created_at": datetime.fromtimestamp(stat.st_ctime, tz=timezone.utc).isoformat(),
                    "modified_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
                })
        
        # Sort by creation time (newest first)
        exports.sort(key=lambda x: x["created_at"], reverse=True)
        
        return exports


# Global instance
_export_service = ExportService()


def get_export_service() -> ExportService:
    """Get the global export service instance."""
    return _export_service


__all__ = [
    "ExportService",
    "get_export_service",
]
