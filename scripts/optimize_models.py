"""CLI entry point for running Bayesian hyperparameter optimization on trade models.

This script orchestrates the optimisation workflow across XGBoost, LightGBM, and
CatBoost models, performs SHAP-based feature selection, and executes advanced
validation suites (confidence threshold sweep, walk-forward, Monte Carlo, and
regime-specific diagnostics).

Usage
-----
    python scripts/optimize_models.py --dataset results/backtest_trades_20250912_014829.csv

The script intentionally fails fast with descriptive errors if any required
column is missing.  This adheres to the system-wide "no fallback" policy.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from packages.core.ml import (
    DatasetConfig,
    FeatureEngineeringConfig,
    HyperparameterSearchConfig,
    ModelTrainer,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Optimize ML models for trade signal classification")
    parser.add_argument("--dataset", required=True, help="Path to labeled dataset CSV")
    parser.add_argument(
        "--target",
        default="Trade_Status",
        help="Target column to model (default: Trade_Status)",
    )
    parser.add_argument(
        "--positive-labels",
        nargs="+",
        default=["WIN", "TP", "TP1"],
        help="Labels considered positive class for classification",
    )
    parser.add_argument(
        "--negative-labels",
        nargs="+",
        default=["LOSS", "SL", "FILTERED", "NO_ENTRY", "BE"],
        help="Labels considered negative class",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("results/model_optimization_summary.parquet"),
        help="Path to write optimisation summary parquet file",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    dataset = Path(args.dataset)
    if not dataset.exists():
        raise ValueError(f"Dataset path does not exist: {dataset}")

    dataset_config = DatasetConfig(
        data_path=dataset,
        target_column=args.target,
        parse_dates=["Signal_Time"],
        positive_labels=args.positive_labels,
        negative_labels=args.negative_labels,
    )

    search_configs = {
        name: HyperparameterSearchConfig(model_name=name, n_trials=40, cv_splits=4)
        for name in ("xgboost", "lightgbm", "catboost")
    }

    trainer = ModelTrainer(
        dataset_config=dataset_config,
        feature_config=FeatureEngineeringConfig(),
        search_configs=search_configs,
        target_feature_count=60,
        confidence_thresholds=(0.5, 0.55, 0.6, 0.65, 0.7),
        walk_forward_window=1000,
        monte_carlo_iterations=1000,
        timestamp_column="Signal_Time",
        regime_column="is_trending",
        profit_column="Profit",
        risk_reward_column="Risk_Reward_Ratio",
    )

    artifacts = trainer.train()

    summary_df = pd.DataFrame(
        {
            "best_model": [artifacts.best_model_name],
            "best_roc_auc": [artifacts.validation_scores[f"{artifacts.best_model_name}_roc_auc"]],
            "threshold": [artifacts.threshold_result.threshold],
            "threshold_precision": [artifacts.threshold_result.precision],
            "threshold_recall": [artifacts.threshold_result.recall],
            "walk_forward_windows": [len(artifacts.walk_forward.windows)],
            "monte_carlo_mean": [artifacts.monte_carlo.mean_return],
            "kelly_fraction": [artifacts.kelly_fraction],
        }
    )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    summary_df.to_parquet(args.output, index=False)

    shap_path = args.output.with_name("shap_feature_importance.parquet")
    artifacts.shap_importance.to_parquet(shap_path, index=False)

    print("Optimization complete")
    print(summary_df.iloc[0].to_string())
    print(f"Artifacts saved to {args.output} and {shap_path}")


if __name__ == "__main__":
    main()


