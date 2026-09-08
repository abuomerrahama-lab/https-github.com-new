"""
Ads Dashboard Backend
======================
Fetches today's ad performance from:
  - Windsor.ai (TikTok Ads, Google Ads, and 350+ other connectors)
  - Meta Marketing API (Facebook/Instagram Ads) directly
Then sends the combined numbers to Claude for analysis and returns one
JSON payload the dashboard's "Refresh" button can render immediately.

Run locally:
    pip install -r requirements.txt
    cp .env.example .env   # fill in your real keys
    uvicorn main:app --reload --port 8000

Then open your dashboard HTML and point the refresh button at
http://localhost:8000/api/refresh (see frontend-refresh.js).
"""

import os
import json
import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Config — all secrets come from environment variables, never hardcode them.
# ---------------------------------------------------------------------------
WINDSOR_API_KEY = os.environ.get("WINDSOR_API_KEY", "")
META_ACCESS_TOKEN = os.environ.get("META_ACCESS_TOKEN", "")
META_AD_ACCOUNT_IDS = [
    a.strip() for a in os.environ.get("META_AD_ACCOUNT_IDS", "").split(",") if a.strip()
]
TIKTOK_ACCOUNT_IDS = [
    a.strip() for a in os.environ.get("TIKTOK_ACCOUNT_IDS", "").split(",") if a.strip()
]
GOOGLE_ADS_ACCOUNT_IDS = [
    a.strip() for a in os.environ.get("GOOGLE_ADS_ACCOUNT_IDS", "").split(",") if a.strip()
]
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")
RIYADH_TZ = ZoneInfo("Asia/Riyadh")

META_GRAPH_VERSION = "v21.0"
WINDSOR_BASE_URL = "https://connectors.windsor.ai"  # verify against current Windsor.ai docs
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"

app = FastAPI(title="Ads Dashboard Backend")

# Allow your dashboard's origin. In production, replace "*" with your
# actual frontend URL (e.g. "https://yourname.github.io") for safety.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


def today_riyadh() -> str:
    return datetime.now(RIYADH_TZ).strftime("%Y-%m-%d")


def yesterday_riyadh() -> str:
    return (datetime.now(RIYADH_TZ) - timedelta(days=1)).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# 1. Windsor.ai fetchers (TikTok Ads, Google Ads)
# ---------------------------------------------------------------------------
async def fetch_windsor(client: httpx.AsyncClient, connector: str, account_ids: list[str],
                         fields: list[str], date_str: str) -> dict:
    """Generic Windsor.ai puller for one connector for a single day."""
    if not WINDSOR_API_KEY or not account_ids:
        return {"available": False, "reason": "missing_api_key_or_accounts", "rows": []}

    params = {
        "api_key": WINDSOR_API_KEY,
        "connector": connector,
        "accounts": ",".join(account_ids),
        "fields": ",".join(fields),
        "date_from": date_str,
        "date_to": date_str,
        "_renderer": "json",
    }
    try:
        resp = await client.get(f"{WINDSOR_BASE_URL}/{connector}", params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        rows = data.get("data", data) if isinstance(data, dict) else data
        return {"available": True, "rows": rows}
    except Exception as exc:  # network error, bad key, connector down, etc.
        return {"available": False, "reason": str(exc), "rows": []}


async def fetch_tiktok(client: httpx.AsyncClient, date_str: str) -> dict:
    fields = ["account_name", "campaign", "spend", "impressions", "clicks",
              "cpm", "cpc", "engagements", "engagement_rate", "conversions",
              "cost_per_conversion"]
    return await fetch_windsor(client, "tiktok", TIKTOK_ACCOUNT_IDS, fields, date_str)


async def fetch_google_ads(client: httpx.AsyncClient, date_str: str) -> dict:
    fields = ["campaign", "campaign_status", "clicks", "conversions",
              "average_cpc", "cost"]
    return await fetch_windsor(client, "google_ads", GOOGLE_ADS_ACCOUNT_IDS, fields, date_str)


# ---------------------------------------------------------------------------
# 2. Meta Marketing API fetcher (called directly, not via Windsor)
# ---------------------------------------------------------------------------
async def fetch_meta(client: httpx.AsyncClient, date_str: str) -> dict:
    if not META_ACCESS_TOKEN or not META_AD_ACCOUNT_IDS:
        return {"available": False, "reason": "missing_token_or_accounts", "accounts": []}

    results = []
    for account_id in META_AD_ACCOUNT_IDS:
        url = f"https://graph.facebook.com/{META_GRAPH_VERSION}/act_{account_id}/insights"
        params = {
            "access_token": META_ACCESS_TOKEN,
            "level": "campaign",
            "time_range": json.dumps({"since": date_str, "until": date_str}),
            "fields": "campaign_name,spend,actions,cost_per_action_type",
        }
        try:
            resp = await client.get(url, params=params, timeout=30)
            resp.raise_for_status()
            payload = resp.json()
            results.append({"account_id": account_id, "available": True,
                             "campaigns": payload.get("data", [])})
        except Exception as exc:
            results.append({"account_id": account_id, "available": False, "reason": str(exc)})
    return {"available": True, "accounts": results}


# ---------------------------------------------------------------------------
# 3. Claude analysis — send the raw numbers, get back structured JSON
# ---------------------------------------------------------------------------
ANALYSIS_SYSTEM_PROMPT = """You are an advertising analyst. You will receive raw, real
performance data for today from Meta Ads, TikTok Ads, and Google Ads for a Saudi-based
advertiser (currency: SAR).

Return ONLY valid JSON (no markdown fences, no commentary before or after) matching
exactly this shape:

{
  "date": "YYYY-MM-DD",
  "totals": {
    "meta_spend": number, "tiktok_spend": number, "google_spend": number,
    "total_spend": number
  },
  "ai_alerts": [
    {"severity": "critical|warning|info", "platform": "meta|tiktok|google",
     "message": "short Arabic sentence describing the anomaly", "metric": "string", "value": "string"}
  ],
  "budget_recommendation": {
    "summary": "one Arabic sentence",
    "moves": [
      {"from": "platform/campaign", "to": "platform/campaign",
       "suggested_change_sar": number, "reason": "short Arabic reason"}
    ]
  }
}

Rules:
- Use ONLY the numbers given to you. Never invent data. If a platform's data is missing
  or marked unavailable, say so in an "info" alert instead of guessing numbers.
- Flag as "critical" only real anomalies: spend >50% above/below what the data shows as
  typical for that campaign if a baseline is provided, or conversions dropping to zero
  while spend continues.
- Flag as "warning" moderate concerns (cost-per-result rising, one platform far less
  efficient than another for the same kind of result).
- budget_recommendation must be conservative and specific — cite the actual SAR numbers
  you were given, never percentages pulled from nowhere.
- Write all alert/recommendation text in Arabic. Keep every message under 25 words.
- Output valid JSON only. No prose outside the JSON object.
"""


async def call_claude(client: httpx.AsyncClient, payload: dict) -> dict:
    if not ANTHROPIC_API_KEY:
        return {
            "date": payload.get("date"),
            "totals": payload.get("totals", {}),
            "ai_alerts": [{"severity": "info", "platform": "system",
                            "message": "مفتاح Anthropic API غير مُعد على السيرفر — لم يتم تشغيل التحليل الذكي.",
                            "metric": "config", "value": "missing_key"}],
            "budget_recommendation": {"summary": "غير متاح بدون مفتاح Anthropic API.", "moves": []},
        }

    body = {
        "model": ANTHROPIC_MODEL,
        "max_tokens": 1500,
        "system": ANALYSIS_SYSTEM_PROMPT,
        "messages": [
            {"role": "user", "content": "Raw data:\n" + json.dumps(payload, ensure_ascii=False)}
        ],
    }
    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    resp = await client.post(ANTHROPIC_URL, json=body, headers=headers, timeout=60)
    resp.raise_for_status()
    data = resp.json()
    text = "".join(block.get("text", "") for block in data.get("content", []) if block.get("type") == "text")
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`").split("\n", 1)[-1]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {
            "date": payload.get("date"),
            "totals": payload.get("totals", {}),
            "ai_alerts": [{"severity": "warning", "platform": "system",
                            "message": "تعذّر تفسير استجابة Claude كـJSON صالح — راجع الرد الخام.",
                            "metric": "parse_error", "value": text[:200]}],
            "budget_recommendation": {"summary": "غير متاح — خطأ في تنسيق الاستجابة.", "moves": []},
            "_raw_claude_text": text,
        }


# ---------------------------------------------------------------------------
# 4. Aggregation helpers
# ---------------------------------------------------------------------------
def sum_spend(rows: list, key: str = "spend") -> float:
    total = 0.0
    for r in rows or []:
        try:
            total += float(r.get(key) or 0)
        except (TypeError, ValueError):
            continue
    return round(total, 2)


def sum_meta_spend(meta_result: dict) -> float:
    total = 0.0
    for acc in meta_result.get("accounts", []):
        for c in acc.get("campaigns", []):
            try:
                total += float(c.get("spend") or 0)
            except (TypeError, ValueError):
                continue
    return round(total, 2)


# ---------------------------------------------------------------------------
# 5. The endpoint the "Refresh" button calls
# ---------------------------------------------------------------------------
class RefreshResponse(BaseModel):
    date: str
    generated_at: str
    raw: dict
    analysis: dict


@app.get("/api/refresh", response_model=RefreshResponse)
async def refresh(day: str = "today"):
    """
    day="today"     -> uses today's date in Riyadh time
    day="yesterday" -> uses yesterday's date in Riyadh time (matches the
                        existing "تقرير أمس" daily report convention)
    """
    date_str = today_riyadh() if day == "today" else yesterday_riyadh()

    async with httpx.AsyncClient() as client:
        tiktok_result, google_result, meta_result = await asyncio.gather(
            fetch_tiktok(client, date_str),
            fetch_google_ads(client, date_str),
            fetch_meta(client, date_str),
        )

        raw = {
            "date": date_str,
            "tiktok": tiktok_result,
            "google_ads": google_result,
            "meta": meta_result,
            "totals": {
                "meta_spend": sum_meta_spend(meta_result),
                "tiktok_spend": sum_spend(tiktok_result.get("rows", [])),
                "google_spend": sum_spend(google_result.get("rows", []), key="cost"),
            },
        }
        raw["totals"]["total_spend"] = round(
            raw["totals"]["meta_spend"] + raw["totals"]["tiktok_spend"] + raw["totals"]["google_spend"], 2
        )

        analysis = await call_claude(client, raw)

    return RefreshResponse(
        date=date_str,
        generated_at=datetime.now(RIYADH_TZ).isoformat(),
        raw=raw,
        analysis=analysis,
    )


@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "windsor_configured": bool(WINDSOR_API_KEY),
        "meta_configured": bool(META_ACCESS_TOKEN and META_AD_ACCOUNT_IDS),
        "anthropic_configured": bool(ANTHROPIC_API_KEY),
    }
