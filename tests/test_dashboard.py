"""Module M11: agent.dashboard — filters model, text parsing, safe WHERE
composition, distinct-value validation, and the KPI registry runner.
"""

from __future__ import annotations

from datetime import date

import pytest

from agent.dashboard import (
    DashboardFilters,
    FilterValidationError,
    UnparseableRangeError,
    build_filter_label,
    build_where,
    extract_filters_from_text,
    get_data_horizon,
    is_dashboard_intent,
    load_kpi_templates,
    parse_relative_range,
    resolve_relative_range,
    run_dashboard,
    validate_filters,
)

pytestmark = pytest.mark.usefixtures("driver")


# ---------------------------------------------------------------------------
# Dashboard-intent detection
# ---------------------------------------------------------------------------
class TestIsDashboardIntent:
    @pytest.mark.parametrize(
        "text",
        [
            "Build me a KPI dashboard for the last 3 months for Germany",
            "build a kpi dashboard",
            "Can I get a dashboard of everything?",
            "kpi board please",
            "kokpit kur",
        ],
    )
    def test_positive(self, text):
        assert is_dashboard_intent(text) is True

    @pytest.mark.parametrize(
        "text",
        [
            "Where is the biggest drop-off?",
            "What is the completion rate for DE?",
            "Give me the funnel overview",
        ],
    )
    def test_negative(self, text):
        assert is_dashboard_intent(text) is False


# ---------------------------------------------------------------------------
# Relative-range parsing — anchored to the data horizon, never wall-clock.
# ---------------------------------------------------------------------------
class TestParseRelativeRange:
    def test_last_n_months_anchored_to_horizon(self):
        horizon = date(2026, 8, 30)
        start, end, label = parse_relative_range("last 3 months", horizon)
        assert end == horizon
        assert start == date(2026, 5, 30)
        assert label == "Last 3 months"

    def test_last_one_month_is_singular_in_label(self):
        horizon = date(2026, 8, 30)
        _, _, label = parse_relative_range("last 1 month", horizon)
        assert label == "Last 1 month"

    def test_last_n_weeks(self):
        horizon = date(2026, 8, 30)
        start, end, label = parse_relative_range("last 6 weeks", horizon)
        assert end == horizon
        assert (end - start).days == 42
        assert label == "Last 6 weeks"

    def test_no_match_returns_none(self):
        assert parse_relative_range("show me everything", date(2026, 8, 30)) is None

    def test_month_end_clamping(self):
        # Mar 31 minus 1 month must clamp into February, not overflow.
        start, _, _ = parse_relative_range("last 1 month", date(2026, 3, 31))
        assert start == date(2026, 2, 28)

    # -- M11-fix: days ------------------------------------------------------
    def test_last_n_days_anchored_to_horizon(self):
        horizon = date(2026, 8, 30)
        start, end, label = parse_relative_range("last 3 days", horizon)
        assert end == horizon
        # Documented boundary choice: horizon - N days, both ends inclusive
        # (same "-N units, inclusive both ends" convention as weeks/months
        # above) -- so "last 3 days" is a 4-day inclusive window.
        assert start == date(2026, 8, 27)
        assert (end - start).days == 3
        assert label == "Last 3 days"

    def test_last_one_day_is_singular_in_label(self):
        _, _, label = parse_relative_range("last 1 day", date(2026, 8, 30))
        assert label == "Last 1 day"

    def test_past_n_days_also_matches(self):
        start, end, label = parse_relative_range("past 5 days", date(2026, 8, 30))
        assert label == "Last 5 days"
        assert (end - start).days == 5

    def test_turkish_gun_matches(self):
        start, end, label = parse_relative_range("son 3 gün", date(2026, 8, 30))
        assert label == "Last 3 days"
        assert (end - start).days == 3

    def test_days_takes_priority_over_being_misread_as_weeks(self):
        # "days" and "weeks" are disjoint units -- a days phrase must never
        # fall through to the week branch (which would silently produce a
        # much wider window, exactly the M11-fix bug).
        start, end, _ = parse_relative_range("last 3 days", date(2026, 8, 30))
        assert (end - start).days == 3  # not 21 (3 weeks)

    def test_unsupported_unit_still_returns_none(self):
        # "hours" is deliberately NOT a supported unit (see
        # UnparseableRangeError) -- parse_relative_range stays a plain
        # "did this match" function; resolve_relative_range below is where
        # the M11-fix honesty rule's structured error lives.
        assert parse_relative_range("last 3 hours", date(2026, 8, 30)) is None


class TestResolveRelativeRange:
    """M11-fix 'honesty rule': an explicit relative_range argument that
    cannot be parsed must raise a structured, retryable error (naming the
    supported units) instead of silently doing nothing -- the root cause
    of the real "last 3 days" -> "last 6 weeks" bug was exactly this kind
    of silent substitution one layer up, in the LLM's own behavior once a
    ToolError came back; this is the code-level half of the fix."""

    def test_supported_phrase_resolves_like_parse_relative_range(self):
        horizon = date(2026, 8, 30)
        assert resolve_relative_range("last 3 days", horizon) == parse_relative_range(
            "last 3 days", horizon
        )

    def test_unsupported_unit_raises_structured_error(self):
        with pytest.raises(UnparseableRangeError) as exc_info:
            resolve_relative_range("last 3 hours", date(2026, 8, 30))
        assert exc_info.value.phrase == "last 3 hours"
        assert exc_info.value.supported_units == ("days", "weeks", "months")
        message = str(exc_info.value)
        assert "days" in message and "weeks" in message and "months" in message

    def test_nonsense_text_raises_structured_error(self):
        with pytest.raises(UnparseableRangeError):
            resolve_relative_range("show me everything", date(2026, 8, 30))


class TestGetDataHorizon:
    def test_matches_health_style_query(self, driver):
        horizon = get_data_horizon(driver)
        expected = driver.query(
            "SELECT MAX(ts) AS max_ts FROM ("
            "SELECT event_timestamp AS ts FROM web_events "
            "UNION ALL SELECT event_timestamp AS ts FROM app_events"
            ") combined"
        ).iloc[0]["max_ts"]
        assert horizon == expected.date()

    def test_is_never_wall_clock_today(self, driver):
        import datetime as _dt

        horizon = get_data_horizon(driver)
        # This is a static, dated synthetic dataset — its horizon must not
        # equal whatever day the test happens to run on.
        assert horizon != _dt.date.today()


# ---------------------------------------------------------------------------
# Free-text filter extraction (the deterministic keyword/offline path).
# ---------------------------------------------------------------------------
class TestExtractFiltersFromText:
    def test_flagship_example_phrase(self):
        out = extract_filters_from_text("Build me a KPI dashboard for the last 3 months for Germany")
        assert out["relative_range_text"] == "last 3 months"
        assert out["market"] == "DE"

    def test_two_letter_code_uppercase_only(self):
        assert extract_filters_from_text("dashboard for UK") == {"market": "UK"}
        # Lowercase "uk"/"us" are common English substrings/words and are
        # deliberately NOT matched as market codes (see the module's
        # _MARKET_CODE_RE comment) -- only the country-name aliases are
        # case-insensitive.
        assert "market" not in extract_filters_from_text("give us the dashboard")

    def test_channel_and_device_and_platform_words(self):
        out = extract_filters_from_text("dashboard for tiktok on mobile, iOS only")
        assert out["channel"] == "paid_social_tiktok"
        assert out["device"] == "mobile"
        assert out["platform"] == "iOS"

    def test_no_filters_found_is_empty_dict(self):
        assert extract_filters_from_text("dashboard please") == {}


# ---------------------------------------------------------------------------
# Distinct-value validation.
# ---------------------------------------------------------------------------
class TestValidateFilters:
    def test_all_none_never_raises(self, driver):
        validate_filters(DashboardFilters(), driver)  # must not raise

    def test_known_market_passes(self, driver):
        validate_filters(DashboardFilters(market="DE"), driver)  # must not raise

    def test_unknown_market_raises_with_field(self, driver):
        with pytest.raises(FilterValidationError) as exc_info:
            validate_filters(DashboardFilters(market="ZZ"), driver)
        assert exc_info.value.field == "market"
        assert "ZZ" in str(exc_info.value)

    def test_unknown_channel_raises(self, driver):
        with pytest.raises(FilterValidationError) as exc_info:
            validate_filters(DashboardFilters(channel="not_a_channel"), driver)
        assert exc_info.value.field == "channel"

    def test_unknown_device_raises(self, driver):
        with pytest.raises(FilterValidationError):
            validate_filters(DashboardFilters(device="phone"), driver)

    def test_unknown_platform_raises(self, driver):
        with pytest.raises(FilterValidationError):
            validate_filters(DashboardFilters(platform="WinPhone"), driver)

    def test_date_end_before_date_start_raises(self, driver):
        with pytest.raises(FilterValidationError) as exc_info:
            validate_filters(
                DashboardFilters(date_start=date(2026, 6, 1), date_end=date(2026, 1, 1)), driver
            )
        assert exc_info.value.field == "date_end"


# ---------------------------------------------------------------------------
# Safe WHERE composition — the "no raw user text spliced into SQL" test.
# ---------------------------------------------------------------------------
class TestBuildWhereSafety:
    WEB_COLUMNS = {
        "date_start": "day_date",
        "date_end": "day_date",
        "market": "market",
        "channel": "channel",
        "device": "device_category",
    }

    def test_plain_filters_produce_expected_predicates(self):
        where = build_where(
            DashboardFilters(date_start=date(2026, 1, 1), market="DE", device="mobile"),
            self.WEB_COLUMNS,
        )
        assert "day_date >= DATE '2026-01-01'" in where
        assert "market = 'DE'" in where
        assert "device_category = 'mobile'" in where

    def test_missing_filter_column_is_silently_skipped(self):
        # `platform` has no entry in WEB_COLUMNS -- applying it must not
        # raise or reference a nonexistent column.
        where = build_where(DashboardFilters(platform="iOS"), self.WEB_COLUMNS)
        assert "platform" not in where
        assert where == "1=1"

    @pytest.mark.parametrize(
        "injection",
        [
            "DE' OR '1'='1",
            "DE'; DROP TABLE gold.web_funnel_daily_cube; --",
            "DE' UNION SELECT * FROM bronze.id_bridge --",
        ],
    )
    def test_injection_attempts_are_rejected(self, injection):
        with pytest.raises(FilterValidationError) as exc_info:
            build_where(DashboardFilters(market=injection), self.WEB_COLUMNS)
        assert exc_info.value.field == "market"

    def test_single_quote_in_an_otherwise_valid_value_is_still_rejected(self):
        # No legitimate value in this dataset contains a quote, but the
        # safety net must hold even if one ever did -- it should be
        # rejected here, never silently spliced in unescaped.
        with pytest.raises(FilterValidationError):
            build_where(DashboardFilters(channel="o'brien"), self.WEB_COLUMNS)

    def test_end_to_end_injection_attempt_rejected_by_validate_filters_first(self, driver):
        """The realistic path: validate_filters (distinct-value allowlist)
        rejects an injection attempt before build_where is ever reached,
        since it is not a real channel/market/device/platform value."""
        with pytest.raises(FilterValidationError):
            validate_filters(DashboardFilters(market="DE' OR '1'='1"), driver)


# ---------------------------------------------------------------------------
# Filter label formatting.
# ---------------------------------------------------------------------------
class TestBuildFilterLabel:
    def test_no_filters_is_all_data(self):
        assert build_filter_label(DashboardFilters()) == "All data"

    def test_range_label_and_market_joined(self):
        label = build_filter_label(DashboardFilters(market="DE"), range_label="Last 3 months")
        assert label == "Last 3 months · DE"

    def test_multiple_dimensions_all_appended(self):
        label = build_filter_label(
            DashboardFilters(market="DE", channel="organic_direct", device="mobile"),
            range_label="Last 3 months",
        )
        assert label == "Last 3 months · DE · organic_direct · mobile"


# ---------------------------------------------------------------------------
# KPI registry + run_dashboard end to end.
# ---------------------------------------------------------------------------
class TestKpiRegistry:
    def test_loads_at_least_ten_templates(self):
        templates = load_kpi_templates()
        assert len(templates) >= 10
        for tmpl in templates:
            assert {"key", "title", "sql", "filter_columns", "cube"} <= set(tmpl)
            assert "{{where}}" in tmpl["sql"]

    def test_keys_are_unique(self):
        templates = load_kpi_templates()
        keys = [t["key"] for t in templates]
        assert len(keys) == len(set(keys))


@pytest.fixture(scope="module")
def unfiltered_dashboard(driver):
    return run_dashboard(driver, DashboardFilters())


class TestRunDashboardUnfiltered:
    @pytest.fixture
    def result(self, unfiltered_dashboard):
        return unfiltered_dashboard

    def test_filter_label_is_all_data(self, result):
        assert result["filter_label"] == "All data"

    def test_one_card_per_template(self, result):
        assert len(result["cards"]) == len(load_kpi_templates())

    def test_every_card_has_the_legacy_card_shape(self, result):
        for card in result["cards"]:
            assert {"key", "title", "chart", "rows", "answer", "consent_note", "sql"} <= set(card)
            assert card["rows"], f"{card['key']} returned no rows"

    def test_every_card_sql_is_a_nonempty_select_statement(self, result):
        # M11 addendum 2: the exact executed statement, formatted for
        # display via agent.sqlfmt.format_sql_for_display — never empty,
        # never the raw unformatted template placeholder.
        for card in result["cards"]:
            sql = card["sql"]
            assert isinstance(sql, str) and sql.strip()
            assert "{{where}}" not in sql
            assert "select" in sql.lower()

    def test_funnel_stages_card_matches_unfiltered_totals(self, result):
        card = next(c for c in result["cards"] if c["key"] == "dash_funnel_stages")
        by_stage = {r["stage"]: r["users"] for r in card["rows"]}
        assert by_stage["test_starts"] == 100000
        assert by_stage["test_completes"] == 42062

    def test_headline_summary_is_one_short_paragraph(self, result):
        summary = result["headline_summary"]
        assert isinstance(summary, str) and summary
        assert "100,000" in summary or "100000" in summary


class TestRunDashboardFiltered:
    def test_filtering_by_market_narrows_totals(self, driver):
        unfiltered = run_dashboard(driver, DashboardFilters())
        filtered = run_dashboard(driver, DashboardFilters(market="DE"))
        assert filtered["filter_label"] == "DE"

        def total_starts(result):
            card = next(c for c in result["cards"] if c["key"] == "dash_funnel_stages")
            return next(r["users"] for r in card["rows"] if r["stage"] == "test_starts")

        assert 0 < total_starts(filtered) < total_starts(unfiltered)

    def test_filtered_card_sql_contains_the_composed_where(self, driver):
        # M11 addendum 2: the "sql" field is the template with THIS
        # request's WHERE actually composed in, not a generic/unfiltered
        # copy of the template.
        filtered = run_dashboard(driver, DashboardFilters(market="DE"))
        card = next(c for c in filtered["cards"] if c["key"] == "dash_funnel_stages")
        assert "'DE'" in card["sql"] or "DE" in card["sql"]

    def test_relative_range_label_is_used_verbatim(self, driver):
        result = run_dashboard(driver, DashboardFilters(), range_label="Last 3 months")
        assert result["filter_label"] == "Last 3 months"

    def test_invalid_filter_raises_before_any_query(self, driver):
        with pytest.raises(FilterValidationError):
            run_dashboard(driver, DashboardFilters(market="Atlantis"))

    # -- M11-fix: day-level filtering actually narrows the result ----------
    def test_last_n_days_narrows_to_a_small_plausible_window(self, driver):
        """The exact real-run scenario this fix targets: 'last 3 days for
        Germany' must return a SMALL, plausible slice, not the ~6-week
        (or ~all-data) window the pre-fix week-grained cubes produced."""
        horizon = get_data_horizon(driver)
        start, end, label = parse_relative_range("last 3 days", horizon)
        result = run_dashboard(
            driver, DashboardFilters(date_start=start, date_end=end, market="DE"), range_label=label
        )
        assert result["filter_label"] == "Last 3 days · DE"
        card = next(c for c in result["cards"] if c["key"] == "dash_funnel_stages")
        total_starts = next(r["users"] for r in card["rows"] if r["stage"] == "test_starts")
        unfiltered = run_dashboard(driver, DashboardFilters())
        unfiltered_card = next(c for c in unfiltered["cards"] if c["key"] == "dash_funnel_stages")
        unfiltered_starts = next(r["users"] for r in unfiltered_card["rows"] if r["stage"] == "test_starts")
        # A 4-day slice of one market must be a small fraction of the
        # unfiltered ~100 days x 3 markets total -- generously bounded at
        # 10% so this stays robust to synthetic-data regeneration.
        assert 0 < total_starts < unfiltered_starts * 0.10

    def test_applied_range_echoes_the_resolved_dates_and_label(self, driver):
        result = run_dashboard(
            driver,
            DashboardFilters(date_start=date(2026, 8, 27), date_end=date(2026, 8, 30)),
            range_label="Last 3 days",
        )
        assert result["applied_range"] == {
            "start": "2026-08-27",
            "end": "2026-08-30",
            "label": "Last 3 days",
        }

    def test_applied_range_is_none_when_no_date_filter(self, driver):
        result = run_dashboard(driver, DashboardFilters(market="DE"))
        assert result["applied_range"] is None
