from paloma_data.runtime_health import (
    LIVE_DETAILS_SOURCE_COLUMNS,
    live_details_runtime_health,
)


class _Cursor:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


class _Connection:
    def __init__(self, security, coverage):
        self.rows = iter((security, coverage))

    def execute(self, _query):
        return _Cursor(next(self.rows))


def test_live_details_runtime_health_accepts_exact_least_privilege_contract():
    result = live_details_runtime_health(
        _Connection(
            {
                "rls_enabled": True,
                "runtime_policy_enabled": True,
                "readable_columns": sorted(LIVE_DETAILS_SOURCE_COLUMNS),
            },
            {
                "expected_publications": 92,
                "eligible_publications": 92,
                "publications_needing_live_hours_or_price": 92,
            },
        )
    )

    assert result["healthy"] is True
    assert all(result["checks"].values())


def test_live_details_runtime_health_rejects_hidden_rows_or_extra_columns():
    result = live_details_runtime_health(
        _Connection(
            {
                "rls_enabled": True,
                "runtime_policy_enabled": False,
                "readable_columns": [*sorted(LIVE_DETAILS_SOURCE_COLUMNS), "hours"],
            },
            {
                "expected_publications": 92,
                "eligible_publications": 0,
                "publications_needing_live_hours_or_price": 92,
            },
        )
    )

    assert result["healthy"] is False
    assert result["checks"] == {
        "source_records_rls_enabled": True,
        "runtime_policy_enabled": False,
        "runtime_columns_least_privilege": False,
        "all_expected_publications_eligible": False,
    }
