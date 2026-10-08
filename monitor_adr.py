#!/usr/bin/env python3
"""
American Debt Relief - website uptime monitor (runs every 15 minutes)
=======================================================================
The site counts as "UP" only when ALL of these pass, on BOTH desktop and mobile:

  Homepage (https://www.americandebtrelief.com/)
    1. The American Debt Relief logo image loads and is visible in the header
       (top-left on desktop; top of the screen on mobile)
    2. The heading "How our debt settlement program works" is visible
    3. The footer line "AMERICAN DEBT RELIEF, LLC (c) [year] All Rights Reserved" is visible
  Free Debt Assessment page (https://www.americandebtrelief.com/debt-assessment/)
    4. A fillable assessment form appears. The monitor never fills it in or submits it.

Alerting (Microsoft Teams):
  - First failure  -> waits 60s and re-checks. If it still fails, posts a red DOWN alert with the likely
                      cause, what failed, and what to check first.
  - Still down     -> posts a reminder every 60 minutes (not every 15) so the chat isn't flooded.
  - Back up        -> posts a green BACK UP message with how long the site was down.
The up/down status is remembered between runs in state/status.json.

Usage:
  python monitor_adr.py                       # normal check
  python monitor_adr.py --test-alert down     # send a sample DOWN alert to Teams
  python monitor_adr.py --test-alert recovery # send a sample BACK UP message to Teams
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import socket
import ssl
import sys
import time
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import requests

# ------------------------------------------------------------------ settings
SITE = os.getenv("MONITOR_SITE", "https://www.americandebtrelief.com").rstrip("/")
TARGETS = {
    "home": {"label": "Homepage", "url": f"{SITE}/"},
    "assessment": {"label": "Free Debt Assessment form", "url": f"{SITE}/debt-assessment/"},
}
VIEWS = ("desktop", "mobile")
WEBHOOK = os.getenv("TEAMS_WEBHOOK_URL", "").strip()
RETRY_DELAY = int(os.getenv("RETRY_DELAY_SECONDS", "60"))
REMINDER_MINUTES = int(os.getenv("REMINDER_MINUTES", "60"))
STATE_FILE = Path(os.getenv("STATE_FILE", "state/status.json"))
SHOT_DIR = Path(os.getenv("SCREENSHOT_DIR", "screenshots"))
RUN_URL = (
    f"{os.getenv('GITHUB_SERVER_URL')}/{os.getenv('GITHUB_REPOSITORY')}/actions/runs/{os.getenv('GITHUB_RUN_ID')}"
    if os.getenv("GITHUB_RUN_ID") else ""
)
MONITOR_TAG = "ADR-UptimeMonitor/2.0"  # firewall teams can allowlist this
DESKTOP_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              f"Chrome/130.0.0.0 Safari/537.36 {MONITOR_TAG}")
CT = ZoneInfo("America/Chicago")

# What we look for. Patterns ignore capitalization and spacing differences.
LOGO_FILE_HINT = "ADR-Logo"  # header logo: /wp-content/uploads/2019/11/ADR-Logo-WHITE.png
HEADING_PATTERN = r"how\s+our\s+debt\s+settlement\s+program\s+works"
# The year is optional. The page HTML has no year (JavaScript adds it), and a hard-coded
# year would cause a false alarm every January 1.
FOOTER_PATTERN = r"american\s+debt\s+relief,?\s+llc\s*(?:©|\(c\)|copyright)\s*(?:\d{4}\s*)?all\s+rights\s+reserved"
# Embedded frames that are never "the assessment form" (CAPTCHA boxes, chat widgets, ads, analytics)
IGNORE_FRAME_HOSTS = ("recaptcha", "hcaptcha", "challenges.cloudflare", "google.com", "doubleclick", "facebook",
                      "livechat", "intercom", "drift", "zendesk", "zopim", "tawk", "hubspot", "youtube",
                      "vimeo", "trustpilot", "bbb.org", "clarity.ms", "hotjar")

CHECK_LABELS = {
    "logo": "Logo", "heading": "Heading", "footer": "Footer", "form": "Form",
}
TARGET_CHECKS = {"home": ("logo", "heading", "footer"), "assessment": ("form",)}

ERROR_SIGNATURES = {
    "error establishing a database connection":
        "WordPress can't reach its database (the database server is down or overloaded, or the DB credentials changed).",
    "there has been a critical error on this website":
        "WordPress PHP fatal error, usually caused by a plugin, theme, or PHP-version update.",
    "briefly unavailable for scheduled maintenance":
        "WordPress is stuck in maintenance mode after an update. Deleting the .maintenance file in the site root fixes it.",
    "just a moment...": "A Cloudflare/bot-protection challenge page was served instead of the site.",
    "attention required! | cloudflare": "A Cloudflare firewall block page was served.",
    "your access to this site has been limited": "The Wordfence security plugin blocked the request.",
}
BLOCK_WORDS = ("cloudflare", "wordfence", "challenge")
HTTP_MEANINGS = {
    500: "Internal Server Error: the site's code crashed",
    502: "Bad Gateway: the CDN/proxy can't reach the web server behind it",
    503: "Service Unavailable: the server is overloaded or in maintenance mode",
    504: "Gateway Timeout: the web server took too long to answer the CDN/proxy",
    520: "Cloudflare 520: the web server returned an empty/invalid response",
    521: "Cloudflare 521: the web server is down or refusing Cloudflare",
    522: "Cloudflare 522: connection to the web server timed out",
    523: "Cloudflare 523: the web server's address is unreachable",
    524: "Cloudflare 524: the web server took too long to respond",
    525: "Cloudflare 525: SSL handshake with the web server failed",
    526: "Cloudflare 526: the web server's SSL certificate is invalid",
}

# ------------------------------------------------------------------ browser-side JS
VIS_JS = """const vis = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0 &&
  (el.checkVisibility ? el.checkVisibility({opacityProperty: true, visibilityProperty: true}) : true); };"""

LOGO_JS = "([hint, mobile]) => {" + VIS_JS + r"""
  window.scrollTo(0, 0);
  const vw = window.innerWidth;
  return [...document.querySelectorAll('img')].filter(i =>
      (i.currentSrc || i.src || '').includes(hint) || /american debt relief/i.test(i.alt || ''))
    .map(i => {
      const r = i.getBoundingClientRect(), x = r.left + scrollX, y = r.top + scrollY;
      const src = i.currentSrc || i.src || '';
      return { url: src, file: src.split('/').pop(), isHeaderLogoFile: src.includes(hint),
               loaded: i.complete && i.naturalWidth > 0, visible: vis(i), x: Math.round(x), y: Math.round(y),
               inPlace: y < 250 && (mobile || x < vw * 0.4) };
    }).slice(0, 8);
}"""

TEXT_JS = "([pattern]) => {" + VIS_JS + r"""
  const re = new RegExp(pattern, 'i');
  const norm = s => (s || '').replace(/\s+/g, ' ');
  const skip = new Set(['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE']);
  const hits = [...document.body.querySelectorAll('*')]
    .filter(el => !skip.has(el.tagName) && re.test(norm(el.textContent)));
  const leaves = hits.filter(el => ![...el.children].some(c => !skip.has(c.tagName) && re.test(norm(c.textContent))));
  const out = leaves.map(el => ({
    tag: el.tagName.toLowerCase(), visible: vis(el),
    stuckAnimation: !!el.closest('.elementor-invisible, .et_animated:not(.et-animated)'),
    text: norm(el.textContent).trim().slice(0, 140),
  }));
  return { inDom: out.length > 0, ok: out.some(o => o.visible), matches: out.slice(0, 6) };
}"""

FORM_JS = "() => {" + VIS_JS + r"""
  const notChrome = el => !el.closest('header, nav, footer, [role=search], .et_search_outer, .et-search-form, #top-header, #main-header');
  const fields = [...document.querySelectorAll('input, select, textarea, [role=radio], [role=slider], [role=combobox], [role=textbox]')]
    .filter(el => !['hidden', 'submit', 'button', 'image', 'reset', 'search'].includes((el.type || '').toLowerCase()))
    .filter(el => notChrome(el) && vis(el));
  // Styled answer cards: the real radio/checkbox is hidden and people click its label
  const cards = [...document.querySelectorAll('label')].filter(l => {
    const inp = l.control || l.querySelector('input');
    return inp && ['radio', 'checkbox'].includes((inp.type || '').toLowerCase()) && notChrome(l) && vis(l);
  });
  const buttons = [...document.querySelectorAll('button, input[type=submit], input[type=button], [role=button]')]
    .filter(b => notChrome(b) && vis(b))
    .map(b => (b.innerText || b.value || b.getAttribute('aria-label') || '').trim().replace(/\s+/g, ' ').slice(0, 40))
    .filter(Boolean);
  return {
    interactive: fields.length + cards.length,
    fields: [...fields.map(f => f.getAttribute('aria-label') || f.name || f.placeholder || f.id || f.type || f.tagName.toLowerCase()),
             ...cards.map(c => 'choice: ' + c.innerText.trim().slice(0, 30))].slice(0, 8),
    buttons: buttons.slice(0, 6),
  };
}"""

IFRAMES_JS = "() => {" + VIS_JS + r"""
  return [...document.querySelectorAll('iframe')].map(f => ({ src: (f.src || '').slice(0, 150), visible: vis(f) })).slice(0, 10);
}"""


# ------------------------------------------------------------------ network checks
def base_network(site: str) -> dict:
    """DNS and SSL certificate. Shared by every page on the site."""
    d: dict = {}
    host = urlparse(site).hostname or ""
    try:
        d["dns"] = {"ok": True, "ips": sorted({a[4][0] for a in socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)})}
    except socket.gaierror as e:
        d["dns"] = {"ok": False, "error": str(e)}
        return d
    if urlparse(site).scheme != "https":
        return d  # no SSL to check
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((host, 443), timeout=15) as sock, ctx.wrap_socket(sock, server_hostname=host) as tls:
            cert = tls.getpeercert()
        expires = dt.datetime.strptime(cert["notAfter"], "%b %d %H:%M:%S %Y %Z")
        d["tls"] = {"ok": True, "expires": expires.date().isoformat(), "days_left": (expires - dt.datetime.utcnow()).days}
    except ssl.SSLCertVerificationError as e:
        d["tls"] = {"ok": False, "kind": "cert", "error": getattr(e, "verify_message", "") or str(e)}
    except (socket.timeout, TimeoutError):
        d["tls"] = {"ok": False, "kind": "timeout", "error": "No answer on port 443 within 15s"}
    except ConnectionRefusedError:
        d["tls"] = {"ok": False, "kind": "refused", "error": "Connection refused on port 443"}
    except Exception as e:  # noqa: BLE001
        d["tls"] = {"ok": False, "kind": "other", "error": f"{type(e).__name__}: {e}"[:200]}
    return d


def http_check(url: str) -> dict:
    """Raw server response for one page: status code, response time, error-page fingerprints."""
    try:
        r = requests.get(url, timeout=(15, 45), headers={"User-Agent": DESKTOP_UA}, allow_redirects=True)
        body = r.text.lower()[:500_000]
        return {"ok": r.status_code < 400, "status": r.status_code,
                "ttfb_ms": int(r.elapsed.total_seconds() * 1000), "final_url": r.url,
                "redirects": [h.status_code for h in r.history], "server": r.headers.get("server", ""),
                "cache": r.headers.get("cf-cache-status") or r.headers.get("x-cache") or "",
                "signatures": [m for s, m in ERROR_SIGNATURES.items() if s in body]}
    except requests.exceptions.Timeout:
        return {"ok": False, "error": "timeout", "detail": "Connected, but no response within 45s"}
    except requests.exceptions.SSLError as e:
        return {"ok": False, "error": "ssl", "detail": str(e)[:200]}
    except requests.exceptions.ConnectionError as e:
        return {"ok": False, "error": "connection", "detail": str(e)[:200]}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "other", "detail": f"{type(e).__name__}: {e}"[:200]}


# ------------------------------------------------------------------ browser checks
def _context_args(pw, view: str) -> dict:
    if view == "desktop":
        return {"viewport": {"width": 1440, "height": 900}, "user_agent": DESKTOP_UA}
    dev = dict(pw.devices.get("Pixel 7") or {
        "viewport": {"width": 412, "height": 839}, "device_scale_factor": 2.625, "is_mobile": True, "has_touch": True,
        "user_agent": "Mozilla/5.0 (Linux; Android 14; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/130.0.0.0 Mobile Safari/537.36"})
    dev.pop("default_browser_type", None)
    dev["user_agent"] = f"{dev['user_agent']} {MONITOR_TAG}"
    return dev


def _scroll_through(page) -> None:
    """Scroll like a visitor so lazy-loaded content and scroll-triggered animations appear."""
    height = page.evaluate("document.body ? document.body.scrollHeight : 0") or 0
    step = page.viewport_size["height"] if page.viewport_size else 700
    for y in range(0, height + step, step):
        page.evaluate(f"window.scrollTo(0, {y})")
        page.wait_for_timeout(120)
    page.wait_for_timeout(1200)


def check_page(pw, browser, target: str, view: str) -> dict:
    from playwright.sync_api import TimeoutError as PWTimeout

    url = TARGETS[target]["url"]
    res = {"loaded": False, "status": None, "load_ms": None, "load_error": None, "notes": [],
           "checks": {}, "console_errors": [], "failed_requests": [], "screenshot": None}
    ctx = browser.new_context(**_context_args(pw, view))
    page = ctx.new_page()
    page.on("console", lambda m: m.type == "error" and res["console_errors"].append(m.text[:200]))
    page.on("requestfailed", lambda r: res["failed_requests"].append(f"{r.failure or 'failed'}: {r.url[:150]}"))
    page.on("response", lambda r: r.status >= 400 and res["failed_requests"].append(f"HTTP {r.status}: {r.url[:150]}"))

    try:
        t0 = time.monotonic()
        try:
            resp = page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            res["status"] = resp.status if resp else None
        except Exception as e:  # noqa: BLE001
            res["load_error"] = str(e).splitlines()[0][:300]
            return res
        try:
            page.wait_for_load_state("load", timeout=30_000)
        except PWTimeout:
            res["notes"].append("The browser 'load' event didn't fire within 30s, so some files are hanging.")
        try:
            page.wait_for_load_state("networkidle", timeout=8_000)
        except PWTimeout:
            pass  # chat widgets and trackers often keep the network busy; not a failure
        res["load_ms"] = int((time.monotonic() - t0) * 1000)
        res["loaded"] = True

        if target == "home":
            logos = page.evaluate(LOGO_JS, [LOGO_FILE_HINT, view == "mobile"])
            res["checks"]["logo"] = {"ok": any(l["loaded"] and l["visible"] and l["inPlace"] for l in logos),
                                     "candidates": logos}
            _scroll_through(page)
            try:  # bring the heading into view in case it has an entrance animation
                page.get_by_text(re.compile(HEADING_PATTERN, re.I)).first.scroll_into_view_if_needed(timeout=3000)
                page.wait_for_timeout(1200)
            except Exception:  # noqa: BLE001
                pass
            res["checks"]["heading"] = page.evaluate(TEXT_JS, [HEADING_PATTERN])
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(1500)
            res["checks"]["footer"] = page.evaluate(TEXT_JS, [FOOTER_PATTERN])

        elif target == "assessment":
            _scroll_through(page)
            page.evaluate("window.scrollTo(0, 0)")
            deadline = time.monotonic() + 20  # forms built by JavaScript can take a few seconds to appear
            while True:
                frames = []
                for fr in page.frames:
                    host = urlparse(fr.url).hostname or ""
                    if fr != page.main_frame and any(h in host for h in IGNORE_FRAME_HOSTS):
                        continue
                    try:
                        info = fr.evaluate(FORM_JS)
                    except Exception:  # noqa: BLE001
                        continue
                    info["where"] = "main page" if fr == page.main_frame else f"embedded form from {host}"
                    frames.append(info)
                total = sum(f["interactive"] for f in frames)
                if total > 0 or time.monotonic() > deadline:
                    break
                page.wait_for_timeout(1000)
            iframes = [f for f in page.evaluate(IFRAMES_JS)
                       if not any(h in (urlparse(f["src"]).hostname or "") for h in IGNORE_FRAME_HOSTS)]
            res["checks"]["form"] = {"ok": total > 0, "interactive": total,
                                     "found": [f for f in frames if f["interactive"]][:3], "iframes": iframes}
    finally:
        if not (res["loaded"] and all(c.get("ok") for c in res["checks"].values())):
            try:
                SHOT_DIR.mkdir(parents=True, exist_ok=True)
                path = SHOT_DIR / f"{target}-{view}.png"
                page.evaluate("window.scrollTo(0, 0)")
                page.screenshot(path=str(path), full_page=True, timeout=20_000)
                res["screenshot"] = str(path)
            except Exception:  # noqa: BLE001
                pass
        ctx.close()
    return res


def view_ok(br: dict) -> bool:
    return bool(br.get("loaded")) and bool(br.get("checks")) and all(c.get("ok") for c in br["checks"].values())


def run_all() -> tuple[bool, dict, dict]:
    base = base_network(SITE)
    results: dict = {t: {"http": http_check(TARGETS[t]["url"])} for t in TARGETS}
    if not base.get("dns", {}).get("ok"):
        for t in TARGETS:
            for v in VIEWS:
                results[t][v] = {"loaded": False, "skipped": True, "checks": {}}
        return False, base, results
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            try:
                for t in TARGETS:
                    for v in VIEWS:
                        try:
                            results[t][v] = check_page(pw, browser, t, v)
                        except Exception as e:  # noqa: BLE001
                            results[t][v] = {"loaded": False, "checks": {},
                                             "load_error": f"Browser error: {type(e).__name__}: {e}"[:300]}
            finally:
                browser.close()
    except Exception as e:  # noqa: BLE001  (browser failed to start: a monitor problem, never a silent pass)
        for t in TARGETS:
            for v in VIEWS:
                results[t].setdefault(v, {"loaded": False, "checks": {},
                                          "load_error": f"Monitor could not start its browser: {e}"[:300]})
    up = all(view_ok(results[t][v]) for t in TARGETS for v in VIEWS)
    return up, base, results


# ------------------------------------------------------------------ diagnosis
class Diagnosis:
    def __init__(self):
        self.headline: str | None = None
        self._reasons: dict[tuple[str, str], list[str]] = {}  # (scope, text) -> views
        self._steps: list[str] = []
        self.false_alarm_possible = False

    def head(self, text):
        self.headline = self.headline or text

    def reason(self, scope, text, view=None):
        views = self._reasons.setdefault((scope, text), [])
        if view and view not in views:
            views.append(view)

    def step(self, text, first=False):
        if text in self._steps:
            return
        self._steps.insert(0, text) if first else self._steps.append(text)

    @property
    def reasons(self):
        out = []
        for (scope, text), views in self._reasons.items():
            tag = f"{scope} · {' & '.join(views)}" if views else scope
            out.append(f"[{tag}] {text}")
        return out

    @property
    def steps(self):
        return self._steps


def _diag_http(d: Diagnosis, http: dict, label: str) -> None:
    if http.get("error"):
        if http["error"] == "timeout":
            d.head("The server accepted the connection but didn't respond within 45 seconds. It's likely overloaded or hung.")
            d.step("Check the hosting dashboard for CPU/memory spikes or unusual traffic (bot attack).")
            d.step("Ask the host to restart PHP/the web server if resources are maxed out.")
        else:
            d.head("The web server couldn't be reached.")
            d.step("Check the hosting provider's status page and contact hosting support.")
        d.reason(label, f"Raw page request failed: {http.get('detail')}")
        return
    status = http.get("status", 0)
    if status >= 500:
        d.head(f"The server is returning an error page (HTTP {status}).")
        d.reason(label, f"Returned HTTP {status}: {HTTP_MEANINGS.get(status, 'Server error')}.")
        d.step("Check the hosting error logs (PHP error log) for the time of this alert.")
        d.step("Undo any plugin, theme, or WordPress updates made in the last 24 hours.")
        d.step("If a CDN/proxy is involved (502/504/52x), check whether the origin server is running.")
    elif status in (403, 429):
        d.false_alarm_possible = True
        d.head(f"The site refused the monitor (HTTP {status}). It may be up for real visitors, with a firewall blocking the check.")
        d.reason(label, f"Returned HTTP {status} ({'Forbidden' if status == 403 else 'Too Many Requests'}).")
        d.step("Open the site on a phone using cellular data. If it loads, the site is up and a firewall is blocking the monitor.", first=True)
        d.step(f"Allowlist the user agent '{MONITOR_TAG.split('/')[0]}' in Cloudflare/Wordfence/security settings.")
    elif status == 404:
        d.head(f"The {label.lower()} page is returning 'Not Found' (HTTP 404).")
        d.reason(label, "Returned HTTP 404 (page not found).")
        d.step("Check whether the page was trashed or its URL (slug) changed in WordPress, then re-save Settings > Permalinks.")
    elif status >= 400:
        d.head(f"The {label.lower()} page returned an error (HTTP {status}).")
        d.reason(label, f"Returned HTTP {status}.")
    for sig in http.get("signatures", []):
        d.reason(label, f"Error page detected: {sig}")
        d.head("The server is showing an error page instead of the website.")
        if any(w in sig.lower() for w in BLOCK_WORDS):
            d.false_alarm_possible = True
            d.step("Open the site on a phone using cellular data. If it loads, it's a firewall/bot-protection block on the monitor, not an outage.", first=True)
        elif "maintenance" in sig:
            d.step("Remove the .maintenance file from the WordPress root folder (via hosting file manager or SFTP).", first=True)
        else:
            d.step("Check the hosting PHP error log and deactivate the most recently updated plugin.", first=True)


def _diag_view(d: Diagnosis, target: str, view: str, br: dict) -> None:
    label = TARGETS[target]["label"]
    if br.get("skipped") or view_ok(br):
        return
    if not br.get("loaded"):
        d.reason(label, f"The page didn't load in a real browser: {br.get('load_error')}", view)
        d.step("Look for slow or failing third-party scripts (chat widgets, tracking tags) and purge the site/CDN cache.")
        return
    c = br.get("checks", {})

    logo = c.get("logo")
    if logo and not logo.get("ok"):
        cands = logo.get("candidates", [])
        files = [x for x in cands if x.get("isHeaderLogoFile")]
        broken = [x for x in files if not x.get("loaded")]
        if not cands:
            d.reason(label, "Logo: no American Debt Relief logo image was found on the page.", view)
            d.step("The header may not be rendering. Check recent edits to the site header template (Theme Builder/Elementor).")
        elif broken:
            d.reason(label, f"Logo: the image file failed to download ({broken[0]['url']}).", view)
            d.step("Open the logo URL directly. If it shows 'Not Found', the file was deleted or renamed in the WordPress "
                   "Media Library. Re-upload it or re-select it in the header settings, then purge cache.")
        elif files and not any(x.get("visible") for x in files):
            d.reason(label, "Logo: the image downloads but is hidden on the page.", view)
            d.step("A stylesheet may have failed or the header was edited. Purge cache and review recent header changes.")
        else:
            d.reason(label, "Logo: a logo is present but not where it belongs in the header.", view)
            d.step("The header layout appears broken or changed. Check the header template and whether CSS files loaded.")

    h = c.get("heading")
    if h and not h.get("ok"):
        if not h.get("inDom"):
            d.reason(label, 'Heading: "How our debt settlement program works" isn\'t on the page.', view)
            d.step("Check the homepage's Revisions history in WordPress. The section or its wording may have been edited "
                   "or removed. (If the wording changed on purpose, update HEADING_PATTERN in the monitor.)")
        elif any(m.get("stuckAnimation") for m in h.get("matches", [])):
            d.reason(label, "Heading: it's on the page but stuck invisible. Its entrance animation never ran, which happens when site JavaScript breaks.", view)
            d.step("Check the JavaScript errors below. Clear the cache/minification plugin (e.g. WP Rocket, Autoptimize); these commonly break scripts.")
        else:
            d.reason(label, "Heading: it's on the page but hidden by styling.", view)
            d.step("Check whether the section's responsive visibility settings (desktop/tablet/mobile) were changed.")

    f = c.get("footer")
    if f and not f.get("ok"):
        if not f.get("inDom"):
            d.reason(label, "Footer: the copyright line is missing. The page may be cut off partway through.", view)
            d.step("A PHP error partway through the page often cuts it off. Check the PHP error log and recent footer/plugin "
                   "changes. (If the footer wording changed on purpose, update FOOTER_PATTERN in the monitor.)")
        else:
            d.reason(label, "Footer: the copyright line exists but is hidden.", view)
            d.step("Check the footer template's responsive visibility settings and whether CSS files loaded.")

    form = c.get("form")
    if form and not form.get("ok"):
        frames = [x for x in form.get("iframes", []) if x.get("src")]
        if frames:
            host = urlparse(frames[0]["src"]).hostname or frames[0]["src"]
            d.reason(label, f"Form: the form's container loads, but the form inside it (from {host}) shows no fields. "
                            "The form service may be down.", view)
            d.step(f"Check the status page of the form provider ({host}) and look for its files in the failed-file list below.")
        else:
            d.reason(label, "Form: no fillable form fields appeared within 20 seconds.", view)
            d.step("Confirm the form plugin/embed is still active and on the page (check the page's Revisions in WordPress), "
                   "and check the JavaScript errors below.")
        if view == "mobile":
            d.step("If the form works on desktop, check the form section's mobile visibility settings.")

    assets = [r for r in br.get("failed_requests", []) if any(x in r.lower() for x in (".css", ".js"))]
    if assets:
        d.reason(label, f"{len(assets)} stylesheet/script file(s) failed to load, which can break layout, animations, and forms.", view)
        d.step("Purge the WordPress cache plugin and the CDN cache, then re-check.")


def diagnose(base: dict, results: dict) -> Diagnosis:
    d = Diagnosis()
    host = urlparse(SITE).hostname
    dns, tls = base.get("dns", {}), base.get("tls", {})

    if not dns.get("ok"):
        d.head("The domain name isn't resolving (DNS failure), so visitors can't reach the site at all.")
        d.reason("Whole site", f"DNS lookup for {host} failed: {dns.get('error')}")
        d.step("Confirm the domain hasn't expired at the registrar.")
        d.step("Check the DNS provider for an outage or recently changed/deleted DNS records.")
        return d
    if tls and not tls.get("ok"):
        if tls.get("kind") == "cert":
            d.head("The SSL certificate is invalid or expired, so browsers show a security warning instead of the site.")
            d.reason("Whole site", f"SSL certificate problem: {tls.get('error')}")
            d.step("Renew or reissue the SSL certificate in the hosting or CDN dashboard.")
            d.step("Make sure the certificate covers both americandebtrelief.com and www.americandebtrelief.com.")
        else:
            d.head("The web server isn't accepting connections. The hosting server or CDN appears to be down.")
            d.reason("Whole site", f"Secure connection failed: {tls.get('error')}")
            d.step("Check the hosting provider's status page and the server's CPU, memory, and disk usage.")
            d.step("Contact hosting support and give them the time of this alert.")
    elif tls.get("ok") and tls.get("days_left", 999) < 14:
        d.reason("Whole site", f"Note: the SSL certificate expires in {tls['days_left']} days ({tls['expires']}).")

    for t in TARGETS:
        _diag_http(d, results[t].get("http", {}), TARGETS[t]["label"])

    # Headline from the pattern of what failed (used only if no server-level cause was found above)
    failing = {(t, v) for t in TARGETS for v in VIEWS if not view_ok(results[t].get(v, {}))}
    all_views = {(t, v) for t in TARGETS for v in VIEWS}
    if failing == all_views:
        d.head("The server responds, but the pages aren't displaying correctly on any device. Visitors likely see a broken page.")
    elif failing and all(v == "mobile" for _, v in failing):
        d.head("Desktop looks fine, but the MOBILE version is broken, and most visitors are on phones.")
    elif failing and all(v == "desktop" for _, v in failing):
        d.head("Mobile looks fine, but the DESKTOP version is broken.")
    elif failing and all(t == "assessment" for t, _ in failing):
        d.head("The site is up, but the Free Debt Assessment form isn't working, so new leads can't come in.")
    elif failing and all(t == "home" for t, _ in failing):
        d.head("The Free Debt Assessment form works, but the homepage is missing key content.")
    else:
        d.head("Parts of the site are missing or broken.")

    for t in TARGETS:
        for v in VIEWS:
            _diag_view(d, t, v, results[t].get(v, {}))

    d.step("Check whether anything changed recently (plugin/theme/WordPress updates, page edits, DNS or hosting changes). "
           "Most outages follow a change.")
    d.step("Open 'View run & screenshots' below to see exactly what the monitor saw.")
    return d


# ------------------------------------------------------------------ Teams cards
def _tb(text, **kw):
    return {"type": "TextBlock", "text": text, "wrap": True, **kw}


def _fmt_duration(minutes: float) -> str:
    m = int(round(minutes))
    if m < 60:
        return f"{m} min"
    h, m = divmod(m, 60)
    return f"{h}h {m}m" if m else f"{h}h"


def _view_summary(t: str, br: dict) -> str:
    if br.get("skipped") or not br.get("loaded"):
        return "⚪ Page didn't load"
    c = br.get("checks", {})
    if t == "assessment":
        f = c.get("form", {})
        return f"✅ Form loaded ({f.get('interactive', 0)} fields)" if f.get("ok") else "❌ Form NOT loading"
    return " · ".join(f"{'✅' if c.get(k, {}).get('ok') else '❌'} {CHECK_LABELS[k]}" for k in TARGET_CHECKS[t])


def _actions():
    acts = [{"type": "Action.OpenUrl", "title": "Open the website", "url": TARGETS["home"]["url"]}]
    if RUN_URL:
        acts.append({"type": "Action.OpenUrl", "title": "View run & screenshots", "url": RUN_URL})
    return acts


def _card(body, actions):
    return {"$schema": "http://adaptivecards.io/schemas/adaptive-card.json", "type": "AdaptiveCard",
            "version": "1.4", "msteams": {"width": "Full"}, "body": body, "actions": actions}


def build_down_card(base, results, d: Diagnosis, down_since: dt.datetime, reminder=False, test=False) -> dict:
    now = dt.datetime.now(CT)
    dur = _fmt_duration((now - down_since).total_seconds() / 60)
    prefix = "🧪 TEST ALERT: " if test else ("🔴 STILL DOWN: " if reminder else "🔴 ")
    title = f"{prefix}americandebtrelief.com {'(down ' + dur + ')' if reminder else 'is DOWN'}"
    sub = (f"Down since {down_since.strftime('%I:%M %p CT')}. Still failing as of {now.strftime('%I:%M %p CT')}. "
           f"Next reminder in {REMINDER_MINUTES} min unless it recovers."
           if reminder else f"Detected {now.strftime('%b %d, %Y %I:%M %p CT')}. Confirmed on 2 checks {RETRY_DELAY}s apart.")

    facts = []
    for t in TARGETS:
        for v in VIEWS:
            facts.append({"title": f"{TARGETS[t]['label']} ({v})", "value": _view_summary(t, results[t].get(v, {}))})
    home_http = results["home"].get("http", {})
    facts.append({"title": "Server response (homepage)",
                  "value": f"HTTP {home_http['status']} in {home_http['ttfb_ms']:,} ms" if "status" in home_http
                  else f"No response ({home_http.get('error', 'n/a')})"})

    body = [
        {"type": "Container", "style": "attention", "bleed": True, "items": [
            _tb(title, size="Large", weight="Bolder"), _tb(sub, isSubtle=True, spacing="None")]},
        _tb(f"**Most likely cause:** {d.headline}", spacing="Medium"),
        {"type": "FactSet", "facts": facts},
    ]
    if d.false_alarm_possible:
        body.append(_tb("⚠️ **This may be a false alarm.** The site might be fine for real visitors while a firewall "
                        "blocks the monitor. Verify on a phone using cellular data.", color="Warning"))
    if d.reasons:
        body += [_tb("**Why we flagged it**", spacing="Medium"), _tb("\n".join(f"- {r}" for r in d.reasons))]
    body += [_tb("**What to check first**", spacing="Medium"),
             _tb("\n".join(f"{i}. {s}" for i, s in enumerate(d.steps, 1)))]

    tech = []
    if base.get("dns", {}).get("ips"):
        tech.append(f"- Server IP(s): {', '.join(base['dns']['ips'][:4])}")
    if home_http.get("server") or home_http.get("cache"):
        tech.append(f"- Server/CDN: {home_http.get('server') or 'n/a'} | cache: {home_http.get('cache') or 'n/a'}")
    if base.get("tls", {}).get("ok"):
        tech.append(f"- SSL certificate valid until {base['tls']['expires']} ({base['tls']['days_left']} days)")
    seen = set()
    for t in TARGETS:
        for v in VIEWS:
            br = results[t].get(v, {})
            if view_ok(br):
                continue
            for r in br.get("failed_requests", [])[:4]:
                if r not in seen:
                    seen.add(r)
                    tech.append(f"- Failed file ({TARGETS[t]['label']}, {v}): {r}")
            for e in br.get("console_errors", [])[:2]:
                if e not in seen:
                    seen.add(e)
                    tech.append(f"- JavaScript error ({TARGETS[t]['label']}, {v}): {e}")
            for n in br.get("notes", []):
                tech.append(f"- {TARGETS[t]['label']} ({v}): {n}")
    if tech:
        body += [_tb("**Technical details (for the web/hosting team)**", spacing="Medium", size="Small"),
                 _tb("\n".join(tech[:14]), size="Small", isSubtle=True)]
    return _card(body, _actions())


def build_recovery_card(down_since: dt.datetime, prior_headline: str, test=False) -> dict:
    now = dt.datetime.now(CT)
    dur = _fmt_duration((now - down_since).total_seconds() / 60)
    body = [
        {"type": "Container", "style": "good", "bleed": True, "items": [
            _tb(f"{'🧪 TEST: ' if test else ''}✅ americandebtrelief.com is back UP", size="Large", weight="Bolder"),
            _tb(f"Recovered {now.strftime('%b %d, %Y %I:%M %p CT')}", isSubtle=True, spacing="None")]},
        {"type": "FactSet", "facts": [
            {"title": "Down for", "value": f"about {dur}"},
            {"title": "Went down", "value": down_since.strftime("%b %d, %I:%M %p CT")},
            {"title": "What was wrong", "value": prior_headline or "n/a"},
            {"title": "Now passing", "value": "Homepage and Free Debt Assessment form, on desktop and mobile"},
        ]},
        _tb("Tip: note what fixed it in this chat so the team knows next time.", isSubtle=True, size="Small"),
    ]
    return _card(body, _actions()[:1])


def send_to_teams(card: dict) -> bool:
    if not WEBHOOK:
        print("TEAMS_WEBHOOK_URL is not set. Card that would have been sent:")
        print(json.dumps(card, indent=2, ensure_ascii=False))
        return False
    payload = {"type": "message", "attachments": [
        {"contentType": "application/vnd.microsoft.card.adaptive", "contentUrl": None, "content": card}]}
    try:
        r = requests.post(WEBHOOK, json=payload, timeout=30)
    except requests.RequestException as e:
        print(f"Could not reach the Teams webhook: {e}")
        return False
    if r.status_code in (200, 202):
        print("Teams message sent.")
        return True
    print(f"Teams webhook returned HTTP {r.status_code}: {r.text[:300]}")
    return False


# ------------------------------------------------------------------ state (remembered between runs)
def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:  # noqa: BLE001
        return {"status": "up"}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2) + "\n")


def _parse(ts: str | None) -> dt.datetime | None:
    return dt.datetime.fromisoformat(ts) if ts else None


# ------------------------------------------------------------------ main
def _sample_results():
    ok_home = {"loaded": True, "checks": {"logo": {"ok": True}, "heading": {"ok": True}, "footer": {"ok": True}}}
    bad_form = {"loaded": True, "failed_requests": ["HTTP 503: https://forms.example-provider.com/embed.js"],
                "console_errors": ["Uncaught ReferenceError: FormEmbed is not defined"], "notes": [],
                "checks": {"form": {"ok": False, "interactive": 0,
                                    "iframes": [{"src": "https://forms.example-provider.com/f/adr", "visible": True}]}}}
    base = {"dns": {"ok": True, "ips": ["203.0.113.10"]}, "tls": {"ok": True, "expires": "2027-01-15", "days_left": 99}}
    http = {"ok": True, "status": 200, "ttfb_ms": 640, "server": "nginx", "cache": "HIT", "signatures": []}
    results = {"home": {"http": http, "desktop": ok_home, "mobile": ok_home},
               "assessment": {"http": http, "desktop": bad_form, "mobile": bad_form}}
    return base, results


def main() -> int:
    if "--test-alert" in sys.argv:
        kind = sys.argv[sys.argv.index("--test-alert") + 1] if len(sys.argv) > sys.argv.index("--test-alert") + 1 else "down"
        since = dt.datetime.now(CT) - dt.timedelta(minutes=47)
        if kind == "recovery":
            card = build_recovery_card(since, "The site is up, but the Free Debt Assessment form isn't working.", test=True)
        else:
            base, results = _sample_results()
            card = build_down_card(base, results, diagnose(base, results), dt.datetime.now(CT), test=True)
        return 0 if send_to_teams(card) else 2

    state = load_state()
    up, base, results = run_all()
    if not up:
        print(f"A check failed. Re-checking in {RETRY_DELAY}s to rule out a momentary blip...")
        time.sleep(RETRY_DELAY)
        up, base, results = run_all()

    now = dt.datetime.now(CT)
    print(json.dumps({"time": now.isoformat(), "up": up, "previous_status": state.get("status"),
                      "views": {f"{t}/{v}": _view_summary(t, results[t].get(v, {})) for t in TARGETS for v in VIEWS},
                      "assessment_form_found": results["assessment"].get("desktop", {}).get("checks", {})
                                                  .get("form", {}).get("found")},
                     indent=2, ensure_ascii=False, default=str))

    if up:
        if state.get("status") == "down":
            since = _parse(state.get("since")) or now
            print(f"✅ RECOVERED after {_fmt_duration((now - since).total_seconds() / 60)}.")
            send_to_teams(build_recovery_card(since, state.get("headline", "")))
            save_state({"status": "up", "since": now.isoformat()})
        else:
            print("✅ UP: all checks passing on desktop and mobile.")
        return 0

    d = diagnose(base, results)
    print("❌ DOWN:", d.headline)
    for r in d.reasons:
        print("  -", r)

    if state.get("status") != "down":
        sent = send_to_teams(build_down_card(base, results, d, now))
        save_state({"status": "down", "since": now.isoformat(), "headline": d.headline,
                    "last_alert": now.isoformat() if sent else None})
    else:
        since = _parse(state.get("since")) or now
        last = _parse(state.get("last_alert"))
        if last is None or (now - last).total_seconds() >= REMINDER_MINUTES * 60 - 120:
            sent = send_to_teams(build_down_card(base, results, d, since, reminder=True))
            if sent:
                state["last_alert"] = now.isoformat()
        else:
            print(f"Still down; last alert at {last.strftime('%I:%M %p CT')}. Next reminder after {REMINDER_MINUTES} min.")
        state["headline"] = d.headline
        save_state(state)
    return 1  # marks the GitHub run red and keeps the screenshots


if __name__ == "__main__":
    sys.exit(main())
