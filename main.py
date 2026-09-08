import os
import requests
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

app = FastAPI()

WINDSOR_API_KEY = os.getenv("WINDSOR_API_KEY", "")

# معرفات الحسابات الإعلانية المحددة
TARGET_ACCOUNTS = "1085415013613251,1203619500645957"

@app.get("/", response_class=HTMLResponse)
def home():
    return "<h1>الخادم يعمل بنجاح! استخدم dashboard.html لقراءة البيانات.</h1>"

@app.get("/api/data")
def get_data():
    if not WINDSOR_API_KEY:
        return {"error": "WINDSOR_API_KEY غير معرف"}
    
    results = {}
    
    # جلب بيانات Meta Ads للحسابين المحددين فقط ومع حالة الحملة
    meta_params = f"&fields=date,campaign_name,clicks,impressions,spend,conversions,campaign_status,account_id&date_preset=last_30d&account_id={TARGET_ACCOUNTS}"
    
    # جلب باقي المنصات (مثل جوجل وتيك توك)
    general_params = "&fields=date,campaign_name,clicks,impressions,spend,conversions,campaign_status&date_preset=last_30d"

    try:
        results["meta_ads"] = requests.get(f"https://connectors.windsor.ai/facebook?api_key={WINDSOR_API_KEY}{meta_params}").json()
    except Exception as e:
        results["meta_ads"] = {"error": str(e)}

    try:
        results["tiktok_ads"] = requests.get(f"https://connectors.windsor.ai/tiktok?api_key={WINDSOR_API_KEY}{general_params}").json()
    except Exception as e:
        results["tiktok_ads"] = {"error": str(e)}

    try:
        results["google_ads"] = requests.get(f"https://connectors.windsor.ai/google_ads?api_key={WINDSOR_API_KEY}{general_params}").json()
    except Exception as e:
        results["google_ads"] = {"error": str(e)}

    return results
