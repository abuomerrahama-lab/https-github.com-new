import os
import asyncio
import logging
from typing import Dict, Any
from fastapi import FastAPI, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse
import httpx

# إعداد التسجيل
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ads-dashboard")

app = FastAPI(title="Advanced Ads Analytics Dashboard")

# الذاكرة المؤقتة (Cache)
CACHE: Dict[str, Any] = {
    "meta_ads": [],
    "tiktok_ads": [],
    "google_ads": [],
    "last_updated": None
}

# الإعدادات والمتغيرات البيئية
WINDSOR_API_KEY = os.getenv("WINDSOR_API_KEY", "")
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "")
REFRESH_INTERVAL_SECONDS = 90

# عناوين حسابات الإعلانات (يمكنك تغييرها أو تركها فارغة للكل)
META_ACCOUNTS = os.getenv("META_ACCOUNTS", "")
TIKTOK_ACCOUNTS = os.getenv("TIKTOK_ACCOUNTS", "")

async def fetch_windsor_connector(connector: str, params: dict) -> list:
    """دالة مساعدة لجلب البيانات من Windsor.ai"""
    if not WINDSOR_API_KEY:
        logger.warning(f"WINDSOR_API_KEY غير معرف لجلب {connector}")
        return []
    
    url = f"https://connectors.windsor.ai/{connector}"
    base_params = {
        "api_key": WINDSOR_API_KEY,
        "_renderer": "json"
    }
    base_params.update(params)
    
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(url, params=base_params)
            if response.status_code == 200:
                res_data = response.json()
                if isinstance(res_data, dict) and "data" in res_data:
                    return res_data["data"]
                elif isinstance(res_data, list):
                    return res_data
            else:
                logger.error(f"خطأ في Windsor API ({connector}): {response.status_code} - {response.text}")
    except Exception as e:
        logger.error(f"استثناء عند طلب {connector}: {str(e)}")
    return []

async def refresh_cache_task():
    """مهمة خلفية لتحديث الذاكرة المؤقتة باستمرار"""
    global CACHE
    while True:
        try:
            logger.info("بدء تحديث البيانات في الخلفية...")
            
            meta_params = {"fields": "campaign,clicks,spend,conversions,impressions,cpc,ctr"}
            if META_ACCOUNTS: meta_params["select_accounts"] = META_ACCOUNTS
            
            tiktok_params = {"fields": "campaign_name,clicks,spend,conversion,impressions,cpc,ctr"}
            if TIKTOK_ACCOUNTS: tiktok_params["select_accounts"] = TIKTOK_ACCOUNTS
            
            google_params = {"fields": "campaign,clicks,spend,conversions,impressions,cpc,ctr"}

            # طلب البيانات بالتوازي
            meta_res, tiktok_res, google_res = await asyncio.gather(
                fetch_windsor_connector("facebook", meta_params),
                fetch_windsor_connector("tiktok", tiktok_params),
                fetch_windsor_connector("google_ads", google_params),
                return_exceptions=True
            )

            CACHE["meta_ads"] = meta_res if isinstance(meta_res, list) else []
            CACHE["tiktok_ads"] = tiktok_res if isinstance(tiktok_res, list) else []
            CACHE["google_ads"] = google_res if isinstance(google_res, list) else []
            CACHE["last_updated"] = asyncio.get_event_loop().time()
            
            logger.info(f"تم تحديث الكاش بنجاح. meta: {len(CACHE['meta_ads'])}, tiktok: {len(CACHE['tiktok_ads'])}, google: {len(CACHE['google_ads'])}")

            # Keep-alive Ping لمنع خمول السيرفر في Render
            if RENDER_EXTERNAL_URL:
                try:
                    async with httpx.AsyncClient(timeout=10.0) as client:
                        await client.get(f"{RENDER_EXTERNAL_URL.rstrip('/')}/api/status")
                except Exception as ping_err:
                    logger.warning(f"فشل Ping منع الخمول: {ping_err}")

        except Exception as e:
            logger.error(f"خطأ أثناء دورة تحديث الكاش: {e}")

        await asyncio.sleep(REFRESH_INTERVAL_SECONDS)

@app.on_event("startup")
async def startup_event():
    """تشغيل المهمة الخلفية فور بدء السيرفر"""
    asyncio.create_task(refresh_cache_task())

@app.get("/api/data")
async def get_dashboard_data():
    """إرجاع البيانات المجهزة فوراً من الكاش"""
    return JSONResponse(content={
        "status": "success",
        "data": {
            "meta_ads": CACHE["meta_ads"],
            "tiktok_ads": CACHE["tiktok_ads"],
            "google_ads": CACHE["google_ads"]
        },
        "last_updated": CACHE["last_updated"]
    })

@app.get("/api/status")
async def get_status():
    """نقطة فحص حالة السيرفر والكاش"""
    last_up = CACHE["last_updated"]
    ago = round(asyncio.get_event_loop().time() - last_up, 1) if last_up else None
    return {
        "cache_ready": last_up is not None,
        "last_update_seconds_ago": ago,
        "refresh_interval_seconds": REFRESH_INTERVAL_SECONDS,
        "rows": {
            "meta_ads": len(CACHE["meta_ads"]),
            "tiktok_ads": len(CACHE["tiktok_ads"]),
            "google_ads": len(CACHE["google_ads"])
        }
    }

@app.get("/", response_class=HTMLResponse)
async def serve_index():
    """صفحة الواجهة الرئيسية للوحة التحليلات"""
    html_content = """
    <!DOCTYPE html>
    <html lang="ar" dir="rtl">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>لوحة تحليلات الإعلانات المتقدمة</title>
        <style>
            :root {
                --bg-main: #0f172a;
                --bg-card: #1e293b;
                --text-main: #f8fafc;
                --text-muted: #94a3b8;
                --accent-blue: #3b82f6;
                --accent-green: #22c55e;
                --accent-yellow: #eab308;
                --border-color: #334155;
            }
            body {
                font-family: system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                background-color: var(--bg-main);
                color: var(--text-main);
                margin: 0;
                padding: 24px;
                direction: rtl;
            }
            .header {
                display: flex;
                justify-content: space-between;
                align-items: center;
                margin-bottom: 24px;
            }
            .title-area h1 { margin: 0 0 8px 0; font-size: 24px; }
            .title-area p { margin: 0; color: var(--text-muted); font-size: 14px; }
            .btn {
                background-color: #2563eb;
                color: white;
                border: none;
                padding: 10px 18px;
                border-radius: 8px;
                cursor: pointer;
                font-weight: bold;
                transition: background 0.2s;
            }
            .btn:hover { background-color: #1d4ed8; }
            .btn-copy { background-color: #059669; margin-left: 8px; }
            .btn-copy:hover { background-color: #047857; }
            .cards-grid {
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
                gap: 16px;
                margin-bottom: 32px;
            }
            .card {
                background-color: var(--bg-card);
                border: 1px solid var(--border-color);
                border-radius: 12px;
                padding: 20px;
            }
            .card-title { font-size: 14px; color: var(--text-muted); margin-bottom: 8px; }
            .card-value { font-size: 24px; font-weight: bold; }
            .card-sub { font-size: 12px; color: var(--text-muted); margin-top: 6px; }
            .section {
                background-color: var(--bg-card);
                border: 1px solid var(--border-color);
                border-radius: 12px;
                padding: 20px;
                margin-bottom: 24px;
            }
            .section-title { font-size: 18px; margin-top: 0; margin-bottom: 16px; }
            table {
                width: 100%;
                border-collapse: collapse;
                text-align: right;
                font-size: 14px;
            }
            th, td {
                padding: 12px;
                border-bottom: 1px solid var(--border-color);
            }
            th { color: var(--text-muted); font-weight: 600; }
            .badge {
                padding: 4px 8px;
                border-radius: 6px;
                font-size: 12px;
                font-weight: bold;
            }
            .badge-success { background: #15803d; color: #dcfce7; }
            .badge-warning { background: #a16207; color: #fef9c3; }
            .badge-danger { background: #b91c1c; color: #fee2e2; }
        </style>
    </head>
    <body>
        <div class="header">
            <div class="title-area">
                <h1>لوحة تحليلات الإعلانات المتقدمة 🚀</h1>
                <p id="status-text">جاري تحميل أحدث نسخة محفوطة...</p>
            </div>
            <div>
                <button class="btn btn-copy" onclick="copyReportForAI()">نسخ التقرير للذكاء الاصطناعي 📋</button>
                <button class="btn" onclick="fetchData()">تحديث سريع 🔄</button>
            </div>
        </div>

        <div class="cards-grid">
            <div class="card">
                <div class="card-title">Meta Ads</div>
                <div class="card-value" id="meta-spend">0.00 ر.س</div>
                <div class="card-sub" id="meta-sub">0 نتائج | CPC: 0.00</div>
            </div>
            <div class="card">
                <div class="card-title">TikTok Ads</div>
                <div class="card-value" id="tiktok-spend">0.00 ر.س</div>
                <div class="card-sub" id="tiktok-sub">0 نتائج | CPC: 0.00</div>
            </div>
            <div class="card">
                <div class="card-title">Google Ads</div>
                <div class="card-value" id="google-spend">0.00 ر.س</div>
                <div class="card-sub" id="google-sub">0 نتائج | CPC: 0.00</div>
            </div>
            <div class="card" style="border-color: var(--accent-yellow);">
                <div class="card-title">إجمالي الإنفاق الكلي</div>
                <div class="card-value" id="total-spend">0.00 ر.س</div>
                <div class="card-sub" id="total-sub">إجمالي النتائج: 0</div>
            </div>
        </div>

        <div class="section">
            <h2 class="section-title">تفاصيل الأداء والحملات (Meta / TikTok / Google)</h2>
            <table>
                <thead>
                    <tr>
                        <th>المنصة</th>
                        <th>الحملة / الإعلان</th>
                        <th>الإنفاق</th>
                        <th>الظهور</th>
                        <th>النقرات</th>
                        <th>معدل النقرة (CTR)</th>
                        <th>النتائج / التحويلات</th>
                        <th>التقييم</th>
                    </tr>
                </thead>
                <tbody id="campaigns-table">
                    <tr><td colspan="8" style="text-align:center;">جاري القراءة والتحميل...</td></tr>
                </tbody>
            </table>
        </div>

        <script>
            // تحويل آمن للأرقام لتجنب أي توقف بالواجهة
            function safeNum(val) {
                if (val === null || val === undefined || val === '') return 0;
                let n = parseFloat(val);
                return isNaN(n) ? 0 : n;
            }

            function evaluateCampaign(ctr, cpc) {
                if (ctr > 1.5) return '<span class="badge badge-success">ممتاز</span>';
                if (ctr >= 0.8) return '<span class="badge badge-warning">متوسط</span>';
                return '<span class="badge badge-danger">يحتاج تحسين</span>';
            }

            async function fetchData() {
                document.getElementById('status-text').innerText = 'جاري التحديث من السيرفر...';
                try {
                    const response = await fetch('/api/data');
                    if (!response.ok) throw new Error('فشل جلب البيانات من السيرفر');
                    
                    const result = await response.json();
                    const data = result.data || {};
                    
                    renderData(data);
                    document.getElementById('status-text').innerText = 'تم التحديث بنجاح (' + new Date().toLocaleTimeString('ar-SA') + ')';
                    localStorage.setItem('cached_ads_data', JSON.stringify(data));
                } catch (err) {
                    console.error("خطأ:", err);
                    document.getElementById('status-text').innerText = 'خطأ أثناء الجلب، تم استخدام النسخة المخزنة محلياً.';
                    const cached = localStorage.getItem('cached_ads_data');
                    if (cached) renderData(JSON.parse(cached));
                }
            }

            function renderData(data) {
                let metaSpend = 0, metaClicks = 0, metaConv = 0;
                let tiktokSpend = 0, tiktokClicks = 0, tiktokConv = 0;
                let googleSpend = 0, googleClicks = 0, googleConv = 0;

                let tableRowsHtml = '';

                // معالجة بيانات Meta
                (data.meta_ads || []).forEach(item => {
                    let spend = safeNum(item.spend || item.cost);
                    let clicks = safeNum(item.clicks);
                    let conv = safeNum(item.conversions || item.results || item.messaging_conversations_started);
                    let impressions = safeNum(item.impressions);
                    let ctr = safeNum(item.ctr) || (impressions > 0 ? (clicks / impressions * 100) : 0);

                    metaSpend += spend;
                    metaClicks += clicks;
                    metaConv += conv;

                    tableRowsHtml += `
                        <tr>
                            <td><strong>Meta</strong></td>
                            <td>${item.campaign || 'حملة بدون اسم'}</td>
                            <td>${spend.toFixed(2)} ر.س</td>
                            <td>${impressions}</td>
                            <td>${clicks}</td>
                            <td>${ctr.toFixed(2)}%</td>
                            <td>${conv}</td>
                            <td>${evaluateCampaign(ctr)}</td>
                        </tr>
                    `;
                });

                // معالجة بيانات TikTok
                (data.tiktok_ads || []).forEach(item => {
                    let spend = safeNum(item.spend || item.cost);
                    let clicks = safeNum(item.clicks);
                    let conv = safeNum(item.conversion || item.conversions);
                    let impressions = safeNum(item.impressions);
                    let ctr = safeNum(item.ctr) || (impressions > 0 ? (clicks / impressions * 100) : 0);

                    tiktokSpend += spend;
                    tiktokClicks += clicks;
                    tiktokConv += conv;

                    tableRowsHtml += `
                        <tr>
                            <td><strong>TikTok</strong></td>
                            <td>${item.campaign_name || item.campaign || 'حملة بدون اسم'}</td>
                            <td>${spend.toFixed(2)} ر.س</td>
                            <td>${impressions}</td>
                            <td>${clicks}</td>
                            <td>${ctr.toFixed(2)}%</td>
                            <td>${conv}</td>
                            <td>${evaluateCampaign(ctr)}</td>
                        </tr>
                    `;
                });

                // معالجة بيانات Google
                (data.google_ads || []).forEach(item => {
                    let spend = safeNum(item.spend || item.cost);
                    let clicks = safeNum(item.clicks);
                    let conv = safeNum(item.conversions || item.all_conversions);
                    let impressions = safeNum(item.impressions);
                    let ctr = safeNum(item.ctr) || (impressions > 0 ? (clicks / impressions * 100) : 0);

                    googleSpend += spend;
                    googleClicks += clicks;
                    googleConv += conv;

                    tableRowsHtml += `
                        <tr>
                            <td><strong>Google</strong></td>
                            <td>${item.campaign || 'حملة بدون اسم'}</td>
                            <td>${spend.toFixed(2)} ر.س</td>
                            <td>${impressions}</td>
                            <td>${clicks}</td>
                            <td>${ctr.toFixed(2)}%</td>
                            <td>${conv}</td>
                            <td>${evaluateCampaign(ctr)}</td>
                        </tr>
                    `;
                });

                // تحديث الكروت العلوية
                document.getElementById('meta-spend').innerText = metaSpend.toFixed(2) + ' ر.س';
                document.getElementById('meta-sub').innerText = `${metaConv} نتائج | CPC: ${(metaClicks > 0 ? metaSpend / metaClicks : 0).toFixed(2)}`;

                document.getElementById('tiktok-spend').innerText = tiktokSpend.toFixed(2) + ' ر.س';
                document.getElementById('tiktok-sub').innerText = `${tiktokConv} نتائج | CPC: ${(tiktokClicks > 0 ? tiktokSpend / tiktokClicks : 0).toFixed(2)}`;

                document.getElementById('google-spend').innerText = googleSpend.toFixed(2) + ' ر.س';
                document.getElementById('google-sub').innerText = `${googleConv} نتائج | CPC: ${(googleClicks > 0 ? googleSpend / googleClicks : 0).toFixed(2)}`;

                let totalSpend = metaSpend + tiktokSpend + googleSpend;
                let totalConv = metaConv + tiktokConv + googleConv;
                document.getElementById('total-spend').innerText = totalSpend.toFixed(2) + ' ر.س';
                document.getElementById('total-sub').innerText = `إجمالي النتائج: ${totalConv}`;

                // تحديث الجدول
                document.getElementById('campaigns-table').innerHTML = tableRowsHtml || '<tr><td colspan="8" style="text-align:center;">لا توجد بيانات متاحة حالياً</td></tr>';
            }

            function copyReportForAI() {
                const cached = localStorage.getItem('cached_ads_data');
                if (!cached) return alert('لا توجد بيانات للنسخ');
                
                const reportText = "تقرير الإعلانات المكتمل للتحليل:\n" + cached;
                navigator.clipboard.writeText(reportText).then(() => {
                    alert('تم نسخ التقرير بنجاح! يمكنك لصقه لطلب التقييم من الذكاء الاصطناعي.');
                });
            }

            // البدء بالتحميل المباشر عند الفتح
            document.addEventListener('DOMContentLoaded', () => {
                const cached = localStorage.getItem('cached_ads_data');
                if (cached) renderData(JSON.parse(cached));
                fetchData();
            });
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)
