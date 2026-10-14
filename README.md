# bankregreports: the BankRegReports API Python SDK

[![PyPI version](https://badge.fury.io/py/bankregreports.svg)](https://pypi.org/project/bankregreports/)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![Typed](https://img.shields.io/badge/typing-PEP%20561-brightgreen)](https://peps.python.org/pep-0561/)
[![Swagger Docs](https://img.shields.io/badge/API-Swagger%20UI-orange)](https://api.bankregreports.com/api/v1/docs/)
[![Docs](https://img.shields.io/badge/docs-bankregreports.com-blue)](https://www.bankregreports.com/api/)

Python client for the **[BankRegReports Data API](https://www.bankregreports.com)**:
clean, validated, cross-source regulatory data on every US bank, credit union, and holding
company, sourced from FFIEC Call Reports, UBPR, FDIC, Federal Reserve, NCUA, and SEC EDGAR.

**[Full SDK docs](https://www.bankregreports.com/api/)**
&nbsp;·&nbsp;
**[Swagger / OpenAPI](https://api.bankregreports.com/api/v1/docs/)**
&nbsp;·&nbsp;
**[Get API key](https://www.bankregreports.com/api/)**
&nbsp;·&nbsp;
**[bankregreports.com](https://www.bankregreports.com)**

---

## Why BankRegReports

Raw US bank regulatory data is public but arrives as pipe-delimited bulk files with cryptic
MDRM codes, four competing unit conventions, silent schema changes between quarters, and
no linkage between FDIC, FFIEC, Federal Reserve, and SEC identifiers. Cleaning it to a
usable state takes months. Enterprise data platforms solve this at $20K–$200K+/year with
multi-year contracts.

**[BankRegReports](https://www.bankregreports.com)** is the middle ground: the same
cleaned, validated, cross-source-linked data through a modern REST API with a Python SDK,
at a fraction of the cost. Built by practitioners who have been in ALCO, been through
regulatory exams, and needed this data to actually exist.

Read the full background: **[Why We Built BankRegReports →](https://www.bankregreports.com)**

---

## Installation

```bash
pip install bankregreports
# with pandas (recommended)
pip install "bankregreports[pandas]"
```

**Requires:** Python 3.10+ · `requests>=2.25`

---

## Quick start

```python
import bankregreports

# Set BANKREG_API_TOKEN env var, or pass the token directly
brr = bankregreports.BankReg("brr_xxx")

# Screen every US bank; returns a pandas DataFrame
df = brr.screener(state="GA", min_assets=500_000)
print(df[["legal_name", "total_assets", "cet1_ratio", "roa"]].head(10))
```

Works as a context manager:

```python
with bankregreports.BankReg("brr_xxx") as brr:
    df_banks = brr.screener(state="TX")
    df_rates = brr.rates("mortgage")
```

---

## Authentication

Get a free API key at **[bankregreports.com/api](https://www.bankregreports.com/api/)**.
All keys begin with `brr_`.

```python
# Explicit token
brr = bankregreports.BankReg("brr_xxx")

# Environment variable (recommended)
# export BANKREG_API_TOKEN=brr_xxx
brr = bankregreports.BankReg()
```

---

## Coverage: 101 datasets across 16 domains

| Domain | What you get |
|---|---|
| Banks: snapshot & trends | Latest metrics, 20-quarter time series, full Call Report/UBPR detail |
| Banks: scores & peers | CAMELS-style scores, percentile ranks, peer benchmarks |
| Banks: sub-books | Deposits, securities (HTM/AFS/unrealized G/L), loans, HMDA |
| Banks: structure | Parent chain, subsidiaries, NIC events, FDIC history, FRY-15 |
| Branches | FDIC SoD + NIC combined, rollup stats, map-ready lat/lon |
| Risk & compliance | ML failure prediction + feature attribution, enforcement actions, CFPB complaints |
| Screeners | Full-universe filter, M&A target scoring, CRE concentration, growth |
| SEC / EDGAR | 10-K/10-Q/8-K, XBRL facts, insider Form-4, exec comp, 13D/G, 13F |
| Industry & macro | Aggregates, rate environment, credit spreads, VIX |
| Rates & yield curve | Mortgage, T-bill, real yield, consumer, FDIC national deposit rates |
| Failures & events | FDIC failure feed, M&A/charter events, enforcement feed |
| Credit unions (NCUA) | 5300 Call Report, trends, branches, ATMs, peer benchmarks |
| Holding companies | Y-9C BHC snapshot and quarterly trends |
| CECL & analytics | Macro-conditioned lifetime ECL, DFAST scenarios |
| Watchlist & alerts | Track institutions, threshold alerts on any metric |
| Reference & catalog | MDRM definitions, metric catalog with formulas |

---

## API reference (selected methods)

```python
# Banks
brr.bank(852218)                                # latest snapshot
brr.bank_trends(852218, quarters=12)            # quarterly time series
brr.bank_deep_dive(852218)                      # full Call Report/UBPR detail (~150 fields)
brr.bank_profile(852218)                        # snapshot + scorecard + ranks + ML prediction
brr.bank_compare([852218, 480228, 37])          # side-by-side comparison
brr.screener(state="GA", min_assets=500_000)    # filter the full ~4,400-bank universe
brr.scorecard(852218)                           # CAMELS-style letter grade + component scores
brr.peer_comparison(852218)                     # bank vs. peer benchmarks

# Sub-books
brr.bank_deposits(852218, trends=True)          # deposit composition trends
brr.bank_securities(852218)                     # HTM/AFS, unrealized G/L, OTTI
brr.loan_portfolio(852218)                      # CRE, C&I, consumer, ag concentrations
brr.bank_hmda(852218)                           # HMDA mortgage lending

# Risk & analytics
brr.prediction(852218, detail=True)             # ML failure probability + feature attribution
brr.ma_screener()                               # M&A target scoring + valuation
brr.enforcement_actions(852218)                 # FDIC/OCC/FRB/CFPB enforcement history

# SEC / EDGAR
brr.sec_filings(852218)                         # 10-K, 10-Q, 8-K, proxy index
brr.sec_insider_txns(852218)                    # Form-4 insider buy/sell
brr.sec_13f(852218)                             # institutional holdings
brr.executive_comp(852218)                      # NEO compensation from proxy

# Credit unions (NCUA)
brr.credit_union(5536, trends=True)             # 5300 Call Report snapshot + trends
brr.cu_peer_benchmarks()                        # CU peer benchmarks by asset tier

# Rates & macro
brr.rates("mortgage")                           # Freddie Mac 30yr/15yr series
brr.yield_curve(latest=True)                    # current yield curve snapshot
brr.industry(trends=True, quarters=20)          # 20-quarter industry aggregate trend

# Generic accessor
brr.list_datasets()                             # all 101 datasets: name, path, tier
brr.dataset("screener", state="GA")             # forward-compatible generic accessor
```

Full reference: **[www.bankregreports.com/api/](https://www.bankregreports.com/api/)**
&nbsp;·&nbsp;
Interactive REST docs: **[api.bankregreports.com/api/v1/docs/](https://api.bankregreports.com/api/v1/docs/)**

---

## Pagination

```python
import pandas as pd

# Iterate pages, 200 rows at a time
for page in brr.pages("screener", page_size=200, state="CA"):
    process(page)

# Collect all pages into one DataFrame
all_banks = pd.concat(brr.pages("screener", page_size=500))
```

---

## Error handling

All errors subclass `BankRegError`. GET requests auto-retry with exponential backoff on
`429` and `5xx` (default 3 retries).

```python
from bankregreports import (
    BankReg, BankRegError,
    AuthenticationError, UpgradeRequiredError, NotFoundError,
    RateLimitError, ValidationError, ServerError,
)

try:
    df = brr.ma_screener()
except UpgradeRequiredError:
    print("Upgrade at bankregreports.com/api/")
except RateLimitError as e:
    time.sleep(e.retry_after or 5)
except NotFoundError:
    print("Institution not found")
```

| Exception | HTTP | Meaning |
|---|---|---|
| `AuthenticationError` | 401 | Missing or revoked API key |
| `UpgradeRequiredError` | 403 | Plan doesn't cover this dataset |
| `NotFoundError` | 404 | Institution or resource not found |
| `ValidationError` | 422 | Invalid parameters |
| `RateLimitError` | 429 | Throttled; check `e.retry_after` |
| `ServerError` | 5xx | Server error (auto-retried) |

---

## Configuration

```python
brr = bankregreports.BankReg(
    token="brr_xxx",               # or BANKREG_API_TOKEN env var
    base_url="https://api.bankregreports.com",  # the default; override only for a proxy
    timeout=30,
    max_retries=3,
)
```

---

## Response format

All list-returning methods return a `pandas.DataFrame` by default (requires `pandas`
extra). Date columns are coerced to `datetime64`. The server response envelope is
available on `df.attrs["meta"]`.

Pass `as_dataframe=False` to any method for raw decoded JSON with no pandas dependency.

---

## Resources

| | |
|---|---|
| Platform | [bankregreports.com](https://www.bankregreports.com) |
| API sign-up | [bankregreports.com/api](https://www.bankregreports.com/api/) |
| Swagger / REST docs | [api.bankregreports.com/api/v1/docs/](https://api.bankregreports.com/api/v1/docs/) |
| SDK docs | [www.bankregreports.com/api/](https://www.bankregreports.com/api/) |
| Changelog | [CHANGELOG.md](https://github.com/BankRegReports-LLC/python-api/blob/main/CHANGELOG.md) |
| Support | [admin@bankregreports.com](mailto:admin@bankregreports.com) |

---

## License

MIT License. Copyright (c) 2024-2026 BankRegReports LLC. See [LICENSE](https://github.com/BankRegReports-LLC/python-api/blob/main/LICENSE).

The license covers this client library only. Use of the BankRegReports API and its data is governed by the [Terms of Service](https://www.bankregreports.com/terms/) and your plan.
