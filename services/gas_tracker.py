"""
Gas Tracker — Fetches real-time Polygon gas prices via RPC and estimates transaction costs in USDC.
"""
import asyncio
import logging
import time
import httpx
from web3 import Web3
from config import settings

_log = logging.getLogger(__name__)

class GasTracker:
    def __init__(self, rpc_url: str = None):
        self.rpc_url = rpc_url or settings.polygon_rpc_url
        self.w3 = Web3(Web3.HTTPProvider(self.rpc_url))
        self._http: httpx.AsyncClient | None = None
        self._cache = {
            "gas_price_gwei": 30.0,
            "matic_price": 0.70,
            "last_updated": 0,
            "last_matic_update": 0
        }
        self.CACHE_TTL = 30
        self.MATIC_CACHE_TTL = 900

    async def _get_http(self) -> httpx.AsyncClient:
        """Lazy-init a persistent httpx client."""
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(timeout=10.0)
        return self._http

    def get_gas_price_gwei(self) -> float:
        """Get current gas price in Gwei with caching."""
        now = time.time()
        if now - self._cache["last_updated"] < self.CACHE_TTL:
            return self._cache["gas_price_gwei"]

        try:
            price_wei = self.w3.eth.gas_price
            price_gwei = Web3.from_wei(price_wei, 'gwei')
            self._cache["gas_price_gwei"] = float(price_gwei)
            self._cache["last_updated"] = now
            return self._cache["gas_price_gwei"]
        except Exception as e:
            _log.debug("Gas price fetch failed, using cached: %s", e)
            return self._cache["gas_price_gwei"]

    async def get_matic_price(self) -> float:
        """Fetch MATIC/USD price from Coinbase Public API (Cached)."""
        now = time.time()
        if now - self._cache["last_matic_update"] < self.MATIC_CACHE_TTL:
            return self._cache["matic_price"]
            
        try:
            client = await self._get_http()
            resp = await client.get(settings.coinbase_matic_spot_url, timeout=5.0)
            if resp.status_code == 200:
                data = resp.json()
                self._cache["matic_price"] = float(data["data"]["amount"])
                self._cache["last_matic_update"] = now
        except Exception as e:
            _log.debug("MATIC price fetch failed, using cached: %s", e)
            
        return self._cache["matic_price"]

    async def get_gas_cost_usdc(self, gas_limit: int = 150000) -> float:
        """
        Estimate the USDC cost of a standard Polygon transaction.
        Formula: (Gas Price in Gwei * Gas Limit) / 10^9 * MATIC_PRICE_USDC
        """
        gwei = await asyncio.to_thread(self.get_gas_price_gwei)
        matic_price = await self.get_matic_price()
        
        cost_matic = (gwei * gas_limit) / 1e9
        cost_usdc = cost_matic * matic_price
        
        return max(0.001, round(cost_usdc, 4))

# Singleton instance
gas_tracker = GasTracker()
