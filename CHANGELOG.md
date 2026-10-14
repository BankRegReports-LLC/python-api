# Changelog

All notable changes to the `bankregreports` SDK are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
Versioning follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

### Added
- `APIConnectionError` and `APITimeoutError` so a transport failure is distinguishable from a server-side 5xx. Both subclass `ServerError`, so existing `except ServerError` and `except BankRegError` handlers keep catching them unchanged, and GET retry behavior is identical. Catch `APITimeoutError` directly when a slow network deserves different handling from a broken backend.
- `DEFAULT_BASE_URL`, `DEFAULT_TIMEOUT`, `DEFAULT_MAX_RETRIES`, `DEFAULT_PAGE_SIZE`, `ENV_TOKEN`, and `ENV_BASE_URL` are now exported in `__all__`. They were already public and documented, just not advertised.
- Pinned `ruff` rule set in `pyproject.toml`, a `pre-commit` config, and Dependabot for pip and GitHub Actions.
- `release.yml`: builds, verifies the tag matches `__version__`, and publishes to PyPI through Trusted Publishing (OIDC), with no stored API token.

### Changed
- **The client is now MIT licensed** (was a proprietary license that barred redistribution). The license covers the client code only; API and data use remain governed by the BankRegReports Terms of Service.
- **Python 3.10 or newer is now required** (`requires-python = ">=3.10"`). Python 3.8 and 3.9 are past end of life; CI tests 3.10 through 3.13.
- `repr(client)` no longer shows any character of the API key. It prints only the key-type prefix, for example `brr_***`.
- The README configuration sample uses the production base URL instead of `http://localhost:8000`, and links to `CHANGELOG.md` and `LICENSE` by absolute GitHub URL instead of a relative path, which returned 404 on PyPI. The README title says BankRegReports instead of BankRegAPI.
- The four single-attempt write helpers (POST, PATCH, PUT, DELETE) now share one implementation. Behavior, headers, and call signatures are unchanged.
- CI runs `ruff`, `mypy`, and `pip-audit` in addition to the tests, across the supported Python versions. The clean lint and type state is now enforced rather than incidental.
- Two dataset descriptions and one docstring use plain ASCII (`x`, `-`) instead of the multiplication sign and en dash, so `list_datasets()` output is safe to write to CSV in any encoding.

### Fixed
- **Dataset tiers were wrong on 56 of 101 datasets, every one of them overstating the gate.** `list_datasets()` reported datasets as `pro` or `free` when the API serves them as `public` or `free`, so users were told data was behind a paywall that does not exist. Among them: `prediction` and `risk_model` (listed `pro`, actually `free`), `ma_screener` and `bank_deep_dive` (listed `pro`, actually `public`), and all 14 SEC datasets (listed `pro`, actually `free`). Tiers are now synced to the API's classification, matched by route rather than by name. This is metadata only; routing was never affected and no call behavior changes.
- The CI matrix pinned Python 3.8 to `ubuntu-latest`, which is now 24.04 and no longer provides 3.8. That job would have failed at interpreter setup on its first run. 3.8 now runs on `ubuntu-22.04`.
- `py.typed` now actually ships in the wheel and sdist. The distribution moved from a single `bankregreports.py` module to a `bankregreports/` package so the PEP 561 marker is packaged with the code; previously the `Typing :: Typed` claim was advertised but the marker was silently dropped at build time, so type checkers saw the client as untyped. The public import surface (`import bankregreports`, `BankReg`, `DATASETS`, …) is unchanged.
- `Retry-After` HTTP-date form (RFC 7231) no longer raises a bare `ValueError`; the header is parsed to seconds and, when malformed, ignored so the `RateLimitError` still surfaces cleanly
- Auto-retry now caps a server-supplied `Retry-After` wait at `DEFAULT_MAX_RETRY_SLEEP` (30s), so a broken or hostile header can't stall the client indefinitely (`e.retry_after` still carries the full server value)

---

## [0.2.0] - 2026-06-20

### Added
- `BankReg` CamelCase class alias (preferred name for new code; `bankreg` retained for backward compatibility)
- `UpgradeRequiredError` as the canonical name for 403 errors; `PermissionError_` retained as a backward-compatible alias
- `__enter__` / `__exit__` context manager support for clean session lifecycle
- `Literal` type constraints on `rates(kind)`, `alert_create(direction)`, `graphs(scope)`, and `graph_create(visibility)`
- `as_map` parameter on `branches()` (replaces `map` which shadowed the Python builtin)
- `py.typed` marker (PEP 561) for fully typed package
- `pyproject.toml` modern packaging (replaces `setup.py`)
- `CHANGELOG.md`

### Changed
- `branches(map=...)` renamed to `branches(as_map=...)`; the old name shadowed the `map` builtin
- `import string` moved to module level (was re-imported inside `_ds()` on every call)
- `__all__` reordered: `BankReg`, `bankreg` first; `UpgradeRequiredError` before `PermissionError_`

### Fixed
- 403 handler now raises `UpgradeRequiredError` (was incorrectly raising `PermissionError_` via the alias)

---

## [0.1.0] - 2024-01-01

### Added
- Initial release
- 80+ methods across 16 data domains
- 101 registered datasets with generic `dataset()` accessor
- Automatic retry with exponential backoff on 429 and 5xx
- Optional pandas integration (`as_dataframe=True` default)
- `df.attrs["meta"]` response envelope
- `pages()` pagination iterator
