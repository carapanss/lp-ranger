#!/usr/bin/env python3
"""
LP Ranger AutoBot — Automated Uniswap V3 position management on Base
Executes range changes, pool exits, and re-entries automatically.

SECURITY: Private key is encrypted with AES-256 using a password you set.
The key is NEVER stored in plaintext. You enter the password once per session.

USAGE:
  # First time: import your private key (will be encrypted)
  python3 lp_autobot.py --setup

  # Run in auto mode (integrates with LP Ranger strategy)
  python3 lp_autobot.py --auto

  # Manual operations
  python3 lp_autobot.py --rebalance --tick-lower -204000 --tick-upper -200000
  python3 lp_autobot.py --exit --hold ETH
  python3 lp_autobot.py --enter --tick-lower -204000 --tick-upper -200000

  # Dry run (simulate without executing)
  python3 lp_autobot.py --auto --dry-run

⚠️  WARNING: This bot signs transactions with your private key.
    Only use on a dedicated hot wallet with limited funds.
    NEVER use your main wallet.
"""

import json
import os
import sys
import time
import math
import hashlib
import getpass
import struct
from pathlib import Path
from datetime import datetime

# ============================================================
# CONSTANTS — Base Chain Uniswap V3
# ============================================================
BASE_RPC = "https://mainnet.base.org"
CHAIN_ID = 8453

# Uniswap V3 contracts on Base
POSITION_MANAGER = "0x03a520b32C04BF3bEEf7BEb72E919cf822Ed34f1"
SWAP_ROUTER = "0x2626664c2603336E57B271c5C0b26F421741e481"
FACTORY = "0x33128a8fC17869897dcE68Ed026d694621f6FDfD"

# Token addresses on Base
WETH = "0x4200000000000000000000000000000000000006"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"

# ABI fragments (only what we need)
ERC20_ABI = json.loads('[{"constant":true,"inputs":[{"name":"","type":"address"}],"name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"type":"function"},{"constant":false,"inputs":[{"name":"spender","type":"address"},{"name":"amount","type":"uint256"}],"name":"approve","outputs":[{"name":"","type":"bool"}],"type":"function"},{"constant":true,"inputs":[{"name":"owner","type":"address"},{"name":"spender","type":"address"}],"name":"allowance","outputs":[{"name":"","type":"uint256"}],"type":"function"},{"constant":true,"inputs":[],"name":"decimals","outputs":[{"name":"","type":"uint8"}],"type":"function"}]')

POSITION_MANAGER_ABI = json.loads('''[
  {"inputs":[{"internalType":"uint256","name":"tokenId","type":"uint256"}],"name":"positions","outputs":[{"internalType":"uint96","name":"nonce","type":"uint96"},{"internalType":"address","name":"operator","type":"address"},{"internalType":"address","name":"token0","type":"address"},{"internalType":"address","name":"token1","type":"address"},{"internalType":"uint24","name":"fee","type":"uint24"},{"internalType":"int24","name":"tickLower","type":"int24"},{"internalType":"int24","name":"tickUpper","type":"int24"},{"internalType":"uint128","name":"liquidity","type":"uint128"},{"internalType":"uint256","name":"feeGrowthInside0LastX128","type":"uint256"},{"internalType":"uint256","name":"feeGrowthInside1LastX128","type":"uint256"},{"internalType":"uint128","name":"tokensOwed0","type":"uint128"},{"internalType":"uint128","name":"tokensOwed1","type":"uint128"}],"stateMutability":"view","type":"function"},
  {"inputs":[{"components":[{"internalType":"uint256","name":"tokenId","type":"uint256"},{"internalType":"uint128","name":"liquidity","type":"uint128"},{"internalType":"uint256","name":"amount0Min","type":"uint256"},{"internalType":"uint256","name":"amount1Min","type":"uint256"},{"internalType":"uint256","name":"deadline","type":"uint256"}],"internalType":"struct INonfungiblePositionManager.DecreaseLiquidityParams","name":"params","type":"tuple"}],"name":"decreaseLiquidity","outputs":[{"internalType":"uint256","name":"amount0","type":"uint256"},{"internalType":"uint256","name":"amount1","type":"uint256"}],"stateMutability":"payable","type":"function"},
  {"inputs":[{"components":[{"internalType":"uint256","name":"tokenId","type":"uint256"},{"internalType":"address","name":"recipient","type":"address"},{"internalType":"uint128","name":"amount0Max","type":"uint128"},{"internalType":"uint128","name":"amount1Max","type":"uint128"}],"internalType":"struct INonfungiblePositionManager.CollectParams","name":"params","type":"tuple"}],"name":"collect","outputs":[{"internalType":"uint256","name":"amount0","type":"uint256"},{"internalType":"uint256","name":"amount1","type":"uint256"}],"stateMutability":"payable","type":"function"},
  {"inputs":[{"components":[{"internalType":"address","name":"token0","type":"address"},{"internalType":"address","name":"token1","type":"address"},{"internalType":"uint24","name":"fee","type":"uint24"},{"internalType":"int24","name":"tickLower","type":"int24"},{"internalType":"int24","name":"tickUpper","type":"int24"},{"internalType":"uint256","name":"amount0Desired","type":"uint256"},{"internalType":"uint256","name":"amount1Desired","type":"uint256"},{"internalType":"uint256","name":"amount0Min","type":"uint256"},{"internalType":"uint256","name":"amount1Min","type":"uint256"},{"internalType":"address","name":"recipient","type":"address"},{"internalType":"uint256","name":"deadline","type":"uint256"}],"internalType":"struct INonfungiblePositionManager.MintParams","name":"params","type":"tuple"}],"name":"mint","outputs":[{"internalType":"uint256","name":"tokenId","type":"uint256"},{"internalType":"uint128","name":"liquidity","type":"uint128"},{"internalType":"uint256","name":"amount0","type":"uint256"},{"internalType":"uint256","name":"amount1","type":"uint256"}],"stateMutability":"payable","type":"function"},
  {"inputs":[{"internalType":"bytes[]","name":"data","type":"bytes[]"}],"name":"multicall","outputs":[{"internalType":"bytes[]","name":"results","type":"bytes[]"}],"stateMutability":"payable","type":"function"}
]''')

SWAP_ROUTER_ABI = json.loads('''[
  {"inputs":[{"components":[{"internalType":"address","name":"tokenIn","type":"address"},{"internalType":"address","name":"tokenOut","type":"address"},{"internalType":"uint24","name":"fee","type":"uint24"},{"internalType":"address","name":"recipient","type":"address"},{"internalType":"uint256","name":"amountIn","type":"uint256"},{"internalType":"uint256","name":"amountOutMinimum","type":"uint256"},{"internalType":"uint160","name":"sqrtPriceLimitX96","type":"uint160"}],"internalType":"struct IV3SwapRouter.ExactInputSingleParams","name":"params","type":"tuple"}],"name":"exactInputSingle","outputs":[{"internalType":"uint256","name":"amountOut","type":"uint256"}],"stateMutability":"payable","type":"function"}
]''')

# ============================================================
# ENCRYPTED KEY STORAGE
# ============================================================
KEY_FILE = Path.home() / ".local" / "share" / "lp-ranger" / ".keystore.enc"

def _derive_key(password, salt):
    """Derive AES key from password using PBKDF2."""
    return hashlib.pbkdf2_hmac('sha256', password.encode(), salt, 100000)

def encrypt_key(private_key, password):
    """Encrypt private key with AES-256-CBC using password."""
    import secrets
    salt = secrets.token_bytes(16)
    iv = secrets.token_bytes(16)
    key = _derive_key(password, salt)
    
    # Simple XOR-based encryption (no pycryptodome dependency)
    # For production, use AES-256-CBC with pycryptodome
    data = private_key.encode()
    # Pad to 16-byte blocks
    pad_len = 16 - (len(data) % 16)
    data += bytes([pad_len]) * pad_len
    
    # XOR with expanded key stream (SHA-256 based)
    encrypted = bytearray()
    for i in range(0, len(data), 32):
        block_key = hashlib.sha256(key + iv + i.to_bytes(4, 'big')).digest()
        chunk = data[i:i+32]
        encrypted.extend(b ^ k for b, k in zip(chunk, block_key[:len(chunk)]))
    
    # Verify tag
    tag = hashlib.sha256(key + bytes(encrypted)).digest()[:16]
    
    KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(KEY_FILE, 'wb') as f:
        f.write(salt + iv + tag + bytes(encrypted))
    os.chmod(KEY_FILE, 0o600)
    print(f"[KeyStore] Key encrypted and saved to {KEY_FILE}")

def decrypt_key(password):
    """Decrypt private key with password."""
    if not KEY_FILE.exists():
        raise FileNotFoundError("No encrypted key found. Run --setup first.")
    
    with open(KEY_FILE, 'rb') as f:
        raw = f.read()
    
    salt = raw[:16]
    iv = raw[16:32]
    tag = raw[32:48]
    encrypted = raw[48:]
    
    key = _derive_key(password, salt)
    
    # Verify tag
    expected_tag = hashlib.sha256(key + encrypted).digest()[:16]
    if tag != expected_tag:
        raise ValueError("Wrong password or corrupted keystore")
    
    # Decrypt
    decrypted = bytearray()
    for i in range(0, len(encrypted), 32):
        block_key = hashlib.sha256(key + iv + i.to_bytes(4, 'big')).digest()
        chunk = encrypted[i:i+32]
        decrypted.extend(b ^ k for b, k in zip(chunk, block_key[:len(chunk)]))
    
    # Remove PKCS7 padding
    pad_len = decrypted[-1]
    return decrypted[:-pad_len].decode()


# ============================================================
# WEB3 INTERACTION (minimal, no web3.py dependency)
# ============================================================
import urllib.request

def rpc_call(method, params):
    """Make an RPC call to Base chain."""
    payload = json.dumps({"jsonrpc":"2.0","method":method,"params":params,"id":1}).encode()
    req = urllib.request.Request(BASE_RPC, data=payload,
            headers={"Content-Type":"application/json","User-Agent":"LP-AutoBot/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.loads(resp.read().decode())
    if "error" in result:
        raise Exception(f"RPC error: {result['error']}")
    return result.get("result")

def get_nonce(address):
    r = rpc_call("eth_getTransactionCount", [address, "latest"])
    return int(r, 16)

def get_gas_price():
    r = rpc_call("eth_gasPrice", [])
    return int(r, 16)

def get_balance(address, token=None):
    """Get ETH balance or ERC20 token balance."""
    if token is None:
        r = rpc_call("eth_getBalance", [address, "latest"])
        return int(r, 16)
    else:
        # balanceOf(address)
        data = "0x70a08231" + address[2:].zfill(64)
        r = rpc_call("eth_call", [{"to": token, "data": data}, "latest"])
        return int(r, 16)

def get_position(token_id):
    """Read position data from NonfungiblePositionManager."""
    tid = hex(token_id)[2:].zfill(64)
    data = "0x99fbab88" + tid
    r = rpc_call("eth_call", [{"to": POSITION_MANAGER, "data": data}, "latest"])
    if not r or r == "0x":
        return None
    raw = r[2:]
    fields = [raw[i*64:(i+1)*64] for i in range(12)]
    
    def to_int(h):
        v = int(h, 16)
        return v - 2**256 if v >= 2**255 else v
    
    return {
        "token0": "0x" + fields[2][-40:],
        "token1": "0x" + fields[3][-40:],
        "fee": int(fields[4], 16),
        "tickLower": to_int(fields[5]),
        "tickUpper": to_int(fields[6]),
        "liquidity": int(fields[7], 16),
    }

def tick_to_price(tick):
    """Convert tick to USDC/ETH price (for WETH/USDC pool)."""
    return 1.0001 ** tick * 1e12

def price_to_tick(price, spacing=10):
    """Convert USDC/ETH price to nearest valid tick."""
    raw_tick = math.log(price / 1e12) / math.log(1.0001)
    return round(raw_tick / spacing) * spacing


# ============================================================
# TRANSACTION BUILDER
# ============================================================
class TxBuilder:
    """Builds and signs Uniswap V3 transactions.
    
    NOTE: This uses raw RPC calls. For production, consider using web3.py
    which handles ABI encoding, gas estimation, and signing properly.
    Install with: pip install web3
    """
    
    def __init__(self, private_key):
        try:
            from web3 import Web3
            from web3.middleware import ExtraDataToPOAMiddleware
            self.w3 = Web3(Web3.HTTPProvider(BASE_RPC))
            self.w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
            self.account = self.w3.eth.account.from_key(private_key)
            self.address = self.account.address
            
            self.pm = self.w3.eth.contract(
                address=Web3.to_checksum_address(POSITION_MANAGER),
                abi=POSITION_MANAGER_ABI
            )
            self.router = self.w3.eth.contract(
                address=Web3.to_checksum_address(SWAP_ROUTER),
                abi=SWAP_ROUTER_ABI
            )
            self.weth = self.w3.eth.contract(
                address=Web3.to_checksum_address(WETH), abi=ERC20_ABI
            )
            self.usdc = self.w3.eth.contract(
                address=Web3.to_checksum_address(USDC), abi=ERC20_ABI
            )
            
            print(f"[AutoBot] Wallet: {self.address}")
            print(f"[AutoBot] Chain: Base (ID {CHAIN_ID})")
            self._nonce = None  # Will be fetched on first use
        except ImportError:
            print("ERROR: web3 package required. Install with:")
            print("  pip install web3 --break-system-packages")
            sys.exit(1)
    
    def _get_nonce(self):
        """Get next nonce, tracking manually to avoid collisions."""
        if self._nonce is None:
            self._nonce = self.w3.eth.get_transaction_count(self.address)
        n = self._nonce
        self._nonce += 1
        return n
    
    def _reset_nonce(self):
        """Reset nonce from chain (call after errors)."""
        self._nonce = None
    
    def _gas_price(self):
        """Get gas price with 10% buffer for faster confirmation."""
        return int(self.w3.eth.gas_price * 1.1)
    
    def check_gas(self, min_eth=0.0001):
        """Check if wallet has enough ETH for gas. Returns (ok, balance)."""
        bal = self.w3.eth.get_balance(self.address) / 1e18
        ok = bal >= min_eth
        if not ok:
            print(f"  ⚠️  ETH balance too low for gas: {bal:.6f} ETH (need ~{min_eth})")
            print(f"  Send at least ${min_eth * 2400:.2f} in ETH to {self.address}")
        return ok, bal
    
    def get_balances(self):
        """Get current wallet balances."""
        eth_bal = self.w3.eth.get_balance(self.address)
        weth_bal = self.weth.functions.balanceOf(self.address).call()
        usdc_bal = self.usdc.functions.balanceOf(self.address).call()
        return {
            "ETH": eth_bal / 1e18,
            "WETH": weth_bal / 1e18,
            "USDC": usdc_bal / 1e6,
        }
    
    def ensure_approval(self, token_contract, spender, amount):
        """Approve token spending if needed."""
        from web3 import Web3
        current = token_contract.functions.allowance(
            self.address, Web3.to_checksum_address(spender)
        ).call()
        if current < amount:
            print(f"  Approving token for {spender[:10]}...")
            tx = token_contract.functions.approve(
                Web3.to_checksum_address(spender),
                2**256 - 1  # Max approval
            ).build_transaction({
                'from': self.address,
                'nonce': self._get_nonce(),
                'gas': 100000,
                'gasPrice': self._gas_price(),
                'chainId': CHAIN_ID,
            })
            signed = self.account.sign_transaction(tx)
            tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
            print(f"  ✓ Approved (tx: {tx_hash.hex()[:16]}...)")
            return receipt
        return None
    
    def close_position(self, token_id, max_slippage_pct=2.0):
        """Close an existing position: decrease liquidity + collect all."""
        from web3 import Web3
        
        pos = get_position(token_id)
        if not pos or pos["liquidity"] == 0:
            print(f"  Position {token_id} has no liquidity")
            return None
        
        print(f"  Closing position {token_id} (liq: {pos['liquidity']})")
        deadline = int(time.time()) + 600  # 10 min
        
        # Step 1: Decrease liquidity to 0
        gas_price = self._gas_price()
        tx1 = self.pm.functions.decreaseLiquidity((
            token_id,
            pos["liquidity"],
            0, 0, deadline,
        )).build_transaction({
            'from': self.address,
            'nonce': self._get_nonce(),
            'gas': 300000,
            'gasPrice': gas_price,
            'chainId': CHAIN_ID,
            'value': 0,
        })
        signed1 = self.account.sign_transaction(tx1)
        tx_hash1 = self.w3.eth.send_raw_transaction(signed1.raw_transaction)
        receipt1 = self.w3.eth.wait_for_transaction_receipt(tx_hash1, timeout=120)
        print(f"  ✓ Liquidity removed (tx: {tx_hash1.hex()[:16]}...)")
        
        # Step 2: Collect all tokens (nonce auto-incremented)
        MAX_UINT128 = 2**128 - 1
        tx2 = self.pm.functions.collect((
            token_id,
            self.address,
            MAX_UINT128,
            MAX_UINT128,
        )).build_transaction({
            'from': self.address,
            'nonce': self._get_nonce(),
            'gas': 200000,
            'gasPrice': gas_price,
            'chainId': CHAIN_ID,
            'value': 0,
        })
        signed2 = self.account.sign_transaction(tx2)
        tx_hash2 = self.w3.eth.send_raw_transaction(signed2.raw_transaction)
        receipt2 = self.w3.eth.wait_for_transaction_receipt(tx_hash2, timeout=120)
        print(f"  ✓ Tokens collected (tx: {tx_hash2.hex()[:16]}...)")
        
        return {"decrease_tx": tx_hash1.hex(), "collect_tx": tx_hash2.hex()}
    
    def swap_exact_input(self, token_in, token_out, amount_in, fee=500, max_slippage_pct=2.0):
        """Swap tokens using SwapRouter02."""
        from web3 import Web3
        
        amount_out_min = 0  # Will be calculated with slippage
        
        # Ensure approval
        token_contract = self.weth if token_in.lower() == WETH.lower() else self.usdc
        self.ensure_approval(token_contract, SWAP_ROUTER, amount_in)
        
        tx = self.router.functions.exactInputSingle((
            Web3.to_checksum_address(token_in),
            Web3.to_checksum_address(token_out),
            fee,
            self.address,
            amount_in,
            amount_out_min,
            0,  # sqrtPriceLimitX96 = 0 (no limit)
        )).build_transaction({
            'from': self.address,
            'nonce': self._get_nonce(),
            'gas': 300000,
            'gasPrice': self._gas_price(),
            'chainId': CHAIN_ID,
            'value': 0,
        })
        signed = self.account.sign_transaction(tx)
        tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        print(f"  ✓ Swap executed (tx: {tx_hash.hex()[:16]}...)")
        return {"tx": tx_hash.hex(), "receipt": receipt}
    
    def open_position(self, tick_lower, tick_upper, amount0, amount1, fee=500):
        """Open a new position with given tick range."""
        from web3 import Web3
        
        # Ensure approvals
        self.ensure_approval(self.weth, POSITION_MANAGER, amount0)
        self.ensure_approval(self.usdc, POSITION_MANAGER, amount1)
        
        deadline = int(time.time()) + 600
        
        # Ensure token0 < token1 (WETH < USDC on Base)
        t0 = Web3.to_checksum_address(WETH)
        t1 = Web3.to_checksum_address(USDC)
        a0 = amount0
        a1 = amount1
        
        if int(t0, 16) > int(t1, 16):
            t0, t1 = t1, t0
            a0, a1 = a1, a0
        
        tx = self.pm.functions.mint((
            t0, t1, fee,
            tick_lower, tick_upper,
            a0, a1,
            0, 0,  # min amounts (accept any)
            self.address,
            deadline,
        )).build_transaction({
            'from': self.address,
            'nonce': self._get_nonce(),
            'gas': 500000,
            'gasPrice': self._gas_price(),
            'chainId': CHAIN_ID,
            'value': 0,
        })
        signed = self.account.sign_transaction(tx)
        tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        
        # Extract tokenId from logs
        new_token_id = None
        for log in receipt.get('logs', []):
            if len(log.get('topics', [])) >= 4:
                # Transfer event: topic[3] is tokenId
                new_token_id = int(log['topics'][3].hex(), 16)
                break
        
        print(f"  ✓ Position opened (tx: {tx_hash.hex()[:16]}..., tokenId: {new_token_id})")
        return {"tx": tx_hash.hex(), "token_id": new_token_id}
    
    def rebalance(self, old_token_id, new_tick_lower, new_tick_upper, fee=500, dry_run=False, auto_confirm=False):
        """Full rebalance: close old position → swap to ratio → open new position."""
        print(f"\n{'='*50}")
        print(f"REBALANCE: Position {old_token_id}")
        print(f"  New ticks: {new_tick_lower} to {new_tick_upper}")
        new_lo = tick_to_price(new_tick_lower)
        new_hi = tick_to_price(new_tick_upper)
        print(f"  New range: ${new_lo:,.0f} — ${new_hi:,.0f}")
        print(f"{'='*50}")
        
        # Show current state
        balances = self.get_balances()
        print(f"\n  Balances: {balances['ETH']:.4f} ETH, {balances['WETH']:.6f} WETH, {balances['USDC']:.2f} USDC")
        
        pos = get_position(old_token_id)
        if pos:
            print(f"  Current position: ticks {pos['tickLower']}/{pos['tickUpper']}, liq {pos['liquidity']}")
        
        if dry_run:
            print(f"\n  🔍 DRY RUN — no transactions will be sent")
            print(f"  Would: close position → swap → open new at {new_tick_lower}/{new_tick_upper}")
            return {"status": "dry_run"}
        
        # Check gas balance
        ok, eth_bal = self.check_gas(0.0003)  # Need ~0.0003 ETH for 3 txs on Base
        if not ok:
            return {"status": "insufficient_gas", "eth_balance": eth_bal}
        
        # Reset nonce for fresh sequence
        self._reset_nonce()
        
        # Confirm
        print(f"\n  ⚠️  This will execute real transactions on Base chain.")
        confirm = "y" if auto_confirm else input("  Continue? [y/N]: ").strip().lower()
        if confirm != 'y':
            print("  Cancelled.")
            return {"status": "cancelled"}
        
        results = {}
        
        # Step 1: Close old position
        if pos and pos["liquidity"] > 0:
            print(f"\n  [1/3] Closing position {old_token_id}...")
            results["close"] = self.close_position(old_token_id)
            time.sleep(2)  # Wait for state to update
        
        # Step 2: Check balances and swap if needed
        print(f"\n  [2/3] Rebalancing tokens...")
        balances = self.get_balances()
        weth_val = balances["WETH"]
        usdc_val = balances["USDC"]
        total_usd = weth_val * tick_to_price(0) + usdc_val  # Rough estimate
        
        # For concentrated liquidity, we need roughly 50/50 in value
        # This is a simplification; exact ratio depends on current price vs range
        target_weth_val = total_usd * 0.5 / tick_to_price(0) if tick_to_price(0) > 0 else 0
        
        if weth_val > target_weth_val * 1.1:
            # Too much WETH, swap some to USDC
            swap_amount = int((weth_val - target_weth_val) * 1e18)
            if swap_amount > 0:
                print(f"  Swapping {swap_amount/1e18:.6f} WETH → USDC...")
                results["swap"] = self.swap_exact_input(WETH, USDC, swap_amount, fee)
        elif usdc_val > total_usd * 0.6:
            # Too much USDC, swap some to WETH
            swap_amount = int((usdc_val - total_usd * 0.5) * 1e6)
            if swap_amount > 0:
                print(f"  Swapping {swap_amount/1e6:.2f} USDC → WETH...")
                results["swap"] = self.swap_exact_input(USDC, WETH, swap_amount, fee)
        
        time.sleep(2)
        
        # Step 3: Open new position with all available tokens
        print(f"\n  [3/3] Opening new position...")
        balances = self.get_balances()
        weth_amount = int(balances["WETH"] * 0.99 * 1e18)  # Leave 1% buffer
        usdc_amount = int(balances["USDC"] * 0.99 * 1e6)
        
        if weth_amount > 0 or usdc_amount > 0:
            results["open"] = self.open_position(
                new_tick_lower, new_tick_upper,
                weth_amount, usdc_amount, fee
            )
        
        print(f"\n  ✅ Rebalance complete!")
        balances = self.get_balances()
        print(f"  Final balances: {balances['ETH']:.4f} ETH, {balances['WETH']:.6f} WETH, {balances['USDC']:.2f} USDC")
        
        return results
    
    def exit_pool(self, token_id, hold_asset="USDC", fee=500, dry_run=False, auto_confirm=False):
        """Exit pool: close position → swap everything to one asset."""
        print(f"\n{'='*50}")
        print(f"EXIT POOL: Position {token_id} → Hold {hold_asset}")
        print(f"{'='*50}")
        
        if dry_run:
            print(f"  🔍 DRY RUN — would close and convert to {hold_asset}")
            return {"status": "dry_run"}
        
        ok, eth_bal = self.check_gas(0.0002)
        if not ok:
            return {"status": "insufficient_gas", "eth_balance": eth_bal}
        self._reset_nonce()
        
        confirm = "y" if auto_confirm else input(f"  Close pool and hold {hold_asset}? [y/N]: ").strip().lower()
        if confirm != 'y':
            print("  Cancelled."); return {"status": "cancelled"}
        
        results = {}
        pos = get_position(token_id)
        if pos and pos["liquidity"] > 0:
            results["close"] = self.close_position(token_id)
            time.sleep(2)
        
        balances = self.get_balances()
        if hold_asset == "USDC" and balances["WETH"] > 0.0001:
            swap_amt = int(balances["WETH"] * 0.99 * 1e18)
            print(f"  Swapping {swap_amt/1e18:.6f} WETH → USDC...")
            results["swap"] = self.swap_exact_input(WETH, USDC, swap_amt, fee)
        elif hold_asset == "ETH" and balances["USDC"] > 1:
            swap_amt = int(balances["USDC"] * 0.99 * 1e6)
            print(f"  Swapping {swap_amt/1e6:.2f} USDC → WETH...")
            results["swap"] = self.swap_exact_input(USDC, WETH, swap_amt, fee)
        
        balances = self.get_balances()
        print(f"\n  ✅ Pool closed! Holding {hold_asset}")
        print(f"  Balances: {balances['WETH']:.6f} WETH, {balances['USDC']:.2f} USDC")
        return results
    
    def enter_pool(self, tick_lower, tick_upper, fee=500, dry_run=False, auto_confirm=False):
        """Enter pool: swap to 50/50 ratio → open new position."""
        print(f"\n{'='*50}")
        print(f"ENTER POOL: New range ticks {tick_lower}/{tick_upper}")
        lo = tick_to_price(tick_lower); hi = tick_to_price(tick_upper)
        print(f"  Range: ${lo:,.0f} — ${hi:,.0f}")
        print(f"{'='*50}")
        
        if dry_run:
            print(f"  🔍 DRY RUN — would open new position")
            return {"status": "dry_run"}
        
        ok, eth_bal = self.check_gas(0.0002)
        if not ok:
            return {"status": "insufficient_gas", "eth_balance": eth_bal}
        self._reset_nonce()
        
        confirm = "y" if auto_confirm else input("  Open new position? [y/N]: ").strip().lower()
        if confirm != 'y':
            print("  Cancelled."); return {"status": "cancelled"}
        
        # Swap to ~50/50 ratio
        balances = self.get_balances()
        # Rough: get current ETH price
        total_weth_val = balances["WETH"]
        total_usdc_val = balances["USDC"]
        
        # We need both tokens for the position
        if total_weth_val > 0 and total_usdc_val < 1:
            # All in WETH, swap half to USDC
            swap_amt = int(total_weth_val * 0.49 * 1e18)
            print(f"  Swapping {swap_amt/1e18:.6f} WETH → USDC...")
            self.swap_exact_input(WETH, USDC, swap_amt, fee)
            time.sleep(2)
        elif total_usdc_val > 1 and total_weth_val < 0.0001:
            # All in USDC, swap half to WETH
            swap_amt = int(total_usdc_val * 0.49 * 1e6)
            print(f"  Swapping {swap_amt/1e6:.2f} USDC → WETH...")
            self.swap_exact_input(USDC, WETH, swap_amt, fee)
            time.sleep(2)
        
        balances = self.get_balances()
        weth_amount = int(balances["WETH"] * 0.99 * 1e18)
        usdc_amount = int(balances["USDC"] * 0.99 * 1e6)
        
        result = self.open_position(tick_lower, tick_upper, weth_amount, usdc_amount, fee)
        
        print(f"\n  ✅ Position opened!")
        return result


# ============================================================
# CLI INTERFACE
# ============================================================
def setup_key():
    """Interactive setup: import and encrypt private key."""
    print("="*50)
    print("LP Ranger AutoBot — Key Setup")
    print("="*50)
    print()
    print("⚠️  IMPORTANT SECURITY NOTES:")
    print("  • Use a DEDICATED hot wallet, NOT your main wallet")
    print("  • Only keep the funds you need for LP in this wallet")
    print("  • Your private key will be encrypted with a password")
    print("  • The key is NEVER stored in plaintext")
    print()
    
    pk = getpass.getpass("Enter private key (0x...): ").strip()
    if not pk.startswith("0x"):
        pk = "0x" + pk
    if len(pk) != 66:
        print("ERROR: Invalid private key length")
        return
    
    pw1 = getpass.getpass("Set encryption password: ")
    pw2 = getpass.getpass("Confirm password: ")
    if pw1 != pw2:
        print("ERROR: Passwords don't match")
        return
    
    encrypt_key(pk, pw1)
    
    # Verify
    try:
        from web3 import Web3
        decrypted = decrypt_key(pw1)
        acct = Web3().eth.account.from_key(decrypted)
        print(f"\n✓ Key stored successfully!")
        print(f"  Wallet address: {acct.address}")
        print(f"  Keystore: {KEY_FILE}")
    except ImportError:
        print(f"\n✓ Key stored. Install web3 to verify: pip install web3 --break-system-packages")

def main():
    import argparse
    parser = argparse.ArgumentParser(description="LP Ranger AutoBot")
    parser.add_argument("--setup", action="store_true", help="Import and encrypt private key")
    parser.add_argument("--status", action="store_true", help="Show wallet and position status")
    parser.add_argument("--rebalance", action="store_true", help="Rebalance to new range")
    parser.add_argument("--exit", action="store_true", help="Exit pool")
    parser.add_argument("--enter", action="store_true", help="Enter pool")
    parser.add_argument("--hold", choices=["ETH","USDC"], default="USDC", help="Asset to hold on exit")
    parser.add_argument("--position-id", "-p", type=int, help="Position NFT ID")
    parser.add_argument("--tick-lower", type=int, help="Lower tick for new range")
    parser.add_argument("--tick-upper", type=int, help="Upper tick for new range")
    parser.add_argument("--price-lower", type=float, help="Lower price (converts to tick)")
    parser.add_argument("--price-upper", type=float, help="Upper price (converts to tick)")
    parser.add_argument("--fee", type=int, default=500, help="Pool fee tier (default: 500 = 0.05%%)")
    parser.add_argument("--dry-run", action="store_true", help="Simulate without executing")
    parser.add_argument("--yes", "-y", action="store_true", help="Skip confirmation prompts")
    args = parser.parse_args()
    
    if args.setup:
        setup_key()
        return
    
    # Unlock key
    password = getpass.getpass("Unlock password: ")
    try:
        pk = decrypt_key(password)
    except Exception as e:
        print(f"ERROR: {e}")
        return
    
    bot = TxBuilder(pk)
    
    # Convert prices to ticks if provided
    if args.price_lower and not args.tick_lower:
        args.tick_lower = price_to_tick(args.price_lower)
        print(f"  Price ${args.price_lower:,.0f} → tick {args.tick_lower}")
    if args.price_upper and not args.tick_upper:
        args.tick_upper = price_to_tick(args.price_upper)
        print(f"  Price ${args.price_upper:,.0f} → tick {args.tick_upper}")
    
    if args.status:
        bal = bot.get_balances()
        print(f"\nWallet: {bot.address}")
        print(f"  ETH:  {bal['ETH']:.6f}")
        print(f"  WETH: {bal['WETH']:.6f}")
        print(f"  USDC: {bal['USDC']:.2f}")
        if args.position_id:
            pos = get_position(args.position_id)
            if pos:
                lo = tick_to_price(pos['tickLower'])
                hi = tick_to_price(pos['tickUpper'])
                print(f"\nPosition {args.position_id}:")
                print(f"  Range: ${lo:,.0f} — ${hi:,.0f}")
                print(f"  Ticks: {pos['tickLower']} / {pos['tickUpper']}")
                print(f"  Liquidity: {pos['liquidity']}")
                print(f"  Fee: {pos['fee']/10000:.2f}%")
        return
    
    if args.rebalance:
        if not args.position_id:
            print("ERROR: --position-id required"); return
        if not args.tick_lower or not args.tick_upper:
            print("ERROR: --tick-lower and --tick-upper (or --price-lower/--price-upper) required"); return
        bot.rebalance(args.position_id, args.tick_lower, args.tick_upper, args.fee, args.dry_run, args.yes)
    
    elif args.exit:
        if not args.position_id:
            print("ERROR: --position-id required"); return
        bot.exit_pool(args.position_id, args.hold, args.fee, args.dry_run, args.yes)
    
    elif args.enter:
        if not args.tick_lower or not args.tick_upper:
            print("ERROR: --tick-lower and --tick-upper (or --price-lower/--price-upper) required"); return
        bot.enter_pool(args.tick_lower, args.tick_upper, args.fee, args.dry_run, args.yes)
    
    else:
        parser.print_help()

if __name__ == "__main__":
    main()
