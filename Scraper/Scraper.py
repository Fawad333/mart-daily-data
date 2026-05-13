"""
Mart admin -- Business Advisor daily scraper.

Logs into the Mart seller portal with credentials from MART_EMAIL /
MART_PASSWORD environment variables and the hostname from MART_HOST,
opens the Business Advisor dashboard (/ba/dashboard), scrapes the
"Key Metrics" panel (every metric card present), and writes the result
to ``data/<DATA_DATE>.json``.

``DATA_DATE`` is the actual data date shown on the dashboard (the
"Yesterday (YYYY-MM-DD ~ YYYY-MM-DD)" range label), falling back to
"yesterday in PKT" if that label can't be read.

The script exits non-zero on any failure so the GitHub Actions job
surfaces the error.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

# Pakistan Standard Time (UTC+5, no DST).
PKT = timezone(timedelta(hours=5))

# JavaScript executed inside the page to extract every visible metric card.
# Runs inside the page because (a) it avoids a roundtrip per locator and
# (b) native DOM queries are easier than Playwright locators when each
# card has multiple nested helper icons.
EXTRACT_JS = r"""
() => {
  const cards = document.querySelectorAll('.D3c3eK');
  const out = [];
  cards.forEach((card) => {
    const titleEl = card.querySelector('.A5EvH0');
    if (!titleEl) return;

    // Title also contains the help-tooltip SVG; keep only direct text nodes.
    let title = '';
    titleEl.childNodes.forEach((n) => {
      if (n.nodeType === Node.TEXT_NODE) title += n.textContent;
    });
    title = title.trim();
    if (!title) {
      title = (titleEl.textContent || '').trim().split('\n')[0].trim();
    }

    const currencyEl = card.querySelector('.Uf5RJX');
    const valueEl = card.querySelector('.dlOQtX');

    const comparisons = {};
    card.querySelectorAll('figure.PZa_H3').forEach((fig) => {
      const labelEl = fig.querySelector('.xAkdsw');
      const label = labelEl ? labelEl.textContent.trim() : '';

      const pctEl = fig.querySelector('.p0ERYN');
      const percent = pctEl ? pctEl.textContent.trim() : null;

      let direction = null;
      if (percent) {
        // The portal renders the same SVG path for both arrows; the
        // "down" arrow is flipped via transform="...scale(1, -1)..." on
        // the <path>.
        const path = fig.querySelector('svg path');
        const t = path ? (path.getAttribute('transform') || '') : '';
        direction = /scale\(\s*1\s*,\s*-1\s*\)/.test(t) ? 'down' : 'up';
      }

      comparisons[label] = { percent, direction };
    });

    out.push({
      title,
      currency: currencyEl ? currencyEl.textContent.trim() : null,
      value: valueEl ? valueEl.textContent.trim() : null,
      comparisons,
    });
  });
  return out;
}
"""


def yesterday_pkt_str() -> str:
    """Return yesterday's date (YYYY-MM-DD) in Pakistan Standard Time."""
    return (datetime.now(PKT) - timedelta(days=1)).strftime("%Y-%m-%d")


def parse_dashboard_date(text: str) -> str | None:
    """Pull a YYYY-MM-DD out of a string like 'Yesterday (2026-05-13 ~ ...)'."""
    if not text:
        return None
    m = re.search(r"(\d{4}-\d{2}-\d{2})", text)
    return m.group(1) if m else None


def _dump_debug(page, label: str) -> None:
    """Save the current page HTML + screenshot for post-mortem debugging."""
    debug_dir = Path(__file__).resolve().parent / "debug"
    debug_dir.mkdir(exist_ok=True)
    try:
        (debug_dir / f"{label}.html").write_text(page.content(), encoding="utf-8")
    except Exception as e:
        print(f"[debug] failed to dump html: {e}", file=sys.stderr)
    try:
        page.screenshot(path=str(debug_dir / f"{label}.png"), full_page=True)
    except Exception as e:
        print(f"[debug] failed to screenshot: {e}", file=sys.stderr)

    # Also log to stdout so you can see it in the Actions log directly.
    try:
        url = page.url
        title = page.title()
        print(f"[debug] url   = {url}", file=sys.stderr)
        print(f"[debug] title = {title!r}", file=sys.stderr)
        inputs = page.evaluate(
            "() => Array.from(document.querySelectorAll('input,iframe')).map(el => ({"
            "tag: el.tagName.toLowerCase(),"
            "type: el.getAttribute('type'),"
            "name: el.getAttribute('name'),"
            "id:   el.id || null,"
            "cls:  el.getAttribute('class'),"
            "placeholder: el.getAttribute('placeholder'),"
            "src:  el.getAttribute('src'),"
            "}))"
        )
        print(f"[debug] inputs+iframes ({len(inputs)}):", file=sys.stderr)
        for el in inputs:
            print(f"  {el}", file=sys.stderr)
    except Exception as e:
        print(f"[debug] failed to enumerate inputs: {e}", file=sys.stderr)


def login(page, login_url: str, email: str, password: str) -> None:
    """Sign in to the Mart seller portal."""
    # Try the deep login URL first (skips a redirect hop).
    deep_login_url = login_url.rstrip("/") + "/apps/seller/login"
    page.goto(deep_login_url, wait_until="domcontentloaded", timeout=60_000)
    # Give the SPA a moment to mount its form even after DOMContentLoaded.
    page.wait_for_timeout(2_500)

    # The portal usually uses name="account" for the email/phone field, but
    # the markup has changed before -- try a wide net of likely selectors.
    email_selectors = [
        'input[name="account"]',
        'input[id="account"]',
        'input[name="username"]',
        'input[name="email"]',
        'input[name="loginName"]',
        'input[type="email"]',
        'input[placeholder*="mail" i]',
        'input[placeholder*="account" i]',
        'input[placeholder*="phone" i]',
        'input[placeholder*="member" i]',
        'input.next-input',                # Ant/Next design
        'form input[type="text"]',
        'input[type="text"]',
    ]
    email_input = None
    for sel in email_selectors:
        try:
            page.wait_for_selector(sel, timeout=4_000, state="visible")
            email_input = page.locator(sel).first
            break
        except PWTimeout:
            continue
    if email_input is None:
        # Maybe the form is inside an iframe (Daraz wraps some flows that way).
        for frame in page.frames:
            try:
                for sel in email_selectors:
                    if frame.locator(sel).count():
                        email_input = frame.locator(sel).first
                        print(f"[debug] found email input inside iframe url={frame.url}", file=sys.stderr)
                        break
                if email_input is not None:
                    break
            except Exception:
                continue
    if email_input is None:
        _dump_debug(page, "login-failure")
        raise RuntimeError("Could not locate the email/account input on the login page.")

    email_input.fill(email)

    pw_input = page.locator('input[type="password"]').first
    pw_input.wait_for(timeout=10_000)
    pw_input.fill(password)

    # Submit. Try the main submit button first, then visible-text fallbacks.
    submit_selectors = [
        'button[type="submit"]',
        'button:has-text("Login")',
        'button:has-text("Sign In")',
        'button:has-text("Sign in")',
        'button:has-text("Submit")',
    ]
    for sel in submit_selectors:
        btn = page.locator(sel).first
        if btn.count():
            btn.click()
            break
    else:
        raise RuntimeError("Could not find a submit button on the login form.")

    # Wait until we leave the login page.
    page.wait_for_url(re.compile(r"^(?!.*login).*$", re.IGNORECASE), timeout=60_000)


def collect_metrics(page, dashboard_url: str) -> tuple[dict, str | None]:
    """Navigate to the BA dashboard and scrape the Key Metrics panel."""
    page.goto(dashboard_url, wait_until="domcontentloaded", timeout=60_000)

    # Wait for at least one metric card to mount.
    page.wait_for_selector(".D3c3eK .A5EvH0", timeout=60_000)
    # Give React a beat to populate values + comparisons.
    page.wait_for_timeout(4_000)

    # Grab the "Yesterday (2026-05-13 ~ 2026-05-13)" label so we can derive
    # the canonical data date from the dashboard itself.
    range_text = ""
    for sel in [
        'text=/Yesterday\\s*\\(/i',
        'div:has-text("Yesterday (")',
    ]:
        loc = page.locator(sel).first
        try:
            range_text = loc.inner_text(timeout=2_000)
            if range_text:
                break
        except Exception:
            continue
    data_date = parse_dashboard_date(range_text)

    seen: dict[str, dict] = {}
    last_count = -1
    # The carousel only renders ~5 cards at a time visually; loop in case
    # we need to advance through pages to surface them all.
    for _ in range(8):
        cards = page.evaluate(EXTRACT_JS)
        for c in cards:
            title = c.get("title")
            if title and title not in seen:
                seen[title] = c

        if len(seen) >= 15:
            break

        if len(seen) == last_count:
            # Nothing new -- try clicking "next".
            clicked = False
            next_selectors = [
                '.CHebL5 .anticon-right',
                '.CHebL5 [aria-label="right"]',
                '.CHebL5 .slick-next',
                '.CHebL5 button:has(.anticon-right)',
                '.CHebL5 svg[aria-label="right"]',
                'button:has(span[aria-label="right"])',
            ]
            for sel in next_selectors:
                loc = page.locator(sel).first
                if loc.count():
                    try:
                        loc.click(timeout=2_000)
                        clicked = True
                        page.wait_for_timeout(800)
                        break
                    except Exception:
                        continue
            if not clicked:
                break
        last_count = len(seen)

    return seen, data_date


def main() -> int:
    email = os.environ.get("MART_EMAIL")
    password = os.environ.get("MART_PASSWORD")
    host = (os.environ.get("MART_HOST") or "").strip()
    if not email or not password or not host:
        print(
            "ERROR: MART_EMAIL, MART_PASSWORD and MART_HOST environment "
            "variables are required.",
            file=sys.stderr,
        )
        return 1

    # Be forgiving: accept "sellercenter.example", "https://sellercenter.example",
    # or "https://sellercenter.example/" all as the same hostname.
    host = re.sub(r"^https?://", "", host, flags=re.IGNORECASE).strip("/")

    login_url = f"https://{host}/"
    dashboard_url = f"https://{host}/ba/dashboard"

    repo_root = Path(__file__).resolve().parent
    data_dir = repo_root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        context = browser.new_context(
            viewport={"width": 1440, "height": 900},
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="en-US",
        )
        page = context.new_page()
        try:
            print("[mart] logging in...")
            login(page, login_url, email, password)
            print("[mart] login OK, collecting Business Advisor metrics...")
            metrics, dashboard_date = collect_metrics(page, dashboard_url)
        finally:
            context.close()
            browser.close()

    if not metrics:
        print("ERROR: no metrics were scraped.", file=sys.stderr)
        return 2

    data_date = dashboard_date or yesterday_pkt_str()

    payload = {
        "data_date": data_date,
        "scraped_at": datetime.now(PKT).isoformat(),
        "source": dashboard_url,
        "metric_count": len(metrics),
        "metrics": metrics,
    }

    out_file = data_dir / f"{data_date}.json"
    out_file.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    print(f"[mart] wrote {out_file} ({len(metrics)} metrics)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
