import asyncio
import aiohttp
import time
from decimal import Decimal
from datetime import datetime, timezone
import os

# ---------- KONFIGURACJA ----------
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

BASE_WALLET = os.environ.get("BASE_WALLET")
SOLANA_WALLET = os.environ.get("SOLANA_WALLET")

BASE_AMOUNT_ETH = Decimal("2.0")
PROFIT_THRESHOLD_ETH = Decimal("0.003")
POLL_INTERVAL = 30.0
MAYAN_PROFIT_THRESHOLD_ETH = Decimal("0.006")

# LI.FI chain IDs
FROM_CHAIN = 8453                   # Base
MIDDLE_CHAIN = 1151111081099710     # Solana
TO_CHAIN = 8453                     # Base

EVM_NATIVE = "0x0000000000000000000000000000000000000000"
SOL_NATIVE = "11111111111111111111111111111111"
# ---------- KONIEC KONFIGURACJI ----------

def now_ts():
    return datetime.now(timezone.utc).strftime("%H:%M:%S")

async def send_telegram_message(session, token, chat_id, text):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    async with session.post(url, json=payload, timeout=10) as resp:
        try:
            return await resp.json()
        except Exception:
            return {"ok": False, "status": resp.status, "text": await resp.text()}

async def get_jumper_routes(session, from_address, to_address, from_chain, to_chain, from_token, to_token, from_amount):
    url = "https://api.jumper.exchange/p/lifi/advanced/routes"
    headers = {
        "accept": "*/*",
        "content-type": "application/json",
        "origin": "https://jumper.exchange",
        "referer": "https://jumper.exchange/",
        "user-agent": "Mozilla/5.0",
        "x-lifi-integrator": "jumper.exchange",
        "x-lifi-sdk": "3.12.11",
        "x-lifi-widget": "3.32.2",
    }

    payload = {
        "fromAddress": from_address,
        "fromAmount": str(from_amount),
        "fromChainId": from_chain,
        "fromTokenAddress": from_token,
        "toAddress": to_address,
        "toChainId": to_chain,
        "toTokenAddress": to_token,
        "options": {
            "integrator": "jumper.exchange",
            "order": "CHEAPEST",
            "maxPriceImpact": 0.4,
            "allowSwitchChain": True
        }
    }

    async with session.post(url, headers=headers, json=payload) as resp:
        data = await resp.json()
        if "routes" not in data or not data["routes"]:
            raise RuntimeError(f"No routes found. Raw: {await resp.text()}")
        return data["routes"]

def to_smallest_unit(amount_decimal: Decimal, decimals: int) -> str:
    scaled = (amount_decimal * (Decimal(10) ** decimals)).to_integral_value()
    return str(scaled)

def from_smallest_unit(amount_str: str, decimals: int) -> Decimal:
    return Decimal(str(amount_str)) / (Decimal(10) ** decimals)

def pick_best_route(routes):
    def route_value(r):
        val = r.get("toAmount") or r.get("toAmountMin")
        return Decimal(val) if val else Decimal(0)
    return max(routes, key=route_value)

def format_route_list(routes, token_decimals, direction):
    """Czytelne wypisanie wszystkich tras."""
    out = [f"\n=== Wszystkie trasy {direction} ==="]
    best = pick_best_route(routes)
    best_amount = Decimal(best.get("toAmount") or best.get("toAmountMin"))
    for i, r in enumerate(routes, 1):
        steps = [s.get("tool", "") for s in r.get("steps", [])]
        name = " + ".join(steps)
        to_raw = r.get("toAmount") or r.get("toAmountMin")
        amount = from_smallest_unit(to_raw, token_decimals)
        diff = amount - from_smallest_unit(best_amount, token_decimals)
        out.append(f"{i:02d}. {name:<40} | {amount:.6f} ({diff:+.6f} od najlepszej)")
    return "\n".join(out)

async def check_once(session):
    from_amount_smallest = int(BASE_AMOUNT_ETH * (10 ** 18))
    try:
        routes_fwd = await get_jumper_routes(session, BASE_WALLET, SOLANA_WALLET, FROM_CHAIN, MIDDLE_CHAIN, EVM_NATIVE, SOL_NATIVE, from_amount_smallest)
        best_fwd = pick_best_route(routes_fwd)
        to_amount_raw_1 = best_fwd.get("toAmount") or best_fwd.get("toAmountMin")
        sol_decimals = int(best_fwd["toToken"]["decimals"])
        bridge1 = " + ".join([s.get("tool", "") for s in best_fwd.get("steps", [])])
        sol_amount = from_smallest_unit(to_amount_raw_1, sol_decimals)
    except Exception as e:
        print(f"[{now_ts()}] Error BASE->SOL: {e}")
        return None

    sol_amount_smallest = to_smallest_unit(sol_amount, sol_decimals)
    try:
        routes_back = await get_jumper_routes(session, SOLANA_WALLET, BASE_WALLET, MIDDLE_CHAIN, TO_CHAIN, SOL_NATIVE, EVM_NATIVE, sol_amount_smallest)
        best_back = pick_best_route(routes_back)
        to_amount_raw_2 = best_back.get("toAmount") or best_back.get("toAmountMin")
        eth_decimals_resp = int(best_back["toToken"]["decimals"])
        bridge2 = " + ".join([s.get("tool", "") for s in best_back.get("steps", [])])
        eth_back = from_smallest_unit(to_amount_raw_2, eth_decimals_resp)
    except Exception as e:
        print(f"[{now_ts()}] Error SOL->BASE: {e}")
        return None

    profit = eth_back - BASE_AMOUNT_ETH
    pct = (profit / BASE_AMOUNT_ETH * 100) if BASE_AMOUNT_ETH != 0 else 0

    color_green = "\033[1;38;5;46m"
    color_gray = "\033[38;5;240m"
    color_reset = "\033[0m"
    color = color_green if profit > PROFIT_THRESHOLD_ETH else color_gray
    profit_mark = "▲" if profit > 0 else "▼"

    print(
        f"{color}[{now_ts()}] {profit_mark} "
        f"2 ETH → {sol_amount:.6f} SOL ({bridge1}) → {eth_back:.6f} ETH ({bridge2}) "
        f"| PROFIT: {profit:+.6f} ETH ({pct:+.3f}%) {color_reset}"
    )

    # wypisz pełne listy tras
    print(format_route_list(routes_fwd, sol_decimals, "Base → Solana"))
    print(format_route_list(routes_back, eth_decimals_resp, "Solana → Base"))

    return {"profit": profit, "eth_back": eth_back, "sol_amount": sol_amount, "bridge1": bridge1, "bridge2": bridge2}

async def main_loop():
    async with aiohttp.ClientSession() as session:
        while True:
            start = time.time()
            await check_once(session)
            elapsed = time.time() - start
            await asyncio.sleep(max(0.1, POLL_INTERVAL - elapsed))

if __name__ == "__main__":
    try:
        asyncio.run(main_loop())
    except KeyboardInterrupt:
        pass
