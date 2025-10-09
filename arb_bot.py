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
PROFIT_THRESHOLD_ETH = Decimal("0.002")
POLL_INTERVAL = 30.0
MAYAN_PROFIT_THRESHOLD_ETH = Decimal("0.006")  # wyższy próg dla Mayan

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

async def get_jumper_route(from_address, to_address, from_chain, to_chain, from_token, to_token, from_amount):
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

    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, json=payload) as resp:
            try:
                data = await resp.json()
                if "routes" not in data or not data["routes"]:
                    raise RuntimeError("No routes found")
                return data
            except Exception:
                text = await resp.text()
                raise RuntimeError(f"Invalid JSON from Jumper: {text}")

def parse_jumper_to_amount(data):
    try:
        route = None
        for r in data.get("routes", []):
            tool = r.get("steps", [{}])[0].get("tool", "").lower()
            if "mayanmctp" not in tool:
                route = r
                break
        if route is None:
            raise RuntimeError("No valid route found (cctp+mayan ignored)")

        to_amount_raw = route.get("toAmount") or route.get("toAmountMin")
        if not to_amount_raw:
            raise RuntimeError("Brak pola toAmount w odpowiedzi Jumper")

        to_amount = Decimal(to_amount_raw)
        decimals = int(route["toToken"]["decimals"])
        tool = route["steps"][0]["tool"]

        return to_amount, decimals, tool
    except Exception as e:
        raise RuntimeError(f"Error parsing Jumper route: {e}")

def to_smallest_unit(amount_decimal: Decimal, decimals: int) -> str:
    scaled = (amount_decimal * (Decimal(10) ** decimals)).to_integral_value()
    return str(scaled)

def from_smallest_unit(amount_str: str, decimals: int) -> Decimal:
    return Decimal(str(amount_str)) / (Decimal(10) ** decimals)

async def check_once(session):
    from_amount_smallest = int(BASE_AMOUNT_ETH * (10 ** 18))
    try:
        data1 = await get_jumper_route(BASE_WALLET, SOLANA_WALLET, FROM_CHAIN, MIDDLE_CHAIN, EVM_NATIVE, SOL_NATIVE, from_amount_smallest)
        to_amount_raw_1, sol_decimals, bridge1 = parse_jumper_to_amount(data1)
        sol_amount = from_smallest_unit(to_amount_raw_1, sol_decimals)
    except Exception as e:
        print(f"[{now_ts()}] Error BASE->SOL via Jumper: {e}")
        return None

    sol_amount_smallest = to_smallest_unit(sol_amount, sol_decimals)
    try:
        data2 = await get_jumper_route(SOLANA_WALLET, BASE_WALLET, MIDDLE_CHAIN, TO_CHAIN, SOL_NATIVE, EVM_NATIVE, sol_amount_smallest)
        to_amount_raw_2, eth_decimals_resp, bridge2 = parse_jumper_to_amount(data2)
        eth_back = from_smallest_unit(to_amount_raw_2, eth_decimals_resp)
    except Exception as e:
        print(f"[{now_ts()}] Error SOL->BASE via Jumper: {e}")
        return None

    profit = eth_back - BASE_AMOUNT_ETH
    pct = (profit / BASE_AMOUNT_ETH * 100) if BASE_AMOUNT_ETH != 0 else 0

    color_green = "\033[1;92m"   # jasny, pogrubiony zielony
    color_gray = "\033[2;37m"    # jasny szary, czytelniejszy
    color_reset = "\033[0m"

    color = color_green if profit > PROFIT_THRESHOLD_ETH else color_gray
    profit_mark = "▲" if profit > 0 else "▼"

    print(
        f"{color}[{now_ts()}] {profit_mark} "
        f"2 ETH → {sol_amount:.6f} SOL ({bridge1}) → {eth_back:.6f} ETH ({bridge2}) "
        f"| PROFIT: {profit:+.6f} ETH ({pct:+.3f}%) {color_reset}"
    )


    return {"profit": profit, "eth_back": eth_back, "sol_amount": sol_amount, "bridge1": bridge1, "bridge2": bridge2}

async def main_loop():
    async with aiohttp.ClientSession() as session:
        while True:
            start = time.time()
            info = await check_once(session)
            if info:
                # Ustal threshold dla bridge1
                threshold = MAYAN_PROFIT_THRESHOLD_ETH if info["bridge1"].lower() == "mayan" else PROFIT_THRESHOLD_ETH
                if info["profit"] >= threshold:
                    # Nagłówek zależny od wartości profit
                    alert_threshold = Decimal("0.01")
                    header = "*SUPER ARBITRAGE ALERT*" if info["profit"] >= alert_threshold else "*ARBITRAGE ALERT*"
                    msg = (
                        f"{header}\n"
                        f"`Profit: {info['profit']:.6f} ETH`\n"
                        f"----------------------------\n"
                        f"*Bridge 1:* {info['bridge1']} (Base→Solana)\n"
                        f"*Bridge 2:* {info['bridge2']} (Solana→Base)\n"
                        f"*Received:* `{info['sol_amount']:.6f} SOL`\n"
                        f"*Returned:* `{info['eth_back']:.6f} ETH`\n"
                        f"----------------------------"
                    )
                    try:
                        await send_telegram_message(session, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, msg)
                    except Exception as e:
                        print(f"[{now_ts()}] Telegram send error: {e}")

            elapsed = time.time() - start
            await asyncio.sleep(max(0.1, POLL_INTERVAL - elapsed))

if __name__ == "__main__":
    try:
        asyncio.run(main_loop())
    except KeyboardInterrupt:
        pass
