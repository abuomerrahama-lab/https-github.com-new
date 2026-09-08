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
        <title>لوحة التحكم الشاملة بالإعلانات والتحليلات</title>
        <style>
            body { font-family: system-ui, -apple-system, sans-serif; padding: 30px; background: #f0f2f5; color: #1c1e21; }
            .container { max-width: 950px; margin: 0 auto; background: white; padding: 25px; border-radius: 12px; box-shadow: 0 4px 12px rgba(0,0,0,0.08); }
            h2 { color: #1877f2; margin-top: 0; }
            button { background: #1877f2; color: white; border: none; padding: 12px 24px; border-radius: 6px; font-size: 16px; font-weight: bold; cursor: pointer; transition: background 0.2s; }
            button:hover { background: #166fe5; }
            pre { margin-top: 20px; background: #282c34; color: #abb2bf; padding: 15px; border-radius: 8px; overflow-x: auto; max-height: 500px; font-size: 14px; text-align: left; dir: ltr; }
            .status { margin-bottom: 15px; font-size: 14px; color: #65676b; }
        </style>
    </head>
    <body>
        <div class="container">
            <h2>📊 لوحة تحكم إعلانات Meta و TikTok و Google Ads & Analytics</h2>
            <p class="status" id="statusText">جاري تحميل البيانات من كافة المنصات عبر Windsor.ai...</p>
            <button onclick="copyData()">📋 نسخ البيانات للتحليل (Export JSON)</button>
            <pre id="jsonDisplay">جاري الاتصال بالخادم...</pre>
        </div>

        <script>
            async function loadData() {
                try {
                    let res = await fetch('/api/data');
                    let data = await res.json();
                    document.getElementById('jsonDisplay').innerText = JSON.stringify(data, null, 2);
                    document.getElementById('statusText').innerText = "تم تحديث البيانات لجميع المنصات بنجاح!";
                } catch (e) {
                    document.getElementById('jsonDisplay').innerText = "حدث خطأ أثناء جلب البيانات.";
                    document.getElementById('statusText').innerText = "فشل التحميل.";
                }
            }

            function copyData() {
                let text = document.getElementById('jsonDisplay').innerText;
                navigator.clipboard.writeText(text);
                alert("تم نسخ بيانات كافة المنصات بنجاح! يمكنك الآن لصقها هنا للتحليل.");
            }

            loadData();
        </script>
    </body>
    </html>
    """

@app.get("/api/data")
def get_data():
    if not WINDSOR_API_KEY:
        return {"error": "WINDSOR_API_KEY غير معرف في Render Environment Variables"}
    
    results = {}
    
    # 1. جلب بيانات Meta Ads
    try:
        fb_url = f"https://connectors.windsor.ai/facebook?api_key={WINDSOR_API_KEY}&fields=campaign_name,clicks,impressions,spend"
        results["meta_ads"] = requests.get(fb_url).json()
    except Exception as e:
        results["meta_ads"] = {"error": str(e)}
        
    # 2. جلب بيانات TikTok Ads
    try:
        tt_url = f"https://connectors.windsor.ai/tiktok?api_key={WINDSOR_API_KEY}&fields=campaign_name,clicks,impressions,spend"
        results["tiktok_ads"] = requests.get(tt_url).json()
    except Exception as e:
        results["tiktok_ads"] = {"error": str(e)}

    # 3. جلب بيانات Google Ads
    try:
        gads_url = f"https://connectors.windsor.ai/google_ads?api_key={WINDSOR_API_KEY}&fields=campaign_name,clicks,impressions,spend"
        results["google_ads"] = requests.get(gads_url).json()
    except Exception as e:
        results["google_ads"] = {"error": str(e)}

    # 4. جلب بيانات Google Analytics (GA4)
    try:
        ga_url = f"https://connectors.windsor.ai/google_analytics_4?api_key={WINDSOR_API_KEY}&fields=sessions,users,pageviews"
        results["google_analytics"] = requests.get(ga_url).json()
    except Exception as e:
        results["google_analytics"] = {"error": str(e)}

    return results
