
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

# =========================
# PATCHRIGHT / PLAYWRIGHT
# =========================

_USING_PATCHRIGHT = False

try:
    from patchright.sync_api import sync_playwright
    from patchright.sync_api import TimeoutError as PWTimeout

    _USING_PATCHRIGHT = True

except ImportError:
    from playwright.sync_api import sync_playwright
    from playwright.sync_api import TimeoutError as PWTimeout

# =========================
# STEALTH
# =========================

try:
    from playwright_stealth import stealth_sync

    def apply_stealth(page):
        try:
            stealth_sync(page)
        except Exception as e:
            print(f"[stealth] failed: {e}", file=sys.stderr)

except Exception:

    def apply_stealth(page):
        pass

# =========================
# CONFIG
# =========================

PKT = timezone(timedelta(hours=5))

PROXY_SOURCES = [
    "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/all/data.txt",
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
]

TARGET_TEST_URL = "https://sellercenter.daraz.pk/"

LOGIN_URL = "https://sellercenter.daraz.pk/apps/seller/login"

DASHBOARD_URL = "https://sellercenter.daraz.pk/ba/dashboard"

# =========================
# HELPERS
# =========================


def log(msg: str):
    print(msg, file=sys.stderr)


def yesterday_pkt_str():
    return (
        datetime.now(PKT) - timedelta(days=1)
    ).strftime("%Y-%m-%d")


def validate_host(host: str):

    try:
        ip = socket.gethostbyname(host)

        log(f"[dns] {host} -> {ip}")

    except Exception as e:

        raise RuntimeError(
            f"DNS resolution failed for {host}: {e}"
        )


# =========================
# DEBUG DUMP
# =========================


def dump_debug(page, label: str):

    debug_dir = Path(__file__).resolve().parent / "debug"

    debug_dir.mkdir(exist_ok=True)

    try:
        page.screenshot(
            path=str(debug_dir / f"{label}.png"),
            full_page=True,
        )

        log(f"[debug] screenshot: {label}.png")

    except Exception as e:
        log(f"[debug] screenshot failed: {e}")

    try:
        html = page.content()

        (debug_dir / f"{label}.html").write_text(
            html,
            encoding="utf-8",
        )

        log(f"[debug] html: {label}.html")

    except Exception as e:
        log(f"[debug] html dump failed: {e}")


# =========================
# PROXY SYSTEM
# =========================


def download_proxy_lists():

    proxies = set()

    for url in PROXY_SOURCES:

        try:

            log(f"[proxy] downloading {url}")

            r = requests.get(
                url,
                timeout=20,
            )

            if r.ok:

                for line in r.text.splitlines():

                    line = line.strip()

                    if not line:
                        continue

                    if ":" not in line:
                        continue

                    if line.startswith("#"):
                        continue

                    proxies.add(line)

        except Exception as e:

            log(f"[proxy] source failed: {e}")

    return list(proxies)


def normalize_proxy(proxy: str):

    if proxy.startswith("http://"):
        return proxy

    if proxy.startswith("https://"):
        return proxy

    if proxy.startswith("socks5://"):
        return proxy

    return f"http://{proxy}"


def test_proxy(proxy_url: str):

    proxies = {
        "http": proxy_url,
        "https": proxy_url,
    }

    try:

        start = time.time()

        r = requests.get(
            TARGET_TEST_URL,
            proxies=proxies,
            timeout=12,
            allow_redirects=True,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/126.0.0.0 Safari/537.36"
                )
            },
        )

        elapsed = time.time() - start

        if elapsed > 8:
            return False

        if r.status_code >= 400:
            return False

        body = r.text.lower()

        if "daraz" not in body:
            return False

        log(f"[proxy] GOOD {proxy_url} ({elapsed:.1f}s)")

        return True

    except Exception:

        return False


def get_working_proxy():

    env_proxy = (
        os.getenv("MART_PROXY") or ""
    ).strip()

    # PRIORITY:
    # explicit proxy from GitHub secrets

    if env_proxy:

        log(f"[proxy] testing MART_PROXY")

        if test_proxy(env_proxy):

            log(f"[proxy] using MART_PROXY")

            return env_proxy

        else:

            log("[proxy] MART_PROXY failed")

    # fallback to free proxy scraping

    raw = download_proxy_lists()

    if not raw:
        return None

    random.shuffle(raw)

    tested = 0

    for proxy in raw:

        tested += 1

        if tested > 150:
            break

        proxy_url = normalize_proxy(proxy)

        log(f"[proxy] testing {proxy_url}")

        if test_proxy(proxy_url):

            return proxy_url

    return None


# =========================
# LOGIN
# =========================


def login(page, email: str, password: str):

    log(f"[login] opening {LOGIN_URL}")

    page.goto(
        LOGIN_URL,
        wait_until="domcontentloaded",
        timeout=45000,
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

        try:

            loc = page.locator(sel).first

            loc.wait_for(
                timeout=4000
            )

            email_input = loc

            break

        except Exception:
            continue

    if not email_input:

        dump_debug(page, "email_not_found")

        raise RuntimeError(
            "Could not locate email field."
        )

    email_input.fill(email)

    pw_input = page.locator(
        'input[type="password"]'
    ).first

    pw_input.fill(password)

    submit_selectors = [
        'button[type="submit"]',
        'button:has-text("Login")',
        'button:has-text("Sign In")',
    ]

    clicked = False

    for sel in submit_selectors:

        btn = page.locator(sel).first

        if btn.count():

            btn.click()

            clicked = True

            break

    if not clicked:

        raise RuntimeError(
            "Could not find login button."
        )

    try:

        page.wait_for_url(
            re.compile(
                r"^(?!.*login).*$",
                re.I,
            ),
            timeout=90000,
        )

    except Exception:

        dump_debug(page, "login_failed")

        raise

    log("[login] success")


# =========================
# SCRAPE DASHBOARD
# =========================


def collect_metrics(page):

    log("[dashboard] opening dashboard")

    page.goto(
        DASHBOARD_URL,
        wait_until="domcontentloaded",
        timeout=45000,
    )

    page.wait_for_timeout(8000)

    dump_debug(page, "dashboard")

    current = page.url.lower()

    if "login" in current:

        raise RuntimeError(
            "Redirected back to login. "
            "Session invalid or blocked."
        )

    cards = page.locator(".D3c3eK")

    count = cards.count()

    log(f"[dashboard] cards: {count}")

    metrics = {}

    for i in range(count):

        try:

            card = cards.nth(i)

            title = (
                card.locator(".A5EvH0")
                .inner_text()
                .strip()
            )

            value = None

            try:
                value = (
                    card.locator(".dlOQtX")
                    .inner_text()
                    .strip()
                )

            except Exception:
                pass

            metrics[title] = value

        except Exception:
            continue

    return metrics


# =========================
# MAIN
# =========================


def main():

    email = os.getenv("MART_EMAIL")

    password = os.getenv("MART_PASSWORD")

    cookies_raw = (
        os.getenv("MART_COOKIES") or ""
    ).strip()

    validate_host("sellercenter.daraz.pk")

    proxy_url = get_working_proxy()

    if proxy_url:

        log(f"[proxy] selected {proxy_url}")

    else:

        log(
            "[proxy] no working proxy found "
            "- using direct connection"
        )

    with sync_playwright() as pw:

        browser = pw.chromium.launch(
            headless=True,
            proxy=(
                {"server": proxy_url}
                if proxy_url
                else None
            ),
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-sandbox",
                "--disable-features=IsolateOrigins,site-per-process",
            ],
        )

        context = browser.new_context(
            viewport={
                "width": 1366,
                "height": 768,
            },
            locale="en-US",
            timezone_id="Asia/Karachi",
            user_agent=(
                "Mozilla/5.0 "
                "(Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            ),
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
            },
        )

        page = context.new_page()

        apply_stealth(page)

        # =========================
        # COOKIES
        # =========================

        if cookies_raw:

            try:

                cookies = json.loads(
                    cookies_raw
                )

                context.add_cookies(
                    cookies
                )

                log("[cookies] loaded")

            except Exception as e:

                raise RuntimeError(
                    f"Invalid MART_COOKIES: {e}"
                )

        # =========================
        # LOGIN
        # =========================

        else:

            if not email or not password:

                raise RuntimeError(
                    "Provide MART_COOKIES or credentials."
                )

            login(
                page,
                email,
                password,
            )

        # =========================
        # SCRAPE
        # =========================

        metrics = collect_metrics(page)

        browser.close()

    if not metrics:

        raise RuntimeError(
            "No metrics scraped."
        )

    data_dir = (
        Path(__file__).resolve().parent
        / "data"
    )

    data_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    payload = {
        "data_date": yesterday_pkt_str(),
        "scraped_at": datetime.now(
            PKT
        ).isoformat(),
        "metric_count": len(metrics),
        "metrics": metrics,
    }

    out_file = (
        data_dir
        / f"{yesterday_pkt_str()}.json"
    )

    out_file.write_text(
        json.dumps(
            payload,
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    log(f"[done] wrote {out_file}")

    return 0


if __name__ == "__main__":
    sys.exit(main())

