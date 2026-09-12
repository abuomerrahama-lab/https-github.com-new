import os
import time
import asyncio
from contextlib import asynccontextmanager
import httpx
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

# ============================================================
# الإعدادات
# ============================================================
WINDSOR_API_KEY = os.getenv("WINDSOR_API_KEY", "")
META_ACCOUNTS = "1085415013613251,1203619500645957"
TIKTOK_ACCOUNTS = "7477300225556824081,7438927058295996417"

REFRESH_INTERVAL = 90     # كل كم ثانية يحدّث الكاش في الخلفية (كان قبل: يُحسب فقط عند الطلب)
FETCH_TIMEOUT = 12.0      # مهلة كل منصة أثناء التحديث الخلفي (لا يشعر بها أي زائر أبدًا)

# ============================================================
# الكاش المشترك — هذا هو الحل الحقيقي للبطء
#
# المشكلة في الكود القديم: /api/data كان يستدعي Windsor مباشرة
# أثناء طلب الزائر نفسه، فإذا تأخرت منصة واحدة (شائع مع TikTok/Meta)
# ينتظر الزائر كامل مدة timeout قبل أن يرى أي شيء.
#
# الحل: مهمة خلفية منفصلة (Background Task) تسحب البيانات من Windsor
# كل REFRESH_INTERVAL ثانية وتحدّث الكاش بصمت. أما /api/data فلا يتصل
# بـ Windsor إطلاقًا — يعيد فقط آخر نسخة جاهزة من الذاكرة، فوريًا
# (أقل من 5 مللي ثانية) بغض النظر عن سرعة Windsor أو حالة الشبكة.
# ============================================================
CACHE = {
    "meta_ads": {"data": []},
    "tiktok_ads": {"data": []},
    "google_ads": {"data": []},
}
CACHE_UPDATED_AT = 0
CACHE_READY = asyncio.Event()   # يُرفع بعد أول تحديث ناجح واحد على الأقل


async def fetch_platform(client: httpx.AsyncClient, name: str, url: str):
    """يجلب منصة واحدة بأمان — فشل منصة واحدة لا يوقف تحديث البقية،
    ولا يمسح آخر بيانات ناجحة لها (يبقيها في الكاش بدل تصفيرها)."""
    try:
        res = await client.get(url, timeout=FETCH_TIMEOUT)
        if res.status_code == 200:
            return name, res.json()
        print(f"⚠️ {name}: HTTP {res.status_code}")
    except Exception as e:
        print(f"⚠️ {name}: فشل التحديث ({e}) — سيتم الاحتفاظ بآخر بيانات صالحة")
    return name, None  # None = فشل → لا نلمس الكاش الحالي لهذه المنصة


async def refresh_cache_once():
    global CACHE_UPDATED_AT
    if not WINDSOR_API_KEY:
        print("⚠️ WINDSOR_API_KEY غير معرف — تخطي التحديث")
        return

    params = f"?api_key={WINDSOR_API_KEY}&date_preset=last_7d"
    urls = {
        "meta_ads": f"https://connectors.windsor.ai/facebook{params}&account_id={META_ACCOUNTS}&fields=date,campaign_name,clicks,impressions,spend,conversions,actions",
        "tiktok_ads": f"https://connectors.windsor.ai/tiktok{params}&account_id={TIKTOK_ACCOUNTS}&fields=date,campaign_name,clicks,impressions,spend,conversions,ctr,cpc",
        "google_ads": f"https://connectors.windsor.ai/google_ads{params}&fields=date,campaign_name,clicks,impressions,spend,conversions",
    }

    async with httpx.AsyncClient() as client:
        # الثلاث منصات تُسحب بالتوازي، وكل واحدة مستقلة عن الأخرى تمامًا
        results = await asyncio.gather(*[
            fetch_platform(client, name, url) for name, url in urls.items()
        ])

    for name, payload in results:
        if payload is not None:
            CACHE[name] = payload

    CACHE_UPDATED_AT = time.time()
    CACHE_READY.set()
    print(f"✅ تحديث الكاش تم — {time.strftime('%H:%M:%S')}")


async def background_refresh_loop():
    # أول تحديث فوري عند إقلاع السيرفر (مرة واحدة فقط لكل نشر/إعادة تشغيل،
    # وليس لكل زائر) — بعده يستمر تلقائيًا كل REFRESH_INTERVAL ثانية
    while True:
        await refresh_cache_once()
        await asyncio.sleep(REFRESH_INTERVAL)


@asynccontextmanager
async def lifespan(app: FastAPI):
    refresh_task = asyncio.create_task(background_refresh_loop())

    # بوت keep-alive لمنع خمول Render المجاني (سبب شائع جدًا لبطء أول تحميل
    # بعد فترة عدم استخدام — Render يوقف الخدمة تمامًا بعد ~15 دقيقة خمول)
    async def keep_alive():
        await asyncio.sleep(10)
        while True:
            render_url = os.getenv("RENDER_EXTERNAL_URL")
            if render_url:
                try:
                    async with httpx.AsyncClient() as client:
                        await client.get(render_url, timeout=5.0)
                        print("⚡ Keep-alive ping sent successfully!")
                except Exception as e:
                    print(f"⚠️ Keep-alive ping failed: {e}")
            await asyncio.sleep(240)  # كل 4 دقائق — أقصر من مهلة الخمول 15 دقيقة بهامش أمان

    keepalive_task = asyncio.create_task(keep_alive())
    yield
    refresh_task.cancel()
    keepalive_task.cancel()


app = FastAPI(lifespan=lifespan)


@app.get("/api/data")
async def get_data():
    """لا يتصل بـ Windsor أبدًا — يعيد فقط آخر نسخة جاهزة في الذاكرة.
    استجابة فورية دائمًا، حتى لو كانت Windsor نفسها بطيئة أو متعطلة."""
    return JSONResponse(CACHE, headers={"X-Cache-Age-Seconds": str(round(time.time() - CACHE_UPDATED_AT, 1)) if CACHE_UPDATED_AT else "n/a"})


@app.get("/api/status")
async def get_status():
    """للتشخيص السريع: هل الكاش دافئ؟ ومتى آخر تحديث فعلي؟"""
    age = round(time.time() - CACHE_UPDATED_AT, 1) if CACHE_UPDATED_AT else None
    return {
        "cache_ready": CACHE_READY.is_set(),
        "last_update_seconds_ago": age,
        "refresh_interval_seconds": REFRESH_INTERVAL,
        "rows": {k: len(v.get("data", [])) for k, v in CACHE.items()},
    }


@app.get("/", response_class=HTMLResponse)
def home():
    return """
    <!DOCTYPE html>
    <html lang="ar" dir="rtl">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>لوحة تحليلات الإعلانات المتقدمة</title>
        <link rel="preconnect" href="https://cdn.jsdelivr.net">
        <link rel="preconnect" href="https://cdnjs.cloudflare.com">
        <script src="https://cdn.jsdelivr.net/npm/chart.js" defer></script>
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
                flex-wrap: wrap;
                gap: 12px;
            }
            .header-title h1 { margin: 0; font-size: 22px; font-weight: 800; color: #fff; }
            .header-title p { margin: 6px 0 0; font-size: 13px; color: var(--text-muted); }
            .header-title p.stale { color: var(--accent-gold); }

            .actions-group { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; }
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
                    <p id="updateTime">جاري تحميل آخر نسخة محفوظة...</p>
                </div>
                <div class="actions-group">
                    <button class="btn btn-accent" onclick="fetchAndAnalyze(true)"><i class="fa-solid fa-rotate"></i> تحديث سريع</button>
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
                    <h3><span class="dot dot-tiktok"></span> TikTok Ads - تقييم الأداء والجاذبية</h3>
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
                        <tr><td colspan="7" style="text-align:center; color:var(--text-muted);">جاري القراءة...</td></tr>
                    </tbody>
                </table>
            </div>

            <!-- Meta Section -->
            <div class="section-card">
                <div class="section-header">
                    <h3><span class="dot dot-meta"></span> Meta Ads - تقييم الحملات والمحادثات</h3>
                </div>
                <table>
                    <thead>
                        <tr>
                            <th>الحملة</th>
                            <th>التقييم الذكي</th>
                            <th>الإنفاق</th>
                            <th>الظهور</th>
                            <th>النقرات</th>
                            <th>CPC</th>
                            <th>المحادثات / الرسائل</th>
                        </tr>
                    </thead>
                    <tbody id="metaTable">
                        <tr><td colspan="7" style="text-align:center; color:var(--text-muted);">جاري القراءة...</td></tr>
                    </tbody>
                </table>
            </div>

            <!-- Google Section -->
            <div class="section-card">
                <div class="section-header">
                    <h3><span class="dot dot-google"></span> Google Ads - تقييم النتائج</h3>
                </div>
                <table>
                    <thead>
                        <tr>
                            <th>الحملة</th>
                            <th>التقييم الذكي</th>
                            <th>الإنفاق</th>
                            <th>الظهور</th>
                            <th>النقرات</th>
                            <th>CPC</th>
                            <th>التحويلات</th>
                        </tr>
                    </thead>
                    <tbody id="googleTable">
                        <tr><td colspan="7" style="text-align:center; color:var(--text-muted);">جاري القراءة...</td></tr>
                    </tbody>
                </table>
            </div>

            <!-- Smart Diagnostics Box -->
            <div class="diag-box">
                <div class="diag-title"><i class="fa-solid fa-lightbulb"></i> التحليل البرمجي التلقائي الشامل لجميع المنصات</div>
                <div class="diag-list" id="diagList">
                    <div class="diag-item diag-yellow"><i class="fa-solid fa-spinner fa-spin"></i> جاري قراءة بيانات المنصات لاستخراج التوصيات...</div>
                </div>
            </div>

            <!-- Chart -->
            <div class="chart-card">
                <div class="chart-title">مقارنة الإنفاق والنتائج حسب المنصة</div>
                <div style="height: 250px;">
                    <canvas id="spendChart"></canvas>
                </div>
            </div>
        </div>

        <div class="toast" id="toast">✅ تم نسخ التقرير الحافظة بنجاح!</div>

        <script>
            const CACHE_KEY = "ads_dashboard_cache_v1";
            let myChart = null;
            let currentSummaryText = "";

            // ============================================================
            // الخطوة 1: ارسم فورًا أي نسخة محفوظة محليًا من الزيارة السابقة
            // (Stale-While-Revalidate) — الزائر يرى أرقامًا خلال أجزاء من
            // الثانية بدل شاشة "جاري القراءة" الفارغة، حتى قبل اكتمال أي
            // اتصال بالشبكة.
            // ============================================================
            function loadFromLocalCache() {
                try {
                    const raw = localStorage.getItem(CACHE_KEY);
                    if (!raw) return false;
                    const cached = JSON.parse(raw);
                    renderData(cached.data, cached.savedAt, true);
                    return true;
                } catch (e) { return false; }
            }

            function saveToLocalCache(data) {
                try {
                    localStorage.setItem(CACHE_KEY, JSON.stringify({ data, savedAt: Date.now() }));
                } catch (e) { /* ignore quota errors */ }
            }

            // ============================================================
            // الخطوة 2: اطلب من السيرفر — وهو الآن يرد فورًا من كاش خلفي
            // دافئ دائمًا (راجع main.py: /api/data لا يتصل بـ Windsor إطلاقًا
            // أثناء الطلب) بدل الانتظار على استجابة Windsor نفسها.
            // ============================================================
            async function fetchAndAnalyze(forceSpinner = false) {
                if (forceSpinner) {
                    document.getElementById('updateTime').innerText = "جاري التحديث...";
                }
                try {
                    let res = await fetch('/api/data', { cache: 'no-store' });
                    let data = await res.json();
                    renderData(data, Date.now(), false);
                    saveToLocalCache(data);
                } catch (e) {
                    console.error(e);
                    document.getElementById('updateTime').innerText = "تعذّر الوصول للسيرفر — يُعرض آخر نسخة محفوظة محليًا.";
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

            function evaluateCampaign(ctr, cpc, spend, conv, platform) {
                if (platform === 'TikTok') {
                    if (ctr >= 1.0 && (cpc <= 1.5 || cpc === 0)) {
                        return { html: '<span class="badge badge-success"><i class="fa-solid fa-circle-check"></i> ناجح ممتاز</span>', status: 'ناجح' };
                    } else if (ctr >= 0.6) {
                        return { html: '<span class="badge badge-warning"><i class="fa-solid fa-triangle-exclamation"></i> متوسط الجاذبية</span>', status: 'متوسط' };
                    } else {
                        return { html: '<span class="badge badge-danger"><i class="fa-solid fa-circle-xmark"></i> ضعيف / فاشل</span>', status: 'فاشل' };
                    }
                } else {
                    if (conv > 0 || (cpc > 0 && cpc <= 2.0 && spend > 0)) {
                        return { html: '<span class="badge badge-success"><i class="fa-solid fa-circle-check"></i> أداء ممتاز</span>', status: 'ناجح' };
                    } else if (spend > 0 && conv === 0 && spend > 50) {
                        return { html: '<span class="badge badge-danger"><i class="fa-solid fa-circle-xmark"></i> مرتفع التكلفة</span>', status: 'فاشل' };
                    } else {
                        return { html: '<span class="badge badge-warning"><i class="fa-solid fa-clock"></i> تحت المراقبة</span>', status: 'متوسط' };
                    }
                }
            }

            function renderData(data, savedAt, isFromLocalCache) {
                const ageLabel = isFromLocalCache
                    ? `آخر نسخة محفوظة على جهازك — ${new Date(savedAt).toLocaleTimeString('ar-SA')} (جاري التحديث في الخلفية...)`
                    : `تم تحديث البيانات: ${new Date(savedAt).toLocaleTimeString('ar-SA')}`;
                const timeEl = document.getElementById('updateTime');
                timeEl.innerText = ageLabel;
                timeEl.classList.toggle('stale', !!isFromLocalCache);

                let mSpend = 0, mConv = 0, mClicks = 0;
                let tSpend = 0, tConv = 0, tClicks = 0;
                let gSpend = 0, gConv = 0, gClicks = 0;

                let allEvaluatedCampaigns = [];

                // 1. TikTok Data
                let tHtml = '';
                if (data.tiktok_ads && data.tiktok_ads.data && data.tiktok_ads.data.length > 0) {
                    data.tiktok_ads.data.forEach(r => {
                        let sp = parseFloat(r.spend || 0);
                        let clk = parseInt(r.clicks || 0);
                        let imp = parseInt(r.impressions || 0);
                        let cv = parseFloat(r.conversions || 0);
                        let ctr = r.ctr ? (parseFloat(r.ctr) * (parseFloat(r.ctr) < 1 ? 100 : 1)) : (imp > 0 ? (clk / imp) * 100 : 0);
                        let cpc = clk > 0 ? (sp / clk) : parseFloat(r.cpc || 0);

                        tSpend += sp; tConv += cv; tClicks += clk;

                        let evalRes = evaluateCampaign(ctr, cpc, sp, cv, 'TikTok');
                        allEvaluatedCampaigns.push({ platform: 'TikTok', name: r.campaign_name, spend: sp, ctr: ctr, cpc: cpc, conv: cv, status: evalRes.status });

                        tHtml += `<tr>
                            <td><strong>${r.campaign_name || 'حملة بدون اسم'}</strong></td>
                            <td>${evalRes.html}</td>
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
                        let cpc = clk > 0 ? (sp / clk) : 0;
                        let ctr = imp > 0 ? (clk / imp) * 100 : 0;

                        mSpend += sp; mConv += cv; mClicks += clk;

                        let evalRes = evaluateCampaign(ctr, cpc, sp, cv, 'Meta');
                        allEvaluatedCampaigns.push({ platform: 'Meta', name: r.campaign_name, spend: sp, ctr: ctr, cpc: cpc, conv: cv, status: evalRes.status });

                        mHtml += `<tr>
                            <td><strong>${r.campaign_name || 'حملة بدون اسم'}</strong></td>
                            <td>${evalRes.html}</td>
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
                document.getElementById('metaTable').innerHTML = mHtml || '<tr><td colspan="7" style="text-align:center">لا توجد بيانات متاحة لـ Meta</td></tr>';

                // 3. Google Data
                let gHtml = '';
                if (data.google_ads && data.google_ads.data && data.google_ads.data.length > 0) {
                    data.google_ads.data.forEach(r => {
                        let sp = parseFloat(r.spend || 0);
                        let clk = parseInt(r.clicks || 0);
                        let imp = parseInt(r.impressions || 0);
                        let cv = parseFloat(r.conversions || 0);
                        let cpc = clk > 0 ? (sp / clk) : 0;
                        let ctr = imp > 0 ? (clk / imp) * 100 : 0;

                        gSpend += sp; gConv += cv; gClicks += clk;

                        let evalRes = evaluateCampaign(ctr, cpc, sp, cv, 'Google');
                        allEvaluatedCampaigns.push({ platform: 'Google', name: r.campaign_name, spend: sp, ctr: ctr, cpc: cpc, conv: cv, status: evalRes.status });

                        gHtml += `<tr>
                            <td><strong>${r.campaign_name || 'حملة بدون اسم'}</strong></td>
                            <td>${evalRes.html}</td>
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
                document.getElementById('googleTable').innerHTML = gHtml || '<tr><td colspan="7" style="text-align:center">لا توجد بيانات متاحة لـ Google</td></tr>';

                // Totals
                let totalSp = mSpend + tSpend + gSpend;
                let totalCv = mConv + tConv + gConv;
                document.getElementById('totalSpend').innerText = totalSp.toFixed(2) + ' ر.س';
                document.getElementById('totalConversions').innerText = `إجمالي النتائج والمحادثات: ${totalCv}`;

                runDiagnostics(allEvaluatedCampaigns);

                currentSummaryText = `تقرير أداء الإعلانات الشامل:\n` +
                    `- إجمالي الإنفاق: ${totalSp.toFixed(2)} ر.س | إجمالي النتائج: ${totalCv}\n` +
                    `- Meta: إنفاق ${mSpend.toFixed(2)} ر.س | نتائج ${mConv}\n` +
                    `- TikTok: إنفاق ${tSpend.toFixed(2)} ر.س | نتائج ${tConv}\n` +
                    `- Google: إنفاق ${gSpend.toFixed(2)} ر.س | نتائج ${gConv}\n\n` +
                    `تفاصيل تقييم الحملات على المنصات:\n` +
                    allEvaluatedCampaigns.map(c => `• [${c.platform}] ${c.name}: حالة (${c.status}) | CTR: ${c.ctr.toFixed(2)}% | CPC: ${c.cpc.toFixed(2)} ر.س | الإنفاق: ${c.spend.toFixed(2)} ر.س`).join('\n');

                renderChart(['Meta Ads', 'TikTok Ads', 'Google Ads'], [mSpend, tSpend, gSpend]);
            }

            function runDiagnostics(campaigns) {
                let diagList = document.getElementById('diagList');
                let items = [];

                let winners = campaigns.filter(c => c.status === 'ناجح');
                winners.forEach(w => {
                    items.push(`<div class="diag-item diag-green"><i class="fa-solid fa-circle-check"></i> <strong>حملة ناجحة [${w.platform}]:</strong> الحملة <strong>"${w.name}"</strong> تحقق أداءً جيداً (الإنفاق: ${w.spend.toFixed(2)} ر.س). يُنصح باستمرارها أو زيادة ميزانيتها.</div>`);
                });

                let failed = campaigns.filter(c => c.status === 'فاشل' && c.spend > 10);
                failed.forEach(f => {
                    items.push(`<div class="diag-item diag-red"><i class="fa-solid fa-triangle-exclamation"></i> <strong>تنبيه إعلان ضعيف [${f.platform}]:</strong> الحملة <strong>"${f.name}"</strong> تحقق أداءً ضعيفاً موازنة بالإنفاق (${f.spend.toFixed(2)} ر.س). يفضل إيقافها أو تحسين المحتوى.</div>`);
                });

                if (items.length === 0) {
                    items.push('<div class="diag-item diag-green"><i class="fa-solid fa-shield-halved"></i> <strong>حالة الإعلانات:</strong> جميع الحملات المكتشفة تعمل بتوازن ومجمل مؤشراتها ضمن النطاق الطبيعي.</div>');
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

            // ابدأ فورًا برسم أي نسخة محفوظة محليًا (بلا أي انتظار شبكة)،
            // ثم اجلب النسخة الحيّة من السيرفر (سريعة الآن لأنها من كاش خلفي دافئ)
            const hadLocalCache = loadFromLocalCache();
            fetchAndAnalyze(!hadLocalCache);

            // تحديث تلقائي كل دقيقتين بصمت (بدون Spinner) طالما الصفحة مفتوحة
            setInterval(() => fetchAndAnalyze(false), 120000);
        </script>
    </body>
    </html>
    """
