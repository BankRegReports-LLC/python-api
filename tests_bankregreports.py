"""
Unit tests for the BankRegReports Python client.

Network is fully mocked (no live API, no DB) — these exercise URL construction,
auth headers, envelope unwrapping → DataFrame, date coercion, the dataset
registry, error mapping, retry/backoff, mutable operations (POST/PATCH/DELETE),
and the pagination helper.

Run from the package directory:

    pytest python-api/tests_bankregreports.py -v
"""

from __future__ import annotations

import json

import pytest
import requests

import bankregreports
from bankregreports import (
    AuthenticationError,
    BankRegError,
    NotFoundError,
    RateLimitError,
    ServerError,
    UpgradeRequiredError,
    ValidationError,
    bankreg,
)

# --------------------------------------------------------------------------- #
# Test infrastructure
# --------------------------------------------------------------------------- #


class FakeResponse:
    def __init__(self, status_code=200, body=None, text="", headers=None, content=b"x"):
        self.status_code = status_code
        self._body = body
        self.text = text if text else (json.dumps(body) if body is not None else "")
        self.headers = headers or {}
        self.content = content if status_code != 204 else b""

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


def make_client(monkeypatch, response):
    """Return (client, calls) where calls captures every GET invocation."""
    calls = []

    def fake_get(url, headers=None, params=None, timeout=None):
        calls.append({"method": "GET", "url": url, "headers": headers, "params": params})
        return response if not isinstance(response, list) else response[len(calls) - 1]

    client = bankreg("brr_testtoken1234", base_url="https://api.example.com")
    monkeypatch.setattr(client._session, "get", fake_get)
    return client, calls


def make_mutable_client(
    monkeypatch, get_resp=None, post_resp=None, patch_resp=None, put_resp=None, delete_resp=None
):
    """Return (client, calls) wired for POST/PATCH/PUT/DELETE as well as GET."""
    calls = []

    def _log(method, url, **kw):
        calls.append({"method": method, "url": url, **kw})

    def fake_get(url, headers=None, params=None, timeout=None):
        _log("GET", url, params=params)
        return get_resp or FakeResponse(body={"data": [], "meta": {}})

    def fake_post(url, headers=None, json=None, params=None, timeout=None):
        _log("POST", url, json=json, params=params)
        return post_resp or FakeResponse(status_code=201, body={"data": {"id": 1}})

    def fake_patch(url, headers=None, json=None, params=None, timeout=None):
        _log("PATCH", url, json=json, params=params)
        return patch_resp or FakeResponse(body={"data": {"id": 1}})

    def fake_put(url, headers=None, json=None, params=None, timeout=None):
        _log("PUT", url, json=json, params=params)
        return put_resp or FakeResponse(body={"data": {"id": 1}})

    def fake_delete(url, headers=None, params=None, timeout=None):
        _log("DELETE", url, params=params)
        return delete_resp or FakeResponse(status_code=204, body=None, content=b"")

    client = bankreg("brr_testtoken1234", base_url="https://api.example.com")
    monkeypatch.setattr(client._session, "get", fake_get)
    monkeypatch.setattr(client._session, "post", fake_post)
    monkeypatch.setattr(client._session, "patch", fake_patch)
    monkeypatch.setattr(client._session, "put", fake_put)
    monkeypatch.setattr(client._session, "delete", fake_delete)
    return client, calls


# --------------------------------------------------------------------------- #
# URL construction + auth
# --------------------------------------------------------------------------- #


def test_url_and_auth_header(monkeypatch):
    resp = FakeResponse(body={"data": [{"rssd_id": 1}], "meta": {"count": 1}})
    client, calls = make_client(monkeypatch, resp)
    client.bank(852218)
    assert calls[0]["url"] == "https://api.example.com/api/v1/banks/852218/"
    assert calls[0]["headers"]["Authorization"] == "Bearer brr_testtoken1234"
    assert calls[0]["headers"]["Accept"] == "application/json"


def test_no_token_omits_auth_header():
    client = bankreg(base_url="https://api.example.com")
    assert "Authorization" not in client._headers


def test_token_from_env(monkeypatch):
    monkeypatch.setenv("BANKREG_API_TOKEN", "brr_fromenv")
    client = bankreg()
    assert client.token == "brr_fromenv"
    assert client._headers["Authorization"] == "Bearer brr_fromenv"


def test_base_url_from_env(monkeypatch):
    monkeypatch.setenv("BANKREG_API_BASE_URL", "https://staging.example.com")
    client = bankreg()
    assert client.base_url == "https://staging.example.com"


def test_repr_contains_dataset_count():
    client = bankreg("brr_abc", base_url="https://x.com")
    r = repr(client)
    assert "datasets=" in r
    assert str(len(bankregreports.DATASETS)) in r


def test_repr_never_shows_secret_characters(monkeypatch):
    monkeypatch.delenv("BANKREG_API_TOKEN", raising=False)
    for token, shown in (
        ("brr_abcd1234efgh", "brr_***"),
        ("brrat_abcd1234efgh", "brrat_***"),
        ("abcd1234efgh", "***"),
        ("abcd_1234efgh", "***"),
    ):
        r = repr(bankreg(token, base_url="https://x.com"))
        assert repr(shown) in r
        assert "1234" not in r
        assert "abcd1" not in r
    assert "no-token" in repr(bankreg(None, base_url="https://x.com"))


def test_parse_retry_after_http_date_past_and_future():
    future = bankregreports._parse_retry_after("Wed, 21 Oct 2099 07:28:00 GMT")
    assert future is not None and future > 0
    assert bankregreports._parse_retry_after("Thu, 01 Jan 1970 00:00:00 GMT") == 0.0


def test_bankreg_alias():
    """BankReg is the CamelCase alias; both names refer to the same class."""
    assert bankregreports.BankReg is bankregreports.bankreg
    client = bankregreports.BankReg("brr_abc", base_url="https://x.com")
    assert isinstance(client, bankreg)


def test_context_manager(monkeypatch):
    closed = []
    client = bankreg("brr_x", base_url="https://x.com")
    monkeypatch.setattr(client._session, "close", lambda: closed.append(True))
    with client as brr:
        assert brr is client
    assert closed == [True]


def test_upgrade_required_error_is_primary():
    """UpgradeRequiredError is the real class; PermissionError_ is its alias."""
    from bankregreports import PermissionError_, UpgradeRequiredError

    assert PermissionError_ is UpgradeRequiredError


# --------------------------------------------------------------------------- #
# Envelope + DataFrame
# --------------------------------------------------------------------------- #


def test_envelope_unwrap_to_dataframe(monkeypatch):
    body = {
        "data": [{"rssd_id": 1, "report_date": "2025-03-31"}],
        "meta": {"count": 1, "source": "MART.BANK_SNAPSHOT"},
    }
    client, _ = make_client(monkeypatch, FakeResponse(body=body))
    df = client.screener()
    assert list(df.columns) == ["rssd_id", "report_date"]
    assert df.attrs["meta"]["source"] == "MART.BANK_SNAPSHOT"
    assert str(df["report_date"].dtype).startswith("datetime64")


def test_single_object_payload_becomes_one_row(monkeypatch):
    body = {"data": {"rssd_id": 852218, "legal_name": "JPMORGAN CHASE BANK"}, "meta": {}}
    client, _ = make_client(monkeypatch, FakeResponse(body=body))
    df = client.bank(852218)
    assert len(df) == 1
    assert df.iloc[0]["legal_name"] == "JPMORGAN CHASE BANK"


def test_as_dataframe_false_returns_raw(monkeypatch):
    body = {"data": [{"a": 1}], "meta": {}}
    client, _ = make_client(monkeypatch, FakeResponse(body=body))
    rows = client.screener(as_dataframe=False)
    assert rows == [{"a": 1}]


def test_multiple_date_cols_coerced(monkeypatch):
    body = {
        "data": [{"report_date": "2025-03-31", "filed_date": "2025-04-10", "legal_name": "X"}],
        "meta": {},
    }
    client, _ = make_client(monkeypatch, FakeResponse(body=body))
    df = client.bank(1)
    assert str(df["report_date"].dtype).startswith("datetime64")
    assert str(df["filed_date"].dtype).startswith("datetime64")


# --------------------------------------------------------------------------- #
# Parameter handling
# --------------------------------------------------------------------------- #


def test_sparse_params_are_dropped(monkeypatch):
    client, calls = make_client(monkeypatch, FakeResponse(body={"data": []}))
    client.screener(state="GA", limit=50)
    sent = calls[0]["params"]
    assert sent == {"state": "GA", "limit": 50}


def test_none_and_empty_string_dropped(monkeypatch):
    client, calls = make_client(monkeypatch, FakeResponse(body={"data": []}))
    client.banks(state="", tier=None)
    assert "state" not in calls[0]["params"]
    assert "tier" not in calls[0]["params"]


def test_empty_list_dropped(monkeypatch):
    client, calls = make_client(monkeypatch, FakeResponse(body={"data": []}))
    client._get_json("test/", {"ids": []})
    assert "ids" not in calls[0]["params"]


def test_extra_filters_pass_through(monkeypatch):
    client, calls = make_client(monkeypatch, FakeResponse(body={"data": []}))
    client.screener(min_roa=1.0, max_efficiency=60)
    assert calls[0]["params"]["min_roa"] == 1.0
    assert calls[0]["params"]["max_efficiency"] == 60


def test_list_param_passed_through(monkeypatch):
    client, calls = make_client(monkeypatch, FakeResponse(body={"data": []}))
    client.bank_compare([852218, 480228, 37])
    assert calls[0]["params"]["rssd_ids"] == [852218, 480228, 37]
    assert calls[0]["url"].endswith("/api/v1/banks/compare/")


# --------------------------------------------------------------------------- #
# Dataset registry
# --------------------------------------------------------------------------- #


def test_dataset_generic_accessor(monkeypatch):
    client, calls = make_client(monkeypatch, FakeResponse(body={"data": {"x": 1}}))
    client.dataset("bank", rssd_id=99)
    assert calls[0]["url"].endswith("/api/v1/banks/99/")


def test_dataset_missing_path_param_raises(monkeypatch):
    client, _ = make_client(monkeypatch, FakeResponse(body={"data": {}}))
    with pytest.raises(BankRegError, match="requires path argument"):
        client.dataset("bank")


def test_unknown_dataset_raises(monkeypatch):
    client, _ = make_client(monkeypatch, FakeResponse(body={"data": {}}))
    with pytest.raises(BankRegError, match="Unknown dataset"):
        client.dataset("not_a_dataset")


def test_list_datasets_columns(monkeypatch):
    client, _ = make_client(monkeypatch, FakeResponse(body={"data": {}}))
    df = client.list_datasets()
    assert {"name", "path", "tier", "description", "path_params"}.issubset(df.columns)
    assert (df["name"] == "screener").any()
    assert (df["name"] == "bank_profile").any()
    assert (df["name"] == "sec_13f").any()
    assert (df["name"] == "healthz").any()


def test_registry_paths_have_consistent_params():
    import string as _string

    for ds in bankregreports.DATASETS.values():
        placeholders = {fn for _, fn, _, _ in _string.Formatter().parse(ds.path) if fn}
        assert placeholders == set(ds.path_params), f"Mismatch on dataset '{ds.name}'"


def test_registry_size():
    assert (
        len(bankregreports.DATASETS) >= 80
    ), f"Expected ≥80 datasets, got {len(bankregreports.DATASETS)}"


# --------------------------------------------------------------------------- #
# Named methods — URL routing
# --------------------------------------------------------------------------- #


def test_bank_search_index_url(monkeypatch):
    client, calls = make_client(monkeypatch, FakeResponse(body={"data": []}))
    client.bank_search_index()
    assert calls[0]["url"].endswith("/api/v1/banks/search-index/")


def test_bank_deposits_url(monkeypatch):
    client, calls = make_client(monkeypatch, FakeResponse(body={"data": {}}))
    client.bank_deposits(99)
    assert calls[0]["url"].endswith("/api/v1/banks/99/deposits/")


def test_bank_deposits_trends_url(monkeypatch):
    client, calls = make_client(monkeypatch, FakeResponse(body={"data": []}))
    client.bank_deposits(99, trends=True, quarters=8)
    assert calls[0]["url"].endswith("/api/v1/banks/99/deposits/trends/")
    assert calls[0]["params"]["quarters"] == 8


def test_bank_securities_url(monkeypatch):
    client, calls = make_client(monkeypatch, FakeResponse(body={"data": {}}))
    client.bank_securities(99)
    assert calls[0]["url"].endswith("/api/v1/banks/99/securities/")


def test_bank_structure_url(monkeypatch):
    client, calls = make_client(monkeypatch, FakeResponse(body={"data": {}}))
    client.bank_structure(99)
    assert calls[0]["url"].endswith("/api/v1/banks/99/structure/")


def test_bank_structure_full_url(monkeypatch):
    client, calls = make_client(monkeypatch, FakeResponse(body={"data": {}}))
    client.bank_structure(99, full=True)
    assert calls[0]["url"].endswith("/api/v1/banks/99/structure/full/")


def test_scorecard_composite_url(monkeypatch):
    client, calls = make_client(monkeypatch, FakeResponse(body={"data": {}}))
    client.scorecard(99, composite=True)
    assert calls[0]["url"].endswith("/api/v1/banks/99/scorecard/composite/")


def test_branches_rollup_url(monkeypatch):
    client, calls = make_client(monkeypatch, FakeResponse(body={"data": []}))
    client.branches(99, rollup=True)
    assert calls[0]["url"].endswith("/api/v1/banks/99/branches/rollup/")


def test_branches_map_url(monkeypatch):
    client, calls = make_client(monkeypatch, FakeResponse(body={"data": []}))
    client.branches(99, as_map=True)
    assert calls[0]["url"].endswith("/api/v1/banks/99/branches/map/")


def test_branches_fdic_url(monkeypatch):
    client, calls = make_client(monkeypatch, FakeResponse(body={"data": []}))
    client.branches(99, source="fdic")
    assert calls[0]["url"].endswith("/api/v1/banks/99/branches/fdic/")


def test_branches_nic_url(monkeypatch):
    client, calls = make_client(monkeypatch, FakeResponse(body={"data": []}))
    client.branches(99, source="nic")
    assert calls[0]["url"].endswith("/api/v1/banks/99/branches/nic/")


def test_prediction_detail_url(monkeypatch):
    client, calls = make_client(monkeypatch, FakeResponse(body={"data": {}}))
    client.prediction(99, detail=True)
    assert calls[0]["url"].endswith("/api/v1/banks/99/prediction/detail/")


def test_sec_8k_body_url(monkeypatch):
    client, calls = make_client(monkeypatch, FakeResponse(body={"data": {}}))
    client.sec_8k_body("0000950170-24-001234")
    assert "sec/8k/0000950170-24-001234/" in calls[0]["url"]


def test_sec_13f_url(monkeypatch):
    client, calls = make_client(monkeypatch, FakeResponse(body={"data": []}))
    client.sec_13f(852218)
    assert calls[0]["url"].endswith("/api/v1/banks/852218/sec/13f/")


def test_holding_company_trends_url(monkeypatch):
    client, calls = make_client(monkeypatch, FakeResponse(body={"data": []}))
    client.holding_company(1234, trends=True, quarters=8)
    assert calls[0]["url"].endswith("/api/v1/holding-cos/1234/trends/")
    assert calls[0]["params"]["quarters"] == 8


def test_cu_branches_url(monkeypatch):
    client, calls = make_client(monkeypatch, FakeResponse(body={"data": []}))
    client.credit_union_branches(12345)
    assert calls[0]["url"].endswith("/api/v1/cus/12345/branches/")


def test_cu_atms_url(monkeypatch):
    client, calls = make_client(monkeypatch, FakeResponse(body={"data": []}))
    client.credit_union_atms(12345)
    assert calls[0]["url"].endswith("/api/v1/cus/12345/atms/")


def test_ncua_enforcement_url(monkeypatch):
    client, calls = make_client(monkeypatch, FakeResponse(body={"data": []}))
    client.ncua_enforcement()
    assert calls[0]["url"].endswith("/api/v1/enforcement/ncua/")


def test_risk_overview_url(monkeypatch):
    client, calls = make_client(monkeypatch, FakeResponse(body={"data": {}}))
    client.risk()
    assert calls[0]["url"].endswith("/api/v1/risk/overview/")


def test_risk_dashboard_url(monkeypatch):
    client, calls = make_client(monkeypatch, FakeResponse(body={"data": {}}))
    client.risk(dashboard=True)
    assert calls[0]["url"].endswith("/api/v1/risk/dashboard/")


def test_risk_model_url(monkeypatch):
    client, calls = make_client(monkeypatch, FakeResponse(body={"data": {}}))
    client.risk(model=True)
    assert calls[0]["url"].endswith("/api/v1/risk/model/")


def test_failures_dashboard_url(monkeypatch):
    client, calls = make_client(monkeypatch, FakeResponse(body={"data": []}))
    client.failures(dashboard=True)
    assert calls[0]["url"].endswith("/api/v1/failures/dashboard/")


def test_reference_index_url(monkeypatch):
    client, calls = make_client(monkeypatch, FakeResponse(body={"data": []}))
    client.reference()
    assert calls[0]["url"].endswith("/api/v1/reference/")


def test_reference_mdrm_url(monkeypatch):
    client, calls = make_client(monkeypatch, FakeResponse(body={"data": {}}))
    client.reference("RCON2170")
    assert calls[0]["url"].endswith("/api/v1/reference/mdrm/RCON2170/")


def test_reference_table_url(monkeypatch):
    client, calls = make_client(monkeypatch, FakeResponse(body={"data": []}))
    client.reference("states")
    assert calls[0]["url"].endswith("/api/v1/reference/states/")


def test_healthz_url(monkeypatch):
    client, calls = make_client(monkeypatch, FakeResponse(body={"data": {"status": "ok"}}))
    result = client.healthz()
    assert calls[0]["url"].endswith("/api/v1/healthz/")
    assert result == {"status": "ok"}  # as_dataframe=False by default


def test_peer_ranking_url(monkeypatch):
    client, calls = make_client(monkeypatch, FakeResponse(body={"data": {}}))
    client.peer_ranking(852218, "cet1_ratio")
    assert calls[0]["url"].endswith("/api/v1/banks/852218/peer-ranking/")
    assert calls[0]["params"]["metric"] == "cet1_ratio"


def test_custom_peer_group_url(monkeypatch):
    client, calls = make_client(monkeypatch, FakeResponse(body={"data": {}}))
    client.custom_peer_group(7)
    assert calls[0]["url"].endswith("/api/v1/peer-groups/custom/7/")


# --------------------------------------------------------------------------- #
# Rate / yield curve routing
# --------------------------------------------------------------------------- #


def test_yield_curve_latest_flag(monkeypatch):
    client, calls = make_client(monkeypatch, FakeResponse(body={"data": []}))
    client.yield_curve(latest=True)
    assert calls[0]["params"]["latest"] == 1


def test_yield_curve_tenor(monkeypatch):
    client, calls = make_client(monkeypatch, FakeResponse(body={"data": []}))
    client.yield_curve(tenor="Y10")
    assert calls[0]["params"]["tenor_code"] == "Y10"


def test_yield_curve_date_range(monkeypatch):
    client, calls = make_client(monkeypatch, FakeResponse(body={"data": []}))
    client.yield_curve(start_date="2020-01-01", end_date="2025-01-01")
    assert calls[0]["params"]["start_date"] == "2020-01-01"
    assert calls[0]["params"]["end_date"] == "2025-01-01"


def test_rates_consumer_url(monkeypatch):
    client, calls = make_client(monkeypatch, FakeResponse(body={"data": []}))
    client.rates("consumer")
    assert calls[0]["url"].endswith("/api/v1/rates/consumer/")


def test_rates_real_yield_url(monkeypatch):
    client, calls = make_client(monkeypatch, FakeResponse(body={"data": []}))
    client.rates("real_yield")
    assert calls[0]["url"].endswith("/api/v1/rates/real-yield/")


def test_rates_national_url(monkeypatch):
    client, calls = make_client(monkeypatch, FakeResponse(body={"data": []}))
    client.rates("national")
    assert calls[0]["url"].endswith("/api/v1/rates/national-rates/")


def test_rates_date_range(monkeypatch):
    client, calls = make_client(monkeypatch, FakeResponse(body={"data": []}))
    client.rates("mortgage", start_date="2022-01-01", end_date="2024-12-31")
    assert calls[0]["params"]["start_date"] == "2022-01-01"
    assert calls[0]["params"]["end_date"] == "2024-12-31"


def test_rates_latest(monkeypatch):
    client, calls = make_client(monkeypatch, FakeResponse(body={"data": []}))
    client.rates("deposit", latest=True)
    assert calls[0]["params"]["latest"] == 1


def test_rates_unknown_kind():
    client = bankreg("brr_x", base_url="https://x.com")
    with pytest.raises(BankRegError, match="must be one of"):
        client.rates("unknown")


# --------------------------------------------------------------------------- #
# Failure routing
# --------------------------------------------------------------------------- #


def test_failures_year_switches_dataset(monkeypatch):
    client, calls = make_client(monkeypatch, FakeResponse(body={"data": []}))
    client.failures(year=2023)
    assert calls[0]["url"].endswith("/api/v1/failures/by-year/")
    assert calls[0]["params"]["year"] == 2023


def test_failures_recent_no_year(monkeypatch):
    client, calls = make_client(monkeypatch, FakeResponse(body={"data": []}))
    client.failures()
    assert calls[0]["url"].endswith("/api/v1/failures/recent/")


# --------------------------------------------------------------------------- #
# Error mapping
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "status_code,exc",
    [
        (401, AuthenticationError),
        (403, UpgradeRequiredError),
        (404, NotFoundError),
        (422, ValidationError),
    ],
)
def test_error_mapping(monkeypatch, status_code, exc):
    body = {"error": {"code": "x", "message": "boom", "request_id": "abc123"}}
    client, _ = make_client(monkeypatch, FakeResponse(status_code=status_code, body=body))
    with pytest.raises(exc) as ei:
        client.bank(1)
    assert ei.value.status_code == status_code
    assert ei.value.request_id == "abc123"


def test_rate_limit_carries_retry_after(monkeypatch):
    body = {"error": {"code": "throttled", "message": "slow down"}}
    resp = FakeResponse(status_code=429, body=body, headers={"Retry-After": "2"})
    client = bankreg("brr_x", base_url="https://api.example.com", max_retries=0)
    monkeypatch.setattr(client._session, "get", lambda *a, **k: resp)
    with pytest.raises(RateLimitError) as ei:
        client.bank(1)
    assert ei.value.retry_after == 2.0


def test_retry_after_http_date_does_not_crash(monkeypatch):
    # RFC 7231 permits an HTTP-date form; it must map to RateLimitError, not a
    # bare ValueError from float() parsing.
    body = {"error": {"code": "throttled", "message": "slow down"}}
    resp = FakeResponse(
        status_code=429, body=body, headers={"Retry-After": "Wed, 21 Oct 2099 07:28:00 GMT"}
    )
    client = bankreg("brr_x", base_url="https://api.example.com", max_retries=0)
    monkeypatch.setattr(client._session, "get", lambda *a, **k: resp)
    with pytest.raises(RateLimitError) as ei:
        client.bank(1)
    assert ei.value.retry_after is not None and ei.value.retry_after > 0


def test_retry_after_malformed_header_is_none(monkeypatch):
    body = {"error": {"code": "throttled", "message": "slow down"}}
    resp = FakeResponse(status_code=429, body=body, headers={"Retry-After": "soon"})
    client = bankreg("brr_x", base_url="https://api.example.com", max_retries=0)
    monkeypatch.setattr(client._session, "get", lambda *a, **k: resp)
    with pytest.raises(RateLimitError) as ei:
        client.bank(1)
    assert ei.value.retry_after is None


def test_parse_retry_after_seconds_and_junk():
    assert bankregreports._parse_retry_after("5") == 5.0
    assert bankregreports._parse_retry_after(None) is None
    assert bankregreports._parse_retry_after("") is None
    assert bankregreports._parse_retry_after("not-a-date") is None


def test_backoff_override_is_capped(monkeypatch):
    slept = []
    monkeypatch.setattr(bankregreports.time, "sleep", lambda s: slept.append(s))
    client = bankreg("brr_x", base_url="https://api.example.com", max_retries=3)
    client._sleep_backoff(0, override=9999.0)
    assert slept == [bankregreports.DEFAULT_MAX_RETRY_SLEEP]


def test_5xx_raises_server_error(monkeypatch):
    body = {"error": {"message": "internal error"}}
    client = bankreg("brr_x", base_url="https://api.example.com", max_retries=0)
    monkeypatch.setattr(
        client._session,
        "get",
        lambda *a, **k: FakeResponse(status_code=503, body=body),
    )
    with pytest.raises(ServerError):
        client.screener()


def test_validation_error_is_not_retried(monkeypatch):
    body = {"error": {"code": "invalid_params", "message": "bad param"}}
    calls = []

    def fake_get(*a, **k):
        calls.append(1)
        return FakeResponse(status_code=422, body=body)

    client = bankreg("brr_x", base_url="https://api.example.com", max_retries=3)
    monkeypatch.setattr(client._session, "get", fake_get)
    monkeypatch.setattr(client, "_sleep_backoff", lambda *a, **k: None)
    with pytest.raises(ValidationError):
        client.bank(1)
    assert len(calls) == 1, "422 should not be retried"


# --------------------------------------------------------------------------- #
# Retry / backoff
# --------------------------------------------------------------------------- #


def test_retry_then_success(monkeypatch):
    ok_body = {"data": [{"a": 1}], "meta": {}}
    responses = [
        FakeResponse(status_code=503, body={"error": {"message": "upstream"}}),
        FakeResponse(status_code=200, body=ok_body),
    ]
    client = bankreg("brr_x", base_url="https://api.example.com", max_retries=2)
    monkeypatch.setattr(client, "_sleep_backoff", lambda *a, **k: None)
    seq = iter(responses)
    monkeypatch.setattr(client._session, "get", lambda *a, **k: next(seq))
    df = client.screener()
    assert len(df) == 1


def test_exhausted_retries_raise(monkeypatch):
    body = {"error": {"message": "upstream"}}
    client = bankreg("brr_x", base_url="https://api.example.com", max_retries=2)
    monkeypatch.setattr(client, "_sleep_backoff", lambda *a, **k: None)
    monkeypatch.setattr(
        client._session,
        "get",
        lambda *a, **k: FakeResponse(status_code=503, body=body),
    )
    with pytest.raises(ServerError):
        client.bank(1)


# --------------------------------------------------------------------------- #
# Transport errors
# --------------------------------------------------------------------------- #


def test_connection_failure_raises_api_connection_error(monkeypatch):
    client = bankreg("brr_x", base_url="https://api.example.com", max_retries=0)

    def boom(*a, **k):
        raise requests.ConnectionError("refused")

    monkeypatch.setattr(client._session, "get", boom)
    with pytest.raises(bankregreports.APIConnectionError):
        client.bank(1)


def test_timeout_raises_api_timeout_error(monkeypatch):
    client = bankreg("brr_x", base_url="https://api.example.com", max_retries=0)

    def boom(*a, **k):
        raise requests.Timeout("too slow")

    monkeypatch.setattr(client._session, "get", boom)
    with pytest.raises(bankregreports.APITimeoutError):
        client.bank(1)


def test_transport_errors_stay_catchable_as_server_error(monkeypatch):
    """The new types subclass ServerError so pre-existing handlers keep working."""
    client = bankreg("brr_x", base_url="https://api.example.com", max_retries=0)

    def boom(*a, **k):
        raise requests.Timeout("too slow")

    monkeypatch.setattr(client._session, "get", boom)
    with pytest.raises(ServerError):
        client.bank(1)


def test_transport_errors_are_retried(monkeypatch):
    ok_body = {"data": [{"a": 1}], "meta": {}}
    calls = []

    def flaky(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            raise requests.ConnectionError("reset")
        return FakeResponse(status_code=200, body=ok_body)

    client = bankreg("brr_x", base_url="https://api.example.com", max_retries=2)
    monkeypatch.setattr(client, "_sleep_backoff", lambda *a, **k: None)
    monkeypatch.setattr(client._session, "get", flaky)
    assert len(client.screener()) == 1
    assert len(calls) == 2


def test_write_transport_failure_is_not_retried(monkeypatch):
    calls = []

    def boom(*a, **k):
        calls.append(1)
        raise requests.ConnectionError("refused")

    client = bankreg("brr_x", base_url="https://api.example.com", max_retries=3)
    monkeypatch.setattr(client._session, "post", boom)
    with pytest.raises(bankregreports.APIConnectionError):
        client.watchlist_add(852218)
    assert len(calls) == 1, "mutations must never be retried"


# --------------------------------------------------------------------------- #
# Pagination
# --------------------------------------------------------------------------- #


def test_pages_iterates_until_short_page(monkeypatch):
    page1 = {"data": [{"id": i} for i in range(3)], "meta": {}}
    page2 = {"data": [{"id": i} for i in range(3, 5)], "meta": {}}  # 2 < page_size=3 → stop

    responses = [FakeResponse(body=page1), FakeResponse(body=page2)]
    seq = iter(responses)

    client = bankreg("brr_x", base_url="https://api.example.com")
    monkeypatch.setattr(client._session, "get", lambda *a, **k: next(seq))

    pages = list(client.pages("banks", page_size=3))
    assert len(pages) == 2
    assert len(pages[0]) == 3
    assert len(pages[1]) == 2


def test_pages_respects_max_records(monkeypatch):
    big_page = {"data": [{"id": i} for i in range(10)], "meta": {}}
    responses = [FakeResponse(body=big_page)] * 5
    seq = iter(responses)

    client = bankreg("brr_x", base_url="https://api.example.com")
    monkeypatch.setattr(client._session, "get", lambda *a, **k: next(seq))

    pages = list(client.pages("banks", page_size=10, max_records=25))
    total = sum(len(p) for p in pages)
    assert total <= 25


def test_pages_offset_increments(monkeypatch):
    calls = []

    def fake_get(url, headers=None, params=None, timeout=None):
        calls.append(params.get("offset", 0))
        # Return 2 rows when offset < 4, empty after
        if (params.get("offset") or 0) < 4:
            return FakeResponse(body={"data": [{"x": 1}, {"x": 2}], "meta": {}})
        return FakeResponse(body={"data": [], "meta": {}})

    client = bankreg("brr_x", base_url="https://api.example.com")
    monkeypatch.setattr(client._session, "get", fake_get)

    list(client.pages("banks", page_size=2))
    assert calls == [0, 2, 4]  # empty page at offset=4 stops the loop


# --------------------------------------------------------------------------- #
# Mutable operations — watchlist
# --------------------------------------------------------------------------- #


def test_watchlist_get_url(monkeypatch):
    client, calls = make_client(monkeypatch, FakeResponse(body={"data": []}))
    client.watchlist()
    assert calls[0]["url"].endswith("/api/v1/watchlist/")


def test_watchlist_add_posts(monkeypatch):
    client, calls = make_mutable_client(
        monkeypatch,
        post_resp=FakeResponse(status_code=201, body={"data": {"rssd_id": 99, "id": 5}}),
    )
    result = client.watchlist_add(99, bank_name="Test Bank")
    post = next(c for c in calls if c["method"] == "POST")
    assert post["url"].endswith("/api/v1/watchlist/")
    assert post["json"]["rssd_id"] == 99
    assert post["json"]["bank_name"] == "Test Bank"
    assert result["data"]["id"] == 5


def test_watchlist_remove_deletes(monkeypatch):
    client, calls = make_mutable_client(monkeypatch)
    ok = client.watchlist_remove(99)
    delete = next(c for c in calls if c["method"] == "DELETE")
    assert "watchlist/99/" in delete["url"]
    assert ok is True


# --------------------------------------------------------------------------- #
# Mutable operations — alerts
# --------------------------------------------------------------------------- #


def test_alert_create_posts(monkeypatch):
    client, calls = make_mutable_client(
        monkeypatch,
        post_resp=FakeResponse(status_code=201, body={"data": {"id": 10}}),
    )
    client.alert_create(99, "cet1_ratio", "below", 8.0)
    post = next(c for c in calls if c["method"] == "POST")
    assert "watchlist/alerts/" in post["url"]
    body = post["json"]
    assert body["metric"] == "cet1_ratio"
    assert body["direction"] == "below"
    assert body["threshold"] == 8.0


def test_alert_create_invalid_direction():
    client = bankreg("brr_x", base_url="https://x.com")
    with pytest.raises(BankRegError, match="direction"):
        client.alert_create(99, "cet1_ratio", "sideways", 8.0)


def test_alert_update_patches(monkeypatch):
    client, calls = make_mutable_client(monkeypatch)
    client.alert_update(10, is_active=False)
    patch = next(c for c in calls if c["method"] == "PATCH")
    assert "watchlist/alerts/10/" in patch["url"]
    assert patch["json"]["is_active"] is False


def test_alert_delete_deletes(monkeypatch):
    client, calls = make_mutable_client(monkeypatch)
    ok = client.alert_delete(10)
    delete = next(c for c in calls if c["method"] == "DELETE")
    assert "watchlist/alerts/10/" in delete["url"]
    assert ok is True


# --------------------------------------------------------------------------- #
# Mutable operations — graphs
# --------------------------------------------------------------------------- #


def test_graph_create_posts(monkeypatch):
    client, calls = make_mutable_client(
        monkeypatch,
        post_resp=FakeResponse(status_code=201, body={"data": {"id": 7}}),
    )
    client.graph_create("My Chart", "trend", description="test", visibility="public")
    post = next(c for c in calls if c["method"] == "POST")
    assert "graphs/" in post["url"]
    assert post["json"]["name"] == "My Chart"
    assert post["json"]["kind"] == "trend"
    assert post["json"]["visibility"] == "public"


def test_graph_update_puts(monkeypatch):
    client, calls = make_mutable_client(monkeypatch)
    client.graph_update(7, name="Renamed")
    put = next(c for c in calls if c["method"] == "PUT")
    assert "graphs/7/" in put["url"]
    assert put["json"]["name"] == "Renamed"


def test_graph_delete_deletes(monkeypatch):
    client, calls = make_mutable_client(monkeypatch)
    ok = client.graph_delete(7)
    delete = next(c for c in calls if c["method"] == "DELETE")
    assert "graphs/7/" in delete["url"]
    assert ok is True


def test_graph_fork_posts(monkeypatch):
    client, calls = make_mutable_client(
        monkeypatch,
        post_resp=FakeResponse(status_code=201, body={"data": {"id": 8}}),
    )
    client.graph_fork(7, name="My Fork")
    post = next(c for c in calls if c["method"] == "POST")
    assert "graphs/7/fork/" in post["url"]
    assert post["json"]["name"] == "My Fork"


def test_chart_resolve_posts(monkeypatch):
    client, calls = make_mutable_client(
        monkeypatch,
        post_resp=FakeResponse(body={"data": {"series": []}}),
    )
    client.chart_resolve("trend", {"metric": "roa", "rssd_id": 99})
    post = next(c for c in calls if c["method"] == "POST")
    assert "charts/resolve/" in post["url"]
    assert post["json"]["kind"] == "trend"
    assert post["json"]["config"]["metric"] == "roa"


# --------------------------------------------------------------------------- #
# Comprehensive URL routing — parametrized over every named method
# --------------------------------------------------------------------------- #
#
# Each tuple: (method_name, positional_args, keyword_args, expected_url_suffix)
# Covers every named method that has a deterministic URL (no branching).
# Methods with routing logic (trends/composite/detail/source flags) are tested
# in dedicated tests below this matrix.
#
_SIMPLE_ROUTES = [
    # bank core
    ("bank_profile", [99], {}, "/api/v1/banks/99/profile/"),
    ("bank_hmda", [99], {}, "/api/v1/banks/99/hmda/"),
    ("loan_portfolio", [99], {}, "/api/v1/banks/99/loan-portfolio/"),
    ("bank_fry15", [99], {}, "/api/v1/banks/99/fry15/"),
    ("bank_events", [99], {}, "/api/v1/banks/99/events/"),
    ("bank_history", [99], {}, "/api/v1/banks/99/history/"),
    ("ranks", [99], {}, "/api/v1/banks/99/ranks/"),
    ("peers", [99], {}, "/api/v1/banks/99/peers/"),
    ("peer_comparison", [99], {}, "/api/v1/banks/99/peers/comparison/"),
    ("enforcement_actions", [99], {}, "/api/v1/banks/99/enforcement/"),
    ("complaints", [99], {}, "/api/v1/banks/99/complaints/"),
    # SEC
    ("sec_filings", [99], {}, "/api/v1/banks/99/sec/filings/"),
    ("sec_facts", [99], {}, "/api/v1/banks/99/sec/facts/"),
    ("sec_8k", [99], {}, "/api/v1/banks/99/sec/8k/"),
    ("sec_ma", [99], {}, "/api/v1/banks/99/sec/ma/"),
    ("sec_officers", [99], {}, "/api/v1/banks/99/sec/officers/"),
    ("executive_comp", [99], {}, "/api/v1/banks/99/sec/executive-comp/"),
    ("sec_restatements", [99], {}, "/api/v1/banks/99/sec/restatements/"),
    ("sec_insider_txns", [99], {}, "/api/v1/banks/99/sec/insider-txns/"),
    ("sec_insider_summary", [99], {}, "/api/v1/banks/99/sec/insider-summary/"),
    ("sec_filing_velocity", [99], {}, "/api/v1/banks/99/sec/velocity/"),
    ("sec_filer_history", [99], {}, "/api/v1/banks/99/sec/filer-history/"),
    ("sec_filing_sections", [99], {}, "/api/v1/banks/99/sec/sections/"),
    ("sec_13dg", [99], {}, "/api/v1/banks/99/sec/13dg/"),
    # screeners
    ("screener_dataset", [], {}, "/api/v1/screener/dataset/"),
    ("ma_screener", [], {}, "/api/v1/screener/ma/"),
    ("loan_concentration", [], {}, "/api/v1/screener/loan-concentration/"),
    ("growth_screener", [], {}, "/api/v1/screener/growth/"),
    # industry
    ("national_deposit_rates", [], {}, "/api/v1/industry/deposit-rates/"),
    ("industry_benchmarks", [], {}, "/api/v1/industry/benchmarks/"),
    ("industry_macro", [], {}, "/api/v1/industry/macro/"),
    ("industry_dashboard", [], {}, "/api/v1/industry/dashboard/"),
    ("industry_securities_trends", [], {}, "/api/v1/industry/securities/trends/"),
    ("peer_groups", [], {}, "/api/v1/industry/peer-groups/"),
    # risk / events
    ("high_risk_banks", [], {}, "/api/v1/risk/high-risk-banks/"),
    ("events", [], {}, "/api/v1/events/"),
    ("enforcement", [], {}, "/api/v1/enforcement/"),
    # credit unions
    ("credit_unions", [], {}, "/api/v1/cus/"),
    ("credit_union", [12345], {}, "/api/v1/cus/12345/"),
    ("cu_peer_benchmarks", [], {}, "/api/v1/cus/peer-benchmarks/"),
    # holding companies
    ("holding_companies", [], {}, "/api/v1/holding-cos/"),
    ("holding_company", [1234], {}, "/api/v1/holding-cos/1234/"),
    # custom peers
    ("custom_peer_groups", [], {}, "/api/v1/peer-groups/custom/"),
    # catalog / reference
    ("metrics", [], {}, "/api/v1/metrics/"),
    ("datasets_catalog", [], {}, "/api/v1/datasets/"),
]


@pytest.mark.parametrize("method,args,kwargs,suffix", _SIMPLE_ROUTES)
def test_method_url_routing(monkeypatch, method, args, kwargs, suffix):
    client, calls = make_client(monkeypatch, FakeResponse(body={"data": []}))
    getattr(client, method)(*args, **kwargs)
    assert calls[0]["url"].endswith(
        suffix
    ), f"{method}({args}, {kwargs}) → {calls[0]['url']!r}, expected suffix {suffix!r}"


# --------------------------------------------------------------------------- #
# Routing methods with branching logic
# --------------------------------------------------------------------------- #


def test_bank_trends_quarters_param(monkeypatch):
    client, calls = make_client(monkeypatch, FakeResponse(body={"data": []}))
    client.bank_trends(99, quarters=8)
    assert calls[0]["url"].endswith("/api/v1/banks/99/trends/")
    assert calls[0]["params"]["quarters"] == 8


def test_bank_deep_dive_periods_param(monkeypatch):
    client, calls = make_client(monkeypatch, FakeResponse(body={"data": []}))
    client.bank_deep_dive(99, periods=3)
    assert calls[0]["url"].endswith("/api/v1/banks/99/deep-dive/")
    assert calls[0]["params"]["periods"] == 3


def test_bank_securities_trends_url(monkeypatch):
    client, calls = make_client(monkeypatch, FakeResponse(body={"data": []}))
    client.bank_securities(99, trends=True, quarters=6)
    assert calls[0]["url"].endswith("/api/v1/banks/99/securities/trends/")
    assert calls[0]["params"]["quarters"] == 6


def test_industry_trends_url_and_quarters(monkeypatch):
    client, calls = make_client(monkeypatch, FakeResponse(body={"data": []}))
    client.industry(trends=True, quarters=10)
    assert calls[0]["url"].endswith("/api/v1/industry/trends/")
    assert calls[0]["params"]["quarters"] == 10


def test_industry_snapshot_url(monkeypatch):
    client, calls = make_client(monkeypatch, FakeResponse(body={"data": {}}))
    client.industry()
    assert calls[0]["url"].endswith("/api/v1/industry/")


def test_rate_environment_snapshot_url(monkeypatch):
    client, calls = make_client(monkeypatch, FakeResponse(body={"data": {}}))
    client.rate_environment()
    assert calls[0]["url"].endswith("/api/v1/industry/rate-environment/")


def test_rate_environment_trends_url(monkeypatch):
    client, calls = make_client(monkeypatch, FakeResponse(body={"data": []}))
    client.rate_environment(trends=True, quarters=8)
    assert calls[0]["url"].endswith("/api/v1/industry/rate-environment/trends/")
    assert calls[0]["params"].get("quarters") == 8


def test_credit_union_trends_url(monkeypatch):
    client, calls = make_client(monkeypatch, FakeResponse(body={"data": []}))
    client.credit_union(12345, trends=True, quarters=8)
    assert calls[0]["url"].endswith("/api/v1/cus/12345/trends/")
    assert calls[0]["params"]["quarters"] == 8


def test_holding_company_snapshot_url(monkeypatch):
    client, calls = make_client(monkeypatch, FakeResponse(body={"data": {}}))
    client.holding_company(1234)
    assert calls[0]["url"].endswith("/api/v1/holding-cos/1234/")
    assert "quarters" not in (calls[0]["params"] or {})


def test_datasets_catalog_key_url(monkeypatch):
    client, calls = make_client(monkeypatch, FakeResponse(body={"data": {}}))
    client.datasets_catalog(key="bank")
    assert calls[0]["url"].endswith("/api/v1/datasets/bank/")


def test_alert_list_url(monkeypatch):
    client, calls = make_client(monkeypatch, FakeResponse(body={"data": []}))
    client.alert_list()
    assert calls[0]["url"].endswith("/api/v1/watchlist/alerts/")


def test_graphs_scope_param(monkeypatch):
    client, calls = make_client(monkeypatch, FakeResponse(body={"data": []}))
    client.graphs(scope="public")
    assert calls[0]["url"].endswith("/api/v1/graphs/")
    assert calls[0]["params"]["scope"] == "public"


def test_graph_get_url(monkeypatch):
    client, calls = make_client(monkeypatch, FakeResponse(body={"data": {}}))
    client.graph(7)
    assert calls[0]["url"].endswith("/api/v1/graphs/7/")


def test_bank_events_limit_param(monkeypatch):
    client, calls = make_client(monkeypatch, FakeResponse(body={"data": []}))
    client.bank_events(99, limit=200)
    assert calls[0]["url"].endswith("/api/v1/banks/99/events/")
    assert calls[0]["params"]["limit"] == 200


def test_scorecard_default_url(monkeypatch):
    client, calls = make_client(monkeypatch, FakeResponse(body={"data": {}}))
    client.scorecard(99)
    assert calls[0]["url"].endswith("/api/v1/banks/99/scorecard/")


def test_prediction_default_url(monkeypatch):
    client, calls = make_client(monkeypatch, FakeResponse(body={"data": {}}))
    client.prediction(99)
    assert calls[0]["url"].endswith("/api/v1/banks/99/prediction/")


def test_branches_default_url(monkeypatch):
    client, calls = make_client(monkeypatch, FakeResponse(body={"data": []}))
    client.branches(99)
    assert calls[0]["url"].endswith("/api/v1/banks/99/branches/")


def test_holding_company_trends_quarters(monkeypatch):
    client, calls = make_client(monkeypatch, FakeResponse(body={"data": []}))
    client.holding_company(1234, trends=True, quarters=6)
    assert calls[0]["url"].endswith("/api/v1/holding-cos/1234/trends/")
    assert calls[0]["params"]["quarters"] == 6


# --------------------------------------------------------------------------- #
# docs/index.html code samples must match the real client
# --------------------------------------------------------------------------- #


def _doc_python_blocks():
    import html
    import re
    from pathlib import Path

    src = (Path(__file__).parent / "docs" / "index.html").read_text(encoding="utf-8")
    blocks = re.findall(r'<code class="language-python">(.*?)</code>', src, flags=re.S)
    sigs = re.findall(r'<div class="method-signature">(.*?)</div>', src, flags=re.S)
    clean = [html.unescape(re.sub(r"<[^>]+>", "", b)) for b in blocks]
    return clean, [html.unescape(re.sub(r"<[^>]+>", "", s)).strip() for s in sigs]


def test_docs_python_samples_call_real_methods_with_real_arguments():
    """The docs page once showed ``BankReg(api_key=..., base_url=".../api/v1")``
    and a dozen methods with arguments the client does not take."""
    import ast
    import inspect

    blocks, _ = _doc_python_blocks()
    client = bankreg("brr_x", base_url="https://x.com")
    ctor = inspect.signature(bankreg)
    problems = []
    for code in blocks:
        for node in ast.walk(ast.parse(code)):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            owner = node.func.value
            if isinstance(owner, ast.Name) and owner.id == "brr":
                fn = getattr(client, node.func.attr, None)
                if fn is None:
                    problems.append(f"no method brr.{node.func.attr}")
                    continue
                sig = inspect.signature(fn)
            elif (
                isinstance(owner, ast.Name)
                and owner.id == "bankregreports"
                and node.func.attr in ("BankReg", "bankreg")
            ):
                sig = ctor
            else:
                continue
            try:
                sig.bind(*[None] * len(node.args), **{k.arg: None for k in node.keywords if k.arg})
            except TypeError as exc:
                problems.append(f"{node.func.attr}: {exc}")
    assert not problems, problems


def test_docs_method_signatures_name_real_parameters():
    import inspect
    import re

    _, sigs = _doc_python_blocks()
    client = bankreg("brr_x", base_url="https://x.com")
    problems = []
    for text in sigs:
        m = re.match(r"brr\.(\w+)\((.*)\)", text)
        assert m, text
        fn = getattr(client, m.group(1), None)
        if fn is None:
            problems.append(f"no method {m.group(1)}")
            continue
        params = inspect.signature(fn).parameters
        for arg in m.group(2).split(","):
            name = arg.strip().split("=")[0]
            if name and not name.startswith("*") and name not in params:
                problems.append(f"{text}: {name}")
    assert not problems, problems
