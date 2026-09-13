import os
import asyncio
import logging
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
            logger.info("جاري تحديث بيانات إعلانات elevenz...")
            
            meta_fields = "account_name,campaign,adset_name,ad_name,clicks,spend,conversions,impressions,cpc,ctr,date,actions,results,inline_post_engagement,onsite_conversion_messaging_conversation_started_7d"
            tiktok_fields = "account_name,campaign_name,adgroup_name,ad_name,clicks,spend,conversion,conversions,impressions,cpc,ctr,date,cost_per_conversion"
            google_fields = "account_name,campaign,ad_group_name,ad_name,clicks,spend,conversions,all_conversions,impressions,cpc,ctr,date"

            meta_res, tiktok_res, google_res = await asyncio.gather(
                fetch_windsor_connector("facebook", {"fields": meta_fields}),
                fetch_windsor_connector("tiktok", {"fields": tiktok_fields}),
                fetch_windsor_connector("google_ads", {"fields": google_fields}),
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

@app.get("/api/status")
async def get_status():
    return {"status": "ok"}

@app.get("/api/data")
async def get_dashboard_data():
    return JSONResponse(content={
        "status": "success",
        "data": CACHE,
        "last_updated": CACHE["last_updated"]
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

            .time-selector { display: flex; gap: 8px; margin-bottom: 26px; }
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
            .card-icon {
                width: 34px; height: 34px; border-radius: 10px;
                display: flex; align-items: center; justify-content: center;
                font-size: 16px; flex-shrink: 0;
            }
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
            .chart-wrapper { height: 280px; position: relative; }
            .donut-wrapper { height: 240px; position: relative; }

            .legend-list { display: flex; flex-direction: column; gap: 10px; margin-top: 16px; }
            .legend-item { display: flex; align-items: center; justify-content: space-between; font-size: 12.5px; font-weight: 600; color: var(--text-muted); }
            .legend-dot { width: 9px; height: 9px; border-radius: 50%; display: inline-block; margin-left: 8px; }
            .legend-label { display: flex; align-items: center; }
            .legend-value { color: var(--text-dark); font-weight: 800; }

            .platform-card {
                background-color: var(--card-bg);
                border-radius: var(--radius-lg);
                box-shadow: var(--shadow-card);
                margin-bottom: 20px;
                overflow: hidden;
            }
            .platform-header {
                padding: 18px 24px;
                font-weight: 800;
                font-size: 15px;
                display: flex;
                align-items: center;
                gap: 10px;
                border-bottom: 1px solid var(--border-color);
                color: var(--text-dark);
            }
            .platform-dot { width: 10px; height: 10px; border-radius: 50%; }

            .table-scroll { overflow-x: auto; }
            table { width: 100%; border-collapse: collapse; text-align: right; font-size: 13.5px; min-width: 760px; }
            th, td { padding: 14px 20px; border-bottom: 1px solid var(--border-color); white-space: nowrap; }
            th { color: var(--text-muted); font-weight: 700; background-color: var(--nested-deep); font-size: 12px; position: sticky; top: 0; }
            tbody tr:last-child td { border-bottom: none; }

            .row-campaign { cursor: pointer; font-weight: 800; background-color: var(--card-bg); }
            .row-campaign:hover { background-color: var(--nested-bg); }
            .row-campaign td:first-child { display: flex; align-items: center; gap: 8px; }
            .row-adgroup { cursor: pointer; background-color: var(--nested-bg); display: none; }
            .row-adgroup:hover { background-color: var(--nested-deep); }
            .row-adgroup td { padding-right: 44px; font-size: 13px; font-weight: 700; }
            .row-ad { background-color: var(--nested-deep); display: none; }
            .row-ad td { padding-right: 66px; font-size: 12.5px; color: var(--text-muted); font-weight: 500; }
            .row-ad td:first-child { display: flex; align-items: center; gap: 8px; color: var(--text-dark); }

            .chevron { display: inline-flex; transition: transform 0.18s ease; color: var(--text-faint); flex-shrink: 0; }
            .chevron svg { width: 13px; height: 13px; }
            .expanded > .chevron, td .chevron.rot { transform: rotate(-90deg); }
            .row-campaign.expanded .chevron, .row-adgroup.expanded .chevron { transform: rotate(0deg); }
            .row-campaign .chevron, .row-adgroup .chevron { transform: rotate(-90deg); }

            .name-icon { font-size: 14px; }

            .badge { padding: 3px 10px; border-radius: 999px; font-size: 10.5px; font-weight: 800; display: inline-block; }
            .badge-good { background-color: var(--stage-green-bg); border: 1px solid var(--stage-green-border); color: var(--stage-green-text); }
            .badge-medium { background-color: var(--stage-blue-bg); border: 1px solid var(--stage-blue-border); color: var(--stage-blue-text); }
            .badge-bad { background-color: var(--stage-red-bg); border: 1px solid var(--stage-red-border); color: var(--stage-red-text); }

            .empty-state { padding: 40px 20px; text-align: center; color: var(--text-muted); font-weight: 600; font-size: 14px; }

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
                    <button class="btn btn-refresh" onclick="fetchData()">
                        <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M23 4v6h-6"></path><path d="M1 20v-6h6"></path><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"></path></svg>
                        تحديث البيانات
                    </button>
                </div>
            </div>

            <div class="time-selector">
                <button class="time-btn" onclick="setTimeRange('today', this)">اليوم</button>
                <button class="time-btn active" onclick="setTimeRange('yesterday', this)">أمس</button>
                <button class="time-btn" onclick="setTimeRange('last7', this)">آخر 7 أيام</button>
            </div>

            <div class="cards-container">
                <div class="card">
                    <div class="card-top">
                        <div class="card-title">إجمالي الإنفاق (الكلي)</div>
                        <div class="card-icon" style="background:var(--accent-orange-light); color:var(--accent-orange);">💰</div>
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
                        <div class="card-icon" style="background:#eef4ff; color:var(--accent-blue);">🔍</div>
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
                        <div class="card-icon" style="background:#fff1ec; color:var(--accent-orange);">🎵</div>
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
                        <div class="card-icon" style="background:#eef4ff; color:#0284c7;">📘</div>
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

            <div id="campaigns-tables-container"></div>
        </div>

        <div id="toast"></div>

        <script>
            Chart.register(ChartDataLabels);
            let chartInstance = null;
            let donutInstance = null;
            let currentTimeRange = 'yesterday';
            let globalData = {};
            let lastMetrics = null;

            function safeNum(val) {
                if (!val) return 0;
                let n = parseFloat(val);
                return isNaN(n) ? 0 : n;
            }

            function parseMetaConversions(item) {
                if (!item) return 0;
                let res = safeNum(item.conversions || item.results || item.onsite_conversion_messaging_conversation_started_7d);
                if (res > 0) return res;
                if (item.actions && Array.isArray(item.actions)) {
                    let total = 0;
                    item.actions.forEach(act => {
                        if (act.action_type && (act.action_type.includes('message') || act.action_type.includes('conversation') || act.action_type.includes('lead'))) {
                            total += safeNum(act.value);
                        }
                    });
                    if (total > 0) return total;
                }
                return safeNum(item.actions);
            }

            function parseGoogleConversions(item) {
                if (!item) return 0;
                return safeNum(item.conversions || item.all_conversions || item.results);
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
                            <span class="legend-value">${p.value.toLocaleString('ar-SA')} (${total > 0 ? ((p.value/total)*100).toFixed(0) : 0}%)</span>
                        </div>
                    `).join('');
                }
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

                document.getElementById('meta-sub').innerText = `${metaConv.toLocaleString('ar-SA')} محادثة/نتيجة`;
                document.getElementById('tiktok-sub').innerText = `${tiktokConv.toLocaleString('ar-SA')} تحويل/نقرة`;
                document.getElementById('google-sub').innerText = `${googleConv.toLocaleString('ar-SA')} إحالات`;

                document.getElementById('total-cpc').innerText = cpcOf(totalSpend, totalClicks).toFixed(2);
                document.getElementById('total-ctr').innerText = ctrOf(totalClicks, totalImpr).toFixed(1) + '%';
                document.getElementById('meta-ctr').innerText = metaCtr.toFixed(1) + '%';
                document.getElementById('tiktok-ctr').innerText = tiktokCtr.toFixed(1) + '%';
                document.getElementById('google-ctr').innerText = googleCtr.toFixed(1) + '%';
                document.getElementById('meta-cpa').innerText = metaCpa !== null ? metaCpa.toFixed(2) : '--';
                document.getElementById('tiktok-cpa').innerText = tiktokCpa !== null ? tiktokCpa.toFixed(2) : '--';
                document.getElementById('google-cpa').innerText = googleCpa !== null ? googleCpa.toFixed(2) : '--';

                let timeText = currentTimeRange === 'today' ? 'اليوم' : (currentTimeRange === 'yesterday' ? 'أمس' : 'آخر 7 أيام');
                let nowStr = new Date().toLocaleTimeString('ar-SA');
                document.getElementById('update-time').innerText = `تقرير الأداء (${timeText}) - آخر تحديث: ${nowStr}`;

                updateChart(metaSpend, tiktokSpend, googleSpend);
                updateDonut(metaConv, tiktokConv, googleConv);
                renderHierarchicalTables(metaList, tiktokList, googleList);

                lastMetrics = {
                    timeText, nowStr,
                    metaSpend, tiktokSpend, googleSpend, totalSpend,
                    metaConv, tiktokConv, googleConv,
                    metaCtr, tiktokCtr, googleCtr,
                    metaCpa, tiktokCpa, googleCpa
                };
            }

            function groupByTree(list, isMeta = false, isGoogle = false) {
                let tree = {};
                list.forEach(i => {
                    let cName = i.campaign || i.campaign_name || 'حملة رئيسية';
                    let gName = i.adset_name || i.adgroup_name || i.ad_group_name || 'المجموعة الإعلانية';
                    let aName = i.ad_name || 'الإعلان';

                    if (!tree[cName]) tree[cName] = { spend: 0, clicks: 0, impressions: 0, conv: 0, groups: {} };
                    if (!tree[cName].groups[gName]) tree[cName].groups[gName] = { spend: 0, clicks: 0, impressions: 0, conv: 0, ads: {} };
                    if (!tree[cName].groups[gName].ads[aName]) tree[cName].groups[gName].ads[aName] = { spend: 0, clicks: 0, impressions: 0, conv: 0 };

                    let sp = safeNum(i.spend || i.cost);
                    let cl = safeNum(i.clicks);
                    let im = safeNum(i.impressions);
                    
                    let cv = 0;
                    if (isMeta) {
                        cv = parseMetaConversions(i);
                    } else if (isGoogle) {
                        cv = parseGoogleConversions(i);
                    } else {
                        cv = safeNum(i.conversions || i.conversion || i.results);
                    }

                    tree[cName].spend += sp; tree[cName].clicks += cl; tree[cName].impressions += im; tree[cName].conv += cv;
                    tree[cName].groups[gName].spend += sp; tree[cName].groups[gName].clicks += cl; tree[cName].groups[gName].impressions += im; tree[cName].groups[gName].conv += cv;
                    
                    let ad = tree[cName].groups[gName].ads[aName];
                    ad.spend += sp; ad.clicks += cl; ad.impressions += im; ad.conv += cv;
                });
                return tree;
            }

            function metricsRow(m) {
                let ctr = m.impressions > 0 ? (m.clicks / m.impressions) * 100 : 0;
                let cpa = m.conv > 0 ? m.spend / m.conv : null;
                return { ctr, cpa };
            }

            function renderHierarchicalTables(metaList, tiktokList, googleList) {
                const container = document.getElementById('campaigns-tables-container');
                container.innerHTML = '';

                const buildPlatformTreeHtml = (title, list, convLabel, platformKey, dotColor, isMeta = false, isGoogle = false) => {
                    if (list.length === 0) return '';
                    let tree = groupByTree(list, isMeta, isGoogle);
                    let rowsHtml = '';
                    let cIndex = 0;

                    for (let cName in tree) {
                        cIndex++;
                        let camp = tree[cName];
                        let campId = `${platformKey}-c-${cIndex}`;
                        let campM = metricsRow(camp);

                        rowsHtml += `
                            <tr class="row-campaign" onclick="toggleRow('${campId}', this)" id="header-${campId}">
                                <td><span class="chevron">${chevronSvg()}</span><span class="name-icon">📂</span> ${cName}</td>
                                <td>${camp.spend.toFixed(2)} ر.س</td>
                                <td>${camp.impressions.toLocaleString('ar-SA')}</td>
                                <td>${camp.clicks.toLocaleString('ar-SA')}</td>
                                <td>${campM.ctr.toFixed(1)}%</td>
                                <td>${campM.cpa !== null ? campM.cpa.toFixed(2) + ' ر.س' : '--'}</td>
                                <td>${camp.conv}</td>
                            </tr>
                        `;

                        let gIndex = 0;
                        for (let gName in camp.groups) {
                            gIndex++;
                            let group = camp.groups[gName];
                            let groupId = `${campId}-g-${gIndex}`;
                            let groupM = metricsRow(group);

                            rowsHtml += `
                                <tr class="row-adgroup ${campId}" onclick="toggleRow('${groupId}', this)" id="header-${groupId}">
                                    <td><span class="chevron">${chevronSvg()}</span><span class="name-icon">📁</span> المجموعة: ${gName}</td>
                                    <td>${group.spend.toFixed(2)} ر.س</td>
                                    <td>${group.impressions.toLocaleString('ar-SA')}</td>
                                    <td>${group.clicks.toLocaleString('ar-SA')}</td>
                                    <td>${groupM.ctr.toFixed(1)}%</td>
                                    <td>${groupM.cpa !== null ? groupM.cpa.toFixed(2) + ' ر.س' : '--'}</td>
                                    <td>${group.conv}</td>
                                </tr>
                            `;

                            for (let aName in group.ads) {
                                let ad = group.ads[aName];
                                let adM = metricsRow(ad);
                                let badgeHtml = tierBadge(adM.ctr);

                                rowsHtml += `
                                    <tr class="row-ad ${campId} ${groupId}">
                                        <td><span class="name-icon">🎯</span> ${aName} ${badgeHtml}</td>
                                        <td>${ad.spend.toFixed(2)} ر.س</td>
                                        <td>${ad.impressions.toLocaleString('ar-SA')}</td>
                                        <td>${ad.clicks.toLocaleString('ar-SA')}</td>
                                        <td>${adM.ctr.toFixed(1)}%</td>
                                        <td>${adM.cpa !== null ? adM.cpa.toFixed(2) + ' ر.س' : '--'}</td>
                                        <td>${ad.conv}</td>
                                    </tr>
                                `;
                            }
                        }
                    }

                    return `
                        <div class="platform-card">
                            <div class="platform-header"><span class="platform-dot" style="background:${dotColor}"></span>${title}</div>
                            <div class="table-scroll">
                            <table>
                                <thead>
                                    <tr>
                                        <th>اسم الحملة / المجموعة / الإعلان</th>
                                        <th>الإنفاق</th>
                                        <th>الظهور</th>
                                        <th>النقرات</th>
                                        <th>CTR</th>
                                        <th>تكلفة/نتيجة</th>
                                        <th>${convLabel}</th>
                                    </tr>
                                </thead>
                                <tbody>${rowsHtml}</tbody>
                            </table>
                            </div>
                        </div>
                    `;
                };

                let html = buildPlatformTreeHtml('Meta Ads', metaList, 'المحادثات/النتائج', 'meta', '#0284c7', true, false);
                html += buildPlatformTreeHtml('TikTok Ads', tiktokList, 'التحويلات/المؤشرات', 'tiktok', '#f05a28', false, false);
                html += buildPlatformTreeHtml('Google Ads', googleList, 'الإحالات', 'google', '#0f2540', false, true);

                container.innerHTML = html || '<div class="platform-card"><div class="empty-state">لا توجد بيانات متاحة لهذه الفترة</div></div>';
            }

            function chevronSvg() {
                return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="15 18 9 12 15 6"></polyline></svg>';
            }

            function toggleRow(targetClass, headerEl) {
                let rows = document.getElementsByClassName(targetClass);
                let header = document.getElementById(`header-${targetClass}`);
                let isExpanding = false;

                if (header) {
                    header.classList.toggle('expanded');
                    isExpanding = header.classList.contains('expanded');
                    const chev = header.querySelector('.chevron');
                    if (chev) chev.style.transform = isExpanding ? 'rotate(0deg)' : 'rotate(-90deg)';
                }

                for (let row of rows) {
                    if (isExpanding) {
                        if (row.classList.contains('row-adgroup') || (header && header.classList.contains('row-adgroup') && row.classList.contains('row-ad'))) {
                            row.style.display = 'table-row';
                        }
                    } else {
                        row.style.display = 'none';
                        row.classList.remove('expanded');
                        const innerChev = row.querySelector('.chevron');
                        if (innerChev) innerChev.style.transform = 'rotate(-90deg)';
                    }
                }
            }

            function setTimeRange(range, btn) {
                document.querySelectorAll('.time-btn').forEach(b => b.classList.remove('active'));
                btn.classList.add('active');
                currentTimeRange = range;
                updateDashboardUI();
            }

            function fmt(n) { return (Math.round(n * 100) / 100).toLocaleString('ar-SA'); }

            function copyReport() {
                if (!lastMetrics) return;
                const m = lastMetrics;
                const line = '—'.repeat(1);
                const text =
`📊 *تقرير أداء الإعلانات - elevenz*
🗓️ الفترة: ${m.timeText}
⏱️ آخر تحديث: ${m.nowStr}

💰 *إجمالي الإنفاق:* ${fmt(m.totalSpend)} ر.س

📘 *Meta Ads*
• الإنفاق: ${fmt(m.metaSpend)} ر.س
• النتائج: ${m.metaConv.toLocaleString('ar-SA')}
• CTR: ${m.metaCtr.toFixed(1)}%
• تكلفة/نتيجة: ${m.metaCpa !== null ? fmt(m.metaCpa) + ' ر.س' : '--'}

🎵 *TikTok Ads*
• الإنفاق: ${fmt(m.tiktokSpend)} ر.س
• التحويلات: ${m.tiktokConv.toLocaleString('ar-SA')}
• CTR: ${m.tiktokCtr.toFixed(1)}%
• تكلفة/نتيجة: ${m.tiktokCpa !== null ? fmt(m.tiktokCpa) + ' ر.س' : '--'}

🔍 *Google Ads*
• الإنفاق: ${fmt(m.googleSpend)} ر.س
• الإحالات: ${m.googleConv.toLocaleString('ar-SA')}
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

            document.addEventListener('DOMContentLoaded', fetchData);
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)
