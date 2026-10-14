"""
BankRegReports — Python client for the BankRegReports Data API.

Programmatic access to regulatory data on every US bank, credit union, and
holding company: Call Report / UBPR metrics, CAMELS-style scorecards, peer
benchmarks, failures, M&A and structural events, SEC/EDGAR filings and
insider transactions, branch networks, HMDA mortgage data, ML failure
predictions, the national rate environment, and a multi-metric screener.

This package returns ``pandas.DataFrame`` results by default, so analysts go
from token to model in three lines:

    >>> import bankregreports
    >>> brr = bankregreports.BankReg("brr_xxx")   # or set BANKREG_API_TOKEN
    >>> df = brr.screener(state="GA", min_assets=500_000)
    >>> jpm = brr.bank(852218)

Every method is a thin wrapper over a documented REST endpoint at
``https://api.bankregreports.com/api/v1/`` and returns a DataFrame whose
``.attrs["meta"]`` carries response metadata (source, report date, pagination
cursors). Authentication is a Bearer API key (``brr_…``) created in your
account dashboard.

The dataset surface is **registry-driven** (see ``DATASETS``): named methods
are thin, and a generic :meth:`bankreg.dataset` accessor plus
:meth:`bankreg.list_datasets` keep the client forward-compatible as the API
adds or reshapes datasets — new datasets are reachable by name without a
client upgrade.

Mutable operations (watchlist, alerts, saved graphs) use the same client
object but call :meth:`bankreg.watchlist_add`, :meth:`bankreg.alert_create`,
etc., which POST/PATCH/DELETE to the API and return the server's response.

Use :meth:`bankreg.pages` to iterate over paginated datasets without manual
offset management:

    >>> for page in brr.pages("screener", page_size=200, state="CA"):
    ...     process(page)

Copyright 2024-2026 BankRegReports LLC. MIT License. See LICENSE.
"""

from __future__ import annotations

import os
import string
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Literal, Optional

import requests

__version__ = "0.2.0"

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

DEFAULT_BASE_URL = "https://api.bankregreports.com"
DEFAULT_API_VERSION = "v1"
DEFAULT_TIMEOUT = 30.0
DEFAULT_MAX_RETRIES = 3
DEFAULT_PAGE_SIZE = 50
DEFAULT_MAX_RETRY_SLEEP = 30.0  # ceiling for a single auto-retry wait, in seconds

ENV_TOKEN = "BANKREG_API_TOKEN"  # nosec B105 — env-var NAME, not a credential
ENV_BASE_URL = "BANKREG_API_BASE_URL"

_USER_AGENT = f"bankregreports-python/{__version__}"

_COMMON_DATE_COLS = (
    "report_date",
    "as_of_date",
    "date",
    "filed_date",
    "filing_date",
    "transaction_date",
    "effective_date",
    "established",
    "event_date",
    "period",
    "quarter",
    "acquired_date",
    "closed_date",
    "open_date",
)


# --------------------------------------------------------------------------- #
# Exceptions
# --------------------------------------------------------------------------- #


class BankRegError(Exception):
    """Base for every error raised by this client.

    Carries the HTTP ``status_code``, a stable machine ``code`` slug, and a
    ``request_id`` (echo it to support to recover full server-side context).
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: Optional[int] = None,
        code: Optional[str] = None,
        request_id: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.request_id = request_id


class AuthenticationError(BankRegError):
    """401 — missing, malformed, or revoked API key."""


class UpgradeRequiredError(BankRegError):
    """403 — authenticated but the key's plan/scope can't reach this dataset."""


PermissionError_ = UpgradeRequiredError  # backward-compat alias


class NotFoundError(BankRegError):
    """404 — the institution or resource does not exist."""


class RateLimitError(BankRegError):
    """429 — throttled. ``retry_after`` is seconds to wait, when provided."""

    def __init__(self, *args: Any, retry_after: Optional[float] = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.retry_after = retry_after


class ValidationError(BankRegError):
    """422 — request parameters failed server-side validation."""


class ServerError(BankRegError):
    """5xx — upstream/server failure (retried automatically up to max_retries)."""


class APIConnectionError(ServerError):
    """The request never reached a server: DNS failure, refused, reset, TLS error.

    Subclasses :class:`ServerError` so existing ``except ServerError`` handlers
    keep working; catch this directly to distinguish "your network" from "our
    server returned 5xx".
    """


class APITimeoutError(APIConnectionError):
    """The request exceeded ``timeout`` seconds without a complete response."""


def _transport_error(url: str, exc: Exception) -> APIConnectionError:
    """Classify a requests transport failure. Timeouts are worth their own type
    because the retry that follows is far more likely to succeed."""
    if isinstance(exc, requests.Timeout):
        return APITimeoutError(f"Request to {url} timed out: {exc}")
    return APIConnectionError(f"Network error contacting {url}: {exc}")


def _parse_retry_after(value: Optional[str]) -> Optional[float]:
    """Parse a ``Retry-After`` header into seconds.

    Per RFC 7231 the value is either a delay in seconds or an HTTP-date; both
    forms are handled. Returns ``None`` when absent or unparseable — never raises,
    so a malformed header can't mask the underlying 429.
    """
    if not value:
        return None
    value = value.strip()
    try:
        return float(value)
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


# --------------------------------------------------------------------------- #
# Dataset registry
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Dataset:
    """Declarative spec for one API dataset (GET endpoints only).

    ``path`` is a format string appended to ``/api/{version}/`` and may
    contain ``{placeholders}`` filled from keyword args; the rest become query
    params. ``records`` names a nested key inside ``data`` when rows aren't
    the top-level payload.
    """

    name: str
    path: str
    description: str = ""
    tier: str = "public"  # public | free | pro | enterprise
    date_cols: Sequence[str] = field(default_factory=tuple)
    records: Optional[str] = None
    path_params: Sequence[str] = field(default_factory=tuple)


def _ds(name: str, path: str, **kw: Any) -> tuple[str, Dataset]:
    params = tuple(fn for _, fn, _, _ in string.Formatter().parse(path) if fn)
    return name, Dataset(name=name, path=path, path_params=params, **kw)


DATASETS: dict[str, Dataset] = dict(
    [
        # Banks: directory + snapshot
        _ds("banks", "banks/", description="Directory of FDIC-insured banks.", tier="public"),
        _ds(
            "bank_search_index",
            "banks/search-index/",
            description=(
                "Lightweight bank search index (rssd_id, name, city, state, assets) "
                "for client-side filtering. Requires a (free) API key."
            ),
            tier="free",
        ),
        _ds("bank", "banks/{rssd_id}/", description="Latest snapshot for one bank.", tier="public"),
        _ds(
            "bank_trends",
            "banks/{rssd_id}/trends/",
            description="Quarterly time series of key metrics for one bank.",
            tier="public",
        ),
        _ds(
            "bank_deep_dive",
            "banks/{rssd_id}/deep-dive/",
            description=(
                "Full Call Report / UBPR detail for one bank (~150 fields, trailing periods)."
            ),
            tier="public",
        ),
        _ds(
            "bank_profile",
            "banks/{rssd_id}/profile/",
            description=(
                "Composite above-the-fold profile: snapshot + scorecard + ranks + ML prediction."
            ),
            tier="public",
        ),
        # Banks: financial sub-books
        _ds(
            "bank_hmda",
            "banks/{rssd_id}/hmda/",
            description=(
                "HMDA mortgage-lending book by year and state "
                "(applications, originations, denials, rates)."
            ),
            tier="free",
        ),
        _ds(
            "bank_deposits",
            "banks/{rssd_id}/deposits/",
            description="Deposit composition dashboard (brokered, CD, demand, savings breakdown).",
            tier="public",
        ),
        _ds(
            "bank_deposits_trends",
            "banks/{rssd_id}/deposits/trends/",
            description="Multi-quarter deposit composition trends.",
            tier="public",
        ),
        _ds(
            "bank_securities",
            "banks/{rssd_id}/securities/",
            description="Securities portfolio composition (HTM/AFS, OTTI, unrealized G/L).",
            tier="public",
        ),
        _ds(
            "bank_securities_trends",
            "banks/{rssd_id}/securities/trends/",
            description="Multi-quarter securities portfolio trends.",
            tier="public",
        ),
        _ds(
            "loan_portfolio",
            "banks/{rssd_id}/loan-portfolio/",
            description="Loan composition and concentration metrics (CRE, C&I, consumer, ag).",
            tier="public",
        ),
        # Banks: structure + org
        _ds(
            "bank_structure",
            "banks/{rssd_id}/structure/",
            description="Simplified organizational structure (parent, subsidiaries, ownership).",
            tier="public",
        ),
        _ds(
            "bank_structure_full",
            "banks/{rssd_id}/structure/full/",
            description="Full organizational structure with all subsidiaries and equity stakes.",
            tier="public",
        ),
        _ds(
            "bank_fry15",
            "banks/{rssd_id}/fry15/",
            description="FRY-15 G-SIB systemic-risk indicators.",
            tier="public",
        ),
        _ds(
            "bank_events",
            "banks/{rssd_id}/events/",
            description="NIC structural-event timeline for one bank (mergers, charters, closures).",
            tier="public",
        ),
        _ds(
            "bank_history",
            "banks/{rssd_id}/history/",
            description="FDIC institutional history change log.",
            tier="public",
        ),
        # Banks: scores + peers
        _ds(
            "scorecard",
            "banks/{rssd_id}/scorecard/",
            description="CAMELS-style component scores + letter grade.",
            tier="free",
        ),
        _ds(
            "scorecard_composite",
            "banks/{rssd_id}/scorecard/composite/",
            description="Composite scorecard with peer-comparison grades.",
            tier="free",
        ),
        _ds(
            "ranks",
            "banks/{rssd_id}/ranks/",
            description="Pre-computed percentile ranks within peer cohort.",
            tier="public",
        ),
        _ds(
            "peer_ranking",
            "banks/{rssd_id}/peer-ranking/",
            description="Percentile rank on one specific metric within the bank's peer group.",
            tier="public",
        ),
        _ds("peers", "banks/{rssd_id}/peers/", description="Peer set for one bank.", tier="public"),
        _ds(
            "peer_comparison",
            "banks/{rssd_id}/peers/comparison/",
            description="One bank vs. its peer-group benchmarks (~30 fields x 7 sections).",
            tier="public",
        ),
        # Banks: branches
        _ds(
            "branches",
            "banks/{rssd_id}/branches/",
            description="FDIC Summary-of-Deposits + NIC branch network combined.",
            tier="public",
        ),
        _ds(
            "branch_map",
            "banks/{rssd_id}/branches/map/",
            description="Branch locations optimized for map rendering (lat/lon + name).",
            tier="public",
        ),
        _ds(
            "branches_rollup",
            "banks/{rssd_id}/branches/rollup/",
            description="Aggregated branch statistics (count, state distribution, deposit share).",
            tier="public",
        ),
        _ds(
            "branches_fdic",
            "banks/{rssd_id}/branches/fdic/",
            description="FDIC Statement of Deposits branch data.",
            tier="public",
        ),
        _ds(
            "branches_nic",
            "banks/{rssd_id}/branches/nic/",
            description="Federal Reserve NIC organizational-structure branches.",
            tier="public",
        ),
        # Banks: risk + compliance
        _ds(
            "prediction",
            "banks/{rssd_id}/prediction/",
            description="ML 1-year failure probability + safety verdict.",
            tier="free",
        ),
        _ds(
            "prediction_detail",
            "banks/{rssd_id}/prediction/detail/",
            description="Detailed ML risk scoring with per-feature contribution breakdown.",
            tier="pro",
        ),
        _ds(
            "enforcement_actions",
            "banks/{rssd_id}/enforcement/",
            description="Regulatory enforcement actions against one bank (FDIC/OCC/FRB/CFPB).",
            tier="free",
        ),
        _ds(
            "complaints",
            "banks/{rssd_id}/complaints/",
            description="CFPB consumer complaints for one bank.",
            tier="free",
        ),
        # SEC / EDGAR
        _ds(
            "sec_filings",
            "banks/{rssd_id}/sec/filings/",
            description=(
                "SEC EDGAR filing index (10-K, 10-Q, 8-K, proxy) for one bank's registrant."
            ),
            tier="free",
        ),
        _ds(
            "sec_facts",
            "banks/{rssd_id}/sec/facts/",
            description="XBRL financial facts from SEC filings.",
            tier="free",
        ),
        _ds(
            "sec_8k",
            "banks/{rssd_id}/sec/8k/",
            description="8-K significant-event filings for one bank.",
            tier="free",
        ),
        _ds(
            "sec_8k_body",
            "sec/8k/{accession_number}/",
            description="Full 8-K body text by accession number (cross-bank lookup).",
            tier="free",
        ),
        _ds(
            "sec_ma_events",
            "banks/{rssd_id}/sec/ma/",
            description="M&A events extracted from SEC filings.",
            tier="free",
        ),
        _ds(
            "sec_officers",
            "banks/{rssd_id}/sec/officers/",
            description="Executive officers and board members from SEC filings.",
            tier="free",
        ),
        _ds(
            "executive_comp",
            "banks/{rssd_id}/sec/executive-comp/",
            description="Named-executive-officer (NEO) compensation from proxy statements.",
            tier="free",
        ),
        _ds(
            "sec_restatements",
            "banks/{rssd_id}/sec/restatements/",
            description="Financial restatements identified in SEC filings.",
            tier="free",
        ),
        _ds(
            "sec_insider_txns",
            "banks/{rssd_id}/sec/insider-txns/",
            description="Insider Form-4 buy/sell transactions.",
            tier="free",
        ),
        _ds(
            "sec_insider_summary",
            "banks/{rssd_id}/sec/insider-summary/",
            description="Aggregated insider trading activity summary.",
            tier="free",
        ),
        _ds(
            "sec_filing_velocity",
            "banks/{rssd_id}/sec/velocity/",
            description="Filing frequency and velocity analysis.",
            tier="free",
        ),
        _ds(
            "sec_filer_history",
            "banks/{rssd_id}/sec/filer-history/",
            description="Historical SEC filer metadata for a bank's registrant.",
            tier="free",
        ),
        _ds(
            "sec_filing_sections",
            "banks/{rssd_id}/sec/sections/",
            description="Parsed text sections (MD&A, risk factors, etc.) from SEC filings.",
            tier="free",
        ),
        _ds(
            "sec_13dg",
            "banks/{rssd_id}/sec/13dg/",
            description="Beneficial ownership disclosures (Schedule 13D/G).",
            tier="free",
        ),
        _ds(
            "sec_13f",
            "banks/{rssd_id}/sec/13f/",
            description="Institutional holdings (Form 13F) for one bank.",
            tier="free",
        ),
        # Screeners
        _ds(
            "screener",
            "screener/",
            description="Every FDIC-insured bank with key ratios; filter and sort.",
            tier="public",
        ),
        _ds(
            "screener_dataset",
            "screener/dataset/",
            description="Complete screener dataset for fast client-side filtering.",
            tier="pro",
        ),
        _ds(
            "ma_screener",
            "screener/ma/",
            description="Banks scored as likely M&A targets (MA score + valuation metrics).",
            tier="public",
        ),
        _ds(
            "loan_concentration",
            "screener/loan-concentration/",
            description=(
                "Screen banks by loan-portfolio concentration (CRE, construction, C&I, ag)."
            ),
            tier="public",
        ),
        _ds(
            "screener_growth",
            "screener/growth/",
            description="Growth metrics screener (loan growth, deposit growth, asset growth).",
            tier="public",
        ),
        # Industry / macro
        _ds(
            "industry",
            "industry/",
            description="Industry-wide aggregate snapshot (latest quarter).",
            tier="public",
        ),
        _ds(
            "industry_trends",
            "industry/trends/",
            description="Industry-wide metric trends over time.",
            tier="public",
        ),
        _ds(
            "rate_environment",
            "industry/rate-environment/",
            description="Current rate environment snapshot (mortgage, consumer, deposit rates).",
            tier="public",
        ),
        _ds(
            "rate_environment_trends",
            "industry/rate-environment/trends/",
            description="Rate environment historical trends.",
            tier="public",
        ),
        _ds(
            "industry_securities_trends",
            "industry/securities/trends/",
            description="Industry-wide securities portfolio trends.",
            tier="public",
        ),
        _ds(
            "national_deposit_rates",
            "industry/deposit-rates/",
            description="National deposit rates by product type (savings, MMA, CD).",
            tier="public",
        ),
        _ds(
            "peer_groups",
            "industry/peer-groups/",
            description="Standard peer-group definitions and rollups.",
            tier="public",
        ),
        _ds(
            "industry_benchmarks",
            "industry/benchmarks/",
            description="Pre-computed peer benchmarks: mean + 5-number summary by peer group.",
            tier="public",
        ),
        _ds(
            "industry_macro",
            "industry/macro/",
            description="Macro context series (rates, yield curves, credit spreads, VIX).",
            tier="public",
        ),
        _ds(
            "industry_dashboard",
            "industry/dashboard/",
            description="Pre-computed industry dashboard data.",
            tier="public",
        ),
        # Rates
        _ds(
            "rates_mortgage",
            "rates/mortgage/",
            description="Freddie Mac mortgage rate series (30yr, 15yr fixed).",
            tier="public",
        ),
        _ds(
            "rates_consumer",
            "rates/consumer/",
            description="Consumer loan rate series.",
            tier="public",
        ),
        _ds(
            "rates_deposit",
            "rates/deposit/",
            description="Deposit rate series.",
            tier="public",
        ),
        _ds(
            "rates_treasury_bill",
            "rates/treasury-bill/",
            description="Treasury-bill rate series.",
            tier="public",
        ),
        _ds(
            "rates_real_yield",
            "rates/real-yield/",
            description="Real yield (inflation-adjusted Treasury) series.",
            tier="public",
        ),
        _ds(
            "rates_national",
            "rates/national-rates/",
            description="FDIC national deposit rate benchmarks.",
            tier="public",
        ),
        _ds(
            "yield_curve",
            "rates/yield-curve/",
            description="US Treasury yield curve (long form; ``?latest=1`` for current snapshot).",
            tier="public",
        ),
        # Risk
        _ds(
            "high_risk_banks",
            "risk/high-risk-banks/",
            description="Banks flagged high-risk by the failure-prediction model.",
            tier="free",
        ),
        _ds(
            "risk_overview",
            "risk/overview/",
            description="Industry risk metrics overview.",
            tier="free",
        ),
        _ds(
            "risk_dashboard",
            "risk/dashboard/",
            description="Risk dashboard pre-computed data.",
            tier="free",
        ),
        _ds(
            "risk_model",
            "risk/model/",
            description="ML failure-prediction model performance metrics and calibration.",
            tier="free",
        ),
        # Failures
        _ds(
            "failures_recent",
            "failures/recent/",
            description="Most recent bank failures (FDIC failure feed).",
            tier="public",
        ),
        _ds(
            "failures_by_year",
            "failures/by-year/",
            description="Bank failure counts and total assets by year.",
            tier="public",
        ),
        _ds(
            "failures_dashboard",
            "failures/dashboard/",
            description="Bank failures dashboard pre-computed data.",
            tier="public",
        ),
        # Events / enforcement
        _ds(
            "events",
            "events/",
            description="Industry-wide structural events feed (M&A, charters, closures).",
            tier="public",
        ),
        _ds(
            "enforcement",
            "enforcement/",
            description="Industry-wide enforcement-action press feed (FDIC/OCC/FRB/CFPB).",
            tier="public",
        ),
        # Credit unions (NCUA)
        _ds(
            "credit_unions",
            "cus/",
            description="Directory of NCUA-insured credit unions.",
            tier="public",
        ),
        _ds(
            "credit_union",
            "cus/{cu_number}/",
            description="Latest snapshot for one credit union.",
            tier="public",
        ),
        _ds(
            "credit_union_trends",
            "cus/{cu_number}/trends/",
            description="Quarterly metric time series for one credit union.",
            tier="public",
        ),
        _ds(
            "cu_branches",
            "cus/{cu_number}/branches/",
            description="Branch locations for one credit union.",
            tier="public",
        ),
        _ds(
            "cu_atms",
            "cus/{cu_number}/atms/",
            description="ATM locations for one credit union.",
            tier="public",
        ),
        _ds(
            "cu_peer_benchmarks",
            "cus/peer-benchmarks/",
            description="Credit union peer benchmarks by asset tier.",
            tier="public",
        ),
        _ds(
            "ncua_enforcement",
            "enforcement/ncua/",
            description="NCUA enforcement actions.",
            tier="public",
        ),
        # Holding companies (BHC / Y-9C)
        _ds(
            "holding_companies",
            "holding-cos/",
            description="Directory of bank holding companies.",
            tier="public",
        ),
        _ds(
            "holding_company",
            "holding-cos/{rssd_id}/",
            description="Latest snapshot for one holding company.",
            tier="public",
        ),
        _ds(
            "holding_company_trends",
            "holding-cos/{rssd_id}/trends/",
            description="Quarterly trends for one holding company.",
            tier="public",
        ),
        # Custom peer groups
        _ds(
            "custom_peer_groups",
            "peer-groups/custom/",
            description="User's custom peer groups (Pro tier; per-account).",
            tier="pro",
        ),
        _ds(
            "custom_peer_group_detail",
            "peer-groups/custom/{group_id}/",
            description="Single custom peer group with full peer-comparison data.",
            tier="pro",
        ),
        # Watchlist + alerts (GET views; mutable ops are class methods)
        _ds(
            "watchlist",
            "watchlist/",
            description="User's watchlist of tracked banks.",
            tier="free",
        ),
        _ds(
            "alerts",
            "watchlist/alerts/",
            description="User's threshold alerts.",
            tier="free",
        ),
        # Saved graphs (GET views)
        _ds(
            "graphs",
            "graphs/",
            description="List of saved analytical graphs.",
            tier="free",
        ),
        _ds(
            "graph_detail",
            "graphs/{graph_id}/",
            description="Single saved graph by id.",
            tier="free",
        ),
        # Catalog + reference
        _ds(
            "metrics",
            "metrics/",
            description="Metric catalog: every available metric with units + definition.",
            tier="public",
        ),
        _ds(
            "reference_index",
            "reference/",
            description="Index of all available reference table slugs.",
            tier="public",
        ),
        _ds(
            "reference",
            "reference/{slug}/",
            description="Reference lookup table by slug (states, regulators, charter classes, …).",
            tier="public",
        ),
        _ds(
            "reference_mdrm",
            "reference/mdrm/{code}/",
            description=(
                "MDRM item definition: name, description, forms, XBRL tag, "
                "UBPR formula, validity window."
            ),
            tier="public",
        ),
        _ds(
            "datasets_catalog",
            "datasets/",
            description="Public data-product catalog grouped by category.",
            tier="public",
        ),
        _ds(
            "dataset_detail",
            "datasets/{key}/",
            description="Single dataset metadata (path, parameters, SDK method, tier).",
            tier="public",
        ),
        _ds(
            "healthz",
            "healthz/",
            description="API health probe: status, latest data quarter, and uptime.",
            tier="public",
        ),
    ]
)


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #


class bankreg:
    """Client for the BankRegReports Data API.

    Args:
        token: Your API key (``brr_…``). Falls back to the ``BANKREG_API_TOKEN``
            environment variable. Public-tier datasets work without a token.
        base_url: API origin. Falls back to ``BANKREG_API_BASE_URL`` then the
            production host. Point this at ``http://localhost:8000`` for local dev.
        api_version: URL-path version segment (default ``"v1"``).
        timeout: Per-request timeout in seconds (default 30).
        max_retries: Automatic retries with exponential backoff on 429 / 5xx
            (default 3). Mutable operations (POST/PATCH/DELETE) are not retried.
        session: Optional pre-configured ``requests.Session`` for connection reuse.

    Example:
        >>> brr = bankreg("brr_xxx")
        >>> brr.bank(852218)                        # JPMorgan Chase Bank, N.A.
        >>> brr.screener(state="GA", min_assets=500_000)
        >>> brr.sec_insider_txns(852218)
        >>> for page in brr.pages("screener", page_size=200):
        ...     process(page)
    """

    def __init__(
        self,
        token: Optional[str] = None,
        *,
        base_url: Optional[str] = None,
        api_version: str = DEFAULT_API_VERSION,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        session: Optional[requests.Session] = None,
    ) -> None:
        self.token = token or os.environ.get(ENV_TOKEN) or ""
        self.base_url = (base_url or os.environ.get(ENV_BASE_URL) or DEFAULT_BASE_URL).rstrip("/")
        self.api_version = api_version.strip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self._session = session or requests.Session()

    # internals #

    @property
    def _headers(self) -> dict[str, str]:
        h = {"Accept": "application/json", "User-Agent": _USER_AGENT}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    @property
    def _json_headers(self) -> dict[str, str]:
        return {**self._headers, "Content-Type": "application/json"}

    def _url(self, path: str) -> str:
        return f"{self.base_url}/api/{self.api_version}/{path.lstrip('/')}"

    def _raise_for_status(self, resp: requests.Response) -> None:
        if resp.status_code < 400:
            return
        code = message = request_id = None
        try:
            body = resp.json()
            err = body.get("error", body) if isinstance(body, dict) else {}
            code = err.get("code")
            message = err.get("message") or err.get("detail")
            request_id = err.get("request_id")
        except ValueError:
            message = (resp.text or "").strip()[:300] or None
        message = message or f"HTTP {resp.status_code}"
        kw: dict[str, Any] = {
            "status_code": resp.status_code,
            "code": code,
            "request_id": request_id,
        }
        if resp.status_code == 401:
            raise AuthenticationError(message, **kw)
        if resp.status_code == 403:
            raise UpgradeRequiredError(message, **kw)
        if resp.status_code == 404:
            raise NotFoundError(message, **kw)
        if resp.status_code == 422:
            raise ValidationError(message, **kw)
        if resp.status_code == 429:
            raise RateLimitError(
                message,
                retry_after=_parse_retry_after(resp.headers.get("Retry-After")),
                **kw,
            )
        if resp.status_code >= 500:
            raise ServerError(message, **kw)
        raise BankRegError(message, **kw)

    @staticmethod
    def _clean_params(params: Optional[Mapping[str, Any]]) -> dict[str, Any]:
        return {k: v for k, v in (params or {}).items() if v is not None and v != "" and v != []}

    def _get_json(self, path: str, params: Optional[Mapping[str, Any]] = None) -> Any:
        """GET with exponential-backoff retry on 429 / 5xx."""
        clean = self._clean_params(params)
        url = self._url(path)
        last_exc: Optional[BankRegError] = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = self._session.get(
                    url, headers=self._headers, params=clean, timeout=self.timeout
                )
            except requests.RequestException as exc:
                last_exc = _transport_error(url, exc)
                self._sleep_backoff(attempt)
                continue
            try:
                self._raise_for_status(resp)
            except (RateLimitError, ServerError) as exc:
                last_exc = exc
                self._sleep_backoff(attempt, override=getattr(exc, "retry_after", None))
                continue
            return resp.json()
        # The loop always assigns last_exc before exhausting; this fallback keeps
        # us from ever raising None (e.g. under `python -O`, or max_retries < 0).
        raise last_exc or ServerError(f"Request to {url} failed after retries")

    def _write(
        self,
        verb: str,
        path: str,
        body: Any = None,
        *,
        params: Optional[Mapping[str, Any]] = None,
    ) -> Any:
        """Single-attempt write. Mutations are never retried, so a timeout leaves
        the outcome genuinely unknown rather than silently duplicating a POST."""
        url = self._url(path)
        clean = self._clean_params(params)
        kwargs: dict[str, Any] = {
            "headers": self._headers,
            "params": clean or None,
            "timeout": self.timeout,
        }
        if verb != "delete":
            kwargs["headers"] = self._json_headers
            kwargs["json"] = body
        try:
            resp = getattr(self._session, verb)(url, **kwargs)
        except requests.RequestException as exc:
            raise _transport_error(url, exc) from exc
        self._raise_for_status(resp)
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    def _post_json(
        self, path: str, body: Any = None, *, params: Optional[Mapping[str, Any]] = None
    ) -> Any:
        return self._write("post", path, body, params=params)

    def _patch_json(
        self, path: str, body: Any = None, *, params: Optional[Mapping[str, Any]] = None
    ) -> Any:
        return self._write("patch", path, body, params=params)

    def _put_json(
        self, path: str, body: Any = None, *, params: Optional[Mapping[str, Any]] = None
    ) -> Any:
        return self._write("put", path, body, params=params)

    def _delete(self, path: str, *, params: Optional[Mapping[str, Any]] = None) -> bool:
        """DELETE — one attempt. Returns True on success."""
        self._write("delete", path, params=params)
        return True

    def _sleep_backoff(self, attempt: int, *, override: Optional[float] = None) -> None:
        # override (a server Retry-After) is honored but capped, so a broken or
        # hostile header can't hang the client for an unbounded stretch.
        if attempt >= self.max_retries:
            return
        delay = override if override is not None else 2.0**attempt
        time.sleep(min(delay, DEFAULT_MAX_RETRY_SLEEP))

    @staticmethod
    def _unwrap(body: Any, records: Optional[str]) -> tuple[Any, dict]:
        """Split ``{"data": …, "meta": …}`` into (rows, meta)."""
        meta: dict = {}
        data = body
        if isinstance(body, dict) and "data" in body:
            meta = body.get("meta") or {}
            data = body["data"]
        if records and isinstance(data, dict):
            data = data.get(records, [])
        return data, meta

    def _to_frame(
        self,
        body: Any,
        *,
        date_cols: Sequence[str],
        records: Optional[str],
        as_dataframe: bool,
    ) -> Any:
        rows, meta = self._unwrap(body, records)
        if not as_dataframe:
            return rows
        pd = _require_pandas()
        if isinstance(rows, dict):
            rows = [rows]
        df = pd.DataFrame(rows if rows is not None else [])
        cols = set(self._all_date_cols(date_cols))
        for col in df.columns:
            if col in cols or col.lower() in cols:
                df[col] = pd.to_datetime(df[col], errors="coerce")
        df.attrs["meta"] = meta
        return df

    @staticmethod
    def _all_date_cols(extra: Sequence[str]) -> tuple[str, ...]:
        return tuple(dict.fromkeys((*_COMMON_DATE_COLS, *extra)))

    # generic, registry-driven access #

    def dataset(self, name: str, *, as_dataframe: bool = True, **kwargs: Any) -> Any:
        """Fetch any registered dataset by name.

        Path placeholders (e.g. ``rssd_id``) are pulled from ``kwargs``; the
        remainder become query-string params. This is the forward-compatible
        escape hatch — a new dataset added to ``DATASETS`` is reachable here
        without a client release.

        >>> brr.dataset("bank", rssd_id=852218)
        >>> brr.dataset("screener", state="GA", min_assets=1_000_000)
        """
        try:
            spec = DATASETS[name]
        except KeyError:
            raise BankRegError(
                f"Unknown dataset '{name}'. Use .list_datasets() to see available datasets."
            ) from None

        path_args: dict[str, Any] = {}
        query: dict[str, Any] = {}
        for key, value in kwargs.items():
            (path_args if key in spec.path_params else query)[key] = value

        missing = [p for p in spec.path_params if p not in path_args]
        if missing:
            raise BankRegError(f"Dataset '{name}' requires path argument(s): {', '.join(missing)}")

        path = spec.path.format(**path_args)
        body = self._get_json(path, query)
        return self._to_frame(
            body,
            date_cols=spec.date_cols,
            records=spec.records,
            as_dataframe=as_dataframe,
        )

    def list_datasets(self, *, as_dataframe: bool = True) -> Any:
        """Return the dataset catalog (name, path, tier, description)."""
        rows = [
            {
                "name": d.name,
                "path": f"/api/{self.api_version}/{d.path}",
                "tier": d.tier,
                "description": d.description,
                "path_params": list(d.path_params),
            }
            for d in DATASETS.values()
        ]
        if not as_dataframe:
            return rows
        return _require_pandas().DataFrame(rows)

    def pages(
        self,
        name: str,
        *,
        page_size: int = DEFAULT_PAGE_SIZE,
        max_records: Optional[int] = None,
        as_dataframe: bool = True,
        **kwargs: Any,
    ) -> Iterator[Any]:
        """Iterate over pages of a paginated dataset, yielding one frame per page.

        Stops when the server returns fewer rows than ``page_size`` (final page)
        or when ``max_records`` total rows have been collected.

        Example:
            >>> for page in brr.pages("screener", page_size=200, state="CA"):
            ...     process(page)
        """
        offset = int(kwargs.pop("offset", 0))
        collected = 0
        while True:
            limit = page_size
            if max_records is not None:
                remaining = max_records - collected
                if remaining <= 0:
                    break
                limit = min(page_size, remaining)
            result = self.dataset(
                name, limit=limit, offset=offset, as_dataframe=as_dataframe, **kwargs
            )
            if as_dataframe:
                count = len(result)
                if max_records is not None and collected + count > max_records:
                    result = result.iloc[: max_records - collected]
                    count = len(result)
            elif isinstance(result, list):
                count = len(result)
                if max_records is not None and collected + count > max_records:
                    result = result[: max_records - collected]
                    count = len(result)
            else:
                count = 1
            yield result
            collected += count
            if count < limit:
                break
            offset += count

    # banks: directory + snapshot #

    def banks(
        self,
        *,
        state: str = "",
        tier: str = "",
        q: str = "",
        sort: str = "",
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        as_dataframe: bool = True,
    ) -> Any:
        """Directory of FDIC-insured banks. Filter by ``state``, asset ``tier``,
        or name search ``q``."""
        return self.dataset(
            "banks",
            state=state,
            tier=tier,
            q=q,
            sort=sort,
            limit=limit,
            offset=offset,
            as_dataframe=as_dataframe,
        )

    def bank_search_index(self, *, as_dataframe: bool = True) -> Any:
        """Lightweight bank search index for client-side filtering.

        Returns every bank's rssd_id, legal name, city, state, and total assets
        in a single cached payload — suitable for autocomplete or local search.
        """
        return self.dataset("bank_search_index", as_dataframe=as_dataframe)

    def bank(self, rssd_id: int, *, as_dataframe: bool = True) -> Any:
        """Latest snapshot for one bank, keyed by Federal Reserve RSSD id."""
        return self.dataset("bank", rssd_id=rssd_id, as_dataframe=as_dataframe)

    def bank_trends(self, rssd_id: int, *, quarters: int = 12, as_dataframe: bool = True) -> Any:
        """Quarterly metric time series for one bank (default last 12 quarters)."""
        return self.dataset(
            "bank_trends", rssd_id=rssd_id, quarters=quarters, as_dataframe=as_dataframe
        )

    def bank_deep_dive(self, rssd_id: int, *, periods: int = 5, as_dataframe: bool = True) -> Any:
        """Full Call Report / UBPR detail for one bank (~150 fields, trailing periods)."""
        return self.dataset(
            "bank_deep_dive", rssd_id=rssd_id, periods=periods, as_dataframe=as_dataframe
        )

    def bank_profile(self, rssd_id: int, *, as_dataframe: bool = True) -> Any:
        """Composite above-the-fold profile: snapshot + scorecard + ranks + ML prediction."""
        return self.dataset("bank_profile", rssd_id=rssd_id, as_dataframe=as_dataframe)

    # banks: financial sub-books #

    def bank_hmda(self, rssd_id: int, *, as_dataframe: bool = True) -> Any:
        """HMDA mortgage-lending book by year and state (applications, originations,
        denials, rate spreads)."""
        return self.dataset("bank_hmda", rssd_id=rssd_id, as_dataframe=as_dataframe)

    def bank_deposits(
        self,
        rssd_id: int,
        *,
        trends: bool = False,
        quarters: int = 12,
        as_dataframe: bool = True,
    ) -> Any:
        """Deposit composition. ``trends=True`` returns the multi-quarter series."""
        name = "bank_deposits_trends" if trends else "bank_deposits"
        kw = {"quarters": quarters} if trends else {}
        return self.dataset(name, rssd_id=rssd_id, as_dataframe=as_dataframe, **kw)

    def bank_securities(
        self,
        rssd_id: int,
        *,
        trends: bool = False,
        quarters: int = 12,
        as_dataframe: bool = True,
    ) -> Any:
        """Securities portfolio. ``trends=True`` returns the multi-quarter series."""
        name = "bank_securities_trends" if trends else "bank_securities"
        kw = {"quarters": quarters} if trends else {}
        return self.dataset(name, rssd_id=rssd_id, as_dataframe=as_dataframe, **kw)

    def loan_portfolio(self, rssd_id: int, *, as_dataframe: bool = True) -> Any:
        """Loan composition and concentration metrics (CRE, C&I, consumer, ag)."""
        return self.dataset("loan_portfolio", rssd_id=rssd_id, as_dataframe=as_dataframe)

    # banks: structure + org #

    def bank_structure(self, rssd_id: int, *, full: bool = False, as_dataframe: bool = True) -> Any:
        """Organizational structure. ``full=True`` includes all subsidiaries and
        equity stakes."""
        name = "bank_structure_full" if full else "bank_structure"
        return self.dataset(name, rssd_id=rssd_id, as_dataframe=as_dataframe)

    def bank_fry15(self, rssd_id: int, *, as_dataframe: bool = True) -> Any:
        """FRY-15 G-SIB systemic-risk indicators for one bank."""
        return self.dataset("bank_fry15", rssd_id=rssd_id, as_dataframe=as_dataframe)

    def bank_events(
        self,
        rssd_id: int,
        *,
        limit: Optional[int] = None,
        as_dataframe: bool = True,
    ) -> Any:
        """NIC structural-event timeline for one bank (mergers, charter changes,
        openings, closures)."""
        return self.dataset("bank_events", rssd_id=rssd_id, limit=limit, as_dataframe=as_dataframe)

    def bank_history(
        self,
        rssd_id: int,
        *,
        limit: Optional[int] = None,
        as_dataframe: bool = True,
    ) -> Any:
        """FDIC institutional history change log for one bank."""
        return self.dataset("bank_history", rssd_id=rssd_id, limit=limit, as_dataframe=as_dataframe)

    def bank_compare(
        self,
        rssd_ids: list[int],
        *,
        as_dataframe: bool = True,
    ) -> Any:
        """Side-by-side comparison of multiple banks.

        Args:
            rssd_ids: List of Federal Reserve RSSD IDs to compare (2-10 banks).

        Example:
            >>> brr.bank_compare([852218, 480228, 37])   # JPM, BofA, Citibank
        """
        body = self._get_json("banks/compare/", {"rssd_ids": rssd_ids})
        return self._to_frame(body, date_cols=(), records=None, as_dataframe=as_dataframe)

    # banks: scores + peers #

    def scorecard(
        self,
        rssd_id: int,
        *,
        composite: bool = False,
        as_dataframe: bool = True,
    ) -> Any:
        """CAMELS-style component scores + letter grade. ``composite=True`` adds
        peer-comparison grades."""
        name = "scorecard_composite" if composite else "scorecard"
        return self.dataset(name, rssd_id=rssd_id, as_dataframe=as_dataframe)

    def ranks(self, rssd_id: int, *, as_dataframe: bool = True) -> Any:
        """Pre-computed percentile ranks for one bank within its peer cohort."""
        return self.dataset("ranks", rssd_id=rssd_id, as_dataframe=as_dataframe)

    def peer_ranking(
        self,
        rssd_id: int,
        metric: str,
        *,
        as_dataframe: bool = True,
    ) -> Any:
        """Percentile rank of one bank on a specific ``metric`` within its peer group.

        Args:
            metric: Metric code, e.g. ``"cet1_ratio"``, ``"roa"``, ``"npl_ratio"``.
        """
        return self.dataset(
            "peer_ranking", rssd_id=rssd_id, metric=metric, as_dataframe=as_dataframe
        )

    def peers(self, rssd_id: int, *, as_dataframe: bool = True) -> Any:
        """Peer set for one bank."""
        return self.dataset("peers", rssd_id=rssd_id, as_dataframe=as_dataframe)

    def peer_comparison(self, rssd_id: int, *, as_dataframe: bool = True) -> Any:
        """One bank vs. its peer-group benchmarks (~30 fields x 7 sections)."""
        return self.dataset("peer_comparison", rssd_id=rssd_id, as_dataframe=as_dataframe)

    # banks: branches #

    def branches(
        self,
        rssd_id: int,
        *,
        source: Optional[str] = None,
        rollup: bool = False,
        as_map: bool = False,
        as_dataframe: bool = True,
    ) -> Any:
        """Branch data for one bank.

        Args:
            source: ``"fdic"`` for FDIC SoD data only; ``"nic"`` for FR NIC only;
                ``None`` (default) for the combined view.
            rollup: Return aggregated statistics instead of individual branches.
            as_map: Return lat/lon-optimized payload for map rendering.
        """
        if rollup:
            return self.dataset("branches_rollup", rssd_id=rssd_id, as_dataframe=as_dataframe)
        if as_map:
            return self.dataset("branch_map", rssd_id=rssd_id, as_dataframe=as_dataframe)
        if source == "fdic":
            return self.dataset("branches_fdic", rssd_id=rssd_id, as_dataframe=as_dataframe)
        if source == "nic":
            return self.dataset("branches_nic", rssd_id=rssd_id, as_dataframe=as_dataframe)
        return self.dataset("branches", rssd_id=rssd_id, as_dataframe=as_dataframe)

    # banks: risk + compliance #

    def prediction(
        self,
        rssd_id: int,
        *,
        detail: bool = False,
        as_dataframe: bool = True,
    ) -> Any:
        """ML 1-year failure probability + safety verdict. ``detail=True`` adds
        per-feature contribution breakdown."""
        name = "prediction_detail" if detail else "prediction"
        return self.dataset(name, rssd_id=rssd_id, as_dataframe=as_dataframe)

    def enforcement_actions(self, rssd_id: int, *, as_dataframe: bool = True) -> Any:
        """Regulatory enforcement actions against one bank."""
        return self.dataset("enforcement_actions", rssd_id=rssd_id, as_dataframe=as_dataframe)

    def complaints(self, rssd_id: int, *, as_dataframe: bool = True) -> Any:
        """CFPB consumer complaints for one bank."""
        return self.dataset("complaints", rssd_id=rssd_id, as_dataframe=as_dataframe)

    # SEC / EDGAR #

    def sec_filings(
        self,
        rssd_id: int,
        *,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        as_dataframe: bool = True,
    ) -> Any:
        """SEC EDGAR filing index (10-K, 10-Q, 8-K, proxy) for one bank's registrant."""
        return self.dataset(
            "sec_filings",
            rssd_id=rssd_id,
            limit=limit,
            offset=offset,
            as_dataframe=as_dataframe,
        )

    def sec_facts(
        self,
        rssd_id: int,
        *,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        as_dataframe: bool = True,
    ) -> Any:
        """XBRL financial facts from SEC filings for one bank."""
        return self.dataset(
            "sec_facts",
            rssd_id=rssd_id,
            limit=limit,
            offset=offset,
            as_dataframe=as_dataframe,
        )

    def sec_8k(
        self,
        rssd_id: int,
        *,
        limit: Optional[int] = None,
        as_dataframe: bool = True,
    ) -> Any:
        """8-K significant-event filings for one bank."""
        return self.dataset("sec_8k", rssd_id=rssd_id, limit=limit, as_dataframe=as_dataframe)

    def sec_8k_body(self, accession_number: str, *, as_dataframe: bool = True) -> Any:
        """Full 8-K body text looked up by accession number (cross-bank)."""
        return self.dataset(
            "sec_8k_body",
            accession_number=accession_number,
            as_dataframe=as_dataframe,
        )

    def sec_ma(self, rssd_id: int, *, as_dataframe: bool = True) -> Any:
        """M&A events extracted from SEC filings for one bank."""
        return self.dataset("sec_ma_events", rssd_id=rssd_id, as_dataframe=as_dataframe)

    def sec_officers(self, rssd_id: int, *, as_dataframe: bool = True) -> Any:
        """Executive officers and board members from SEC filings."""
        return self.dataset("sec_officers", rssd_id=rssd_id, as_dataframe=as_dataframe)

    def executive_comp(self, rssd_id: int, *, as_dataframe: bool = True) -> Any:
        """Named-executive-officer (NEO) compensation from proxy statements."""
        return self.dataset("executive_comp", rssd_id=rssd_id, as_dataframe=as_dataframe)

    def sec_restatements(self, rssd_id: int, *, as_dataframe: bool = True) -> Any:
        """Financial restatements identified in SEC filings."""
        return self.dataset("sec_restatements", rssd_id=rssd_id, as_dataframe=as_dataframe)

    def sec_insider_txns(
        self,
        rssd_id: int,
        *,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        as_dataframe: bool = True,
    ) -> Any:
        """Insider Form-4 buy/sell transactions for one bank."""
        return self.dataset(
            "sec_insider_txns",
            rssd_id=rssd_id,
            limit=limit,
            offset=offset,
            as_dataframe=as_dataframe,
        )

    def sec_insider_summary(self, rssd_id: int, *, as_dataframe: bool = True) -> Any:
        """Aggregated insider trading activity summary for one bank."""
        return self.dataset("sec_insider_summary", rssd_id=rssd_id, as_dataframe=as_dataframe)

    def sec_filing_velocity(self, rssd_id: int, *, as_dataframe: bool = True) -> Any:
        """Filing frequency and velocity analysis for one bank's registrant."""
        return self.dataset("sec_filing_velocity", rssd_id=rssd_id, as_dataframe=as_dataframe)

    def sec_filer_history(self, rssd_id: int, *, as_dataframe: bool = True) -> Any:
        """Historical SEC filer metadata for a bank's registrant."""
        return self.dataset("sec_filer_history", rssd_id=rssd_id, as_dataframe=as_dataframe)

    def sec_filing_sections(self, rssd_id: int, *, as_dataframe: bool = True) -> Any:
        """Parsed text sections (MD&A, risk factors, etc.) from SEC filings."""
        return self.dataset("sec_filing_sections", rssd_id=rssd_id, as_dataframe=as_dataframe)

    def sec_13dg(self, rssd_id: int, *, as_dataframe: bool = True) -> Any:
        """Beneficial ownership disclosures (Schedule 13D/G) for one bank."""
        return self.dataset("sec_13dg", rssd_id=rssd_id, as_dataframe=as_dataframe)

    def sec_13f(self, rssd_id: int, *, as_dataframe: bool = True) -> Any:
        """Institutional holdings (Form 13F) for one bank."""
        return self.dataset("sec_13f", rssd_id=rssd_id, as_dataframe=as_dataframe)

    # screeners #

    def screener(
        self,
        *,
        state: str = "",
        tier: str = "",
        sort: str = "",
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        as_dataframe: bool = True,
        **filters: Any,
    ) -> Any:
        """Every FDIC-insured bank with key ratios. Extra ``**filters`` pass
        through as query params (e.g. ``min_roa=1.0``), so new screener filters
        work without a client upgrade."""
        return self.dataset(
            "screener",
            state=state,
            tier=tier,
            sort=sort,
            limit=limit,
            offset=offset,
            as_dataframe=as_dataframe,
            **filters,
        )

    def screener_dataset(self, *, as_dataframe: bool = True) -> Any:
        """Complete screener dataset for fast client-side filtering."""
        return self.dataset("screener_dataset", as_dataframe=as_dataframe)

    def ma_screener(self, *, as_dataframe: bool = True, **filters: Any) -> Any:
        """Banks scored as likely M&A targets (MA score + valuation metrics)."""
        return self.dataset("ma_screener", as_dataframe=as_dataframe, **filters)

    def loan_concentration(self, *, as_dataframe: bool = True, **filters: Any) -> Any:
        """Screen banks by loan-portfolio concentration."""
        return self.dataset("loan_concentration", as_dataframe=as_dataframe, **filters)

    def growth_screener(self, *, as_dataframe: bool = True, **filters: Any) -> Any:
        """Growth metrics screener (loan growth, deposit growth, asset growth)."""
        return self.dataset("screener_growth", as_dataframe=as_dataframe, **filters)

    # industry / macro #

    def industry(
        self,
        *,
        trends: bool = False,
        quarters: int = 20,
        as_dataframe: bool = True,
        **filters: Any,
    ) -> Any:
        """Industry-wide aggregates. ``trends=True`` returns the time series."""
        name = "industry_trends" if trends else "industry"
        kw = {"quarters": quarters} if trends else {}
        return self.dataset(name, as_dataframe=as_dataframe, **kw, **filters)

    def rate_environment(
        self,
        *,
        trends: bool = False,
        quarters: Optional[int] = None,
        as_dataframe: bool = True,
    ) -> Any:
        """Rate environment snapshot. ``trends=True`` returns the historical series."""
        name = "rate_environment_trends" if trends else "rate_environment"
        kw = {"quarters": quarters} if (trends and quarters) else {}
        return self.dataset(name, as_dataframe=as_dataframe, **kw)

    def national_deposit_rates(self, *, as_dataframe: bool = True) -> Any:
        """National deposit rates by product type (savings, MMA, CD)."""
        return self.dataset("national_deposit_rates", as_dataframe=as_dataframe)

    def industry_benchmarks(self, *, as_dataframe: bool = True) -> Any:
        """Pre-computed peer benchmarks: mean + 5-number summary by peer group."""
        return self.dataset("industry_benchmarks", as_dataframe=as_dataframe)

    def industry_macro(self, *, as_dataframe: bool = True) -> Any:
        """Macro context series (rates, yield curves, credit spreads, VIX)."""
        return self.dataset("industry_macro", as_dataframe=as_dataframe)

    def industry_dashboard(self, *, as_dataframe: bool = True) -> Any:
        """Pre-computed industry dashboard data."""
        return self.dataset("industry_dashboard", as_dataframe=as_dataframe)

    def industry_securities_trends(
        self, *, quarters: Optional[int] = None, as_dataframe: bool = True
    ) -> Any:
        """Industry-wide securities portfolio trends."""
        kw = {"quarters": quarters} if quarters else {}
        return self.dataset("industry_securities_trends", as_dataframe=as_dataframe, **kw)

    def peer_groups(self, *, as_dataframe: bool = True, **filters: Any) -> Any:
        """Standard peer-group definitions and rollups."""
        return self.dataset("peer_groups", as_dataframe=as_dataframe, **filters)

    # risk / failures #

    def high_risk_banks(self, *, as_dataframe: bool = True, **filters: Any) -> Any:
        """Banks flagged high-risk by the failure-prediction model."""
        return self.dataset("high_risk_banks", as_dataframe=as_dataframe, **filters)

    def risk(
        self,
        *,
        dashboard: bool = False,
        model: bool = False,
        as_dataframe: bool = True,
    ) -> Any:
        """Risk data. Default: overview. ``dashboard=True`` for the dashboard payload;
        ``model=True`` for model performance metrics."""
        if dashboard:
            return self.dataset("risk_dashboard", as_dataframe=as_dataframe)
        if model:
            return self.dataset("risk_model", as_dataframe=as_dataframe)
        return self.dataset("risk_overview", as_dataframe=as_dataframe)

    def failures(
        self,
        *,
        year: Optional[int] = None,
        dashboard: bool = False,
        as_dataframe: bool = True,
        **filters: Any,
    ) -> Any:
        """Bank failures. ``year`` returns the by-year rollup; ``dashboard=True``
        for the dashboard payload; otherwise the most-recent-failures feed."""
        if dashboard:
            return self.dataset("failures_dashboard", as_dataframe=as_dataframe, **filters)
        if year is not None:
            return self.dataset("failures_by_year", year=year, as_dataframe=as_dataframe, **filters)
        return self.dataset("failures_recent", as_dataframe=as_dataframe, **filters)

    def events(self, *, as_dataframe: bool = True, **filters: Any) -> Any:
        """Industry-wide structural events feed (M&A, charters, closures)."""
        return self.dataset("events", as_dataframe=as_dataframe, **filters)

    def enforcement(self, *, as_dataframe: bool = True, **filters: Any) -> Any:
        """Industry-wide enforcement-action press feed."""
        return self.dataset("enforcement", as_dataframe=as_dataframe, **filters)

    # rates #

    def rates(
        self,
        kind: Literal[
            "mortgage", "consumer", "deposit", "treasury_bill", "treasury", "real_yield", "national"
        ] = "mortgage",
        *,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        latest: bool = False,
        as_dataframe: bool = True,
        **filters: Any,
    ) -> Any:
        """National rate series.

        Args:
            kind: One of ``mortgage``, ``consumer``, ``deposit``, ``treasury_bill``
                (alias ``treasury``), ``real_yield``, ``national``.
            start_date: ISO date string, e.g. ``"2020-01-01"``.
            end_date: ISO date string.
            latest: If True, request only the most recent observation.
        """
        name = {
            "mortgage": "rates_mortgage",
            "consumer": "rates_consumer",
            "deposit": "rates_deposit",
            "treasury_bill": "rates_treasury_bill",
            "treasury": "rates_treasury_bill",
            "real_yield": "rates_real_yield",
            "national": "rates_national",
        }.get(kind)
        if name is None:
            raise BankRegError(
                "rates(kind=…) must be one of: mortgage, consumer, deposit, "
                "treasury_bill, real_yield, national"
            )
        return self.dataset(
            name,
            start_date=start_date,
            end_date=end_date,
            latest=1 if latest else None,
            as_dataframe=as_dataframe,
            **filters,
        )

    def yield_curve(
        self,
        *,
        latest: bool = False,
        tenor: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        as_dataframe: bool = True,
        **filters: Any,
    ) -> Any:
        """US Treasury yield curve (long form, one row per date/tenor).

        Args:
            latest: True for the current curve snapshot (~12 rows).
            tenor: Filter to a single tenor code, e.g. ``"Y10"`` for the
                10-year daily series.
            start_date / end_date: ISO date strings.
        """
        kw: dict[str, Any] = {}
        if latest:
            kw["latest"] = 1
        if tenor:
            kw["tenor_code"] = tenor
        if start_date:
            kw["start_date"] = start_date
        if end_date:
            kw["end_date"] = end_date
        return self.dataset("yield_curve", as_dataframe=as_dataframe, **kw, **filters)

    # credit unions #

    def credit_unions(
        self,
        *,
        state: str = "",
        sort: str = "",
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        as_dataframe: bool = True,
    ) -> Any:
        """Directory of NCUA-insured credit unions."""
        return self.dataset(
            "credit_unions",
            state=state,
            sort=sort,
            limit=limit,
            offset=offset,
            as_dataframe=as_dataframe,
        )

    def credit_union(
        self,
        cu_number: int,
        *,
        trends: bool = False,
        quarters: int = 12,
        as_dataframe: bool = True,
    ) -> Any:
        """One credit union: latest snapshot, or its trend series with ``trends=True``."""
        name = "credit_union_trends" if trends else "credit_union"
        kw = {"quarters": quarters} if trends else {}
        return self.dataset(name, cu_number=cu_number, as_dataframe=as_dataframe, **kw)

    def credit_union_branches(self, cu_number: int, *, as_dataframe: bool = True) -> Any:
        """Branch locations for one credit union."""
        return self.dataset("cu_branches", cu_number=cu_number, as_dataframe=as_dataframe)

    def credit_union_atms(self, cu_number: int, *, as_dataframe: bool = True) -> Any:
        """ATM locations for one credit union."""
        return self.dataset("cu_atms", cu_number=cu_number, as_dataframe=as_dataframe)

    def cu_peer_benchmarks(self, *, as_dataframe: bool = True) -> Any:
        """Credit union peer benchmarks by asset tier."""
        return self.dataset("cu_peer_benchmarks", as_dataframe=as_dataframe)

    def ncua_enforcement(self, *, as_dataframe: bool = True) -> Any:
        """NCUA enforcement actions feed."""
        return self.dataset("ncua_enforcement", as_dataframe=as_dataframe)

    # holding companies #

    def holding_companies(self, *, as_dataframe: bool = True, **filters: Any) -> Any:
        """Directory of bank holding companies."""
        return self.dataset("holding_companies", as_dataframe=as_dataframe, **filters)

    def holding_company(
        self,
        rssd_id: int,
        *,
        trends: bool = False,
        quarters: int = 12,
        as_dataframe: bool = True,
    ) -> Any:
        """One holding company snapshot, or its trend series with ``trends=True``."""
        name = "holding_company_trends" if trends else "holding_company"
        kw = {"quarters": quarters} if trends else {}
        return self.dataset(name, rssd_id=rssd_id, as_dataframe=as_dataframe, **kw)

    # custom peer groups #

    def custom_peer_groups(
        self,
        *,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        as_dataframe: bool = True,
    ) -> Any:
        """User's custom peer groups (Pro tier; per-account)."""
        return self.dataset(
            "custom_peer_groups", limit=limit, offset=offset, as_dataframe=as_dataframe
        )

    def custom_peer_group(self, group_id: int, *, as_dataframe: bool = True) -> Any:
        """Single custom peer group with full peer-comparison data."""
        return self.dataset(
            "custom_peer_group_detail", group_id=group_id, as_dataframe=as_dataframe
        )

    # reference / catalog #

    def metrics(self, *, as_dataframe: bool = True) -> Any:
        """Metric catalog: every available metric with units + definition."""
        return self.dataset("metrics", as_dataframe=as_dataframe)

    def reference(
        self,
        slug: Optional[str] = None,
        *,
        as_of: Optional[str] = None,
        limit: Optional[int] = None,
        as_dataframe: bool = True,
    ) -> Any:
        """Reference data. No ``slug`` returns the index of available tables.
        ``slug="states"`` / ``"regulators"`` / etc. returns that table.
        For MDRM item definitions pass the item code, e.g. ``slug="RCON2170"``
        — the client routes to the MDRM endpoint automatically.

        Args:
            slug: Table slug or MDRM item code.
            as_of: ISO date for MDRM validity-window resolution (defaults to today).
            limit: Row limit (default 1000, max 10000).
        """
        if slug is None:
            return self.dataset("reference_index", as_dataframe=as_dataframe)
        if slug.upper().startswith(("RCON", "RIAD", "RCFD", "RCOA", "RCOB", "RCOC")):
            return self.dataset(
                "reference_mdrm",
                code=slug.upper(),
                as_of=as_of,
                as_dataframe=as_dataframe,
            )
        return self.dataset("reference", slug=slug, limit=limit, as_dataframe=as_dataframe)

    def datasets_catalog(self, *, key: Optional[str] = None, as_dataframe: bool = True) -> Any:
        """Public data-product catalog. ``key`` fetches metadata for one dataset."""
        if key:
            return self.dataset("dataset_detail", key=key, as_dataframe=as_dataframe)
        return self.dataset("datasets_catalog", as_dataframe=as_dataframe)

    def healthz(self, *, as_dataframe: bool = False) -> Any:
        """API health probe: status, latest data quarter, and uptime."""
        return self.dataset("healthz", as_dataframe=as_dataframe)

    # watchlist (mutable) #

    def watchlist(self, *, as_dataframe: bool = True) -> Any:
        """Return the current user's watchlist of tracked banks."""
        return self.dataset("watchlist", as_dataframe=as_dataframe)

    def watchlist_add(
        self,
        rssd_id: int,
        *,
        bank_name: Optional[str] = None,
    ) -> Any:
        """Add a bank to the watchlist. Returns the created watchlist entry."""
        body: dict[str, Any] = {"rssd_id": rssd_id}
        if bank_name:
            body["bank_name"] = bank_name
        return self._post_json("watchlist/", body)

    def watchlist_remove(self, rssd_id: int) -> bool:
        """Remove a bank from the watchlist. Returns True on success."""
        return self._delete(f"watchlist/{rssd_id}/")

    # alerts (mutable) #

    def alert_list(self, *, as_dataframe: bool = True) -> Any:
        """Return the current user's threshold alerts."""
        return self.dataset("alerts", as_dataframe=as_dataframe)

    def alert_create(
        self,
        rssd_id: int,
        metric: str,
        direction: Literal["above", "below"],
        threshold: float,
    ) -> Any:
        """Create a threshold alert.

        Args:
            rssd_id: Bank to monitor.
            metric: Metric code to watch (e.g. ``"cet1_ratio"``).
            direction: ``"above"`` or ``"below"``.
            threshold: Numeric threshold value.
        """
        if direction not in ("above", "below"):
            raise BankRegError("alert_create: direction must be 'above' or 'below'")
        return self._post_json(
            "watchlist/alerts/",
            {"rssd_id": rssd_id, "metric": metric, "direction": direction, "threshold": threshold},
        )

    def alert_update(
        self,
        alert_id: int,
        *,
        is_active: Optional[bool] = None,
    ) -> Any:
        """Update an alert (e.g. toggle active status). Returns the updated alert."""
        body: dict[str, Any] = {}
        if is_active is not None:
            body["is_active"] = is_active
        return self._patch_json(f"watchlist/alerts/{alert_id}/", body)

    def alert_delete(self, alert_id: int) -> bool:
        """Delete an alert. Returns True on success."""
        return self._delete(f"watchlist/alerts/{alert_id}/")

    # saved graphs (mutable) #

    def graphs(
        self,
        *,
        scope: Literal["mine", "public", "all"] = "mine",
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        as_dataframe: bool = True,
    ) -> Any:
        """List saved analytical graphs.

        Args:
            scope: ``"mine"`` (default), ``"public"``, or ``"all"``.
        """
        return self.dataset(
            "graphs",
            scope=scope,
            limit=limit,
            offset=offset,
            as_dataframe=as_dataframe,
        )

    def graph(self, graph_id: int, *, as_dataframe: bool = True) -> Any:
        """Fetch a single saved graph by id (owner or public)."""
        return self.dataset("graph_detail", graph_id=graph_id, as_dataframe=as_dataframe)

    def graph_create(
        self,
        name: str,
        kind: str,
        *,
        description: str = "",
        config: Optional[dict] = None,
        visibility: Literal["private", "public"] = "private",
        template_key: Optional[str] = None,
    ) -> Any:
        """Create a new saved graph. Returns the created graph object."""
        body: dict[str, Any] = {"name": name, "kind": kind, "visibility": visibility}
        if description:
            body["description"] = description
        if config:
            body["config"] = config
        if template_key:
            body["template_key"] = template_key
        return self._post_json("graphs/", body)

    def graph_update(self, graph_id: int, **fields: Any) -> Any:
        """Update a graph (owner only). Pass any fields to update. Returns the
        updated graph."""
        return self._put_json(f"graphs/{graph_id}/", fields)

    def graph_delete(self, graph_id: int) -> bool:
        """Delete a graph (owner only). Returns True on success."""
        return self._delete(f"graphs/{graph_id}/")

    def graph_fork(self, graph_id: int, *, name: Optional[str] = None) -> Any:
        """Clone a public graph into the current user's account. Returns the new
        graph."""
        body: dict[str, Any] = {}
        if name:
            body["name"] = name
        return self._post_json(f"graphs/{graph_id}/fork/", body)

    def chart_resolve(self, kind: str, config: dict) -> Any:
        """Resolve a chart configuration into a series payload.

        Args:
            kind: Chart kind (e.g. ``"trend"``, ``"peer_bar"``).
            config: Chart configuration dict (metrics, banks, date range, etc.).
        """
        return self._post_json("charts/resolve/", {"kind": kind, "config": config})

    def __enter__(self) -> bankreg:
        return self

    def __exit__(self, *args: Any) -> None:
        self._session.close()

    def __repr__(self) -> str:
        # Never echo any secret character: keep only the public key-type prefix
        # (``brr_`` / ``brrat_``) so a pasted repr or log line cannot leak the key.
        if not self.token:
            masked = "no-token"
        else:
            head = self.token.partition("_")[0]
            masked = f"{head}_***" if head in ("brr", "brrat") else "***"
        return (
            f"bankreg(base_url={self.base_url!r}, token={masked!r}, " f"datasets={len(DATASETS)})"
        )


BankReg = bankreg  # CamelCase alias; preferred for new code


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _require_pandas() -> Any:
    try:
        import pandas as pd
    except ImportError as exc:  # pragma: no cover
        raise BankRegError(
            "pandas is required for DataFrame results. Install it with "
            "`pip install pandas`, or call methods with as_dataframe=False for raw JSON."
        ) from exc
    return pd


__all__ = [
    "DATASETS",
    "DEFAULT_BASE_URL",
    "DEFAULT_MAX_RETRIES",
    "DEFAULT_PAGE_SIZE",
    "DEFAULT_TIMEOUT",
    "ENV_BASE_URL",
    "ENV_TOKEN",
    "APIConnectionError",
    "APITimeoutError",
    "AuthenticationError",
    "BankReg",
    "BankRegError",
    "Dataset",
    "NotFoundError",
    "PermissionError_",
    "RateLimitError",
    "ServerError",
    "UpgradeRequiredError",
    "ValidationError",
    "__version__",
    "bankreg",
]
