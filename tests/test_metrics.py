"""Every registered KPI must run on DuckDB and return data."""

from __future__ import annotations

import pytest

REQUIRED_FIELDS = ("key", "title", "description", "sql", "chart")
ALLOWED_CHART_TYPES = {"bar", "line", "funnel", "stat"}


def test_registry_has_twelve_metrics(registry):
    assert len(registry) == 12


def test_registry_entries_are_complete(registry):
    for key, metric in registry.items():
        for field in REQUIRED_FIELDS:
            assert field in metric, f"{key} is missing '{field}'"
        chart = metric["chart"]
        assert chart["type"] in ALLOWED_CHART_TYPES
        assert "x" in chart and "y" in chart


@pytest.mark.parametrize(
    "key",
    [
        "funnel_overview",
        "step_conversion_rates",
        "completion_rate_by_channel",
        "completion_rate_by_device",
        "downloads_by_channel",
        "pairing_rate_by_channel",
        "pairing_rate_by_platform_market",
        "d30_retention_by_channel",
        "weekly_test_starts_trend",
        "linkable_share_by_market",
        "attribution_first_vs_last",
        "completion_by_channel_device",
    ],
)
def test_metric_sql_executes_and_returns_rows(key, registry, driver):
    metric = registry[key]
    df = driver.query(metric["sql"])
    assert len(df) > 0, f"{key} returned no rows"
    assert not df.empty
    # chart columns must exist in the result
    chart = metric["chart"]
    assert chart["x"] in df.columns, f"{key}: chart x '{chart['x']}' not in result"
    assert chart["y"] in df.columns, f"{key}: chart y '{chart['y']}' not in result"
    if chart.get("series"):
        assert chart["series"] in df.columns


def test_rate_metrics_are_fractions(registry, driver):
    for key, metric in registry.items():
        df = driver.query(metric["sql"])
        for col in df.columns:
            if any(t in col.lower() for t in ("rate", "share")):
                assert df[col].dropna().between(0, 1).all(), (
                    f"{key}.{col} is not a fraction in [0, 1]"
                )
