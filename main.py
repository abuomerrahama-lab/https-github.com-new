import os
import asyncio
from contextlib import asynccontextmanager
import httpx
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

WINDSOR_API_KEY = os.getenv("WINDSOR_API_KEY", "")

META_ACCOUNTS = "1085415013613251,1203619500645957"
TIKTOK_ACCOUNTS = "7477300225556824081,7438927058295996417"

@asynccontextmanager
async def lifespan(app: FastAPI):
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

    task = asyncio.create_task(keep_alive())
    yield
    task.cancel()

app = FastAPI(lifespan=lifespan)

@app.get("/", response_class=HTMLResponse)
def home():
    return """
    <!DOCTYPE html>
    <html lang="ar" dir="rtl">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>لوحة الأداء الذكية للإعلانات | Smart Ads Dashboard</title>
        <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
        <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css" rel="stylesheet">
        <style>
            :root {
                --bg-main: #090a0f;
                --bg-card: #12151e;
                --bg-card-hover: #1a1e2b;
                --border-color: #232838;
                --text-main: #f1f5f9;
                --text-muted: #94a3b8;
                --accent-blue: #3b82f6;
                --accent-pink: #ec4899;
                --accent-green: #10b981;
                --accent-gold: #f59e0b;
                --accent-red: #ef4444;
            }
            body {
                font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
                background-color: var(--bg-main);
                color: var(--text-main);
                margin: 0;
                padding: 24px;
                direction: rtl;
            }
            .container { max-width: 1300px; margin: 0 auto; }
            
            /* Header */
            .header-bar {
                display: flex;
                justify-content: space-between;
                align-items: center;
                margin-bottom: 24px;
                background: var(--bg-card);
                padding: 20px 24px;
                border-radius: 12px;
                border: 1px solid var(--border-color);
            }
            .header-title h1 { margin: 0; font-size: 22px; font-weight: 800; color: #fff; }
            .header-title p { margin: 6px 0 0; font-size: 13px; color: var(--text-muted); }
            
            .actions-group { display: flex; gap: 12px; align-items: center; }
            .btn {
                background: var(--bg-card);
                border: 1px solid var(--border-color);
                color: var(--text-main);
                padding: 9px 16px;
                border-radius: 8px;
                cursor: pointer;
                font-size: 13px;
                font-weight: 600;
                display: inline-flex;
                align-items: center;
                gap: 8px;
                transition: all 0.2s;
            }
            .btn:hover { background: var(--bg-card-hover); border-color: var(--accent-blue); }
            .btn-accent { background: linear-gradient(135deg, #d97706, #b45309); color: #fff; border: none; }
            .btn-accent:hover { opacity: 0.9; }
            .btn-copy { background: linear-gradient(135deg, #10b981, #047857); color: #fff; border: none; }
            .btn-copy:hover { opacity: 0.9; }

            /* Time Filters */
            .time-selector {
                display: flex;
                background: var(--bg-main);
                padding: 4px;
                border-radius: 8px;
                border: 1px solid var(--border-color);
            }
            .time-btn {
                background: transparent;
                border: none;
                color: var(--text-muted);
                padding: 6px 14px;
                border-radius: 6px;
                cursor: pointer;
                font-size: 12px;
                font-weight: 600;
            }
            .time-btn.active { background: var(--border-color); color: #fff; }

            /* Top KPI Cards */
            .kpi-grid {
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(230px, 1fr));
                gap: 16px;
                margin-bottom: 24px;
            }
            .kpi-card {
                background: var(--bg-card);
                border: 1px solid var(--border-color);
                border-radius: 12px;
                padding: 20px;
                position: relative;
                transition: transform 0.2s;
            }
            .kpi-card:hover { transform: translateY(-2px); }
            .kpi-card.highlight { border-color: var(--accent-gold); background: linear-gradient(180deg, rgba(245,158,11,0.05) 0%, var(--bg-card) 100%); }
            .kpi-card .label { font-size: 13px; color: var(--text-muted); margin-bottom: 10px; display: flex; justify-content: space-between; align-items: center; }
            .kpi-card .val { font-size: 26px; font-weight: 800; color: #fff; }
            .kpi-card .sub { font-size: 12px; color: var(--text-muted); margin-top: 6px; display: flex; gap: 8px; }
            .kpi-card .badge { padding: 2px 6px; border-radius: 4px; font-size: 11px; font-weight: 700; }
            .badge-blue { background: rgba(59,130,246,0.15); color: var(--accent-blue); }
            .badge-pink { background: rgba(236,72,153,0.15); color: var(--accent-pink); }
            .badge-green { background: rgba(16,185,129,0.15); color: var(--accent-green); }

            /* Smart Diagnostics Box */
            .diag-box {
                background: var(--bg-card);
                border: 1px solid var(--border-color);
                border-radius: 12px;
                padding: 20px;
                margin-bottom: 24px;
            }
            .diag-title { font-size: 15px; font-weight: 700; margin-bottom: 14px; display: flex; align-items: center; gap: 8px; color: var(--accent-gold); }
            .diag-list { display: flex; flex-direction: column; gap: 10px; }
            .diag-item {
                padding: 12px 16px;
                border-radius: 8px;
                font-size: 13px;
                display: flex;
                align-items: center;
                gap: 12px;
            }
            .diag-red { background: rgba(239,68,68,0.1); border: 1px solid rgba(239,68,68,0.25); color: #fca5a5; }
            .diag-green { background: rgba(16,185,129,0.1); border: 1px solid rgba(16,185,129,0.25); color: #6ee7b7; }
            .diag-yellow { background: rgba(245,158,11,0.1); border: 1px solid rgba(245,158,11,0.25); color: #fde047; }

            /* Chart Card */
            .chart-card {
                background: var(--bg-card);
                border: 1px solid var(--border-color);
                border-radius: 12px;
                padding: 24px;
                margin-bottom: 24px;
            }
            .chart-title { font-size: 15px; font-weight: 700; text-align: center; margin-bottom: 20px; color: var(--text-muted); }

            /* Tables Section */
            .section-card {
                background: var(--bg-card);
                border: 1px solid var(--border-color);
                border-radius: 12px;
                padding: 24px;
                margin-bottom: 24px;
            }
            .section-header {
                display: flex;
                justify-content: space-between;
                align-items: center;
                margin-bottom: 20px;
            }
            .section-header h3 { margin: 0; font-size: 17px; font-weight: 700; display: flex; align-items: center; gap: 10px; }
            .dot { width: 10px; height: 10px; border-radius: 50%; display: inline-block; }
            .dot-meta { background: var(--accent-blue); }
            .dot-tiktok { background: var(--accent-pink); }
            .dot-google { background: var(--accent-green); }

            table { width: 100%; border-collapse: collapse; font-size: 13px; }
            th, td { padding: 14px 12px; text-align: right; border-bottom: 1px solid var(--border-color); }
            th { color: var(--text-muted); font-weight: 600; font-size: 12px; background: rgba(255,255,255,0.02); }
            tr:hover td { background: var(--bg-card-hover); }
            
            .text-green { color: var(--accent-green); font-weight: 700; }
            .text-gold { color: var(--accent-gold); font-weight: 700; }
            .toast {
                position: fixed;
                bottom: 20px;
                left: 20px;
                background: #10b981;
                color: #fff;
                padding: 12px 24px;
                border-radius: 8px;
                font-size: 14px;
                font-weight: 700;
                display: none;
                box-shadow: 0 4px 12px rgba(0,0,0,0.3);
            }
        </style>
    </head>
    <body>
        <div class="container">
            <!-- Header -->
            <div class="header-bar">
                <div class="header-title">
                    <h1><i class="fa-solid fa-chart-pie" style="color: var(--accent-gold); margin-left:8px;"></i> لوحة تحليلات الإعلانات المتقدمة</h1>
                    <p id="updateTime">جاري تحميل البيانات...</p>
                </div>
                <div class="actions-group">
                    <div class="time-selector">
                        <button class="time-btn" onclick="setTimeRange('today')">اليوم</button>
                        <button class="time-btn active" onclick="setTimeRange('yesterday')">أمس</button>
                        <button class="time-btn" onclick="setTimeRange('last7')">آخر 7 أيام</button>
                    </div>
                    <button class="btn btn-accent" onclick="fetchAndAnalyze()"><i class="fa-solid fa-rotate"></i> تحديث</button>
                    <button class="btn btn-copy" onclick="copyDataForAI()"><i class="fa-solid fa-copy"></i> نسخ التقرير للذكاء الاصطناعي</button>
                </div>
            </div>

            <!-- Top KPIs -->
            <div class="kpi-grid">
                <div class="kpi-card">
                    <div class="label"><span>Meta Ads</span> <i class="fa-brands fa-meta" style="color: var(--accent-blue);"></i></div>
                    <div class="val" id="metaSpend">0.00 ر.س</div>
                    <div class="sub"><span class="badge badge-blue" id="metaConv">0 محادثة</span> <span id="metaCpc">CPC: 0.00</span></div>
                </div>
                <div class="kpi-card">
                    <div class="label"><span>TikTok Ads</span> <i class="fa-brands fa-tiktok" style="color: var(--accent-pink);"></i></div>
                    <div class="val" id="tiktokSpend">0.00 ر.س</div>
                    <div class="sub"><span class="badge badge-pink" id="tiktokConv">0 تحويل</span> <span id="tiktokCpc">CPC: 0.00</span></div>
                </div>
                <div class="kpi-card">
                    <div class="label"><span>Google Ads</span> <i class="fa-brands fa-google" style="color: var(--accent-green);"></i></div>
                    <div class="val" id="googleSpend">0.00 ر.س</div>
                    <div class="sub"><span class="badge badge-green" id="googleConv">0 تحويلات</span> <span id="googleCpc">CPC: 0.00</span></div>
                </div>
                <div class="kpi-card highlight">
                    <div class="label"><span>إجمالي الإنفاق الكلي</span> <i class="fa-solid fa-wallet" style="color: var(--accent-gold);"></i></div>
                    <div class="val" id="totalSpend">0.00 ر.س</div>
                    <div class="sub"><span id="totalConversions">النتائج الإجمالية: 0</span></div>
                </div>
            </div>

            <!-- Smart Diagnostics Box -->
            <div class="diag-box">
                <div class="diag-title"><i class="fa-solid fa-lightbulb"></i> التحليل البرمجي التلقائي للحملات</div>
                <div class="diag-list" id="diagList">
                    <div class="diag-item diag-yellow"><i class="fa-solid fa-spinner fa-spin"></i> جاري فحص البيانات واستخراج التوصيات...</div>
                </div>
            </div>

            <!-- Chart -->
            <div class="chart-card">
                <div class="chart-title">مقارنة الإنفاق والنتائج حسب المنصة</div>
                <div style="height: 280px;">
                    <canvas id="spendChart"></canvas>
                </div>
            </div>

            <!-- Meta Section -->
            <div class="section-card">
                <div class="section-header">
                    <h3><span class="dot dot-meta"></span> Meta Ads - أداء الحملات والرسائل</h3>
                </div>
                <table>
                    <thead>
                        <tr>
                            <th>الحملة</th>
                            <th>الإنفاق</th>
                            <th>الظهور</th>
                            <th>النقرات</th>
                            <th>CPC</th>
                            <th>المحادثات / الرسائل</th>
                            <th>تكلفة الرسالة (CPA)</th>
                        </tr>
                    </thead>
                    <tbody id="metaTable">
                        <tr><td colspan="7" style="text-align:center; color:var(--text-muted);">جاري التحميل...</td></tr>
                    </tbody>
                </table>
            </div>

            <!-- TikTok Section -->
            <div class="section-card">
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
                            <th>CPC</th>
                            <th>التحويلات</th>
                            <th>تكلفة التحويل</th>
                        </tr>
                    </thead>
                    <tbody id="tiktokTable">
                        <tr><td colspan="7" style="text-align:center; color:var(--text-muted);">جاري التحميل...</td></tr>
                    </tbody>
                </table>
            </div>

            <!-- Google Section -->
            <div class="section-card">
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
                            <th>CPC</th>
                            <th>التحويلات</th>
                            <th>تكلفة التحويل</th>
                        </tr>
                    </thead>
                    <tbody id="googleTable">
                        <tr><td colspan="7" style="text-align:center; color:var(--text-muted);">جاري التحميل...</td></tr>
                    </tbody>
                </table>
            </div>
        </div>

        <div class="toast" id="toast">✅ تم نسخ تقرير الإعلانات إلى الحافظة بنجاح!</div>

        <script>
            let rawDataCache = null;
            let currentRange = 'yesterday';
            let myChart = null;
            let currentSummaryText = "";

            function setTimeRange(range) {
                currentRange = range;
                document.querySelectorAll('.time-btn').forEach(b => b.classList.remove('active'));
                event.target.classList.add('active');
                if (rawDataCache) renderData(rawDataCache);
            }

            function getDateList(range) {
                let dates = [];
                let now = new Date();
                if (range === 'today') {
                    dates.push(now.toISOString().split('T')[0]);
                } else if (range === 'yesterday') {
                    let y = new Date(now); y.setDate(now.getDate() - 1);
                    dates.push(y.toISOString().split('T')[0]);
                } else if (range === 'last7') {
                    for (let i = 1; i <= 7; i++) {
                        let d = new Date(now); d.setDate(now.getDate() - i);
                        dates.push(d.toISOString().split('T')[0]);
                    }
                }
                return dates;
            }

            async function fetchAndAnalyze() {
                try {
                    let res = await fetch('/api/data');
                    rawDataCache = await res.json();
                    renderData(rawDataCache);
                } catch(e) {
                    console.error(e);
                }
            }

            function extractMetaConversations(row) {
                let cv = parseFloat(row.conversions || 0);
                if (row.actions && Array.isArray(row.actions)) {
                    let msgAction = row.actions.find(a => 
                        a.action_type.includes('messaging_conversation_started') || 
                        a.action_type.includes('onsite_conversion.messaging') ||
                        a.action_type.includes('lead')
                    );
                    if (msgAction) cv = Math.max(cv, parseFloat(msgAction.value || 0));
                }
                return cv;
            }

            function renderData(data) {
                let targetDates = getDateList(currentRange);
                document.getElementById('updateTime').innerText = `النطاق المحدد: ${targetDates.length === 1 ? targetDates[0] : 'آخر 7 أيام'} - آخر تحديث: ${new Date().toLocaleTimeString('ar-SA')}`;

                let mSpend = 0, mConv = 0, mClicks = 0;
                let tSpend = 0, tConv = 0, tClicks = 0;
                let gSpend = 0, gConv = 0, gClicks = 0;

                let allCampaigns = [];

                // Meta
                let mHtml = '';
                if (data.meta_ads && data.meta_ads.data) {
                    let rows = data.meta_ads.data.filter(r => targetDates.includes(r.date) && (r.spend > 0 || r.clicks > 0));
                    rows.forEach(r => {
                        let sp = parseFloat(r.spend || 0);
                        let clk = parseInt(r.clicks || 0);
                        let cv = extractMetaConversations(r);
                        mSpend += sp; mConv += cv; mClicks += clk;
                        
                        let cpc = clk > 0 ? (sp / clk) : 0;
                        let cpa = cv > 0 ? (sp / cv) : 0;

                        allCampaigns.push({ platform: 'Meta', name: r.campaign_name, spend: sp, clicks: clk, conv: cv, cpc: cpc, cpa: cpa });

                        mHtml += `<tr>
                            <td><strong>${r.campaign_name}</strong></td>
                            <td>${sp.toFixed(2)} ر.س</td>
                            <td>${Number(r.impressions||0).toLocaleString()}</td>
                            <td>${clk.toLocaleString()}</td>
                            <td>${cpc.toFixed(2)} ر.س</td>
                            <td class="text-green">${cv}</td>
                            <td class="text-gold">${cpa === 0 ? '-' : cpa.toFixed(2) + ' ر.س'}</td>
                        </tr>`;
                    });
                }
                document.getElementById('metaSpend').innerText = mSpend.toFixed(2) + ' ر.س';
                document.getElementById('metaConv').innerText = mConv + ' محادثة / رسالة';
                document.getElementById('metaCpc').innerText = 'CPC: ' + (mClicks > 0 ? (mSpend/mClicks).toFixed(2) : '0.00') + ' ر.س';
                document.getElementById('metaTable').innerHTML = mHtml || '<tr><td colspan="7" style="text-align:center">لا توجد بيانات للفترة المحددة</td></tr>';

                // TikTok
                let tHtml = '';
                if (data.tiktok_ads && data.tiktok_ads.data) {
                    let rows = data.tiktok_ads.data.filter(r => targetDates.includes(r.date) && (r.spend > 0 || r.clicks > 0));
                    rows.forEach(r => {
                        let sp = parseFloat(r.spend || 0);
                        let clk = parseInt(r.clicks || 0);
                        let cv = parseFloat(r.conversions || 0);
                        tSpend += sp; tConv += cv; tClicks += clk;

                        let cpc = clk > 0 ? (sp / clk) : 0;
                        let cpa = cv > 0 ? (sp / cv) : 0;

                        allCampaigns.push({ platform: 'TikTok', name: r.campaign_name, spend: sp, clicks: clk, conv: cv, cpc: cpc, cpa: cpa });

                        tHtml += `<tr>
                            <td><strong>${r.campaign_name}</strong></td>
                            <td>${sp.toFixed(2)} ر.س</td>
                            <td>${Number(r.impressions||0).toLocaleString()}</td>
                            <td>${clk.toLocaleString()}</td>
                            <td>${cpc.toFixed(2)} ر.س</td>
                            <td class="text-green">${cv}</td>
                            <td class="text-gold">${cpa === 0 ? '-' : cpa.toFixed(2) + ' ر.س'}</td>
                        </tr>`;
                    });
                }
                document.getElementById('tiktokSpend').innerText = tSpend.toFixed(2) + ' ر.س';
                document.getElementById('tiktokConv').innerText = tConv + ' تحويل';
                document.getElementById('tiktokCpc').innerText = 'CPC: ' + (tClicks > 0 ? (tSpend/tClicks).toFixed(2) : '0.00') + ' ر.س';
                document.getElementById('tiktokTable').innerHTML = tHtml || '<tr><td colspan="7" style="text-align:center">لا توجد بيانات للفترة المحددة</td></tr>';

                // Google
                let gHtml = '';
                if (data.google_ads && data.google_ads.data) {
                    let rows = data.google_ads.data.filter(r => targetDates.includes(r.date) && (r.spend > 0 || r.clicks > 0));
                    rows.forEach(r => {
                        let sp = parseFloat(r.spend || 0);
                        let clk = parseInt(r.clicks || 0);
                        let cv = parseFloat(r.conversions || 0);
                        gSpend += sp; gConv += cv; gClicks += clk;

                        let cpc = clk > 0 ? (sp / clk) : 0;
                        let cpa = cv > 0 ? (sp / cv) : 0;

                        allCampaigns.push({ platform: 'Google', name: r.campaign_name, spend: sp, clicks: clk, conv: cv, cpc: cpc, cpa: cpa });

                        gHtml += `<tr>
                            <td><strong>${r.campaign_name}</strong></td>
                            <td>${sp.toFixed(2)} ر.س</td>
                            <td>${Number(r.impressions||0).toLocaleString()}</td>
                            <td>${clk.toLocaleString()}</td>
                            <td>${cpc.toFixed(2)} ر.س</td>
                            <td class="text-green">${cv}</td>
                            <td class="text-gold">${cpa === 0 ? '-' : cpa.toFixed(2) + ' ر.س'}</td>
                        </tr>`;
                    });
                }
                document.getElementById('googleSpend').innerText = gSpend.toFixed(2) + ' ر.س';
                document.getElementById('googleConv').innerText = gConv + ' تحويلات';
                document.getElementById('googleCpc').innerText = 'CPC: ' + (gClicks > 0 ? (gSpend/gClicks).toFixed(2) : '0.00') + ' ر.س';
                document.getElementById('googleTable').innerHTML = gHtml || '<tr><td colspan="7" style="text-align:center">لا توجد بيانات للفترة المحددة</td></tr>';

                // Total
                let totalSp = mSpend + tSpend + gSpend;
                let totalCv = mConv + tConv + gConv;
                document.getElementById('totalSpend').innerText = totalSp.toFixed(2) + ' ر.س';
                document.getElementById('totalConversions').innerText = `إجمالي التحويلات والمحادثات: ${totalCv}`;

                // Run Rule-Based Diagnostics
                runDiagnostics(allCampaigns);

                // Build Summary text for copying
                currentSummaryText = `تقرير أداء الإعلانات للفترة (${targetDates.length === 1 ? targetDates[0] : 'آخر 7 أيام'}):\n` +
                    `- إجمالي الإنفاق: ${totalSp.toFixed(2)} ر.س\n` +
                    `- إجمالي النتائج: ${totalCv}\n` +
                    `- Meta: إنفاق ${mSpend.toFixed(2)} ر.س | نتائج ${mConv}\n` +
                    `- TikTok: إنفاق ${tSpend.toFixed(2)} ر.س | نتائج ${tConv}\n` +
                    `- Google: إنفاق ${gSpend.toFixed(2)} ر.س | نتائج ${gConv}\n\n` +
                    `تفاصيل الحملات النشطة:\n` +
                    allCampaigns.map(c => `• [${c.platform}] ${c.name}: إنفاق ${c.spend.toFixed(2)} ر.س، نقرات ${c.clicks} (CPC: ${c.cpc.toFixed(2)}ر.س)، نتائج ${c.conv} (CPA: ${c.cpa === 0 ? '-' : c.cpa.toFixed(2) + 'ر.س'})`).join('\n') +
                    `\n\nالمطلوب: قم بتزويدي بتحليل استراتيجي وتوصيات لتطوير الأداء وتوزيع الميزانية بشكل أفضل.`;

                renderChart(['Meta Ads', 'TikTok Ads', 'Google Ads'], [mSpend, tSpend, gSpend]);
            }

            function runDiagnostics(campaigns) {
                let diagList = document.getElementById('diagList');
                let items = [];

                if (campaigns.length === 0) {
                    diagList.innerHTML = '<div class="diag-item diag-yellow"><i class="fa-solid fa-info-circle"></i> لا توجد حملات نشطة للفترة المحددة ليتم تحليلها.</div>';
                    return;
                }

                // Rule 1: Wasted spend (Spend > 100 & Conv == 0)
                let wasted = campaigns.filter(c => c.spend > 100 && c.conv === 0);
                wasted.forEach(c => {
                    items.push(`<div class="diag-item diag-red"><i class="fa-solid fa-triangle-exclamation"></i> <strong>تنبيه إهدار [${c.platform}]:</strong> الحملة <strong>"${c.name}"</strong> أنفقت ${c.spend.toFixed(2)} ر.س بدون تحقيق أي نتيجة. يُنصح بتعديل الاستهداف أو إيقافها.</div>`);
                });

                // Rule 2: High efficiency (Best CPA)
                let winning = campaigns.filter(c => c.conv > 0).sort((a,b) => a.cpa - b.cpa);
                if (winning.length > 0) {
                    let best = winning[0];
                    items.push(`<div class="diag-item diag-green"><i class="fa-solid fa-circle-check"></i> <strong>حملة متميزة [${best.platform}]:</strong> الحملة <strong>"${best.name}"</strong> حققت أفضل تكلفة نتيجة بـ (${best.cpa.toFixed(2)} ر.س/نتيجة). فرصة لزيادة الميزانية بها.</div>`);
                }

                // Rule 3: High CPC (> 2.50 SAR)
                let highCpc = campaigns.filter(c => c.clicks > 10 && c.cpc > 2.50);
                highCpc.forEach(c => {
                    items.push(`<div class="diag-item diag-yellow"><i class="fa-solid fa-triangle-exclamation"></i> <strong>ارتفاع تكلفة النقرة [${c.platform}]:</strong> الحملة <strong>"${c.name}"</strong> سجلت CPC مرتفع بـ (${c.cpc.toFixed(2)} ر.س). يرجى مراجعة جودة الإعلان والمحتوى.</div>`);
                });

                if (items.length === 0) {
                    items.push('<div class="diag-item diag-green"><i class="fa-solid fa-shield-halved"></i> <strong>الأداء مستقر:</strong> جميع الحملات تعمل ضمن معدلات كفاءة متوازنة ولاتوجد أي تنبيهات حرجة اليوم.</div>');
                }

                diagList.innerHTML = items.join('');
            }

            function renderChart(labels, spendData) {
                const ctx = document.getElementById('spendChart').getContext('2d');
                if (myChart) myChart.destroy();

                myChart = new Chart(ctx, {
                    type: 'bar',
                    data: {
                        labels: labels,
                        datasets: [{
                            label: 'الإنفاق (ر.س)',
                            data: spendData,
                            backgroundColor: ['#3b82f6', '#ec4899', '#10b981'],
                            borderRadius: 8,
                            barThickness: 40
                        }]
                    },
                    options: {
                        responsive: true,
                        maintainAspectRatio: false,
                        plugins: { legend: { display: false } },
                        scales: {
                            x: { grid: { display: false }, ticks: { color: '#94a3b8' } },
                            y: { grid: { color: '#232838' }, ticks: { color: '#94a3b8' } }
                        }
                    }
                });
            }

            function copyDataForAI() {
                if (!currentSummaryText) return;
                navigator.clipboard.writeText(currentSummaryText).then(() => {
                    let toast = document.getElementById('toast');
                    toast.style.display = 'block';
                    setTimeout(() => { toast.style.display = 'none'; }, 3000);
                });
            }

            fetchAndAnalyze();
        </script>
    </body>
    </html>
    """

@app.get("/api/data")
async def get_data():
    if not WINDSOR_API_KEY:
        return {"error": "WINDSOR_API_KEY غير معرف"}
    
    results = {}
    meta_params = f"&fields=date,campaign_name,clicks,impressions,spend,conversions,actions,campaign_status,account_id&date_preset=last_30d&account_id={META_ACCOUNTS}"
    tiktok_params = f"&fields=date,campaign_name,clicks,impressions,spend,conversions,campaign_status,account_id,advertiser_id&date_preset=last_30d&account_id={TIKTOK_ACCOUNTS}"
    general_params = "&fields=date,campaign_name,clicks,impressions,spend,conversions,campaign_status&date_preset=last_30d"

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            r = await client.get(f"https://connectors.windsor.ai/facebook?api_key={WINDSOR_API_KEY}{meta_params}")
            results["meta_ads"] = r.json()
        except Exception as e:
            results["meta_ads"] = {"error": str(e)}

        try:
            r = await client.get(f"https://connectors.windsor.ai/tiktok?api_key={WINDSOR_API_KEY}{tiktok_params}")
            results["tiktok_ads"] = r.json()
        except Exception as e:
            results["tiktok_ads"] = {"error": str(e)}

        try:
            r = await client.get(f"https://connectors.windsor.ai/google_ads?api_key={WINDSOR_API_KEY}{general_params}")
            results["google_ads"] = r.json()
        except Exception as e:
            results["google_ads"] = {"error": str(e)}

    return results
