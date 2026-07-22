"""
Multi-LLM Provider Engine
Provides unified asynchronous evaluation across OpenAI, Anthropic, Moonshot/Kimi, and DeepSeek.
Retrieves API keys dynamically from services.keystore and evaluates multi-outcome tail-risk and probabilities.
"""
import json
import logging
from typing import Dict, List, Optional
import httpx
from services.keystore import keystore

_log = logging.getLogger(__name__)

# Supported Providers Configuration
PROVIDER_CONFIGS = {
    "openai": {
        "name": "OpenAI (GPT-4o)",
        "default_model": "gpt-4o",
        "api_base": "https://api.openai.com/v1",
        "header_key": "Authorization",
        "header_prefix": "Bearer "
    },
    "anthropic": {
        "name": "Anthropic (Claude 3.5 Sonnet)",
        "default_model": "claude-3-5-sonnet-20241022",
        "api_base": "https://api.anthropic.com/v1",
        "header_key": "x-api-key",
        "header_prefix": ""
    },
    "kimi": {
        "name": "Moonshot / Kimi (Kimi-k1.5)",
        "default_model": "moonshot-v1-8k",
        "api_base": "https://api.moonshot.cn/v1",
        "header_key": "Authorization",
        "header_prefix": "Bearer "
    },
    "deepseek": {
        "name": "DeepSeek (V3 / R1)",
        "default_model": "deepseek-chat",
        "api_base": "https://api.deepseek.com/v1",
        "header_key": "Authorization",
        "header_prefix": "Bearer "
    }
}

_SYSTEM_PROMPT = """You are an elite quantitative analyst specializing in prediction market probability estimation and tail-risk analysis (Polymarket / Kalshi).
Your task is to analyze a multi-outcome market, evaluate news/polling/field dynamics, and estimate the probability distribution.

Given a market question, description, and list of candidates with current implied prices:
1. Identify the top selected candidates provided in the query.
2. Estimate the true combined probability P_model(Top_Set) that one of these selected candidates wins.
3. Estimate the Tail Risk probability P_tail (1 - P_model(Top_Set)) that a candidate OUTSIDE the top set wins.
4. Assess confidence level (0.0 to 1.0) and provide a concise risk rationale.

You MUST respond strictly in valid JSON format with NO markdown wrapping or additional text:
{
    "p_model_top_set": float,       // e.g. 0.94 (probability top set candidate wins)
    "p_tail_risk": float,           // e.g. 0.06 (probability an outside longshot wins)
    "confidence": float,            // e.g. 0.85 (confidence score 0.0 - 1.0)
    "tail_risk_assessment": "low" | "medium" | "high",
    "rationale": "Concise 1-2 sentence explanation of tail risk & news/field factors"
}"""


class LLMProviderService:
    """Unified client for multi-LLM probability & tail-risk inference."""

    async def evaluate_market(
        self,
        provider_key: str,
        market_question: str,
        market_description: str,
        candidates: List[Dict],
        top_set_names: List[str],
        custom_model: Optional[str] = None,
        http_client: Optional[httpx.AsyncClient] = None
    ) -> Dict:
        """
        Query a specified LLM provider to evaluate market tail-risk and true set probability.
        """
        cfg = PROVIDER_CONFIGS.get(provider_key.lower())
        if not cfg:
            return self._fallback_response(f"Unsupported provider: {provider_key}")

        api_key = keystore.get_decrypted(provider_key.lower())
        if not api_key:
            return self._fallback_response(f"Missing API key for {cfg['name']}. Please configure in Key Vault.")

        model = custom_model or cfg["default_model"]
        close_client = False
        if http_client is None:
            http_client = httpx.AsyncClient(timeout=20.0)
            close_client = True

        prompt_user = (
            f"Market Question: {market_question}\n"
            f"Market Context: {market_description or 'None provided.'}\n\n"
            f"All Candidates & Market Prices:\n" +
            "\n".join([f"- {c['name']}: ${c['price']:.3f}" for c in candidates]) +
            f"\n\nSelected Candidate Set to Dutch: {', '.join(top_set_names)}\n"
            f"Combined Market Price of Selected Set: ${sum(c['price'] for c in candidates if c['name'] in top_set_names):.3f}\n"
        )

        try:
            if provider_key.lower() == "anthropic":
                resp_data = await self._call_anthropic(http_client, cfg, api_key, model, prompt_user)
            else:
                # OpenAI compatible endpoint (OpenAI, Kimi, DeepSeek)
                resp_data = await self._call_openai_compatible(http_client, cfg, api_key, model, prompt_user)

            parsed = self._parse_json_response(resp_data)
            parsed["provider"] = provider_key
            parsed["model"] = model
            return parsed

        except Exception as e:
            _log.error("LLM evaluation error for provider %s (%s): %s", provider_key, model, e)
            return self._fallback_response(f"Inference error: {str(e)}")
        finally:
            if close_client:
                await http_client.aclose()

    async def _call_openai_compatible(
        self, client: httpx.AsyncClient, cfg: Dict, api_key: str, model: str, prompt_user: str
    ) -> str:
        headers = {
            cfg["header_key"]: f"{cfg['header_prefix']}{api_key}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": prompt_user}
            ],
            "temperature": 0.2
        }

        url = f"{cfg['api_base'].rstrip('/')}/chat/completions"
        res = await client.post(url, json=payload, headers=headers)
        res.raise_for_status()
        data = res.json()
        return data["choices"][0]["message"]["content"]

    async def _call_anthropic(
        self, client: httpx.AsyncClient, cfg: Dict, api_key: str, model: str, prompt_user: str
    ) -> str:
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json"
        }
        payload = {
            "model": model,
            "system": _SYSTEM_PROMPT,
            "messages": [
                {"role": "user", "content": prompt_user}
            ],
            "max_tokens": 500,
            "temperature": 0.2
        }
        url = f"{cfg['api_base'].rstrip('/')}/messages"
        res = await client.post(url, json=payload, headers=headers)
        res.raise_for_status()
        data = res.json()
        return data["content"][0]["text"]

    def _parse_json_response(self, raw_text: str) -> Dict:
        text = raw_text.strip()
        if text.startswith("```json"):
            text = text[7:]
        if text.startswith("```"):
            text = text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

        data = json.loads(text)
        p_top = float(data.get("p_model_top_set", 0.90))
        p_tail = float(data.get("p_tail_risk", 1.0 - p_top))
        conf = float(data.get("confidence", 0.75))

        return {
            "p_model_top_set": round(p_top, 4),
            "p_tail_risk": round(p_tail, 4),
            "confidence": round(conf, 4),
            "tail_risk_assessment": data.get("tail_risk_assessment", "medium"),
            "rationale": data.get("rationale", "Standard model estimation"),
            "status": "success"
        }

    def _fallback_response(self, reason: str) -> Dict:
        return {
            "p_model_top_set": 0.85,
            "p_tail_risk": 0.15,
            "confidence": 0.50,
            "tail_risk_assessment": "high",
            "rationale": f"Fallback estimate applied ({reason})",
            "status": "fallback",
            "error": reason
        }


# Global singleton
llm_provider = LLMProviderService()
