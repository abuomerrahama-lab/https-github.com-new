import os
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, Any
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
import httpx

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ads-dashboard")

app = FastAPI(title="منصة إعلانات elevenz")

CACHE: Dict[str, Any] = {
    "meta_ads": [],
    "tiktok_ads": [],
    "google_ads": [],
    "last_updated": None
}

WINDSOR_API_KEY = os.getenv("WINDSOR_API_KEY", "")
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "")
REFRESH_INTERVAL_SECONDS = 90
DEFAULT_LOOKBACK_DAYS = 30

CONNECTOR_FIELDS = {
    "facebook": "account_name,campaign,adset_name,ad_name,clicks,spend,conversions,impressions,cpc,ctr,date,actions",
    "tiktok": "account_name,campaign_name,adgroup_name,ad_name,clicks,spend,conversion,conversions,impressions,cpc,ctr,date",
    "google_ads": "account_name,campaign,ad_group_name,ad_name,clicks,spend,conversions,impressions,cpc,ctr,date",
}

def get_default_date_range(days: int = DEFAULT_LOOKBACK_DAYS):
    today = datetime.now(timezone.utc).date()
    date_from = today - timedelta(days=days)
    return date_from.isoformat(), today.isoformat()

async def fetch_windsor_connector(connector: str, params: dict) -> list:
    if not WINDSOR_API_KEY:
        return []
    url = f"https://connectors.windsor.ai/{connector}"
    date_from, date_to = get_default_date_range()
    base_params = {
        "api_key": WINDSOR_API_KEY,
        "_renderer": "json",
        "date_from": date_from,
        "date_to": date_to,
    }
    base_params.update(params)
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            res = await client.get(url, params=base_params)
            if res.status_code != 200:
                return []
            res_data = res.json()
            if isinstance(res_data, dict):
                return res_data.get("data", [])
            elif isinstance(res_data, list):
                return res_data
            return []
    except Exception:
        return []

async def refresh_cache_and_keep_alive():
    global CACHE
    while True:
        try:
            meta_res, tiktok_res, google_res = await asyncio.gather(
                fetch_windsor_connector("facebook", {"fields": CONNECTOR_FIELDS["facebook"]}),
                fetch_windsor_connector("tiktok", {"fields": CONNECTOR_FIELDS["tiktok"]}),
                fetch_windsor_connector("google_ads", {"fields": CONNECTOR_FIELDS["google_ads"]}),
                return_exceptions=True
            )
            CACHE["meta_ads"] = meta_res if isinstance(meta_res, list) else []
            CACHE["tiktok_ads"] = tiktok_res if isinstance(tiktok_res, list) else []
            CACHE["google_ads"] = google_res if isinstance(google_res, list) else []
            CACHE["last_updated"] = datetime.now(timezone.utc).isoformat()
        except Exception as e:
            logger.error(f"Cache error: {e}")
        await asyncio.sleep(REFRESH_INTERVAL_SECONDS)

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(refresh_cache_and_keep_alive())

@app.get("/api/data")
async def get_dashboard_data():
    return JSONResponse(content={"status": "success", "data": CACHE, "last_updated": CACHE["last_updated"]})

@app.get("/", response_class=HTMLResponse)
async def serve_index():
    html_content = """
    <!DOCTYPE html>
    <html lang="ar" dir="rtl">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>منصة إعلانات elevenz</title>
        <link href="https://fonts.googleapis.com/css2?family=Cairo:wght@400;600;700;800&display=swap" rel="stylesheet">
        <style>
            :root {
                --bg-main: #f4f6fa;
                --card-bg: #ffffff;
                --text-main: #0f2540;
                --text-muted: #6c757d;
                --border: #e2e8f0;
            }
            body {
                font-family: 'Cairo', sans-serif;
                background-color: var(--bg-main);
                margin: 0;
                padding: 24px;
                color: var(--text-main);
            }
            .dashboard-container {
                max-width: 1200px;
                margin: 0 auto;
            }
            .header {
                display: flex;
                justify-content: space-between;
                align-items: center;
                margin-bottom: 24px;
            }
            .header h1 {
                font-size: 22px;
                font-weight: 800;
                margin: 0;
            }
            .cards-grid {
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
                gap: 20px;
            }
            .card {
                background: var(--card-bg);
                border: 1px solid var(--border);
                border-radius: 12px;
                padding: 20px;
                box-shadow: 0 2px 8px rgba(0,0,0,0.04);
            }
            .card-header {
                font-size: 14px;
                font-weight: 700;
                color: var(--text-muted);
                margin-bottom: 12px;
            }
            .card-value {
                font-size: 28px;
                font-weight: 800;
                color: var(--text-main);
                margin-bottom: 6px;
            }
            .card-sub {
                font-size: 14px;
                font-weight: 700;
                color: #2563eb;
                margin-bottom: 16px;
            }
            .card-footer {
                display: flex;
                justify-content: space-between;
                font-size: 12px;
                color: var(--text-muted);
                border-top: 1px dashed var(--border);
                padding-top: 12px;
            }
        </style>
    </head>
    <body>
        <div class="dashboard-container">
            <div class="header">
                <h1>لوحة قياس الإعلانات - Elevens</h1>
            </div>

            <div class="cards-grid">
                <!-- Meta Ads -->
                <div class="card">
                    <div class="card-header">Meta Ads (فيس بوك / إنستغرام)</div>
                    <div class="card-value" id="meta-spend">0.00 ر.س</div>
                    <div class="card-sub" id="meta-results">0 محادثة</div>
                    <div class="card-footer">
                        <span>CTR: <b id="meta-ctr">0%</b></span>
                        <span>CPA: <b id="meta-cpa">0.00 ر.س</b></span>
                    </div>
                </div>

                <!-- TikTok Ads -->
                <div class="card">
                    <div class="card-header">TikTok Ads</div>
                    <div class="card-value" id="tiktok-spend">0.00 ر.س</div>
                    <div class="card-sub" id="tiktok-results">0 تحويل</div>
                    <div class="card-footer">
                        <span>CTR: <b id="tiktok-ctr">0%</b></span>
                        <span>CPA: <b id="tiktok-cpa">0.00 ر.س</b></span>
                    </div>
                </div>

                <!-- Google Ads -->
                <div class="card">
                    <div class="card-header">Google Ads</div>
                    <div class="card-value" id="google-spend">0.00 ر.س</div>
                    <div class="card-sub" id="google-results">0 تحويل</div>
                    <div class="card-footer">
                        <span>CTR: <b id="google-ctr">0%</b></span>
                        <span>CPA: <b id="google-cpa">0.00 ر.س</b></span>
                    </div>
                </div>
            </div>
        </div>

        <script>
            function extractMetaConversations(actions) {
                if (!actions) return 0;
                let parsed = actions;
                if (typeof actions === 'string') {
                    try { parsed = JSON.parse(actions); } catch (e) { return 0; }
                }
                if (Array.isArray(parsed)) {
                    const match = parsed.find(a => 
                        a.action_type === 'onsite_conversion.messaging_conversation_started_7d' ||
                        a.action_type === 'messaging_conversation_started'
                    );
                    if (match) return parseFloat(match.value || 0);
                }
                return typeof parsed === 'number' ? parsed : 0;
            }

            async function loadDashboardData() {
                try {
                    const res = await fetch('/api/data');
                    const json = await res.json();
                    if (json.status === 'success' && json.data) {
                        
                        // Meta Ads
                        const meta = json.data.meta_ads || [];
                        let mSpend = 0, mClicks = 0, mImp = 0, mConvs = 0;
                        meta.forEach(r => {
                            mSpend += parseFloat(r.spend || 0);
                            mClicks += parseFloat(r.clicks || 0);
                            mImp += parseFloat(r.impressions || 0);
                            mConvs += extractMetaConversations(r.actions) || parseFloat(r.conversions || 0);
                        });
                        document.getElementById('meta-spend').innerText = mSpend.toFixed(2) + ' ر.س';
                        document.getElementById('meta-results').innerText = mConvs + ' محادثة';
                        document.getElementById('meta-ctr').innerText = (mImp > 0 ? (mClicks / mImp * 100).toFixed(2) : '0.00') + '%';
                        document.getElementById('meta-cpa').innerText = (mConvs > 0 ? (mSpend / mConvs).toFixed(2) : '0.00') + ' ر.س';

                        // TikTok Ads
                        const tiktok = json.data.tiktok_ads || [];
                        let tSpend = 0, tClicks = 0, tImp = 0, tConvs = 0;
                        tiktok.forEach(r => {
                            tSpend += parseFloat(r.spend || 0);
                            tClicks += parseFloat(r.clicks || 0);
                            tImp += parseFloat(r.impressions || 0);
                            tConvs += parseFloat(r.conversion || r.conversions || 0);
                        });
                        document.getElementById('tiktok-spend').innerText = tSpend.toFixed(2) + ' ر.س';
                        document.getElementById('tiktok-results').innerText = tConvs + ' تحويل';
                        document.getElementById('tiktok-ctr').innerText = (tImp > 0 ? (tClicks / tImp * 100).toFixed(2) : '0.00') + '%';
                        document.getElementById('tiktok-cpa').innerText = (tConvs > 0 ? (tSpend / tConvs).toFixed(2) : '0.00') + ' ر.س';

                        // Google Ads
                        const google = json.data.google_ads || [];
                        let gSpend = 0, gClicks = 0, gImp = 0, gConvs = 0;
                        google.forEach(r => {
                            gSpend += parseFloat(r.spend || 0);
                            gClicks += parseFloat(r.clicks || 0);
                            gImp += parseFloat(r.impressions || 0);
                            gConvs += parseFloat(r.conversions || 0);
                        });
                        document.getElementById('google-spend').innerText = gSpend.toFixed(2) + ' ر.س';
                        document.getElementById('google-results').innerText = gConvs + ' تحويل';
                        document.getElementById('google-ctr').innerText = (gImp > 0 ? (gClicks / gImp * 100).toFixed(2) : '0.00') + '%';
                        document.getElementById('google-cpa').innerText = (gConvs > 0 ? (gSpend / gConvs).toFixed(2) : '0.00') + ' ر.س';
                    }
                } catch (e) {
                    console.error("خطأ في تحميل البيانات:", e);
                }
            }

            loadDashboardData();
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)
