import os
import asyncio
import logging
from typing import Dict, Any
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
import httpx

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ads-dashboard")

app = FastAPI(title="التقرير اليومي للإعلانات")

CACHE: Dict[str, Any] = {
    "meta_ads": [],
    "tiktok_ads": [],
    "google_ads": [],
    "last_updated": None
}

WINDSOR_API_KEY = os.getenv("WINDSOR_API_KEY", "")
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "")
REFRESH_INTERVAL_SECONDS = 90

async def fetch_windsor_connector(connector: str, params: dict) -> list:
    if not WINDSOR_API_KEY:
        return []
    url = f"https://connectors.windsor.ai/{connector}"
    base_params = {"api_key": WINDSOR_API_KEY, "_renderer": "json"}
    base_params.update(params)
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            res = await client.get(url, params=base_params)
            if res.status_code == 200:
                res_data = res.json()
                if isinstance(res_data, dict) and "data" in res_data:
                    return res_data["data"]
                elif isinstance(res_data, list):
                    return res_data
    except Exception as e:
        logger.error(f"Error fetching {connector}: {e}")
    return []

# 🚀 مهمة تنشيط السيرفر وتحديث الذاكرة المؤقتة (Keep-Alive Task)
async def refresh_cache_and_keep_alive():
    global CACHE
    while True:
        try:
            logger.info("جاري تحديث بيانات الإعلانات...")
            meta_res, tiktok_res, google_res = await asyncio.gather(
                fetch_windsor_connector("facebook", {"fields": "account_name,campaign,clicks,spend,conversions,impressions,cpc,ctr,date"}),
                fetch_windsor_connector("tiktok", {"fields": "account_name,campaign_name,clicks,spend,conversion,impressions,cpc,ctr,date"}),
                fetch_windsor_connector("google_ads", {"fields": "account_name,campaign,clicks,spend,conversions,impressions,cpc,ctr,date"}),
                return_exceptions=True
            )

            CACHE["meta_ads"] = meta_res if isinstance(meta_res, list) else []
            CACHE["tiktok_ads"] = tiktok_res if isinstance(tiktok_res, list) else []
            CACHE["google_ads"] = google_res if isinstance(google_res, list) else []
            CACHE["last_updated"] = asyncio.get_event_loop().time()
            
            # 🔄 إجراء التنشيط المباشر (Self-Ping) لمنع الخمول على Render
            target_url = RENDER_EXTERNAL_URL.rstrip('/') if RENDER_EXTERNAL_URL else "http://127.0.0.1:8000"
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    await client.get(f"{target_url}/api/status")
                    logger.info("تم تنشيط السيرفر بنجاح للحفاظ على الجاهزية.")
            except Exception as ping_err:
                logger.warning(f"Self-ping note: {ping_err}")

        except Exception as e:
            logger.error(f"Cache error: {e}")
            
        await asyncio.sleep(REFRESH_INTERVAL_SECONDS)

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(refresh_cache_and_keep_alive())

@app.get("/api/data")
async def get_dashboard_data():
    return JSONResponse(content={
        "status": "success",
        "data": CACHE,
        "last_updated": CACHE["last_updated"]
    })

@app.get("/api/status")
async def get_status():
    last_up = CACHE["last_updated"]
    ago = round(asyncio.get_event_loop().time() - last_up, 1) if last_up else None
    return {
        "cache_ready": last_up is not None,
        "last_update_seconds_ago": ago,
        "rows": {
            "meta_ads": len(CACHE["meta_ads"]),
            "tiktok_ads": len(CACHE["tiktok_ads"]),
            "google_ads": len(CACHE["google_ads"])
        }
    }

@app.get("/", response_class=HTMLResponse)
async def serve_index():
    html_content = """
    <!DOCTYPE html>
    <html lang="ar" dir="rtl">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>التقرير اليومي للإعلانات</title>
        <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
        <style>
            :root {
                --bg-main: #0b0f17;
                --bg-card: #151c28;
                --bg-card-hover: #1b2434;
                --text-main: #f8fafc;
                --text-muted: #94a3b8;
                --accent-gold: #f59e0b;
                --accent-blue: #3b82f6;
                --accent-pink: #ec4899;
                --accent-green: #10b981;
                --border-color: #232d3f;
            }
            body {
                font-family: system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                background-color: var(--bg-main);
                color: var(--text-main);
                margin: 0;
                padding: 30px;
                direction: rtl;
            }
            .top-bar {
                display: flex;
                justify-content: space-between;
                align-items: center;
                margin-bottom: 25px;
            }
            .header-title h1 { margin: 0; font-size: 26px; font-weight: 800; }
            .header-title p { margin: 4px 0 0 0; color: var(--text-muted); font-size: 13px; }
            .btn-refresh {
                background-color: var(--accent-gold);
                color: #000;
                font-weight: bold;
                border: none;
                padding: 10px 20px;
                border-radius: 8px;
                cursor: pointer;
                transition: opacity 0.2s;
            }
            .btn-refresh:hover { opacity: 0.9; }

            .cards-container {
                display: grid;
                grid-template-columns: repeat(4, 1fr);
                gap: 16px;
                margin-bottom: 25px;
            }
            .card {
                background-color: var(--bg-card);
                border: 1px solid var(--border-color);
                border-radius: 12px;
                padding: 20px;
                text-align: center;
            }
            .card-title { font-size: 13px; color: var(--text-muted); margin-bottom: 10px; }
            .card-value { font-size: 28px; font-weight: 800; margin-bottom: 4px; }
            .card-sub { font-size: 12px; color: var(--text-muted); }
            
            .card-highlight {
                border-color: var(--accent-gold);
            }

            .chart-section {
                background-color: var(--bg-card);
                border: 1px solid var(--border-color);
                border-radius: 12px;
                padding: 24px;
                margin-bottom: 25px;
            }
            .chart-title {
                text-align: center;
                color: var(--text-muted);
                font-size: 15px;
                margin-bottom: 20px;
            }
            .chart-wrapper {
                height: 320px;
                position: relative;
            }

            .filters-bar {
                display: flex;
                gap: 10px;
                margin-bottom: 20px;
            }
            .filter-btn {
                background-color: var(--bg-card);
                border: 1px solid var(--border-color);
                color: var(--text-muted);
                padding: 8px 20px;
                border-radius: 20px;
                cursor: pointer;
                font-size: 13px;
                font-weight: 600;
            }
            .filter-btn.active {
                background-color: var(--accent-gold);
                color: #000;
                border-color: var(--accent-gold);
            }

            .table-container {
                background-color: var(--bg-card);
                border: 1px solid var(--border-color);
                border-radius: 12px;
                overflow: hidden;
            }
            .table-header {
                padding: 16px 20px;
                font-weight: bold;
                border-bottom: 1px solid var(--border-color);
                display: flex;
                justify-content: space-between;
                align-items: center;
            }
            .dot {
                height: 8px;
                width: 8px;
                background-color: var(--accent-blue);
                border-radius: 50%;
                display: inline-block;
                margin-right: 6px;
            }
            table {
                width: 100%;
                border-collapse: collapse;
                text-align: right;
                font-size: 14px;
            }
            th, td {
                padding: 12px 20px;
                border-bottom: 1px solid var(--border-color);
            }
            th { color: var(--text-muted); font-weight: 600; background-color: rgba(0,0,0,0.1); }
        </style>
    </head>
    <body>
        <div class="top-bar">
            <button class="btn-refresh" onclick="fetchData()">تحديث البيانات</button>
            <div class="header-title" style="text-align: left;">
                <h1>التقرير اليومي للإعلانات</h1>
                <p id="update-time">تقرير أداء يوم -- | آخر تحديث: --</p>
            </div>
        </div>

        <div class="cards-container">
            <div class="card card-highlight">
                <div class="card-title">إجمالي الإنفاق (كل المنصات)</div>
                <div class="card-value" id="total-spend">0.00 ر.س</div>
                <div class="card-sub" id="total-sub">الحسابات النشطة</div>
            </div>
            <div class="card">
                <div class="card-title">Google Ads - إجمالي الإنفاق</div>
                <div class="card-value" id="google-spend">0.00 ر.س</div>
                <div class="card-sub" id="google-sub">0 تحويلات</div>
            </div>
            <div class="card">
                <div class="card-title">TikTok - إجمالي الإنفاق</div>
                <div class="card-value" id="tiktok-spend">0.00 ر.س</div>
                <div class="card-sub" id="tiktok-sub">0 تحويل</div>
            </div>
            <div class="card">
                <div class="card-title">Meta - إجمالي الإنفاق</div>
                <div class="card-value" id="meta-spend">0.00 ر.س</div>
                <div class="card-sub" id="meta-sub">0 محادثة/نتيجة</div>
            </div>
        </div>

        <div class="chart-section">
            <div class="chart-title">الإنفاق حسب الحساب - أمس</div>
            <div class="chart-wrapper">
                <canvas id="spendChart"></canvas>
            </div>
        </div>

        <div class="filters-bar">
            <button class="filter-btn active" id="btn-all" onclick="setFilter('all', this)">الكل</button>
            <button class="filter-btn" id="btn-meta" onclick="setFilter('meta', this)">Meta</button>
            <button class="filter-btn" id="btn-tiktok" onclick="setFilter('tiktok', this)">TikTok</button>
            <button class="filter-btn" id="btn-google" onclick="setFilter('google', this)">Google Ads</button>
        </div>

        <div class="table-container">
            <div class="table-header">
                <span id="table-title">Meta Ads <span class="dot"></span></span>
            </div>
            <table>
                <thead>
                    <tr>
                        <th>المنصة</th>
                        <th>الحساب / الحملة</th>
                        <th>الإنفاق</th>
                        <th>الظهور</th>
                        <th>النقرات</th>
                        <th>النتائج</th>
                    </tr>
                </thead>
                <tbody id="campaigns-tbody">
                    <tr><td colspan="6" style="text-align:center;">جاري تحميل البيانات...</td></tr>
                </tbody>
            </table>
        </div>

        <script>
            let chartInstance = null;
            let currentFilter = 'all';
            let globalData = {};

            function safeNum(val) {
                if (!val) return 0;
                let n = parseFloat(val);
                return isNaN(n) ? 0 : n;
            }

            function initChart(metaSpend, tiktokSpend, googleSpend) {
                const ctx = document.getElementById('spendChart').getContext('2d');
                if (chartInstance) chartInstance.destroy();

                chartInstance = new Chart(ctx, {
                    type: 'bar',
                    data: {
                        labels: ['Meta Ads', 'TikTok Ads', 'Google Ads'],
                        datasets: [{
                            data: [metaSpend, tiktokSpend, googleSpend],
                            backgroundColor: ['#3b82f6', '#ec4899', '#10b981'],
                            borderRadius: 6,
                            barThickness: 50
                        }]
                    },
                    options: {
                        responsive: true,
                        maintainAspectRatio: false,
                        plugins: { legend: { display: false } },
                        scales: {
                            y: {
                                grid: { color: '#232d3f' },
                                ticks: { color: '#94a3b8' }
                            },
                            x: {
                                grid: { display: false },
                                ticks: { color: '#94a3b8' }
                            }
                        }
                    }
                });
            }

            async function fetchData() {
                try {
                    const res = await fetch('/api/data');
                    const json = await res.json();
                    globalData = json.data || {};

                    let metaSpend = 0, metaConv = 0;
                    (globalData.meta_ads || []).forEach(i => {
                        metaSpend += safeNum(i.spend || i.cost);
                        metaConv += safeNum(i.conversions || i.results);
                    });

                    let tiktokSpend = 0, tiktokConv = 0;
                    (globalData.tiktok_ads || []).forEach(i => {
                        tiktokSpend += safeNum(i.spend || i.cost);
                        tiktokConv += safeNum(i.conversion || i.conversions);
                    });

                    let googleSpend = 0, googleConv = 0;
                    (globalData.google_ads || []).forEach(i => {
                        googleSpend += safeNum(i.spend || i.cost);
                        googleConv += safeNum(i.conversions);
                    });

                    let totalSpend = metaSpend + tiktokSpend + googleSpend;

                    document.getElementById('meta-spend').innerText = metaSpend.toFixed(2) + ' ر.س';
                    document.getElementById('meta-sub').innerText = metaConv + ' محادثة/نتيجة';

                    document.getElementById('tiktok-spend').innerText = tiktokSpend.toFixed(2) + ' ر.س';
                    document.getElementById('tiktok-sub').innerText = tiktokConv + ' تحويل';

                    document.getElementById('google-spend').innerText = googleSpend.toFixed(2) + ' ر.س';
                    document.getElementById('google-sub').innerText = googleConv + ' تحويلات';

                    document.getElementById('total-spend').innerText = totalSpend.toFixed(2) + ' ر.س';

                    const now = new Date();
                    const yesterday = new Date(now);
                    yesterday.setDate(yesterday.getDate() - 1);
                    const dateStr = yesterday.toISOString().split('T')[0];
                    document.getElementById('update-time').innerText = `تقرير أداء يوم ${dateStr} - آخر تحديث: ${now.toLocaleTimeString('ar-SA')}`;

                    initChart(metaSpend, tiktokSpend, googleSpend);
                    renderTable();
                } catch (e) {
                    console.error('Error loading dashboard:', e);
                }
            }

            function renderTable() {
                let tbody = document.getElementById('campaigns-tbody');
                let rowsHtml = '';

                const appendRows = (list, platformName) => {
                    list.forEach(i => {
                        let spend = safeNum(i.spend || i.cost);
                        let impressions = safeNum(i.impressions);
                        let clicks = safeNum(i.clicks);
                        let conv = safeNum(i.conversions || i.conversion || i.results);
                        let name = i.account_name || i.campaign || i.campaign_name || 'حملة عامة';

                        rowsHtml += `
                            <tr>
                                <td><strong>${platformName}</strong></td>
                                <td>${name}</td>
                                <td>${spend.toFixed(2)} ر.س</td>
                                <td>${impressions}</td>
                                <td>${clicks}</td>
                                <td>${conv}</td>
                            </tr>
                        `;
                    });
                };

                if (currentFilter === 'all' || currentFilter === 'meta') appendRows(globalData.meta_ads || [], 'Meta');
                if (currentFilter === 'all' || currentFilter === 'tiktok') appendRows(globalData.tiktok_ads || [], 'TikTok');
                if (currentFilter === 'all' || currentFilter === 'google') appendRows(globalData.google_ads || [], 'Google');

                tbody.innerHTML = rowsHtml || '<tr><td colspan="6" style="text-align:center;">لا توجد بيانات لهذه المنصة حالياً</td></tr>';
            }

            function setFilter(type, btn) {
                document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
                btn.classList.add('active');
                currentFilter = type;
                renderTable();
            }

            document.addEventListener('DOMContentLoaded', fetchData);
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)
