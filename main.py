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

# حقول أساسية ومضمونة الدعم في موصلات Windsor.ai (مرجع واحد يُستخدم في
# حلقة التحديث ونقطة التشخيص معاً لتفادي أي تعارض بين المكانين)
CONNECTOR_FIELDS = {
    "facebook": "account_name,campaign,adset_name,ad_name,clicks,spend,conversions,impressions,cpc,ctr,date,actions",
    "tiktok": "account_name,campaign_name,adgroup_name,ad_name,clicks,spend,conversion,conversions,impressions,cpc,ctr,date",
    "google_ads": "account_name,campaign,ad_group_name,ad_name,clicks,spend,conversions,impressions,cpc,ctr,date",
}


def get_default_date_range(days: int = DEFAULT_LOOKBACK_DAYS):
    """يحسب نطاق تاريخ افتراضي (date_from / date_to) بصيغة YYYY-MM-DD.
    يغطي آخر `days` يوماً بما فيها اليوم الحالي، لضمان أن Windsor.ai
    لا يُرجع بيانات فارغة بسبب غياب نطاق زمني صريح في الطلب."""
    today = datetime.now(timezone.utc).date()
    date_from = today - timedelta(days=days)
    return date_from.isoformat(), today.isoformat()


async def fetch_windsor_connector(connector: str, params: dict) -> list:
    """يجلب بيانات من موصل Windsor.ai معيّن، مع:
    - إرسال نطاق تاريخ افتراضي (آخر 30 يوماً) تلقائياً إن لم يُحدَّد صراحة.
    - تسجيل تفصيلي للأخطاء (رمز الحالة + نص الاستجابة) لتسهيل تتبع الأعطال.
    - إرجاع قائمة فارغة دائماً عند أي فشل، حتى لا يتسبب في كسر asyncio.gather
      أو باقي مسار التحديث."""
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
    # السماح باستبدال fields أو date_from/date_to إذا مُرِّرت صراحة في params
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
                logger.warning(
                    f"استجابة غير متوقعة (dict بدون قائمة data) من موصل '{connector}': "
                    f"{str(res_data)[:300]}"
                )
                return []
            elif isinstance(res_data, list):
                return res_data
            else:
                logger.warning(f"نوع استجابة غير معروف من موصل '{connector}': {type(res_data)}")
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
            CACHE["last_updated"] = asyncio.get_event_loop().time()

            logger.info(
                f"تم التحديث: Meta={len(CACHE['meta_ads'])} صف، "
                f"TikTok={len(CACHE['tiktok_ads'])} صف، "
                f"Google={len(CACHE['google_ads'])} صف"
            )
            
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
    """نقطة تشخيص: تنفّذ نفس طلب Windsor.ai المستخدم في التحديث التلقائي
    وتُرجع الاستجابة الخام (رمز الحالة + نص الاستجابة) دون الحاجة لقراءة
    سجلات (Logs) Render. افتح مباشرة في المتصفح:
    /api/debug/windsor?connector=facebook
    /api/debug/windsor?connector=tiktok
    /api/debug/windsor?connector=google_ads
    مفتاح الـ API لا يظهر أبداً في الاستجابة."""
    if connector not in CONNECTOR_FIELDS:
        return JSONResponse(content={
            "error": f"موصل غير معروف: '{connector}'",
            "supported_connectors": list(CONNECTOR_FIELDS.keys())
        }, status_code=400)

    if not WINDSOR_API_KEY:
        return JSONResponse(content={
            "error": "WINDSOR_API_KEY غير مضبوط في متغيرات البيئة (Environment Variables) على Render. "
                     "أضِفه من إعدادات الخدمة (Environment) ثم أعد النشر."
        })

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
    masked_url = (
        f"{url}?api_key=***&_renderer=json&date_from={date_from}"
        f"&date_to={date_to}&fields={fields}"
    )

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
        return JSONResponse(content={
            "connector": connector,
            "request_url_masked": masked_url,
            "error": str(e)
        })

def _valid_iso_date(value: str) -> bool:
    try:
        datetime.strptime(value, "%Y-%m-%d")
        return True
    except (ValueError, TypeError):
        return False


@app.get("/api/data")
async def get_dashboard_data(date_from: str = None, date_to: str = None):
    # لا يوجد نطاق تاريخ مخصص → إرجاع الكاش التلقائي (يُحدَّث كل REFRESH_INTERVAL_SECONDS لآخر 30 يوماً)
    if not date_from and not date_to:
        return JSONResponse(content={
            "status": "success",
            "data": CACHE,
            "last_updated": CACHE["last_updated"]
        })

    # طُلب نطاق تاريخ مخصص من الواجهة → إعادة الاستعلام من Windsor.ai مباشرة بهذا النطاق
    if not date_from or not date_to or not _valid_iso_date(date_from) or not _valid_iso_date(date_to):
        return JSONResponse(content={
            "status": "error",
            "message": "صيغة التاريخ غير صحيحة. الصيغة المطلوبة: YYYY-MM-DD ويجب توفير date_from و date_to معاً."
        }, status_code=400)

    if date_from > date_to:
        return JSONResponse(content={
            "status": "error",
            "message": "تاريخ البداية (date_from) يجب أن يسبق أو يساوي تاريخ النهاية (date_to)."
        }, status_code=400)

    logger.info(f"طلب نطاق مخصص من الواجهة: {date_from} إلى {date_to}")

    meta_res, tiktok_res, google_res = await asyncio.gather(
        fetch_windsor_connector("facebook", {
            "fields": CONNECTOR_FIELDS["facebook"], "date_from": date_from, "date_to": date_to
        }),
        fetch_windsor_connector("tiktok", {
            "fields": CONNECTOR_FIELDS["tiktok"], "date_from": date_from, "date_to": date_to
        }),
        fetch_windsor_connector("google_ads", {
            "fields": CONNECTOR_FIELDS["google_ads"], "date_from": date_from, "date_to": date_to
        }),
        return_exceptions=True
    )

    fresh_data = {
        "meta_ads": meta_res if isinstance(meta_res, list) else [],
        "tiktok_ads": tiktok_res if isinstance(tiktok_res, list) else [],
        "google_ads": google_res if isinstance(google_res, list) else [],
        "last_updated": asyncio.get_event_loop().time(),
    }

    logger.info(
        f"نتيجة النطاق المخصص ({date_from} → {date_to}): "
        f"Meta={len(fresh_data['meta_ads'])} صف، "
        f"TikTok={len(fresh_data['tiktok_ads'])} صف، "
        f"Google={len(fresh_data['google_ads'])} صف"
    )

    return JSONResponse(content={
        "status": "success",
        "data": fresh_data,
        "last_updated": fresh_data["last_updated"],
        "range": {"date_from": date_from, "date_to": date_to}
    })

@app.get("/", response_class=HTMLResponse)
async def serve_index():
    html_content = """
    <!DOCTYPE html>
    <html lang="ar" dir="rtl">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>منصة إعلانات elevenz</title>
        <link rel="preconnect" href="https://fonts.googleapis.com">
        <link href="https://fonts.googleapis.com/css2?family=Cairo:wght@400;500;600;700;800;900&display=swap" rel="stylesheet">
        <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
        <script src="https://cdn.jsdelivr.net/npm/chartjs-plugin-datalabels@2.2.0"></script>
        <style>
            :root {
                --sidebar-bg: #071c35;
                --sidebar-bg-2: #0a2544;
                --main-bg: #f4f6fa;
                --card-bg: #ffffff;
                --text-dark: #0f2540;
                --text-muted: #8592a6;
                --text-faint: #b0bac9;
                --accent-orange: #f05a28;
                --accent-orange-light: #fff1ec;
                --accent-blue: #2f6fed;
                --accent-green: #31a24c;
                --border-color: #eef1f6;
                --nested-bg: #fafbfd;
                --nested-deep: #f4f6fa;

                --radius-lg: 18px;
                --radius-md: 14px;
                --radius-sm: 10px;
                --shadow-card: 0 1px 2px rgba(15, 37, 64, 0.04), 0 10px 24px -14px rgba(15, 37, 64, 0.12);
                --shadow-pop: 0 12px 32px -8px rgba(15, 37, 64, 0.22);

                --stage-green-bg: #eefcf3;
                --stage-green-border: #cdf2dc;
                --stage-green-text: #128a4d;
                --stage-blue-bg: #eef4ff;
                --stage-blue-border: #d6e4ff;
                --stage-blue-text: #2f6fed;
                --stage-red-bg: #fef1f0;
                --stage-red-border: #fbd6d2;
                --stage-red-text: #d13b2c;
                --stage-gray-bg: #f2f4f7;
                --stage-gray-border: #e3e7ee;
                --stage-gray-text: #667085;
            }
            * { box-sizing: border-box; }
            body {
                font-family: 'Cairo', sans-serif;
                background-color: var(--main-bg);
                color: var(--text-dark);
                margin: 0;
                display: flex;
                height: 100vh;
                overflow: hidden;
                direction: rtl;
                -webkit-font-smoothing: antialiased;
            }
            .sidebar {
                width: 264px;
                background: linear-gradient(180deg, var(--sidebar-bg-2) 0%, var(--sidebar-bg) 100%);
                color: #ffffff;
                display: flex;
                flex-direction: column;
                padding: 22px 0;
                flex-shrink: 0;
            }
            .sidebar-logo {
                padding: 0 26px 22px 26px;
                font-size: 28px;
                font-weight: 800;
                color: #fff;
                border-bottom: 1px solid rgba(255,255,255,0.08);
                letter-spacing: -0.5px;
            }
            .sidebar-logo span { color: var(--accent-orange); }
            .sidebar-menu { list-style: none; padding: 18px 14px; margin: 0; flex: 1; }
            .sidebar-menu li { margin-bottom: 4px; }
            .sidebar-menu li a {
                display: flex;
                align-items: center;
                gap: 10px;
                padding: 12px 16px;
                color: #9fb0c7;
                text-decoration: none;
                font-weight: 600;
                font-size: 14px;
                border-radius: var(--radius-sm);
                transition: background-color 0.15s ease, color 0.15s ease;
            }
            .sidebar-menu li.active a {
                background-color: rgba(240, 90, 40, 0.16);
                color: #ffffff;
            }
            .sidebar-menu li a:hover { background-color: rgba(255,255,255,0.07); color: #ffffff; }

            .user-profile {
                margin: 8px 14px 0 14px;
                padding: 14px 16px;
                border-top: 1px solid rgba(255,255,255,0.08);
                display: flex;
                align-items: center;
                gap: 12px;
            }
            .user-avatar {
                width: 38px;
                height: 38px;
                border-radius: 50%;
                background: linear-gradient(135deg, var(--accent-orange), #ff8657);
                display: flex;
                align-items: center;
                justify-content: center;
                font-weight: bold;
                color: #fff;
                flex-shrink: 0;
            }

            .main-content {
                flex: 1;
                overflow-y: auto;
                padding: 32px 34px;
            }
            .top-bar { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 24px; gap: 16px; flex-wrap: wrap; }
            .header-title h1 { margin: 0; font-size: 23px; font-weight: 800; color: var(--text-dark); }
            .header-title p { margin: 6px 0 0 0; color: var(--text-muted); font-size: 13px; font-weight: 500; }
            .top-actions { display: flex; gap: 10px; }

            .btn {
                display: inline-flex;
                align-items: center;
                gap: 8px;
                font-weight: 700;
                border: none;
                padding: 11px 20px;
                border-radius: var(--radius-sm);
                cursor: pointer;
                font-family: inherit;
                font-size: 13.5px;
                transition: transform 0.12s ease, box-shadow 0.12s ease, opacity 0.12s ease;
            }
            .btn:active { transform: scale(0.97); }
            .btn-refresh {
                background-color: var(--accent-orange);
                color: #fff;
                box-shadow: 0 6px 16px -4px rgba(240, 90, 40, 0.45);
            }
            .btn-refresh:hover { opacity: 0.92; }
            .btn-copy {
                background-color: var(--card-bg);
                color: var(--text-dark);
                border: 1px solid var(--border-color);
                box-shadow: var(--shadow-card);
            }
            .btn-copy:hover { border-color: var(--accent-orange); color: var(--accent-orange); }
            .btn-copy svg { width: 16px; height: 16px; flex-shrink: 0; }

            .time-selector { display: flex; gap: 8px; margin-bottom: 14px; flex-wrap: wrap; align-items: center; }
            .time-btn {
                background-color: var(--card-bg);
                border: 1px solid var(--border-color);
                color: var(--text-muted);
                padding: 9px 20px;
                border-radius: 999px;
                cursor: pointer;
                font-size: 13px;
                font-weight: 700;
                font-family: inherit;
                transition: all 0.15s ease;
            }
            .time-btn:hover { border-color: var(--accent-orange); color: var(--accent-orange); }
            .time-btn.active {
                background-color: var(--sidebar-bg);
                color: #fff;
                border-color: var(--sidebar-bg);
            }
            .time-btn:disabled, .btn:disabled { opacity: 0.6; cursor: not-allowed; }
            .time-btn svg { width: 13px; height: 13px; vertical-align: -2px; margin-left: 5px; }

            .custom-range-box {
                display: none;
                align-items: center;
                gap: 8px;
                background-color: var(--card-bg);
                border: 1px solid var(--border-color);
                border-radius: 999px;
                padding: 6px 8px 6px 16px;
                flex-wrap: wrap;
            }
            .custom-range-box input[type="date"] {
                border: 1px solid var(--border-color);
                border-radius: 999px;
                padding: 7px 12px;
                font-family: inherit;
                font-size: 12.5px;
                color: var(--text-dark);
                background: var(--nested-bg);
            }
            .custom-range-box span { font-size: 12px; color: var(--text-muted); font-weight: 700; }
            .custom-range-box .btn-apply-range {
                background-color: var(--accent-orange);
                color: #fff;
                border: none;
                padding: 8px 16px;
                border-radius: 999px;
                font-family: inherit;
                font-size: 12.5px;
                font-weight: 700;
                cursor: pointer;
            }
            .custom-range-box .btn-apply-range:hover { opacity: 0.9; }
            .range-caption { margin-bottom: 26px; font-size: 12px; color: var(--text-muted); font-weight: 600; }
            .range-caption b { color: var(--text-dark); }

            .cards-container { display: grid; grid-template-columns: repeat(4, 1fr); gap: 18px; margin-bottom: 22px; }
            .card {
                background-color: var(--card-bg);
                border-radius: var(--radius-lg);
                box-shadow: var(--shadow-card);
                padding: 22px 22px;
                text-align: right;
            }
            .card-top { display: flex; align-items: center; justify-content: space-between; margin-bottom: 14px; }
            .card-title { font-size: 13px; color: var(--text-muted); font-weight: 600; }
            .card-icon {
                width: 36px; height: 36px; border-radius: 10px;
                display: flex; align-items: center; justify-content: center;
                flex-shrink: 0; overflow: hidden;
            }
            .card-icon svg { width: 20px; height: 20px; display: block; }
            .card-icon.icon-wallet svg { width: 18px; height: 18px; }
            .card-value { font-size: 27px; font-weight: 800; color: var(--text-dark); margin-bottom: 6px; letter-spacing: -0.5px; }
            .card-sub { font-size: 12.5px; font-weight: 700; }
            .card-meta { margin-top: 12px; padding-top: 12px; border-top: 1px dashed var(--border-color); display: flex; gap: 14px; font-size: 11.5px; color: var(--text-muted); font-weight: 600; }
            .card-meta span b { color: var(--text-dark); font-weight: 800; }

            .grid-2 { display: grid; grid-template-columns: 2fr 1fr; gap: 18px; margin-bottom: 22px; align-items: stretch; }

            .chart-section {
                background-color: var(--card-bg);
                border-radius: var(--radius-lg);
                box-shadow: var(--shadow-card);
                padding: 24px;
            }
            .chart-section-title { font-weight: 800; margin-bottom: 4px; color: var(--text-dark); font-size: 15px; }
            .chart-section-sub { font-size: 12px; color: var(--text-muted); margin-bottom: 16px; font-weight: 500; }
            .chart-wrapper { height: 260px; position: relative; }
            .donut-wrapper { height: 220px; position: relative; }

            .legend-list { display: flex; flex-direction: column; gap: 10px; margin-top: 16px; }
            .legend-item { display: flex; align-items: center; justify-content: space-between; font-size: 12.5px; font-weight: 600; color: var(--text-muted); }
            .legend-dot { width: 9px; height: 9px; border-radius: 50%; display: inline-block; margin-left: 8px; }
            .legend-label { display: flex; align-items: center; }
            .legend-value { color: var(--text-dark); font-weight: 800; }

            /* ===== Explorer (Meta Ads Manager style) ===== */
            .explorer-card {
                background-color: var(--card-bg);
                border-radius: var(--radius-lg);
                box-shadow: var(--shadow-card);
            }
            .explorer-sticky-zone {
                position: sticky;
                top: 0;
                z-index: 15;
                background: var(--card-bg);
                border-radius: var(--radius-lg) var(--radius-lg) 0 0;
                box-shadow: 0 6px 14px -12px rgba(15, 37, 64, 0.3);
            }
            .explorer-top {
                display: flex;
                align-items: center;
                justify-content: space-between;
                gap: 14px;
                padding: 16px 22px 0 22px;
                flex-wrap: wrap;
            }
            .platform-tabs { display: flex; gap: 6px; }
            .platform-tab {
                display: flex; align-items: center; gap: 7px;
                padding: 8px 16px; border-radius: 999px; cursor: pointer;
                font-size: 12.5px; font-weight: 700; color: var(--text-muted);
                border: 1px solid var(--border-color); background: var(--card-bg);
                transition: all 0.15s ease;
            }
            .platform-tab .pdot { width: 8px; height: 8px; border-radius: 50%; }
            .platform-tab.active { background: var(--text-dark); color: #fff; border-color: var(--text-dark); }
            .platform-tab:hover:not(.active) { border-color: var(--text-dark); color: var(--text-dark); }

            /* Compact synced date-filter dropdown, positioned top-left of the explorer */
            .date-filter-dropdown { position: relative; }
            .date-filter-trigger {
                display: flex; align-items: center; gap: 8px;
                background: var(--nested-bg); border: 1px solid var(--border-color);
                border-radius: 999px; padding: 9px 15px; font-family: inherit;
                font-size: 12.5px; font-weight: 700; color: var(--text-dark); cursor: pointer;
                white-space: nowrap;
            }
            .date-filter-trigger:hover { border-color: var(--accent-orange); }
            .date-filter-trigger svg { width: 14px; height: 14px; flex-shrink: 0; color: var(--text-muted); }
            .date-filter-trigger .chevron-down { width: 11px; height: 11px; color: var(--text-faint); transition: transform 0.15s ease; }
            .date-filter-dropdown.open .date-filter-trigger { border-color: var(--accent-orange); }
            .date-filter-dropdown.open .chevron-down { transform: rotate(180deg); }
            .date-filter-panel {
                display: none;
                position: absolute; top: calc(100% + 8px); left: 0;
                background: var(--card-bg); border: 1px solid var(--border-color);
                border-radius: 14px; box-shadow: var(--shadow-pop); padding: 14px;
                width: 250px; z-index: 30;
            }
            .date-filter-dropdown.open .date-filter-panel { display: block; }
            .date-filter-presets { display: flex; flex-direction: column; gap: 6px; margin-bottom: 12px; }
            .date-filter-btn {
                text-align: right; background: var(--nested-bg); border: 1px solid transparent;
                border-radius: 10px; padding: 9px 12px; font-family: inherit; font-size: 13px;
                font-weight: 600; color: var(--text-dark); cursor: pointer; width: 100%;
                transition: all 0.12s ease;
            }
            .date-filter-btn:hover { background: var(--nested-deep); }
            .date-filter-btn.active { background: var(--text-dark); color: #fff; }
            .date-filter-custom-block { border-top: 1px dashed var(--border-color); padding-top: 12px; }
            .date-filter-custom-block > span { font-size: 11.5px; font-weight: 700; color: var(--text-muted); display: block; margin-bottom: 8px; }
            .date-filter-custom-inputs { display: flex; align-items: center; gap: 6px; margin-bottom: 10px; }
            .date-filter-custom-inputs input[type="date"] {
                flex: 1; min-width: 0; border: 1px solid var(--border-color); border-radius: 8px;
                padding: 7px 6px; font-family: inherit; font-size: 11.5px; background: var(--nested-bg);
            }
            .date-filter-custom-inputs span { font-size: 11px; color: var(--text-muted); }
            .date-filter-custom-block .btn-apply-range { width: 100%; }

            .level-tabs {
                display: flex;
                gap: 6px;
                padding: 16px 22px 0 22px;
                border-bottom: 1px solid var(--border-color);
                overflow-x: auto;
            }
            .level-tab {
                padding: 10px 6px 14px 6px;
                font-weight: 700;
                font-size: 14px;
                color: var(--text-muted);
                cursor: pointer;
                border-bottom: 3px solid transparent;
                margin-bottom: -1px;
                white-space: nowrap;
                transition: color 0.15s ease;
            }
            .level-tab .tab-count {
                display: inline-flex; align-items:center; justify-content:center;
                min-width: 18px; height: 18px; padding: 0 5px; margin-right: 6px;
                border-radius: 999px; background: var(--nested-deep); color: var(--text-muted);
                font-size: 10.5px; font-weight: 800;
            }
            .level-tab.active { color: var(--text-dark); border-bottom-color: var(--accent-orange); }
            .level-tab.active .tab-count { background: var(--accent-orange-light); color: var(--accent-orange); }
            .level-tab:hover { color: var(--text-dark); }

            .filter-chips { display: flex; gap: 8px; padding: 14px 22px 0 22px; flex-wrap: wrap; }
            .filter-chip {
                display: inline-flex; align-items: center; gap: 8px;
                background: #eef4ff; color: var(--accent-blue);
                padding: 6px 8px 6px 14px; border-radius: 999px;
                font-size: 12px; font-weight: 700;
            }
            .filter-chip button {
                background: rgba(47,111,237,0.14); border: none; color: var(--accent-blue);
                width: 18px; height: 18px; border-radius: 50%; cursor: pointer;
                display: flex; align-items: center; justify-content: center; font-size: 12px; line-height: 1;
                font-family: inherit;
            }

            .table-toolbar {
                display: flex; align-items: center; gap: 10px;
                padding: 14px 22px; flex-wrap: wrap;
            }
            .search-input {
                flex: 1; min-width: 220px;
                border: 1px solid var(--border-color); border-radius: 10px;
                padding: 10px 14px; font-family: inherit; font-size: 13px;
                background: var(--nested-bg); color: var(--text-dark);
            }
            .search-input:focus { outline: none; border-color: var(--accent-orange); background: #fff; }
            .toolbar-btn {
                background: var(--card-bg); border: 1px solid var(--border-color); border-radius: 10px;
                padding: 9px 14px; font-size: 12.5px; font-weight: 700; color: var(--text-muted);
                cursor: pointer; display: flex; gap: 6px; align-items: center; font-family: inherit;
                white-space: nowrap;
            }
            .toolbar-btn:hover { border-color: var(--accent-orange); color: var(--accent-orange); }
            .toolbar-btn.primary { background: var(--text-dark); color: #fff; border-color: var(--text-dark); }
            .toolbar-btn.primary:hover { opacity: 0.9; color: #fff; }
            .toolbar-btn svg { width: 14px; height: 14px; }
            .table-scroll { border-radius: 0 0 var(--radius-lg) var(--radius-lg); overflow-x: auto; overflow-y: hidden; }
            table { width: 100%; border-collapse: collapse; text-align: right; font-size: 13.5px; min-width: 900px; }
            th, td { padding: 14px 18px; border-bottom: 1px solid var(--border-color); white-space: nowrap; }
            th {
                color: var(--text-muted); font-weight: 700; background-color: var(--nested-deep); font-size: 12px;
                position: sticky; top: 0; cursor: pointer; user-select: none;
            }
            th.no-sort { cursor: default; }
            th .sort-ico { font-size: 10px; margin-right: 4px; opacity: 0.45; }
            th.sorted .sort-ico { opacity: 1; color: var(--accent-orange); }
            tbody tr:last-child td { border-bottom: none; }
            tbody tr.data-row { transition: background-color 0.1s ease; }
            tbody tr.data-row.clickable { cursor: pointer; }
            tbody tr.data-row.clickable:hover { background-color: var(--nested-bg); }

            .name-cell { display: flex; align-items: center; gap: 8px; font-weight: 700; color: var(--text-dark); max-width: 280px; overflow: hidden; text-overflow: ellipsis; }
            .name-icon { flex-shrink: 0; }
            .drill-arrow { margin-right: auto; color: var(--text-faint); font-size: 12px; }

            .toggle-switch { position: relative; display: inline-block; width: 36px; height: 20px; direction: ltr; }
            .toggle-switch input { opacity: 0; width: 0; height: 0; position: absolute; }
            .toggle-slider {
                position: absolute; inset: 0; cursor: not-allowed;
                background-color: #d7dee8; transition: 0.2s; border-radius: 999px;
            }
            .toggle-slider:before {
                content: ""; position: absolute; height: 14px; width: 14px; left: 3px; top: 3px;
                background-color: #fff; transition: 0.2s; border-radius: 50%; box-shadow: 0 1px 2px rgba(0,0,0,0.25);
            }
            .toggle-switch input:checked + .toggle-slider { background-color: var(--accent-green); }
            .toggle-switch input:checked + .toggle-slider:before { transform: translateX(16px); }

            .status-pill { display: inline-flex; align-items: center; gap: 6px; font-size: 12.5px; font-weight: 700; }
            .status-dot { width: 7px; height: 7px; border-radius: 50%; }
            .status-active .status-dot { background-color: var(--accent-green); }
            .status-active { color: var(--text-dark); }
            .status-paused .status-dot { background-color: var(--text-faint); }
            .status-paused { color: var(--text-muted); }

            .metric-main { font-weight: 800; color: var(--text-dark); }
            .metric-sub { font-size: 11px; color: var(--text-muted); font-weight: 500; margin-top: 2px; white-space: normal; max-width: 160px; }

            .badge { padding: 3px 10px; border-radius: 999px; font-size: 10.5px; font-weight: 800; display: inline-block; }
            .badge-good { background-color: var(--stage-green-bg); border: 1px solid var(--stage-green-border); color: var(--stage-green-text); }
            .badge-medium { background-color: var(--stage-blue-bg); border: 1px solid var(--stage-blue-border); color: var(--stage-blue-text); }
            .badge-bad { background-color: var(--stage-red-bg); border: 1px solid var(--stage-red-border); color: var(--stage-red-text); }

            .empty-state { padding: 50px 20px; text-align: center; color: var(--text-muted); font-weight: 600; font-size: 14px; }

            /* Toast */
            #toast {
                position: fixed;
                bottom: 28px;
                left: 50%;
                transform: translateX(-50%) translateY(20px);
                background-color: var(--text-dark);
                color: #fff;
                padding: 14px 26px;
                border-radius: 999px;
                font-weight: 700;
                font-size: 14px;
                box-shadow: var(--shadow-pop);
                opacity: 0;
                pointer-events: none;
                transition: opacity 0.25s ease, transform 0.25s ease;
                z-index: 999;
                display: flex;
                align-items: center;
                gap: 8px;
                white-space: nowrap;
            }
            #toast.show { opacity: 1; transform: translateX(-50%) translateY(0); }

            ::-webkit-scrollbar { height: 8px; width: 8px; }
            ::-webkit-scrollbar-thumb { background-color: #dbe1ea; border-radius: 10px; }
            ::-webkit-scrollbar-track { background: transparent; }

            @media (max-width: 1100px) {
                .cards-container { grid-template-columns: repeat(2, 1fr); }
                .grid-2 { grid-template-columns: 1fr; }
            }
        </style>
    </head>
    <body>
        <div class="sidebar">
            <div class="sidebar-logo">eleven<span>z</span></div>
            <ul class="sidebar-menu">
                <li class="active"><a href="#">📊 الإعلانات والتحليلات</a></li>
                <li><a href="#">🎯 الحملات النشطة</a></li>
                <li><a href="#">📈 تقارير الأداء الأسبوعية</a></li>
            </ul>
            <div class="user-profile">
                <div class="user-avatar">A</div>
                <div>
                    <div style="font-weight: 800; font-size: 14px;">أبو بكر</div>
                    <div style="font-size: 11px; color: #93a4bb; font-weight: 600;">مدير الحملات</div>
                </div>
            </div>
        </div>

        <div class="main-content">
            <div class="top-bar">
                <div class="header-title">
                    <h1>منصة التقرير الإعلاني - elevenz</h1>
                    <p id="update-time">تتبع مباشر للأداء والمؤشرات | آخر تحديث: --</p>
                </div>
                <div class="top-actions">
                    <button class="btn btn-copy" onclick="copyReport()">
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="12" height="12" rx="3"></rect><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path></svg>
                        نسخ التقرير
                    </button>
                    <button class="btn btn-refresh" onclick="refreshData()">
                        <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M23 4v6h-6"></path><path d="M1 20v-6h6"></path><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"></path></svg>
                        تحديث البيانات
                    </button>
                </div>
            </div>

            <div class="time-selector">
                <button class="time-btn" data-preset="today" onclick="applyDatePreset('today')">اليوم</button>
                <button class="time-btn active" data-preset="yesterday" onclick="applyDatePreset('yesterday')">أمس</button>
                <button class="time-btn" data-preset="last7" onclick="applyDatePreset('last7')">آخر 7 أيام</button>
                <button class="time-btn" data-preset="last14" onclick="applyDatePreset('last14')">آخر 14 يوماً</button>
                <button class="time-btn" data-preset="thismonth" onclick="applyDatePreset('thismonth')">هذا الشهر</button>
                <button class="time-btn" id="btn-custom-toggle" onclick="toggleCustomRange(this)">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="18" rx="3"></rect><path d="M16 2v4M8 2v4M3 10h18"></path></svg>
                    نطاق مخصص
                </button>
                <div class="custom-range-box" id="custom-range-box">
                    <span>من</span>
                    <input type="date" id="date-from-input">
                    <span>إلى</span>
                    <input type="date" id="date-to-input">
                    <button class="btn-apply-range" onclick="applyCustomRange()">تطبيق</button>
                </div>
            </div>
            <div class="range-caption" id="range-caption">الفترة المعروضة: <b>أمس</b></div>

            <div class="cards-container">
                <div class="card">
                    <div class="card-top">
                        <div class="card-title">إجمالي الإنفاق (الكلي)</div>
                        <div class="card-icon icon-wallet" style="background:var(--accent-orange-light);">
                            <svg viewBox="0 0 24 24" fill="none" stroke="#f05a28" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12V7H5a2 2 0 0 1 0-4h14v4"></path><path d="M3 5v14a2 2 0 0 0 2 2h16v-5"></path><path d="M18 12a2 2 0 0 0 0 4h4v-4Z"></path></svg>
                        </div>
                    </div>
                    <div class="card-value" id="total-spend">0.00 ر.س</div>
                    <div class="card-sub" style="color:var(--text-muted);">جميع حسابات الربط</div>
                    <div class="card-meta">
                        <span>CPC: <b id="total-cpc">0.00</b></span>
                        <span>CTR: <b id="total-ctr">0.0%</b></span>
                    </div>
                </div>
                <div class="card">
                    <div class="card-top">
                        <div class="card-title">Google Ads</div>
                        <div class="card-icon" style="background:#f8f9fa;">
                            <svg viewBox="0 0 48 48"><path fill="#FFC107" d="M43.611,20.083H42V20H24v8h11.303c-1.649,4.657-6.08,8-11.303,8c-6.627,0-12-5.373-12-12c0-6.627,5.373-12,12-12c3.059,0,5.842,1.154,7.961,3.039l5.657-5.657C34.046,6.053,29.268,4,24,4C12.955,4,4,12.955,4,24c0,11.045,8.955,20,20,20c11.045,0,20-8.955,20-20C44,22.659,43.862,21.35,43.611,20.083z"></path><path fill="#FF3D00" d="M6.306,14.691l6.571,4.819C14.655,15.108,18.961,12,24,12c3.059,0,5.842,1.154,7.961,3.039l5.657-5.657C34.046,6.053,29.268,4,24,4C16.318,4,9.656,8.337,6.306,14.691z"></path><path fill="#4CAF50" d="M24,44c5.166,0,9.86-1.977,13.409-5.192l-6.19-5.238C29.211,35.091,26.715,36,24,36c-5.202,0-9.619-3.317-11.283-7.946l-6.522,5.025C9.505,39.556,16.227,44,24,44z"></path><path fill="#1976D2" d="M43.611,20.083H42V20H24v8h11.303c-0.792,2.237-2.231,4.166-4.087,5.571c0.001-0.001,0.002-0.001,0.003-0.002l6.19,5.238C36.971,39.205,44,34,44,24C44,22.659,43.862,21.35,43.611,20.083z"></path></svg>
                        </div>
                    </div>
                    <div class="card-value" id="google-spend">0.00 ر.س</div>
                    <div class="card-sub" id="google-sub" style="color:var(--accent-blue);">0 إحالات</div>
                    <div class="card-meta">
                        <span>CPA: <b id="google-cpa">--</b></span>
                        <span>CTR: <b id="google-ctr">0.0%</b></span>
                    </div>
                </div>
                <div class="card">
                    <div class="card-top">
                        <div class="card-title">TikTok Ads</div>
                        <div class="card-icon" style="background:#0f0f0f;">
                            <svg viewBox="0 0 48 48"><path fill="#25F4EE" d="M33.6,15.4c-2.1-1.4-3.6-3.6-4-6.2c-0.1-0.5-0.1-1-0.1-1.5h-6v24.6c0,2.9-2.4,5.3-5.3,5.3 c-0.9,0-1.8-0.2-2.5-0.7c-1.7-0.9-2.8-2.7-2.8-4.7c0-2.9,2.4-5.3,5.3-5.3c0.5,0,1.1,0.1,1.6,0.3v-6.1c-0.5-0.1-1-0.1-1.6-0.1 c-6.3,0-11.4,5.1-11.4,11.4c0,3.9,1.9,7.3,4.9,9.4c1.9,1.3,4.2,2.1,6.7,2.1c6.3,0,11.4-5.1,11.4-11.4V19.1 c2.4,1.8,5.4,2.8,8.6,2.8v-6C36.9,15.9,35.1,15.8,33.6,15.4z"></path><path fill="#FE2C55" d="M31.6,13.4c-2.1-1.4-3.6-3.6-4-6.2c-0.1-0.5-0.1-1-0.1-1.5h-6v24.6c0,2.9-2.4,5.3-5.3,5.3 c-0.9,0-1.8-0.2-2.5-0.7c-1.5-0.8-2.6-2.4-2.8-4.2c-0.4-2.9,1.7-5.6,4.5-6c0.5-0.1,1.1-0.1,1.6,0v-6.1c-6.2-0.1-11.3,4.9-11.4,11.1 c0,3.9,1.9,7.5,4.9,9.6c1.9,1.3,4.2,2.1,6.7,2.1c6.3,0,11.4-5.1,11.4-11.4V17.1c2.4,1.8,5.4,2.8,8.6,2.8v-6 C34.9,13.9,33.1,13.8,31.6,13.4z"></path><path fill="#ffffff" d="M35,17.9c-3.2,0-6.2-1-8.6-2.8v14.7c0,6.3-5.1,11.4-11.4,11.4c-2.5,0-4.8-0.8-6.7-2.1 c2.1,2.3,5.1,3.7,8.4,3.7c6.3,0,11.4-5.1,11.4-11.4V16.7c2.4,1.8,5.4,2.8,8.6,2.8v-6C36.4,13.5,36.4,13.5,35,17.9z"></path></svg>
                        </div>
                    </div>
                    <div class="card-value" id="tiktok-spend">0.00 ر.س</div>
                    <div class="card-sub" id="tiktok-sub" style="color:var(--accent-orange);">0 تحويل/نقرة</div>
                    <div class="card-meta">
                        <span>CPA: <b id="tiktok-cpa">--</b></span>
                        <span>CTR: <b id="tiktok-ctr">0.0%</b></span>
                    </div>
                </div>
                <div class="card">
                    <div class="card-top">
                        <div class="card-title">Meta Ads</div>
                        <div class="card-icon" style="background:#e7f0ff;">
                            <svg viewBox="0 0 36 36"><path fill="#1877F2" d="M36,18c0-9.94-8.06-18-18-18S0,8.06,0,18c0,8.98,6.58,16.41,15.19,17.76V23.13h-4.57V18h4.57v-3.91 c0-4.51,2.69-7.01,6.8-7.01c1.97,0,4.03,0.35,4.03,0.35v4.43h-2.27c-2.24,0-2.94,1.39-2.94,2.81V18h5.01l-0.8,5.13h-4.21v12.63 C29.42,34.41,36,26.98,36,18z"></path></svg>
                        </div>
                    </div>
                    <div class="card-value" id="meta-spend">0.00 ر.س</div>
                    <div class="card-sub" id="meta-sub" style="color:#0284c7;">0 محادثة/نتيجة</div>
                    <div class="card-meta">
                        <span>CPA: <b id="meta-cpa">--</b></span>
                        <span>CTR: <b id="meta-ctr">0.0%</b></span>
                    </div>
                </div>
            </div>

            <div class="grid-2">
                <div class="chart-section">
                    <div class="chart-section-title" id="chart-period-title">تحليل الإنفاق حسب المنصة</div>
                    <div class="chart-section-sub">مقارنة الإنفاق الإعلاني عبر جميع المنصات المرتبطة</div>
                    <div class="chart-wrapper">
                        <canvas id="spendChart"></canvas>
                    </div>
                </div>
                <div class="chart-section">
                    <div class="chart-section-title">توزيع النتائج</div>
                    <div class="chart-section-sub">حصة كل منصة من إجمالي النتائج/التحويلات</div>
                    <div class="donut-wrapper">
                        <canvas id="donutChart"></canvas>
                    </div>
                    <div class="legend-list" id="donut-legend"></div>
                </div>
            </div>

            <div class="explorer-card">
                <div class="explorer-sticky-zone">
                    <div class="explorer-top">
                        <div class="platform-tabs" id="platform-tabs"></div>
                        <div class="date-filter-dropdown" id="date-filter-dropdown">
                            <button class="date-filter-trigger" onclick="toggleDateDropdown(event)">
                                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="18" rx="3"></rect><path d="M16 2v4M8 2v4M3 10h18"></path></svg>
                                <span id="date-filter-trigger-label">أمس</span>
                                <svg class="chevron-down" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"></polyline></svg>
                            </button>
                            <div class="date-filter-panel">
                                <div class="date-filter-presets">
                                    <button class="date-filter-btn" data-preset="today" onclick="applyDatePreset('today')">اليوم</button>
                                    <button class="date-filter-btn" data-preset="yesterday" onclick="applyDatePreset('yesterday')">أمس</button>
                                    <button class="date-filter-btn" data-preset="last7" onclick="applyDatePreset('last7')">آخر 7 أيام</button>
                                    <button class="date-filter-btn" data-preset="last14" onclick="applyDatePreset('last14')">آخر 14 يوماً</button>
                                    <button class="date-filter-btn" data-preset="thismonth" onclick="applyDatePreset('thismonth')">هذا الشهر</button>
                                </div>
                                <div class="date-filter-custom-block">
                                    <span>نطاق مخصص</span>
                                    <div class="date-filter-custom-inputs">
                                        <input type="date" id="date-filter-panel-from">
                                        <span>إلى</span>
                                        <input type="date" id="date-filter-panel-to">
                                    </div>
                                    <button class="btn-apply-range" onclick="applyCustomRangeFromPanel()">تطبيق</button>
                                </div>
                            </div>
                        </div>
                    </div>
                    <div class="level-tabs" id="level-tabs"></div>
                    <div class="filter-chips" id="filter-chips"></div>
                    <div class="table-toolbar">
                        <input type="text" class="search-input" id="search-input" placeholder="ابحث عن حملة أو مجموعة أو إعلان" oninput="onSearch(this.value)">
                        <button class="toolbar-btn" onclick="notImplementedYet()">
                            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7" height="18"></rect><rect x="14" y="3" width="7" height="10"></rect></svg>
                            الأعمدة
                        </button>
                        <button class="toolbar-btn" onclick="notImplementedYet()">
                            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15V6M18.5 18a2.5 2.5 0 1 0 0-5 2.5 2.5 0 0 0 0 5ZM12 12H3M16 6H3M12 18H3"></path></svg>
                            التجميع
                        </button>
                        <button class="toolbar-btn" id="export-btn" onclick="exportCsv()">
                            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path><polyline points="7 10 12 15 17 10"></polyline><line x1="12" y1="15" x2="12" y2="3"></line></svg>
                            تصدير CSV
                        </button>
                    </div>
                </div>
                <div class="table-scroll">
                    <table>
                        <thead>
                            <tr id="table-head"></tr>
                        </thead>
                        <tbody id="table-body"></tbody>
                    </table>
                </div>
            </div>
        </div>

        <div id="toast"></div>

        <script>
            Chart.register(ChartDataLabels);
            Chart.defaults.locale = 'en-US';
            Chart.defaults.font.family = 'Cairo';

            let chartInstance = null;
            let donutInstance = null;
            let globalData = {};
            let lastMetrics = null;
            let isLoadingData = false;

            // نطاق التاريخ الحالي المطبَّق فعلياً على الطلب المرسل لـ Windsor.ai
            let dateRangeState = { preset: 'yesterday', date_from: null, date_to: null };

            // ===== Explorer state =====
            const PLATFORMS = [
                { key: 'meta', label: 'Meta Ads', dataKey: 'meta_ads', dot: '#0284c7', isMeta: true, isGoogle: false, resultLabel: 'النتائج', resultSub: 'محادثات تم بدؤها عبر الرسائل' },
                { key: 'tiktok', label: 'TikTok Ads', dataKey: 'tiktok_ads', dot: '#f05a28', isMeta: false, isGoogle: false, resultLabel: 'النتائج', resultSub: 'تحويلات/نقرات إعلانية' },
                { key: 'google', label: 'Google Ads', dataKey: 'google_ads', dot: '#0f2540', isMeta: false, isGoogle: true, resultLabel: 'النتائج', resultSub: 'إحالات' }
            ];
            const LEVELS = [
                { key: 'campaigns', label: 'الحملات الإعلانية' },
                { key: 'adsets', label: 'المجموعات الإعلانية' },
                { key: 'ads', label: 'الإعلانات' }
            ];

            let explorerState = {
                platform: 'meta',
                level: 'campaigns',
                selectedCampaign: null,
                selectedGroup: null,
                search: '',
                sortKey: 'spend',
                sortDir: -1
            };

            function safeNum(val) {
                if (!val) return 0;
                let n = parseFloat(val);
                return isNaN(n) ? 0 : n;
            }

            // أنواع الأحداث الخاصة بمحادثات الرسائل على ميتا مرتبة بحسب الأولوية.
            // هذه المراحل الثلاثة تُمثّل نفس قمع المحادثة (بدء → رد أول → اتصال مكتمل)
            // وليست نتائج منفصلة - لذلك يجب أخذ واحدة منها فقط وليس جمعها معاً.
            const META_MESSAGING_ACTION_PRIORITY = [
                'onsite_conversion.messaging_conversation_started_7d',
                'onsite_conversion.total_messaging_connection',
                'onsite_conversion.messaging_first_reply'
            ];

            function parseMetaConversions(item) {
                if (!item) return 0;

                // 1) إن كان حقل conversions القياسي أكبر من صفر، اعتمده مباشرة (هذا يغطي
                //    حملات أهداف الشراء/التحويل القياسية التي تملأ ميتا هذا الحقل لها فعلياً)
                const standardConv = safeNum(item.conversions || item.results);
                if (standardConv > 0) return standardConv;

                // 2) حملات المراسلة/الرسائل عادة لا تملأ حقل conversions، فنستخرج النتيجة
                //    من actions بدلاً من ذلك
                if (item.actions && Array.isArray(item.actions)) {
                    // طابق أحد الأنواع القياسية المعروفة بالضبط (وليس بالاحتواء الجزئي)
                    // حتى لا نخلط بين مراحل قمع المحادثة نفسها أو مع أحداث لا علاقة لها
                    // (مثل "lead" من هدف إعلاني مختلف) ونجمعها خطأً كنتائج مضاعفة.
                    for (const actionType of META_MESSAGING_ACTION_PRIORITY) {
                        const match = item.actions.find(act => act.action_type === actionType);
                        if (match) return safeNum(match.value);
                    }
                    // لم يُعثر على أي من الأنواع القياسية أعلاه: خذ أعلى قيمة مفردة من
                    // الأحداث المتعلقة بالمراسلة/المحادثة دون جمعها (Math.max وليس +=)
                    // لتفادي مضاعفة الرقم في حال وجود أكثر من حدث متشابه بنفس الصف.
                    const relevant = item.actions.filter(act =>
                        act.action_type && (act.action_type.includes('messaging') || act.action_type.includes('conversation'))
                    );
                    if (relevant.length > 0) {
                        return Math.max(...relevant.map(act => safeNum(act.value)));
                    }
                }

                return 0;
            }

            function parseGoogleConversions(item) {
                if (!item) return 0;
                return safeNum(item.conversions || item.all_conversions || item.results);
            }

            // ملاحظة: الباك إند الآن يُرجع بيانات مُصفّاة مسبقاً بحسب نطاق التاريخ
            // المطلوب فعلياً (date_from/date_to تُرسَل مباشرة إلى Windsor.ai)، لذلك لم
            // تعد هناك حاجة لتخمين النطاق على العميل. هذه دالة أمان فقط تضمن مصفوفة صالحة.
            function scopedList(list) {
                return Array.isArray(list) ? list : [];
            }

            function pad2(n) { return n < 10 ? '0' + n : '' + n; }
            function toIsoDate(d) { return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`; }

            function computePresetRange(preset) {
                const today = new Date();
                if (preset === 'today') {
                    return { from: toIsoDate(today), to: toIsoDate(today) };
                }
                if (preset === 'yesterday') {
                    const y = new Date(today);
                    y.setDate(y.getDate() - 1);
                    return { from: toIsoDate(y), to: toIsoDate(y) };
                }
                if (preset === 'last7') {
                    const f = new Date(today);
                    f.setDate(f.getDate() - 6);
                    return { from: toIsoDate(f), to: toIsoDate(today) };
                }
                if (preset === 'last14') {
                    const f = new Date(today);
                    f.setDate(f.getDate() - 13);
                    return { from: toIsoDate(f), to: toIsoDate(today) };
                }
                if (preset === 'thismonth') {
                    const f = new Date(today.getFullYear(), today.getMonth(), 1);
                    return { from: toIsoDate(f), to: toIsoDate(today) };
                }
                return null;
            }

            function buildRangeLabel(preset, dateFrom, dateTo) {
                const presetNames = {
                    today: 'اليوم', yesterday: 'أمس', last7: 'آخر 7 أيام',
                    last14: 'آخر 14 يوماً', thismonth: 'هذا الشهر'
                };
                if (preset && presetNames[preset]) {
                    if (dateFrom === dateTo) return `${presetNames[preset]} (${dateFrom})`;
                    return `${presetNames[preset]} (${dateFrom} → ${dateTo})`;
                }
                if (dateFrom === dateTo) return dateFrom;
                return `من ${dateFrom} إلى ${dateTo}`;
            }

            function setLoadingState(loading) {
                isLoadingData = loading;
                document.querySelectorAll('.time-btn, .btn-refresh, .btn-apply-range, .date-filter-btn, .date-filter-trigger').forEach(b => { b.disabled = loading; });
                if (loading) {
                    document.getElementById('update-time').innerText = 'جاري تحديث البيانات من Windsor.ai...';
                }
            }

            const PRESET_LABELS = {
                today: 'اليوم', yesterday: 'أمس', last7: 'آخر 7 أيام',
                last14: 'آخر 14 يوماً', thismonth: 'هذا الشهر', custom: 'نطاق مخصص'
            };

            // يزامن كل عناصر واجهة فلتر التاريخ (الشريط العلوي + القائمة المنسدلة
            // المصغّرة فوق الجدول) مع dateRangeState الحالي، أياً كان مصدر التغيير.
            function refreshDateFilterUI() {
                const label = PRESET_LABELS[dateRangeState.preset] || 'نطاق مخصص';
                const labelEl = document.getElementById('date-filter-trigger-label');
                if (labelEl) labelEl.innerText = label;

                document.querySelectorAll('[data-preset]').forEach(btn => {
                    btn.classList.toggle('active', btn.dataset.preset === dateRangeState.preset);
                });

                ['date-from-input', 'date-filter-panel-from'].forEach(id => {
                    const el = document.getElementById(id);
                    if (el && dateRangeState.date_from) el.value = dateRangeState.date_from;
                });
                ['date-to-input', 'date-filter-panel-to'].forEach(id => {
                    const el = document.getElementById(id);
                    if (el && dateRangeState.date_to) el.value = dateRangeState.date_to;
                });
            }

            async function applyDatePreset(preset) {
                if (isLoadingData) return;
                document.getElementById('custom-range-box').style.display = 'none';
                closeDateDropdown();

                const range = computePresetRange(preset);
                dateRangeState = { preset, date_from: range.from, date_to: range.to };
                refreshDateFilterUI();
                await fetchDataForRange(range.from, range.to);
            }

            function toggleCustomRange(btn) {
                closeDateDropdown();
                const box = document.getElementById('custom-range-box');
                const isHidden = box.style.display === 'none' || !box.style.display;
                box.style.display = isHidden ? 'flex' : 'none';
                if (isHidden) {
                    if (!document.getElementById('date-from-input').value) {
                        document.getElementById('date-from-input').value = dateRangeState.date_from || toIsoDate(new Date());
                        document.getElementById('date-to-input').value = dateRangeState.date_to || toIsoDate(new Date());
                    }
                }
            }

            function toggleDateDropdown(evt) {
                if (evt) evt.stopPropagation();
                document.getElementById('custom-range-box').style.display = 'none';
                const dd = document.getElementById('date-filter-dropdown');
                const opening = !dd.classList.contains('open');
                dd.classList.toggle('open', opening);
                if (opening) {
                    const fromEl = document.getElementById('date-filter-panel-from');
                    const toEl = document.getElementById('date-filter-panel-to');
                    if (fromEl && !fromEl.value) fromEl.value = dateRangeState.date_from || toIsoDate(new Date());
                    if (toEl && !toEl.value) toEl.value = dateRangeState.date_to || toIsoDate(new Date());
                }
            }

            function closeDateDropdown() {
                const dd = document.getElementById('date-filter-dropdown');
                if (dd) dd.classList.remove('open');
            }

            document.addEventListener('click', (e) => {
                const dd = document.getElementById('date-filter-dropdown');
                if (dd && dd.classList.contains('open') && !dd.contains(e.target)) {
                    dd.classList.remove('open');
                }
            });

            async function applyCustomRangeGeneric(fromId, toId) {
                if (isLoadingData) return;
                const from = document.getElementById(fromId).value;
                const to = document.getElementById(toId).value;
                if (!from || !to) { showToast('يرجى تحديد تاريخي البداية والنهاية'); return; }
                if (from > to) { showToast('تاريخ البداية يجب أن يسبق تاريخ النهاية أو يساويه'); return; }

                dateRangeState = { preset: 'custom', date_from: from, date_to: to };
                refreshDateFilterUI();
                closeDateDropdown();
                document.getElementById('custom-range-box').style.display = 'none';
                await fetchDataForRange(from, to);
            }

            async function applyCustomRange() {
                await applyCustomRangeGeneric('date-from-input', 'date-to-input');
            }

            async function applyCustomRangeFromPanel() {
                await applyCustomRangeGeneric('date-filter-panel-from', 'date-filter-panel-to');
            }

            async function fetchDataForRange(dateFrom, dateTo) {
                setLoadingState(true);
                try {
                    const res = await fetch(`/api/data?date_from=${dateFrom}&date_to=${dateTo}`);
                    const json = await res.json();
                    if (json.status !== 'success') {
                        showToast(json.message || 'تعذر تحديث البيانات لهذا النطاق');
                        return;
                    }
                    globalData = json.data || {};
                    updateDashboardUI();
                } catch (e) {
                    console.error('Error fetching range data:', e);
                    showToast('تعذر الاتصال بالخادم');
                } finally {
                    setLoadingState(false);
                }
            }

            async function refreshData() {
                if (isLoadingData) return;
                const { date_from, date_to } = dateRangeState;
                if (!date_from || !date_to) return;
                await fetchDataForRange(date_from, date_to);
                showToast('تم تحديث البيانات! 🔄');
            }

            function tierBadge(ctr) {
                if (ctr >= 1.5) return '<span class="badge badge-good">أداء ممتاز</span>';
                if (ctr >= 0.6) return '<span class="badge badge-medium">أداء متوسط</span>';
                return '<span class="badge badge-bad">يحتاج متابعة</span>';
            }

            function updateChart(mSpend, tSpend, gSpend) {
                const ctx = document.getElementById('spendChart').getContext('2d');
                if (chartInstance) chartInstance.destroy();

                chartInstance = new Chart(ctx, {
                    type: 'bar',
                    data: {
                        labels: ['Meta Ads', 'TikTok Ads', 'Google Ads'],
                        datasets: [{
                            data: [mSpend, tSpend, gSpend],
                            backgroundColor: ['#0284c7', '#f05a28', '#0f2540'],
                            borderRadius: 8,
                            barThickness: 46
                        }]
                    },
                    options: {
                        responsive: true,
                        maintainAspectRatio: false,
                        plugins: {
                            legend: { display: false },
                            tooltip: {
                                backgroundColor: '#0f2540',
                                titleFont: { family: 'Cairo', weight: '700' },
                                bodyFont: { family: 'Cairo' },
                                padding: 10,
                                cornerRadius: 8,
                                callbacks: {
                                    label: (c) => `الإنفاق: ${c.parsed.y.toFixed(2)} ر.س`
                                }
                            },
                            datalabels: {
                                anchor: 'end',
                                align: 'top',
                                color: '#0f2540',
                                font: { weight: 'bold', family: 'Cairo', size: 11 },
                                formatter: (val) => val > 0 ? val.toFixed(0) + ' ر.س' : ''
                            }
                        },
                        scales: {
                            y: { grid: { color: '#eef1f6' }, ticks: { font: { family: 'Cairo' } } },
                            x: { grid: { display: false }, ticks: { font: { family: 'Cairo', weight: '600' } } }
                        }
                    }
                });
            }

            function updateDonut(metaConv, tiktokConv, googleConv) {
                const ctx = document.getElementById('donutChart').getContext('2d');
                if (donutInstance) donutInstance.destroy();
                const total = metaConv + tiktokConv + googleConv;
                const legendEl = document.getElementById('donut-legend');

                if (total <= 0) {
                    legendEl.innerHTML = '<div style="text-align:center; color:var(--text-muted); font-size:12.5px; font-weight:600;">لا توجد نتائج بعد لهذه الفترة</div>';
                }

                donutInstance = new Chart(ctx, {
                    type: 'doughnut',
                    data: {
                        labels: ['Meta Ads', 'TikTok Ads', 'Google Ads'],
                        datasets: [{
                            data: [metaConv, tiktokConv, googleConv],
                            backgroundColor: ['#0284c7', '#f05a28', '#0f2540'],
                            borderWidth: 3,
                            borderColor: '#ffffff'
                        }]
                    },
                    options: {
                        responsive: true,
                        maintainAspectRatio: false,
                        cutout: '68%',
                        plugins: {
                            legend: { display: false },
                            datalabels: { display: false },
                            tooltip: {
                                backgroundColor: '#0f2540',
                                bodyFont: { family: 'Cairo' },
                                padding: 10,
                                cornerRadius: 8
                            }
                        }
                    }
                });

                if (total > 0) {
                    const parts = [
                        { label: 'Meta Ads', value: metaConv, color: '#0284c7' },
                        { label: 'TikTok Ads', value: tiktokConv, color: '#f05a28' },
                        { label: 'Google Ads', value: googleConv, color: '#0f2540' }
                    ];
                    legendEl.innerHTML = parts.map(p => `
                        <div class="legend-item">
                            <span class="legend-label"><span class="legend-dot" style="background:${p.color}"></span>${p.label}</span>
                            <span class="legend-value">${p.value.toLocaleString('en-US')} (${total > 0 ? ((p.value/total)*100).toFixed(0) : 0}%)</span>
                        </div>
                    `).join('');
                }
            }

            function updateDashboardUI() {
                let metaList = scopedList(globalData.meta_ads);
                let tiktokList = scopedList(globalData.tiktok_ads);
                let googleList = scopedList(globalData.google_ads);

                let metaSpend = metaList.reduce((s, i) => s + safeNum(i.spend || i.cost), 0);
                let tiktokSpend = tiktokList.reduce((s, i) => s + safeNum(i.spend || i.cost), 0);
                let googleSpend = googleList.reduce((s, i) => s + safeNum(i.spend || i.cost), 0);

                let metaConv = metaList.reduce((s, i) => s + parseMetaConversions(i), 0);
                let tiktokConv = tiktokList.reduce((s, i) => s + safeNum(i.conversions || i.conversion || i.results), 0);
                let googleConv = googleList.reduce((s, i) => s + parseGoogleConversions(i), 0);

                let metaClicks = metaList.reduce((s, i) => s + safeNum(i.clicks), 0);
                let tiktokClicks = tiktokList.reduce((s, i) => s + safeNum(i.clicks), 0);
                let googleClicks = googleList.reduce((s, i) => s + safeNum(i.clicks), 0);

                let metaImpr = metaList.reduce((s, i) => s + safeNum(i.impressions), 0);
                let tiktokImpr = tiktokList.reduce((s, i) => s + safeNum(i.impressions), 0);
                let googleImpr = googleList.reduce((s, i) => s + safeNum(i.impressions), 0);

                const ctrOf = (clicks, impr) => impr > 0 ? (clicks / impr) * 100 : 0;
                const cpaOf = (spend, conv) => conv > 0 ? spend / conv : null;
                const cpcOf = (spend, clicks) => clicks > 0 ? spend / clicks : 0;

                let metaCtr = ctrOf(metaClicks, metaImpr);
                let tiktokCtr = ctrOf(tiktokClicks, tiktokImpr);
                let googleCtr = ctrOf(googleClicks, googleImpr);

                let metaCpa = cpaOf(metaSpend, metaConv);
                let tiktokCpa = cpaOf(tiktokSpend, tiktokConv);
                let googleCpa = cpaOf(googleSpend, googleConv);

                let totalSpend = metaSpend + tiktokSpend + googleSpend;
                let totalClicks = metaClicks + tiktokClicks + googleClicks;
                let totalImpr = metaImpr + tiktokImpr + googleImpr;

                document.getElementById('meta-spend').innerText = metaSpend.toFixed(2) + ' ر.س';
                document.getElementById('tiktok-spend').innerText = tiktokSpend.toFixed(2) + ' ر.س';
                document.getElementById('google-spend').innerText = googleSpend.toFixed(2) + ' ر.س';
                document.getElementById('total-spend').innerText = totalSpend.toFixed(2) + ' ر.س';

                document.getElementById('meta-sub').innerText = `${metaConv.toLocaleString('en-US')} محادثة/نتيجة`;
                document.getElementById('tiktok-sub').innerText = `${tiktokConv.toLocaleString('en-US')} تحويل/نقرة`;
                document.getElementById('google-sub').innerText = `${googleConv.toLocaleString('en-US')} إحالات`;

                document.getElementById('total-cpc').innerText = cpcOf(totalSpend, totalClicks).toFixed(2);
                document.getElementById('total-ctr').innerText = ctrOf(totalClicks, totalImpr).toFixed(1) + '%';
                document.getElementById('meta-ctr').innerText = metaCtr.toFixed(1) + '%';
                document.getElementById('tiktok-ctr').innerText = tiktokCtr.toFixed(1) + '%';
                document.getElementById('google-ctr').innerText = googleCtr.toFixed(1) + '%';
                document.getElementById('meta-cpa').innerText = metaCpa !== null ? metaCpa.toFixed(2) : '--';
                document.getElementById('tiktok-cpa').innerText = tiktokCpa !== null ? tiktokCpa.toFixed(2) : '--';
                document.getElementById('google-cpa').innerText = googleCpa !== null ? googleCpa.toFixed(2) : '--';

                let timeText = buildRangeLabel(dateRangeState.preset, dateRangeState.date_from, dateRangeState.date_to);
                let nowStr = new Date().toLocaleTimeString('en-US');
                document.getElementById('update-time').innerText = `تقرير الأداء (${timeText}) - آخر تحديث: ${nowStr}`;
                document.getElementById('range-caption').innerHTML = `الفترة المعروضة: <b>${timeText}</b>`;

                updateChart(metaSpend, tiktokSpend, googleSpend);
                updateDonut(metaConv, tiktokConv, googleConv);
                renderExplorer();

                lastMetrics = {
                    timeText, nowStr,
                    metaSpend, tiktokSpend, googleSpend, totalSpend,
                    metaConv, tiktokConv, googleConv,
                    metaCtr, tiktokCtr, googleCtr,
                    metaCpa, tiktokCpa, googleCpa
                };
            }

            // ===== Name accessors shared with previous tree logic =====
            function campaignOf(i) { return i.campaign || i.campaign_name || 'حملة رئيسية'; }
            function groupOf(i) { return i.adset_name || i.adgroup_name || i.ad_group_name || 'المجموعة الإعلانية'; }
            function adOf(i) { return i.ad_name || 'الإعلان'; }

            function convOf(item, cfg) {
                if (cfg.isMeta) return parseMetaConversions(item);
                if (cfg.isGoogle) return parseGoogleConversions(item);
                return safeNum(item.conversions || item.conversion || item.results);
            }

            function platformCfg(key) {
                return PLATFORMS.find(p => p.key === key);
            }

            function aggregateRows(list, keyFn, cfg) {
                let map = {};
                list.forEach(i => {
                    let name = keyFn(i);
                    if (!map[name]) map[name] = { name, spend: 0, clicks: 0, impressions: 0, conv: 0 };
                    map[name].spend += safeNum(i.spend || i.cost);
                    map[name].clicks += safeNum(i.clicks);
                    map[name].impressions += safeNum(i.impressions);
                    map[name].conv += convOf(i, cfg);
                });
                return Object.values(map).map(r => {
                    r.ctr = r.impressions > 0 ? (r.clicks / r.impressions) * 100 : 0;
                    r.cpa = r.conv > 0 ? r.spend / r.conv : null;
                    r.isActive = r.spend > 0;
                    return r;
                });
            }

            function getExplorerRows() {
                const cfg = platformCfg(explorerState.platform);
                const fullList = scopedList(globalData[cfg.dataKey]);
                let rows = [];

                if (explorerState.level === 'campaigns') {
                    rows = aggregateRows(fullList, campaignOf, cfg);
                } else if (explorerState.level === 'adsets') {
                    let scoped = explorerState.selectedCampaign
                        ? fullList.filter(i => campaignOf(i) === explorerState.selectedCampaign)
                        : fullList;
                    rows = aggregateRows(scoped, groupOf, cfg);
                } else {
                    let scoped = fullList;
                    if (explorerState.selectedCampaign) scoped = scoped.filter(i => campaignOf(i) === explorerState.selectedCampaign);
                    if (explorerState.selectedGroup) scoped = scoped.filter(i => groupOf(i) === explorerState.selectedGroup);
                    rows = aggregateRows(scoped, adOf, cfg);
                }

                if (explorerState.search.trim()) {
                    const q = explorerState.search.trim().toLowerCase();
                    rows = rows.filter(r => r.name.toLowerCase().includes(q));
                }

                rows.sort((a, b) => {
                    let av = a[explorerState.sortKey];
                    let bv = b[explorerState.sortKey];
                    if (av === null) av = -1;
                    if (bv === null) bv = -1;
                    if (typeof av === 'string') return av.localeCompare(bv) * explorerState.sortDir;
                    return (av - bv) * explorerState.sortDir;
                });

                return rows;
            }

            function levelCount(levelKey) {
                const prevLevel = explorerState.level;
                explorerState.level = levelKey;
                const rows = getExplorerRows();
                explorerState.level = prevLevel;
                return rows.length;
            }

            function renderExplorer() {
                renderPlatformTabs();
                renderLevelTabs();
                renderFilterChips();
                renderTableHead();
                renderTableBody();
            }

            function renderPlatformTabs() {
                const el = document.getElementById('platform-tabs');
                el.innerHTML = PLATFORMS.map(p => `
                    <div class="platform-tab ${explorerState.platform === p.key ? 'active' : ''}" onclick="setPlatform('${p.key}')">
                        <span class="pdot" style="background:${p.dot}"></span>${p.label}
                    </div>
                `).join('');
            }

            function renderLevelTabs() {
                const el = document.getElementById('level-tabs');
                el.innerHTML = LEVELS.map(l => `
                    <div class="level-tab ${explorerState.level === l.key ? 'active' : ''}" onclick="setLevel('${l.key}')">
                        <span class="tab-count">${levelCount(l.key)}</span>${l.label}
                    </div>
                `).join('');
            }

            function renderFilterChips() {
                const el = document.getElementById('filter-chips');
                let chips = '';
                if (explorerState.selectedCampaign) {
                    chips += `<span class="filter-chip">الحملة: ${explorerState.selectedCampaign}<button onclick="clearCampaign()">✕</button></span>`;
                }
                if (explorerState.selectedGroup) {
                    chips += `<span class="filter-chip">المجموعة: ${explorerState.selectedGroup}<button onclick="clearGroup()">✕</button></span>`;
                }
                el.innerHTML = chips;
                el.style.display = chips ? 'flex' : 'none';
            }

            function sortIco(key) {
                if (explorerState.sortKey !== key) return '↕';
                return explorerState.sortDir === 1 ? '↑' : '↓';
            }

            function renderTableHead() {
                const cfg = platformCfg(explorerState.platform);
                const head = document.getElementById('table-head');
                const cols = [
                    { key: null, label: '', sortable: false },
                    { key: 'name', label: 'الاسم', sortable: true },
                    { key: 'isActive', label: 'حالة العرض', sortable: true },
                    { key: 'conv', label: cfg.resultLabel, sortable: true },
                    { key: 'cpa', label: 'التكلفة لكل نتيجة', sortable: true },
                    { key: 'spend', label: 'المبلغ الذي تم إنفاقه', sortable: true },
                    { key: 'impressions', label: 'الظهور', sortable: true },
                    { key: 'clicks', label: 'النقرات', sortable: true },
                    { key: 'ctr', label: 'CTR', sortable: true }
                ];
                head.innerHTML = cols.map(c => {
                    if (!c.sortable) return `<th class="no-sort">${c.label}</th>`;
                    const sorted = explorerState.sortKey === c.key ? 'sorted' : '';
                    return `<th class="${sorted}" onclick="sortBy('${c.key}')">${c.label} <span class="sort-ico">${sortIco(c.key)}</span></th>`;
                }).join('');
            }

            function renderTableBody() {
                const cfg = platformCfg(explorerState.platform);
                const rows = getExplorerRows();
                const body = document.getElementById('table-body');
                const isLeaf = explorerState.level === 'ads';

                if (rows.length === 0) {
                    body.innerHTML = `<tr><td colspan="9"><div class="empty-state">لا توجد بيانات مطابقة لهذه الفترة أو الفلتر الحالي</div></td></tr>`;
                    return;
                }

                const icon = explorerState.level === 'campaigns' ? '📂' : (explorerState.level === 'adsets' ? '📁' : '🎯');

                body.innerHTML = rows.map(r => {
                    const cpaText = r.cpa !== null ? r.cpa.toFixed(2) + ' ر.س' : '--';
                    const badge = isLeaf ? ' ' + tierBadge(r.ctr) : '';
                    const clickAttr = isLeaf ? '' : `onclick="drillInto('${r.name.replace(/'/g, "\\'")}')"`;
                    const rowClass = isLeaf ? 'data-row' : 'data-row clickable';

                    return `
                        <tr class="${rowClass}" ${clickAttr}>
                            <td>
                                <label class="toggle-switch" title="حالة تقديرية للقراءة فقط بناءً على النشاط خلال الفترة المحددة">
                                    <input type="checkbox" ${r.isActive ? 'checked' : ''} disabled>
                                    <span class="toggle-slider"></span>
                                </label>
                            </td>
                            <td>
                                <div class="name-cell">
                                    <span class="name-icon">${icon}</span>
                                    <span>${r.name}</span>
                                    ${badge}
                                    ${!isLeaf ? '<span class="drill-arrow">‹</span>' : ''}
                                </div>
                            </td>
                            <td>
                                <span class="status-pill ${r.isActive ? 'status-active' : 'status-paused'}">
                                    <span class="status-dot"></span>${r.isActive ? 'نشطة' : 'متوقفة'}
                                </span>
                            </td>
                            <td>
                                <div class="metric-main">${r.conv.toLocaleString('en-US')}</div>
                                <div class="metric-sub">${cfg.resultSub}</div>
                            </td>
                            <td>${cpaText}</td>
                            <td>${r.spend.toFixed(2)} ر.س</td>
                            <td>${r.impressions.toLocaleString('en-US')}</td>
                            <td>${r.clicks.toLocaleString('en-US')}</td>
                            <td>${r.ctr.toFixed(1)}%</td>
                        </tr>
                    `;
                }).join('');
            }

            function setPlatform(key) {
                explorerState.platform = key;
                explorerState.level = 'campaigns';
                explorerState.selectedCampaign = null;
                explorerState.selectedGroup = null;
                explorerState.search = '';
                document.getElementById('search-input').value = '';
                renderExplorer();
            }

            function setLevel(key) {
                explorerState.level = key;
                if (key === 'campaigns') { explorerState.selectedCampaign = null; explorerState.selectedGroup = null; }
                if (key === 'adsets') { explorerState.selectedGroup = null; }
                renderExplorer();
            }

            function drillInto(name) {
                if (explorerState.level === 'campaigns') {
                    explorerState.selectedCampaign = name;
                    explorerState.selectedGroup = null;
                    explorerState.level = 'adsets';
                } else if (explorerState.level === 'adsets') {
                    explorerState.selectedGroup = name;
                    explorerState.level = 'ads';
                }
                renderExplorer();
            }

            function clearCampaign() {
                explorerState.selectedCampaign = null;
                explorerState.selectedGroup = null;
                explorerState.level = 'campaigns';
                renderExplorer();
            }

            function clearGroup() {
                explorerState.selectedGroup = null;
                explorerState.level = 'adsets';
                renderExplorer();
            }

            function sortBy(key) {
                if (explorerState.sortKey === key) {
                    explorerState.sortDir *= -1;
                } else {
                    explorerState.sortKey = key;
                    explorerState.sortDir = -1;
                }
                renderExplorer();
            }

            function onSearch(val) {
                explorerState.search = val;
                renderExplorer();
            }

            function notImplementedYet() {
                showToast('هذه الميزة قيد التطوير قريباً ⏳');
            }

            function exportCsv() {
                const cfg = platformCfg(explorerState.platform);
                const rows = getExplorerRows();
                if (rows.length === 0) { showToast('لا توجد بيانات لتصديرها'); return; }

                const headers = ['الاسم', 'الحالة', cfg.resultLabel, 'التكلفة لكل نتيجة', 'الإنفاق', 'الظهور', 'النقرات', 'CTR'];
                const lines = [headers.join(',')];
                rows.forEach(r => {
                    const line = [
                        `"${r.name.replace(/"/g, '""')}"`,
                        r.isActive ? 'نشطة' : 'متوقفة',
                        r.conv,
                        r.cpa !== null ? r.cpa.toFixed(2) : '',
                        r.spend.toFixed(2),
                        r.impressions,
                        r.clicks,
                        r.ctr.toFixed(1)
                    ].join(',');
                    lines.push(line);
                });

                const csvContent = '\\ufeff' + lines.join('\\n');
                const blob = new Blob([csvContent], { type: 'text/csv;charset=utf-8;' });
                const url = URL.createObjectURL(blob);
                const a = document.createElement('a');
                a.href = url;
                a.download = `elevenz-${explorerState.platform}-${explorerState.level}-${dateRangeState.date_from}_to_${dateRangeState.date_to}.csv`;
                document.body.appendChild(a);
                a.click();
                document.body.removeChild(a);
                URL.revokeObjectURL(url);
                showToast('تم تصدير الملف بنجاح! 📁');
            }

            function fmt(n) { return (Math.round(n * 100) / 100).toLocaleString('en-US'); }

            function copyReport() {
                if (!lastMetrics) return;
                const m = lastMetrics;
                const text =
`📊 *تقرير أداء الإعلانات - elevenz*
🗓️ الفترة: ${m.timeText}
⏱️ آخر تحديث: ${m.nowStr}

💰 *إجمالي الإنفاق:* ${fmt(m.totalSpend)} ر.س

📘 *Meta Ads*
• الإنفاق: ${fmt(m.metaSpend)} ر.س
• النتائج: ${m.metaConv.toLocaleString('en-US')}
• CTR: ${m.metaCtr.toFixed(1)}%
• تكلفة/نتيجة: ${m.metaCpa !== null ? fmt(m.metaCpa) + ' ر.س' : '--'}

🎵 *TikTok Ads*
• الإنفاق: ${fmt(m.tiktokSpend)} ر.س
• التحويلات: ${m.tiktokConv.toLocaleString('en-US')}
• CTR: ${m.tiktokCtr.toFixed(1)}%
• تكلفة/نتيجة: ${m.tiktokCpa !== null ? fmt(m.tiktokCpa) + ' ر.س' : '--'}

🔍 *Google Ads*
• الإنفاق: ${fmt(m.googleSpend)} ر.س
• الإحالات: ${m.googleConv.toLocaleString('en-US')}
• CTR: ${m.googleCtr.toFixed(1)}%
• تكلفة/نتيجة: ${m.googleCpa !== null ? fmt(m.googleCpa) + ' ر.س' : '--'}

_تم إنشاء هذا التقرير تلقائياً عبر منصة elevenz_`;

                const finish = () => showToast('تم نسخ التقرير بنجاح! 📋');

                if (navigator.clipboard && navigator.clipboard.writeText) {
                    navigator.clipboard.writeText(text).then(finish).catch(() => fallbackCopy(text, finish));
                } else {
                    fallbackCopy(text, finish);
                }
            }

            function fallbackCopy(text, cb) {
                const ta = document.createElement('textarea');
                ta.value = text;
                ta.style.position = 'fixed';
                ta.style.opacity = '0';
                document.body.appendChild(ta);
                ta.select();
                try { document.execCommand('copy'); } catch (e) {}
                document.body.removeChild(ta);
                cb();
            }

            let toastTimer = null;
            function showToast(msg) {
                const toast = document.getElementById('toast');
                toast.innerText = msg;
                toast.classList.add('show');
                if (toastTimer) clearTimeout(toastTimer);
                toastTimer = setTimeout(() => toast.classList.remove('show'), 2600);
            }

            document.addEventListener('DOMContentLoaded', () => {
                applyDatePreset('yesterday');
            });
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)
