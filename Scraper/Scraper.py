```python
from __future__ import annotations

import json
import os
import random
import re
import socket
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from patchright.sync_api import sync_playwright
from playwright_stealth import stealth_sync

PKT = timezone(timedelta(hours=5))

PROXY_SOURCES = [
    "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/all/data.txt",
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks5.txt",
]

TEST_URL = "https://httpbin.org/ip"


def log(msg: str):
    print(msg, file=sys.stderr)


def yesterday_pkt_str():
    return (datetime.now(PKT) - timedelta(days=1)).strftime("%Y-%m-%d")


def validate_host(host: str):
    try:
        ip = socket.gethostbyname(host)
        log(f"[dns] {host} -> {ip}")
    except Exception as e:
        raise RuntimeError(f"DNS failed for {host}: {e}")


def download_proxy_lists():
    proxies = set()

    for url in PROXY_SOURCES:
        try:
            log(f"[proxy] downloading {url}")

            r = requests.get(url, timeout=20)

            if r.ok:
                for line in r.text.splitlines():
                    line = line.strip()

                    if not line:
                        continue

                    if ":" not in line:
                        continue

                    proxies.add(line)

        except Exception as e:
            log(f"[proxy] failed source {url}: {e}")

    return list(proxies)


def build_proxy_candidates(raw_proxies):
    out = []

    for p in raw_proxies:

        if p.startswith("http://") or p.startswith("https://"):
            out.append(p)
            continue

        if p.startswith("socks5://"):
            out.append(p)
            continue

        out.append(f"http://{p}")
        out.append(f"socks5://{p}")

    random.shuffle(out)

    return out


def test_proxy(proxy_url):
    try:
        proxies = {
            "http": proxy_url,
            "https": proxy_url,
        }

        r = requests.get(
            TEST_URL,
            proxies=proxies,
            timeout=12,
        )

        if r.ok:
            ip = r.json().get("origin")

            log(f"[proxy] working {proxy_url} -> {ip}")

            return True

    except Exception:
        return False

    return False


def get_working_proxy():
    raw = download_proxy_lists()

    if not raw:
        raise RuntimeError("No proxies downloaded.")

    candidates = build_proxy_candidates(raw)

    log(f"[proxy] testing {len(candidates)} proxies")

    for proxy in candidates[:80]:

        if test_proxy(proxy):
            return proxy

    return None


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


def login(page, email, password):

    login_url = "https://sellercenter.daraz.pk/apps/seller/login"

    log(f"[login] opening {login_url}")

    page.goto(
        login_url,
        wait_until="domcontentloaded",
        timeout=120000,
    )

    page.wait_for_timeout(6000)

    dump_debug(page, "before_login")

    selectors = [
        'input[name="account"]',
        'input[type="email"]',
        'input[name="email"]',
        'input[type="text"]',
    ]

    email_input = None

    for sel in selectors:
        try:
            loc = page.locator(sel).first

            loc.wait_for(timeout=4000)

            email_input = loc

            break

        except Exception:
            continue

    if not email_input:
        dump_debug(page, "email_not_found")
        raise RuntimeError("Email input not found.")

    email_input.fill(email)

    pw = page.locator('input[type="password"]').first

    pw.fill(password)

    buttons = [
        'button[type="submit"]',
        'button:has-text("Login")',
        'button:has-text("Sign In")',
    ]

    clicked = False

    for sel in buttons:

        btn = page.locator(sel).first

        if btn.count():

            btn.click()

            clicked = True

            break

    if not clicked:
        raise RuntimeError("Login button not found.")

    try:
        page.wait_for_url(
            re.compile(r"^(?!.*login).*$", re.I),
            timeout=90000,
        )

    except Exception:
        dump_debug(page, "login_failed")
        raise

    log("[login] success")


def collect_metrics(page):

    dashboard_url = "https://sellercenter.daraz.pk/ba/dashboard"

    page.goto(
        dashboard_url,
        wait_until="domcontentloaded",
        timeout=120000,
    )

    page.wait_for_timeout(8000)

    dump_debug(page, "dashboard")

    if "login" in page.url.lower():
        raise RuntimeError(
            "Still redirected to login. "
            "Proxy likely blocked."
        )

    cards = page.locator(".D3c3eK")

    count = cards.count()

    log(f"[dashboard] cards: {count}")

    metrics = {}

    for i in range(count):

        try:
            card = cards.nth(i)

            title = card.locator(".A5EvH0").inner_text().strip()

            try:
                value = card.locator(".dlOQtX").inner_text().strip()
            except Exception:
                value = None

            metrics[title] = value

        except Exception:
            continue

    return metrics


def main():

    email = os.getenv("MART_EMAIL")
    password = os.getenv("MART_PASSWORD")
    cookies_raw = os.getenv("MART_COOKIES", "").strip()

    validate_host("sellercenter.daraz.pk")

    proxy_url = get_working_proxy()

    if not proxy_url:
        raise RuntimeError(
            "No working proxies found."
        )

    log(f"[proxy] selected {proxy_url}")

    with sync_playwright() as pw:

        browser = pw.chromium.launch(
            headless=True,
            proxy={
                "server": proxy_url
            },
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-sandbox",
            ],
        )

        context = browser.new_context(
            viewport={
                "width": 1366,
                "height": 768,
            },
            timezone_id="Asia/Karachi",
            locale="en-US",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            ),
        )

        page = context.new_page()

        stealth_sync(page)

        if cookies_raw:

            cookies = json.loads(cookies_raw)

            context.add_cookies(cookies)

            log("[cookies] loaded")

        else:

            if not email or not password:
                raise RuntimeError(
                    "Provide cookies or credentials."
                )

            login(page, email, password)

        metrics = collect_metrics(page)

        browser.close()

    if not metrics:
        raise RuntimeError("No metrics scraped.")

    Path("data").mkdir(exist_ok=True)

    out = {
        "data_date": yesterday_pkt_str(),
        "scraped_at": datetime.now(PKT).isoformat(),
        "metric_count": len(metrics),
        "metrics": metrics,
    }

    out_file = Path("data") / f"{yesterday_pkt_str()}.json"

    out_file.write_text(
        json.dumps(out, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    log(f"[done] wrote {out_file}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
```
