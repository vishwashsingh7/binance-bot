import os
import time
import hmac
import hashlib
import logging
import requests
import argparse
import csv
from urllib.parse import urlencode
from dotenv import load_dotenv
from decimal import Decimal, ROUND_DOWN, ROUND_UP, getcontext
from datetime import datetime
from colorama import Fore, Style, init as colorama_init

# increase decimal precision for safety
getcontext().prec = 18

colorama_init(autoreset=True)
load_dotenv()

API_KEY = os.getenv("BINANCE_API_KEY") or os.getenv("API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET") or os.getenv("API_SECRET")
BASE_URL = "https://testnet.binancefuture.com"

# logging
os.makedirs("logs", exist_ok=True)
logger = logging.getLogger("basic_bot")
logger.setLevel(logging.DEBUG)
fh = logging.FileHandler("logs/trading.log")
fh.setLevel(logging.DEBUG)
fh.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
logger.addHandler(fh)
ch = logging.StreamHandler()
ch.setLevel(logging.INFO)
ch.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
logger.addHandler(ch)

if not API_KEY or not API_SECRET:
    logger.error("Missing API keys in environment (.env). Exiting.")
    raise SystemExit("Set BINANCE_API_KEY and BINANCE_API_SECRET in .env")

session = requests.Session()
session.headers.update({"X-MBX-APIKEY": API_KEY})

def sign_payload(secret, qs):
    return hmac.new(secret.encode(), qs.encode(), hashlib.sha256).hexdigest()

def timestamp_ms():
    return int(time.time() * 1000)

def _get(path, params=None):
    url = BASE_URL + path
    try:
        r = session.get(url, params=params, timeout=10)
        r.raise_for_status()
        return r.json()
    except requests.HTTPError as e:
        logger.exception("GET %s failed: %s", path, e)
        raise

def _post_signed(path, params):
    url = BASE_URL + path
    params = dict(params)
    params["timestamp"] = timestamp_ms()
    params["recvWindow"] = 5000
    qs = urlencode(params, doseq=True)
    params["signature"] = sign_payload(API_SECRET, qs)
    try:
        r = session.post(url, params=params, timeout=10)
        logger.debug("POST %s params=%s", url, params)
        body = r.text
        logger.debug("Response status=%s body=%s", r.status_code, body)
        data = r.json()
    except Exception as e:
        logger.exception("POST %s failed: %s", path, e)
        raise
    if r.status_code != 200:
        logger.error("Binance error: %s", data)
        return {"error": data}
    return data

def get_symbol_filters(symbol):
    info = _get("/fapi/v1/exchangeInfo")
    for s in info.get("symbols", []):
        if s.get("symbol") == symbol:
            return {f["filterType"]: f for f in s.get("filters", [])}
    raise ValueError(f"Symbol {symbol} not found in exchangeInfo")

def get_market_price(symbol):
    data = _get("/fapi/v1/ticker/price", {"symbol": symbol})
    return Decimal(str(data["price"]))

def ceil_to_step(value: Decimal, step: Decimal) -> Decimal:
    if value <= 0:
        return Decimal("0")
    factor = (value / step).to_integral_value(rounding=ROUND_UP)
    return (factor * step).quantize(step)

def floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    factor = (value / step).to_integral_value(rounding=ROUND_DOWN)
    return (factor * step).quantize(step)

def compute_qty(symbol, requested_qty: Decimal, used_price: Decimal):
    filters = get_symbol_filters(symbol)
    step_size = Decimal(filters["LOT_SIZE"]["stepSize"])
    min_qty = Decimal(filters["LOT_SIZE"]["minQty"])
    max_qty = Decimal(filters["LOT_SIZE"]["maxQty"])
    min_notional = Decimal(filters["MIN_NOTIONAL"]["notional"])

    # first, round user's qty UP to allowed step
    qty = ceil_to_step(requested_qty, step_size)

    # ensure min notional
    if (used_price * qty) < min_notional:
        needed = ceil_to_step((min_notional / used_price), step_size)
        if needed > qty:
            qty = needed
            logger.info("Qty increased to %s to meet minNotional %s", qty, min_notional)

    # enforce minQty
    if qty < min_qty:
        qty = min_qty

    # enforce maxQty
    if qty > max_qty:
        raise ValueError(f"Adjusted quantity {qty} exceeds maxQty {max_qty}")

    return qty, min_notional

def adjust_price_to_tick(symbol, price: Decimal):
    filters = get_symbol_filters(symbol)
    tick = Decimal(filters["PRICE_FILTER"]["tickSize"])
    return floor_to_step(price, tick)

def place_order(symbol, side, otype, qty: Decimal, price: Decimal = None):
    if otype == "MARKET":
        params = {"symbol": symbol, "side": side, "type": "MARKET", "quantity": str(qty)}
    else:
        params = {"symbol": symbol, "side": side, "type": "LIMIT", "timeInForce": "GTC", "quantity": str(qty), "price": str(price)}
    return _post_signed("/fapi/v1/order", params)

def log_trade_csv(resp):
    csv_path = "logs/trades.csv"
    file_exists = os.path.isfile(csv_path)
    with open(csv_path, mode="a", newline="") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(["Time","Symbol","Side","Type","Price","Qty","Notional","OrderId","Status"])
        price = resp.get("price") or "0"
        qty = resp.get("origQty") or resp.get("quantity") or "0"
        notional = (Decimal(price) * Decimal(qty)) if price and qty else ""
        writer.writerow([datetime.now().strftime("%Y-%m-%d %H:%M:%S"), resp.get("symbol"), resp.get("side"), resp.get("type"), price, qty, str(notional), resp.get("orderId"), resp.get("status")])

def print_rules(symbol):
    f = get_symbol_filters(symbol)
    print(f"\nSymbol rules for {symbol}:")
    print(f"  minQty: {f['LOT_SIZE']['minQty']}  maxQty: {f['LOT_SIZE']['maxQty']}  stepSize: {f['LOT_SIZE']['stepSize']}")
    print(f"  tickSize: {f['PRICE_FILTER']['tickSize']}  minPrice: {f['PRICE_FILTER']['minPrice']}  maxPrice: {f['PRICE_FILTER']['maxPrice']}")
    print(f"  minNotional: {f['MIN_NOTIONAL']['notional']}\n")
    logger.info("Displayed symbol rules for %s", symbol)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", help="Symbol e.g., BTCUSDT")
    parser.add_argument("--side", choices=["BUY","SELL"], help="BUY or SELL")
    parser.add_argument("--type", choices=["MARKET","LIMIT"], help="Order type")
    parser.add_argument("--quantity", type=float, help="Quantity")
    parser.add_argument("--price", type=float, help="Price for LIMIT")
    parser.add_argument("--yes", action="store_true", help="Auto-approve adjustments")
    parser.add_argument("--dry-run", action="store_true", help="Show adjustments but do not place order")
    args = parser.parse_args()

    if not (args.symbol and args.side and args.type and args.quantity):
        symbol = input("Symbol (e.g., BTCUSDT): ").strip().upper()
        print_rules(symbol)
        side = input("Side (BUY/SELL): ").strip().upper()
        otype = input("Type (MARKET/LIMIT): ").strip().upper()
        qty_in = Decimal(input("Quantity: ").strip())
        price_in = None
        if otype == "LIMIT":
            price_in = Decimal(input("Price: ").strip())
        auto_yes = False
        dry_run = False
    else:
        symbol = args.symbol.upper()
        print_rules(symbol)
        side = args.side
        otype = args.type
        qty_in = Decimal(str(args.quantity))
        price_in = Decimal(str(args.price)) if args.price else None
        auto_yes = args.yes
        dry_run = args.dry_run

    logger.info("Request: symbol=%s side=%s type=%s qty=%s price=%s", symbol, side, otype, qty_in, price_in)

    try:
        if otype == "MARKET":
            used_price = get_market_price(symbol)
            final_qty, min_notional = compute_qty(symbol, qty_in, used_price)
            planned_price = used_price
        else:
            if price_in is None:
                raise ValueError("Price required for LIMIT order")
            price_adj = adjust_price_to_tick(symbol, price_in)
            if price_adj != price_in:
                logger.info("Price adjusted from %s to %s based on tickSize", price_in, price_adj)
                print(f"Price adjusted from {price_in} to {price_adj} (tickSize).")
            used_price = price_adj
            final_qty, min_notional = compute_qty(symbol, qty_in, used_price)
            planned_price = used_price

        notional = planned_price * final_qty
        print(f"\nPlanned order:\n  Symbol : {symbol}\n  Side   : {side}\n  Type   : {otype}\n  Price  : {planned_price if otype=='LIMIT' else 'market'}\n  Qty    : {final_qty}\n  Notional (approx): {notional}\n")

        if final_qty != qty_in:
            print(Fore.YELLOW + f"Note: quantity adjusted from {qty_in} -> {final_qty} (minNotional/stepSize).")

        if dry_run:
            print("Dry-run enabled. Not placing order.")
            return

        if not auto_yes:
            cont = input("Proceed to place order? (y/n): ").strip().lower()
            if cont != "y":
                print("Aborted by user.")
                return

        resp = place_order(symbol, side, otype, final_qty, planned_price if otype=="LIMIT" else None)
        if resp is None:
            print("No response received. Check logs.")
            return
        if "error" in resp:
            print(Fore.RED + "Order failed:", resp["error"])
            return

        print(Fore.GREEN + "\n=== ORDER RESULT ===")
        print(resp)
        logger.info("Order placed: %s", resp)
        try:
            log_trade_csv(resp)
        except Exception:
            logger.exception("Failed to write trade CSV")

    except Exception as e:
        logger.exception("Order placement failed: %s", e)
        print(Fore.RED + "Error:", e)

if __name__ == "__main__":
    main()
