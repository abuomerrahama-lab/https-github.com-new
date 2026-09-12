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

async def refresh_cache_and_keep_alive():
    global CACHE
    while True:
        try:
            logger.info("جاري تحديث بيانات الإعلانات وتجميع النتائج...")
            
            # 🔍 جلب جميع حقول نتائج المحادثات والتحويلات الممكنة من Meta
            meta_fields = "account_name,campaign,clicks,spend,conversions,impressions,cpc,ctr,date,actions,results,inline_post_engagement,onsite_conversion_messaging_conversation_started_7d"
            
            meta_res, tiktok_res, google_res = await asyncio.gather(
                fetch_windsor_connector("facebook", {"fields": meta_fields}),
                fetch_windsor_connector("tiktok", {"fields": "account_name,campaign_name,clicks,spend,conversion,impressions,cpc,ctr,date"}),
                fetch_windsor_connector("google_ads", {"fields": "account_name,campaign,clicks,spend,conversions,impressions,cpc,ctr,date"}),
                return_exceptions=True
            )

            CACHE["meta_ads"] = meta_res if isinstance(meta_res, list) else []
            CACHE["tiktok_ads"] = tiktok_res if isinstance(tiktok_res, list) else []
            CACHE["google_ads"] = google_res if isinstance(google_res, list) else []
            CACHE["last_updated"] = asyncio.get_event_loop().time()
            
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
                margin-bottom: 20px;
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
            }

            .time-selector {
                display: flex;
                gap: 10px;
                margin-bottom: 20px;
            }
            .time-btn {
                background-color: var(--bg-card);
                border: 1px solid var(--border-color);
                color: var(--text-muted);
                padding: 8px 18px;
                border-radius: 20px;
                cursor: pointer;
                font-size: 13px;
                font-weight: 600;
            }
            .time-btn.active {
                background-color: var(--accent-gold);
                color: #000;
                border-color: var(--accent-gold);
            }

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
            .card-highlight { border-color: var(--accent-gold); }

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
            .chart-wrapper { height: 320px; position: relative; }

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

            .platform-card {
                background-color: var(--bg-card);
                border: 1px solid var(--border-color);
                border-radius: 12px;
                margin-bottom: 20px;
                overflow: hidden;
            }
            .platform-header {
                padding: 16px 20px;
                font-weight: bold;
                font-size: 16px;
                display: flex;
                justify-content: space-between;
                align-items: center;
                border-bottom: 1px solid var(--border-color);
            }
            .dot {
                height: 8px; width: 8px; border-radius: 50%; display: inline-block; margin-left: 8px;
            }
            .dot-meta { background-color: var(--accent-blue); }
            .dot-tiktok { background-color: var(--accent-pink); }
            .dot-google { background-color: var(--accent-green); }

            table { width: 100%; border-collapse: collapse; text-align: right; font-size: 14px; }
            th, td { padding: 14px 20px; border-bottom: 1px solid var(--border-color); }
            th { color: var(--text-muted); font-weight: 600; background-color: rgba(0,0,0,0.15); }
        </style>
    </head>
    <body>
        <div class="top-bar">
            <button class="btn-refresh" onclick="fetchData()">تحديث البيانات</button>
            <div class="header-title" style="text-align: left;">
                <h1>التقرير اليومي للإعلانات</h1>
                <p id="update-time">تقرير الأداء | آخر تحديث: --</p>
            </div>
        </div>

        <div class="time-selector">
            <button class="time-btn" onclick="setTimeRange('today', this)">اليوم</button>
            <button class="time-btn active" onclick="setTimeRange('yesterday', this)">أمس</button>
            <button class="time-btn" onclick="setTimeRange('last7', this)">آخر 7 أيام</button>
        </div>

        <div class="cards-container">
            <div class="card card-highlight">
                <div class="card-title">إجمالي الإنفاق (كل المنصات)</div>
                <div class="card-value" id="total-spend">0.00 ر.س</div>
                <div class="card-sub">الحسابات النشطة</div>
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
            <div class="chart-title" id="chart-period-title">الإنفاق حسب الحساب - أمس</div>
            <div class="chart-wrapper">
                <canvas id="spendChart"></canvas>
            </div>
        </div>

        <div class="filters-bar">
            <button class="filter-btn active" onclick="setPlatformFilter('all', this)">الكل</button>
            <button class="filter-btn" onclick="setPlatformFilter('meta', this)">Meta</button>
            <button class="filter-btn" onclick="setPlatformFilter('tiktok', this)">TikTok</button>
            <button class="filter-btn" onclick="setPlatformFilter('google', this)">Google Ads</button>
        </div>

        <div id="campaigns-tables-container"></div>

        <script>
            let chartInstance = null;
            let currentPlatform = 'all';
            let currentTimeRange = 'yesterday';
            let globalData = {};

            function safeNum(val) {
                if (!val) return 0;
                let n = parseFloat(val);
                return isNaN(n) ? 0 : n;
            }

            // 🎯 دالة مرنة لحساب نتائج ومحادثات ميتا المتعددة
            function parseMetaConversions(item) {
                if (!item) return 0;
                let res = safeNum(item.conversions || item.results || item.actions || item.onsite_conversion_messaging_conversation_started_7d);
                if (res > 0) return res;

                // التفتيش في كائن الأكشنز إذا كان آتياً كمصفوفة أو أوبجكت من Windsor
                if (item.actions && Array.isArray(item.actions)) {
                    let totalActions = 0;
                    item.actions.forEach(act => {
                        if (act.action_type && (
                            act.action_type.includes('message') || 
                            act.action_type.includes('conversation') || 
                            act.action_type.includes('lead')
                        )) {
                            totalActions += safeNum(act.value);
                        }
                    });
                    if (totalActions > 0) return totalActions;
                }
                return 0;
            }

            function filterByDate(list) {
                if (!list || !Array.isArray(list)) return [];
                const now = new Date();
                const todayStr = now.toISOString().split('T')[0];

                const yesterday = new Date(now);
                yesterday.setDate(yesterday.getDate() - 1);
                const yesterdayStr = yesterday.toISOString().split('T')[0];

                const d7 = new Date(now);
                d7.setDate(d7.getDate() - 7);

                return list.filter(i => {
                    if (!i.date) return true;
                    if (currentTimeRange === 'today') return i.date === todayStr;
                    if (currentTimeRange === 'yesterday') return i.date === yesterdayStr;
                    if (currentTimeRange === 'last7') return new Date(i.date) >= d7;
                    return true;
                });
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
                            y: { grid: { color: '#232d3f' }, ticks: { color: '#94a3b8' } },
                            x: { grid: { display: false }, ticks: { color: '#94a3b8' } }
                        }
                    }
                });
            }

            async function fetchData() {
                try {
                    const res = await fetch('/api/data');
                    const json = await res.json();
                    globalData = json.data || {};
                    updateDashboardUI();
                } catch (e) {
                    console.error('Error fetching data:', e);
                }
            }

            function updateDashboardUI() {
                let metaList = filterByDate(globalData.meta_ads);
                let tiktokList = filterByDate(globalData.tiktok_ads);
                let googleList = filterByDate(globalData.google_ads);

                let metaSpend = 0, metaConv = 0;
                metaList.forEach(i => {
                    metaSpend += safeNum(i.spend || i.cost);
                    metaConv += parseMetaConversions(i);
                });

                let tiktokSpend = 0, tiktokConv = 0;
                tiktokList.forEach(i => {
                    tiktokSpend += safeNum(i.spend || i.cost);
                    tiktokConv += safeNum(i.conversion || i.conversions);
                });

                let googleSpend = 0, googleConv = 0;
                googleList.forEach(i => {
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

                let timeText = currentTimeRange === 'today' ? 'اليوم' : (currentTimeRange === 'yesterday' ? 'أمس' : 'آخر 7 أيام');
                document.getElementById('chart-period-title').innerText = `الإنفاق حسب الحساب - ${timeText}`;
                document.getElementById('update-time').innerText = `تقرير أداء (${timeText}) - آخر تحديث: ${new Date().toLocaleTimeString('ar-SA')}`;

                initChart(metaSpend, tiktokSpend, googleSpend);
                renderTables(metaList, tiktokList, googleList);
            }

            function renderTables(metaList, tiktokList, googleList) {
                const container = document.getElementById('campaigns-tables-container');
                container.innerHTML = '';

                const buildTableHtml = (title, dotClass, list, convLabel, isMeta = false) => {
                    if (list.length === 0) return '';
                    let rows = list.map(i => {
                        let convCount = isMeta ? parseMetaConversions(i) : safeNum(i.conversions || i.conversion || i.results);
                        return `
                            <tr>
                                <td>${i.campaign || i.campaign_name || i.account_name || 'حملة عامة'}</td>
                                <td>${safeNum(i.spend || i.cost).toFixed(2)} ر.س</td>
                                <td>${safeNum(i.impressions).toLocaleString('ar-SA')}</td>
                                <td>${safeNum(i.clicks).toLocaleString('ar-SA')}</td>
                                <td>${convCount}</td>
                            </tr>
                        `;
                    }).join('');

                    return `
                        <div class="platform-card">
                            <div class="platform-header">
                                <span><span class="dot ${dotClass}"></span> ${title}</span>
                            </div>
                            <table>
                                <thead>
                                    <tr>
                                        <th>الحملة</th>
                                        <th>الإنفاق</th>
                                        <th>الظهور</th>
                                        <th>النقرات</th>
                                        <th>${convLabel}</th>
                                    </tr>
                                </thead>
                                <tbody>${rows}</tbody>
                            </table>
                        </div>
                    `;
                };

                let html = '';
                if (currentPlatform === 'all' || currentPlatform === 'meta') {
                    html += buildTableHtml('Meta Ads', 'dot-meta', metaList, 'المحادثات/النتائج', true);
                }
                if (currentPlatform === 'all' || currentPlatform === 'tiktok') {
                    html += buildTableHtml('TikTok Ads', 'dot-tiktok', tiktokList, 'التحويلات');
                }
                if (currentPlatform === 'all' || currentPlatform === 'google') {
                    html += buildTableHtml('Google Ads', 'dot-google', googleList, 'التحويلات');
                }

                container.innerHTML = html || '<div class="platform-card" style="padding:20px; text-align:center; color:var(--text-muted);">لا توجد بيانات حملات متاحة لهذا الفلتر</div>';
            }

            function setTimeRange(range, btn) {
                document.querySelectorAll('.time-btn').forEach(b => b.classList.remove('active'));
                btn.classList.add('active');
                currentTimeRange = range;
                updateDashboardUI();
            }

            function setPlatformFilter(type, btn) {
                document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
                btn.classList.add('active');
                currentPlatform = type;
                updateDashboardUI();
            }

            document.addEventListener('DOMContentLoaded', fetchData);
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)
