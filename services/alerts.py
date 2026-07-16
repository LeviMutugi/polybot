"""
Alerts Dispatcher
Sends notifications to Telegram channel and Discord webhooks dynamically.
"""
import time
import httpx
import logging
from config import settings

_log = logging.getLogger(__name__)

class AlertsDispatcher:
    """Dispatches logs and trade signals to Telegram and Discord channels."""

    # Rate limit: max 20 messages per 60 seconds
    MAX_MESSAGES = 20
    RATE_WINDOW = 60.0

    def __init__(self):
        self.telegram_enabled = bool(settings.telegram_bot_token and settings.telegram_chat_id)
        self.discord_enabled = bool(settings.discord_webhook_url)
        self._http: httpx.AsyncClient | None = None
        self._send_times: list[float] = []

        if self.telegram_enabled:
            self.telegram_url = f"https://api.telegram.org/bot{settings.telegram_bot_token}"
        else:
            self.telegram_url = ""

    ICONS = {
        "info": "ℹ️",
        "success": "✅",
        "warning": "⚠️",
        "critical": "🚨",
        "error": "❌",
    }

    async def _get_http(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(timeout=10.0)
        return self._http

    def _rate_limited(self) -> bool:
        now = time.time()
        self._send_times = [t for t in self._send_times if now - t < self.RATE_WINDOW]
        if len(self._send_times) >= self.MAX_MESSAGES:
            return True
        self._send_times.append(now)
        return False

    async def send(self, message: str, level: str = "info"):
        """Send message to all enabled channels asynchronously."""
        icon = self.ICONS.get(level, "📌")
        formatted_message = f"{icon} [PolyYield] {message}"

        # Standard console log fallback
        print(f"[Alert] [{level.upper()}] {message}")

        if self._rate_limited():
            _log.warning("Alert rate limit exceeded, skipping external dispatch")
            return

        # Dispatch Telegram
        if self.telegram_enabled:
            await self._send_telegram(formatted_message)

        # Dispatch Discord
        if self.discord_enabled:
            await self._send_discord(formatted_message)

    async def _send_telegram(self, message: str):
        try:
            client = await self._get_http()
            resp = await client.post(
                f"{self.telegram_url}/sendMessage",
                json={
                    "chat_id": settings.telegram_chat_id,
                    "text": message,
                    "parse_mode": "Markdown"
                }
            )
            if resp.status_code != 200:
                _log.debug("Telegram alert failed status code=%s: %s", resp.status_code, resp.text)
        except Exception as e:
            _log.debug("Telegram alert send failed: %s", e)

    async def _send_discord(self, message: str):
        try:
            client = await self._get_http()
            resp = await client.post(
                settings.discord_webhook_url,
                json={
                    "content": message
                }
            )
            if resp.status_code not in (200, 204):
                _log.debug("Discord alert failed status code=%s: %s", resp.status_code, resp.text)
        except Exception as e:
            _log.debug("Discord alert send failed: %s", e)

# Singleton alert dispatcher
alert = AlertsDispatcher()
