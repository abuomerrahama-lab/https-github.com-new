import os
import requests
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

app = FastAPI()

WINDSOR_API_KEY = os.getenv("WINDSOR_API_KEY", "")

# معرّفات الحسابات الإعلانية المحددة
META_ACCOUNTS = "1085415013613251,1203619500645957"
TIKTOK_ACCOUNTS = "7477300225556824081,7438927058295996417"

@app.get("/", response_class=HTMLResponse)
def home():
    return """
    <!DOCTYPE html>
    <html lang="ar" dir="rtl">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>لوحة تحليل الحملات الإعلانية</title>
        <style>
            body { font-family: system-ui, -apple-system, sans-serif; background: #f8fafc; color: #0f172a; padding: 20px; direction: rtl; }
            .container { max-width: 1000px; margin: 0 auto; }
            .header { background: #1e293b; color: #fff; padding: 25px; border-radius: 12px; text-align: center; margin-bottom: 25px; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.1); }
            .btn-refresh { background: #0284c7; color: white; border: none; padding: 12px 28px; border-radius: 8px; font-weight: bold; cursor: pointer; font-size: 16px; margin-top: 12px; transition: background 0.2s; }
            .btn-refresh:hover { background: #0369a1; }
            .kpi-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 15px; margin-bottom: 25px; }
            .kpi-card { background: #fff; border: 1px solid #e2e8f0; border-radius: 10px; padding: 18px; text-align: center; box-shadow: 0 1px 3px rgba(0,0,0,0.05); }
            .kpi-title { font-size: 13px; color: #64748b; margin-bottom: 6px; }
            .kpi-value { font-size: 22px; font-weight: bold; color: #0f172a; }
            table { width: 100%; border-collapse: collapse; background: #fff; border-radius: 10px; overflow: hidden; border: 1px solid #e2e8f0; margin-bottom: 30px; box-shadow: 0 1px 3px rgba(0,0,0,0.05); }
            th, td { padding: 14px; text-align: right; border-bottom: 1px solid #e2e8f0; font-size: 14px; }
            th { background: #f1f5f9; color: #475569; font-weight: 600; }
            tr:hover { background: #f8fafc; }
            .section-title { font-size: 18px; margin: 25px 0 12px; color: #1e293b; font-weight: bold; border-right: 4px solid #0284c7; padding-right: 10px; }
            .status { font-size: 14px; color: #38bdf8; margin-top: 8px; }
            .trend-up { color: #16a34a; font-weight: bold; }
            .trend-down { color: #dc2626; font-weight: bold; }
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1 style="margin:0; font-size: 24px;">📊 تقرير قراءة النتائج المباشر</h1>
                <p class="status" id="statusText">جاري تحميل البيانات تلقائياً...</p>
                <button class="btn-refresh" onclick="fetchAndAnalyze()">🔄 تحديث التقرير الآن</button>
            </div>

            <div class="section-title">📌 ملخص أداء يوم أمس (الحملات النشطة)</div>
            <div class="kpi-grid">
                <div class="kpi-card"><div class="kpi-title">إجمالي الإنفاق</div><div class="kpi-value" id="yesterdaySpend">0.00 SAR</div></div>
                <div class="kpi-card"><div class="kpi-title">إجمالي النقرات</div><div class="kpi-value" id="yesterdayClicks">0</div></div>
                <div class="kpi-card"><div class="kpi-title">إجمالي التحويلات</div><div class="kpi-value" id="yesterdayConv">0</div></div>
                <div class="kpi-card"><div class="kpi-title">متوسط تكلفة النقرة (CPC)</div><div class="kpi-value" id="yesterdayCPC">0.00 SAR</div></div>
            </div>

            <table id="yesterdayTable">
                <thead>
                    <tr><th>المنصة</th><th>اسم الحملة</th><th>الإنفاق</th><th>الظهور</th><th>النقرات</th><th>التحويلات</th></tr>
                </thead>
                <tbody>
                    <tr><td colspan="6" style="text-align:center; color:#64748b;">جاري تحديث البيانات...</td></tr>
                </tbody>
            </table>

            <div class="section-title">⚖️ مقارنة أداء الـ 14 يوماً الأخيرة بالفترة السابقة</div>
            <table>
                <thead>
                    <tr><th>المؤشر</th><th>الـ 14 يوماً السابقة</th><th>الـ 14 يوماً الأخيرة</th><th>نسبة التغير</th></tr>
                </thead>
                <tbody id="compTable">
                    <tr><td colspan="4" style="text-align:center; color:#64748b;">في انتظار قراءة البيانات...</td></tr>
                </tbody>
            </table>
        </div>

        <script>
            const allowedMetaAccounts = ["1085415013613251", "1203619500645957"];
            const allowedTiktokAccounts = ["7477300225556824081", "7438927058295996417"];

            async function fetchAndAnalyze() {
                document.getElementById('statusText').innerText = "جاري الاتصال بالـ API وجلب حملات Meta و TikTok المحددة...";
                try {
                    let res = await fetch('/api/data');
                    let rawData = await res.json();
                    
                    let allRows = [];

                    // 1. معالجة Meta Ads
                    if (rawData.meta_ads && rawData.meta_ads.data) {
                        rawData.meta_ads.data.forEach(r => {
                            let isAccountValid = allowedMetaAccounts.includes(String(r.account_id));
                            let status = (r.campaign_status || '').toUpperCase();
                            let isActive = !r.campaign_status || status === 'ACTIVE' || status === 'ENABLE';
                            if (isAccountValid && isActive) {
                                r.platform = 'Meta Ads';
                                allRows.push(r);
                            }
                        });
                    }

                    // 2. معالجة TikTok Ads (مع التحقق من ID الحساب والحملات النشطة)
                    if (rawData.tiktok_ads && rawData.tiktok_ads.data) {
                        rawData.tiktok_ads.data.forEach(r => {
                            let isAccountValid = allowedTiktokAccounts.includes(String(r.account_id)) || allowedTiktokAccounts.includes(String(r.advertiser_id));
                            let status = (r.campaign_status || r.operation_status || '').toUpperCase();
                            let isActive = !status || status === 'ENABLE' || status === 'ACTIVE' || status === 'CAMPAIGN_STATUS_ENABLE';
                            
                            if ((isAccountValid || !r.account_id) && isActive) {
                                r.platform = 'TikTok Ads';
                                allRows.push(r);
                            }
                        });
                    }

                    // 3. معالجة Google Ads
                    if (rawData.google_ads && rawData.google_ads.data) {
                        rawData.google_ads.data.forEach(r => {
                            let status = (r.campaign_status || '').toUpperCase();
                            let isActive = !status || status === 'ENABLED' || status === 'ACTIVE';
                            if (isActive) {
                                r.platform = 'Google Ads';
                                allRows.push(r);
                            }
                        });
                    }

                    // حساب تاريخ الأمس
                    let now = new Date();
                    let yesterday = new Date(now);
                    yesterday.setDate(now.getDate() - 1);
                    let yesterdayStr = yesterday.toISOString().split('T')[0];

                    // تصفية أداء يوم أمس
                    let yRows = allRows.filter(r => r.date === yesterdayStr && (r.clicks > 0 || r.spend > 0));
                    let ySpend = 0, yClicks = 0, yConv = 0;
                    let yTbody = '';

                    yRows.forEach(r => {
                        let spend = parseFloat(r.spend || 0);
                        let clicks = parseInt(r.clicks || 0);
                        let conv = parseFloat(r.conversions || 0);
                        
                        ySpend += spend;
                        yClicks += clicks;
                        yConv += conv;

                        yTbody += `<tr>
                            <td><strong>${r.platform}</strong></td>
                            <td>${r.campaign_name}</td>
                            <td>${spend.toFixed(2)} SAR</td>
                            <td>${Number(r.impressions || 0).toLocaleString()}</td>
                            <td>${clicks.toLocaleString()}</td>
                            <td>${conv}</td>
                        </tr>`;
                    });

                    document.getElementById('yesterdaySpend').innerText = ySpend.toFixed(2) + ' SAR';
                    document.getElementById('yesterdayClicks').innerText = yClicks.toLocaleString();
                    document.getElementById('yesterdayConv').innerText = yConv;
                    document.getElementById('yesterdayCPC').innerText = yClicks > 0 ? (ySpend / yClicks).toFixed(2) + ' SAR' : '0.00 SAR';
                    document.getElementById('yesterdayTable').querySelector('tbody').innerHTML = yTbody || '<tr><td colspan="6" style="text-align:center">لا توجد حملات نشطة مسجلة بتاريخ أمس.</td></tr>';

                    // مقارنة الـ 14 يوماً
                    let d14 = new Date(); d14.setDate(d14.getDate() - 14);
                    let d28 = new Date(); d28.setDate(d28.getDate() - 28);

                    let recent14 = allRows.filter(r => new Date(r.date) >= d14);
                    let prev14 = allRows.filter(r => new Date(r.date) >= d28 && new Date(r.date) < d14);

                    let sumR14 = sumData(recent14);
                    let sumP14 = sumData(prev14);

                    let compHtml = `
                        <tr><td>إجمالي الإنفاق</td><td>${sumP14.spend.toFixed(2)} SAR</td><td>${sumR14.spend.toFixed(2)} SAR</td><td>${getDiff(sumP14.spend, sumR14.spend)}</td></tr>
                        <tr><td>إجمالي التحويلات</td><td>${sumP14.conv.toFixed(2)}</td><td>${sumR14.conv.toFixed(2)}</td><td>${getDiff(sumP14.conv, sumR14.conv)}</td></tr>
                        <tr><td>إجمالي النقرات</td><td>${sumP14.clicks.toLocaleString()}</td><td>${sumR14.clicks.toLocaleString()}</td><td>${getDiff(sumP14.clicks, sumR14.clicks)}</td></tr>
                    `;
                    document.getElementById('compTable').innerHTML = compHtml;
                    document.getElementById('statusText').innerText = "تم تحديث التقرير بنجاح!";

                } catch(e) {
                    document.getElementById('statusText').innerText = "حدث خطأ أثناء جلب البيانات من API.";
                }
            }

            function sumData(arr) {
                return arr.reduce((acc, r) => {
                    acc.spend += parseFloat(r.spend || 0);
                    acc.clicks += parseInt(r.clicks || 0);
                    acc.conv += parseFloat(r.conversions || 0);
                    return acc;
                }, {spend:0, clicks:0, conv:0});
            }

            function getDiff(prev, curr) {
                if (prev === 0) return 'N/A';
                let pct = (((curr - prev) / prev) * 100).toFixed(1);
                return pct >= 0 ? `<span class="trend-up">📈 +${pct}%</span>` : `<span class="trend-down">📉 ${pct}%</span>`;
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
