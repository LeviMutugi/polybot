"""
Strategy: Smart Money Leaderboard Copy Trading
Description: Monitored target top wallets from the Polymarket leaderboard on-chain.
Detects high-conviction trades and replicates them within risk parameters.
"""
import uuid
import json
import asyncio
import logging
from typing import List
import httpx
from config import settings
from strategies.base import BaseStrategy, parse_list, days_to_expiry, get_market_url, calculate_execution_price
from db.config import cfg
from services.gas_tracker import gas_tracker

_log = logging.getLogger(__name__)

# Polymarket CTF (Conditional Token Framework) contract on Polygon (canonical Gnosis CTF)
CTF_CONTRACT = "0x4D97DCd97eC945f40CF65F87097CAe16E4bb2830"
# TransferSingle(address operator, address from, address to, uint256 id, uint256 value)
# topics layout: [signature, operator, from, to]
TRANSFER_SINGLE_TOPIC = "0xc3d58168c5ae7397731d063d5bbf3d65785442f3484c048b909b5ffc0217f254"

class LeaderboardCopyTradingStrategy(BaseStrategy):
    def __init__(self):
        super().__init__(
            key="copy_trading",
            name="Leaderboard Copy",
            risk_level="Medium",
            market_type="Binary",
            default_exec_mode="auto"
        )
        self._seen_tx_hashes: set = set()  # Dedup tracker

    async def scan(self, markets: List[dict], balance: float, http_client: httpx.AsyncClient) -> List[dict]:
        enabled = await cfg.get_typed("copy_trading.enabled", bool, True)
        if not enabled:
            return []

        max_pos_pct = await cfg.get_typed("copy_trading.max_position_pct", float, 0.05)
        raw_wallets = await cfg.get_async("copy_trading.target_wallets", "[]")
        exec_mode = await cfg.get_typed("copy_trading.exec_mode", str, "auto")

        try:
            target_wallets = json.loads(raw_wallets)
            if not isinstance(target_wallets, list) or len(target_wallets) == 0:
                return []
        except Exception:
            return []

        opps = []
        scan_gas_usdc = await gas_tracker.get_gas_cost_usdc()

        # Connect to Web3 to query event logs
        from web3 import Web3
        w3 = Web3(Web3.HTTPProvider(settings.polygon_rpc_url))
        
        try:
            # Query recent logs on Polygon for the CTF contract
            # Scan depth matches scan_interval to avoid missing trades
            scan_interval = int(await cfg.get_async("poly_yield.scan_interval_s", "120"))
            blocks_to_scan = max(10, scan_interval // 2)  # ~2s per block on Polygon
            latest_block = w3.eth.block_number
            start_block = max(0, latest_block - blocks_to_scan)
        except Exception as e:
            _log.debug("CopyTrading: RPC connection failed: %s", e)
            return []

        for wallet in target_wallets:
            wallet_addr = w3.to_checksum_address(wallet)
            try:
                # Query TransferSingle logs where the wallet is the RECIPIENT (a buy/mint).
                # Recipient ('to') is the 3rd indexed param => topics[3].
                # (Filtering topics[2] would match transfers FROM the wallet, i.e. sells.)
                recipient_topic = "0x" + wallet_addr[2:].lower().zfill(64)

                # Fetch logs synchronously inside executor to prevent blocking
                logs = await asyncio.get_running_loop().run_in_executor(
                    None,
                    lambda: w3.eth.get_logs({
                        "fromBlock": start_block,
                        "toBlock": latest_block,
                        "address": w3.to_checksum_address(CTF_CONTRACT),
                        "topics": [TRANSFER_SINGLE_TOPIC, None, None, recipient_topic]
                    })
                )
                
                for log in logs:
                    # Deduplicate by transaction hash
                    tx_hash = log.get("transactionHash", b"").hex() if isinstance(log.get("transactionHash"), bytes) else str(log.get("transactionHash", ""))
                    if tx_hash in self._seen_tx_hashes:
                        continue
                    self._seen_tx_hashes.add(tx_hash)
                    if len(self._seen_tx_hashes) > 10000:
                        # Bound memory: drop roughly half the dedup cache
                        self._seen_tx_hashes = set(list(self._seen_tx_hashes)[5000:])
                    # Parse token ID from log data/topics
                    # For TransferSingle, topics are: [Event Signature, Operator, From, To]
                    # Data contains: [ID, Value]
                    data = log["data"].hex() if isinstance(log["data"], bytes) else log["data"]
                    if len(data) >= 128:
                        token_id_hex = data[2:66]
                        token_id = int(token_id_hex, 16)
                        
                        # Find the corresponding Polymarket from the shared markets list
                        matched_market = None
                        matched_outcome_idx = -1
                        
                        for m in markets:
                            clob_tokens = parse_list(m.get("clobTokenIds"))
                            for idx, t_id in enumerate(clob_tokens):
                                try:
                                    if int(t_id) == token_id:
                                        matched_market = m
                                        matched_outcome_idx = idx
                                        break
                                except ValueError:
                                    continue
                            if matched_market:
                                break

                        if matched_market and matched_outcome_idx != -1:
                            outcomes = parse_list(matched_market.get("outcomes"))
                            prices = parse_list(matched_market.get("outcomePrices"))
                            token_ids = parse_list(matched_market.get("clobTokenIds"))
                            
                            outcome = outcomes[matched_outcome_idx]
                            price = float(prices[matched_outcome_idx])
                            token_id_str = token_ids[matched_outcome_idx]

                            suggested_usdc = max(0.50, balance * max_pos_pct)
                            
                            # Walk book depth — skip if not fillable within tolerance
                            from strategies.base import is_fillable
                            max_slippage = await cfg.get_typed("poly_yield.max_slippage_pct", float, 1.5)
                            exec_data = await calculate_execution_price(token_id_str, suggested_usdc, side="buy", http_client=http_client)
                            if not is_fillable(exec_data, max_slippage):
                                continue

                            real_price = exec_data["price"]
                            slippage = exec_data.get("slippage", 0)

                            days = days_to_expiry(matched_market.get("endDate"))
                            market_url = get_market_url(matched_market)

                            opps.append({
                                "id": f"copy_{wallet_addr[:6]}_{token_id_str[:8]}",
                                "strategy": self.key,
                                "market_id": str(matched_market.get("id", "")),
                                "market_title": matched_market.get("question", ""),
                                "market_url": market_url,
                                "outcome": outcome,
                                "entry_price": round(real_price, 4),
                                "yes_price": round(price if matched_outcome_idx == 0 else 1.0 - price, 4),
                                "no_price": round(1.0 - price if matched_outcome_idx == 0 else price, 4),
                                "implied_prob": round(real_price * 100, 2),
                                "slippage_bps": round(slippage * 100, 2),
                                "annualized_apy": None,
                                "profit_pct": None,
                                "days_to_expiry": round(days, 1) if days else None,
                                "action": "buy_yes" if matched_outcome_idx == 0 else "buy_no",
                                "exec_mode": exec_mode,
                                "suggested_usdc": round(suggested_usdc, 2),
                                "token_id": token_id_str,
                                "status": "open",
                                "notes": f"Replicating top wallet {wallet_addr[:8]} buy on outcome '{outcome}'. Expected fill price ${real_price:.4f}.",
                                "instructions": [
                                    f"Open: {market_url}",
                                    f"Copy top trader {wallet_addr[:8]} - Buy '{outcome}' for ${round(suggested_usdc, 2)} USDC.",
                                    f"Position follows smart money. Watch closely."
                                ]
                            })
            except Exception as ex:
                _log.debug("Failed checking logs for wallet %s: %s", wallet_addr, ex)

        return opps

    async def execute(self, opportunity: dict, clob_client) -> dict:
        """Execute Copy Buy order."""
        from py_clob_client.clob_types import OrderArgs
        try:
            token_id = opportunity.get("token_id")
            price = float(opportunity["entry_price"])
            usdc = float(opportunity["suggested_usdc"])
            shares = round(usdc / price, 2)

            order = clob_client.create_order(OrderArgs(
                price=price, size=shares, side="BUY", token_id=token_id
            ))
            resp = clob_client.post_order(order)
            return {"success": True, "order_id": resp.get("orderID")}
        except Exception as e:
            return {"success": False, "error": str(e)}
