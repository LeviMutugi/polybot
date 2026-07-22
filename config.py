"""
PolyYield Bot Configuration System
Loads settings from .env file and validates with Pydantic.
"""
from pathlib import Path
from pydantic_settings import BaseSettings
from pydantic import Field

BASE_DIR = Path(__file__).resolve().parent

class Settings(BaseSettings):
    # Host & Port
    host: str = "127.0.0.1"
    port: int = 8000
    api_secret: str = "poly_yield_default_secret_key_change_me"

    # Polymarket Settings
    polymarket_web_url: str = "https://polymarket.com"
    polymarket_gamma_url: str = "https://gamma-api.polymarket.com"
    polymarket_clob_url: str = "https://clob.polymarket.com"
    
    # Polygon Node / RPC
    polygon_rpc_url: str = "https://polygon-bor-rpc.publicnode.com"
    polygon_usdc_contract: str = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
    polygon_chain_id: int = 137  # Default to Mainnet (137)
    
    # MATIC Price Oracle
    coinbase_matic_spot_url: str = "https://api.coinbase.com/v2/prices/MATIC-USD/spot"

    # Database Path
    sqlite_path: str = str(BASE_DIR / "poly_yield.db")

    # Webhooks & Telemetry
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    discord_webhook_url: str = ""

    # Alchemy Key
    alchemy_api_key: str = ""

    model_config = {
        "env_file": str(BASE_DIR / ".env"),
        "extra": "ignore"
    }

settings = Settings()
