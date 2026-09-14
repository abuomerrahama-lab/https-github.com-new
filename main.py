import os
import asyncio
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Dict, Any
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, Depends, HTTPException, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.middleware.cors import CORSMiddleware
import httpx

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ads-dashboard")

# ===== تحديد معدل الطلبات (اختياري بأمان) =====
try:
    from slowapi import Limiter, _rate_limit_exceeded_handler
    from slowapi.util import get_remote_address
    from slowapi.errors import RateLimitExceeded
    SLOWAPI_AVAILABLE = True
except ImportError:
    SLOWAPI_AVAILABLE = False

    class _NoOpLimiter:
        """بديل آمن عند غياب slowapi: `@limiter.limit(...)` يُصبح ديكوراتور
        شفّاف لا يفعل شيئاً (لا يحدّ من شيء)، بدل أن يُسقط التطبيق بالكامل."""
        def limit(self, *args, **kwargs):
            def decorator(func):
                return func
            return decorator

    Limiter = None
    get_remote_address = None
    RateLimitExceeded = Exception
    _rate_limit_exceeded_handler = None

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

# ===== حماية الوصول (Basic Auth) =====
DASHBOARD_USERNAME = os.getenv("DASHBOARD_USERNAME", "")
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "")
AUTH_ENABLED = bool(DASHBOARD_USERNAME and DASHBOARD_PASSWORD)

if not AUTH_ENABLED:
    logger.warning(
        "⚠️ الحماية بكلمة مرور معطّلة: لم يتم ضبط DASHBOARD_USERNAME/DASHBOARD_PASSWORD "
        "في متغيرات البيئة على Render. اللوحة متاحة للجميع دون تسجيل دخول حالياً."
    )

_security = HTTPBasic(auto_error=False)


async def verify_dashboard_auth(credentials: HTTPBasicCredentials = Depends(_security)):
    if not AUTH_ENABLED:
        return True
    if credentials is None or not (
        secrets.compare_digest(credentials.username, DASHBOARD_USERNAME)
        and secrets.compare_digest(credentials.password, DASHBOARD_PASSWORD)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="بيانات الدخول مطلوبة أو غير صحيحة",
            headers={"WWW-Authenticate": "Basic"},
        )
    return True


# ===== النطاقات المسموح لها بطلب الـ API عبر CORS =====
_allowed_origins = ["http://localhost:8000", "http://127.0.0.1:8000"]
if RENDER_EXTERNAL_URL:
    _allowed_origins.append(RENDER_EXTERNAL_URL.rstrip("/"))

# ===== تحديد معدل الطلبات (Rate Limiting) =====
if SLOWAPI_AVAILABLE:
    limiter = Limiter(key_func=get_remote_address)
else:
    logger.warning(
        "⚠️ مكتبة slowapi غير مثبّتة - تحديد معدل الطلبات معطّل مؤقتاً."
    )
    limiter = _NoOpLimiter()

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
                logger.error(f"فشل طلب Windsor لموصل '{connector}' - رمز الحالة: {res.status_code}")
                return []

            try:
                res_data = res.json()
            except Exception as parse_err:
                logger.error(f"تعذر تحليل JSON من موصل '{connector}': {parse_err}")
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


CONNECTOR_STATUS_FIELD_TIERS = {
    "facebook": ["campaign_status,adset_status,effective_status", ""],
    "tiktok": ["campaign_operation_status,ad_group_operation_status,ad_operation_status,campaign_status,adgroup_status,ad_status", ""],
    "google_ads": ["campaign_status,ad_group_status,ad_group_ad_status", ""],
}


async def fetch_platform_data(connector: str, date_from: str, date_to: str) -> list:
    base_fields = CONNECTOR_FIELDS[connector]
    tiers = CONNECTOR_STATUS_FIELD_TIERS.get(connector, [""])

    for tier_fields in tiers:
        combined_fields = f"{base_fields},{tier_fields}" if tier_fields else base_fields
        rows = await fetch_windsor_connector(connector, {
            "fields": combined_fields, "date_from": date_from, "date_to": date_to
        })
        if rows:
            return rows
    return []


async def refresh_cache_and_keep_alive():
    global CACHE
    while True:
        try:
            date_from, date_to = get_default_date_range()
            logger.info(f"جاري تحديث بيانات إعلانات elevenz... (النطاق الزمني: {date_from} إلى {date_to})")

            meta_res, tiktok_res, google_res = await asyncio.gather(
                fetch_platform_data("facebook", date_from, date_to),
                fetch_platform_data("tiktok", date_from, date_to),
                fetch_platform_data("google_ads", date_from, date_to),
                return_exceptions=True
            )

            new_cache = {
                "meta_ads": meta_res if isinstance(meta_res, list) else [],
                "tiktok_ads": tiktok_res if isinstance(tiktok_res, list) else [],
                "google_ads": google_res if isinstance(google_res, list) else [],
                "last_updated": asyncio.get_event_loop().time(),
            }
            CACHE = new_cache

            target_url = RENDER_EXTERNAL_URL.rstrip('/') if RENDER_EXTERNAL_URL else "http://127.0.0.1:8000"
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    await client.get(f"{target_url}/api/status")
            except Exception:
                pass

        except Exception as e:
            logger.error(f"Cache error: {e}")
            
        await asyncio.sleep(REFRESH_INTERVAL_SECONDS)

@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(refresh_cache_and_keep_alive())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

app = FastAPI(title="منصة إعلانات elevenz", lifespan=lifespan)

app.state.limiter = limiter
if SLOWAPI_AVAILABLE:
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=True,
    allow_methods=["GET"],
    allow_headers=["*"],
)


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data:; "
        "connect-src 'self';"
    )
    return response


@app.get("/api/status")
async def get_status():
    return {"status": "ok"}

@app.get("/api/debug/windsor", dependencies=[Depends(verify_dashboard_auth)])
@limiter.limit("10/minute")
async def debug_windsor(request: Request, connector: str = "facebook", include_status: bool = False):
    if connector not in CONNECTOR_FIELDS:
        return JSONResponse(content={"error": f"موصل غير معروف: '{connector}'"}, status_code=400)

    if not WINDSOR_API_KEY:
        return JSONResponse(content={"error": "WINDSOR_API_KEY غير مضبوط."})

    date_from, date_to = get_default_date_range()
    url = f"https://connectors.windsor.ai/{connector}"
    base_fields = CONNECTOR_FIELDS[connector]

    async def try_fields(fields: str):
        params = {"api_key": WINDSOR_API_KEY, "_renderer": "json", "date_from": date_from, "date_to": date_to, "fields": fields}
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                res = await client.get(url, params=params)
            parsed = res.json() if res.status_code == 200 else None
            row_count = len(parsed["data"]) if isinstance(parsed, dict) and isinstance(parsed.get("data"), list) else (len(parsed) if isinstance(parsed, list) else 0)
            return {"fields_requested": fields, "status_code": res.status_code, "row_count": row_count, "raw_body_preview": res.text[:1500]}
        except Exception as e:
            return {"fields_requested": fields, "error": str(e)}

    result = await try_fields(base_fields)
    return JSONResponse(content={"connector": connector, "date_from": date_from, "date_to": date_to, **result})

def _valid_iso_date(value: str) -> bool:
    try:
        datetime.strptime(value, "%Y-%m-%d")
        return True
    except (ValueError, TypeError):
        return False


@app.get("/api/data", dependencies=[Depends(verify_dashboard_auth)])
@limiter.limit("15/minute")
async def get_dashboard_data(request: Request, date_from: str = None, date_to: str = None):
    if not date_from and not date_to:
        return JSONResponse(content={"status": "success", "data": CACHE, "last_updated": CACHE["last_updated"]})

    if not date_from or not date_to or not _valid_iso_date(date_from) or not _valid_iso_date(date_to) or date_from > date_to:
        return JSONResponse(content={"status": "error", "message": "نطاق تاريخ غير صحيح."}, status_code=400)

    meta_res, tiktok_res, google_res = await asyncio.gather(
        fetch_platform_data("facebook", date_from, date_to),
        fetch_platform_data("tiktok", date_from, date_to),
        fetch_platform_data("google_ads", date_from, date_to),
        return_exceptions=True
    )

    fresh_data = {
        "meta_ads": meta_res if isinstance(meta_res, list) else [],
        "tiktok_ads": tiktok_res if isinstance(tiktok_res, list) else [],
        "google_ads": google_res if isinstance(google_res, list) else [],
        "last_updated": asyncio.get_event_loop().time(),
    }

    return JSONResponse(content={"status": "success", "data": fresh_data, "last_updated": fresh_data["last_updated"], "range": {"date_from": date_from, "date_to": date_to}})

@app.get("/", response_class=HTMLResponse, dependencies=[Depends(verify_dashboard_auth)])
@limiter.limit("30/minute")
async def serve_index(request: Request):
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
                padding: 10px 24px 22px 24px;
                border-bottom: 1px solid rgba(255,255,255,0.08);
                display: flex;
                align-items: center;
                justify-content: flex-start;
            }
            .sidebar-logo svg {
                max-width: 170px;
                height: auto;
                display: block;
            }
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
            .card-value { font-size: 27px; font-weight: 800; color: var(--text-dark); margin-bottom: 6px; letter-spacing: -0.5px; }
            .card-sub { font-size: 12.5px; font-weight: 700; color: var(--text-muted); }

            .grid-2 { display: grid; grid-template-columns: 2fr 1fr; gap: 18px; margin-bottom: 22px; align-items: stretch; }
            .chart-section {
                background-color: var(--card-bg);
                border-radius: var(--radius-lg);
                box-shadow: var(--shadow-card);
                padding: 24px;
            }
            .chart-section-title { font-weight: 800; margin-bottom: 4px; color: var(--text-dark); font-size: 15px; }
            .chart-container { position: relative; height: 260px; width: 100%; }

            .accounts-table {
                width: 100%;
                border-collapse: separate;
                border-spacing: 0;
            }
            .accounts-table th {
                background-color: #f8fafc;
                padding: 12px 16px;
                font-size: 12px;
                font-weight: 700;
                color: var(--text-muted);
                text-align: right;
                border-bottom: 1px solid var(--border-color);
            }
            .accounts-table td {
                padding: 14px 16px;
                font-size: 13.5px;
                border-bottom: 1px solid var(--border-color);
            }
        </style>
    </head>
    <body>
        <div class="sidebar">
            <div class="sidebar-logo">
                <!-- شعار elevenz الداكن الرسمي (SVG) -->
                <svg viewBox="0 0 450 120" xmlns="http://www.w3.org/2000/svg" fill="none">
                    <path d="M42 35 C42 22 53 12 66 12 L100 12 C113 12 124 22 124 35 L124 85 C124 98 113 108 100 108 L66 108 C53 108 42 98 42 85 Z" fill="#F05A28"/>
                    <path d="M68 38 H98 V48 H68 Z M68 55 H98 V65 H68 Z M68 72 H98 V82 H68 Z" fill="#FFFFFF"/>
                    <text x="145" y="82" font-family="'Cairo', sans-serif" font-weight="900" font-size="68" fill="#FFFFFF" letter-spacing="-1.5">elevenz</text>
                </svg>
            </div>
            <ul class="sidebar-menu">
                <li class="active"><a href="#">📊 لوحة الأداء العامة</a></li>
                <li><a href="#">🎯 أداء الحملات</a></li>
                <li><a href="#">📈 التحليلات والتقارير</a></li>
            </ul>
            <div class="user-profile">
                <div class="user-avatar">E</div>
                <div style="font-size: 13px; font-weight: 700;">فريق إيليفنز</div>
            </div>
        </div>

        <div class="main-content">
            <div class="top-bar">
                <div class="header-title">
                    <h1>لوحة إعلانات elevenz</h1>
                    <p>متابعة وتجميع نتائج الحملات الإعلانية المباشرة عبر المنصات</p>
                </div>
                <div class="top-actions">
                    <button class="btn btn-refresh" onclick="fetchData()">تحديث البيانات</button>
                </div>
            </div>

            <div class="time-selector">
                <button class="time-btn active">آخر 30 يوم</button>
            </div>

            <div class="cards-container">
                <div class="card">
                    <div class="card-top"><span class="card-title">إجمالي الإنفاق</span></div>
                    <div class="card-value" id="total-spend">$0</div>
                    <div class="card-sub">جميع المنصات</div>
                </div>
                <div class="card">
                    <div class="card-top"><span class="card-title">إجمالي النقرات</span></div>
                    <div class="card-value" id="total-clicks">0</div>
                    <div class="card-sub">جميع الحملات</div>
                </div>
                <div class="card">
                    <div class="card-top"><span class="card-title">إجمالي التحويلات</span></div>
                    <div class="card-value" id="total-conv">0</div>
                    <div class="card-sub">Conversions</div>
                </div>
                <div class="card">
                    <div class="card-top"><span class="card-title">إجمالي الظهور</span></div>
                    <div class="card-value" id="total-impressions">0</div>
                    <div class="card-sub">Impressions</div>
                </div>
            </div>

            <div class="grid-2">
                <div class="chart-section">
                    <div class="chart-section-title">توزيع الإنفاق حسب المنصة</div>
                    <div class="chart-container">
                        <canvas id="spendChart"></canvas>
                    </div>
                </div>
                <div class="chart-section">
                    <div class="chart-section-title">ملخص المنصات</div>
                    <div id="platform-summary" style="margin-top: 15px;"></div>
                </div>
            </div>
        </div>

        <script>
            let spendChart = null;

            async function fetchData() {
                try {
                    const response = await fetch('/api/data');
                    const result = await response.json();
                    if(result.status === 'success') {
                        renderDashboard(result.data);
                    }
                } catch(e) {
                    console.error("Error fetching data:", e);
                }
            }

            function renderDashboard(data) {
                let totalSpend = 0, totalClicks = 0, totalConv = 0, totalImp = 0;
                
                ['meta_ads', 'tiktok_ads', 'google_ads'].forEach(platform => {
                    (data[platform] || []).forEach(row => {
                        totalSpend += parseFloat(row.spend || 0);
                        totalClicks += parseInt(row.clicks || 0);
                        totalConv += parseFloat(row.conversions || row.conversion || 0);
                        totalImp += parseInt(row.impressions || 0);
                    });
                });

                document.getElementById('total-spend').innerText = '$' + totalSpend.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2});
                document.getElementById('total-clicks').innerText = totalClicks.toLocaleString();
                document.getElementById('total-conv').innerText = totalConv.toLocaleString();
                document.getElementById('total-impressions').innerText = totalImp.toLocaleString();

                initChart(data);
            }

            function initChart(data) {
                const ctx = document.getElementById('spendChart').getContext('2d');
                
                let metaSpend = (data.meta_ads || []).reduce((a, b) => a + parseFloat(b.spend || 0), 0);
                let tiktokSpend = (data.tiktok_ads || []).reduce((a, b) => a + parseFloat(b.spend || 0), 0);
                let googleSpend = (data.google_ads || []).reduce((a, b) => a + parseFloat(b.spend || 0), 0);

                if(spendChart) spendChart.destroy();

                spendChart = new Chart(ctx, {
                    type: 'bar',
                    data: {
                        labels: ['Meta Ads', 'TikTok Ads', 'Google Ads'],
                        datasets: [{
                            label: 'الإنفاق ($)',
                            data: [metaSpend, tiktokSpend, googleSpend],
                            backgroundColor: ['#1877F2', '#000000', '#4285F4'],
                            borderRadius: 8
                        }]
                    },
                    options: {
                        responsive: true,
                        maintainAspectRatio: false,
                        plugins: { legend: { display: false } }
                    }
                });
            }

            window.onload = fetchData;
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)
