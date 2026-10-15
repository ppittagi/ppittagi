#!/usr/bin/env python3
"""
Screenshot a Superset dashboard (optionally one tab) with a local headless Chromium, so the result
can be compared against the source (Tableau) image without configuring server-side screenshots.

  pip install playwright && playwright install chromium      # once
  python screenshot_dashboard.py study-enrollment-safety --out overview.png
  python screenshot_dashboard.py 7 --tab Safety --out safety.png --width 1600
  python screenshot_dashboard.py 7 --all-tabs --out dash.png     # dash_Overview.png, dash_Safety.png ...

Uses SUPERSET_URL / SUPERSET_USERNAME / SUPERSET_PASSWORD. Logs in through the normal login form
(works with DB auth; for SSO run with --cookie "session=<value>" copied from your browser).
Set CHROMIUM_PATH to use a specific browser binary.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys


async def run(a: argparse.Namespace) -> int:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        sys.exit("pip install playwright && playwright install chromium")
    url = (a.url or os.environ.get("SUPERSET_URL", "http://localhost:8088")).rstrip("/")
    user = a.user or os.environ.get("SUPERSET_USERNAME", "admin")
    password = a.password or os.environ.get("SUPERSET_PASSWORD", "admin")
    async with async_playwright() as p:
        kw = {"args": ["--no-sandbox"]}
        if os.environ.get("CHROMIUM_PATH"):
            kw["executable_path"] = os.environ["CHROMIUM_PATH"]
        browser = await p.chromium.launch(**kw)
        ctx = await browser.new_context(viewport={"width": a.width, "height": a.height})
        if a.cookie:
            name, _, value = a.cookie.partition("=")
            await ctx.add_cookies([{"name": name, "value": value, "url": url}])
        page = await ctx.new_page()
        if not a.cookie:
            await page.goto(f"{url}/login/")
            await page.fill("#username, input[name=username]", user)
            await page.fill("#password, input[name=password]", password)
            await page.click("button[type=submit], input[type=submit]")
            await page.wait_for_load_state("networkidle")
        await page.goto(f"{url}/superset/dashboard/{a.dashboard}/?standalone={a.standalone}"
                        + (f"&{a.query}" if a.query else ""))
        await page.mouse.move(0, a.height - 1)   # keep hover tooltips ("Click to edit") out of the shot
        await page.wait_for_timeout(a.wait * 1000)
        tabs = await page.locator("[role=tab]").all_inner_texts()
        tabs = [t.strip() for t in tabs if t.strip()]
        print(f"tabs: {tabs}", file=sys.stderr)

        async def shoot(path: str) -> None:
            await page.wait_for_timeout(1500)
            await page.screenshot(path=path, full_page=True)
            errors = await page.locator("text=/Unexpected error|error while rendering|No results/i").count()
            print(f"saved {path} (chart error banners: {errors})", file=sys.stderr)

        base, ext = os.path.splitext(a.out)
        ext = ext or ".png"
        if a.all_tabs and tabs:
            for i, t in enumerate(tabs):
                await page.locator("[role=tab]").nth(i).click()
                await page.wait_for_timeout(a.wait * 1000)
                await shoot(f"{base}_{re.sub(r'[^A-Za-z0-9가-힣]+', '_', t)}{ext}")
        else:
            if a.tab:
                await page.get_by_role("tab", name=a.tab).first.click()
                await page.wait_for_timeout(a.wait * 1000)
            await shoot(base + ext)
        await browser.close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dashboard", help="dashboard id or slug")
    ap.add_argument("--out", default="dashboard.png")
    ap.add_argument("--tab", help="tab title to open before the screenshot")
    ap.add_argument("--all-tabs", action="store_true")
    ap.add_argument("--width", type=int, default=1500)
    ap.add_argument("--height", type=int, default=1000)
    ap.add_argument("--wait", type=int, default=10, help="seconds to let charts load")
    ap.add_argument("--standalone", default="1", help="0 shows the Superset nav bar; 1 hides it")
    ap.add_argument("--cookie", help='"session=<value>" to reuse an SSO browser session')
    ap.add_argument("--query", help="extra URL query, e.g. native_filters=(...) to screenshot a filter state")
    ap.add_argument("--url"), ap.add_argument("--user"), ap.add_argument("--password")
    return asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
