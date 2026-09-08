import os
import requests
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

app = FastAPI()

WINDSOR_API_KEY = os.getenv("WINDSOR_API_KEY", "")

@app.get("/", response_class=HTMLResponse)
def home():
    return """
    <!DOCTYPE html>
    <html lang="ar" dir="rtl">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>تقرير أداء الحملات التفاعلي</title>
        <style>
            body { font-family: system-ui, -apple-system, sans-serif; background: #f8fafc; color: #0f172a; padding: 20px; direction: rtl; }
            .container { max-width: 1000px; margin: 0 auto; }
            .header { background: #1e293b; color: #fff; padding: 20px; border-radius: 10px; text-align: center; margin-bottom: 20px; }
            .btn-refresh { background: #0284c7; color: white; border: none; padding: 12px 24px; border-radius: 6px; font-weight: bold; cursor: pointer; font-size: 15px; margin-top: 10px; }
            .btn-refresh:hover { background: #0369a1; }
            .kpi-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 15px; margin-bottom: 25px; }
            .kpi-card { background: #fff; border: 1px solid #e2e8f0; border-radius: 8px; padding: 15px; text-align: center; }
            .kpi-title { font-size: 13px; color: #64748b; margin-bottom: 5px; }
            .kpi-value { font-size: 20px; font-weight: bold; }
            table { width: 100%; border-collapse: collapse; background: #fff; border-radius: 8px; overflow: hidden; border: 1px solid #e2e8f0; margin-bottom: 25px; }
            th, td { padding: 12px; text-align: right; border-bottom: 1px solid #e2e8f0; font-size: 14px; }
            th { background: #f1f5f9; color: #475569; }
            .section-title { font-size: 18px; margin: 20px 0 10px; color: #1e293b; font-weight: bold; }
            .status { font-size: 13px; color: #38bdf8; margin-top: 5px; }
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1 style="margin:0; font-size: 22px;">📊 التقرير المباشر للحملات النشطة</h1>
                <p class="status" id="statusText">جاهز للتحميل...</p>
                <button class="btn-refresh" onclick="fetchAndAnalyze()">🔄 تحديث التقرير الآن</button>
            </div>

            <div class="section-title">📌 أداء يوم أمس (الحملات النشطة)</div>
            <div class="kpi-grid">
                <div class="kpi-card"><div class="kpi-title">إجمالي الإنفاق</div><div class="kpi-value" id="yesterdaySpend">0.00 SAR</div></div>
                <div class="kpi-card"><div class="kpi-title">إجمالي النقرات</div><div class="kpi-value" id="yesterdayClicks">0</div></div>
                <div class="kpi-card"><div class="kpi-title">إجمالي التحويلات</div><div class="kpi-value" id="yesterdayConv">0</div></div>
                <div class="kpi-card"><div class="kpi-title">متوسط تكلفة النقرة</div><div class="kpi-value" id="yesterdayCPC">0.00 SAR</div></div>
            </div>

            <table id="yesterdayTable">
                <thead>
                    <tr><th>المنصة</th><th>اسم الحملة</th><th>الإنفاق</th><th>الظهور</th><th>النقرات</th><th>التحويلات</th></tr>
                </thead>
                <tbody><tr><td colspan="6" style="text-align:center">اضغط على زر التحديث لجلب البيانات</td></tr></tbody>
            </table>

            <div class="section-title">⚖️ مقارنة أداء الـ 14 يوماً الأخيرة بالفترة السابقة</div>
            <table>
                <thead>
                    <tr><th>المؤشر</th><th>الـ 14 يوماً السابقة</th><th>الـ 14 يوماً الأخيرة</th><th>نسبة التغير</th></tr>
                </thead>
                <tbody id="compTable">
                    <tr><td colspan="4" style="text-align:center">في انتظار جلب البيانات...</td></tr>
                </tbody>
            </table>
        </div>

        <script>
            async function fetchAndAnalyze() {
                document.getElementById('statusText').innerText = "جاري جلب أحدث البيانات...";
                try {
                    let res = await fetch('/api/data');
                    let rawData = await res.json();
                    
                    let allRows = [];
                    if (rawData.tiktok_ads && rawData.tiktok_ads.data) {
                        rawData.tiktok_ads.data.forEach(r => { r.platform = 'TikTok'; allRows.push(r); });
                    }
                    if (rawData.google_ads && rawData.google_ads.data) {
                        rawData.google_ads.data.forEach(r => { r.platform = 'Google Ads'; allRows.push(r); });
                    }

                    // تاريخ اليوم وأمس
                    let now = new Date();
                    let yesterdayStr = new Date(now.setDate(now.getDate() - 1)).toISOString().split('T')[0];

                    // 1. معالجة يوم أمس
                    let yRows = allRows.filter(r => r.date === yesterdayStr && (r.clicks > 0 || r.spend > 0));
                    let ySpend = 0, yClicks = 0, yConv = 0;
                    let yTbody = '';

                    yRows.forEach(r => {
                        ySpend += parseFloat(r.spend || 0);
                        yClicks += parseInt(r.clicks || 0);
                        yConv += parseFloat(r.conversions || 0);
                        yTbody += `<tr><td>${r.platform}</td><td>${r.campaign_name}</td><td>${parseFloat(r.spend).toFixed(2)} SAR</td><td>${r.impressions}</td><td>${r.clicks}</td><td>${r.conversions}</td></tr>`;
                    });

                    document.getElementById('yesterdaySpend').innerText = ySpend.toFixed(2) + ' SAR';
                    document.getElementById('yesterdayClicks').innerText = yClicks;
                    document.getElementById('yesterdayConv').innerText = yConv;
                    document.getElementById('yesterdayCPC').innerText = yClicks > 0 ? (ySpend / yClicks).toFixed(2) + ' SAR' : '0.00 SAR';
                    document.getElementById('yesterdayTable').querySelector('tbody').innerHTML = yTbody || '<tr><td colspan="6" style="text-align:center">لا توجد حملات نشطة مسجلة بتاريخ أمس</td></tr>';

                    // 2. معالجة الـ 14 يوماً
                    let d14 = new Date(); d14.setDate(d14.getDate() - 14);
                    let d28 = new Date(); d28.setDate(d28.getDate() - 28);

                    let recent14 = allRows.filter(r => new Date(r.date) >= d14);
                    let prev14 = allRows.filter(r => new Date(r.date) >= d28 && new Date(r.date) < d14);

                    let sumR14 = sumData(recent14), sumP14 = sumData(prev14);

                    let compHtml = `
                        <tr><td>إجمالي الإنفاق</td><td>${sumP14.spend.toFixed(2)} SAR</td><td>${sumR14.spend.toFixed(2)} SAR</td><td>${getDiff(sumP14.spend, sumR14.spend)}</td></tr>
                        <tr><td>إجمالي التحويلات</td><td>${sumP14.conv.toFixed(2)}</td><td>${sumR14.conv.toFixed(2)}</td><td>${getDiff(sumP14.conv, sumR14.conv)}</td></tr>
                        <tr><td>إجمالي النقرات</td><td>${sumP14.clicks}</td><td>${sumR14.clicks}</td><td>${getDiff(sumP14.clicks, sumR14.clicks)}</td></tr>
                    `;
                    document.getElementById('compTable').innerHTML = compHtml;
                    document.getElementById('statusText').innerText = "تم تحديث التقرير بنجاح!";
                } catch(e) {
                    document.getElementById('statusText').innerText = "حدث خطأ أثناء التحديث.";
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
                return pct >= 0 ? `📈 +${pct}%` : `📉 ${pct}%`;
            }

            fetchAndAnalyze();
        </script>
    </body>
    </html>
    """

@app.get("/api/data")
def get_data():
    if not WINDSOR_API_KEY:
        return {"error": "WINDSOR_API_KEY غير معرف في Render Environment Variables"}
    
    results = {}
    params = "&fields=date,campaign_name,clicks,impressions,spend,conversions&date_preset=last_30d"
    
    try:
        results["meta_ads"] = requests.get(f"https://connectors.windsor.ai/facebook?api_key={WINDSOR_API_KEY}{params}").json()
    except Exception as e:
        results["meta_ads"] = {"error": str(e)}
        
    try:
        results["tiktok_ads"] = requests.get(f"https://connectors.windsor.ai/tiktok?api_key={WINDSOR_API_KEY}{params}").json()
    except Exception as e:
        results["tiktok_ads"] = {"error": str(e)}

    try:
        results["google_ads"] = requests.get(f"https://connectors.windsor.ai/google_ads?api_key={WINDSOR_API_KEY}{params}").json()
    except Exception as e:
        results["google_ads"] = {"error": str(e)}

    return results
