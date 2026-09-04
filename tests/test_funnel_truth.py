"""Validate funnel_overview against the generator's ground truth.

The ground truth parquet is test-only material: agent code never reads
it (its table is not even registered in the driver). Here we recompute
the truth aggregates directly from the parquet and require the agent's
event-derived funnel to land within 2% at every stage.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

GROUND_TRUTH = Path(__file__).resolve().parents[1] / "data" / "_ground_truth.parquet"

TOLERANCE = 0.02


@pytest.fixture(scope="module")
def truth_counts():
    con = duckdb.connect()
    row = con.execute(
        f"""
        WITH horizon AS (
          SELECT MAX(event_timestamp) AS max_ts
          FROM '{GROUND_TRUTH.parent.as_posix()}/app_events.parquet'
        )
        SELECT
          COUNT(*)                                          AS started,
          SUM(CASE WHEN completed  THEN 1 ELSE 0 END)       AS completed,
          SUM(CASE WHEN downloaded THEN 1 ELSE 0 END)       AS downloaded,
          SUM(CASE WHEN paired     THEN 1 ELSE 0 END)       AS paired,
          SUM(CASE WHEN d30        THEN 1 ELSE 0 END)       AS d30
        FROM '{GROUND_TRUTH.as_posix()}'
        """
    ).fetchone()
    con.close()
    return {
        "hearing_test_start": row[0],
        "hearing_test_complete": row[1],
        "app_download": row[2],
        "hearing_aid_paired": row[3],
        "active_d30": row[4],
    }


def test_funnel_overview_matches_ground_truth(driver, registry, truth_counts):
    df = driver.query(registry["funnel_overview"]["sql"])
    observed = dict(zip(df["stage"], df["users"]))

    assert set(observed) == set(truth_counts)
    for stage, truth in truth_counts.items():
        got = observed[stage]
        rel_err = abs(got - truth) / truth
        assert rel_err <= TOLERANCE, (
            f"Stage '{stage}': agent={got}, truth={truth}, "
            f"relative error {rel_err:.4f} > {TOLERANCE:.0%}"
        )


def test_funnel_is_monotonically_decreasing(driver, registry):
    df = driver.query(registry["funnel_overview"]["sql"]).sort_values("stage_order")
    users = df["users"].tolist()
    assert all(a >= b for a, b in zip(users, users[1:])), (
        f"Funnel stages should not grow: {users}"
    )
