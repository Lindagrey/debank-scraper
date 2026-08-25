# debank-scraper

Console tool for fetching a wallet's full portfolio (Total Net Worth,
balance by chain, protocol and token) from [debank.com](https://debank.com) via Selenium.

## Install

```bash
pip install -r requirements.txt
```

Requires Google Chrome to be installed — chromedriver is downloaded
automatically via `webdriver-manager`.

## Usage

Single address:

```bash
python main.py 0xYourAddress
```

With JSON output:

```bash
python main.py 0xYourAddress --json
```

Batch mode (list of addresses from a file, checked in parallel):

```bash
python main.py --file addresses.txt --concurrency 3 --out results.json
```

With a proxy:

```bash
# one proxy for all requests (http scheme by default)
python main.py 0xYourAddress --proxy 1.2.3.4:8080

# authenticated proxy
python main.py 0xYourAddress --proxy 1.2.3.4:8080:user:pass

# proxy with an explicit scheme (http, https, socks4, socks5)
python main.py 0xYourAddress --proxy socks5://1.2.3.4:1080

# batch with a proxy pool (assigned round-robin)
python main.py --file addresses.txt --proxy-file proxies.txt --concurrency 3
```

> **SOCKS4/5 + username:password.** `user:pass` authentication in the Chrome
> extension only works for HTTP/HTTPS proxies. Chrome has no built-in support
> for authenticating SOCKS proxies this way — use a SOCKS proxy without a
> built-in password (e.g. IP-whitelisted) or a local tunnel that forwards
> SOCKS5 into a local HTTP proxy.

## Options

| Flag | Description |
|------|----------|
| `address` | Wallet address (positional, for a single request) |
| `--file FILE` | File with a list of addresses — enables batch mode |
| `--headful` | Show the browser window (headless by default) |
| `--timeout SEC` | Timeout while waiting for balance data, sec (default 45) |
| `--proxy [SCHEME://]HOST:PORT[:USER:PASS]` | One proxy for all requests. `SCHEME`: `http`, `https`, `socks4`, `socks5` (default `http`) |
| `--proxy-file FILE` | File with proxies, assigned round-robin (batch mode) |
| `--concurrency N` | How many wallets to check in parallel (default 2) |
| `--json` | Output as JSON instead of a text table |
| `--out FILE` | Save the combined result to a file (`.json` or `.csv`) |

## File format

`addresses.txt` / `proxies.txt` — one item per line, lines starting with `#` are ignored.
See `addresses.example.txt` and `proxies.example.txt`.

## Result

```json
{
  "address": "0x...",
  "total": "$1,234.56",
  "chains": [{"name": "Ethereum", "balance": "$1,000.00"}],
  "protocols": [{"name": "Uniswap V3", "balance": "$200.00", "assets": [...]}],
  "tokens": [{"symbol": "USDT", "chain": "eth", "contract": "0x...", "usd_value": "$34.56"}]
}
```

## Check history (`checked/` folder)

Every single wallet check — in both single and batch mode — is automatically
saved as a separate JSON file in the `checked/` folder next to the script,
regardless of whether `--out` is used. Filename: `checked/<address>_<YYYYMMDD_HHMMSS>.json`.

```json
{
  "checked_at": "2026-08-04T21:15:03+03:00",
  "address": "0x...",
  "total": "$1,234.56",
  "chains": [...],
  "protocols": [...],
  "tokens": [...]
}
```

`--out` saves a combined summary file for the whole run (a list of results),
while `checked/` keeps a persistent, dated history of every check — these are
two independent mechanisms.

## Limitations

- Keep no more than 2-3 concurrent Chrome instances (`--concurrency`) — each one eats ~300 MB of RAM.
- DeBank may show a captcha or rate-limit frequent requests — use proxies for batch scanning.
# debank-scraper
