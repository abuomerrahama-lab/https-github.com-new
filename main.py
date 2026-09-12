import os
import asyncio
import logging
from typing import Dict, Any
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
import httpx

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ads-dashboard")

app = FastAPI(title="Simple Ads Analytics Dashboard")

# ذاكرة مؤقتة بسيطة
CACHE: Dict[str, Any] = {
    "meta_ads": [],
    "tiktok_ads": [],
    "google_ads": [],
    "last_updated": None
}

WINDSOR_API_KEY = os.getenv("WINDSOR_API_KEY", "")
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "")

async def fetch_windsor(connector: str) -> list:
    if not WINDSOR_API_KEY:
        return []
    url = f"https://connectors.windsor.ai/{connector}"
    params = {"api_key": WINDSOR_API_KEY, "_renderer": "json"}
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            res = await client.get(url, params=params)
            if res.status_code == 200:
                data = res.json()
                return data.get("data", data) if isinstance(data, dict) else data
    except Exception as e:
        logger.error(f"Error fetching {connector}: {e}")
    return []

async def refresh_cache():
    global CACHE
    while True:
        try:
            meta, tiktok, google = await asyncio.gather(
                fetch_windsor("facebook"),
                fetch_windsor("tiktok"),
                fetch_windsor("google_ads"),
                return_exceptions=True
            )
            CACHE["meta_ads"] = meta if isinstance(meta, list) else []
            CACHE["tiktok_ads"] = tiktok if isinstance(tiktok, list) else []
            CACHE["google_ads"] = google if isinstance(google, list) else []
            CACHE["last_updated"] = asyncio.get_event_loop().time()
            
            if RENDER_EXTERNAL_URL:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    await client.get(f"{RENDER_EXTERNAL_URL.rstrip('/')}/api/status")
        except Exception as e:
            logger.error(f"Cache update error: {e}")
        await asyncio.sleep(90)

@app.on_event("startup")
async def startup():
    asyncio.create_task(refresh_cache())

@app.get("/api/data")
async def get_data():
    return JSONResponse(content={"status": "success", "data": CACHE})

@app.get("/api/status")
async def get_status():
    last_up = CACHE["last_updated"]
    ago = round(asyncio.get_event_loop().time() - last_up, 1) if last_up else None
    return {
        "cache_ready": last_up is not None,
        "last_update_seconds_ago": ago,
        "rows": {
            "meta_ads": len(CACHE["meta_ads"]),
            "tiktok_ads": len(CACHE["tiktok_ads"]),
            "google_ads": len(CACHE["google_ads"])
        }
    }

@app.get("/", response_class=HTMLResponse)
async def serve_index():
    return HTMLResponse(content="""
    <!DOCTYPE html>
    <html lang="ar" dir="rtl">
    <head>
        <meta charset="UTF-8">
        <title>لوحة الإعلانات المباشرة</title>
        <style>
            body { font-family: sans-serif; background: #0f172a; color: #fff; padding: 20px; direction: rtl; }
            .card { background: #1e293b; padding: 15px; border-radius: 8px; margin-bottom: 10px; display: inline-block; width: 22%; text-align: center; }
            table { width: 100%; border-collapse: collapse; margin-top: 20px; background: #1e293b; }
            th, td { padding: 10px; border: 1px solid #334155; text-align: right; }
            th { background: #0f172a; }
            .btn { background: #2563eb; color: white; border: none; padding: 8px 16px; border-radius: 5px; cursor: pointer; }
        </style>
    </head>
    <body>
        <h2>لوحة قراءة البيانات المباشرة 📊</h2>
        <button class="btn" onclick="loadData()">تحديث البيانات 🔄</button>
        <button class="btn" style="background:#059669" onclick="copyData()">نسخ التقرير للذكاء الاصطناعي 📋</button>
        <p id="status">جاري التحميل...</p>

        <div style="display:flex; gap:10px;">
            <div class="card">Meta: <h3 id="meta-val">0</h3></div>
            <div class="card">TikTok: <h3 id="tiktok-val">0</h3></div>
            <div class="card">Google: <h3 id="google-val">0</h3></div>
            <div class="card">إجمالي الصفوف: <h3 id="total-val">0</h3></div>
        </div>

        <table>
            <thead>
                <tr>
                    <th>المنصة</th>
                    <th>بيانات الصف (سحب خام مباشر)</th>
                </tr>
            </thead>
            <tbody id="table-body">
                <tr><td colspan="2">جاري التحميل...</td></tr>
            </tbody>
        </table>

        <script>
            let rawData = {};
            async function loadData() {
                try {
                    const res = await fetch('/api/data');
                    const json = await res.json();
                    rawData = json.data || {};
                    
                    let meta = rawData.meta_ads || [];
                    let tiktok = rawData.tiktok_ads || [];
                    let google = rawData.google_ads || [];

                    document.getElementById('meta-val').innerText = meta.length + ' صف';
                    document.getElementById('tiktok-val').innerText = tiktok.length + ' صف';
                    document.getElementById('google-val').innerText = google.length + ' صف';
                    document.getElementById('total-val').innerText = (meta.length + tiktok.length + google.length) + ' صف';

                    let html = '';
                    const addRows = (list, name) => {
                        list.forEach(row => {
                            html += `<tr><td><strong>${name}</strong></td><td><pre style="margin:0; font-size:12px;">${JSON.stringify(row, null, 2)}</pre></td></tr>`;
                        });
                    };

                    addRows(meta, 'Meta');
                    addRows(tiktok, 'TikTok');
                    addRows(google, 'Google');

                    document.getElementById('table-body').innerHTML = html || '<tr><td colspan="2">لا توجد بيانات متاحة</td></tr>';
                    document.getElementById('status').innerText = 'تم التحديث: ' + new Date().toLocaleTimeString();
                } catch(e) {
                    document.getElementById('status').innerText = 'خطأ في جلب البيانات';
                }
            }
            function copyData() {
                navigator.clipboard.writeText(JSON.stringify(rawData, null, 2));
                alert('تم نسخ البيانات كاملة لخافظة الجهاز!');
            }
            loadData();
        </script>
    </body>
    </html>
    """)
