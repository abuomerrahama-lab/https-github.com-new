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
            google_fields = "account_name,campaign,ad_group_name,ad_name,clicks,spend,conversions,impressions,cpc,ctr,date"

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
        <link href="https://fonts.googleapis.com/css2?family=Tajawal:wght@400;500;700;800&display=swap" rel="stylesheet">
        <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
        <script src="https://cdn.jsdelivr.net/npm/chartjs-plugin-datalabels@2.2.0"></script>
        <style>
            :root {
                --sidebar-bg: #031b33;
                --main-bg: #f4f6f9;
                --card-bg: #ffffff;
                --text-dark: #031b33;
                --text-muted: #64748b;
                --accent-orange: #f05a28;
                --accent-blue: #0284c7;
                --border-color: #e2e8f0;
                --nested-bg: #f8fafc;
                --nested-deep: #f1f5f9;

                /* ألوان المراحل والبادجات */
                --stage-green-bg: #f0fdf4;
                --stage-green-border: #bbf7d0;
                --stage-green-text: #166534;
                --stage-blue-bg: #f0f9ff;
                --stage-blue-border: #bae6fd;
                --stage-blue-text: #0369a1;
                --stage-red-bg: #fef2f2;
                --stage-red-border: #fecaca;
                --stage-red-text: #991b1b;
            }
            body {
                font-family: 'Tajawal', sans-serif;
                background-color: var(--main-bg);
                color: var(--text-dark);
                margin: 0;
                display: flex;
                height: 100vh;
                overflow: hidden;
                direction: rtl;
            }
            .sidebar {
                width: 260px;
                background-color: var(--sidebar-bg);
                color: #ffffff;
                display: flex;
                flex-direction: column;
                padding: 20px 0;
                box-shadow: -2px 0 10px rgba(0,0,0,0.05);
            }
            .sidebar-logo {
                padding: 0 24px 20px 24px;
                font-size: 28px;
                font-weight: 800;
                color: #fff;
                border-bottom: 1px solid rgba(255,255,255,0.08);
            }
            .sidebar-logo span { color: var(--accent-orange); }
            .sidebar-menu { list-style: none; padding: 20px 0; margin: 0; flex: 1; }
            .sidebar-menu li a {
                display: flex;
                align-items: center;
                padding: 12px 24px;
                color: #94a3b8;
                text-decoration: none;
                font-weight: 500;
                font-size: 14px;
                transition: all 0.2s;
            }
            .sidebar-menu li.active a, .sidebar-menu li a:hover {
                background-color: rgba(255,255,255,0.06);
                color: #ffffff;
                border-right: 4px solid var(--accent-orange);
            }

            .user-profile {
                padding: 16px 24px;
                border-top: 1px solid rgba(255,255,255,0.08);
                display: flex;
                align-items: center;
                gap: 12px;
            }
            .user-avatar {
                width: 36px;
                height: 36px;
                border-radius: 50%;
                background-color: var(--accent-orange);
                display: flex;
                align-items: center;
                justify-content: center;
                font-weight: bold;
                color: #fff;
            }

            .main-content {
                flex: 1;
                overflow-y: auto;
                padding: 30px;
            }
            .top-bar { display: flex; justify-content: space-between; align-items: center; margin-bottom: 25px; }
            .header-title h1 { margin: 0; font-size: 24px; font-weight: 800; color: var(--sidebar-bg); }
            .header-title p { margin: 4px 0 0 0; color: var(--text-muted); font-size: 13px; }
            .btn-refresh {
                background-color: var(--accent-orange);
                color: #fff;
                font-weight: bold;
                border: none;
                padding: 10px 22px;
                border-radius: 8px;
                cursor: pointer;
                box-shadow: 0 2px 6px rgba(240, 90, 40, 0.3);
            }

            .time-selector { display: flex; gap: 8px; margin-bottom: 25px; }
            .time-btn {
                background-color: var(--card-bg);
                border: 1px solid var(--border-color);
                color: var(--text-muted);
                padding: 8px 18px;
                border-radius: 20px;
                cursor: pointer;
                font-size: 13px;
                font-weight: 600;
            }
            .time-btn.active {
                background-color: var(--sidebar-bg);
                color: #fff;
                border-color: var(--sidebar-bg);
            }

            .cards-container { display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; margin-bottom: 25px; }
            .card {
                background-color: var(--card-bg);
                border: 1px solid var(--border-color);
                border-radius: 10px;
                padding: 20px;
                text-align: right;
                box-shadow: 0 1px 3px rgba(0,0,0,0.02);
            }
            .card-title { font-size: 13px; color: var(--text-muted); margin-bottom: 12px; font-weight: 500; }
            .card-value { font-size: 26px; font-weight: 800; color: var(--sidebar-bg); margin-bottom: 4px; }
            .card-sub { font-size: 12px; color: var(--accent-orange); font-weight: 600; }

            .chart-section {
                background-color: var(--card-bg);
                border: 1px solid var(--border-color);
                border-radius: 10px;
                padding: 24px;
                margin-bottom: 25px;
            }
            .chart-wrapper { height: 320px; position: relative; }

            .platform-card {
                background-color: var(--card-bg);
                border: 1px solid var(--border-color);
                border-radius: 10px;
                margin-bottom: 20px;
                overflow: hidden;
            }
            .platform-header {
                padding: 16px 20px;
                font-weight: 700;
                font-size: 15px;
                background-color: #fafafa;
                border-bottom: 1px solid var(--border-color);
                color: var(--sidebar-bg);
            }

            table { width: 100%; border-collapse: collapse; text-align: right; font-size: 14px; }
            th, td { padding: 14px 20px; border-bottom: 1px solid var(--border-color); }
            th { color: var(--text-muted); font-weight: 600; background-color: #f8fafc; font-size: 13px; }

            .row-campaign { cursor: pointer; font-weight: 700; background-color: var(--card-bg); }
            .row-campaign:hover { background-color: #f1f5f9; }
            .row-adgroup { cursor: pointer; background-color: var(--nested-bg); display: none; }
            .row-adgroup td { padding-right: 40px; font-size: 13px; }
            .row-ad { background-color: var(--nested-deep); display: none; }
            .row-ad td { padding-right: 60px; font-size: 12px; color: var(--text-muted); }

            .badge { padding: 3px 8px; border-radius: 6px; font-size: 11px; font-weight: 700; display: inline-block; margin-right: 6px; }
            .badge-good { background-color: var(--stage-green-bg); border: 1px solid var(--stage-green-border); color: var(--stage-green-text); }
            .badge-medium { background-color: var(--stage-blue-bg); border: 1px solid var(--stage-blue-border); color: var(--stage-blue-text); }
            .badge-bad { background-color: var(--stage-red-bg); border: 1px solid var(--stage-red-border); color: var(--stage-red-text); }
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
                    <div style="font-weight: 700; font-size: 14px;">أبو بكر</div>
                    <div style="font-size: 11px; color: #94a3b8;">مدير الحملات</div>
                </div>
            </div>
        </div>

        <div class="main-content">
            <div class="top-bar">
                <div class="header-title">
                    <h1>منصة التقرير الإعلاني - elevenz</h1>
                    <p id="update-time">تتبع مباشر للأداء والمؤشرات | آخر تحديث: --</p>
                </div>
                <button class="btn-refresh" onclick="fetchData()">تحديث البيانات</button>
            </div>

            <div class="time-selector">
                <button class="time-btn" onclick="setTimeRange('today', this)">اليوم</button>
                <button class="time-btn active" onclick="setTimeRange('yesterday', this)">أمس</button>
                <button class="time-btn" onclick="setTimeRange('last7', this)">آخر 7 أيام</button>
            </div>

            <div class="cards-container">
                <div class="card">
                    <div class="card-title">إجمالي الإنفاق (الكلي)</div>
                    <div class="card-value" id="total-spend">0.00 ر.س</div>
                    <div class="card-sub">جميع حسابات الربط</div>
                </div>
                <div class="card">
                    <div class="card-title">Google Ads</div>
                    <div class="card-value" id="google-spend">0.00 ر.س</div>
                    <div class="card-sub" id="google-sub">0 تحويلات</div>
                </div>
                <div class="card">
                    <div class="card-title">TikTok Ads</div>
                    <div class="card-value" id="tiktok-spend">0.00 ر.س</div>
                    <div class="card-sub" id="tiktok-sub">0 تحويل/نقرة</div>
                </div>
                <div class="card">
                    <div class="card-title">Meta Ads</div>
                    <div class="card-value" id="meta-spend">0.00 ر.س</div>
                    <div class="card-sub" id="meta-sub">0 محادثة/نتيجة</div>
                </div>
            </div>

            <div class="chart-section">
                <div style="font-weight:700; margin-bottom:15px; color:var(--sidebar-bg);" id="chart-period-title">تحليل الإنفاق حسب الحساب</div>
                <div class="chart-wrapper">
                    <canvas id="spendChart"></canvas>
                </div>
            </div>

            <div id="campaigns-tables-container"></div>
        </div>

        <script>
            Chart.register(ChartDataLabels);
            let chartInstance = null;
            let currentTimeRange = 'yesterday';
            let globalData = {};

            function safeNum(val) {
                if (!val) return 0;
                let n = parseFloat(val);
                return isNaN(n) ? 0 : n;
            }

            function parseMetaConversions(item) {
                if (!item) return 0;
                let res = safeNum(item.conversions || item.results || item.actions || item.onsite_conversion_messaging_conversation_started_7d);
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

            function updateChart(metaList, tiktokList, googleList) {
                const ctx = document.getElementById('spendChart').getContext('2d');
                if (chartInstance) chartInstance.destroy();

                let mSpend = metaList.reduce((s, i) => s + safeNum(i.spend || i.cost), 0);
                let tSpend = tiktokList.reduce((s, i) => s + safeNum(i.spend || i.cost), 0);
                let gSpend = googleList.reduce((s, i) => s + safeNum(i.spend || i.cost), 0);

                chartInstance = new Chart(ctx, {
                    type: 'bar',
                    data: {
                        labels: ['Meta Ads', 'TikTok Ads', 'Google Ads'],
                        datasets: [{
                            data: [mSpend, tSpend, gSpend],
                            backgroundColor: ['#0284c7', '#f05a28', '#031b33'],
                            borderRadius: 6,
                            barThickness: 45
                        }]
                    },
                    options: {
                        responsive: true,
                        maintainAspectRatio: false,
                        plugins: {
                            legend: { display: false },
                            datalabels: {
                                anchor: 'end',
                                align: 'top',
                                color: '#031b33',
                                font: { weight: 'bold', family: 'Tajawal' },
                                formatter: (val) => val > 0 ? val.toFixed(0) + ' ر.س' : ''
                            }
                        },
                        scales: {
                            y: { grid: { color: '#e2e8f0' } },
                            x: { grid: { display: false } }
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

                let metaSpend = metaList.reduce((s, i) => s + safeNum(i.spend || i.cost), 0);
                let tiktokSpend = tiktokList.reduce((s, i) => s + safeNum(i.spend || i.cost), 0);
                let googleSpend = googleList.reduce((s, i) => s + safeNum(i.spend || i.cost), 0);

                document.getElementById('meta-spend').innerText = metaSpend.toFixed(2) + ' ر.س';
                document.getElementById('tiktok-spend').innerText = tiktokSpend.toFixed(2) + ' ر.س';
                document.getElementById('google-spend').innerText = googleSpend.toFixed(2) + ' ر.س';
                document.getElementById('total-spend').innerText = (metaSpend + tiktokSpend + googleSpend).toFixed(2) + ' ر.س';

                let timeText = currentTimeRange === 'today' ? 'اليوم' : (currentTimeRange === 'yesterday' ? 'أمس' : 'آخر 7 أيام');
                document.getElementById('update-time').innerText = `تقرير الأداء (${timeText}) - آخر تحديث: ${new Date().toLocaleTimeString('ar-SA')}`;

                updateChart(metaList, tiktokList, googleList);
                renderHierarchicalTables(metaList, tiktokList, googleList);
            }

            function groupByTree(list, isMeta = false) {
                let tree = {};
                list.forEach(i => {
                    let cName = i.campaign || i.campaign_name || 'حملة رئيسية';
                    let gName = i.adset_name || i.adgroup_name || i.ad_group_name || 'المجموعة الإعلانية';
                    let aName = i.ad_name || 'الإعلان';

                    if (!tree[cName]) tree[cName] = { spend: 0, clicks: 0, impressions: 0, conv: 0, groups: {} };
                    if (!tree[cName].groups[gName]) tree[cName].groups[gName] = { spend: 0, clicks: 0, impressions: 0, conv: 0, ads: {} };
                    if (!tree[cName].groups[gName].ads[aName]) tree[cName].groups[gName].ads[aName] = { spend: 0, clicks: 0, impressions: 0, conv: 0, cpc: 0, ctr: 0 };

                    let sp = safeNum(i.spend || i.cost);
                    let cl = safeNum(i.clicks);
                    let im = safeNum(i.impressions);
                    let cv = isMeta ? parseMetaConversions(i) : safeNum(i.conversions || i.conversion || i.results);

                    tree[cName].spend += sp; tree[cName].clicks += cl; tree[cName].impressions += im; tree[cName].conv += cv;
                    tree[cName].groups[gName].spend += sp; tree[cName].groups[gName].clicks += cl; tree[cName].groups[gName].impressions += im; tree[cName].groups[gName].conv += cv;
                    
                    let ad = tree[cName].groups[gName].ads[aName];
                    ad.spend += sp; ad.clicks += cl; ad.impressions += im; ad.conv += cv;
                    ad.cpc = safeNum(i.cpc) || (ad.clicks > 0 ? ad.spend / ad.clicks : 0);
                    ad.ctr = safeNum(i.ctr) || (ad.impressions > 0 ? (ad.clicks / ad.impressions) * 100 : 0);
                });
                return tree;
            }

            function renderHierarchicalTables(metaList, tiktokList, googleList) {
                const container = document.getElementById('campaigns-tables-container');
                container.innerHTML = '';

                const buildPlatformTreeHtml = (title, list, convLabel, platformKey, isTikTok = false, isMeta = false) => {
                    if (list.length === 0) return '';
                    let tree = groupByTree(list, isMeta);
                    let rowsHtml = '';
                    let cIndex = 0;

                    for (let cName in tree) {
                        cIndex++;
                        let camp = tree[cName];
                        let campId = `${platformKey}-c-${cIndex}`;

                        rowsHtml += `
                            <tr class="row-campaign" onclick="toggleRow('${campId}')" id="header-${campId}">
                                <td>📂 ${cName}</td>
                                <td>${camp.spend.toFixed(2)} ر.س</td>
                                <td>${camp.impressions.toLocaleString('ar-SA')}</td>
                                <td>${camp.clicks.toLocaleString('ar-SA')}</td>
                                <td>${camp.conv}</td>
                            </tr>
                        `;

                        let gIndex = 0;
                        for (let gName in camp.groups) {
                            gIndex++;
                            let group = camp.groups[gName];
                            let groupId = `${campId}-g-${gIndex}`;

                            rowsHtml += `
                                <tr class="row-adgroup ${campId}" onclick="toggleRow('${groupId}')" id="header-${groupId}">
                                    <td>📁 المجموعة: ${gName}</td>
                                    <td>${group.spend.toFixed(2)} ر.س</td>
                                    <td>${group.impressions.toLocaleString('ar-SA')}</td>
                                    <td>${group.clicks.toLocaleString('ar-SA')}</td>
                                    <td>${group.conv}</td>
                                </tr>
                            `;

                            for (let aName in group.ads) {
                                let ad = group.ads[aName];
                                let badgeHtml = isTikTok ? (ad.ctr >= 0.8 ? '<span class="badge badge-good">أداء ممتاز</span>' : '<span class="badge badge-medium">متابعة</span>') : '';

                                rowsHtml += `
                                    <tr class="row-ad ${campId} ${groupId}">
                                        <td>🎯 الإعلان: ${aName} ${badgeHtml}</td>
                                        <td>${ad.spend.toFixed(2)} ر.س</td>
                                        <td>${ad.impressions.toLocaleString('ar-SA')}</td>
                                        <td>${ad.clicks.toLocaleString('ar-SA')}</td>
                                        <td>${ad.conv}</td>
                                    </tr>
                                `;
                            }
                        }
                    }

                    return `
                        <div class="platform-card">
                            <div class="platform-header">${title}</div>
                            <table>
                                <thead>
                                    <tr>
                                        <th>اسم الحملة / المجموعة / الإعلان</th>
                                        <th>الإنفاق</th>
                                        <th>الظهور</th>
                                        <th>النقرات</th>
                                        <th>${convLabel}</th>
                                    </tr>
                                </thead>
                                <tbody>${rowsHtml}</tbody>
                            </table>
                        </div>
                    `;
                };

                let html = buildPlatformTreeHtml('Meta Ads', metaList, 'المحادثات/النتائج', 'meta', false, true);
                html += buildPlatformTreeHtml('TikTok Ads', tiktokList, 'التحويلات/المؤشرات', 'tiktok', true, false);
                html += buildPlatformTreeHtml('Google Ads', googleList, 'التحويلات', 'google');

                container.innerHTML = html || '<div class="platform-card" style="padding:20px; text-align:center;">لا توجد بيانات متاحة</div>';
            }

            function toggleRow(targetClass) {
                let rows = document.getElementsByClassName(targetClass);
                let header = document.getElementById(`header-${targetClass}`);
                let isExpanding = false;

                if (header) {
                    header.classList.toggle('expanded');
                    isExpanding = header.classList.contains('expanded');
                }

                for (let row of rows) {
                    if (isExpanding) {
                        if (row.classList.contains('row-adgroup') || (header && header.classList.contains('row-adgroup') && row.classList.contains('row-ad'))) {
                            row.style.display = 'table-row';
                        }
                    } else {
                        row.style.display = 'none';
                        row.classList.remove('expanded');
                    }
                }
            }

            function setTimeRange(range, btn) {
                document.querySelectorAll('.time-btn').forEach(b => b.classList.remove('active'));
                btn.classList.add('active');
                currentTimeRange = range;
                updateDashboardUI();
            }

            document.addEventListener('DOMContentLoaded', fetchData);
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)
