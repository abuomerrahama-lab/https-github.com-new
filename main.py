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
        logger.warning(f"WINDSOR_API_KEY غير مضبوط - تم تخطي الاتصال بموصل '{connector}'")
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
                logger.error(
                    f"فشل طلب Windsor لموصل '{connector}' - رمز الحالة: {res.status_code} - "
                    f"نص الاستجابة: {res.text[:500]}"
                )
                return []

            try:
                res_data = res.json()
            except Exception as parse_err:
                logger.error(
                    f"تعذر تحليل JSON من موصل '{connector}': {parse_err} - "
                    f"نص الاستجابة: {res.text[:500]}"
                )
                return []

            if isinstance(res_data, dict):
                data_field = res_data.get("data")
                if isinstance(data_field, list):
                    return data_field
                if "error" in res_data:
                    logger.error(f"خطأ من Windsor لموصل '{connector}': {res_data.get('error')}")
                    return []
                return []
            elif isinstance(res_data, list):
                return res_data
            else:
                return []

    except httpx.TimeoutException:
        logger.error(f"انتهت مهلة الاتصال (Timeout) بموصل '{connector}'")
        return []
    except Exception as e:
        logger.error(f"خطأ غير متوقع أثناء جلب بيانات '{connector}': {e}")
        return []

async def refresh_cache_and_keep_alive():
    global CACHE
    while True:
        try:
            date_from, date_to = get_default_date_range()
            logger.info(f"جاري تحديث بيانات إعلانات elevenz... (النطاق الزمني: {date_from} إلى {date_to})")

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

            target_url = RENDER_EXTERNAL_URL.rstrip('/') if RENDER_EXTERNAL_URL else "http://127.0.0.1:8000"
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    await client.get(f"{target_url}/api/status")
            except Exception:
                pass

        except Exception as e:
            logger.error(f"Cache error: {e}")
            
        await asyncio.sleep(REFRESH_INTERVAL_SECONDS)

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(refresh_cache_and_keep_alive())

@app.get("/api/status")
async def get_status():
    return {"status": "ok"}

@app.get("/api/debug/windsor")
async def debug_windsor(connector: str = "facebook"):
    if connector not in CONNECTOR_FIELDS:
        return JSONResponse(content={
            "error": f"موصل غير معروف: '{connector}'",
            "supported_connectors": list(CONNECTOR_FIELDS.keys())
        }, status_code=400)

    if not WINDSOR_API_KEY:
        return JSONResponse(content={"error": "WINDSOR_API_KEY غير مضبوط"})

    fields = CONNECTOR_FIELDS[connector]
    date_from, date_to = get_default_date_range()
    url = f"https://connectors.windsor.ai/{connector}"
    params = {
        "api_key": WINDSOR_API_KEY,
        "_renderer": "json",
        "date_from": date_from,
        "date_to": date_to,
        "fields": fields,
    }
    masked_url = f"{url}?api_key=***&_renderer=json&date_from={date_from}&date_to={date_to}&fields={fields}"

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            res = await client.get(url, params=params)

        try:
            parsed = res.json()
        except Exception:
            parsed = None

        row_count = None
        if isinstance(parsed, dict) and isinstance(parsed.get("data"), list):
            row_count = len(parsed["data"])
        elif isinstance(parsed, list):
            row_count = len(parsed)

        return JSONResponse(content={
            "connector": connector,
            "request_url_masked": masked_url,
            "status_code": res.status_code,
            "date_from": date_from,
            "date_to": date_to,
            "fields_requested": fields,
            "row_count": row_count,
            "parsed_json": parsed,
            "raw_body_preview": res.text[:3000],
        })
    except Exception as e:
        return JSONResponse(content={"connector": connector, "error": str(e)})

def _valid_iso_date(value: str) -> bool:
    try:
        datetime.strptime(value, "%Y-%m-%d")
        return True
    except (ValueError, TypeError):
        return False

@app.get("/api/data")
async def get_dashboard_data(date_from: str = None, date_to: str = None):
    if not date_from and not date_to:
        return JSONResponse(content={
            "status": "success",
            "data": CACHE,
            "last_updated": CACHE["last_updated"]
        })

    if not date_from or not date_to or not _valid_iso_date(date_from) or not _valid_iso_date(date_to):
        return JSONResponse(content={"status": "error", "message": "صيغة التاريخ غير صحيحة."}, status_code=400)

    if date_from > date_to:
        return JSONResponse(content={"status": "error", "message": "تاريخ البداية يجب أن يسبق تاريخ النهاية."}, status_code=400)

    meta_res, tiktok_res, google_res = await asyncio.gather(
        fetch_windsor_connector("facebook", {"fields": CONNECTOR_FIELDS["facebook"], "date_from": date_from, "date_to": date_to}),
        fetch_windsor_connector("tiktok", {"fields": CONNECTOR_FIELDS["tiktok"], "date_from": date_from, "date_to": date_to}),
        fetch_windsor_connector("google_ads", {"fields": CONNECTOR_FIELDS["google_ads"], "date_from": date_from, "date_to": date_to}),
        return_exceptions=True
    )

    fresh_data = {
        "meta_ads": meta_res if isinstance(meta_res, list) else [],
        "tiktok_ads": tiktok_res if isinstance(tiktok_res, list) else [],
        "google_ads": google_res if isinstance(google_res, list) else [],
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }

    return JSONResponse(content={"status": "success", "data": fresh_data, "last_updated": fresh_data["last_updated"]})

@app.get("/", response_class=HTMLResponse)
async def serve_index():
    html_content = """
    <!DOCTYPE html>
    <html lang="ar" dir="rtl">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>منصة إعلانات elevenz</title>
        <link href="https://fonts.googleapis.com/css2?family=Cairo:wght@400;500;600;700;800;900&display=swap" rel="stylesheet">
        <style>
            :root {
                --sidebar-bg: #071c35;
                --main-bg: #f4f6fa;
                --card-bg: #ffffff;
                --text-dark: #0f2540;
                --text-muted: #8592a6;
                --accent-orange: #f05a28;
                --border-color: #eef1f6;
            }
            body { font-family: 'Cairo', sans-serif; background: var(--main-bg); margin: 0; padding: 24px; direction: rtl; }
            .cards-container { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 18px; }
            .card { background: var(--card-bg); padding: 22px; border-radius: 16px; border: 1px solid var(--border-color); box-shadow: 0 4px 12px rgba(0,0,0,0.03); }
            .card-title { font-size: 13px; color: var(--text-muted); font-weight: 700; }
            .card-value { font-size: 26px; font-weight: 800; color: var(--text-dark); margin: 10px 0; }
            .card-sub { font-size: 13.5px; font-weight: 700; color: #2f6fed; }
            .card-meta { font-size: 12px; color: var(--text-muted); margin-top: 12px; padding-top: 10px; border-top: 1px dashed var(--border-color); }
        </style>
    </head>
    <body>
        <div class="cards-container">
            <div class="card">
                <div class="card-title">Meta Ads (فيس بوك / إنستغرام)</div>
                <div class="card-value" id="meta-spend">0.00 ر.س</div>
                <div class="card-sub" id="meta-results">0 محادثة/نتيجة</div>
                <div class="card-meta">
                    <span>CTR: <b id="meta-ctr">0%</b></span> | 
                    <span>CPA: <b id="meta-cpa">0.00 ر.س</b></span>
                </div>
            </div>
        </div>

        <script>
            function extractMetaConversations(actions) {
                if (!actions) return 0;
                
                let parsedActions = actions;
                if (typeof actions === 'string') {
                    try {
                        parsedActions = JSON.parse(actions);
                    } catch (e) {
                        return 0;
                    }
                }
                
                if (Array.isArray(parsedActions)) {
                    const convAction = parsedActions.find(a => 
                        a.action_type === 'onsite_conversion.messaging_conversation_started_7d' ||
                        a.action_type === 'messaging_conversation_started' ||
                        a.action_type === 'messaging_user_depth_2_conversations'
                    );
                    if (convAction) return parseFloat(convAction.value || 0);

                    const fallbackAction = parsedActions.find(a => 
                        a.action_type && a.action_type.includes('messaging') && !a.action_type.includes('initiated')
                    );
                    if (fallbackAction) return parseFloat(fallbackAction.value || 0);
                }
                
                return typeof parsedActions === 'number' ? parsedActions : 0;
            }

            async function loadDashboardData() {
                try {
                    const res = await fetch('/api/data');
                    const json = await res.json();
                    if (json.status === 'success' && json.data) {
                        const metaRows = json.data.meta_ads || [];
                        
                        let totalSpend = 0;
                        let totalClicks = 0;
                        let totalImpressions = 0;
                        let totalConversations = 0;

                        metaRows.forEach(row => {
                            totalSpend += parseFloat(row.spend || 0);
                            totalClicks += parseFloat(row.clicks || 0);
                            totalImpressions += parseFloat(row.impressions || 0);
                            
                            let convs = extractMetaConversations(row.actions);
                            if (convs === 0 && row.conversions) {
                                convs = parseFloat(row.conversions || 0);
                            }
                            totalConversations += convs;
                        });

                        const ctr = totalImpressions > 0 ? ((totalClicks / totalImpressions) * 100).toFixed(2) : '0.00';
                        const cpa = totalConversations > 0 ? (totalSpend / totalConversations).toFixed(2) : '0.00';

                        document.getElementById('meta-spend').innerText = totalSpend.toFixed(2) + ' ر.س';
                        document.getElementById('meta-results').innerText = totalConversations + ' محادثة/نتيجة';
                        document.getElementById('meta-ctr').innerText = ctr + '%';
                        document.getElementById('meta-cpa').innerText = cpa + ' ر.س';
                    }
                } catch (e) {
                    console.error("خطأ أثناء جلب البيانات:", e);
                }
            }

            loadDashboardData();
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)
