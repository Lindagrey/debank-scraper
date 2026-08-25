"""
main.py — console tool for fetching wallet balance(s) from debank.com.

Single address:
    python main.py 0xAddress...

Batch (list of addresses from a file, one per line):
    python main.py --file addresses.txt --concurrency 3 --out results.json

Options:
    --headful           show the browser window (headless by default)
    --timeout SEC        timeout while waiting for balance data (default 45)
    --proxy [SCHEME://]HOST:PORT[:USER:PASS]   one proxy for all requests
                          SCHEME: http, https, socks4, socks5 (default http)
    --proxy-file FILE     file with proxies (one per line), assigned round-robin
    --concurrency N       how many wallets to check in parallel in batch mode (default 2)
    --json               output as JSON instead of a text table
    --out FILE            save the combined result to a file (.json or .csv by extension)

Every check is automatically saved as a separate JSON file in the checked/
folder (checked/<address>_<YYYYMMDD_HHMMSS>.json) with a checked_at field —
this doesn't replace --out, it keeps a history of every check.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

from colorama import Fore, Style, init as colorama_init

from scraper.core import fetch_balance
from scraper.proxy import ProxyPool

colorama_init(autoreset=True)

C_BANNER = Fore.CYAN + Style.BRIGHT
C_HEADER = Fore.MAGENTA + Style.BRIGHT
C_LABEL = Fore.YELLOW
C_VALUE = Fore.GREEN + Style.BRIGHT
C_ERROR = Fore.RED + Style.BRIGHT
C_DIM = Style.DIM
C_RESET = Style.RESET_ALL


class ColorFormatter(logging.Formatter):
    LEVEL_COLORS = {
        logging.DEBUG: Fore.BLUE,
        logging.INFO: Fore.GREEN,
        logging.WARNING: Fore.YELLOW,
        logging.ERROR: Fore.RED + Style.BRIGHT,
        logging.CRITICAL: Fore.RED + Style.BRIGHT,
    }

    def format(self, record: logging.LogRecord) -> str:
        color = self.LEVEL_COLORS.get(record.levelno, "")
        record.levelname = f"{color}{record.levelname}{C_RESET}"
        record.msg = f"{color}{record.msg}{C_RESET}" if record.levelno >= logging.WARNING else record.msg
        return super().format(record)


handler = logging.StreamHandler()
handler.setFormatter(ColorFormatter(f"{C_DIM}%(levelname)s:{C_RESET} %(message)s"))
logging.basicConfig(level=logging.INFO, handlers=[handler])
log = logging.getLogger("debank_scraper")

CHECKED_DIR = "checked"

BANNER = r"""
    ____  __________  ___    _   ____ __     _____ __________  ___    ____  ____  __________            ___ _____
   / __ \/ ____/ __ )/   |  / | / / //_/    / ___// ____/ __ \/   |  / __ \/ __ \/ ____/ __ \   _   __ <  /|__  /
  / / / / __/ / __  / /| | /  |/ / ,<       \__ \/ /   / /_/ / /| | / /_/ / /_/ / __/ / /_/ /  | | / / / /  /_ < 
 / /_/ / /___/ /_/ / ___ |/ /|  / /| |     ___/ / /___/ _, _/ ___ |/ ____/ ____/ /___/ _, _/   | |/ / / / ___/ / 
/_____/_____/_____/_/  |_/_/ |_/_/ |_|____/____/\____/_/ |_/_/  |_/_/   /_/   /_____/_/ |_|    |___(_)_(_)____/  
                                    /_____/                                                                                                                    
"""


def save_checked(result: dict, checked_dir: str = CHECKED_DIR) -> str:
    """Saves a single check's result as a separate JSON file with a check timestamp."""
    out_dir = Path(checked_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.now().astimezone()
    safe_addr = result["address"].replace("/", "_").replace("\\", "_")
    filename = f"{safe_addr}_{now.strftime('%Y%m%d_%H%M%S')}.json"

    data = {
        "checked_at": now.isoformat(timespec="seconds"),
        "address": result["address"],
        "total": result.get("total"),
        "chains": result.get("chains", []),
        "protocols": result.get("protocols", []),
        "tokens": result.get("tokens", []),
    }
    if "error" in result:
        data["error"] = result["error"]

    path = out_dir / filename
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return str(path)


def print_result(result: dict) -> None:
    print(f"\n{C_LABEL}Wallet :{C_RESET} {result['address']}")
    print(f"{C_LABEL}Balance:{C_RESET} {C_VALUE}{result['total']}{C_RESET}")
    if result["chains"]:
        print(f"\n{C_HEADER}By chain:{C_RESET}")
        for c in result["chains"]:
            print(f"  {c['name']:<20} {C_VALUE}{c['balance']}{C_RESET}")
    if result["protocols"]:
        print(f"\n{C_HEADER}By protocol:{C_RESET}")
        for p in result["protocols"]:
            print(f"  {p['name']:<20} {C_VALUE}{p['balance']}{C_RESET}")
    if result["tokens"]:
        print(f"\n{C_HEADER}Tokens (contracts):{C_RESET}")
        print(f"  {C_DIM}{'Symbol':<10} {'Chain':<10} {'Contract':<45} {'USD'}{C_RESET}")
        print(f"  {C_DIM}{'-'*10} {'-'*10} {'-'*45} {'-'*10}{C_RESET}")
        for t in result["tokens"]:
            print(f"  {t['symbol']:<10} {t['chain']:<10} {t['contract']:<45} {C_VALUE}{t['usd_value']}{C_RESET}")


def save_results(results: list[dict], path: str) -> None:
    if path.lower().endswith(".csv"):
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["address", "total", "chains", "protocols", "tokens_count"])
            for r in results:
                writer.writerow([
                    r["address"],
                    r["total"],
                    "; ".join(f"{c['name']}={c['balance']}" for c in r["chains"]),
                    "; ".join(f"{p['name']}={p['balance']}" for p in r["protocols"]),
                    len(r["tokens"]),
                ])
    else:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
    log.info("Results saved to %s", path)


async def run_batch(
    addresses: list[str],
    proxy_pool: ProxyPool,
    single_proxy: str | None,
    headful: bool,
    timeout: int,
    concurrency: int,
) -> list[dict]:
    sem = asyncio.Semaphore(max(1, concurrency))
    results: list[dict] = []

    async def worker(addr: str):
        proxy = single_proxy or (proxy_pool.next() if proxy_pool else None)
        try:
            r = await fetch_balance(addr, sem, proxy=proxy, headful=headful, timeout=timeout)
        except Exception as e:
            log.error("[%s] Error: %s", addr, e)
            r = {"address": addr, "total": "ERROR", "chains": [], "protocols": [], "tokens": [], "error": str(e)}
        results.append(r)
        checked_path = save_checked(r)
        log.info("[%s] Check saved to %s", addr, checked_path)

    await asyncio.gather(*(worker(a) for a in addresses))
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch wallet balance(s) from debank.com")
    parser.add_argument("address", nargs="?", help="Wallet address (0x...)")
    parser.add_argument("--file", metavar="FILE", help="File with a list of addresses (one per line) — enables batch mode")
    parser.add_argument("--headful", action="store_true", help="Show the browser window")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--timeout", type=int, default=45, help="Timeout while waiting for data, sec")
    parser.add_argument(
        "--proxy",
        metavar="[SCHEME://]HOST:PORT[:USER:PASS]",
        help="One proxy for all requests. SCHEME: http, https, socks4, socks5 (default http)",
    )
    parser.add_argument("--proxy-file", metavar="FILE", help="File with proxies, assigned round-robin (batch mode)")
    parser.add_argument("--concurrency", type=int, default=2, help="Parallel Chrome instances in batch mode (default 2)")
    parser.add_argument("--out", metavar="FILE", help="Save results to a file (.json or .csv)")
    args = parser.parse_args()

    if not args.address and not args.file:
        parser.error("provide a wallet address or --file with a list of addresses")

    proxy_pool = ProxyPool.from_file(args.proxy_file) if args.proxy_file else ProxyPool([])
    if proxy_pool:
        log.info("Loaded %d proxies from %s", len(proxy_pool), args.proxy_file)

    if args.file:
        with open(args.file, encoding="utf-8") as f:
            addresses = [ln.strip() for ln in f if ln.strip() and not ln.strip().startswith("#")]
        if not addresses:
            log.error("File %s is empty", args.file)
            return 1
        log.info("Batch mode: %d addresses, concurrency=%d", len(addresses), args.concurrency)
        results = asyncio.run(run_batch(
            addresses, proxy_pool, args.proxy, args.headful, args.timeout, args.concurrency
        ))
    else:
        sem = asyncio.Semaphore(1)
        try:
            result = asyncio.run(fetch_balance(
                args.address, sem, proxy=args.proxy, headful=args.headful, timeout=args.timeout
            ))
        except Exception as e:
            log.error("Error: %s", e)
            return 1
        checked_path = save_checked(result)
        log.info("[%s] Check saved to %s", args.address, checked_path)
        results = [result]

    if args.out:
        save_results(results, args.out)

    if args.json:
        print(json.dumps(results if args.file else results[0], ensure_ascii=False, indent=2))
    else:
        for r in results:
            if r["total"] == "ERROR":
                print(f"\n{C_LABEL}Wallet :{C_RESET} {r['address']}\n{C_ERROR}Error  : {r.get('error')}{C_RESET}")
            else:
                print_result(r)

    return 0


if __name__ == "__main__":
    print(f"{C_BANNER}{BANNER}{C_RESET}")
    sys.exit(main())
