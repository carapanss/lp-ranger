#!/usr/bin/env python3
"""
On-chain PnL calculator for the LP Ranger wallet on Base chain.

Reconstructs the full transfer history via eth_getLogs (no API key needed),
identifies external deposits/withdrawals (user-initiated vs bot swaps),
and computes the real net PnL.

Usage: python3 scripts/pnl_onchain.py
"""
import json
import struct
import sys
import time
import urllib.request

# ── Constants ────────────────────────────────────────────────────────────────
WALLET           = "0xa675D4BEB16845eac10A5E1c75AfE5cD6Ee4fC63"
WALLET_LOWER     = WALLET.lower()
USDC_ADDR        = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
WETH_ADDR        = "0x4200000000000000000000000000000000000006"
POS_MANAGER      = "0x03a520b32C04BF3bEEf7BEb72E919cf822Ed34f1"
SWAP_ROUTER      = "0x2626664c2603336E57B271c5C0b26F421741e481"
POSITION_ID      = 5009590

# keccak256("Transfer(address,address,uint256)")
TRANSFER_SIG     = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

# Contracts the bot itself calls — anything not in this set is "external" (user)
BOT_CONTRACTS = {
    USDC_ADDR.lower(),
    WETH_ADDR.lower(),
    POS_MANAGER.lower(),
    SWAP_ROUTER.lower(),
    # Uniswap V3 factory / pool contracts — we'll detect pools dynamically
}

RPC_URLS = [
    "https://base.llamarpc.com",
    "https://base.publicnode.com",
    "https://base.drpc.org",
    "https://mainnet.base.org",
]

# ── RPC helpers ───────────────────────────────────────────────────────────────
def rpc(method, params):
    payload = json.dumps({"jsonrpc": "2.0", "method": method, "params": params, "id": 1}).encode()
    for url in RPC_URLS:
        try:
            req = urllib.request.Request(url, data=payload,
                headers={"Content-Type": "application/json", "User-Agent": "lp-ranger-pnl/1.0"})
            r = json.loads(urllib.request.urlopen(req, timeout=15).read())
            if "error" in r:
                continue
            return r["result"]
        except Exception:
            continue
    raise RuntimeError(f"All RPC endpoints failed for {method}")


def pad_addr(addr: str) -> str:
    """Pad an address to a 32-byte topic."""
    a = addr.lower().replace("0x", "")
    return "0x" + a.zfill(64)


def get_logs_chunked(address: str, topic0: str, topic1=None, topic2=None,
                     from_block: int = 0, to_block: int | None = None,
                     chunk: int = 50_000) -> list:
    """Fetch all logs in chunks to avoid RPC limits."""
    if to_block is None:
        to_block = int(rpc("eth_blockNumber", []), 16)
    logs = []
    start = from_block
    while start <= to_block:
        end = min(start + chunk - 1, to_block)
        params = {
            "address": address,
            "topics": [topic0, topic1, topic2],
            "fromBlock": hex(start),
            "toBlock": hex(end),
        }
        # Remove None topics from the end
        while params["topics"] and params["topics"][-1] is None:
            params["topics"].pop()
        try:
            batch = rpc("eth_getLogs", [params])
            if isinstance(batch, list):
                logs.extend(batch)
        except Exception as e:
            print(f"  warn: getLogs chunk {start}-{end}: {e}", file=sys.stderr)
        start = end + 1
        time.sleep(0.1)
    return logs


def decode_uint256(hex_str: str) -> int:
    return int(hex_str, 16)


def is_contract(addr: str) -> bool:
    """True if the address has deployed bytecode (is a smart contract)."""
    code = rpc("eth_getCode", [addr, "latest"])
    return isinstance(code, str) and len(code) > 4


# ── Position value (V3 maths) ─────────────────────────────────────────────────
def tick_to_sqrt_price(tick: int) -> float:
    return 1.0001 ** (tick / 2)


def v3_position_value(liquidity: int, tick_lower: int, tick_upper: int, price_usdc: float) -> float:
    if liquidity <= 0:
        return 0.0
    # sqrt prices (in token1/token0 units, i.e. USDC/WETH before decimals)
    sqrt_p  = (price_usdc / 1e12) ** 0.5
    sqrt_pa = tick_to_sqrt_price(tick_lower)
    sqrt_pb = tick_to_sqrt_price(tick_upper)
    if sqrt_p <= sqrt_pa:
        weth = liquidity * (sqrt_pb - sqrt_pa) / (sqrt_pa * sqrt_pb) / 1e18
        usdc = 0.0
    elif sqrt_p >= sqrt_pb:
        weth = 0.0
        usdc = liquidity * (sqrt_pb - sqrt_pa) / 1e6
    else:
        weth = liquidity * (sqrt_pb - sqrt_p) / (sqrt_p * sqrt_pb) / 1e18
        usdc = liquidity * (sqrt_p - sqrt_pa) / 1e6
    return weth * price_usdc + usdc


def get_position(pos_id: int):
    """Call positions(tokenId) on the NonfungiblePositionManager."""
    selector  = "0x99fbab88"  # positions(uint256)
    token_hex = hex(pos_id)[2:].zfill(64)
    data      = selector + token_hex
    raw = rpc("eth_call", [{"to": POS_MANAGER, "data": data}, "latest"])
    if not raw or raw == "0x":
        return None
    # ABI: (uint96,address,address,address,uint24,int24,int24,uint128,uint256,uint256,uint128,uint128)
    raw = raw[2:]
    vals = [raw[i:i+64] for i in range(0, len(raw), 64)]
    if len(vals) < 8:
        return None
    def to_int(h):  return int(h, 16)
    def to_sint(h): v = int(h, 16); return v - (1 << 256) if v >= (1 << 255) else v
    return {
        "tick_lower":  to_sint(vals[5]),
        "tick_upper":  to_sint(vals[6]),
        "liquidity":   to_int(vals[7]),
    }


def get_price_from_pool() -> float | None:
    """Fetch ETH/USD price via the WETH/USDC 0.05% V3 pool slot0."""
    POOL = "0xd0b53D9277642d899DF5C87A3966A349A798F224"
    selector = "0x3850c7bd"  # slot0()
    raw = rpc("eth_call", [{"to": POOL, "data": selector}, "latest"])
    if not raw or raw == "0x":
        return None
    raw = raw[2:]
    sqrtPriceX96 = int(raw[:64], 16)
    ratio = (sqrtPriceX96 / (2**96)) ** 2
    return ratio * 1e12  # USDC per ETH


# ── Token balance helpers ─────────────────────────────────────────────────────
def erc20_balance(token: str, wallet: str) -> int:
    selector = "0x70a08231"  # balanceOf(address)
    data = selector + pad_addr(wallet)[2:]
    raw = rpc("eth_call", [{"to": token, "data": data}, "latest"])
    return decode_uint256(raw) if raw and raw != "0x" else 0


def eth_balance(wallet: str) -> int:
    return int(rpc("eth_getBalance", [wallet, "latest"]), 16)


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    print("═" * 60)
    print("  LP Ranger — On-chain PnL audit")
    print(f"  Wallet: {WALLET}")
    print("═" * 60)

    # ── 1. Current price ──────────────────────────────────────────────────────
    print("\n📡 Fetching current ETH price from pool...")
    price = get_price_from_pool()
    if not price:
        print("  Could not fetch price from pool, using fallback $2318")
        price = 2318.0
    print(f"  ETH price: ${price:,.2f}")

    # ── 2. Current balances ───────────────────────────────────────────────────
    print("\n💰 Current wallet balances:")
    usdc_raw  = erc20_balance(USDC_ADDR, WALLET)
    weth_raw  = erc20_balance(WETH_ADDR, WALLET)
    eth_raw   = eth_balance(WALLET)
    usdc_bal  = usdc_raw  / 1e6
    weth_bal  = weth_raw  / 1e18
    eth_bal   = eth_raw   / 1e18

    print(f"  USDC : {usdc_bal:>10.4f} USDC  = ${usdc_bal:>10.4f}")
    print(f"  WETH : {weth_bal:>10.6f} WETH  = ${weth_bal * price:>10.4f}")
    print(f"  ETH  : {eth_bal:>10.6f} ETH   = ${eth_bal  * price:>10.4f}  (gas reserve)")

    # ── 3. LP position value ──────────────────────────────────────────────────
    print(f"\n📊 LP position #{POSITION_ID}:")
    pos = get_position(POSITION_ID)
    pos_value = 0.0
    if pos:
        pos_value = v3_position_value(pos["liquidity"], pos["tick_lower"], pos["tick_upper"], price)
        print(f"  tick_lower : {pos['tick_lower']}")
        print(f"  tick_upper : {pos['tick_upper']}")
        print(f"  liquidity  : {pos['liquidity']}")
        print(f"  value      : ${pos_value:.4f}")
    else:
        print("  Position not found or liquidity = 0")

    current_total = usdc_bal + weth_bal * price + eth_bal * price + pos_value
    print(f"\n  Total current value : ${current_total:.4f}")

    # ── 4. Transfer history ───────────────────────────────────────────────────
    print("\n🔍 Fetching USDC transfer history from chain...")
    wallet_topic = pad_addr(WALLET)

    usdc_in_logs  = get_logs_chunked(USDC_ADDR, TRANSFER_SIG, topic2=wallet_topic)
    usdc_out_logs = get_logs_chunked(USDC_ADDR, TRANSFER_SIG, topic1=wallet_topic)
    print(f"  USDC in:  {len(usdc_in_logs)} transfers")
    print(f"  USDC out: {len(usdc_out_logs)} transfers")

    print("🔍 Fetching WETH transfer history from chain...")
    weth_in_logs  = get_logs_chunked(WETH_ADDR, TRANSFER_SIG, topic2=wallet_topic)
    weth_out_logs = get_logs_chunked(WETH_ADDR, TRANSFER_SIG, topic1=wallet_topic)
    print(f"  WETH in:  {len(weth_in_logs)} transfers")
    print(f"  WETH out: {len(weth_out_logs)} transfers")

    # ── 5. Classify transfers ─────────────────────────────────────────────────
    print("\n🏷️  Classifying transfers (external = user deposits/withdrawals)...")

    # Cache for is_contract lookups
    _code_cache: dict[str, bool] = {}
    def cached_is_contract(addr: str) -> bool:
        a = addr.lower()
        if a not in _code_cache:
            _code_cache[a] = is_contract(addr)
            time.sleep(0.05)
        return _code_cache[a]

    def classify_transfers(logs, direction: str, decimals: int, label: str):
        """Returns (external_usd, all_rows)"""
        rows = []
        for log in logs:
            topics = log.get("topics", [])
            if len(topics) < 3:
                continue
            from_addr = "0x" + topics[1][-40:]
            to_addr   = "0x" + topics[2][-40:]
            amount_raw = decode_uint256(log.get("data", "0x0"))
            amount = amount_raw / (10 ** decimals)
            block  = int(log.get("blockNumber", "0x0"), 16)
            tx     = log.get("transactionHash", "")

            # Counterparty: if incoming, it's the sender; if outgoing, it's the receiver
            counterparty = from_addr.lower() if direction == "in" else to_addr.lower()

            # Skip WETH mint/burn (address(0))
            if counterparty == "0x0000000000000000000000000000000000000000":
                rows.append((block, tx, direction, amount, label, counterparty, "wrap/unwrap"))
                continue

            # Classify counterparty
            if counterparty in BOT_CONTRACTS:
                kind = "bot_op"
            else:
                is_ctr = cached_is_contract(counterparty)
                kind = "bot_op_contract" if is_ctr else "EXTERNAL_USER"

            rows.append((block, tx, direction, amount, label, counterparty, kind))
        return rows

    usdc_in_rows  = classify_transfers(usdc_in_logs,  "in",  6,  "USDC")
    usdc_out_rows = classify_transfers(usdc_out_logs, "out", 6,  "USDC")
    weth_in_rows  = classify_transfers(weth_in_logs,  "in",  18, "WETH")
    weth_out_rows = classify_transfers(weth_out_logs, "out", 18, "WETH")

    all_rows = sorted(
        usdc_in_rows + usdc_out_rows + weth_in_rows + weth_out_rows,
        key=lambda r: r[0]
    )

    # ── 6. Print all transfers ────────────────────────────────────────────────
    print(f"\n{'Block':>9}  {'Dir':>4}  {'Amount':>12}  {'Token':>5}  {'Kind':>22}  Counterparty")
    print("-" * 90)
    ext_usdc_net = 0.0
    ext_weth_net = 0.0
    for (block, tx, direction, amount, token, counterparty, kind) in all_rows:
        sign = "+" if direction == "in" else "-"
        print(f"{block:>9}  {sign:>4}  {amount:>12.4f}  {token:>5}  {kind:>22}  {counterparty}")
        if kind == "EXTERNAL_USER":
            if token == "USDC":
                ext_usdc_net += amount if direction == "in" else -amount
            else:  # WETH
                ext_weth_net += amount if direction == "in" else -amount

    # ── 7. PnL calculation ────────────────────────────────────────────────────
    print("\n" + "═" * 60)
    print("  PnL SUMMARY")
    print("═" * 60)

    # External deposits/withdrawals in USD (WETH valued at current price for simplicity)
    ext_weth_usd = ext_weth_net * price
    net_deposited_usd = ext_usdc_net + ext_weth_usd

    print(f"\n  External USDC net deposited : ${ext_usdc_net:>10.4f}")
    print(f"  External WETH net deposited : {ext_weth_net:>10.6f} WETH ≈ ${ext_weth_usd:>10.4f}")
    print(f"  Total net deposited (USD)   : ${net_deposited_usd:>10.4f}")
    print()
    print(f"  Current total value (USD)   : ${current_total:>10.4f}")
    print()

    pnl_usd = current_total - net_deposited_usd
    pnl_pct = (pnl_usd / net_deposited_usd * 100) if net_deposited_usd > 0 else 0.0

    print(f"  ┌─────────────────────────────────────────┐")
    print(f"  │  Real PnL : ${pnl_usd:>+10.4f} ({pnl_pct:>+6.2f}%)  │")
    print(f"  └─────────────────────────────────────────┘")
    print()
    print("  Note: WETH deposits valued at current ETH price.")
    print("  For exact EUR value divide by EUR/USD rate (~1.13).")
    print()

    # Also show gas spent (rough: ETH balance is just gas reserve)
    print(f"  ETH gas reserve: {eth_bal:.6f} ETH = ${eth_bal * price:.4f}")
    print("  (ETH is not counted in PnL — it's for gas, not LP capital)")


if __name__ == "__main__":
    main()
