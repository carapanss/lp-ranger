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
import threading
from pathlib import Path
from datetime import datetime

# ------------------------------------------------------------
# Safety knobs (conservative defaults — override via env vars).
# ------------------------------------------------------------
# Max acceptable adverse price move, fractional. E.g. 0.01 = 1%.
DEFAULT_SLIPPAGE = float(os.environ.get("LP_SLIPPAGE", "0.01"))
# Uniswap V3 absolute tick bounds.
MIN_TICK, MAX_TICK = -887272, 887272
# Pool fee tier → tick spacing.
FEE_TO_TICK_SPACING = {100: 1, 500: 10, 3000: 60, 10000: 200}

# ============================================================
# CONSTANTS — Base Chain Uniswap V3
# ============================================================
# Base RPC endpoints, tried in order. Override via LP_RPC_ENDPOINTS
# (comma-separated) to pin to private/paid providers.
_DEFAULT_RPCS = [
    "https://mainnet.base.org",
    "https://base.publicnode.com",
    "https://base.llamarpc.com",
]
_env_rpcs = os.environ.get("LP_RPC_ENDPOINTS", "")
RPC_ENDPOINTS = [u.strip() for u in _env_rpcs.split(",") if u.strip()] or _DEFAULT_RPCS
BASE_RPC = RPC_ENDPOINTS[0]  # Back-compat alias for direct rpc_call().
CHAIN_ID = 8453

# Uniswap V3 contracts on Base
POSITION_MANAGER = "0x03a520b32C04BF3bEEf7BEb72E919cf822Ed34f1"
SWAP_ROUTER = "0x2626664c2603336E57B271c5C0b26F421741e481"
FACTORY = "0x33128a8fC17869897dcE68Ed026d694621f6FDfD"

# Token addresses on Base
WETH = "0x4200000000000000000000000000000000000006"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"

# Gas reserve: extra ETH we keep in the wallet beyond the cost of the current
# operation so we don't run dry. ~0.002 ETH covers ~20 txs on Base at normal
# gas prices, which is plenty of headroom for a few days of unattended runs.
GAS_RESERVE_ETH = float(os.environ.get("LP_GAS_RESERVE_ETH", "0.002"))

# Compound-fees default: only collect+redeposit when estimated gas is at
# most this fraction of the fees we'd reinvest. 0.01 = 1%.
DEFAULT_COMPOUND_RATIO = float(os.environ.get("LP_COMPOUND_RATIO", "0.01"))

# ABI fragments (only what we need)
ERC20_ABI = json.loads('[{"constant":true,"inputs":[{"name":"","type":"address"}],"name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"type":"function"},{"constant":false,"inputs":[{"name":"spender","type":"address"},{"name":"amount","type":"uint256"}],"name":"approve","outputs":[{"name":"","type":"bool"}],"type":"function"},{"constant":true,"inputs":[{"name":"owner","type":"address"},{"name":"spender","type":"address"}],"name":"allowance","outputs":[{"name":"","type":"uint256"}],"type":"function"},{"constant":true,"inputs":[],"name":"decimals","outputs":[{"name":"","type":"uint8"}],"type":"function"}]')

POSITION_MANAGER_ABI = json.loads('''[
  {"inputs":[{"internalType":"uint256","name":"tokenId","type":"uint256"}],"name":"positions","outputs":[{"internalType":"uint96","name":"nonce","type":"uint96"},{"internalType":"address","name":"operator","type":"address"},{"internalType":"address","name":"token0","type":"address"},{"internalType":"address","name":"token1","type":"address"},{"internalType":"uint24","name":"fee","type":"uint24"},{"internalType":"int24","name":"tickLower","type":"int24"},{"internalType":"int24","name":"tickUpper","type":"int24"},{"internalType":"uint128","name":"liquidity","type":"uint128"},{"internalType":"uint256","name":"feeGrowthInside0LastX128","type":"uint256"},{"internalType":"uint256","name":"feeGrowthInside1LastX128","type":"uint256"},{"internalType":"uint128","name":"tokensOwed0","type":"uint128"},{"internalType":"uint128","name":"tokensOwed1","type":"uint128"}],"stateMutability":"view","type":"function"},
  {"inputs":[{"components":[{"internalType":"uint256","name":"tokenId","type":"uint256"},{"internalType":"uint128","name":"liquidity","type":"uint128"},{"internalType":"uint256","name":"amount0Min","type":"uint256"},{"internalType":"uint256","name":"amount1Min","type":"uint256"},{"internalType":"uint256","name":"deadline","type":"uint256"}],"internalType":"struct INonfungiblePositionManager.DecreaseLiquidityParams","name":"params","type":"tuple"}],"name":"decreaseLiquidity","outputs":[{"internalType":"uint256","name":"amount0","type":"uint256"},{"internalType":"uint256","name":"amount1","type":"uint256"}],"stateMutability":"payable","type":"function"},
  {"inputs":[{"components":[{"internalType":"uint256","name":"tokenId","type":"uint256"},{"internalType":"address","name":"recipient","type":"address"},{"internalType":"uint128","name":"amount0Max","type":"uint128"},{"internalType":"uint128","name":"amount1Max","type":"uint128"}],"internalType":"struct INonfungiblePositionManager.CollectParams","name":"params","type":"tuple"}],"name":"collect","outputs":[{"internalType":"uint256","name":"amount0","type":"uint256"},{"internalType":"uint256","name":"amount1","type":"uint256"}],"stateMutability":"payable","type":"function"},
  {"inputs":[{"components":[{"internalType":"uint256","name":"tokenId","type":"uint256"},{"internalType":"uint256","name":"amount0Desired","type":"uint256"},{"internalType":"uint256","name":"amount1Desired","type":"uint256"},{"internalType":"uint256","name":"amount0Min","type":"uint256"},{"internalType":"uint256","name":"amount1Min","type":"uint256"},{"internalType":"uint256","name":"deadline","type":"uint256"}],"internalType":"struct INonfungiblePositionManager.IncreaseLiquidityParams","name":"params","type":"tuple"}],"name":"increaseLiquidity","outputs":[{"internalType":"uint128","name":"liquidity","type":"uint128"},{"internalType":"uint256","name":"amount0","type":"uint256"},{"internalType":"uint256","name":"amount1","type":"uint256"}],"stateMutability":"payable","type":"function"},
  {"inputs":[{"components":[{"internalType":"address","name":"token0","type":"address"},{"internalType":"address","name":"token1","type":"address"},{"internalType":"uint24","name":"fee","type":"uint24"},{"internalType":"int24","name":"tickLower","type":"int24"},{"internalType":"int24","name":"tickUpper","type":"int24"},{"internalType":"uint256","name":"amount0Desired","type":"uint256"},{"internalType":"uint256","name":"amount1Desired","type":"uint256"},{"internalType":"uint256","name":"amount0Min","type":"uint256"},{"internalType":"uint256","name":"amount1Min","type":"uint256"},{"internalType":"address","name":"recipient","type":"address"},{"internalType":"uint256","name":"deadline","type":"uint256"}],"internalType":"struct INonfungiblePositionManager.MintParams","name":"params","type":"tuple"}],"name":"mint","outputs":[{"internalType":"uint256","name":"tokenId","type":"uint256"},{"internalType":"uint128","name":"liquidity","type":"uint128"},{"internalType":"uint256","name":"amount0","type":"uint256"},{"internalType":"uint256","name":"amount1","type":"uint256"}],"stateMutability":"payable","type":"function"},
  {"inputs":[{"internalType":"bytes[]","name":"data","type":"bytes[]"}],"name":"multicall","outputs":[{"internalType":"bytes[]","name":"results","type":"bytes[]"}],"stateMutability":"payable","type":"function"}
]''')

SWAP_ROUTER_ABI = json.loads('''[
  {"inputs":[{"components":[{"internalType":"address","name":"tokenIn","type":"address"},{"internalType":"address","name":"tokenOut","type":"address"},{"internalType":"uint24","name":"fee","type":"uint24"},{"internalType":"address","name":"recipient","type":"address"},{"internalType":"uint256","name":"amountIn","type":"uint256"},{"internalType":"uint256","name":"amountOutMinimum","type":"uint256"},{"internalType":"uint160","name":"sqrtPriceLimitX96","type":"uint160"}],"internalType":"struct IV3SwapRouter.ExactInputSingleParams","name":"params","type":"tuple"}],"name":"exactInputSingle","outputs":[{"internalType":"uint256","name":"amountOut","type":"uint256"}],"stateMutability":"payable","type":"function"}
]''')

FACTORY_ABI = json.loads('''[
  {"inputs":[{"internalType":"address","name":"tokenA","type":"address"},{"internalType":"address","name":"tokenB","type":"address"},{"internalType":"uint24","name":"fee","type":"uint24"}],"name":"getPool","outputs":[{"internalType":"address","name":"pool","type":"address"}],"stateMutability":"view","type":"function"}
]''')

POOL_ABI = json.loads('''[
  {"inputs":[],"name":"slot0","outputs":[{"internalType":"uint160","name":"sqrtPriceX96","type":"uint160"},{"internalType":"int24","name":"tick","type":"int24"},{"internalType":"uint16","name":"observationIndex","type":"uint16"},{"internalType":"uint16","name":"observationCardinality","type":"uint16"},{"internalType":"uint16","name":"observationCardinalityNext","type":"uint16"},{"internalType":"uint8","name":"feeProtocol","type":"uint8"},{"internalType":"bool","name":"unlocked","type":"bool"}],"stateMutability":"view","type":"function"},
  {"inputs":[],"name":"token0","outputs":[{"internalType":"address","name":"","type":"address"}],"stateMutability":"view","type":"function"},
  {"inputs":[],"name":"token1","outputs":[{"internalType":"address","name":"","type":"address"}],"stateMutability":"view","type":"function"},
  {"inputs":[],"name":"tickSpacing","outputs":[{"internalType":"int24","name":"","type":"int24"}],"stateMutability":"view","type":"function"}
]''')

# ============================================================
# ENCRYPTED KEY STORAGE — AES-256-GCM + Scrypt (keystore v2)
#
# Legacy v1 keystores (XOR-with-SHA256 + PBKDF2) still decrypt and are
# migrated in place on the next successful unlock; a backup is kept in
# .keystore.enc.legacy-backup.
# ============================================================
KEY_FILE = Path.home() / ".local" / "share" / "lp-ranger" / ".keystore.enc"

# v2 binary layout:
#   [4]  magic = b"LPR2"
#   [1]  version = 0x01
#   [1]  kdf_id  = 0x01 (scrypt)
#   [2]  reserved (flags, currently 0x0000)
#   [16] salt
#   [12] nonce
#   [..] ciphertext + GCM tag (AEAD appends a 16-byte tag)
_KEYSTORE_MAGIC = b"LPR2"
_KEYSTORE_VERSION = 0x01
_KDF_SCRYPT = 0x01
_KEYSTORE_HEADER_LEN = 4 + 1 + 1 + 2 + 16 + 12  # 36
# Scrypt params: ~128 MB RAM, a few hundred ms on a modern CPU.
# Tuned for once-per-session unlock; adjust with caution (see migration note).
_SCRYPT_N = 2 ** 17
_SCRYPT_R = 8
_SCRYPT_P = 1
_KEY_LEN = 32  # AES-256

def _require_cryptography():
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: F401
        from cryptography.hazmat.primitives.kdf.scrypt import Scrypt  # noqa: F401
    except ImportError as e:
        raise RuntimeError(
            "Missing dependency 'cryptography'. Install with:\n"
            "  pip install cryptography --break-system-packages"
        ) from e

def _scrypt_key(password: str, salt: bytes) -> bytes:
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    return Scrypt(
        salt=salt, length=_KEY_LEN, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P
    ).derive(password.encode())

def _atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / (path.name + ".tmp")
    with open(tmp, "wb") as f:
        f.write(data)
    os.chmod(tmp, mode)
    os.replace(tmp, path)
    os.chmod(path, mode)

def encrypt_key(private_key, password):
    """Encrypt private key with AES-256-GCM using a Scrypt-derived key."""
    _require_cryptography()
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    import secrets
    salt = secrets.token_bytes(16)
    nonce = secrets.token_bytes(12)
    key = _scrypt_key(password, salt)
    ct = AESGCM(key).encrypt(nonce, private_key.encode(), None)
    header = _KEYSTORE_MAGIC + bytes([_KEYSTORE_VERSION, _KDF_SCRYPT, 0x00, 0x00])
    _atomic_write(KEY_FILE, header + salt + nonce + ct)
    print(f"[KeyStore] Key encrypted (AES-256-GCM) and saved to {KEY_FILE}")

def _decrypt_v2(raw: bytes, password: str) -> str:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.exceptions import InvalidTag
    if len(raw) < _KEYSTORE_HEADER_LEN + 16:
        raise ValueError("Corrupted keystore (truncated)")
    version = raw[4]
    kdf_id = raw[5]
    if version != _KEYSTORE_VERSION:
        raise ValueError(f"Unsupported keystore version: {version}")
    if kdf_id != _KDF_SCRYPT:
        raise ValueError(f"Unsupported KDF id: {kdf_id}")
    salt = raw[8:24]
    nonce = raw[24:36]
    ct = raw[36:]
    key = _scrypt_key(password, salt)
    try:
        pt = AESGCM(key).decrypt(nonce, ct, None)
    except InvalidTag:
        raise ValueError("Wrong password or corrupted keystore")
    return pt.decode()

def _decrypt_legacy_v1(raw: bytes, password: str) -> str:
    """Legacy XOR+SHA256 keystore (v1). Kept only to enable migration to v2."""
    if len(raw) < 48:
        raise ValueError("Corrupted keystore (truncated)")
    salt = raw[:16]
    iv = raw[16:32]
    tag = raw[32:48]
    encrypted = raw[48:]
    key = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, 100000)
    expected_tag = hashlib.sha256(key + encrypted).digest()[:16]
    if tag != expected_tag:
        raise ValueError("Wrong password or corrupted keystore")
    decrypted = bytearray()
    for i in range(0, len(encrypted), 32):
        block_key = hashlib.sha256(key + iv + i.to_bytes(4, 'big')).digest()
        chunk = encrypted[i:i+32]
        decrypted.extend(b ^ k for b, k in zip(chunk, block_key[:len(chunk)]))
    pad_len = decrypted[-1]
    return decrypted[:-pad_len].decode()

def decrypt_key(password):
    """Decrypt private key.

    Transparently supports v2 (AES-256-GCM) and legacy v1 (XOR) keystores.
    Legacy v1 keystores are re-encrypted to v2 in place on first successful
    unlock; the original file is copied to ``.keystore.enc.legacy-backup``.
    """
    if not KEY_FILE.exists():
        raise FileNotFoundError("No encrypted key found. Run --setup first.")
    with open(KEY_FILE, "rb") as f:
        raw = f.read()
    if raw.startswith(_KEYSTORE_MAGIC):
        _require_cryptography()
        return _decrypt_v2(raw, password)

    # Legacy path. Decrypt first, then attempt in-place upgrade.
    pk = _decrypt_legacy_v1(raw, password)
    try:
        _require_cryptography()
        backup = KEY_FILE.with_name(KEY_FILE.name + ".legacy-backup")
        if not backup.exists():
            with open(backup, "wb") as f:
                f.write(raw)
            os.chmod(backup, 0o600)
        encrypt_key(pk, password)
        print(
            f"[KeyStore] Legacy keystore migrated to AES-256-GCM. "
            f"Backup saved to {backup}."
        )
    except Exception as e:
        # Migration is best-effort; never block unlock on it.
        print(
            f"[KeyStore] WARNING: legacy keystore in use, migration to v2 failed: {e}",
            file=sys.stderr,
        )
    return pk


# ============================================================
# WEB3 INTERACTION (minimal, no web3.py dependency)
# ============================================================
import urllib.request

def rpc_call(method, params, endpoints=None):
    """Make an RPC call, failing over across ``endpoints`` on error."""
    urls = endpoints or RPC_ENDPOINTS
    payload = json.dumps({"jsonrpc":"2.0","method":method,"params":params,"id":1}).encode()
    last_err = None
    for url in urls:
        try:
            req = urllib.request.Request(
                url, data=payload,
                headers={"Content-Type":"application/json","User-Agent":"LP-AutoBot/1.0"},
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read().decode())
            if "error" in result:
                last_err = Exception(f"RPC error from {url}: {result['error']}")
                continue
            return result.get("result")
        except Exception as e:
            last_err = e
            continue
    raise RuntimeError(f"All RPC endpoints failed; last error: {last_err}")

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
    if not (MIN_TICK <= tick <= MAX_TICK):
        raise ValueError(f"tick {tick} out of Uniswap V3 bounds")
    return 1.0001 ** tick * 1e12

def _sqrt_ratio_at_tick(tick):
    """Q64.96 sqrt price at a given tick (float approximation).

    Matches Uniswap's TickMath within a handful of wei for the ranges we
    use; precise enough to produce conservative ``amountMin`` bounds.
    """
    if not (MIN_TICK <= tick <= MAX_TICK):
        raise ValueError(f"tick {tick} out of Uniswap V3 bounds")
    return int((1.0001 ** (tick / 2)) * (2 ** 96))

def price_to_tick(price, spacing=10):
    """Convert USDC/ETH price to nearest valid, spacing-aligned tick.

    Always returns a tick that is both inside the protocol bounds
    [MIN_TICK, MAX_TICK] and aligned to ``spacing``.
    """
    if price <= 0:
        raise ValueError(f"price must be positive, got {price}")
    raw_tick = math.log(price / 1e12) / math.log(1.0001)
    t = int(round(raw_tick / spacing)) * spacing
    # Clamp to the largest spacing-aligned tick inside the protocol bounds.
    max_aligned = (MAX_TICK // spacing) * spacing
    min_aligned = -max_aligned
    return max(min_aligned, min(max_aligned, t))


# ============================================================
# TRANSACTION BUILDER
# ============================================================
class TxBuilder:
    """Builds and signs Uniswap V3 transactions.
    
    NOTE: This uses raw RPC calls. For production, consider using web3.py
    which handles ABI encoding, gas estimation, and signing properly.
    Install with: pip install web3
    """
    
    def __init__(self, private_key, slippage=DEFAULT_SLIPPAGE):
        try:
            from web3 import Web3
            from web3.middleware import ExtraDataToPOAMiddleware
            # Try each RPC endpoint until one responds. This gives the bot
            # a fighting chance when mainnet.base.org is having an outage.
            last_err = None
            self.w3 = None
            for url in RPC_ENDPOINTS:
                try:
                    w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 30}))
                    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
                    if w3.is_connected():
                        self.w3 = w3
                        print(f"[AutoBot] RPC: {url}")
                        break
                except Exception as e:
                    last_err = e
                    continue
            if self.w3 is None:
                raise RuntimeError(f"All RPCs unreachable; last error: {last_err}")

            # Wrap the provider so rate limits / 5xx / timeouts rotate across
            # endpoints instead of crashing the tx in flight. Without this a
            # 429 from the public Base RPC mid-rebalance leaves the position
            # closed + a half-filled wallet — ask me how I know.
            self._install_rpc_failover()
            self.account = self.w3.eth.account.from_key(private_key)
            self.address = self.account.address
            self.slippage = slippage

            self.pm = self.w3.eth.contract(
                address=Web3.to_checksum_address(POSITION_MANAGER),
                abi=POSITION_MANAGER_ABI
            )
            self.router = self.w3.eth.contract(
                address=Web3.to_checksum_address(SWAP_ROUTER),
                abi=SWAP_ROUTER_ABI
            )
            self.factory = self.w3.eth.contract(
                address=Web3.to_checksum_address(FACTORY),
                abi=FACTORY_ABI
            )
            self.weth = self.w3.eth.contract(
                address=Web3.to_checksum_address(WETH), abi=ERC20_ABI
            )
            self.usdc = self.w3.eth.contract(
                address=Web3.to_checksum_address(USDC), abi=ERC20_ABI
            )

            print(f"[AutoBot] Wallet: {self.address}")
            print(f"[AutoBot] Chain: Base (ID {CHAIN_ID})")
            print(f"[AutoBot] Slippage tolerance: {self.slippage*100:.2f}%")
            self._nonce = None  # Will be fetched on first use
            self._nonce_lock = threading.Lock()
            self._pool_cache = {}  # fee -> pool address (checksummed)
        except ImportError:
            print("ERROR: web3 package required. Install with:")
            print("  pip install web3 --break-system-packages")
            sys.exit(1)

    def _install_rpc_failover(self):
        """Wrap the provider's make_request so rate limits and transient
        HTTP errors rotate to the next endpoint instead of exploding.

        This survives the web3 provider caching its session — we build a
        fresh HTTPProvider on each rotation and reuse the new session for
        subsequent calls.
        """
        from web3 import Web3
        import requests

        w3 = self.w3
        endpoints = list(RPC_ENDPOINTS)
        state = {"idx": 0}
        # Keep the original bound make_request so we can call it cleanly.
        orig = w3.provider.make_request

        def rotating(method, params):
            last_err = None
            max_attempts = max(len(endpoints), 1) * 2
            for attempt in range(max_attempts):
                try:
                    return state["fn"](method, params)
                except requests.exceptions.HTTPError as e:
                    code = getattr(e.response, "status_code", 0)
                    if code not in (429, 500, 502, 503, 504):
                        raise
                    last_err = e
                except (requests.exceptions.ConnectionError,
                        requests.exceptions.Timeout,
                        requests.exceptions.ChunkedEncodingError) as e:
                    last_err = e
                # Rotate to the next endpoint and back off a bit.
                state["idx"] = (state["idx"] + 1) % len(endpoints)
                new_url = endpoints[state["idx"]]
                try:
                    new_provider = Web3.HTTPProvider(
                        new_url, request_kwargs={"timeout": 30},
                    )
                    w3.provider = new_provider
                    state["fn"] = new_provider.make_request
                    print(f"[AutoBot] RPC rotated -> {new_url}")
                except Exception as e:
                    last_err = e
                time.sleep(min(1.0 + attempt * 0.5, 4.0))
            raise last_err or RuntimeError("all RPCs failed")

        state["fn"] = orig
        w3.provider.make_request = rotating

    def _get_nonce(self):
        """Return next nonce, serialised via lock to avoid collisions."""
        with self._nonce_lock:
            if self._nonce is None:
                self._nonce = self.w3.eth.get_transaction_count(self.address)
            n = self._nonce
            self._nonce += 1
            return n

    def _reset_nonce(self):
        """Reset nonce from chain (call after errors / stuck tx)."""
        with self._nonce_lock:
            self._nonce = None

    def _gas_price(self):
        """Get gas price with 10% buffer for faster confirmation."""
        return int(self.w3.eth.gas_price * 1.1)

    def _pool_address(self, fee):
        """Look up the WETH/USDC pool for a given fee tier."""
        from web3 import Web3
        cached = self._pool_cache.get(fee)
        if cached:
            return cached
        addr = self.factory.functions.getPool(
            Web3.to_checksum_address(WETH),
            Web3.to_checksum_address(USDC),
            fee,
        ).call()
        if int(addr, 16) == 0:
            raise RuntimeError(f"No WETH/USDC pool for fee tier {fee}")
        checksummed = Web3.to_checksum_address(addr)
        self._pool_cache[fee] = checksummed
        return checksummed

    def _pool(self, fee):
        """Return a web3 contract instance for the pool of ``fee`` tier."""
        return self.w3.eth.contract(address=self._pool_address(fee), abi=POOL_ABI)

    def _pool_token_order(self, fee):
        """Return (token0, token1) as reported by the pool contract."""
        pool = self._pool(fee)
        return pool.functions.token0().call(), pool.functions.token1().call()

    def get_pool_price_usdc_per_eth(self, fee=500):
        """Current on-chain price from slot0, USDC per 1 ETH.

        Uses only the pool contract so it cannot desync from the venue
        we are about to trade against (no external oracle).
        """
        pool = self._pool(fee)
        sqrt_price_x96, *_ = pool.functions.slot0().call()
        # raw = (sqrtPrice / 2^96)^2 = price of token1/token0 in their smallest units
        raw = (sqrt_price_x96 / (2 ** 96)) ** 2
        # WETH=token0 (18 dec), USDC=token1 (6 dec) on Base → scale by 10^(18-6)
        return raw * 1e12

    def _send_and_wait(self, tx, label, timeout=180):
        """Sign, send and require a SUCCESSFUL receipt.

        Raises RuntimeError on failure (revert, timeout, status != 1) so
        the caller never silently proceeds with an invalid state.
        """
        from web3.exceptions import TimeExhausted
        try:
            signed = self.account.sign_transaction(tx)
            tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=timeout)
        except TimeExhausted as e:
            # Tx may still be in mempool — drop local nonce so the next
            # action refreshes it from chain state.
            self._reset_nonce()
            raise RuntimeError(f"{label} timed out waiting for receipt: {e}") from e
        if receipt.get("status", 0) != 1:
            self._reset_nonce()
            raise RuntimeError(
                f"{label} reverted on-chain (tx: {tx_hash.hex()})"
            )
        print(f"  ✓ {label} (tx: {tx_hash.hex()[:16]}...)")
        return tx_hash, receipt
    
    def check_gas(self, min_eth=0.0001, reserve_eth=None):
        """Check if wallet has enough ETH for gas. Returns (ok, balance).

        `min_eth` is the estimated cost of the current operation.
        `reserve_eth` is an extra buffer we keep for FUTURE txs so we never
        run the wallet down to empty. Defaults to GAS_RESERVE_ETH (~20 txs
        on Base at normal gas). Set to 0 to bypass the reserve check.
        """
        if reserve_eth is None:
            reserve_eth = GAS_RESERVE_ETH
        required = min_eth + reserve_eth
        bal = self.w3.eth.get_balance(self.address) / 1e18
        ok = bal >= required
        if not ok:
            print(f"  ⚠️  ETH balance too low: {bal:.6f} ETH "
                  f"(need {min_eth:.6f} op + {reserve_eth:.6f} reserve = {required:.6f})")
            print(f"  Send at least ${required * 2400:.2f} in ETH to {self.address}")
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
    
    def ensure_approval(self, token_contract, spender, amount, buffer_mult=1.01):
        """Approve exactly the required spend (plus a small safety buffer).

        Avoids unlimited (2^256-1) approvals so a compromise of the
        spender cannot drain future balances.
        """
        from web3 import Web3
        spender_cs = Web3.to_checksum_address(spender)
        current = token_contract.functions.allowance(self.address, spender_cs).call()
        if current >= amount:
            return None
        target = int(amount * buffer_mult)
        # Some ERC20s (notably USDT) require resetting allowance to 0 first.
        if current != 0:
            reset_tx = token_contract.functions.approve(
                spender_cs, 0
            ).build_transaction({
                'from': self.address,
                'nonce': self._get_nonce(),
                'gas': 100000,
                'gasPrice': self._gas_price(),
                'chainId': CHAIN_ID,
            })
            self._send_and_wait(reset_tx, "reset allowance")
        print(f"  Approving {target} for {spender_cs[:10]}...")
        tx = token_contract.functions.approve(
            spender_cs, target
        ).build_transaction({
            'from': self.address,
            'nonce': self._get_nonce(),
            'gas': 100000,
            'gasPrice': self._gas_price(),
            'chainId': CHAIN_ID,
        })
        _, receipt = self._send_and_wait(tx, "approve")
        return receipt

    def revoke_approval(self, token_contract, spender):
        """Set allowance back to 0. Best-effort — failures are logged not raised."""
        from web3 import Web3
        spender_cs = Web3.to_checksum_address(spender)
        try:
            current = token_contract.functions.allowance(self.address, spender_cs).call()
            if current == 0:
                return
            tx = token_contract.functions.approve(
                spender_cs, 0
            ).build_transaction({
                'from': self.address,
                'nonce': self._get_nonce(),
                'gas': 100000,
                'gasPrice': self._gas_price(),
                'chainId': CHAIN_ID,
            })
            self._send_and_wait(tx, "revoke approval")
        except Exception as e:
            print(f"  ⚠️  revoke_approval failed (non-fatal): {e}")
    
    def close_position(self, token_id):
        """Close an existing position: decrease liquidity + collect all.

        Applies the configured slippage tolerance to the minimum-output
        amounts on ``decreaseLiquidity`` so a mid-tx price move cannot
        rug the withdrawal.
        """
        pos = get_position(token_id)
        if not pos or pos["liquidity"] == 0:
            print(f"  Position {token_id} has no liquidity")
            return None

        print(f"  Closing position {token_id} (liq: {pos['liquidity']})")
        deadline = int(time.time()) + 600  # 10 min

        # Estimate expected amounts at the current price to derive min outs.
        try:
            exp0, exp1 = self._estimate_burn_amounts(pos)
            min0 = max(0, int(exp0 * (1 - self.slippage)))
            min1 = max(0, int(exp1 * (1 - self.slippage)))
        except Exception as e:
            # Fallback: accept any — less safe but preserves forward-compat.
            print(f"  ⚠️  could not estimate burn amounts ({e}); skipping min bounds")
            min0, min1 = 0, 0

        gas_price = self._gas_price()
        tx1 = self.pm.functions.decreaseLiquidity((
            token_id,
            pos["liquidity"],
            min0, min1, deadline,
        )).build_transaction({
            'from': self.address,
            'nonce': self._get_nonce(),
            'gas': 300000,
            'gasPrice': gas_price,
            'chainId': CHAIN_ID,
            'value': 0,
        })
        tx_hash1, _ = self._send_and_wait(tx1, "liquidity removed")

        # Step 2: Collect all tokens.
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
        tx_hash2, _ = self._send_and_wait(tx2, "tokens collected")

        return {"decrease_tx": tx_hash1.hex(), "collect_tx": tx_hash2.hex()}

    def _estimate_burn_amounts(self, pos, fee=500):
        """Estimate token amounts returned by decreaseLiquidity at current price.

        Uses the standard Uniswap V3 concentrated-liquidity formulae from
        the whitepaper, operating on sqrtPriceX96 to stay exact.
        """
        pool = self._pool(fee)
        sqrt_p, _tick, *_ = pool.functions.slot0().call()
        L = int(pos["liquidity"])
        sqrt_a = int(_sqrt_ratio_at_tick(pos["tickLower"]))
        sqrt_b = int(_sqrt_ratio_at_tick(pos["tickUpper"]))
        if sqrt_a > sqrt_b:
            sqrt_a, sqrt_b = sqrt_b, sqrt_a
        Q96 = 2 ** 96
        if sqrt_p <= sqrt_a:
            amount0 = L * (sqrt_b - sqrt_a) * Q96 // (sqrt_b * sqrt_a)
            amount1 = 0
        elif sqrt_p < sqrt_b:
            amount0 = L * (sqrt_b - sqrt_p) * Q96 // (sqrt_b * sqrt_p)
            amount1 = L * (sqrt_p - sqrt_a) // Q96
        else:
            amount0 = 0
            amount1 = L * (sqrt_b - sqrt_a) // Q96
        return amount0, amount1
    
    def swap_exact_input(self, token_in, token_out, amount_in, fee=500, slippage=None):
        """Swap tokens using SwapRouter02 with MEV-aware slippage.

        Computes the minimum accepted output from the on-chain pool price
        (``slot0``), the pool fee, and the configured slippage tolerance.
        Refuses to swap if ``amount_in`` is non-positive.
        """
        from web3 import Web3

        if amount_in <= 0:
            print("  ⚠️  swap_exact_input called with amount_in<=0, skipping")
            return None

        slip = self.slippage if slippage is None else slippage

        # Derive expected output from pool price (no external oracle).
        pool_price = self.get_pool_price_usdc_per_eth(fee)  # USDC per 1 ETH
        fee_mult = 1 - fee / 1_000_000  # e.g. 0.05% fee tier → 0.9995
        if token_in.lower() == WETH.lower():
            # ETH-in (18 dec) → USDC-out (6 dec)
            expected_out_units = (amount_in / 1e18) * pool_price
            amount_out_min = int(expected_out_units * 1e6 * fee_mult * (1 - slip))
        else:
            # USDC-in (6 dec) → ETH-out (18 dec)
            expected_out_units = (amount_in / 1e6) / pool_price
            amount_out_min = int(expected_out_units * 1e18 * fee_mult * (1 - slip))

        # Approve only the amount we are about to spend.
        token_contract = self.weth if token_in.lower() == WETH.lower() else self.usdc
        self.ensure_approval(token_contract, SWAP_ROUTER, amount_in)

        tx = self.router.functions.exactInputSingle((
            Web3.to_checksum_address(token_in),
            Web3.to_checksum_address(token_out),
            fee,
            self.address,
            amount_in,
            amount_out_min,
            0,  # sqrtPriceLimitX96 = 0 (no limit — min-out handles MEV)
        )).build_transaction({
            'from': self.address,
            'nonce': self._get_nonce(),
            'gas': 300000,
            'gasPrice': self._gas_price(),
            'chainId': CHAIN_ID,
            'value': 0,
        })
        tx_hash, receipt = self._send_and_wait(tx, "swap executed")
        # Revoke remaining allowance after the swap as a precaution.
        try:
            self.revoke_approval(token_contract, SWAP_ROUTER)
        except Exception:
            pass
        return {"tx": tx_hash.hex(), "receipt": receipt}
    
    def open_position(self, tick_lower, tick_upper, amount0, amount1, fee=500):
        """Open a new position with given tick range.

        Validates tick bounds / alignment, reads token order from the pool
        contract (instead of trusting a local hex compare), and sets
        ``amount0Min / amount1Min`` from slippage.
        """
        from web3 import Web3

        # Validate tick spacing + bounds against the live pool.
        pool = self._pool(fee)
        spacing = pool.functions.tickSpacing().call()
        if tick_lower >= tick_upper:
            raise ValueError(f"tick_lower {tick_lower} >= tick_upper {tick_upper}")
        if tick_lower < MIN_TICK or tick_upper > MAX_TICK:
            raise ValueError("tick range out of protocol bounds")
        if tick_lower % spacing or tick_upper % spacing:
            raise ValueError(
                f"ticks not aligned to pool spacing {spacing}: "
                f"lower={tick_lower}, upper={tick_upper}"
            )

        # Read the actual token order from the pool rather than assuming.
        pool_t0, pool_t1 = self._pool_token_order(fee)
        weth_cs = Web3.to_checksum_address(WETH)
        usdc_cs = Web3.to_checksum_address(USDC)
        if {pool_t0.lower(), pool_t1.lower()} != {weth_cs.lower(), usdc_cs.lower()}:
            raise RuntimeError(
                f"pool token mismatch: got {pool_t0}/{pool_t1}, expected WETH/USDC"
            )
        # Map caller-provided amounts (WETH=amount0, USDC=amount1) onto the
        # pool's token0/token1 order.
        if pool_t0.lower() == weth_cs.lower():
            a0, a1 = amount0, amount1
        else:
            a0, a1 = amount1, amount0

        # Approve the exact amounts we intend to spend, no more.
        if a0 > 0:
            self.ensure_approval(
                self.w3.eth.contract(address=Web3.to_checksum_address(pool_t0), abi=ERC20_ABI),
                POSITION_MANAGER, a0,
            )
        if a1 > 0:
            self.ensure_approval(
                self.w3.eth.contract(address=Web3.to_checksum_address(pool_t1), abi=ERC20_ABI),
                POSITION_MANAGER, a1,
            )

        # Slippage-protected min amounts.
        a0_min = int(a0 * (1 - self.slippage)) if a0 > 0 else 0
        a1_min = int(a1 * (1 - self.slippage)) if a1 > 0 else 0

        deadline = int(time.time()) + 600

        tx = self.pm.functions.mint((
            pool_t0, pool_t1, fee,
            tick_lower, tick_upper,
            a0, a1,
            a0_min, a1_min,
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
        tx_hash, receipt = self._send_and_wait(tx, "position opened", timeout=240)

        # Extract tokenId from logs
        new_token_id = None
        for log in receipt.get('logs', []):
            if len(log.get('topics', [])) >= 4:
                # Transfer event: topic[3] is tokenId
                new_token_id = int(log['topics'][3].hex(), 16)
                break

        # Revoke leftover allowances to the PositionManager.
        for tok in (pool_t0, pool_t1):
            try:
                tc = self.w3.eth.contract(address=Web3.to_checksum_address(tok), abi=ERC20_ABI)
                self.revoke_approval(tc, POSITION_MANAGER)
            except Exception:
                pass

        print(f"  tokenId: {new_token_id}")
        return {"tx": tx_hash.hex(), "token_id": new_token_id}

    def read_uncollected_fees(self, token_id, fee=500):
        """Return (weth_fee, usdc_fee, price_usdc_per_eth) for the position.

        Uses a static `collect` call with amount*Max = 2^128-1 — this is the
        canonical Uniswap pattern for reading true collectable fees (the
        on-chain `tokensOwed*` fields only update on pokes).
        """
        from web3 import Web3
        MAX_UINT128 = 2**128 - 1
        try:
            amount0, amount1 = self.pm.functions.collect((
                token_id, self.address, MAX_UINT128, MAX_UINT128,
            )).call({"from": self.address})
        except Exception as e:
            print(f"  ⚠️  could not read uncollected fees ({e})")
            return 0.0, 0.0, 0.0
        pool_t0, _ = self._pool_token_order(fee)
        weth_cs = Web3.to_checksum_address(WETH)
        if pool_t0.lower() == weth_cs.lower():
            weth_fee, usdc_fee = amount0 / 1e18, amount1 / 1e6
        else:
            weth_fee, usdc_fee = amount1 / 1e18, amount0 / 1e6
        price = self.get_pool_price_usdc_per_eth(fee)
        return weth_fee, usdc_fee, price

    def compound_fees(self, token_id, fee=500, gas_ratio_max=DEFAULT_COMPOUND_RATIO,
                     dry_run=False, auto_confirm=False):
        """Collect pending fees and redeposit them into the same position.

        Only fires when estimated gas cost is ≤ `gas_ratio_max` of the
        fees to reinvest (default 1%). This is how the GUI/daemon trigger
        opportunistic compounding without bleeding gas on tiny claims.

        Returns a dict with `status` ∈ {ok, skipped_gas_ratio, no_fees,
        no_position, insufficient_gas, cancelled, dry_run, fee_read_failed}.
        """
        from web3 import Web3

        print(f"\n{'='*50}")
        print(f"COMPOUND: Position {token_id}")
        print(f"{'='*50}")

        pos = get_position(token_id)
        if not pos or pos["liquidity"] == 0:
            print("  No active liquidity — skipping.")
            return {"status": "no_position"}

        weth_fee, usdc_fee, price = self.read_uncollected_fees(token_id, fee)
        if price <= 0:
            return {"status": "fee_read_failed"}
        fees_usd = weth_fee * price + usdc_fee

        gas_price = self._gas_price()
        # collect (~150k) + increaseLiquidity (~250k). Add one swap (~120k) for
        # the rebalance-to-ratio step when the fees are lopsided.
        est_gas_units = 520_000
        gas_cost_eth = est_gas_units * gas_price / 1e18
        gas_cost_usd = gas_cost_eth * price
        ratio = (gas_cost_usd / fees_usd) if fees_usd > 0 else float("inf")

        print(f"  Uncollected: {weth_fee:.6f} WETH + {usdc_fee:.4f} USDC ≈ ${fees_usd:.2f}")
        print(f"  Est. gas:    {gas_cost_eth:.6f} ETH ≈ ${gas_cost_usd:.3f}")
        print(f"  Ratio:       {ratio*100:.2f}% (threshold {gas_ratio_max*100:.1f}%)")

        if fees_usd <= 0:
            print("  No fees to compound.")
            return {"status": "no_fees", "fees_usd": 0.0}
        if ratio > gas_ratio_max:
            print("  Skipping — gas too high relative to fees.")
            return {"status": "skipped_gas_ratio",
                    "fees_usd": fees_usd, "gas_usd": gas_cost_usd, "ratio": ratio}

        if dry_run:
            print("  🔍 DRY RUN — would collect and redeposit.")
            return {"status": "dry_run", "fees_usd": fees_usd,
                    "gas_usd": gas_cost_usd, "ratio": ratio}

        ok, eth_bal = self.check_gas(min_eth=gas_cost_eth * 1.3)
        if not ok:
            return {"status": "insufficient_gas", "eth_balance": eth_bal}

        confirm = "y" if auto_confirm else input(
            f"  Compound ${fees_usd:.2f} for ${gas_cost_usd:.3f} gas? [y/N]: "
        ).strip().lower()
        if confirm != 'y':
            return {"status": "cancelled"}

        self._reset_nonce()

        # Step 1: collect fees to wallet.
        MAX_UINT128 = 2**128 - 1
        tx1 = self.pm.functions.collect((
            token_id, self.address, MAX_UINT128, MAX_UINT128,
        )).build_transaction({
            'from': self.address, 'nonce': self._get_nonce(),
            'gas': 200000, 'gasPrice': gas_price,
            'chainId': CHAIN_ID, 'value': 0,
        })
        tx_hash1, _ = self._send_and_wait(tx1, "fees collected")
        time.sleep(2)

        # Step 2: balance the collected tokens to the pool's current ratio
        # so increaseLiquidity can consume (almost) all of them. We don't
        # need a perfect split — the PM just returns the leftover dust.
        pool_t0, pool_t1 = self._pool_token_order(fee)
        weth_cs = Web3.to_checksum_address(WETH)
        bals = self.get_balances()
        weth_raw = int(bals["WETH"] * 1e18)
        usdc_raw = int(bals["USDC"] * 1e6)

        # Skip rebalancing if one side is negligible (< $0.10 worth).
        weth_usd = bals["WETH"] * price
        usdc_usd = bals["USDC"]
        total_usd = weth_usd + usdc_usd
        if total_usd > 0 and abs(weth_usd - usdc_usd) / total_usd > 0.15:
            # Swap the heavier side down to 50/50.
            target_weth_usd = total_usd / 2
            if weth_usd > target_weth_usd:
                excess_usd = weth_usd - target_weth_usd
                swap_in_raw = int(excess_usd / price * 1e18)
                if swap_in_raw > 0:
                    try:
                        self.swap_exact_input(WETH, USDC, swap_in_raw, fee)
                        time.sleep(2)
                    except Exception as e:
                        print(f"  ⚠️  ratio swap failed ({e}); continuing with current balances")
            else:
                excess_usd = usdc_usd - target_weth_usd
                swap_in_raw = int(excess_usd * 1e6)
                if swap_in_raw > 0:
                    try:
                        self.swap_exact_input(USDC, WETH, swap_in_raw, fee)
                        time.sleep(2)
                    except Exception as e:
                        print(f"  ⚠️  ratio swap failed ({e}); continuing with current balances")
            bals = self.get_balances()
            weth_raw = int(bals["WETH"] * 1e18)
            usdc_raw = int(bals["USDC"] * 1e6)

        if pool_t0.lower() == weth_cs.lower():
            a0, a1 = weth_raw, usdc_raw
        else:
            a0, a1 = usdc_raw, weth_raw

        if a0 == 0 and a1 == 0:
            print("  Nothing to redeposit.")
            return {"status": "no_fees_collected"}

        if a0 > 0:
            self.ensure_approval(
                self.w3.eth.contract(address=Web3.to_checksum_address(pool_t0), abi=ERC20_ABI),
                POSITION_MANAGER, a0,
            )
        if a1 > 0:
            self.ensure_approval(
                self.w3.eth.contract(address=Web3.to_checksum_address(pool_t1), abi=ERC20_ABI),
                POSITION_MANAGER, a1,
            )

        deadline = int(time.time()) + 600
        a0_min = int(a0 * (1 - self.slippage)) if a0 > 0 else 0
        a1_min = int(a1 * (1 - self.slippage)) if a1 > 0 else 0

        tx2 = self.pm.functions.increaseLiquidity((
            token_id, a0, a1, a0_min, a1_min, deadline,
        )).build_transaction({
            'from': self.address, 'nonce': self._get_nonce(),
            'gas': 350000, 'gasPrice': gas_price,
            'chainId': CHAIN_ID, 'value': 0,
        })
        tx_hash2, _ = self._send_and_wait(tx2, "liquidity increased", timeout=240)

        # Revoke any remaining allowances.
        for tok in (pool_t0, pool_t1):
            try:
                tc = self.w3.eth.contract(address=Web3.to_checksum_address(tok), abi=ERC20_ABI)
                self.revoke_approval(tc, POSITION_MANAGER)
            except Exception:
                pass

        print(f"  ✓ Compounded ${fees_usd:.2f} into position {token_id}")
        return {
            "status": "ok",
            "collect_tx": tx_hash1.hex(),
            "increase_tx": tx_hash2.hex(),
            "fees_usd": fees_usd,
            "gas_usd": gas_cost_usd,
            "ratio": ratio,
        }

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
        
        # Step 2: Check balances and swap if needed. Uses the live pool price
        # (not tick_to_price(0) which is WETH-USDC at tick 0 ≈ $1) to compute
        # the USD-value ratio we need.
        print(f"\n  [2/3] Rebalancing tokens...")
        balances = self.get_balances()
        weth_val = balances["WETH"]
        usdc_val = balances["USDC"]
        pool_price = self.get_pool_price_usdc_per_eth(fee)  # USDC per 1 ETH
        total_usd = weth_val * pool_price + usdc_val
        print(f"  Pool price: ${pool_price:,.2f} USDC/ETH — total ${total_usd:,.2f}")

        # Rough 50/50 USD split. For out-of-range positions you would
        # want a skewed ratio; this is left as a follow-up (the pool will
        # consume whatever it needs — our slippage bounds cap the risk).
        target_weth_val = total_usd * 0.5 / pool_price

        if weth_val > target_weth_val * 1.05:
            # Too much WETH, swap some to USDC
            swap_amount = int((weth_val - target_weth_val) * 1e18)
            if swap_amount > 0:
                print(f"  Swapping {swap_amount/1e18:.6f} WETH → USDC...")
                results["swap"] = self.swap_exact_input(WETH, USDC, swap_amount, fee)
        elif usdc_val > (total_usd * 0.5) * 1.05:
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
    parser.add_argument("--compound", action="store_true",
                        help="Collect pending fees and redeposit (only if gas <= --compound-ratio of fees)")
    parser.add_argument("--compound-ratio", type=float, default=DEFAULT_COMPOUND_RATIO,
                        help=f"Max gas/fees ratio for --compound (default: {DEFAULT_COMPOUND_RATIO})")
    parser.add_argument("--hold", choices=["ETH","USDC"], default="USDC", help="Asset to hold on exit")
    parser.add_argument("--position-id", "-p", type=int, help="Position NFT ID")
    parser.add_argument("--tick-lower", type=int, help="Lower tick for new range")
    parser.add_argument("--tick-upper", type=int, help="Upper tick for new range")
    parser.add_argument("--price-lower", type=float, help="Lower price (converts to tick)")
    parser.add_argument("--price-upper", type=float, help="Upper price (converts to tick)")
    parser.add_argument("--fee", type=int, default=500, help="Pool fee tier (default: 500 = 0.05%%)")
    parser.add_argument("--dry-run", action="store_true", help="Simulate without executing")
    parser.add_argument("--yes", "-y", action="store_true", help="Skip confirmation prompts")
    parser.add_argument(
        "--pk-fd",
        type=int,
        default=None,
        help=(
            "Read the already-decrypted private key (hex) from this file descriptor "
            "instead of prompting for the keystore password. Used by the GUI/daemon "
            "to avoid shipping the password into subprocess memory."
        ),
    )
    args = parser.parse_args()

    if args.setup:
        setup_key()
        return

    # Unlock key. Preferred path: parent process has already decrypted the key
    # and passes it via a dedicated pipe FD — this keeps the password out of
    # the subprocess's address space entirely. Fallback: interactive password.
    if args.pk_fd is not None:
        try:
            with os.fdopen(args.pk_fd, "r", closefd=True) as fd:
                pk = fd.read().strip()
        except OSError as e:
            print(f"ERROR: cannot read private key from fd {args.pk_fd}: {e}")
            return
        if not pk.startswith("0x") or len(pk) != 66:
            print("ERROR: Invalid private key received on --pk-fd")
            return
    else:
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

    elif args.compound:
        if not args.position_id:
            print("ERROR: --position-id required"); return
        bot.compound_fees(args.position_id, args.fee, args.compound_ratio,
                          args.dry_run, args.yes)

    else:
        parser.print_help()

if __name__ == "__main__":
    main()
