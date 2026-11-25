import pandas as pd
from .config import DATA_DIR
from pathlib import Path
from .logger import get_logger
from .session import ensure_ist

logger = get_logger(__name__)


def load_csv(name: str) -> pd.DataFrame:
    """Load a CSV from data/processed, normalising the index column."""
    path = (DATA_DIR / name).resolve()
    logger.info(f"Loading CSV: {path}")
    df = pd.read_csv(path)
    logger.info(f"Loaded {len(df)} rows from {name}")
    idx_col = 'time' if 'time' in df.columns else 'date'
    df[idx_col] = pd.to_datetime(df[idx_col]).map(ensure_ist)
    df = df.rename(columns={idx_col: 'time'})
    return df.set_index('time').sort_index()
