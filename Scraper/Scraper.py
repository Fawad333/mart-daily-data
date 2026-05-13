```python
from __future__ import annotations

import json
import os
import re
import socket
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from patchright.sync_api import sync_playwright
from playwright_stealth import stealth_sync

PKT = timezone(timedelta(hours=5))


def log(msg: str):
    print(msg, file=sys.stderr)


def yesterday_pkt_str():
    return (datetime.now(PKT) - timedelta(days=1)).strftime("%Y-%m-%d")


def validate_host(host: str):
    try:
        ip = socket.gethostbyname(host)
        log(f"[dns] {host} -> {ip}")
        return ip
    except Exception as e:
        raise RuntimeError(
            f"DNS resolution failed for '{host}'. "
            f"This usually means:\n"
            f"- invalid MART_HOST\n"
            f"- Daraz blocking GitHub cloud DNS\n"
            f"- internal/private hostname\n"
            f"- proxy/VPN required\n\n"
            f"Original error: {e}"
        )


def dump_debug(page, label: str):
    debug_dir = Path("debug")
    debug_dir.mkdir(exist_ok=True)

    try:
        page.screenshot(
            path=str(debug_dir / f"{label}.png"),
            full_page=True
        )
    except Exception:
        pass

    try:
        html = page.content()
        (debug_dir / f"{label}.html").write_text(
            html,
            encoding="utf-8"
        )
    except Exception:
        pass


def login(page, email: str, password: str):
    login_url = "https://sellercenter.daraz.pk/apps/seller/login"

    log(f"[login] opening {login_url}")

    page.goto(
        login_url,
        wait_until="domcontentloaded",
        timeout=120000
    )

    page.wait_for_timeout(5000)

    dump_debug(page, "before_login")

    email_selectors = [
        'input[name="account"]',
        'input[type="email"]',
        'input[name="email"]',
        'input[type="text"]',
    ]

    email_input = None

    for sel in email_selectors:
        loc = page.locator(sel).first
        try:
            loc.wait_for(timeout=5000)
            email_input = loc
            break
        except Exception:
            continue

    if not email_input:
        dump_debug(page, "email_not_found")
        raise RuntimeError("Could not locate email field.")

    email_input.fill(email)

    pw_input = page.locator('input[type="password"]').first
    pw_input.fill(password)

    submit_btns = [
        'button[type="submit"]',
        'button:has-text("Login")',
        'button:has-text("Sign In")',
    ]

    clicked = False

    for sel in submit_btns:
        btn = page.locator(sel).first
        if btn.count():
            btn.click()
            clicked = True
            break

    if not clicked:
        raise RuntimeError("Could not locate login button.")

    try:
        page.wait_for_url(
            re.compile(r"^(?!.*login).*$", re.I),
            timeout=90000
        )
    except Exception:
        dump_debug(page, "login_failed")
        raise

    log("[login] success")


def collect_metrics(page):
    dashboard_url = "https://sellercenter.daraz.pk/ba/dashboard"

    log("[dashboard] opening dashboard")

    page.goto(
        dashboard_url,
        wait_until="domcontentloaded",
        timeout=120000
    )

    page.wait_for_timeout(8000)

    dump_debug(page, "dashboard")

    current = page.url.lower()

    if "login" in current:
        raise RuntimeError(
            "Still redirected to login page. "
            "Daraz likely blocked authentication."
        )

    cards = page.locator(".D3c3eK")

    count = cards.count()

    log(f"[dashboard] metric cards: {count}")

    data = {}

    for i in range(count):
        try:
            card = cards.nth(i)

            title = card.locator(".A5EvH0").inner_text().strip()

            value = None

            try:
                value = card.locator(".dlOQtX").inner_text().strip()
            except Exception:
                pass

            data[title] = value

        except Exception:
            continue

    return data


def main():
    email = os.getenv("MART_EMAIL")
    password = os.getenv("MART_PASSWORD")
    proxy_url = os.getenv("MART_PROXY", "").strip()
    cookies_raw = os.getenv("MART_COOKIES", "").strip()

    host = "sellercenter.daraz.pk"

    validate_host(host)

    proxy = None

    if proxy_url:
        proxy = {"server": proxy_url}
        log(f"[proxy] using proxy {proxy_url}")

    with sync_playwright() as pw:

        browser = pw.chromium.launch(
            headless=True,
            proxy=proxy,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-sandbox",
            ]
        )

        context = browser.new_context(
            viewport={"width": 1366, "height": 768},
            locale="en-US",
            timezone_id="Asia/Karachi",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            ),
        )

        page = context.new_page()

        stealth_sync(page)

        if cookies_raw:
            try:
                cookies = json.loads(cookies_raw)
                context.add_cookies(cookies)
                log("[cookies] loaded")
            except Exception as e:
                raise RuntimeError(f"Invalid cookies JSON: {e}")

        else:
            if not email or not password:
                raise RuntimeError(
                    "Provide MART_COOKIES or email/password."
                )

            login(page, email, password)

        metrics = collect_metrics(page)

        browser.close()

    if not metrics:
        raise RuntimeError("No metrics collected.")

    out = {
        "data_date": yesterday_pkt_str(),
        "scraped_at": datetime.now(PKT).isoformat(),
        "metrics": metrics,
    }

    Path("data").mkdir(exist_ok=True)

    out_file = Path("data") / f"{yesterday_pkt_str()}.json"

    out_file.write_text(
        json.dumps(out, indent=2, ensure_ascii=False),
        encoding="utf-8"
    )

    log(f"[done] wrote {out_file}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
```
