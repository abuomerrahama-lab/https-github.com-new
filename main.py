import os
import asyncio
import requests
import httpx
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

app = FastAPI()

WINDSOR_API_KEY = os.getenv("WINDSOR_API_KEY", "")

META_ACCOUNTS = "1085415013613251,1203619500645957"
TIKTOK_ACCOUNTS = "7477300225556824081,7438927058295996417"

# آلية Keep-Alive لمنع خمول السيرفر
@app.on_event("startup")
async def start_keep_alive():
    async def keep_alive():
        await asyncio.sleep(10)
        while True:
            render_url = os.getenv("RENDER_EXTERNAL_URL")
            if render_url:
                try:
                    async with httpx.AsyncClient() as client:
                        await client.get(render_url, timeout=10.0)
                except Exception:
                    pass
            await asyncio.sleep(300)

    asyncio.create_task(keep_alive())

@app.get("/", response_class=HTMLResponse)
def home():
    return """
    <!DOCTYPE html>
    <html lang="ar" dir="rtl">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>التقرير اليومي للإعلانات</title>
        <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
        <style>
            :root {
                --bg-main: #0d0f12;
                --bg-card: #15181e;
                --bg-card-hover: #1c2029;
                --border-color: #262b36;
                --text-main: #e2e8f0;
                --text-muted: #8a94a6;
                --accent-blue: #3b82f6;
                --accent-pink: #ec4899;
                --accent-green: #10b981;
                --accent-gold: #f59e0b;
            }
            body {
                font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
                background-color: var(--bg-main);
                color: var(--text-main);
                margin: 0;
                padding: 24px;
                direction: rtl;
            }
            .container { max-width: 1200px; margin: 0 auto; }
            
            /* Header */
            .header-bar {
                display: flex;
                justify-content: space-between;
                align-items: center;
                margin-bottom: 24px;
            }
            .header-title h1 { margin: 0; font-size: 22px; font-weight: 700; }
            .header-title p { margin: 4px 0 0; font-size: 13px; color: var(--text-muted); }
            
            .actions-group { display: flex; gap: 8px; }
            .btn {
                background: var(--bg-card);
                border: 1px solid var(--border-color);
                color: var(--text-main);
                padding: 8px 16px;
                border-radius: 6px;
                cursor: pointer;
                font-size: 13px;
                font-weight: 500;
                transition: all 0.2s;
            }
            .btn:hover { background: var(--bg-card-hover); border-color: #3b82f6; }
            .btn-accent { background: #d97706; color: #fff; border: none; }
            .btn-accent:hover { background: #b45309; }

            /* Top KPI Cards */
            .kpi-grid {
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
                gap: 12px;
                margin-bottom: 24px;
            }
            .kpi-card {
                background: var(--bg-card);
                border: 1px solid var(--border-color);
                border-radius: 8px;
                padding: 16px;
                text-align: center;
                position: relative;
            }
            .kpi-card.highlight { border-color: var(--accent-gold); }
            .kpi-card .label { font-size: 12px; color: var(--text-muted); margin-bottom: 8px; }
            .kpi-card .val { font-size: 22px; font-weight: 700; color: #fff; }
            .kpi-card .sub { font-size: 11px; color: var(--text-muted); margin-top: 4px; }

            /* Chart Card */
            .chart-card {
                background: var(--bg-card);
                border: 1px solid var(--border-color);
                border-radius: 8px;
                padding: 20px;
                margin-bottom: 24px;
            }
            .chart-title { font-size: 14px; font-weight: 600; text-align: center; margin-bottom: 16px; color: var(--text-muted); }

            /* Nav Filters */
            .filter-tabs {
                display: flex;
                justify-content: flex-end;
                gap: 8px;
                margin-bottom: 20px;
            }
            .tab-btn {
                background: var(--bg-card);
                border: 1px solid var(--border-color);
                color: var(--text-muted);
                padding: 6px 16px;
                border-radius: 20px;
                cursor: pointer;
                font-size: 13px;
            }
            .tab-btn.active {
                background: #f59e0b;
                color: #000;
                font-weight: 700;
                border-color: #f59e0b;
            }

            /* Tables & Cards Section */
            .section-card {
                background: var(--bg-card);
                border: 1px solid var(--border-color);
                border-radius: 8px;
                padding: 20px;
                margin-bottom: 24px;
            }
            .section-header {
                display: flex;
                justify-content: space-between;
                align-items: center;
                margin-bottom: 16px;
            }
            .section-header h3 { margin: 0; font-size: 16px; display: flex; align-items: center; gap: 8px; }
            .dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
            .dot-meta { background: #3b82f6; }
            .dot-tiktok { background: #ec4899; }
            .dot-google { background: #10b981; }

            table { width: 100%; border-collapse: collapse; font-size: 13px; }
            th, td { padding: 12px; text-align: right; border-bottom: 1px solid var(--border-color); }
            th { color: var(--text-muted); font-weight: 500; font-size: 12px; }
            tr:last-child td { border-bottom: none; }
            
            /* Banner Callout */
            .info-banner {
                background: #19160a;
                border: 1px solid #45320d;
                border-radius: 6px;
                padding: 12px 16px;
                margin-bottom: 24px;
                font-size: 13px;
                color: #fcd34d;
                display: flex;
                align-items: center;
                gap: 10px;
            }
        </style>
    </head>
    <body>
        <div class="container">
            <!-- Header -->
            <div class="header-bar">
                <div class="header-title">
                    <h1>التقرير اليومي للإعلانات</h1>
                    <p id="updateTime">تقرير أداء يوم أمس - آخر تحديث: جاري التحميل...</p>
                </div>
                <div class="actions-group">
                    <button class="btn btn-accent" onclick="fetchAndAnalyze()">تحديث البيانات</button>
                </div>
            </div>

            <!-- Top KPIs -->
            <div class="kpi-grid">
                <div class="kpi-card">
                    <div class="label">Meta - إجمالي الإنفاق</div>
                    <div class="val" id="metaSpend">0.00 ر.س</div>
                    <div class="sub" id="metaConv">0 محادثة</div>
                </div>
                <div class="kpi-card">
                    <div class="label">TikTok - إجمالي الإنفاق</div>
                    <div class="val" id="tiktokSpend">0.00 ر.س</div>
                    <div class="sub" id="tiktokConv">0 تحويل</div>
                </div>
                <div class="kpi-card">
                    <div class="label">Google Ads - إجمالي الإنفاق</div>
                    <div class="val" id="googleSpend">0.00 ر.س</div>
                    <div class="sub" id="googleConv">0 تحويلات</div>
                </div>
                <div class="kpi-card highlight">
                    <div class="label">إجمالي الإنفاق (كل المنصات)</div>
                    <div class="val" id="totalSpend">0.00 ر.س</div>
                    <div class="sub" id="totalAccounts">الحسابات النشطة</div>
                </div>
            </div>

            <!-- Chart -->
            <div class="chart-card">
                <div class="chart-title" id="chartLabel">الإنفاق حسب الحساب - أمس</div>
                <div style="height: 280px;">
                    <canvas id="spendChart"></canvas>
                </div>
            </div>

            <!-- Tabs Filter -->
            <div class="filter-tabs">
                <button class="tab-btn active" onclick="filterPlatform('all')">الكل</button>
                <button class="tab-btn" onclick="filterPlatform('meta')">Meta</button>
                <button class="tab-btn" onclick="filterPlatform('tiktok')">TikTok</button>
                <button class="tab-btn" onclick="filterPlatform('google')">Google Ads</button>
            </div>

            <!-- Meta Section -->
            <div class="section-card platform-sec" id="sec-meta">
                <div class="section-header">
                    <h3><span class="dot dot-meta"></span> Meta Ads</h3>
                </div>
                <table>
                    <thead>
                        <tr>
                            <th>الحملة</th>
                            <th>الإنفاق</th>
                            <th>الظهور</th>
                            <th>النقرات</th>
                            <th>المحادثات/النتائج</th>
                        </tr>
                    </thead>
                    <tbody id="metaTable">
                        <tr><td colspan="5" style="text-align:center; color:var(--text-muted);">جاري التحميل...</td></tr>
                    </tbody>
                </table>
            </div>

            <!-- TikTok Section -->
            <div class="section-card platform-sec" id="sec-tiktok">
                <div class="section-header">
                    <h3><span class="dot dot-tiktok"></span> TikTok Ads</h3>
                </div>
                <table>
                    <thead>
                        <tr>
                            <th>الحملة</th>
                            <th>الإنفاق</th>
                            <th>الظهور</th>
                            <th>النقرات</th>
                            <th>التحويلات</th>
                        </tr>
                    </thead>
                    <tbody id="tiktokTable">
                        <tr><td colspan="5" style="text-align:center; color:var(--text-muted);">جاري التحميل...</td></tr>
                    </tbody>
                </table>
            </div>

            <!-- Google Section -->
            <div class="section-card platform-sec" id="sec-google">
                <div class="section-header">
                    <h3><span class="dot dot-google"></span> Google Ads</h3>
                </div>
                <table>
                    <thead>
                        <tr>
                            <th>الحملة</th>
                            <th>الإنفاق</th>
                            <th>الظهور</th>
                            <th>النقرات</th>
                            <th>التحويلات</th>
                        </tr>
                    </thead>
                    <tbody id="googleTable">
                        <tr><td colspan="5" style="text-align:center; color:var(--text-muted);">جاري التحميل...</td></tr>
                    </tbody>
                </table>
            </div>

            <div class="info-banner">
                ⚠️ لا تقارن "محادثات Meta" بـ "تحويلات Google" كأنها نفس النتيجة – كل منصة تُعرّف النتيجة بحدث مختلف تماماً.
            </div>
        </div>

        <script>
            let myChart = null;

            async function fetchAndAnalyze() {
                try {
                    let res = await fetch('/api/data');
                    let rawData = await res.json();
                    
                    let now = new Date();
                    let yesterday = new Date(now);
                    yesterday.setDate(now.getDate() - 1);
                    let yesterdayStr = yesterday.toISOString().split('T')[0];

                    document.getElementById('updateTime').innerText = `تقرير أداء يوم ${yesterdayStr} - آخر تحديث: ${now.toLocaleTimeString('ar-SA')}`;

                    let mSpend = 0, mConv = 0;
                    let tSpend = 0, tConv = 0;
                    let gSpend = 0, gConv = 0;

                    let chartLabels = [];
                    let chartData = [];
                    let chartColors = [];

                    // 1. Meta
                    let mHtml = '';
                    if (rawData.meta_ads && rawData.meta_ads.data) {
                        let mRows = rawData.meta_ads.data.filter(r => r.date === yesterdayStr && (r.spend > 0 || r.clicks > 0));
                        mRows.forEach(r => {
                            let sp = parseFloat(r.spend || 0);
                            let cv = parseFloat(r.conversions || 0);
                            mSpend += sp;
                            mConv += cv;
                            mHtml += `<tr>
                                <td><strong>${r.campaign_name}</strong></td>
                                <td>${sp.toFixed(2)} ر.س</td>
                                <td>${Number(r.impressions||0).toLocaleString()}</td>
                                <td>${Number(r.clicks||0).toLocaleString()}</td>
                                <td>${cv}</td>
                            </tr>`;
                        });
                        if (mSpend > 0) {
                            chartLabels.push('Meta Ads');
                            chartData.push(mSpend);
                            chartColors.push('#3b82f6');
                        }
                    }
                    document.getElementById('metaSpend').innerText = mSpend.toFixed(2) + ' ر.س';
                    document.getElementById('metaConv').innerText = mConv + ' محادثة/نتيجة';
                    document.getElementById('metaTable').innerHTML = mHtml || '<tr><td colspan="5" style="text-align:center">لا توجد حملات نشطة بتاريخ أمس</td></tr>';

                    // 2. TikTok
                    let tHtml = '';
                    if (rawData.tiktok_ads && rawData.tiktok_ads.data) {
                        let tRows = rawData.tiktok_ads.data.filter(r => r.date === yesterdayStr && (r.spend > 0 || r.clicks > 0));
                        tRows.forEach(r => {
                            let sp = parseFloat(r.spend || 0);
                            let cv = parseFloat(r.conversions || 0);
                            tSpend += sp;
                            tConv += cv;
                            tHtml += `<tr>
                                <td><strong>${r.campaign_name}</strong></td>
                                <td>${sp.toFixed(2)} ر.س</td>
                                <td>${Number(r.impressions||0).toLocaleString()}</td>
                                <td>${Number(r.clicks||0).toLocaleString()}</td>
                                <td>${cv}</td>
                            </tr>`;
                        });
                        if (tSpend > 0) {
                            chartLabels.push('TikTok Ads');
                            chartData.push(tSpend);
                            chartColors.push('#ec4899');
                        }
                    }
                    document.getElementById('tiktokSpend').innerText = tSpend.toFixed(2) + ' ر.س';
                    document.getElementById('tiktokConv').innerText = tConv + ' تحويل';
                    document.getElementById('tiktokTable').innerHTML = tHtml || '<tr><td colspan="5" style="text-align:center">لا توجد حملات نشطة بتاريخ أمس</td></tr>';

                    // 3. Google
                    let gHtml = '';
                    if (rawData.google_ads && rawData.google_ads.data) {
                        let gRows = rawData.google_ads.data.filter(r => r.date === yesterdayStr && (r.spend > 0 || r.clicks > 0));
                        gRows.forEach(r => {
                            let sp = parseFloat(r.spend || 0);
                            let cv = parseFloat(r.conversions || 0);
                            gSpend += sp;
                            gConv += cv;
                            gHtml += `<tr>
                                <td><strong>${r.campaign_name}</strong></td>
                                <td>${sp.toFixed(2)} ر.س</td>
                                <td>${Number(r.impressions||0).toLocaleString()}</td>
                                <td>${Number(r.clicks||0).toLocaleString()}</td>
                                <td>${cv}</td>
                            </tr>`;
                        });
                        if (gSpend > 0) {
                            chartLabels.push('Google Ads');
                            chartData.push(gSpend);
                            chartColors.push('#10b981');
                        }
                    }
                    document.getElementById('googleSpend').innerText = gSpend.toFixed(2) + ' ر.س';
                    document.getElementById('googleConv').innerText = gConv + ' تحويلات';
                    document.getElementById('googleTable').innerHTML = gHtml || '<tr><td colspan="5" style="text-align:center">لا توجد حملات نشطة بتاريخ أمس</td></tr>';

                    // Totals
                    let total = mSpend + tSpend + gSpend;
                    document.getElementById('totalSpend').innerText = total.toFixed(2) + ' ر.س';

                    // Render Chart
                    renderChart(chartLabels, chartData, chartColors);

                } catch(e) {
                    console.error(e);
                }
            }

            function renderChart(labels, data, colors) {
                const ctx = document.getElementById('spendChart').getContext('2d');
                if (myChart) myChart.destroy();

                myChart = new Chart(ctx, {
                    type: 'bar',
                    data: {
                        labels: labels.length ? labels : ['لا توجد بيانات'],
                        datasets: [{
                            data: data.length ? data : [0],
                            backgroundColor: colors.length ? colors : ['#334155'],
                            borderRadius: 6,
                            barThickness: 50
                        }]
                    },
                    options: {
                        responsive: true,
                        maintainAspectRatio: false,
                        plugins: { legend: { display: false } },
                        scales: {
                            x: { grid: { display: false }, ticks: { color: '#8a94a6' } },
                            y: { grid: { color: '#262b36' }, ticks: { color: '#8a94a6' } }
                        }
                    }
                });
            }

            function filterPlatform(p) {
                document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
                event.target.classList.add('active');

                let secs = document.querySelectorAll('.platform-sec');
                if (p === 'all') {
                    secs.forEach(s => s.style.display = 'block');
                } else {
                    secs.forEach(s => s.style.display = 'none');
                    let target = document.getElementById('sec-' + p);
                    if (target) target.style.display = 'block';
                }
            }

            fetchAndAnalyze();
        </script>
    </body>
    </html>
    """

@app.get("/api/data")
def get_data():
    if not WINDSOR_API_KEY:
        return {"error": "WINDSOR_API_KEY غير معرف"}
    
    results = {}
    meta_params = f"&fields=date,campaign_name,clicks,impressions,spend,conversions,campaign_status,account_id&date_preset=last_30d&account_id={META_ACCOUNTS}"
    tiktok_params = f"&fields=date,campaign_name,clicks,impressions,spend,conversions,campaign_status,account_id,advertiser_id&date_preset=last_30d&account_id={TIKTOK_ACCOUNTS}"
    general_params = "&fields=date,campaign_name,clicks,impressions,spend,conversions,campaign_status&date_preset=last_30d"

    try:
        results["meta_ads"] = requests.get(f"https://connectors.windsor.ai/facebook?api_key={WINDSOR_API_KEY}{meta_params}").json()
    except Exception as e:
        results["meta_ads"] = {"error": str(e)}

    try:
        results["tiktok_ads"] = requests.get(f"https://connectors.windsor.ai/tiktok?api_key={WINDSOR_API_KEY}{tiktok_params}").json()
    except Exception as e:
        results["tiktok_ads"] = {"error": str(e)}

    try:
        results["google_ads"] = requests.get(f"https://connectors.windsor.ai/google_ads?api_key={WINDSOR_API_KEY}{general_params}").json()
    except Exception as e:
        results["google_ads"] = {"error": str(e)}

    return results
