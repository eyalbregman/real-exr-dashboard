"""
Fetches live data for the ILS/USD Real Exchange Rate dashboard and regenerates
"index.html" with the results embedded.

Sources:
  - FRED (St. Louis Fed): US CPI (CPIAUCSL) and US PCEPI (PCEPI), via the
    authenticated FRED API -- requires a free API key
    (https://fred.stlouisfed.org/docs/api/api_key.html) passed via the
    FRED_API_KEY environment variable. (The unauthenticated fredgraph.csv
    endpoint works fine from a home connection but is unreliable/blocked
    from cloud CI runner IPs, which is why this uses the real API instead.)
  - CBS (Israel Central Bureau of Statistics): Israel CPI, index 120010 (public)
  - Bank of Israel Fusion Data Browser: representative USD/ILS exchange rate (public)

Run manually with: FRED_API_KEY=xxxx python fetch_and_build.py
Intended to also run unattended on a schedule (see README.txt), and via the
GitHub Actions workflow in .github/workflows for the hosted GitHub Pages copy
(where FRED_API_KEY comes from a repo secret).
"""

import csv
import io
import json
import os
import re
import sys
import urllib.request
import urllib.error
from datetime import date, datetime, timedelta, timezone

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


def target_tuesday_for_month(year, month):
    """Tuesday of the (Sunday-Saturday) week that contains the 1st of the given month."""
    d1 = date(year, month, 1)
    days_since_sunday = (d1.weekday() + 1) % 7  # Python: Monday=0 ... Sunday=6
    sunday_of_week = d1 - timedelta(days=days_since_sunday)
    return sunday_of_week + timedelta(days=2)


def is_scheduled_run_day(today):
    """True if `today` is the Tuesday of the week containing the 1st of this,
    the previous, or the next month (covers the week straddling a month boundary)."""
    for month_offset in (-1, 0, 1):
        y, m = today.year, today.month + month_offset
        while m < 1:
            m += 12
            y -= 1
        while m > 12:
            m -= 12
            y += 1
        if target_tuesday_for_month(y, m) == today:
            return True
    return False


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
    return cpi, pcepi


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


def build_dataset():
    print("Fetching FRED (US CPI, US PCEPI)...")
    cpi_us_raw, pcepi_us_raw = fetch_fred()
    print(f"  {len(cpi_us_raw)} CPI months, {len(pcepi_us_raw)} PCEPI months")

    print("Fetching Bank of Israel (nominal USD/ILS)...")
    nominal = fetch_boi()
    print(f"  {len(nominal)} months")

    print("Fetching CBS (Israel CPI, index 120010)...")
    cpi_isr, base_year = fetch_cbs()
    print(f"  {len(cpi_isr)} months, current base year = {base_year}")

    cpi_us = rebase_to_year(cpi_us_raw, base_year)
    pcepi_us = rebase_to_year(pcepi_us_raw, base_year)

    common_dates = sorted(set(nominal) & set(cpi_us) & set(cpi_isr))
    rows = []
    for ym in common_dates:
        n = nominal[ym]
        c_us = cpi_us[ym]
        c_isr = cpi_isr[ym]
        p_us = pcepi_us.get(ym)
        real_cpi = (n * c_us / c_isr) if c_isr else None
        real_pcepi = (n * p_us / c_isr) if (p_us and c_isr) else None
        rows.append(
            {
                "date": ym,
                "nominal": round(n, 3),
                "realCPI": round(real_cpi, 3) if real_cpi is not None else None,
                "realPCEPI": round(real_pcepi, 3) if real_pcepi is not None else None,
            }
        )

    meta = {
        "baseYear": base_year,
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sources": {
            "usCpi": "FRED CPIAUCSL (Consumer Price Index for All Urban Consumers)",
            "usPcepi": "FRED PCEPI (Personal Consumption Expenditures Price Index)",
            "israelCpi": f"CBS (Israel Central Bureau of Statistics) index {CBS_INDEX_ID}",
            "nominalExr": "Bank of Israel Fusion Data Browser, representative USD/ILS rate",
        },
    }
    return rows, meta


def render_html(rows, meta):
    template = TEMPLATE.read_text(encoding="utf-8")
    payload = json.dumps({"rows": rows, "meta": meta}, ensure_ascii=False)
    html = template.replace("__DASHBOARD_DATA__", payload)
    HTML_OUT.write_text(html, encoding="utf-8")


def main():
    if "--scheduled" in sys.argv[1:]:
        today = date.today()
        if not is_scheduled_run_day(today):
            print(
                f"{today.isoformat()} is not the scheduled Tuesday "
                "(week containing the 1st of the month) -- skipping."
            )
            return

    rows, meta = build_dataset()
    if not rows:
        print("No overlapping data across all three sources — aborting.", file=sys.stderr)
        sys.exit(1)

    CACHE_OUT.write_text(
        json.dumps({"rows": rows, "meta": meta}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    render_html(rows, meta)
    print(f"Wrote {len(rows)} months ({rows[0]['date']} to {rows[-1]['date']})")
    print(f"Dashboard updated: {HTML_OUT}")


if __name__ == "__main__":
    main()
