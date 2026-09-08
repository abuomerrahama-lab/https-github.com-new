import os
import requests
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

app = FastAPI()

WINDSOR_API_KEY = os.getenv("WINDSOR_API_KEY", "")
META_ACCESS_TOKEN = os.getenv("META_ACCESS_TOKEN", "")

@app.get("/", response_class=HTMLResponse)
def home():
    return """
    <!DOCTYPE html>
    <html lang="ar" dir="rtl">
    <head>
        <meta charset="UTF-8">
        <title>لوحة إعلانات Meta</title>
        <style>
            body { font-family: sans-serif; padding: 20px; background: #f4f6f8; }
            .card { background: white; padding: 20px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }
            button { background: #0084ff; color: white; border: none; padding: 10px 20px; border-radius: 5px; cursor: pointer; }
        </style>
    </head>
    <body>
        <div class="card">
            <h2>لوحة تحكم بيانات Meta Ads</h2>
            <button onclick="copyData()">نسخ البيانات للتحليل (Export JSON)</button>
            <pre id="jsonDisplay" style="margin-top:20px; background:#eee; padding:10px;"></pre>
        </div>
        <script>
            async function loadData() {
                let res = await fetch('/api/data');
                let data = await res.json();
                document.getElementById('jsonDisplay').innerText = JSON.stringify(data, null, 2);
            }
            function copyData() {
                let text = document.getElementById('jsonDisplay').innerText;
                navigator.clipboard.writeText(text);
                alert("تم نسخ البيانات بنجاح!");
            }
            loadData();
        </script>
    </body>
    </html>
    """

@app.get("/api/data")
def get_data():
    if not WINDSOR_API_KEY:
        return {"error": "WINDSOR_API_KEY غير معرف في Environment Variables"}
    
    url = f"https://connectors.windsor.ai/facebook?api_key={WINDSOR_API_KEY}&fields=campaign_name,clicks,impressions,spend"
    try:
        response = requests.get(url)
        return response.json()
    except Exception as e:
        return {"error": str(e)}
