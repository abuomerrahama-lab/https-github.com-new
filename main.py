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
            logger.info("جاري تحديث بيانات الإعلانات وتفاصيل المجموعات والإعلانات...")
            
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
        <title>التقرير اليومي والشجري للإعلانات</title>
        <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
        <script src="https://cdn.jsdelivr.net/npm/chartjs-plugin-datalabels@2.2.0"></script>
        <style>
            :root {
                --bg-main: #0b0f17;
                --bg-card: #151c28;
                --bg-nested: #1e2738;
                --bg-nested-deep: #253247;
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
            .top-bar { display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; }
            .header-title h1 { margin: 0; font-size: 26px; font-weight: 800; }
            .header-title p { margin: 4px 0 0 0; color: var(--text-muted); font-size: 13px; }
            .btn-refresh { background-color: var(--accent-gold); color: #000; font-weight: bold; border: none; padding: 10px 20px; border-radius: 8px; cursor: pointer; }

            .time-selector { display: flex; gap: 10px; margin-bottom: 20px; }
            .time-btn { background-color: var(--bg-card); border: 1px solid var(--border-color); color: var(--text-muted); padding: 8px 18px; border-radius: 20px; cursor: pointer; font-size: 13px; font-weight: 600; }
            .time-btn.active { background-color: var(--accent-gold); color: #000; border-color: var(--accent-gold); }

            .cards-container { display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; margin-bottom: 25px; }
            .card { background-color: var(--bg-card); border: 1px solid var(--border-color); border-radius: 12px; padding: 20px; text-align: center; }
            .card-title { font-size: 13px; color: var(--text-muted); margin-bottom: 10px; }
            .card-value { font-size: 28px; font-weight: 800; margin-bottom: 4px; }
            .card-sub { font-size: 12px; color: var(--text-muted); }
            .card-highlight { border-color: var(--accent-gold); }

            .chart-section { background-color: var(--bg-card); border: 1px solid var(--border-color); border-radius: 12px; padding: 24px; margin-bottom: 25px; }
            .chart-title { text-align: center; color: var(--text-muted); font-size: 15px; margin-bottom: 20px; }
            .chart-wrapper { height: 350px; position: relative; }

            .filters-bar { display: flex; gap: 10px; margin-bottom: 20px; }
            .filter-btn { background-color: var(--bg-card); border: 1px solid var(--border-color); color: var(--text-muted); padding: 8px 20px; border-radius: 20px; cursor: pointer; font-size: 13px; font-weight: 600; }
            .filter-btn.active { background-color: var(--accent-gold); color: #000; border-color: var(--accent-gold); }

            .platform-card { background-color: var(--bg-card); border: 1px solid var(--border-color); border-radius: 12px; margin-bottom: 20px; overflow: hidden; }
            .platform-header { padding: 16px 20px; font-weight: bold; font-size: 16px; display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid var(--border-color); }
            .dot { height: 8px; width: 8px; border-radius: 50%; display: inline-block; margin-left: 8px; }
            .dot-meta { background-color: var(--accent-blue); }
            .dot-tiktok { background-color: var(--accent-pink); }
            .dot-google { background-color: var(--accent-green); }

            table { width: 100%; border-collapse: collapse; text-align: right; font-size: 14px; }
            th, td { padding: 14px 20px; border-bottom: 1px solid var(--border-color); }
            th { color: var(--text-muted); font-weight: 600; background-color: rgba(0,0,0,0.15); }

            /* أنماط القوائم الشجرية والتفاعلية */
            .row-campaign { cursor: pointer; font-weight: bold; background-color: var(--bg-card); transition: background-color 0.2s; }
            .row-campaign:hover { background-color: rgba(255,255,255,0.03); }
            .row-adgroup { cursor: pointer; background-color: var(--bg-nested); display: none; }
            .row-adgroup td { padding-right: 40px; font-size: 13px; }
            .row-ad { background-color: var(--bg-nested-deep); display: none; }
            .row-ad td { padding-right: 60px; font-size: 12px; color: var(--text-muted); }

            .toggle-icon { display: inline-block; width: 18px; text-align: center; margin-left: 6px; font-size: 12px; transition: transform 0.2s; }
            .expanded .toggle-icon { transform: rotate(-90deg); }

            .badge { padding: 4px 10px; border-radius: 12px; font-size: 11px; font-weight: bold; display: inline-block; margin-right: 8px; }
            .badge-good { background-color: rgba(16, 185, 129, 0.15); color: #10b981; border: 1px solid #10b981; }
            .badge-medium { background-color: rgba(245, 158, 11, 0.15); color: #f59e0b; border: 1px solid #f59e0b; }
            .badge-bad { background-color: rgba(239, 68, 68, 0.15); color: #ef4444; border: 1px solid #ef4444; }
            .weekly-trend { font-size: 11px; padding: 2px 6px; border-radius: 4px; background: rgba(255,255,255,0.05); }
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
                <div class="card-sub" id="tiktok-sub">0 تحويل/نقرة</div>
            </div>
            <div class="card">
                <div class="card-title">Meta - إجمالي الإنفاق</div>
                <div class="card-value" id="meta-spend">0.00 ر.س</div>
                <div class="card-sub" id="meta-sub">0 محادثة/نتيجة</div>
            </div>
        </div>

        <div class="chart-section">
            <div class="chart-title" id="chart-period-title">الإنفاق حسب الحساب</div>
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
            Chart.register(ChartDataLabels);

            let chartInstance = null;
            let currentPlatform = 'all';
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

            function getLast7DaysDates() {
                let dates = [];
                for (let i = 6; i >= 0; i--) {
                    let d = new Date();
                    d.setDate(d.getDate() - i);
                    dates.push(d.toISOString().split('T')[0]);
                }
                return dates;
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

                let labels = [];
                let datasets = [];

                if (currentTimeRange === 'last7') {
                    labels = getLast7DaysDates();
                    
                    let metaDaily = labels.map(d => metaList.filter(i => i.date === d).reduce((s, i) => s + safeNum(i.spend || i.cost), 0));
                    let tiktokDaily = labels.map(d => tiktokList.filter(i => i.date === d).reduce((s, i) => s + safeNum(i.spend || i.cost), 0));
                    let googleDaily = labels.map(d => googleList.filter(i => i.date === d).reduce((s, i) => s + safeNum(i.spend || i.cost), 0));

                    datasets = [
                        { label: 'Meta Ads', data: metaDaily, backgroundColor: '#3b82f6', borderRadius: 4 },
                        { label: 'TikTok Ads', data: tiktokDaily, backgroundColor: '#ec4899', borderRadius: 4 },
                        { label: 'Google Ads', data: googleDaily, backgroundColor: '#10b981', borderRadius: 4 }
                    ];
                } else {
                    labels = ['Meta Ads', 'TikTok Ads', 'Google Ads'];
                    let mSpend = metaList.reduce((s, i) => s + safeNum(i.spend || i.cost), 0);
                    let tSpend = tiktokList.reduce((s, i) => s + safeNum(i.spend || i.cost), 0);
                    let gSpend = googleList.reduce((s, i) => s + safeNum(i.spend || i.cost), 0);

                    datasets = [{
                        data: [mSpend, tSpend, gSpend],
                        backgroundColor: ['#3b82f6', '#ec4899', '#10b981'],
                        borderRadius: 6,
                        barThickness: 50
                    }];
                }

                chartInstance = new Chart(ctx, {
                    type: 'bar',
                    data: { labels: labels, datasets: datasets },
                    options: {
                        responsive: true,
                        maintainAspectRatio: false,
                        plugins: {
                            legend: { display: currentTimeRange === 'last7', labels: { color: '#94a3b8' } },
                            datalabels: {
                                anchor: 'end',
                                align: 'top',
                                color: '#f8fafc',
                                font: { weight: 'bold', size: 10 },
                                formatter: (val) => val > 0 ? val.toFixed(0) + ' ر.س' : ''
                            }
                        },
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

                let tiktokSpend = 0, tiktokConv = 0, tiktokClicks = 0;
                tiktokList.forEach(i => {
                    tiktokSpend += safeNum(i.spend || i.cost);
                    tiktokConv += safeNum(i.conversion || i.conversions);
                    tiktokClicks += safeNum(i.clicks);
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
                document.getElementById('tiktok-sub').innerText = tiktokConv > 0 ? tiktokConv + ' تحويل' : tiktokClicks + ' نقرة';

                document.getElementById('google-spend').innerText = googleSpend.toFixed(2) + ' ر.س';
                document.getElementById('google-sub').innerText = googleConv + ' تحويلات';

                document.getElementById('total-spend').innerText = totalSpend.toFixed(2) + ' ر.س';

                let timeText = currentTimeRange === 'today' ? 'اليوم' : (currentTimeRange === 'yesterday' ? 'أمس' : 'آخر 7 أيام');
                document.getElementById('chart-period-title').innerText = `الإنفاق حسب الحساب - ${timeText}`;
                document.getElementById('update-time').innerText = `تقرير أداء (${timeText}) - آخر تحديث: ${new Date().toLocaleTimeString('ar-SA')}`;

                updateChart(metaList, tiktokList, googleList);
                renderHierarchicalTables(metaList, tiktokList, googleList);
            }

            function getTikTokBadge(spend, conv, clicks, cpc, ctr) {
                if (conv > 0) {
                    let cpa = spend / conv;
                    if (cpa <= 40) return `<span class="badge badge-good">ناجح (CPA: ${cpa.toFixed(1)} ر.س)</span>`;
                    if (cpa <= 80) return `<span class="badge badge-medium">متوسط (CPA: ${cpa.toFixed(1)} ر.س)</span>`;
                    return `<span class="badge badge-bad">مرتفع التكلفة (CPA: ${cpa.toFixed(1)} ر.س)</span>`;
                }

                if (spend === 0) return '<span class="badge badge-medium">غير نشط</span>';
                if (clicks === 0 && spend > 20) return '<span class="badge badge-bad">ضعيف (بدون نقرات)</span>';

                if (ctr >= 0.8 && cpc <= 3.0) {
                    return `<span class="badge badge-good">جودة عالية (${clicks} نقرة | CTR: ${ctr.toFixed(2)}%)</span>`;
                } else if (ctr >= 0.4 || cpc <= 4.0) {
                    return `<span class="badge badge-medium">أداء متوسط (${clicks} نقرة)</span>`;
                } else {
                    return `<span class="badge badge-bad">تكلفة مرتفعة/نقرات قليلة</span>`;
                }
            }

            // 🌳 تجميع وتسلسل البيانات شجرياً (الحملة -> المجموعة -> الإعلانات)
            function groupByTree(list, isMeta = false) {
                let tree = {};
                list.forEach(i => {
                    let cName = i.campaign || i.campaign_name || 'حملة عامة';
                    let gName = i.adset_name || i.adgroup_name || i.ad_group_name || 'المجموعة العامة';
                    let aName = i.ad_name || 'الإعلان الرئيسي';

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

                const buildPlatformTreeHtml = (title, dotClass, list, convLabel, platformKey, isTikTok = false, isMeta = false) => {
                    if (list.length === 0) return '';
                    let tree = groupByTree(list, isMeta);
                    let rowsHtml = '';
                    let cIndex = 0;

                    for (let cName in tree) {
                        cIndex++;
                        let camp = tree[cName];
                        let campId = `${platformKey}-c-${cIndex}`;

                        // سطر الحملة
                        rowsHtml += `
                            <tr class="row-campaign" onclick="toggleRow('${campId}')" id="header-${campId}">
                                <td><span class="toggle-icon">◀</span> 📂 ${cName} <span class="weekly-trend">📈 مستقر أسبوعياً</span></td>
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

                            // سطر المجموعة الإعلانية
                            rowsHtml += `
                                <tr class="row-adgroup ${campId}" onclick="toggleRow('${groupId}')" id="header-${groupId}">
                                    <td><span class="toggle-icon">◀</span> 📁 المجموعة: ${gName}</td>
                                    <td>${group.spend.toFixed(2)} ر.س</td>
                                    <td>${group.impressions.toLocaleString('ar-SA')}</td>
                                    <td>${group.clicks.toLocaleString('ar-SA')}</td>
                                    <td>${group.conv}</td>
                                </tr>
                            `;

                            for (let aName in group.ads) {
                                let ad = group.ads[aName];
                                let badgeHtml = isTikTok ? getTikTokBadge(ad.spend, ad.conv, ad.clicks, ad.cpc, ad.ctr) : '';

                                // سطر الإعلان
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
                            <div class="platform-header">
                                <span><span class="dot ${dotClass}"></span> ${title}</span>
                            </div>
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

                let html = '';
                if (currentPlatform === 'all' || currentPlatform === 'meta') {
                    html += buildPlatformTreeHtml('Meta Ads', 'dot-meta', metaList, 'المحادثات/النتائج', 'meta', false, true);
                }
                if (currentPlatform === 'all' || currentPlatform === 'tiktok') {
                    html += buildPlatformTreeHtml('TikTok Ads', 'dot-tiktok', tiktokList, 'التحويلات/المؤشرات', 'tiktok', true, false);
                }
                if (currentPlatform === 'all' || currentPlatform === 'google') {
                    html += buildPlatformTreeHtml('Google Ads', 'dot-google', googleList, 'التحويلات', 'google');
                }

                container.innerHTML = html || '<div class="platform-card" style="padding:20px; text-align:center; color:var(--text-muted);">لا توجد بيانات حملات متاحة لهذا الفلتر</div>';
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
                        // إظهار المستوى المباشر التالي فقط
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
