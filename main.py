import os
import time
import asyncio
from contextlib import asynccontextmanager
import httpx
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

# المفاتيح الحسابية
WINDSOR_API_KEY = os.getenv("WINDSOR_API_KEY", "")
META_ACCOUNTS = "1085415013613251,1203619500645957"
TIKTOK_ACCOUNTS = "7477300225556824081,7438927058295996417"

# الذاكرة المؤقتة للسرعة الفائقة
CACHE_DATA = None
LAST_FETCH_TIME = 0
CACHE_DURATION = 300  # التخزين لمدة 5 دقائق

@asynccontextmanager
async def lifespan(app: FastAPI):
    # بوت التنشيط التلقائي لمنع خمول Render
    async def keep_alive():
        await asyncio.sleep(15)
        while True:
            render_url = os.getenv("RENDER_EXTERNAL_URL")
            if render_url:
                try:
                    async with httpx.AsyncClient() as client:
                        await client.get(render_url, timeout=10.0)
                        print("⚡ Keep-alive ping sent successfully!")
                except Exception as e:
                    print(f"⚠️ Keep-alive ping failed: {e}")
            await asyncio.sleep(600) # يكرر التنشيط كل 10 دقائق

    task = asyncio.create_task(keep_alive())
    yield
    task.cancel()

app = FastAPI(lifespan=lifespan)

@app.get("/api/data")
async def get_data():
    global CACHE_DATA, LAST_FETCH_TIME
    
    if not WINDSOR_API_KEY:
        return {"error": "WINDSOR_API_KEY غير معرف"}
    
    now = time.time()
    if CACHE_DATA and (now - LAST_FETCH_TIME < CACHE_DURATION):
        return CACHE_DATA

    meta_params = f"&fields=date,campaign_name,clicks,impressions,spend,conversions,actions&date_preset=last_7d&account_id={META_ACCOUNTS}"
    # إضافة ctr صراحة في حقول تيك توك
    tiktok_params = f"&fields=date,campaign_name,clicks,impressions,spend,conversions,ctr,cpc&date_preset=last_7d&account_id={TIKTOK_ACCOUNTS}"
    google_params = "&fields=date,campaign_name,clicks,impressions,spend,conversions&date_preset=last_7d"

    async with httpx.AsyncClient(timeout=20.0) as client:
        meta_req = client.get(f"https://connectors.windsor.ai/facebook?api_key={WINDSOR_API_KEY}{meta_params}")
        tiktok_req = client.get(f"https://connectors.windsor.ai/tiktok?api_key={WINDSOR_API_KEY}{tiktok_params}")
        google_req = client.get(f"https://connectors.windsor.ai/google_ads?api_key={WINDSOR_API_KEY}{google_params}")

        res_meta, res_tiktok, res_google = await asyncio.gather(
            meta_req, tiktok_req, google_req, return_exceptions=True
        )

        results = {
            "meta_ads": res_meta.json() if isinstance(res_meta, httpx.Response) and res_meta.status_code == 200 else {"data": []},
            "tiktok_ads": res_tiktok.json() if isinstance(res_tiktok, httpx.Response) and res_tiktok.status_code == 200 else {"data": []},
            "google_ads": res_google.json() if isinstance(res_google, httpx.Response) and res_google.status_code == 200 else {"data": []}
        }

    CACHE_DATA = results
    LAST_FETCH_TIME = now
    return results

@app.get("/", response_class=HTMLResponse)
def home():
    return """
    <!DOCTYPE html>
    <html lang="ar" dir="rtl">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>لوحة التحليلات المتقدمة للإعلانات</title>
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
            .btn-copy { background: linear-gradient(135deg, #10b981, #047857); color: #fff; border: none; }

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
                transition: transform 0.2s;
            }
            .kpi-card.highlight { border-color: var(--accent-gold); background: linear-gradient(180deg, rgba(245,158,11,0.05) 0%, var(--bg-card) 100%); }
            .kpi-card .label { font-size: 13px; color: var(--text-muted); margin-bottom: 10px; display: flex; justify-content: space-between; align-items: center; }
            .kpi-card .val { font-size: 26px; font-weight: 800; color: #fff; }
            .kpi-card .sub { font-size: 12px; color: var(--text-muted); margin-top: 6px; display: flex; gap: 8px; }
            
            .badge { padding: 4px 8px; border-radius: 6px; font-size: 11px; font-weight: 700; display: inline-flex; align-items: center; gap: 4px; }
            .badge-success { background: rgba(16,185,129,0.2); color: #34d399; border: 1px solid rgba(16,185,129,0.3); }
            .badge-warning { background: rgba(245,158,11,0.2); color: #fbbf24; border: 1px solid rgba(245,158,11,0.3); }
            .badge-danger { background: rgba(239,68,68,0.2); color: #f87171; border: 1px solid rgba(239,68,68,0.3); }

            .diag-box {
                background: var(--bg-card);
                border: 1px solid var(--border-color);
                border-radius: 12px;
                padding: 20px;
                margin-bottom: 24px;
            }
            .diag-title { font-size: 15px; font-weight: 700; margin-bottom: 14px; display: flex; align-items: center; gap: 8px; color: var(--accent-gold); }
            .diag-list { display: flex; flex-direction: column; gap: 10px; }
            .diag-item { padding: 12px 16px; border-radius: 8px; font-size: 13px; display: flex; align-items: center; gap: 12px; }
            .diag-red { background: rgba(239,68,68,0.1); border: 1px solid rgba(239,68,68,0.25); color: #fca5a5; }
            .diag-green { background: rgba(16,185,129,0.1); border: 1px solid rgba(16,185,129,0.25); color: #6ee7b7; }
            .diag-yellow { background: rgba(245,158,11,0.1); border: 1px solid rgba(245,158,11,0.25); color: #fde047; }

            .chart-card {
                background: var(--bg-card);
                border: 1px solid var(--border-color);
                border-radius: 12px;
                padding: 24px;
                margin-bottom: 24px;
            }
            .chart-title { font-size: 15px; font-weight: 700; text-align: center; margin-bottom: 20px; color: var(--text-muted); }

            .section-card {
                background: var(--bg-card);
                border: 1px solid var(--border-color);
                border-radius: 12px;
                padding: 24px;
                margin-bottom: 24px;
            }
            .section-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; }
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
            <div class="header-bar">
                <div class="header-title">
                    <h1><i class="fa-solid fa-chart-pie" style="color: var(--accent-gold); margin-left:8px;"></i> لوحة تحليلات الإعلانات المتقدمة</h1>
                    <p id="updateTime">جاري قراءة البيانات المباشرة...</p>
                </div>
                <div class="actions-group">
                    <div class="time-selector">
                        <button class="time-btn" onclick="setTimeRange('all')">كل البيانات المسحوبة</button>
                        <button class="time-btn active" onclick="setTimeRange('last7')">آخر 7 أيام</button>
                    </div>
                    <button class="btn btn-accent" onclick="fetchAndAnalyze()"><i class="fa-solid fa-rotate"></i> تحديث</button>
                    <button class="btn btn-copy" onclick="copyDataForAI()"><i class="fa-solid fa-copy"></i> نسخ التقرير للذكاء الاصطناعي</button>
                </div>
            </div>

            <!-- KPIs -->
            <div class="kpi-grid">
                <div class="kpi-card">
                    <div class="label"><span>Meta Ads</span> <i class="fa-brands fa-meta" style="color: var(--accent-blue);"></i></div>
                    <div class="val" id="metaSpend">0.00 ر.س</div>
                    <div class="sub"><span id="metaConv">0 محادثة</span> | <span id="metaCpc">CPC: 0.00</span></div>
                </div>
                <div class="kpi-card">
                    <div class="label"><span>TikTok Ads</span> <i class="fa-brands fa-tiktok" style="color: var(--accent-pink);"></i></div>
                    <div class="val" id="tiktokSpend">0.00 ر.س</div>
                    <div class="sub"><span id="tiktokConv">0 تحويل</span> | <span id="tiktokCpc">CPC: 0.00</span></div>
                </div>
                <div class="kpi-card">
                    <div class="label"><span>Google Ads</span> <i class="fa-brands fa-google" style="color: var(--accent-green);"></i></div>
                    <div class="val" id="googleSpend">0.00 ر.س</div>
                    <div class="sub"><span id="googleConv">0 تحويلات</span> | <span id="googleCpc">CPC: 0.00</span></div>
                </div>
                <div class="kpi-card highlight">
                    <div class="label"><span>إجمالي الإنفاق الكلي</span> <i class="fa-solid fa-wallet" style="color: var(--accent-gold);"></i></div>
                    <div class="val" id="totalSpend">0.00 ر.س</div>
                    <div class="sub"><span id="totalConversions">إجمالي النتائج: 0</span></div>
                </div>
            </div>

            <!-- TikTok Section -->
            <div class="section-card">
                <div class="section-header">
                    <h3><span class="dot dot-tiktok"></span> TikTok Ads - تقييم نجاح الإعلانات حسب الجاذبية (CTR & CPC)</h3>
                </div>
                <table>
                    <thead>
                        <tr>
                            <th>الإعلان / الحملة</th>
                            <th>التقييم الذكي</th>
                            <th>الإنفاق</th>
                            <th>الظهور</th>
                            <th>النقرات</th>
                            <th>معدل النقر (CTR)</th>
                            <th>تكلفة النقرة (CPC)</th>
                        </tr>
                    </thead>
                    <tbody id="tiktokTable">
                        <tr><td colspan="7" style="text-align:center; color:var(--text-muted);">جاري قراءة البيانات...</td></tr>
                    </tbody>
                </table>
            </div>

            <!-- Smart Diagnostics Box -->
            <div class="diag-box">
                <div class="diag-title"><i class="fa-solid fa-lightbulb"></i> التحليل البرمجي التلقائي للحملات</div>
                <div class="diag-list" id="diagList">
                    <div class="diag-item diag-yellow"><i class="fa-solid fa-spinner fa-spin"></i> جاري إكمال قراءة البيانات لبدء التحليل...</div>
                </div>
            </div>

            <!-- Chart -->
            <div class="chart-card">
                <div class="chart-title">مقارنة الإنفاق والنتائج حسب المنصة</div>
                <div style="height: 250px;">
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
                        </tr>
                    </thead>
                    <tbody id="metaTable">
                        <tr><td colspan="6" style="text-align:center; color:var(--text-muted);">جاري القراءة...</td></tr>
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
                        </tr>
                    </thead>
                    <tbody id="googleTable">
                        <tr><td colspan="6" style="text-align:center; color:var(--text-muted);">جاري القراءة...</td></tr>
                    </tbody>
                </table>
            </div>
        </div>

        <div class="toast" id="toast">✅ تم نسخ التقرير الحافظة بنجاح!</div>

        <script>
            let rawDataCache = null;
            let currentRange = 'last7';
            let myChart = null;
            let currentSummaryText = "";

            function setTimeRange(range) {
                currentRange = range;
                document.querySelectorAll('.time-btn').forEach(b => b.classList.remove('active'));
                event.target.classList.add('active');
                if (rawDataCache) renderData(rawDataCache);
            }

            async function fetchAndAnalyze() {
                try {
                    document.getElementById('updateTime').innerText = "جاري الاتصال بـ Windsor.ai لقراءة البيانات...";
                    let res = await fetch('/api/data');
                    rawDataCache = await res.json();
                    renderData(rawDataCache);
                } catch(e) {
                    console.error(e);
                    document.getElementById('updateTime').innerText = "حدث خطأ في جلب البيانات، يرجى تحديث الصفحة.";
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
                document.getElementById('updateTime').innerText = `تم تحديث البيانات المباشرة بنجاح: ${new Date().toLocaleTimeString('ar-SA')}`;

                let mSpend = 0, mConv = 0, mClicks = 0;
                let tSpend = 0, tConv = 0, tClicks = 0;
                let gSpend = 0, gConv = 0, gClicks = 0;

                let tiktokCampaignsList = [];
                let allCampaigns = [];

                // 1. TikTok Data Processing & Smart Evaluation Badge
                let tHtml = '';
                if (data.tiktok_ads && data.tiktok_ads.data && data.tiktok_ads.data.length > 0) {
                    let rows = data.tiktok_ads.data;
                    rows.forEach(r => {
                        let sp = parseFloat(r.spend || 0);
                        let clk = parseInt(r.clicks || 0);
                        let imp = parseInt(r.impressions || 0);
                        let cv = parseFloat(r.conversions || 0);
                        let ctr = r.ctr ? (parseFloat(r.ctr) * (parseFloat(r.ctr) < 1 ? 100 : 1)) : (imp > 0 ? (clk / imp) * 100 : 0);
                        let cpc = clk > 0 ? (sp / clk) : (parseFloat(r.cpc || 0));

                        tSpend += sp; tConv += cv; tClicks += clk;

                        // Evaluation Logic based on CTR & CPC
                        let badgeHtml = '';
                        let statusText = '';
                        if (ctr >= 1.0 && (cpc <= 1.5 || cpc === 0)) {
                            badgeHtml = '<span class="badge badge-success"><i class="fa-solid fa-circle-check"></i> ناجح ممتاز</span>';
                            statusText = 'ناجح';
                        } else if (ctr >= 0.6) {
                            badgeHtml = '<span class="badge badge-warning"><i class="fa-solid fa-triangle-exclamation"></i> متوسط الجاذبية</span>';
                            statusText = 'متوسط';
                        } else {
                            badgeHtml = '<span class="badge badge-danger"><i class="fa-solid fa-circle-xmark"></i> ضعيف / فاشل</span>';
                            statusText = 'فاشل';
                        }

                        tiktokCampaignsList.push({ name: r.campaign_name, spend: sp, clicks: clk, ctr: ctr, cpc: cpc, status: statusText });
                        allCampaigns.push({ platform: 'TikTok', name: r.campaign_name, spend: sp, clicks: clk, conv: cv, cpc: cpc });

                        tHtml += `<tr>
                            <td><strong>${r.campaign_name || 'حملة بدون اسم'}</strong></td>
                            <td>${badgeHtml}</td>
                            <td>${sp.toFixed(2)} ر.س</td>
                            <td>${imp.toLocaleString()}</td>
                            <td>${clk.toLocaleString()}</td>
                            <td class="${ctr >= 1.0 ? 'text-green' : ''}">${ctr.toFixed(2)}%</td>
                            <td>${cpc.toFixed(2)} ر.س</td>
                        </tr>`;
                    });
                }
                document.getElementById('tiktokSpend').innerText = tSpend.toFixed(2) + ' ر.س';
                document.getElementById('tiktokConv').innerText = tConv + ' تحويل';
                document.getElementById('tiktokCpc').innerText = 'CPC: ' + (tClicks > 0 ? (tSpend/tClicks).toFixed(2) : '0.00') + ' ر.س';
                document.getElementById('tiktokTable').innerHTML = tHtml || '<tr><td colspan="7" style="text-align:center">لا توجد بيانات متاحة لـ TikTok</td></tr>';

                // 2. Meta Data
                let mHtml = '';
                if (data.meta_ads && data.meta_ads.data && data.meta_ads.data.length > 0) {
                    data.meta_ads.data.forEach(r => {
                        let sp = parseFloat(r.spend || 0);
                        let clk = parseInt(r.clicks || 0);
                        let imp = parseInt(r.impressions || 0);
                        let cv = extractMetaConversations(r);
                        mSpend += sp; mConv += cv; mClicks += clk;
                        let cpc = clk > 0 ? (sp / clk) : 0;

                        allCampaigns.push({ platform: 'Meta', name: r.campaign_name, spend: sp, clicks: clk, conv: cv, cpc: cpc });

                        mHtml += `<tr>
                            <td><strong>${r.campaign_name || 'حملة بدون اسم'}</strong></td>
                            <td>${sp.toFixed(2)} ر.س</td>
                            <td>${imp.toLocaleString()}</td>
                            <td>${clk.toLocaleString()}</td>
                            <td>${cpc.toFixed(2)} ر.س</td>
                            <td class="text-green">${cv}</td>
                        </tr>`;
                    });
                }
                document.getElementById('metaSpend').innerText = mSpend.toFixed(2) + ' ر.س';
                document.getElementById('metaConv').innerText = mConv + ' محادثة';
                document.getElementById('metaCpc').innerText = 'CPC: ' + (mClicks > 0 ? (mSpend/mClicks).toFixed(2) : '0.00') + ' ر.س';
                document.getElementById('metaTable').innerHTML = mHtml || '<tr><td colspan="6" style="text-align:center">لا توجد بيانات متاحة لـ Meta</td></tr>';

                // 3. Google Data
                let gHtml = '';
                if (data.google_ads && data.google_ads.data && data.google_ads.data.length > 0) {
                    data.google_ads.data.forEach(r => {
                        let sp = parseFloat(r.spend || 0);
                        let clk = parseInt(r.clicks || 0);
                        let imp = parseInt(r.impressions || 0);
                        let cv = parseFloat(r.conversions || 0);
                        gSpend += sp; gConv += cv; gClicks += clk;
                        let cpc = clk > 0 ? (sp / clk) : 0;

                        allCampaigns.push({ platform: 'Google', name: r.campaign_name, spend: sp, clicks: clk, conv: cv, cpc: cpc });

                        gHtml += `<tr>
                            <td><strong>${r.campaign_name || 'حملة بدون اسم'}</strong></td>
                            <td>${sp.toFixed(2)} ر.س</td>
                            <td>${imp.toLocaleString()}</td>
                            <td>${clk.toLocaleString()}</td>
                            <td>${cpc.toFixed(2)} ر.س</td>
                            <td class="text-green">${cv}</td>
                        </tr>`;
                    });
                }
                document.getElementById('googleSpend').innerText = gSpend.toFixed(2) + ' ر.س';
                document.getElementById('googleConv').innerText = gConv + ' تحويلات';
                document.getElementById('googleCpc').innerText = 'CPC: ' + (gClicks > 0 ? (gSpend/gClicks).toFixed(2) : '0.00') + ' ر.س';
                document.getElementById('googleTable').innerHTML = gHtml || '<tr><td colspan="6" style="text-align:center">لا توجد بيانات متاحة لـ Google</td></tr>';

                // Total Totals
                let totalSp = mSpend + tSpend + gSpend;
                let totalCv = mConv + tConv + gConv;
                document.getElementById('totalSpend').innerText = totalSp.toFixed(2) + ' ر.س';
                document.getElementById('totalConversions').innerText = `إجمالي النتائج والمحادثات: ${totalCv}`;

                // Run Diagnostics AFTER reading and rendering table
                runDiagnostics(tiktokCampaignsList, allCampaigns);

                // Build Summary for AI
                currentSummaryText = `تقرير أداء الإعلانات المباشر:\n` +
                    `- إجمالي الإنفاق: ${totalSp.toFixed(2)} ر.س | إجمالي النتائج: ${totalCv}\n` +
                    `- Meta: إنفاق ${mSpend.toFixed(2)} ر.س | نتائج ${mConv}\n` +
                    `- TikTok: إنفاق ${tSpend.toFixed(2)} ر.س | نتائج ${tConv}\n` +
                    `- Google: إنفاق ${gSpend.toFixed(2)} ر.س | نتائج ${gConv}\n\n` +
                    `تقييم إعلانات تيك توك المباشرة:\n` +
                    tiktokCampaignsList.map(t => `• ${t.name}: التقييم (${t.status}) | CTR: ${t.ctr.toFixed(2)}% | CPC: ${t.cpc.toFixed(2)} ر.س | الإنفاق: ${t.spend.toFixed(2)} ر.س`).join('\n');

                renderChart(['Meta Ads', 'TikTok Ads', 'Google Ads'], [mSpend, tSpend, gSpend]);
            }

            function runDiagnostics(tiktokList, allCampaigns) {
                let diagList = document.getElementById('diagList');
                let items = [];

                // TikTok specific AI Diagnostics
                let winners = tiktokList.filter(t => t.status === 'ناجح');
                if (winners.length > 0) {
                    items.push(`<div class="diag-item diag-green"><i class="fa-solid fa-circle-check"></i> <strong>إعلان تيك توك الناجح:</strong> الإعلان <strong>"${winners[0].name}"</strong> يحقق أعلى نسبة جاذبية (CTR: ${winners[0].ctr.toFixed(2)}%) بتكلفة نقرة ضئيلة (${winners[0].cpc.toFixed(2)} ر.س). يوصى بزيادة ميزانيته.</div>`);
                }

                let failed = tiktokList.filter(t => t.status === 'فاشل' && t.spend > 10);
                failed.forEach(t => {
                    items.push(`<div class="diag-item diag-red"><i class="fa-solid fa-triangle-exclamation"></i> <strong>إعلان تيك توك فاشل/ضعيف:</strong> الإعلان <strong>"${t.name}"</strong> يحقق CTR ضعيف جداً (${t.ctr.toFixed(2)}%). يتطلب إيقافه أو تغيير الفيديو.</div>`);
                });

                if (items.length === 0) {
                    items.push('<div class="diag-item diag-green"><i class="fa-solid fa-shield-halved"></i> <strong>حالة الإعلانات:</strong> تم جلب البيانات بنجاح، وجميع الإعلانات تعمل ضمن المستويات الطبيعية.</div>');
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
                            barThickness: 36
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
