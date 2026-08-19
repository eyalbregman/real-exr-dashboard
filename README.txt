Real EXR Dashboard
==================

What this is
-------------
An automatically-updating dashboard of the ILS/USD real exchange rate,
rebuilding the same calculation as "Monthly Real EXR, from 2000, Updated
File.xlsx" but pulling live data instead of manually pasted numbers.

Real EXR = Nominal USD/ILS rate x (US price index / Israel CPI), rebased so
both price indices share the same 100 = base year (whatever year is currently
Israel CBS's official base -- this is detected automatically each run, so it
keeps working after CBS's next base-year rebasing).

Files
-----
- index.html                 <- open this in any browser. Just the chart:
                                a year-range slider and hover-for-values.
                                (Named index.html so it also serves directly
                                from GitHub Pages -- see "Hosted copy" below.)
- fetch_and_build.py         Pulls fresh data and regenerates the .html above.
- dashboard_template.html    The page layout/chart code (do not open directly
                              -- it has a placeholder instead of real data).
- data_cache.json            Last fetched dataset, for reference/debugging.
- run_update.bat             Wrapper used by the scheduled task.
- update_log.txt             Created after the first run; log of each update.
- .github/workflows/update-dashboard.yml
                              GitHub Actions workflow that regenerates and
                              publishes index.html on GitHub Pages.

Data sources (all public, no account/API key needed)
------------------------------------------------------
- US CPI & PCEPI:  FRED (Federal Reserve Bank of St. Louis)
- Israel CPI:      CBS (Israel Central Bureau of Statistics), index 120010
- Nominal USD/ILS: Bank of Israel Fusion Data Browser (representative rate)

Automatic updates
------------------
A Windows scheduled task runs run_update.bat once a day, which re-fetches all
three sources and regenerates the dashboard. Just keep the "Real EXR
Dashboard" folder where it is -- no need to reopen anything for the data to
refresh; the .html file rewrites itself in place.

To update manually right now, double-click run_update.bat (or run
"python fetch_and_build.py" from this folder).

To change the schedule: open Task Scheduler -> Task Scheduler Library ->
"Real EXR Dashboard Update" -> Properties -> Triggers.

Hosted copy (GitHub Pages)
---------------------------
This folder is also pushed to the "real-exr-dashboard" GitHub repo, where a
GitHub Actions workflow (.github/workflows/update-dashboard.yml) runs the
same script every Tuesday and publishes index.html via GitHub Pages -- so
anyone with the link sees an always-current dashboard with nothing installed
locally. The script's own --scheduled logic still decides whether that
particular Tuesday is an actual update day; on off days the workflow just
redeploys the last generated index.html unchanged. Trigger an update from
GitHub any time via Actions -> "Update Dashboard" -> "Run workflow".
