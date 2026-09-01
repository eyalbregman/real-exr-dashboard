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


def rebase_to_year(series, year):
    vals = [v for k, v in series.items() if k.startswith(str(year))]
    if not vals:
        raise ValueError(f"No observations found for base year {year}")
    avg = sum(vals) / len(vals)
    return {k: (v / avg) * 100 for k, v in series.items()}


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

    cpi_us = rebase_to_year(cpi_us_raw, base_year)
    pcepi_us = rebase_to_year(pcepi_us_raw, base_year)
    cpi_us_ns = rebase_to_year(cpi_us_ns_raw, base_year)
    pcepilfe_us = rebase_to_year(pcepilfe_us_raw, base_year)

    common_dates = sorted(
        set(nominal) & set(cpi_us) & set(cpi_us_ns) & set(pcepi_us) & set(pcepilfe_us) & set(cpi_isr)
    )
    rows = []
    for ym in common_dates:
        n = nominal[ym]
        c_isr = cpi_isr[ym]

        def real_exr(us_series):
            v = us_series.get(ym)
            return round(n * v / c_isr, 3) if (v is not None and c_isr) else None

        rows.append(
            {
                "date": ym,
                "nominal": round(n, 3),
                "realCPI": real_exr(cpi_us),
                "realPCEPI": real_exr(pcepi_us),
                "realCPIAUCNS": real_exr(cpi_us_ns),
                "realPCEPILFE": real_exr(pcepilfe_us),
            }
        )

    # Second dataset: Israel CPI vs. the four US price indices, all rebased
    # so `base_year` = 100, plus each series' year-over-year inflation.
    # Independent of the nominal EXR's date coverage.
    price_common_dates = sorted(
        set(cpi_isr) & set(cpi_us) & set(cpi_us_ns) & set(pcepi_us) & set(pcepilfe_us)
    )
    price_rows = []
    for ym in price_common_dates:
        price_rows.append(
            {
                "date": ym,
                "israelCPI": round(cpi_isr[ym], 3),
                "usCPIAUCSL": round(cpi_us[ym], 3),
                "usCPIAUCNS": round(cpi_us_ns[ym], 3),
                "usPCEPI": round(pcepi_us[ym], 3),
                "usPCEPILFE": round(pcepilfe_us[ym], 3),
                "israelInflation": yoy_inflation_pct(cpi_isr, ym),
                "usCPIAUCSLInflation": yoy_inflation_pct(cpi_us, ym),
                "usCPIAUCNSInflation": yoy_inflation_pct(cpi_us_ns, ym),
                "usPCEPIInflation": yoy_inflation_pct(pcepi_us, ym),
                "usPCEPILFEInflation": yoy_inflation_pct(pcepilfe_us, ym),
            }
        )

    meta = {
        "baseYear": base_year,
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sources": {
            "usCpi": "FRED CPIAUCSL (Consumer Price Index for All Urban Consumers)",
            "usPcepi": "FRED PCEPI (Personal Consumption Expenditures Price Index)",
            "usCpiNs": "FRED CPIAUCNS (Consumer Price Index for All Urban Consumers, Not Seasonally Adjusted)",
            "usPcepiLfe": "FRED PCEPILFE (Personal Consumption Expenditures Excluding Food and Energy, Chain-Type Price Index)",
            "israelCpi": f"CBS (Israel Central Bureau of Statistics) index {CBS_INDEX_ID}",
            "nominalExr": "Bank of Israel Fusion Data Browser, representative USD/ILS rate",
        },
    }
    return rows, price_rows, meta


def render_html(rows, price_rows, meta):
    template = TEMPLATE.read_text(encoding="utf-8")
    payload = json.dumps(
        {"rows": rows, "priceRows": price_rows, "meta": meta}, ensure_ascii=False
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

    fetched_rows, fetched_price_rows, fetched_meta = build_dataset()
    if not fetched_rows or not fetched_price_rows:
        print("No overlapping data across all sources — aborting.", file=sys.stderr)
        sys.exit(1)
    fetched_latest = fetched_price_rows[-1]["date"]

    cached = load_cache()
    cached_latest = cached["priceRows"][-1]["date"] if cached and cached.get("priceRows") else None

    if cached is not None and not force and cached_latest is not None and fetched_latest <= cached_latest:
        print(
            f"Latest month with all price indices available is still {cached_latest} "
            f"(just-fetched data also tops out at {fetched_latest}) -- keeping existing data."
        )
        rows, price_rows, meta = cached["rows"], cached["priceRows"], cached["meta"]
    else:
        rows, price_rows, meta = fetched_rows, fetched_price_rows, fetched_meta
        CACHE_OUT.write_text(
            json.dumps(
                {"rows": rows, "priceRows": price_rows, "meta": meta},
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        print(f"New data adopted: price indices now cover through {fetched_latest}.")

    # Always regenerate index.html from the current template, even when the
    # data itself didn't change -- so template/styling changes go out on the
    # next run without waiting for new source data.
    render_html(rows, price_rows, meta)
    print(f"Wrote {len(rows)} months ({rows[0]['date']} to {rows[-1]['date']})")
    print(
        f"Wrote {len(price_rows)} price-index months "
        f"({price_rows[0]['date']} to {price_rows[-1]['date']})"
    )
    print(f"Dashboard updated: {HTML_OUT}")


if __name__ == "__main__":
    main()
