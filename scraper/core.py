"""
scraper/core.py — fetches a wallet's portfolio from debank.com via Selenium.

Returns a dict:
    {address, total, chains, protocols, tokens}
    tokens: [{symbol, chain, contract, usd_value}]

Supports proxies in the form host:port and host:port:user:pass (authenticated
via a temporary Chrome extension), as well as an explicit scheme:
scheme://host:port[:user:pass], where scheme is http, https, socks4 or socks5
(defaults to http if no scheme is given).

Note: Chrome extensions cannot pass a username/password for SOCKS4/5 via
onAuthRequired — user:pass authentication is only guaranteed to work for
http/https proxies. For a password-protected SOCKS5 proxy, use a proxy
without built-in auth (e.g. IP-whitelisted) or a local tunnel client
(e.g. forwarding SOCKS5 into a local HTTP proxy).
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile
import time
import zipfile
from typing import Callable

log = logging.getLogger("debank_scraper")

DEBANK_URL       = "https://debank.com/profile/{address}"
BALANCE_SELECTOR = "[class*='HeaderInfo_totalAssetInner']"
MONEY_RE         = re.compile(r"\$([\d,]+\.?\d*)")


# ── Proxy: Chrome extension for host:port:user:pass authentication ────────────

SUPPORTED_PROXY_SCHEMES = {"http", "https", "socks4", "socks5"}


def _build_proxy_extension(scheme: str, host: str, port: str, user: str, pwd: str, tmpdir: str) -> str:
    manifest = """{
  "version": "1.0.0",
  "manifest_version": 2,
  "name": "Proxy Auth",
  "permissions": [
    "proxy", "tabs", "unlimitedStorage", "storage",
    "<all_urls>", "webRequest", "webRequestBlocking"
  ],
  "background": {"scripts": ["bg.js"]},
  "minimum_chrome_version": "22.0.0"
}"""
    bg = f"""
var config = {{
  mode: "fixed_servers",
  rules: {{
    singleProxy: {{ scheme: "{scheme}", host: "{host}", port: parseInt("{port}") }},
    bypassList: ["localhost", "127.0.0.1"]
  }}
}};
chrome.proxy.settings.set({{value: config, scope: "regular"}}, function() {{}});
chrome.webRequest.onAuthRequired.addListener(
  function(details) {{
    return {{ authCredentials: {{ username: "{user}", password: "{pwd}" }} }};
  }},
  {{ urls: ["<all_urls>"] }},
  ["blocking"]
);
"""
    path = os.path.join(tmpdir, "proxy_ext.zip")
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("manifest.json", manifest)
        zf.writestr("bg.js", bg)
    return path


def _parse_proxy(proxy_str: str) -> tuple[str, str, str, str, str] | None:
    """Parses host:port, host:port:user:pass or scheme://host:port[:user:pass].

    scheme is http, https, socks4 or socks5 (defaults to http).
    """
    if not proxy_str:
        return None
    raw = proxy_str.strip()

    scheme = "http"
    if "://" in raw:
        scheme, raw = raw.split("://", 1)
        scheme = scheme.lower()
        if scheme == "socks5h":  # alias — Chrome doesn't distinguish socks5/socks5h
            scheme = "socks5"
        if scheme not in SUPPORTED_PROXY_SCHEMES:
            raise ValueError(
                f"Unsupported proxy scheme: {scheme!r}. "
                f"Allowed: {', '.join(sorted(SUPPORTED_PROXY_SCHEMES))}"
            )

    parts = raw.split(":")
    if len(parts) == 2:
        return scheme, parts[0], parts[1], "", ""
    if len(parts) == 4:
        if scheme in ("socks4", "socks5"):
            log.warning(
                "Proxy %s://...: Chrome does not support user:pass authentication "
                "for SOCKS proxies via extension — the credentials will likely "
                "be ignored.", scheme,
            )
        return scheme, parts[0], parts[1], parts[2], parts[3]
    return None


# ── Selenium helpers ────────────────────────────────────────────────────────

def _make_driver(proxy_str: str | None, headful: bool, tmpdir: str):
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    from webdriver_manager.chrome import ChromeDriverManager

    options = Options()
    if not headful:
        options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )

    if proxy_str:
        parsed = _parse_proxy(proxy_str)
        if parsed:
            scheme, host, port, user, pwd = parsed
            if user and pwd:
                ext = _build_proxy_extension(scheme, host, port, user, pwd, tmpdir)
                options.add_extension(ext)
            else:
                options.add_argument(f"--proxy-server={scheme}://{host}:{port}")

    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=options)
    driver.set_page_load_timeout(60)
    return driver


def _wait_balance(driver, timeout: int) -> str:
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait

    wait = WebDriverWait(driver, timeout)
    el = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, BALANCE_SELECTOR)))

    deadline = time.time() + timeout
    while time.time() < deadline:
        text = el.text.strip()
        m = MONEY_RE.search(text)
        if m and float(m.group(1).replace(",", "")) > 0:
            break
        time.sleep(0.5)

    prev, streak = "", 0
    stable = time.time() + 15
    while time.time() < stable:
        text = el.text.strip()
        if text == prev:
            streak += 1
            if streak >= 3:
                break
        else:
            prev, streak = text, 1
        time.sleep(1)

    return el.text.strip()


def _extract_tokens(driver) -> list[dict]:
    from selenium.webdriver.common.by import By
    tokens, seen = [], set()
    try:
        for link in driver.find_elements(By.CSS_SELECTOR, "a[href^='/token/']"):
            href = link.get_attribute("href") or ""
            parts = href.rstrip("/").split("/token/")[-1].split("/")
            if len(parts) < 2:
                continue
            chain, contract = parts[0], parts[1]
            symbol = link.text.strip()
            if not symbol or (chain, contract) in seen:
                continue
            seen.add((chain, contract))
            usd = ""
            try:
                row = link.find_element(
                    By.XPATH, "ancestor::div[contains(@class,'db-table-row')]"
                )
                cells = row.find_elements(By.CSS_SELECTOR, ".db-table-cell.is-right")
                if cells:
                    usd = cells[-1].text.strip()
            except Exception:
                pass
            tokens.append({"symbol": symbol, "chain": chain, "contract": contract, "usd_value": usd})
    except Exception as e:
        log.debug("tokens: %s", e)
    return tokens


def _extract_chains(driver) -> list[dict]:
    from selenium.webdriver.common.by import By
    chains = []
    try:
        for item in driver.find_elements(By.CSS_SELECTOR, "[class*='AssetsOnChain_item']"):
            try:
                name = item.find_element(By.CSS_SELECTOR, "[class*='AssetsOnChain_chainName']").text.strip()
                val = item.find_element(By.CSS_SELECTOR, "[class*='AssetsOnChain_usdValue']").text.strip()
                if name and val:
                    chains.append({"name": name, "balance": val})
            except Exception:
                continue
    except Exception as e:
        log.debug("chains: %s", e)
    return chains


def _extract_protocols(driver) -> list[dict]:
    """Extracts protocols and their positions via JS (more reliable than CSS selectors)."""
    try:
        return driver.execute_script("""
const results = [];
const processedCards = new Set();
const nameEls = document.querySelectorAll("[class*='ProjectCell_assetsItemNameText']");

for (const nameEl of nameEls) {
    const name = nameEl.innerText.trim();
    if (!name) continue;

    const wrap = nameEl.closest("[class*='ProjectCell_assetsItemWrap']");
    const worthEl = wrap && wrap.querySelector("[class*='ProjectCell_assetsItemWorth']");
    const balance = worthEl ? worthEl.innerText.trim() : '';

    let card = nameEl.parentElement;
    for (let i = 0; i < 10; i++) {
        if (!card) break;
        if (card.querySelectorAll("[class*='db-table-row']").length > 0) break;
        card = card.parentElement;
    }
    if (!card || processedCards.has(card)) {
        if (!processedCards.has(nameEl)) {
            processedCards.add(nameEl);
            results.push({ name, balance, assets: [] });
        }
        continue;
    }
    processedCards.add(card);

    const assets = [];
    const rows = card.querySelectorAll("[class*='db-table-row']");
    for (const row of rows) {
        const cells = row.querySelectorAll("[class*='db-table-cell']");
        if (cells.length < 2) continue;
        const firstText = cells[0].innerText.trim();
        if (!firstText || /^(pool|balance|rewards|usd|token|asset)$/i.test(firstText)) continue;
        const lastText = cells[cells.length - 1].innerText.trim();
        const midTexts = [];
        for (let i = 1; i < cells.length - 1; i++) {
            const t = cells[i].innerText.trim();
            if (t) midTexts.push(t);
        }
        assets.push({ symbol: firstText, details: midTexts.join(' | '), usd: lastText });
    }

    results.push({ name, balance, assets });
}
return results;
        """)
    except Exception as e:
        log.debug("protocols JS extract: %s", e)
        return []


# ── Main sync function (runs in a thread in batch mode) ────────────────────

def fetch_balance_sync(
    address: str,
    proxy: str | None = None,
    headful: bool = False,
    timeout: int = 45,
    on_log: Callable[[str], None] | None = None,
) -> dict:
    def log_msg(msg: str):
        log.info(msg)
        if on_log:
            on_log(msg)

    with tempfile.TemporaryDirectory() as tmpdir:
        log_msg(f"[{address}] Launching Chrome{'  (proxy: ' + proxy + ')' if proxy else ''}...")
        driver = _make_driver(proxy, headful, tmpdir)
        try:
            url = DEBANK_URL.format(address=address)
            log_msg(f"[{address}] Opening {url}")
            driver.get(url)

            log_msg(f"[{address}] Waiting for balance to load...")
            raw = _wait_balance(driver, timeout)
            m = MONEY_RE.search(raw)
            total = "$" + m.group(1) if m else raw
            log_msg(f"[{address}] Balance: {total}")

            tokens = _extract_tokens(driver)
            chains = _extract_chains(driver)
            protocols = _extract_protocols(driver)
            log_msg(f"[{address}] Done: {len(tokens)} tokens, {len(chains)} chains, {len(protocols)} protocols")

            return {
                "address": address,
                "total": total,
                "chains": chains,
                "protocols": protocols,
                "tokens": tokens,
            }
        finally:
            driver.quit()


# ── Async wrapper with concurrency limit ────────────────────────────────────

async def fetch_balance(
    address: str,
    sem: asyncio.Semaphore,
    proxy: str | None = None,
    headful: bool = False,
    timeout: int = 45,
    on_log: Callable[[str], None] | None = None,
) -> dict:
    async with sem:
        return await asyncio.to_thread(
            fetch_balance_sync, address, proxy, headful, timeout, on_log
        )
