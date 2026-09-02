"""
Fetches live data for the ILS/USD Real Exchange Rate dashboard and regenerates
"index.html" with the results embedded.

Also builds a second dataset (Israel CPI vs. four US price indices, plus
year-over-year inflation for each) used by the "Price Indices" tab.

Sources:
  - FRED (St. Louis Fed): US CPI (CPIAUCSL), US CPI not seasonally adjusted
    (CPIAUCNS), US PCEPI (PCEPI), and US core PCEPI (PCEPILFE), via the
    authenticated FRED API -- requires a free API key
    (https://fred.stlouisfed.org/docs/api/api_key.html) passed via the
    FRED_API_KEY environment variable. (The unauthenticated fredgraph.csv
    endpoint works fine from a home connection but is unreliable/blocked
    from cloud CI runner IPs, which is why this uses the real API instead.)
  - CBS (Israel Central Bureau of Statistics): Israel CPI, index 120010 (public)
  - Bank of Israel Fusion Data Browser: representative USD/ILS exchange rate (public)

Run manually with: FRED_API_KEY=xxxx python fetch_and_build.py
The GitHub Actions workflow (.github/workflows) runs this daily. There is no
day-of-month scheduling rule: every run fetches fresh data, and the dashboard
only actually updates when a new month has become available across *all* of
the price indices -- see `latest_common_month()` and `main()` below. Pass
--force to make it adopt whatever it just fetched regardless (e.g. to pick up
a backward revision to an already-published month).
"""

import csv
import io
import json
import os
import re
import sys
import urllib.request
import urllib.error
from datetime import date, datetime, timezone

START_YEAR = 2000
HEADERS = {"User-Agent": "Mozilla/5.0 (RealEXRDashboard/1.0)"}

FRED_API_KEY = os.environ.get("FRED_API_KEY")
FRED_API_URL = "https://api.stlouisfed.org/fred/series/observations"
BOI_URL = (
    "https://edge.boi.org.il/FusionEdgeServer/sdmx/v2/data/dataflow/"
    "BOI.STATISTICS/EXR/1.0/RER_USD_ILS"
    f"?format=csv&normalisefreq=M;mean&startperiod={START_YEAR}-01-01"
)
CBS_URL = "https://api.cbs.gov.il/index/data/price"
CBS_INDEX_ID = 120010

HERE = __import__("pathlib").Path(__file__).resolve().parent
HTML_OUT = HERE / "index.html"
CACHE_OUT = HERE / "data_cache.json"
TEMPLATE = HERE / "dashboard_template.html"


def http_get(url, retries=3):
    last_err = None
    for _ in range(retries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read().decode("utf-8")
        except (urllib.error.URLError, TimeoutError) as e:
            last_err = e
    raise RuntimeError(f"Failed to fetch {url}: {last_err}")


def fetch_fred_series(series_id):
    if not FRED_API_KEY:
        raise RuntimeError(
            "FRED_API_KEY environment variable is not set. Get a free key at "
            "https://fred.stlouisfed.org/docs/api/api_key.html"
        )
    url = (
        f"{FRED_API_URL}?series_id={series_id}&api_key={FRED_API_KEY}"
        f"&file_type=json&observation_start={START_YEAR}-01-01"
    )
    text = http_get(url)
    data = json.loads(text)
    out = {}
    for obs in data["observations"]:
        ym = obs["date"][:7]
        val = (obs.get("value") or "").strip()
        if val and val != ".":
            out[ym] = float(val)
    return out


def fetch_fred():
    cpi = fetch_fred_series("CPIAUCSL")
    pcepi = fetch_fred_series("PCEPI")
    cpi_ns = fetch_fred_series("CPIAUCNS")
    pcepilfe = fetch_fred_series("PCEPILFE")
    return cpi, pcepi, cpi_ns, pcepilfe


def fetch_boi():
    text = http_get(BOI_URL)
    reader = csv.DictReader(io.StringIO(text))
    out = {}
    for row in reader:
        val = (row.get("OBS_VALUE") or "").strip()
        ym = (row.get("TIME_PERIOD") or "").strip()
        if val and ym:
            out[ym] = float(val)
    return out


def fetch_cbs_page(page, end_period):
    url = (
        f"{CBS_URL}?id={CBS_INDEX_ID}&format=json&download=false"
        f"&startPeriod=01-{START_YEAR}&endPeriod={end_period}"
        f"&lang=en&coef=true&Page={page}"
    )
    text = http_get(url)
    return json.loads(text)


def fetch_cbs():
    end_period = f"12-{date.today().year}"
    first = fetch_cbs_page(1, end_period)
    months = list(first["month"][0]["date"])
    last_page = first["paging"]["last_page"]
    for page in range(2, last_page + 1):
        data = fetch_cbs_page(page, end_period)
        months.extend(data["month"][0]["date"])

    latest = max(months, key=lambda r: (r["year"], r["month"]))
    base_desc = latest["currBase"]["baseDesc"]
    factor_map = {base_desc: 1.0}
    for pb in latest.get("prevBase") or []:
        factor_map[pb["baseDesc"]] = pb["value"] / latest["currBase"]["value"]

    m = re.search(r"(\d{4})", base_desc)
    base_year = int(m.group(1)) if m else None

    out = {}
    for r in months:
        ym = f"{r['year']:04d}-{r['month']:02d}"
        raw = r["currBase"]["value"]
        regime = r["currBase"]["baseDesc"]
        factor = factor_map.get(regime, 1.0)
        out[ym] = raw / factor
    return out, base_year


def years_with_data(*series_dicts):
    """Years present (with at least one month) in every one of the given
    series -- the full set of selectable base years."""
    common = None
    for series in series_dicts:
        years = {int(k.split("-")[0]) for k in series}
        common = years if common is None else common & years
    return sorted(common or [])


def full_years_available(*series_dicts):
    """Subset of years_with_data() where every series has all 12 months
    present -- an unbiased average to rebase against. A year missing this
    (e.g. a month delayed by a government shutdown, or the current year
    still in progress) is still selectable, just noted as partial."""
    common = None
    for series in series_dicts:
        months_by_year = {}
        for k in series:
            y, m = k.split("-")
            months_by_year.setdefault(int(y), set()).add(int(m))
        full = {y for y, months in months_by_year.items() if len(months) == 12}
        common = full if common is None else common & full
    return sorted(common or [])


def yoy_inflation_pct(series, ym):
    """Year-over-year % change (as percentage points, e.g. 3.2 meaning 3.2%).
    Scale-invariant, so it doesn't matter whether `series` is raw or rebased."""
    y, m = map(int, ym.split("-"))
    prev_ym = f"{y - 1:04d}-{m:02d}"
    prev = series.get(prev_ym)
    cur = series.get(ym)
    if prev is None or cur is None or prev == 0:
        return None
    return round((cur / prev - 1) * 100, 3)


def build_dataset():
    print("Fetching FRED (US CPI, PCEPI, CPI-NS, PCEPILFE)...")
    cpi_us_raw, pcepi_us_raw, cpi_us_ns_raw, pcepilfe_us_raw = fetch_fred()
    print(
        f"  {len(cpi_us_raw)} CPI months, {len(pcepi_us_raw)} PCEPI months, "
        f"{len(cpi_us_ns_raw)} CPI-NS months, {len(pcepilfe_us_raw)} PCEPILFE months"
    )

    print("Fetching Bank of Israel (nominal USD/ILS)...")
    nominal = fetch_boi()
    print(f"  {len(nominal)} months")

    print("Fetching CBS (Israel CPI, index 120010)...")
    cpi_isr, base_year = fetch_cbs()
    print(f"  {len(cpi_isr)} months, current base year = {base_year}")

    # Rebasing to `base_year` (the base year toggle's default selection) is
    # done client-side, from the raw series below, so the dashboard can
    # re-rebase to any other complete year on the fly without a re-fetch.
    # Year-over-year inflation is scale-invariant -- it comes out identical
    # regardless of base year -- so it's computed once here, from the raw
    # series, rather than being recomputed client-side on every toggle.
    price_common_dates = sorted(
        set(cpi_isr) & set(cpi_us_raw) & set(cpi_us_ns_raw) & set(pcepi_us_raw) & set(pcepilfe_us_raw)
    )
    inflation = {
        ym: {
            "israel": yoy_inflation_pct(cpi_isr, ym),
            "usCPIAUCSL": yoy_inflation_pct(cpi_us_raw, ym),
            "usCPIAUCNS": yoy_inflation_pct(cpi_us_ns_raw, ym),
            "usPCEPI": yoy_inflation_pct(pcepi_us_raw, ym),
            "usPCEPILFE": yoy_inflation_pct(pcepilfe_us_raw, ym),
        }
        for ym in price_common_dates
    }

    valid_base_years = years_with_data(
        cpi_isr, cpi_us_raw, cpi_us_ns_raw, pcepi_us_raw, pcepilfe_us_raw
    )
    full_base_years = set(
        full_years_available(cpi_isr, cpi_us_raw, cpi_us_ns_raw, pcepi_us_raw, pcepilfe_us_raw)
    )
    partial_base_years = sorted(set(valid_base_years) - full_base_years)

    raw = {
        "nominal": {k: round(v, 6) for k, v in nominal.items()},
        "israelCPI": {k: round(v, 6) for k, v in cpi_isr.items()},
        "usCPIAUCSL": {k: round(v, 6) for k, v in cpi_us_raw.items()},
        "usCPIAUCNS": {k: round(v, 6) for k, v in cpi_us_ns_raw.items()},
        "usPCEPI": {k: round(v, 6) for k, v in pcepi_us_raw.items()},
        "usPCEPILFE": {k: round(v, 6) for k, v in pcepilfe_us_raw.items()},
    }

    meta = {
        "defaultBaseYear": base_year,
        "validBaseYears": valid_base_years,
        "partialBaseYears": partial_base_years,
        "latestCommonMonth": price_common_dates[-1] if price_common_dates else None,
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sources": {
            "usCpi": "FRED CPIAUCSL (Consumer Price Index for All Urban Consumers). Published monthly. Seasonally adjusted",
            "usPcepi": "FRED PCEPI (Personal Consumption Expenditures, Chain-Type Price Index). Published monthly. Seasonally adjusted",
            "usCpiNs": "FRED CPIAUCNS (Consumer Price Index for All Urban Consumers). Published monthly. Not seasonally adjusted",
            "usPcepiLfe": "FRED PCEPILFE (Personal Consumption Expenditures Excluding Food and Energy, Chain-Type Price Index). Published monthly. Seasonally adjusted",
            "israelCpi": f"CBS index {CBS_INDEX_ID} (Consumer Price Index - General). Published monthly. Not seasonally adjusted",
            "nominalExr": "Bank of Israel Fusion Data Browser, representative USD/ILS rate. Published daily; monthly average used here. Not seasonally adjusted",
        },
    }
    return raw, inflation, meta


def render_html(raw, inflation, meta):
    template = TEMPLATE.read_text(encoding="utf-8")
    payload = json.dumps(
        {"raw": raw, "inflation": inflation, "meta": meta}, ensure_ascii=False
    )
    html = template.replace("__DASHBOARD_DATA__", payload)
    HTML_OUT.write_text(html, encoding="utf-8")


def load_cache():
    if not CACHE_OUT.exists():
        return None
    try:
        return json.loads(CACHE_OUT.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def main():
    force = "--force" in sys.argv[1:]

    fetched_raw, fetched_inflation, fetched_meta = build_dataset()
    if not fetched_raw["nominal"] or not fetched_inflation:
        print("No overlapping data across all sources — aborting.", file=sys.stderr)
        sys.exit(1)
    fetched_latest = fetched_meta["latestCommonMonth"]

    cached = load_cache()
    cached_latest = (cached or {}).get("meta", {}).get("latestCommonMonth")

    if cached is not None and not force and cached_latest is not None and fetched_latest <= cached_latest:
        print(
            f"Latest month with all price indices available is still {cached_latest} "
            f"(just-fetched data also tops out at {fetched_latest}) -- keeping existing data."
        )
        raw, inflation, meta = cached["raw"], cached["inflation"], cached["meta"]
    else:
        raw, inflation, meta = fetched_raw, fetched_inflation, fetched_meta
        CACHE_OUT.write_text(
            json.dumps(
                {"raw": raw, "inflation": inflation, "meta": meta},
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        print(f"New data adopted: price indices now cover through {fetched_latest}.")

    # Always regenerate index.html from the current template, even when the
    # data itself didn't change -- so template/styling changes go out on the
    # next run without waiting for new source data.
    render_html(raw, inflation, meta)
    print(f"Wrote {len(raw['nominal'])} nominal-EXR months, {len(inflation)} price-index months")
    print(f"Valid base years: {meta['validBaseYears'][0]}-{meta['validBaseYears'][-1]}")
    print(f"Dashboard updated: {HTML_OUT}")


if __name__ == "__main__":
    main()
