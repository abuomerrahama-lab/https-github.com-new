import os
import asyncio
import logging
import secrets
import hmac
import hashlib
import base64
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, Any
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, Depends, HTTPException, status
from urllib.parse import parse_qs
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
import httpx

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ads-dashboard")

# ===== تحديد معدل الطلبات (اختياري بأمان) =====
# slowapi مكتبة خارجية يجب إضافتها إلى requirements.txt على Render (راجع رسالة
# التسليم). إن نُسيت أو تعذّر تثبيتها لأي سبب، لا يجب أن يتوقف التطبيق بالكامل
# عن العمل بسبب ميزة حماية إضافية واحدة - لذلك نستورد بأمان، وإن فشل الاستيراد
# نُفعّل بديلاً وهمياً (No-Op) يُعطّل تحديد المعدل فقط مع تحذير واضح في السجلات،
# بينما تستمر بقية اللوحة (المصادقة، CORS، الترويسات، البيانات) بالعمل طبيعياً.
try:
    from slowapi import Limiter, _rate_limit_exceeded_handler
    from slowapi.util import get_remote_address
    from slowapi.errors import RateLimitExceeded
    SLOWAPI_AVAILABLE = True
except ImportError:
    SLOWAPI_AVAILABLE = False

    class _NoOpLimiter:
        """بديل آمن عند غياب slowapi: `@limiter.limit(...)` يُصبح ديكوراتور
        شفّاف لا يفعل شيئاً (لا يحدّ من شيء)، بدل أن يُسقط التطبيق بالكامل."""
        def limit(self, *args, **kwargs):
            def decorator(func):
                return func
            return decorator

    Limiter = None
    get_remote_address = None
    RateLimitExceeded = Exception  # نوع استثناء بديل غير قابل للحدوث فعلياً
    _rate_limit_exceeded_handler = None

CACHE: Dict[str, Any] = {
    "meta_ads": [],
    "tiktok_ads": [],
    "google_ads": [],
    "last_updated": None
}

WINDSOR_API_KEY = os.getenv("WINDSOR_API_KEY", "").strip()
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "").strip()
REFRESH_INTERVAL_SECONDS = 90
DEFAULT_LOOKBACK_DAYS = 30

# ===== حماية الوصول: صفحة دخول مخصّصة + جلسة موقّعة (بدل Basic Auth) =====
# التحوّل من Basic Auth: نافذة تسجيل الدخول التي يفرضها المتصفح (Basic Auth)
# رمادية قياسية ولا يمكن أبداً تخصيص شكلها أو إضافة الشعار إليها - هذا قيد من
# المتصفح نفسه وليس شيئاً يمكن حله بالكود. لذلك استبدلناها بصفحة "/login" مصمّمة
# بالكامل بهوية elevenz، وعند نجاح الدخول نُصدر كوكي جلسة موقّعة (HMAC-SHA256)
# للتحقق منها لاحقاً - بدون أي مكتبة خارجية إضافية (لا itsdangerous ولا غيرها)،
# فقط وحدات بايثون القياسية (hmac/hashlib/base64/time)، حفاظاً على نفس مبدأ
# التبعيات الدنيا المتّبع في بقية الملف.
DASHBOARD_USERNAME = os.getenv("DASHBOARD_USERNAME", "").strip()
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "").strip()
AUTH_ENABLED = bool(DASHBOARD_USERNAME and DASHBOARD_PASSWORD)

if not AUTH_ENABLED:
    logger.warning(
        "⚠️ الحماية بكلمة مرور معطّلة: لم يتم ضبط DASHBOARD_USERNAME/DASHBOARD_PASSWORD "
        "في متغيرات البيئة على Render. اللوحة متاحة للجميع دون تسجيل دخول حالياً."
    )

SESSION_COOKIE_NAME = "elevenz_session"
SESSION_MAX_AGE_SECONDS = 7 * 24 * 60 * 60  # الجلسة صالحة 7 أيام قبل طلب دخول جديد
# الكوكي "secure" (لا يُرسَل إلا عبر HTTPS) تلقائياً على Render، ويُعطَّل محلياً
# (http://localhost) حيث لا تتوفر شهادة SSL أصلاً، حتى يعمل الاختبار المحلي.
_COOKIE_SECURE = bool(RENDER_EXTERNAL_URL)


def _session_secret() -> bytes:
    """مفتاح توقيع الجلسة. يُفضَّل ضبط DASHBOARD_SECRET_KEY صراحة في البيئة؛
    وإلا يُشتق تلقائياً من بيانات الدخول نفسها - يبقى ثابتاً وصالحاً طالما لم
    تتغيّر كلمة المرور، دون فرض متغير بيئة إضافي إجباري."""
    explicit = os.getenv("DASHBOARD_SECRET_KEY", "").strip()
    if explicit:
        return explicit.encode("utf-8")
    return f"elevenz:{DASHBOARD_USERNAME}:{DASHBOARD_PASSWORD}".encode("utf-8")


def create_session_token(username: str) -> str:
    """يُصدر رمز جلسة موقّعاً (اسم المستخدم + وقت الإصدار + توقيع HMAC)،
    مُرمَّزاً بـ base64 ليصلح كقيمة كوكي."""
    issued_at = str(int(time.time()))
    payload = f"{username}:{issued_at}"
    sig = hmac.new(_session_secret(), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    raw = f"{payload}:{sig}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("utf-8")


def verify_session_token(token: str) -> bool:
    """يتحقق من صحة رمز الجلسة: التوقيع صحيح (بمقارنة زمنية ثابتة تقاوم
    هجمات التوقيت)، اسم المستخدم مطابق، والجلسة لم تنتهِ صلاحيتها بعد."""
    if not token:
        return False
    try:
        raw = base64.urlsafe_b64decode(token.encode("utf-8")).decode("utf-8")
        username, issued_at, sig = raw.rsplit(":", 2)
    except Exception:
        return False

    expected_sig = hmac.new(
        _session_secret(), f"{username}:{issued_at}".encode("utf-8"), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(sig, expected_sig):
        return False
    if not hmac.compare_digest(username, DASHBOARD_USERNAME):
        return False
    try:
        if time.time() - int(issued_at) > SESSION_MAX_AGE_SECONDS:
            return False
    except ValueError:
        return False
    return True


def has_valid_session(request: Request) -> bool:
    if not AUTH_ENABLED:
        return True
    return verify_session_token(request.cookies.get(SESSION_COOKIE_NAME, ""))


async def verify_dashboard_auth(request: Request):
    """حارس (dependency) لنقاط الـ API. يتحقق من كوكي الجلسة بدل ترويسة Basic
    Auth. إن لم تُضبط بيانات الدخول في البيئة، يبقى الوصول مفتوحاً (لتفادي حظر
    صاحب اللوحة عن نفسه بالخطأ) مع تحذير واضح في السجلات (أعلاه)."""
    if not has_valid_session(request):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="الجلسة غير صالحة أو منتهية - يرجى تسجيل الدخول من جديد",
        )
    return True


LOGO_BASE64 = "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"


def render_login_page(error: bool = False) -> str:
    error_html = (
        '<div class="login-error">⚠️ اسم المستخدم أو كلمة المرور غير صحيحة</div>'
        if error else ""
    )
    return f"""
    <!DOCTYPE html>
    <html lang="ar" dir="rtl">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>تسجيل الدخول - لوحة إعلانات elevenz</title>
        <link rel="preconnect" href="https://fonts.googleapis.com">
        <link href="https://fonts.googleapis.com/css2?family=Cairo:wght@400;600;700;800&display=swap" rel="stylesheet">
        <style>
            :root {{
                --sidebar-bg: #071c35;
                --sidebar-bg-2: #0a2544;
                --main-bg: #f4f6fa;
                --card-bg: #ffffff;
                --text-dark: #0f2540;
                --text-muted: #8592a6;
                --accent-orange: #f05a28;
                --border-color: #eef1f6;
                --radius-lg: 18px;
                --radius-sm: 10px;
                --shadow-pop: 0 20px 50px -12px rgba(7, 28, 53, 0.35);
            }}
            * {{ box-sizing: border-box; }}
            body {{
                font-family: 'Cairo', sans-serif;
                margin: 0;
                min-height: 100vh;
                display: flex;
                align-items: center;
                justify-content: center;
                background: radial-gradient(circle at 20% 20%, #0e2c50 0%, var(--sidebar-bg) 55%, #05121f 100%);
                direction: rtl;
                padding: 20px;
            }}
            .login-card {{
                background: var(--card-bg);
                border-radius: var(--radius-lg);
                box-shadow: var(--shadow-pop);
                padding: 40px 36px 34px;
                width: 100%;
                max-width: 380px;
                text-align: center;
            }}
            .login-logo {{ max-width: 190px; height: auto; margin-bottom: 22px; }}
            .login-card h1 {{ font-size: 19px; font-weight: 800; color: var(--text-dark); margin: 0 0 6px; }}
            .login-sub {{ font-size: 13px; color: var(--text-muted); margin: 0 0 26px; font-weight: 500; }}
            .login-error {{
                background: #fef1f0; border: 1px solid #fbd6d2; color: #d13b2c;
                border-radius: var(--radius-sm); padding: 10px 14px; font-size: 12.5px;
                font-weight: 700; margin-bottom: 18px;
            }}
            form {{ text-align: right; }}
            label {{ display: block; font-size: 12.5px; font-weight: 700; color: var(--text-dark); margin-bottom: 6px; }}
            input {{
                width: 100%; border: 1px solid var(--border-color); border-radius: var(--radius-sm);
                padding: 12px 14px; font-family: inherit; font-size: 14px; color: var(--text-dark);
                background: #f8fafc; margin-bottom: 16px; transition: border-color 0.15s ease;
            }}
            input:focus {{ outline: none; border-color: var(--accent-orange); background: #fff; }}
            button {{
                width: 100%; background: var(--accent-orange); color: #fff; border: none;
                border-radius: var(--radius-sm); padding: 13px; font-family: inherit; font-size: 14.5px;
                font-weight: 800; cursor: pointer; box-shadow: 0 8px 18px -6px rgba(240, 90, 40, 0.5);
                transition: opacity 0.15s ease;
            }}
            button:hover {{ opacity: 0.92; }}
            .login-footer {{ margin-top: 22px; font-size: 11px; color: var(--text-muted); }}
        </style>
    </head>
    <body>
        <div class="login-card">
            <img class="login-logo" src="data:image/png;base64,{LOGO_BASE64}" alt="elevenz">
            <h1>لوحة الدخول للإعلانات</h1>
            <p class="login-sub">الرجاء تسجيل الدخول للمتابعة إلى منصة التقارير الإعلانية</p>
            {error_html}
            <form method="POST" action="/login">
                <label>اسم المستخدم</label>
                <input type="text" name="username" required autofocus autocomplete="username">
                <label>كلمة المرور</label>
                <input type="password" name="password" required autocomplete="current-password">
                <button type="submit">تسجيل الدخول</button>
            </form>
            <div class="login-footer">منصة إعلانات elevenz — تقارير الأداء الإعلاني</div>
        </div>
    </body>
    </html>
    """


# ===== النطاقات المسموح لها بطلب الـ API عبر CORS =====
# نفس الأصل (same-origin) لا يحتاج أصلاً لرؤوس CORS - هذه الإضافة تمنع
# مواقع أخرى من تضمين هذا الـ API داخل صفحاتها عبر JavaScript من نطاق مختلف.
_allowed_origins = ["http://localhost:8000", "http://127.0.0.1:8000"]
if RENDER_EXTERNAL_URL:
    _allowed_origins.append(RENDER_EXTERNAL_URL.rstrip("/"))

# ===== تحديد معدل الطلبات (Rate Limiting) =====
if SLOWAPI_AVAILABLE:
    limiter = Limiter(key_func=get_remote_address)
else:
    logger.warning(
        "⚠️ مكتبة slowapi غير مثبّتة - تحديد معدل الطلبات معطّل مؤقتاً. "
        "أضف 'slowapi' إلى requirements.txt على Render وأعد النشر لتفعيله."
    )
    limiter = _NoOpLimiter()

# حقول أساسية ومضمونة الدعم في موصلات Windsor.ai (مرجع واحد يُستخدم في
# حلقة التحديث ونقطة التشخيص معاً لتفادي أي تعارض بين المكانين)
CONNECTOR_FIELDS = {
    "facebook": "account_name,campaign,adset_name,ad_name,clicks,spend,conversions,impressions,cpc,ctr,date,actions",
    "tiktok": "account_name,campaign_name,adgroup_name,ad_name,clicks,spend,conversion,conversions,impressions,cpc,ctr,date",
    "google_ads": "account_name,campaign,ad_group_name,ad_name,clicks,spend,conversions,impressions,cpc,ctr,date",
}


def get_default_date_range(days: int = DEFAULT_LOOKBACK_DAYS):
    """يحسب نطاق تاريخ افتراضي (date_from / date_to) بصيغة YYYY-MM-DD.
    يغطي آخر `days` يوماً بما فيها اليوم الحالي، لضمان أن Windsor.ai
    لا يُرجع بيانات فارغة بسبب غياب نطاق زمني صريح في الطلب."""
    today = datetime.now(timezone.utc).date()
    date_from = today - timedelta(days=days)
    return date_from.isoformat(), today.isoformat()


async def fetch_windsor_connector(connector: str, params: dict) -> list:
    """يجلب بيانات من موصل Windsor.ai معيّن، مع:
    - إرسال نطاق تاريخ افتراضي (آخر 30 يوماً) تلقائياً إن لم يُحدَّد صراحة.
    - تسجيل تفصيلي للأخطاء (رمز الحالة + نص الاستجابة) لتسهيل تتبع الأعطال.
    - إرجاع قائمة فارغة دائماً عند أي فشل، حتى لا يتسبب في كسر asyncio.gather
      أو باقي مسار التحديث."""
    if not WINDSOR_API_KEY:
        logger.warning(f"WINDSOR_API_KEY غير مضبوط - تم تخطي الاتصال بموصل '{connector}'")
        return []

    url = f"https://connectors.windsor.ai/{connector}"
    date_from, date_to = get_default_date_range()

    base_params = {
        "api_key": WINDSOR_API_KEY,
        "_renderer": "json",
        "date_from": date_from,
        "date_to": date_to,
    }
    # السماح باستبدال fields أو date_from/date_to إذا مُرِّرت صراحة في params
    base_params.update(params)

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            res = await client.get(url, params=base_params)

            if res.status_code != 200:
                logger.error(
                    f"فشل طلب Windsor لموصل '{connector}' - رمز الحالة: {res.status_code} - "
                    f"نص الاستجابة: {res.text[:500]}"
                )
                return []

            try:
                res_data = res.json()
            except Exception as parse_err:
                logger.error(
                    f"تعذر تحليل JSON من موصل '{connector}': {parse_err} - "
                    f"نص الاستجابة: {res.text[:500]}"
                )
                return []

            if isinstance(res_data, dict):
                data_field = res_data.get("data")
                if isinstance(data_field, list):
                    return data_field
                if "error" in res_data:
                    logger.error(f"خطأ من Windsor لموصل '{connector}': {res_data.get('error')}")
                    return []
                logger.warning(
                    f"استجابة غير متوقعة (dict بدون قائمة data) من موصل '{connector}': "
                    f"{str(res_data)[:300]}"
                )
                return []
            elif isinstance(res_data, list):
                return res_data
            else:
                logger.warning(f"نوع استجابة غير معروف من موصل '{connector}': {type(res_data)}")
                return []

    except httpx.TimeoutException:
        logger.error(f"انتهت مهلة الاتصال (Timeout) بموصل '{connector}'")
        return []
    except Exception as e:
        logger.error(f"خطأ غير متوقع أثناء جلب بيانات '{connector}': {e}")
        return []


# حقول "الحالة الفعلية" (نشطة/متوقفة) من كل منصة. جميع الأسماء أدناه مؤكدة
# 100% (وليست تخميناً) بعد التحقق المباشر من حساب Windsor.ai الفعلي للمستخدم
# عبر أداة get_fields لكل موصل (Meta, TikTok, Google Ads) بتاريخ هذا التحديث.
# يبقى نظام "المستويات" (Tiers) قائماً كطبقة أمان إضافية فقط - وليس لأن الأسماء
# مشكوك فيها - بحيث لو مُنعت إحدى الحقول لسبب خاص بحساب أو باقة معينة، يتراجع
# النظام تلقائياً دون أي تأثير على بيانات الأداء الأساسية.
CONNECTOR_STATUS_FIELD_TIERS = {
    "facebook": [
        "campaign_status,adset_status,effective_status",
        "",
    ],
    "tiktok": [
        # مؤكدة جميعها عبر get_fields: campaign_operation_status/ad_group_operation_status/
        # ad_operation_status تُرجع Enum نظيف (ENABLE/DISABLE/FROZEN)، والحقول الثلاثة
        # الأخرى نصية عامة (Campaign/Adgroup/Ad Status) كبديل احتياطي إضافي بنفس الطلب
        "campaign_operation_status,ad_group_operation_status,ad_operation_status,campaign_status,adgroup_status,ad_status",
        "",
    ],
    "google_ads": [
        "campaign_status,ad_group_status,ad_group_ad_status",
        "",
    ],
}


async def fetch_platform_data(connector: str, date_from: str, date_to: str) -> list:
    """يجلب بيانات المنصة، ويحاول تضمين حقول الحالة الفعلية (نشطة/متوقفة) من
    Windsor.ai متى أمكن، بتجربة عدة مجموعات حقول تلقائياً من الأكثر اكتمالاً
    إلى الأكثر أماناً. إن فشلت كل المحاولات التي تتضمن حقول حالة (بسبب اسم حقل
    غير مدعوم لهذا الحساب تحديداً) يعود تلقائياً لجلب الحقول الأساسية فقط دون أي
    تأثير على صحة بيانات الأداء (الإنفاق/النقرات/الظهور...) نفسها."""
    base_fields = CONNECTOR_FIELDS[connector]
    tiers = CONNECTOR_STATUS_FIELD_TIERS.get(connector, [""])

    for tier_fields in tiers:
        combined_fields = f"{base_fields},{tier_fields}" if tier_fields else base_fields
        rows = await fetch_windsor_connector(connector, {
            "fields": combined_fields, "date_from": date_from, "date_to": date_to
        })
        if rows:
            if tier_fields:
                logger.info(f"موصل '{connector}': نجح جلب حقول الحالة ({tier_fields})")
            return rows
        elif tier_fields:
            logger.warning(f"موصل '{connector}': لم تُرجع حقول الحالة ({tier_fields}) بيانات، تجربة مجموعة أبسط...")

    return []


async def refresh_cache_and_keep_alive():
    global CACHE
    while True:
        try:
            date_from, date_to = get_default_date_range()
            logger.info(f"جاري تحديث بيانات إعلانات elevenz... (النطاق الزمني: {date_from} إلى {date_to})")

            meta_res, tiktok_res, google_res = await asyncio.gather(
                fetch_platform_data("facebook", date_from, date_to),
                fetch_platform_data("tiktok", date_from, date_to),
                fetch_platform_data("google_ads", date_from, date_to),
                return_exceptions=True
            )

            new_cache = {
                "meta_ads": meta_res if isinstance(meta_res, list) else [],
                "tiktok_ads": tiktok_res if isinstance(tiktok_res, list) else [],
                "google_ads": google_res if isinstance(google_res, list) else [],
                "last_updated": asyncio.get_event_loop().time(),
            }
            CACHE = new_cache

            logger.info(
                f"تم التحديث: Meta={len(CACHE['meta_ads'])} صف، "
                f"TikTok={len(CACHE['tiktok_ads'])} صف، "
                f"Google={len(CACHE['google_ads'])} صف"
            )
            
            target_url = RENDER_EXTERNAL_URL.rstrip('/') if RENDER_EXTERNAL_URL else "http://127.0.0.1:8000"
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    await client.get(f"{target_url}/api/status")
            except Exception:
                pass

        except Exception as e:
            logger.error(f"Cache error: {e}")
            
        await asyncio.sleep(REFRESH_INTERVAL_SECONDS)

@asynccontextmanager
async def lifespan(app: FastAPI):
    """يستبدل @app.on_event('startup') المهجورة في FastAPI الحديث. يبدأ حلقة
    التحديث الخلفية عند إقلاع التطبيق، ويُلغيها بأمان عند إيقافه."""
    task = asyncio.create_task(refresh_cache_and_keep_alive())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

app = FastAPI(title="منصة إعلانات elevenz", lifespan=lifespan)

app.state.limiter = limiter
if SLOWAPI_AVAILABLE:
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=True,
    allow_methods=["GET"],
    allow_headers=["*"],
)


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    """يضيف ترويسات أمان موصى بها لكل استجابة. ملاحظة صريحة حول CSP: الصفحة
    الحالية تعتمد بالكامل على CSS وJavaScript مضمّنين داخل HTML نفسه (وليس
    ملفات خارجية منفصلة)، لذلك لا بد من 'unsafe-inline' في script-src/style-src
    حالياً - وإلا تتعطل اللوحة بالكامل. لرفع الحماية لاحقاً لمستوى أعلى (بلا
    unsafe-inline) يتطلب ذلك فصل كل الأكواد المضمّنة إلى ملفات خارجية مع نظام
    nonce، وهو تغيير معماري أكبر خارج نطاق هذا التحديث."""
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    # يُخبر المتصفح بتفضيل HTTPS دائماً لهذا النطاق مستقبلاً (Render يوفّر HTTPS
    # تلقائياً بالفعل؛ هذه الترويسة إضافة دفاعية بلا أي خطر حلقة إعادة توجيه).
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    # يمنع المتصفح من طلب أذونات (كاميرا/ميكروفون/موقع...) لا تحتاجها اللوحة إطلاقاً
    response.headers["Permissions-Policy"] = (
        "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
    )
    # لوحة بيانات تجارية خاصة - لا فائدة تُرجى من التخزين المؤقت، وتعطيله طبقة
    # خصوصية إضافية تمنع بقاء بيانات الحملات/الإنفاق في ذاكرة تخزين المتصفح
    # المؤقتة أو أي وسيط شبكي (proxy) بعد إغلاق الجلسة.
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data:; "
        "connect-src 'self';"
    )
    return response


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: int = 0):
    """صفحة الدخول المخصّصة بهوية elevenz. إن كانت الحماية معطّلة أصلاً، أو كانت
    هناك جلسة صالحة بالفعل، تُوجّه مباشرة للوحة الرئيسية دون عرض النموذج."""
    if not AUTH_ENABLED or has_valid_session(request):
        return RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)
    return HTMLResponse(content=render_login_page(error=bool(error)))


@app.post("/login")
@limiter.limit("10/minute")
async def login_submit(request: Request):
    """يتحقق من بيانات الدخول (مقارنة زمنية ثابتة تقاوم هجمات التوقيت)، وعند
    النجاح يُصدر كوكي جلسة موقّعة صالحة لمدة 7 أيام. محدود بـ10 محاولات/دقيقة
    لكل عنوان IP لإبطاء أي محاولة تخمين آلية.

    ملاحظة تقنية: نقرأ جسم الطلب ونحلّله يدوياً بدل استخدام معامل FastAPI
    القياسي `Form(...)`، لأن ذاك المعامل يتطلب مكتبة خارجية إضافية
    (python-multipart) حتى لتحليل نموذج urlencoded بسيط - وهي بالضبط المكتبة
    الناقصة التي أسقطت النشر آخر مرة. التحليل هنا يستخدم فقط `urllib.parse`
    القياسية في بايثون، فلا يمكن لأي حزمة ناقصة أن تُسقط تسجيل الدخول مجدداً."""
    if not AUTH_ENABLED:
        return RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)

    body = await request.body()
    fields = parse_qs(body.decode("utf-8", errors="ignore"))
    username = fields.get("username", [""])[0].strip()
    password = fields.get("password", [""])[0].strip()

    if secrets.compare_digest(username, DASHBOARD_USERNAME) and secrets.compare_digest(
        password, DASHBOARD_PASSWORD
    ):
        token = create_session_token(DASHBOARD_USERNAME)
        response = RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
        response.set_cookie(
            key=SESSION_COOKIE_NAME,
            value=token,
            max_age=SESSION_MAX_AGE_SECONDS,
            httponly=True,
            secure=_COOKIE_SECURE,
            samesite="lax",
        )
        return response

    return RedirectResponse(url="/login?error=1", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/logout")
async def logout():
    response = RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
    response.delete_cookie(SESSION_COOKIE_NAME)
    return response


@app.get("/api/status")
async def get_status():
    # ملاحظة: هذه النقطة متروكة بلا حماية عمداً (لا تحتوي أي بيانات حساسة)،
    # لأن آلية keep-alive الداخلية في التطبيق تستدعيها دون بيانات دخول، وهي
    # عادة تُترك مفتوحة لأدوات المراقبة (Render Health Check ونحوها).
    return {"status": "ok"}

@app.get("/api/debug/windsor", dependencies=[Depends(verify_dashboard_auth)])
@limiter.limit("10/minute")
async def debug_windsor(request: Request, connector: str = "facebook", include_status: bool = False):
    """نقطة تشخيص: تنفّذ نفس طلب Windsor.ai المستخدم في التحديث التلقائي
    وتُرجع الاستجابة الخام (رمز الحالة + نص الاستجابة) دون الحاجة لقراءة
    سجلات (Logs) Render. افتح مباشرة في المتصفح:
    /api/debug/windsor?connector=facebook
    /api/debug/windsor?connector=tiktok
    /api/debug/windsor?connector=google_ads

    أضف include_status=true لاختبار حقول "الحالة الفعلية" (نشطة/متوقفة) تحديداً،
    مثال: /api/debug/windsor?connector=tiktok&include_status=true
    سيُعيد نتيجة كل مستوى (Tier) من حقول الحالة على حدة، لمعرفة أيها مدعوم فعلياً
    لحسابك. مفتاح الـ API لا يظهر أبداً في الاستجابة."""
    if connector not in CONNECTOR_FIELDS:
        return JSONResponse(content={
            "error": f"موصل غير معروف: '{connector}'",
            "supported_connectors": list(CONNECTOR_FIELDS.keys())
        }, status_code=400)

    if not WINDSOR_API_KEY:
        return JSONResponse(content={
            "error": "WINDSOR_API_KEY غير مضبوط في متغيرات البيئة (Environment Variables) على Render. "
                     "أضِفه من إعدادات الخدمة (Environment) ثم أعد النشر."
        })

    date_from, date_to = get_default_date_range()
    url = f"https://connectors.windsor.ai/{connector}"
    base_fields = CONNECTOR_FIELDS[connector]

    async def try_fields(fields: str):
        params = {
            "api_key": WINDSOR_API_KEY, "_renderer": "json",
            "date_from": date_from, "date_to": date_to, "fields": fields,
        }
        masked_url = (
            f"{url}?api_key=***&_renderer=json&date_from={date_from}"
            f"&date_to={date_to}&fields={fields}"
        )
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                res = await client.get(url, params=params)
            try:
                parsed = res.json()
            except Exception:
                parsed = None
            row_count = None
            if isinstance(parsed, dict) and isinstance(parsed.get("data"), list):
                row_count = len(parsed["data"])
            elif isinstance(parsed, list):
                row_count = len(parsed)
            return {
                "fields_requested": fields,
                "request_url_masked": masked_url,
                "status_code": res.status_code,
                "row_count": row_count,
                "raw_body_preview": res.text[:1500],
            }
        except Exception as e:
            return {"fields_requested": fields, "request_url_masked": masked_url, "error": str(e)}

    if include_status:
        tiers = CONNECTOR_STATUS_FIELD_TIERS.get(connector, [""])
        attempts = []
        for tier_fields in tiers:
            combined = f"{base_fields},{tier_fields}" if tier_fields else base_fields
            result = await try_fields(combined)
            result["tier_status_fields"] = tier_fields or "(بلا حقول حالة - الأساس فقط)"
            attempts.append(result)
        return JSONResponse(content={
            "connector": connector,
            "date_from": date_from,
            "date_to": date_to,
            "base_fields": base_fields,
            "status_field_tiers_tested": attempts,
        })

    result = await try_fields(base_fields)
    return JSONResponse(content={
        "connector": connector,
        "date_from": date_from,
        "date_to": date_to,
        **result,
        "parsed_json": None,
    })

def _valid_iso_date(value: str) -> bool:
    try:
        datetime.strptime(value, "%Y-%m-%d")
        return True
    except (ValueError, TypeError):
        return False


@app.get("/api/data", dependencies=[Depends(verify_dashboard_auth)])
@limiter.limit("15/minute")
async def get_dashboard_data(request: Request, date_from: str = None, date_to: str = None):
    # لا يوجد نطاق تاريخ مخصص → إرجاع الكاش التلقائي (يُحدَّث كل REFRESH_INTERVAL_SECONDS لآخر 30 يوماً)
    if not date_from and not date_to:
        return JSONResponse(content={
            "status": "success",
            "data": CACHE,
            "last_updated": CACHE["last_updated"]
        })

    # طُلب نطاق تاريخ مخصص من الواجهة → إعادة الاستعلام من Windsor.ai مباشرة بهذا النطاق
    if not date_from or not date_to or not _valid_iso_date(date_from) or not _valid_iso_date(date_to):
        return JSONResponse(content={
            "status": "error",
            "message": "صيغة التاريخ غير صحيحة. الصيغة المطلوبة: YYYY-MM-DD ويجب توفير date_from و date_to معاً."
        }, status_code=400)

    if date_from > date_to:
        return JSONResponse(content={
            "status": "error",
            "message": "تاريخ البداية (date_from) يجب أن يسبق أو يساوي تاريخ النهاية (date_to)."
        }, status_code=400)

    logger.info(f"طلب نطاق مخصص من الواجهة: {date_from} إلى {date_to}")

    meta_res, tiktok_res, google_res = await asyncio.gather(
        fetch_platform_data("facebook", date_from, date_to),
        fetch_platform_data("tiktok", date_from, date_to),
        fetch_platform_data("google_ads", date_from, date_to),
        return_exceptions=True
    )

    fresh_data = {
        "meta_ads": meta_res if isinstance(meta_res, list) else [],
        "tiktok_ads": tiktok_res if isinstance(tiktok_res, list) else [],
        "google_ads": google_res if isinstance(google_res, list) else [],
        "last_updated": asyncio.get_event_loop().time(),
    }

    logger.info(
        f"نتيجة النطاق المخصص ({date_from} → {date_to}): "
        f"Meta={len(fresh_data['meta_ads'])} صف، "
        f"TikTok={len(fresh_data['tiktok_ads'])} صف، "
        f"Google={len(fresh_data['google_ads'])} صف"
    )

    return JSONResponse(content={
        "status": "success",
        "data": fresh_data,
        "last_updated": fresh_data["last_updated"],
        "range": {"date_from": date_from, "date_to": date_to}
    })

@app.get("/", response_class=HTMLResponse)
@limiter.limit("30/minute")
async def serve_index(request: Request):
    if not has_valid_session(request):
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
    html_content = """
    <!DOCTYPE html>
    <html lang="ar" dir="rtl">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>منصة إعلانات elevenz</title>
        <link rel="preconnect" href="https://fonts.googleapis.com">
        <link href="https://fonts.googleapis.com/css2?family=Cairo:wght@400;500;600;700;800;900&display=swap" rel="stylesheet">
        <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
        <script src="https://cdn.jsdelivr.net/npm/chartjs-plugin-datalabels@2.2.0"></script>
        <style>
            :root {
                --sidebar-bg: #071c35;
                --sidebar-bg-2: #0a2544;
                --main-bg: #f4f6fa;
                --card-bg: #ffffff;
                --text-dark: #0f2540;
                --text-muted: #8592a6;
                --text-faint: #b0bac9;
                --accent-orange: #f05a28;
                --accent-orange-light: #fff1ec;
                --accent-blue: #2f6fed;
                --accent-green: #31a24c;
                --border-color: #eef1f6;
                --nested-bg: #fafbfd;
                --nested-deep: #f4f6fa;

                --radius-lg: 18px;
                --radius-md: 14px;
                --radius-sm: 10px;
                --shadow-card: 0 1px 2px rgba(15, 37, 64, 0.04), 0 10px 24px -14px rgba(15, 37, 64, 0.12);
                --shadow-pop: 0 12px 32px -8px rgba(15, 37, 64, 0.22);

                --stage-green-bg: #eefcf3;
                --stage-green-border: #cdf2dc;
                --stage-green-text: #128a4d;
                --stage-blue-bg: #eef4ff;
                --stage-blue-border: #d6e4ff;
                --stage-blue-text: #2f6fed;
                --stage-red-bg: #fef1f0;
                --stage-red-border: #fbd6d2;
                --stage-red-text: #d13b2c;
                --stage-gray-bg: #f2f4f7;
                --stage-gray-border: #e3e7ee;
                --stage-gray-text: #667085;
            }
            * { box-sizing: border-box; }
            body {
                font-family: 'Cairo', sans-serif;
                background-color: var(--main-bg);
                color: var(--text-dark);
                margin: 0;
                display: flex;
                height: 100vh;
                overflow: hidden;
                direction: rtl;
                -webkit-font-smoothing: antialiased;
            }
            .sidebar {
                width: 264px;
                background: linear-gradient(180deg, var(--sidebar-bg-2) 0%, var(--sidebar-bg) 100%);
                color: #ffffff;
                display: flex;
                flex-direction: column;
                padding: 22px 0;
                flex-shrink: 0;
            }
            .sidebar-logo {
                padding: 0 26px 22px 26px;
                font-size: 28px;
                font-weight: 800;
                color: #fff;
                border-bottom: 1px solid rgba(255,255,255,0.08);
                letter-spacing: -0.5px;
            }
            .sidebar-logo span { color: var(--accent-orange); }
            .sidebar-menu { list-style: none; padding: 18px 14px; margin: 0; flex: 1; }
            .sidebar-menu li { margin-bottom: 4px; }
            .sidebar-menu li a {
                display: flex;
                align-items: center;
                gap: 10px;
                padding: 12px 16px;
                color: #9fb0c7;
                text-decoration: none;
                font-weight: 600;
                font-size: 14px;
                border-radius: var(--radius-sm);
                transition: background-color 0.15s ease, color 0.15s ease;
            }
            .sidebar-menu li.active a {
                background-color: rgba(240, 90, 40, 0.16);
                color: #ffffff;
            }
            .sidebar-menu li a:hover { background-color: rgba(255,255,255,0.07); color: #ffffff; }

            .user-profile {
                margin: 8px 14px 0 14px;
                padding: 14px 16px;
                border-top: 1px solid rgba(255,255,255,0.08);
                display: flex;
                align-items: center;
                gap: 12px;
            }
            .user-avatar {
                width: 38px;
                height: 38px;
                border-radius: 50%;
                background: linear-gradient(135deg, var(--accent-orange), #ff8657);
                display: flex;
                align-items: center;
                justify-content: center;
                font-weight: bold;
                color: #fff;
                flex-shrink: 0;
            }

            .main-content {
                flex: 1;
                overflow-y: auto;
                padding: 32px 34px;
            }
            .top-bar { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 24px; gap: 16px; flex-wrap: wrap; }
            .header-title h1 { margin: 0; font-size: 23px; font-weight: 800; color: var(--text-dark); }
            .header-title p { margin: 6px 0 0 0; color: var(--text-muted); font-size: 13px; font-weight: 500; }
            .top-actions { display: flex; gap: 10px; }

            .btn {
                display: inline-flex;
                align-items: center;
                gap: 8px;
                font-weight: 700;
                border: none;
                padding: 11px 20px;
                border-radius: var(--radius-sm);
                cursor: pointer;
                font-family: inherit;
                font-size: 13.5px;
                transition: transform 0.12s ease, box-shadow 0.12s ease, opacity 0.12s ease;
            }
            .btn:active { transform: scale(0.97); }
            .btn-refresh {
                background-color: var(--accent-orange);
                color: #fff;
                box-shadow: 0 6px 16px -4px rgba(240, 90, 40, 0.45);
            }
            .btn-refresh:hover { opacity: 0.92; }
            .btn-copy {
                background-color: var(--card-bg);
                color: var(--text-dark);
                border: 1px solid var(--border-color);
                box-shadow: var(--shadow-card);
            }
            .btn-copy:hover { border-color: var(--accent-orange); color: var(--accent-orange); }
            .btn-copy svg { width: 16px; height: 16px; flex-shrink: 0; }

            .time-selector { display: flex; gap: 8px; margin-bottom: 14px; flex-wrap: wrap; align-items: center; }
            .time-btn {
                background-color: var(--card-bg);
                border: 1px solid var(--border-color);
                color: var(--text-muted);
                padding: 9px 20px;
                border-radius: 999px;
                cursor: pointer;
                font-size: 13px;
                font-weight: 700;
                font-family: inherit;
                transition: all 0.15s ease;
            }
            .time-btn:hover { border-color: var(--accent-orange); color: var(--accent-orange); }
            .time-btn.active {
                background-color: var(--sidebar-bg);
                color: #fff;
                border-color: var(--sidebar-bg);
            }
            .time-btn:disabled, .btn:disabled { opacity: 0.6; cursor: not-allowed; }
            .time-btn svg { width: 13px; height: 13px; vertical-align: -2px; margin-left: 5px; }

            .custom-range-box {
                display: none;
                align-items: center;
                gap: 8px;
                background-color: var(--card-bg);
                border: 1px solid var(--border-color);
                border-radius: 999px;
                padding: 6px 8px 6px 16px;
                flex-wrap: wrap;
            }
            .custom-range-box input[type="date"] {
                border: 1px solid var(--border-color);
                border-radius: 999px;
                padding: 7px 12px;
                font-family: inherit;
                font-size: 12.5px;
                color: var(--text-dark);
                background: var(--nested-bg);
            }
            .custom-range-box span { font-size: 12px; color: var(--text-muted); font-weight: 700; }
            .custom-range-box .btn-apply-range {
                background-color: var(--accent-orange);
                color: #fff;
                border: none;
                padding: 8px 16px;
                border-radius: 999px;
                font-family: inherit;
                font-size: 12.5px;
                font-weight: 700;
                cursor: pointer;
            }
            .custom-range-box .btn-apply-range:hover { opacity: 0.9; }
            .range-caption { margin-bottom: 26px; font-size: 12px; color: var(--text-muted); font-weight: 600; }
            .range-caption b { color: var(--text-dark); }

            .cards-container { display: grid; grid-template-columns: repeat(4, 1fr); gap: 18px; margin-bottom: 22px; }
            .card {
                background-color: var(--card-bg);
                border-radius: var(--radius-lg);
                box-shadow: var(--shadow-card);
                padding: 22px 22px;
                text-align: right;
            }
            .card-top { display: flex; align-items: center; justify-content: space-between; margin-bottom: 14px; }
            .card-title { font-size: 13px; color: var(--text-muted); font-weight: 600; }
            .card-icon {
                width: 36px; height: 36px; border-radius: 10px;
                display: flex; align-items: center; justify-content: center;
                flex-shrink: 0; overflow: hidden;
            }
            .card-icon svg { width: 20px; height: 20px; display: block; }
            .card-icon.icon-wallet svg { width: 18px; height: 18px; }
            .card-value { font-size: 27px; font-weight: 800; color: var(--text-dark); margin-bottom: 6px; letter-spacing: -0.5px; }
            .card-sub { font-size: 12.5px; font-weight: 700; }
            .card-meta { margin-top: 12px; padding-top: 12px; border-top: 1px dashed var(--border-color); display: flex; gap: 14px; font-size: 11.5px; color: var(--text-muted); font-weight: 600; }
            .card-meta span b { color: var(--text-dark); font-weight: 800; }

            .grid-2 { display: grid; grid-template-columns: 2fr 1fr; gap: 18px; margin-bottom: 22px; align-items: stretch; }

            .chart-section {
                background-color: var(--card-bg);
                border-radius: var(--radius-lg);
                box-shadow: var(--shadow-card);
                padding: 24px;
            }
            .chart-section-title { font-weight: 800; margin-bottom: 4px; color: var(--text-dark); font-size: 15px; }
            .chart-section-sub { font-size: 12px; color: var(--text-muted); margin-bottom: 16px; font-weight: 500; }
            .chart-wrapper { height: 260px; position: relative; }
            .donut-wrapper { height: 220px; position: relative; }

            .legend-list { display: flex; flex-direction: column; gap: 10px; margin-top: 16px; }
            .legend-item { display: flex; align-items: center; justify-content: space-between; font-size: 12.5px; font-weight: 600; color: var(--text-muted); }
            .legend-dot { width: 9px; height: 9px; border-radius: 50%; display: inline-block; margin-left: 8px; }
            .legend-label { display: flex; align-items: center; }
            .legend-value { color: var(--text-dark); font-weight: 800; }

            /* ===== Explorer (Meta Ads Manager style) ===== */
            .explorer-card {
                background-color: var(--card-bg);
                border-radius: var(--radius-lg);
                box-shadow: var(--shadow-card);
            }
            .explorer-sticky-zone {
                position: sticky;
                top: 0;
                z-index: 15;
                background: var(--card-bg);
                border-radius: var(--radius-lg) var(--radius-lg) 0 0;
                box-shadow: 0 6px 14px -12px rgba(15, 37, 64, 0.3);
            }
            .explorer-top {
                display: flex;
                align-items: center;
                justify-content: space-between;
                gap: 14px;
                padding: 16px 22px 0 22px;
                flex-wrap: wrap;
            }
            .platform-tabs { display: flex; gap: 6px; }
            .platform-tab {
                display: flex; align-items: center; gap: 7px;
                padding: 8px 16px; border-radius: 999px; cursor: pointer;
                font-size: 12.5px; font-weight: 700; color: var(--text-muted);
                border: 1px solid var(--border-color); background: var(--card-bg);
                transition: all 0.15s ease;
            }
            .platform-tab .pdot { width: 8px; height: 8px; border-radius: 50%; }
            .platform-tab.active { background: var(--text-dark); color: #fff; border-color: var(--text-dark); }
            .platform-tab:hover:not(.active) { border-color: var(--text-dark); color: var(--text-dark); }

            /* Compact synced date-filter dropdown, positioned top-left of the explorer */
            .date-filter-dropdown { position: relative; }
            .date-filter-trigger {
                display: flex; align-items: center; gap: 8px;
                background: var(--nested-bg); border: 1px solid var(--border-color);
                border-radius: 999px; padding: 9px 15px; font-family: inherit;
                font-size: 12.5px; font-weight: 700; color: var(--text-dark); cursor: pointer;
                white-space: nowrap;
            }
            .date-filter-trigger:hover { border-color: var(--accent-orange); }
            .date-filter-trigger svg { width: 14px; height: 14px; flex-shrink: 0; color: var(--text-muted); }
            .date-filter-trigger .chevron-down { width: 11px; height: 11px; color: var(--text-faint); transition: transform 0.15s ease; }
            .date-filter-dropdown.open .date-filter-trigger { border-color: var(--accent-orange); }
            .date-filter-dropdown.open .chevron-down { transform: rotate(180deg); }
            .date-filter-panel {
                display: none;
                position: absolute; top: calc(100% + 8px); left: 0;
                background: var(--card-bg); border: 1px solid var(--border-color);
                border-radius: 14px; box-shadow: var(--shadow-pop); padding: 14px;
                width: 250px; z-index: 30;
            }
            .date-filter-dropdown.open .date-filter-panel { display: block; }
            .date-filter-presets { display: flex; flex-direction: column; gap: 6px; margin-bottom: 12px; }
            .date-filter-btn {
                text-align: right; background: var(--nested-bg); border: 1px solid transparent;
                border-radius: 10px; padding: 9px 12px; font-family: inherit; font-size: 13px;
                font-weight: 600; color: var(--text-dark); cursor: pointer; width: 100%;
                transition: all 0.12s ease;
            }
            .date-filter-btn:hover { background: var(--nested-deep); }
            .date-filter-btn.active { background: var(--text-dark); color: #fff; }
            .date-filter-custom-block { border-top: 1px dashed var(--border-color); padding-top: 12px; }
            .date-filter-custom-block > span { font-size: 11.5px; font-weight: 700; color: var(--text-muted); display: block; margin-bottom: 8px; }
            .date-filter-custom-inputs { display: flex; align-items: center; gap: 6px; margin-bottom: 10px; }
            .date-filter-custom-inputs input[type="date"] {
                flex: 1; min-width: 0; border: 1px solid var(--border-color); border-radius: 8px;
                padding: 7px 6px; font-family: inherit; font-size: 11.5px; background: var(--nested-bg);
            }
            .date-filter-custom-inputs span { font-size: 11px; color: var(--text-muted); }
            .date-filter-custom-block .btn-apply-range { width: 100%; }

            .level-tabs {
                display: flex;
                gap: 6px;
                padding: 16px 22px 0 22px;
                border-bottom: 1px solid var(--border-color);
                overflow-x: auto;
            }
            .level-tab {
                padding: 10px 6px 14px 6px;
                font-weight: 700;
                font-size: 14px;
                color: var(--text-muted);
                cursor: pointer;
                border-bottom: 3px solid transparent;
                margin-bottom: -1px;
                white-space: nowrap;
                transition: color 0.15s ease;
            }
            .level-tab .tab-count {
                display: inline-flex; align-items:center; justify-content:center;
                min-width: 18px; height: 18px; padding: 0 5px; margin-right: 6px;
                border-radius: 999px; background: var(--nested-deep); color: var(--text-muted);
                font-size: 10.5px; font-weight: 800;
            }
            .level-tab.active { color: var(--text-dark); border-bottom-color: var(--accent-orange); }
            .level-tab.active .tab-count { background: var(--accent-orange-light); color: var(--accent-orange); }
            .level-tab:hover { color: var(--text-dark); }

            .filter-chips { display: flex; gap: 8px; padding: 14px 22px 0 22px; flex-wrap: wrap; }
            .filter-chip {
                display: inline-flex; align-items: center; gap: 8px;
                background: #eef4ff; color: var(--accent-blue);
                padding: 6px 8px 6px 14px; border-radius: 999px;
                font-size: 12px; font-weight: 700;
            }
            .filter-chip button {
                background: rgba(47,111,237,0.14); border: none; color: var(--accent-blue);
                width: 18px; height: 18px; border-radius: 50%; cursor: pointer;
                display: flex; align-items: center; justify-content: center; font-size: 12px; line-height: 1;
                font-family: inherit;
            }

            .table-toolbar {
                display: flex; align-items: center; gap: 10px;
                padding: 14px 22px; flex-wrap: wrap;
            }
            .search-input {
                flex: 1; min-width: 220px;
                border: 1px solid var(--border-color); border-radius: 10px;
                padding: 10px 14px; font-family: inherit; font-size: 13px;
                background: var(--nested-bg); color: var(--text-dark);
            }
            .search-input:focus { outline: none; border-color: var(--accent-orange); background: #fff; }
            .toolbar-btn {
                background: var(--card-bg); border: 1px solid var(--border-color); border-radius: 10px;
                padding: 9px 14px; font-size: 12.5px; font-weight: 700; color: var(--text-muted);
                cursor: pointer; display: flex; gap: 6px; align-items: center; font-family: inherit;
                white-space: nowrap;
            }
            .toolbar-btn:hover { border-color: var(--accent-orange); color: var(--accent-orange); }
            .toolbar-btn.primary { background: var(--text-dark); color: #fff; border-color: var(--text-dark); }
            .toolbar-btn.primary:hover { opacity: 0.9; color: #fff; }
            .toolbar-btn svg { width: 14px; height: 14px; }
            .table-scroll { border-radius: 0 0 var(--radius-lg) var(--radius-lg); overflow-x: auto; overflow-y: hidden; }
            table { width: 100%; border-collapse: collapse; text-align: right; font-size: 13.5px; min-width: 900px; }
            th, td { padding: 14px 18px; border-bottom: 1px solid var(--border-color); white-space: nowrap; }
            th {
                color: var(--text-muted); font-weight: 700; background-color: var(--nested-deep); font-size: 12px;
                position: sticky; top: 0; cursor: pointer; user-select: none;
            }
            th.no-sort { cursor: default; }
            th .sort-ico { font-size: 10px; margin-right: 4px; opacity: 0.45; }
            th.sorted .sort-ico { opacity: 1; color: var(--accent-orange); }
            tbody tr:last-child td { border-bottom: none; }
            tbody tr.data-row { transition: background-color 0.1s ease; }
            tbody tr.data-row.clickable { cursor: pointer; }
            tbody tr.data-row.clickable:hover { background-color: var(--nested-bg); }

            .name-cell { display: flex; align-items: center; gap: 8px; font-weight: 700; color: var(--text-dark); max-width: 280px; overflow: hidden; text-overflow: ellipsis; }
            .name-icon { flex-shrink: 0; }
            .drill-arrow { margin-right: auto; color: var(--text-faint); font-size: 12px; }

            .toggle-switch { position: relative; display: inline-block; width: 36px; height: 20px; direction: ltr; }
            .toggle-switch input { opacity: 0; width: 0; height: 0; position: absolute; }
            .toggle-slider {
                position: absolute; inset: 0; cursor: not-allowed;
                background-color: #d7dee8; transition: 0.2s; border-radius: 999px;
            }
            .toggle-slider:before {
                content: ""; position: absolute; height: 14px; width: 14px; left: 3px; top: 3px;
                background-color: #fff; transition: 0.2s; border-radius: 50%; box-shadow: 0 1px 2px rgba(0,0,0,0.25);
            }
            .toggle-switch input:checked + .toggle-slider { background-color: var(--accent-green); }
            .toggle-switch input:checked + .toggle-slider:before { transform: translateX(16px); }

            .status-pill { display: inline-flex; align-items: center; gap: 6px; font-size: 12.5px; font-weight: 700; }
            .status-dot { width: 7px; height: 7px; border-radius: 50%; }
            .status-active .status-dot { background-color: var(--accent-green); }
            .status-active { color: var(--text-dark); }
            .status-paused .status-dot { background-color: var(--text-faint); }
            .status-paused { color: var(--text-muted); }

            .metric-main { font-weight: 800; color: var(--text-dark); }
            .metric-sub { font-size: 11px; color: var(--text-muted); font-weight: 500; margin-top: 2px; white-space: normal; max-width: 160px; }

            .badge { padding: 3px 10px; border-radius: 999px; font-size: 10.5px; font-weight: 800; display: inline-block; }
            .badge-good { background-color: var(--stage-green-bg); border: 1px solid var(--stage-green-border); color: var(--stage-green-text); }
            .badge-medium { background-color: var(--stage-blue-bg); border: 1px solid var(--stage-blue-border); color: var(--stage-blue-text); }
            .badge-bad { background-color: var(--stage-red-bg); border: 1px solid var(--stage-red-border); color: var(--stage-red-text); }

            .empty-state { padding: 50px 20px; text-align: center; color: var(--text-muted); font-weight: 600; font-size: 14px; }

            /* Toast */
            #toast {
                position: fixed;
                bottom: 28px;
                left: 50%;
                transform: translateX(-50%) translateY(20px);
                background-color: var(--text-dark);
                color: #fff;
                padding: 14px 26px;
                border-radius: 999px;
                font-weight: 700;
                font-size: 14px;
                box-shadow: var(--shadow-pop);
                opacity: 0;
                pointer-events: none;
                transition: opacity 0.25s ease, transform 0.25s ease;
                z-index: 999;
                display: flex;
                align-items: center;
                gap: 8px;
                white-space: nowrap;
            }
            #toast.show { opacity: 1; transform: translateX(-50%) translateY(0); }

            ::-webkit-scrollbar { height: 8px; width: 8px; }
            ::-webkit-scrollbar-thumb { background-color: #dbe1ea; border-radius: 10px; }
            ::-webkit-scrollbar-track { background: transparent; }

            @media (max-width: 1100px) {
                .cards-container { grid-template-columns: repeat(2, 1fr); }
                .grid-2 { grid-template-columns: 1fr; }
            }
        </style>
    </head>
    <body>
        <div class="sidebar">
            <div class="sidebar-logo">eleven<span>z</span></div>
            <ul class="sidebar-menu">
                <li class="active"><a href="#">📊 الإعلانات والتحليلات</a></li>
                <li><a href="#">🎯 الحملات النشطة</a></li>
                <li><a href="#">📈 تقارير الأداء الأسبوعية</a></li>
            </ul>
            <div class="user-profile">
                <div class="user-avatar">A</div>
                <div style="flex:1;">
                    <div style="font-weight: 800; font-size: 14px;">أبو بكر</div>
                    <div style="font-size: 11px; color: #93a4bb; font-weight: 600;">مدير الحملات</div>
                </div>
                <a href="/logout" title="تسجيل الخروج" style="color:#93a4bb; text-decoration:none; font-size:16px; padding:4px;">⏻</a>
            </div>
        </div>

        <div class="main-content">
            <div class="top-bar">
                <div class="header-title">
                    <h1>منصة التقرير الإعلاني - elevenz</h1>
                    <p id="update-time">تتبع مباشر للأداء والمؤشرات | آخر تحديث: --</p>
                </div>
                <div class="top-actions">
                    <button class="btn btn-copy" onclick="copyReport()">
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="12" height="12" rx="3"></rect><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path></svg>
                        نسخ التقرير
                    </button>
                    <button class="btn btn-refresh" onclick="refreshData()">
                        <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M23 4v6h-6"></path><path d="M1 20v-6h6"></path><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"></path></svg>
                        تحديث البيانات
                    </button>
                </div>
            </div>

            <div class="time-selector">
                <button class="time-btn" data-preset="today" onclick="applyDatePreset('today')">اليوم</button>
                <button class="time-btn active" data-preset="yesterday" onclick="applyDatePreset('yesterday')">أمس</button>
                <button class="time-btn" data-preset="last7" onclick="applyDatePreset('last7')">آخر 7 أيام</button>
                <button class="time-btn" data-preset="last14" onclick="applyDatePreset('last14')">آخر 14 يوماً</button>
                <button class="time-btn" data-preset="thismonth" onclick="applyDatePreset('thismonth')">هذا الشهر</button>
                <button class="time-btn" data-preset="lastmonth" onclick="applyDatePreset('lastmonth')">الشهر الماضي</button>
                <button class="time-btn" id="btn-custom-toggle" onclick="toggleCustomRange(this)">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="18" rx="3"></rect><path d="M16 2v4M8 2v4M3 10h18"></path></svg>
                    نطاق مخصص
                </button>
                <div class="custom-range-box" id="custom-range-box">
                    <span>من</span>
                    <input type="date" id="date-from-input">
                    <span>إلى</span>
                    <input type="date" id="date-to-input">
                    <button class="btn-apply-range" onclick="applyCustomRange()">تطبيق</button>
                </div>
            </div>
            <div class="range-caption" id="range-caption">الفترة المعروضة: <b>أمس</b></div>

            <div class="cards-container">
                <div class="card">
                    <div class="card-top">
                        <div class="card-title">إجمالي الإنفاق (الكلي)</div>
                        <div class="card-icon icon-wallet" style="background:var(--accent-orange-light);">
                            <svg viewBox="0 0 24 24" fill="none" stroke="#f05a28" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12V7H5a2 2 0 0 1 0-4h14v4"></path><path d="M3 5v14a2 2 0 0 0 2 2h16v-5"></path><path d="M18 12a2 2 0 0 0 0 4h4v-4Z"></path></svg>
                        </div>
                    </div>
                    <div class="card-value" id="total-spend">0.00 ر.س</div>
                    <div class="card-sub" style="color:var(--text-muted);">جميع حسابات الربط</div>
                    <div class="card-meta">
                        <span>CPC: <b id="total-cpc">0.00</b></span>
                        <span>CTR: <b id="total-ctr">0.0%</b></span>
                    </div>
                </div>
                <div class="card">
                    <div class="card-top">
                        <div class="card-title">Google Ads</div>
                        <div class="card-icon" style="background:#f8f9fa;">
                            <svg viewBox="0 0 48 48"><path fill="#FFC107" d="M43.611,20.083H42V20H24v8h11.303c-1.649,4.657-6.08,8-11.303,8c-6.627,0-12-5.373-12-12c0-6.627,5.373-12,12-12c3.059,0,5.842,1.154,7.961,3.039l5.657-5.657C34.046,6.053,29.268,4,24,4C12.955,4,4,12.955,4,24c0,11.045,8.955,20,20,20c11.045,0,20-8.955,20-20C44,22.659,43.862,21.35,43.611,20.083z"></path><path fill="#FF3D00" d="M6.306,14.691l6.571,4.819C14.655,15.108,18.961,12,24,12c3.059,0,5.842,1.154,7.961,3.039l5.657-5.657C34.046,6.053,29.268,4,24,4C16.318,4,9.656,8.337,6.306,14.691z"></path><path fill="#4CAF50" d="M24,44c5.166,0,9.86-1.977,13.409-5.192l-6.19-5.238C29.211,35.091,26.715,36,24,36c-5.202,0-9.619-3.317-11.283-7.946l-6.522,5.025C9.505,39.556,16.227,44,24,44z"></path><path fill="#1976D2" d="M43.611,20.083H42V20H24v8h11.303c-0.792,2.237-2.231,4.166-4.087,5.571c0.001-0.001,0.002-0.001,0.003-0.002l6.19,5.238C36.971,39.205,44,34,44,24C44,22.659,43.862,21.35,43.611,20.083z"></path></svg>
                        </div>
                    </div>
                    <div class="card-value" id="google-spend">0.00 ر.س</div>
                    <div class="card-sub" id="google-sub" style="color:var(--accent-blue);">0 إحالات</div>
                    <div class="card-meta">
                        <span>CPA: <b id="google-cpa">--</b></span>
                        <span>CTR: <b id="google-ctr">0.0%</b></span>
                    </div>
                </div>
                <div class="card">
                    <div class="card-top">
                        <div class="card-title">TikTok Ads</div>
                        <div class="card-icon" style="background:#0f0f0f;">
                            <svg viewBox="0 0 48 48"><path fill="#25F4EE" d="M33.6,15.4c-2.1-1.4-3.6-3.6-4-6.2c-0.1-0.5-0.1-1-0.1-1.5h-6v24.6c0,2.9-2.4,5.3-5.3,5.3 c-0.9,0-1.8-0.2-2.5-0.7c-1.7-0.9-2.8-2.7-2.8-4.7c0-2.9,2.4-5.3,5.3-5.3c0.5,0,1.1,0.1,1.6,0.3v-6.1c-0.5-0.1-1-0.1-1.6-0.1 c-6.3,0-11.4,5.1-11.4,11.4c0,3.9,1.9,7.3,4.9,9.4c1.9,1.3,4.2,2.1,6.7,2.1c6.3,0,11.4-5.1,11.4-11.4V19.1 c2.4,1.8,5.4,2.8,8.6,2.8v-6C36.9,15.9,35.1,15.8,33.6,15.4z"></path><path fill="#FE2C55" d="M31.6,13.4c-2.1-1.4-3.6-3.6-4-6.2c-0.1-0.5-0.1-1-0.1-1.5h-6v24.6c0,2.9-2.4,5.3-5.3,5.3 c-0.9,0-1.8-0.2-2.5-0.7c-1.5-0.8-2.6-2.4-2.8-4.2c-0.4-2.9,1.7-5.6,4.5-6c0.5-0.1,1.1-0.1,1.6,0v-6.1c-6.2-0.1-11.3,4.9-11.4,11.1 c0,3.9,1.9,7.5,4.9,9.6c1.9,1.3,4.2,2.1,6.7,2.1c6.3,0,11.4-5.1,11.4-11.4V17.1c2.4,1.8,5.4,2.8,8.6,2.8v-6 C34.9,13.9,33.1,13.8,31.6,13.4z"></path><path fill="#ffffff" d="M35,17.9c-3.2,0-6.2-1-8.6-2.8v14.7c0,6.3-5.1,11.4-11.4,11.4c-2.5,0-4.8-0.8-6.7-2.1 c2.1,2.3,5.1,3.7,8.4,3.7c6.3,0,11.4-5.1,11.4-11.4V16.7c2.4,1.8,5.4,2.8,8.6,2.8v-6C36.4,13.5,36.4,13.5,35,17.9z"></path></svg>
                        </div>
                    </div>
                    <div class="card-value" id="tiktok-spend">0.00 ر.س</div>
                    <div class="card-sub" id="tiktok-sub" style="color:var(--accent-orange);">0 تحويل/نقرة</div>
                    <div class="card-meta">
                        <span>CPA: <b id="tiktok-cpa">--</b></span>
                        <span>CTR: <b id="tiktok-ctr">0.0%</b></span>
                    </div>
                </div>
                <div class="card">
                    <div class="card-top">
                        <div class="card-title">Meta Ads</div>
                        <div class="card-icon" style="background:#e7f0ff;">
                            <svg viewBox="0 0 36 36"><path fill="#1877F2" d="M36,18c0-9.94-8.06-18-18-18S0,8.06,0,18c0,8.98,6.58,16.41,15.19,17.76V23.13h-4.57V18h4.57v-3.91 c0-4.51,2.69-7.01,6.8-7.01c1.97,0,4.03,0.35,4.03,0.35v4.43h-2.27c-2.24,0-2.94,1.39-2.94,2.81V18h5.01l-0.8,5.13h-4.21v12.63 C29.42,34.41,36,26.98,36,18z"></path></svg>
                        </div>
                    </div>
                    <div class="card-value" id="meta-spend">0.00 ر.س</div>
                    <div class="card-sub" id="meta-sub" style="color:#0284c7;">0 محادثة/نتيجة</div>
                    <div class="card-meta">
                        <span>CPA: <b id="meta-cpa">--</b></span>
                        <span>CTR: <b id="meta-ctr">0.0%</b></span>
                    </div>
                </div>
            </div>

            <div class="grid-2">
                <div class="chart-section">
                    <div class="chart-section-title" id="chart-period-title">تحليل الإنفاق حسب المنصة</div>
                    <div class="chart-section-sub">مقارنة الإنفاق الإعلاني عبر جميع المنصات المرتبطة</div>
                    <div class="chart-wrapper">
                        <canvas id="spendChart"></canvas>
                    </div>
                </div>
                <div class="chart-section">
                    <div class="chart-section-title">توزيع النتائج</div>
                    <div class="chart-section-sub">حصة كل منصة من إجمالي النتائج/التحويلات</div>
                    <div class="donut-wrapper">
                        <canvas id="donutChart"></canvas>
                    </div>
                    <div class="legend-list" id="donut-legend"></div>
                </div>
            </div>

            <div class="explorer-card">
                <div class="explorer-sticky-zone">
                    <div class="explorer-top">
                        <div class="platform-tabs" id="platform-tabs"></div>
                        <div class="date-filter-dropdown" id="date-filter-dropdown">
                            <button class="date-filter-trigger" onclick="toggleDateDropdown(event)">
                                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="18" rx="3"></rect><path d="M16 2v4M8 2v4M3 10h18"></path></svg>
                                <span id="date-filter-trigger-label">أمس</span>
                                <svg class="chevron-down" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"></polyline></svg>
                            </button>
                            <div class="date-filter-panel">
                                <div class="date-filter-presets">
                                    <button class="date-filter-btn" data-preset="today" onclick="applyDatePreset('today')">اليوم</button>
                                    <button class="date-filter-btn" data-preset="yesterday" onclick="applyDatePreset('yesterday')">أمس</button>
                                    <button class="date-filter-btn" data-preset="last7" onclick="applyDatePreset('last7')">آخر 7 أيام</button>
                                    <button class="date-filter-btn" data-preset="last14" onclick="applyDatePreset('last14')">آخر 14 يوماً</button>
                                    <button class="date-filter-btn" data-preset="thismonth" onclick="applyDatePreset('thismonth')">هذا الشهر</button>
                                    <button class="date-filter-btn" data-preset="lastmonth" onclick="applyDatePreset('lastmonth')">الشهر الماضي</button>
                                </div>
                                <div class="date-filter-custom-block">
                                    <span>نطاق مخصص</span>
                                    <div class="date-filter-custom-inputs">
                                        <input type="date" id="date-filter-panel-from">
                                        <span>إلى</span>
                                        <input type="date" id="date-filter-panel-to">
                                    </div>
                                    <button class="btn-apply-range" onclick="applyCustomRangeFromPanel()">تطبيق</button>
                                </div>
                            </div>
                        </div>
                    </div>
                    <div class="level-tabs" id="level-tabs"></div>
                    <div class="filter-chips" id="filter-chips"></div>
                    <div class="table-toolbar">
                        <input type="text" class="search-input" id="search-input" placeholder="ابحث عن حملة أو مجموعة أو إعلان" oninput="onSearch(this.value)">
                        <button class="toolbar-btn" onclick="notImplementedYet()">
                            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7" height="18"></rect><rect x="14" y="3" width="7" height="10"></rect></svg>
                            الأعمدة
                        </button>
                        <button class="toolbar-btn" onclick="notImplementedYet()">
                            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15V6M18.5 18a2.5 2.5 0 1 0 0-5 2.5 2.5 0 0 0 0 5ZM12 12H3M16 6H3M12 18H3"></path></svg>
                            التجميع
                        </button>
                        <button class="toolbar-btn" id="copy-table-report-btn" onclick="copyTableReport()">
                            📋 نسخ التقرير
                        </button>
                        <button class="toolbar-btn" id="export-btn" onclick="exportCsv()">
                            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path><polyline points="7 10 12 15 17 10"></polyline><line x1="12" y1="15" x2="12" y2="3"></line></svg>
                            تصدير CSV
                        </button>
                    </div>
                </div>
                <div class="table-scroll">
                    <table>
                        <thead>
                            <tr id="table-head"></tr>
                        </thead>
                        <tbody id="table-body"></tbody>
                    </table>
                </div>
            </div>
        </div>

        <div id="toast"></div>

        <script>
            Chart.register(ChartDataLabels);
            Chart.defaults.locale = 'en-US';
            Chart.defaults.font.family = 'Cairo';

            let chartInstance = null;
            let donutInstance = null;
            let globalData = {};
            let lastMetrics = null;
            let isLoadingData = false;

            // نطاق التاريخ الحالي المطبَّق فعلياً على الطلب المرسل لـ Windsor.ai
            let dateRangeState = { preset: 'yesterday', date_from: null, date_to: null };

            // ===== Explorer state =====
            const PLATFORMS = [
                { key: 'meta', label: 'Meta Ads', dataKey: 'meta_ads', dot: '#0284c7', isMeta: true, isGoogle: false, resultLabel: 'النتائج', resultSub: 'محادثات تم بدؤها عبر الرسائل' },
                { key: 'tiktok', label: 'TikTok Ads', dataKey: 'tiktok_ads', dot: '#f05a28', isMeta: false, isGoogle: false, resultLabel: 'النتائج', resultSub: 'تحويلات/نقرات إعلانية' },
                { key: 'google', label: 'Google Ads', dataKey: 'google_ads', dot: '#0f2540', isMeta: false, isGoogle: true, resultLabel: 'النتائج', resultSub: 'إحالات' }
            ];
            const LEVELS = [
                { key: 'campaigns', label: 'الحملات الإعلانية' },
                { key: 'adsets', label: 'المجموعات الإعلانية' },
                { key: 'ads', label: 'الإعلانات' }
            ];

            // خريطة أسماء حقول "الحالة الفعلية" المحتملة لكل منصة ومستوى، بترتيب
            // الأولوية. كل الأسماء أدناه مؤكدة (تم التحقق المباشر من حساب Windsor.ai
            // الفعلي عبر get_fields وليست تخميناً)، وتُفحص بالترتيب حتى يُعثر على أول
            // حقل يحمل قيمة فعلية في الصف - هذا يجعل الواجهة تعمل بشكل صحيح بغض النظر
            // عن أي مستوى (Tier) من حقول الحالة استخدمه الباك إند فعلياً في هذا الطلب.
            const STATUS_FIELD_MAP = {
                meta: {
                    campaigns: ['campaign_status'],
                    adsets: ['adset_status'],
                    ads: ['effective_status']
                },
                tiktok: {
                    campaigns: ['campaign_operation_status', 'campaign_status'],
                    adsets: ['ad_group_operation_status', 'adgroup_status'],
                    ads: ['ad_operation_status', 'ad_status']
                },
                google: {
                    campaigns: ['campaign_status'],
                    adsets: ['ad_group_status'],
                    ads: ['ad_group_ad_status']
                }
            };

            // يحوّل قيمة حالة خام (من أي من المنصات الثلاث، بأي تهجئة شائعة) إلى
            // true (نشطة) / false (متوقفة) / null (قيمة غير معروفة - تجاهلها).
            function normalizeStatusValue(raw) {
                if (raw === null || raw === undefined || raw === '') return null;
                const s = String(raw).trim().toUpperCase();
                if (!s) return null;
                // علامات الإيقاف تُفحص أولاً (بالاحتواء) لأنها أكثر تحديداً، ولتفادي
                // تطابق زائف مثل "NOT_ELIGIBLE" مع كلمة "ELIGIBLE" التي تعني نشطة
                const PAUSED_MARKERS = ['NOT_ELIGIBLE', 'PAUSE', 'DISABLE', 'FROZEN', 'ARCHIVED', 'DELETED', 'REMOVED', 'ENDED', 'REJECTED', 'INACTIVE', 'LIMITED'];
                if (PAUSED_MARKERS.some(marker => s.includes(marker))) return false;
                const ACTIVE_MARKERS = ['ACTIVE', 'ENABLE', 'RUNNING', 'ELIGIBLE'];
                if (ACTIVE_MARKERS.some(marker => s.includes(marker))) return true;
                return null;
            }

            let explorerState = {
                platform: 'meta',
                level: 'campaigns',
                selectedCampaign: null,
                selectedGroup: null,
                search: '',
                sortKey: 'spend',
                sortDir: -1
            };

            // أسماء الحملات/المجموعات/الإعلانات تأتي من منصات خارجية (Meta/TikTok/Google
            // عبر Windsor.ai) ولا يمكن الوثوق بمحتواها. يجب تشفيرها دائماً قبل إدراجها
            // ضمن innerHTML لمنع ثغرات XSS المخزّنة (مثال: اسم حملة يحتوي وسم <script>).
            function escapeHtml(str) {
                if (str === null || str === undefined) return '';
                return String(str)
                    .replace(/&/g, '&amp;')
                    .replace(/</g, '&lt;')
                    .replace(/>/g, '&gt;')
                    .replace(/"/g, '&quot;')
                    .replace(/'/g, '&#39;');
            }

            function safeNum(val) {
                if (!val) return 0;
                let n = parseFloat(val);
                return isNaN(n) ? 0 : n;
            }

            // أنواع الأحداث الخاصة بمحادثات الرسائل على ميتا مرتبة بحسب الأولوية.
            // هذه المراحل الثلاثة تُمثّل نفس قمع المحادثة (بدء → رد أول → اتصال مكتمل)
            // وليست نتائج منفصلة - لذلك يجب أخذ واحدة منها فقط وليس جمعها معاً.
            const META_MESSAGING_ACTION_PRIORITY = [
                'onsite_conversion.messaging_conversation_started_7d',
                'onsite_conversion.total_messaging_connection',
                'onsite_conversion.messaging_first_reply'
            ];

            function parseMetaConversions(item) {
                if (!item) return 0;

                // 1) إن كان حقل conversions القياسي أكبر من صفر، اعتمده مباشرة (هذا يغطي
                //    حملات أهداف الشراء/التحويل القياسية التي تملأ ميتا هذا الحقل لها فعلياً)
                const standardConv = safeNum(item.conversions || item.results);
                if (standardConv > 0) return standardConv;

                // 2) حملات المراسلة/الرسائل عادة لا تملأ حقل conversions، فنستخرج النتيجة
                //    من actions بدلاً من ذلك
                if (item.actions && Array.isArray(item.actions)) {
                    // طابق أحد الأنواع القياسية المعروفة بالضبط (وليس بالاحتواء الجزئي)
                    // حتى لا نخلط بين مراحل قمع المحادثة نفسها أو مع أحداث لا علاقة لها
                    // (مثل "lead" من هدف إعلاني مختلف) ونجمعها خطأً كنتائج مضاعفة.
                    for (const actionType of META_MESSAGING_ACTION_PRIORITY) {
                        const match = item.actions.find(act => act.action_type === actionType);
                        if (match) return safeNum(match.value);
                    }
                    // لم يُعثر على أي من الأنواع القياسية أعلاه: خذ أعلى قيمة مفردة من
                    // الأحداث المتعلقة بالمراسلة/المحادثة دون جمعها (Math.max وليس +=)
                    // لتفادي مضاعفة الرقم في حال وجود أكثر من حدث متشابه بنفس الصف.
                    const relevant = item.actions.filter(act =>
                        act.action_type && (act.action_type.includes('messaging') || act.action_type.includes('conversation'))
                    );
                    if (relevant.length > 0) {
                        return Math.max(...relevant.map(act => safeNum(act.value)));
                    }
                }

                return 0;
            }

            function parseGoogleConversions(item) {
                if (!item) return 0;
                return safeNum(item.conversions || item.all_conversions || item.results);
            }

            // ملاحظة: الباك إند الآن يُرجع بيانات مُصفّاة مسبقاً بحسب نطاق التاريخ
            // المطلوب فعلياً (date_from/date_to تُرسَل مباشرة إلى Windsor.ai)، لذلك لم
            // تعد هناك حاجة لتخمين النطاق على العميل. هذه دالة أمان فقط تضمن مصفوفة صالحة.
            function scopedList(list) {
                return Array.isArray(list) ? list : [];
            }

            function pad2(n) { return n < 10 ? '0' + n : '' + n; }
            function toIsoDate(d) { return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`; }

            // يحوّل تاريخاً بصيغة ISO (YYYY-MM-DD، مستخدمة داخلياً للـ API وحقول
            // الإدخال) إلى صيغة عرض DD-MM-YYYY للمستخدم. تنسيق صريح بدل الاعتماد
            // على إعادة ترتيب المتصفح البصري تلقائياً (bidi) لنص RTL/LTR مختلط،
            // لضمان ظهور التاريخ بنفس الترتيب الصحيح دوماً - على الشاشة وفي أي
            // تقرير مُلصَق لاحقاً في واتساب أو الإيميل.
            function formatDisplayDate(iso) {
                if (!iso || iso.indexOf('-') === -1) return iso;
                const parts = iso.split('-');
                if (parts.length !== 3) return iso;
                const [y, m, d] = parts;
                return `${d}-${m}-${y}`;
            }

            function computePresetRange(preset) {
                const today = new Date();
                if (preset === 'today') {
                    return { from: toIsoDate(today), to: toIsoDate(today) };
                }
                if (preset === 'yesterday') {
                    const y = new Date(today);
                    y.setDate(y.getDate() - 1);
                    return { from: toIsoDate(y), to: toIsoDate(y) };
                }
                if (preset === 'last7') {
                    const f = new Date(today);
                    f.setDate(f.getDate() - 6);
                    return { from: toIsoDate(f), to: toIsoDate(today) };
                }
                if (preset === 'last14') {
                    const f = new Date(today);
                    f.setDate(f.getDate() - 13);
                    return { from: toIsoDate(f), to: toIsoDate(today) };
                }
                if (preset === 'thismonth') {
                    const f = new Date(today.getFullYear(), today.getMonth(), 1);
                    return { from: toIsoDate(f), to: toIsoDate(today) };
                }
                if (preset === 'lastmonth') {
                    const f = new Date(today.getFullYear(), today.getMonth() - 1, 1);
                    const t = new Date(today.getFullYear(), today.getMonth(), 0); // اليوم الأخير من الشهر الماضي
                    return { from: toIsoDate(f), to: toIsoDate(t) };
                }
                return null;
            }

            function buildRangeLabel(preset, dateFrom, dateTo) {
                const presetNames = {
                    today: 'اليوم', yesterday: 'أمس', last7: 'آخر 7 أيام',
                    last14: 'آخر 14 يوماً', thismonth: 'هذا الشهر', lastmonth: 'الشهر الماضي'
                };
                const fFrom = formatDisplayDate(dateFrom);
                const fTo = formatDisplayDate(dateTo);
                if (preset && presetNames[preset]) {
                    if (dateFrom === dateTo) return `${presetNames[preset]} (${fFrom})`;
                    return `${presetNames[preset]} (${fFrom} ← ${fTo})`;
                }
                if (dateFrom === dateTo) return fFrom;
                return `من ${fFrom} إلى ${fTo}`;
            }

            function setLoadingState(loading) {
                isLoadingData = loading;
                document.querySelectorAll('.time-btn, .btn-refresh, .btn-apply-range, .date-filter-btn, .date-filter-trigger').forEach(b => { b.disabled = loading; });
                if (loading) {
                    document.getElementById('update-time').innerText = 'جاري تحديث البيانات من Windsor.ai...';
                }
            }

            const PRESET_LABELS = {
                today: 'اليوم', yesterday: 'أمس', last7: 'آخر 7 أيام',
                last14: 'آخر 14 يوماً', thismonth: 'هذا الشهر', lastmonth: 'الشهر الماضي', custom: 'نطاق مخصص'
            };

            // يزامن كل عناصر واجهة فلتر التاريخ (الشريط العلوي + القائمة المنسدلة
            // المصغّرة فوق الجدول) مع dateRangeState الحالي، أياً كان مصدر التغيير.
            function refreshDateFilterUI() {
                const label = PRESET_LABELS[dateRangeState.preset] || 'نطاق مخصص';
                const labelEl = document.getElementById('date-filter-trigger-label');
                if (labelEl) labelEl.innerText = label;

                document.querySelectorAll('[data-preset]').forEach(btn => {
                    btn.classList.toggle('active', btn.dataset.preset === dateRangeState.preset);
                });

                ['date-from-input', 'date-filter-panel-from'].forEach(id => {
                    const el = document.getElementById(id);
                    if (el && dateRangeState.date_from) el.value = dateRangeState.date_from;
                });
                ['date-to-input', 'date-filter-panel-to'].forEach(id => {
                    const el = document.getElementById(id);
                    if (el && dateRangeState.date_to) el.value = dateRangeState.date_to;
                });
            }

            async function applyDatePreset(preset) {
                if (isLoadingData) return;
                document.getElementById('custom-range-box').style.display = 'none';
                closeDateDropdown();

                const range = computePresetRange(preset);
                dateRangeState = { preset, date_from: range.from, date_to: range.to };
                refreshDateFilterUI();
                await fetchDataForRange(range.from, range.to);
            }

            function toggleCustomRange(btn) {
                closeDateDropdown();
                const box = document.getElementById('custom-range-box');
                const isHidden = box.style.display === 'none' || !box.style.display;
                box.style.display = isHidden ? 'flex' : 'none';
                if (isHidden) {
                    if (!document.getElementById('date-from-input').value) {
                        document.getElementById('date-from-input').value = dateRangeState.date_from || toIsoDate(new Date());
                        document.getElementById('date-to-input').value = dateRangeState.date_to || toIsoDate(new Date());
                    }
                }
            }

            function toggleDateDropdown(evt) {
                if (evt) evt.stopPropagation();
                document.getElementById('custom-range-box').style.display = 'none';
                const dd = document.getElementById('date-filter-dropdown');
                const opening = !dd.classList.contains('open');
                dd.classList.toggle('open', opening);
                if (opening) {
                    const fromEl = document.getElementById('date-filter-panel-from');
                    const toEl = document.getElementById('date-filter-panel-to');
                    if (fromEl && !fromEl.value) fromEl.value = dateRangeState.date_from || toIsoDate(new Date());
                    if (toEl && !toEl.value) toEl.value = dateRangeState.date_to || toIsoDate(new Date());
                }
            }

            function closeDateDropdown() {
                const dd = document.getElementById('date-filter-dropdown');
                if (dd) dd.classList.remove('open');
            }

            document.addEventListener('click', (e) => {
                const dd = document.getElementById('date-filter-dropdown');
                if (dd && dd.classList.contains('open') && !dd.contains(e.target)) {
                    dd.classList.remove('open');
                }
            });

            async function applyCustomRangeGeneric(fromId, toId) {
                if (isLoadingData) return;
                const from = document.getElementById(fromId).value;
                const to = document.getElementById(toId).value;
                if (!from || !to) { showToast('يرجى تحديد تاريخي البداية والنهاية'); return; }
                if (from > to) { showToast('تاريخ البداية يجب أن يسبق تاريخ النهاية أو يساويه'); return; }

                dateRangeState = { preset: 'custom', date_from: from, date_to: to };
                refreshDateFilterUI();
                closeDateDropdown();
                document.getElementById('custom-range-box').style.display = 'none';
                await fetchDataForRange(from, to);
            }

            async function applyCustomRange() {
                await applyCustomRangeGeneric('date-from-input', 'date-to-input');
            }

            async function applyCustomRangeFromPanel() {
                await applyCustomRangeGeneric('date-filter-panel-from', 'date-filter-panel-to');
            }

            async function fetchDataForRange(dateFrom, dateTo) {
                setLoadingState(true);
                try {
                    const res = await fetch(`/api/data?date_from=${dateFrom}&date_to=${dateTo}`);
                    const json = await res.json();
                    if (json.status !== 'success') {
                        showToast(json.message || 'تعذر تحديث البيانات لهذا النطاق');
                        return;
                    }
                    globalData = json.data || {};
                    updateDashboardUI();
                } catch (e) {
                    console.error('Error fetching range data:', e);
                    showToast('تعذر الاتصال بالخادم');
                } finally {
                    setLoadingState(false);
                }
            }

            async function refreshData() {
                if (isLoadingData) return;
                const { date_from, date_to } = dateRangeState;
                if (!date_from || !date_to) return;
                await fetchDataForRange(date_from, date_to);
                showToast('تم تحديث البيانات! 🔄');
            }

            function tierBadge(ctr) {
                if (ctr >= 1.5) return '<span class="badge badge-good">أداء ممتاز</span>';
                if (ctr >= 0.6) return '<span class="badge badge-medium">أداء متوسط</span>';
                return '<span class="badge badge-bad">يحتاج متابعة</span>';
            }

            function updateChart(mSpend, tSpend, gSpend) {
                const ctx = document.getElementById('spendChart').getContext('2d');
                if (chartInstance) chartInstance.destroy();

                chartInstance = new Chart(ctx, {
                    type: 'bar',
                    data: {
                        labels: ['Meta Ads', 'TikTok Ads', 'Google Ads'],
                        datasets: [{
                            data: [mSpend, tSpend, gSpend],
                            backgroundColor: ['#0284c7', '#f05a28', '#0f2540'],
                            borderRadius: 8,
                            barThickness: 46
                        }]
                    },
                    options: {
                        responsive: true,
                        maintainAspectRatio: false,
                        plugins: {
                            legend: { display: false },
                            tooltip: {
                                backgroundColor: '#0f2540',
                                titleFont: { family: 'Cairo', weight: '700' },
                                bodyFont: { family: 'Cairo' },
                                padding: 10,
                                cornerRadius: 8,
                                callbacks: {
                                    label: (c) => `الإنفاق: ${c.parsed.y.toFixed(2)} ر.س`
                                }
                            },
                            datalabels: {
                                anchor: 'end',
                                align: 'top',
                                color: '#0f2540',
                                font: { weight: 'bold', family: 'Cairo', size: 11 },
                                formatter: (val) => val > 0 ? val.toFixed(0) + ' ر.س' : ''
                            }
                        },
                        scales: {
                            y: { grid: { color: '#eef1f6' }, ticks: { font: { family: 'Cairo' } } },
                            x: { grid: { display: false }, ticks: { font: { family: 'Cairo', weight: '600' } } }
                        }
                    }
                });
            }

            function updateDonut(metaConv, tiktokConv, googleConv) {
                const ctx = document.getElementById('donutChart').getContext('2d');
                if (donutInstance) donutInstance.destroy();
                const total = metaConv + tiktokConv + googleConv;
                const legendEl = document.getElementById('donut-legend');

                if (total <= 0) {
                    legendEl.innerHTML = '<div style="text-align:center; color:var(--text-muted); font-size:12.5px; font-weight:600;">لا توجد نتائج بعد لهذه الفترة</div>';
                }

                donutInstance = new Chart(ctx, {
                    type: 'doughnut',
                    data: {
                        labels: ['Meta Ads', 'TikTok Ads', 'Google Ads'],
                        datasets: [{
                            data: [metaConv, tiktokConv, googleConv],
                            backgroundColor: ['#0284c7', '#f05a28', '#0f2540'],
                            borderWidth: 3,
                            borderColor: '#ffffff'
                        }]
                    },
                    options: {
                        responsive: true,
                        maintainAspectRatio: false,
                        cutout: '68%',
                        plugins: {
                            legend: { display: false },
                            datalabels: { display: false },
                            tooltip: {
                                backgroundColor: '#0f2540',
                                bodyFont: { family: 'Cairo' },
                                padding: 10,
                                cornerRadius: 8
                            }
                        }
                    }
                });

                if (total > 0) {
                    const parts = [
                        { label: 'Meta Ads', value: metaConv, color: '#0284c7' },
                        { label: 'TikTok Ads', value: tiktokConv, color: '#f05a28' },
                        { label: 'Google Ads', value: googleConv, color: '#0f2540' }
                    ];
                    legendEl.innerHTML = parts.map(p => `
                        <div class="legend-item">
                            <span class="legend-label"><span class="legend-dot" style="background:${p.color}"></span>${p.label}</span>
                            <span class="legend-value">${p.value.toLocaleString('en-US')} (${total > 0 ? ((p.value/total)*100).toFixed(0) : 0}%)</span>
                        </div>
                    `).join('');
                }
            }

            function updateDashboardUI() {
                let metaList = scopedList(globalData.meta_ads);
                let tiktokList = scopedList(globalData.tiktok_ads);
                let googleList = scopedList(globalData.google_ads);

                let metaSpend = metaList.reduce((s, i) => s + safeNum(i.spend || i.cost), 0);
                let tiktokSpend = tiktokList.reduce((s, i) => s + safeNum(i.spend || i.cost), 0);
                let googleSpend = googleList.reduce((s, i) => s + safeNum(i.spend || i.cost), 0);

                let metaConv = metaList.reduce((s, i) => s + parseMetaConversions(i), 0);
                let tiktokConv = tiktokList.reduce((s, i) => s + safeNum(i.conversions || i.conversion || i.results), 0);
                let googleConv = googleList.reduce((s, i) => s + parseGoogleConversions(i), 0);

                let metaClicks = metaList.reduce((s, i) => s + safeNum(i.clicks), 0);
                let tiktokClicks = tiktokList.reduce((s, i) => s + safeNum(i.clicks), 0);
                let googleClicks = googleList.reduce((s, i) => s + safeNum(i.clicks), 0);

                let metaImpr = metaList.reduce((s, i) => s + safeNum(i.impressions), 0);
                let tiktokImpr = tiktokList.reduce((s, i) => s + safeNum(i.impressions), 0);
                let googleImpr = googleList.reduce((s, i) => s + safeNum(i.impressions), 0);

                const ctrOf = (clicks, impr) => impr > 0 ? (clicks / impr) * 100 : 0;
                const cpaOf = (spend, conv) => conv > 0 ? spend / conv : null;
                const cpcOf = (spend, clicks) => clicks > 0 ? spend / clicks : 0;

                let metaCtr = ctrOf(metaClicks, metaImpr);
                let tiktokCtr = ctrOf(tiktokClicks, tiktokImpr);
                let googleCtr = ctrOf(googleClicks, googleImpr);

                let metaCpa = cpaOf(metaSpend, metaConv);
                let tiktokCpa = cpaOf(tiktokSpend, tiktokConv);
                let googleCpa = cpaOf(googleSpend, googleConv);

                let totalSpend = metaSpend + tiktokSpend + googleSpend;
                let totalClicks = metaClicks + tiktokClicks + googleClicks;
                let totalImpr = metaImpr + tiktokImpr + googleImpr;

                document.getElementById('meta-spend').innerText = metaSpend.toFixed(2) + ' ر.س';
                document.getElementById('tiktok-spend').innerText = tiktokSpend.toFixed(2) + ' ر.س';
                document.getElementById('google-spend').innerText = googleSpend.toFixed(2) + ' ر.س';
                document.getElementById('total-spend').innerText = totalSpend.toFixed(2) + ' ر.س';

                document.getElementById('meta-sub').innerText = `${metaConv.toLocaleString('en-US')} محادثة/نتيجة`;
                document.getElementById('tiktok-sub').innerText = `${tiktokConv.toLocaleString('en-US')} تحويل/نقرة`;
                document.getElementById('google-sub').innerText = `${googleConv.toLocaleString('en-US')} إحالات`;

                document.getElementById('total-cpc').innerText = cpcOf(totalSpend, totalClicks).toFixed(2);
                document.getElementById('total-ctr').innerText = ctrOf(totalClicks, totalImpr).toFixed(1) + '%';
                document.getElementById('meta-ctr').innerText = metaCtr.toFixed(1) + '%';
                document.getElementById('tiktok-ctr').innerText = tiktokCtr.toFixed(1) + '%';
                document.getElementById('google-ctr').innerText = googleCtr.toFixed(1) + '%';
                document.getElementById('meta-cpa').innerText = metaCpa !== null ? metaCpa.toFixed(2) : '--';
                document.getElementById('tiktok-cpa').innerText = tiktokCpa !== null ? tiktokCpa.toFixed(2) : '--';
                document.getElementById('google-cpa').innerText = googleCpa !== null ? googleCpa.toFixed(2) : '--';

                let timeText = buildRangeLabel(dateRangeState.preset, dateRangeState.date_from, dateRangeState.date_to);
                let nowStr = new Date().toLocaleTimeString('en-US');
                document.getElementById('update-time').innerText = `تقرير الأداء (${timeText}) - آخر تحديث: ${nowStr}`;
                document.getElementById('range-caption').innerHTML = `الفترة المعروضة: <b>${timeText}</b>`;

                updateChart(metaSpend, tiktokSpend, googleSpend);
                updateDonut(metaConv, tiktokConv, googleConv);
                renderExplorer();

                lastMetrics = {
                    timeText, nowStr,
                    metaSpend, tiktokSpend, googleSpend, totalSpend,
                    metaConv, tiktokConv, googleConv,
                    metaCtr, tiktokCtr, googleCtr,
                    metaCpa, tiktokCpa, googleCpa
                };
            }

            // ===== Name accessors shared with previous tree logic =====
            function campaignOf(i) { return i.campaign || i.campaign_name || 'حملة رئيسية'; }
            function groupOf(i) { return i.adset_name || i.adgroup_name || i.ad_group_name || 'المجموعة الإعلانية'; }
            function adOf(i) { return i.ad_name || 'الإعلان'; }

            function convOf(item, cfg) {
                if (cfg.isMeta) return parseMetaConversions(item);
                if (cfg.isGoogle) return parseGoogleConversions(item);
                return safeNum(item.conversions || item.conversion || item.results);
            }

            function platformCfg(key) {
                return PLATFORMS.find(p => p.key === key);
            }

            function aggregateRows(list, keyFn, cfg, level) {
                let map = {};
                const statusFields = (STATUS_FIELD_MAP[cfg.key] || {})[level] || [];

                list.forEach(i => {
                    let name = keyFn(i);
                    if (!map[name]) map[name] = { name, spend: 0, clicks: 0, impressions: 0, conv: 0, realStatus: null, lastStatusDate: null };
                    map[name].spend += safeNum(i.spend || i.cost);
                    map[name].clicks += safeNum(i.clicks);
                    map[name].impressions += safeNum(i.impressions);
                    map[name].conv += convOf(i, cfg);

                    // التقط حالة العنصر الحقيقية من المنصة: نجرّب حقول المرشحين بالترتيب
                    // (أول حقل يحمل قيمة صالحة يُعتمد)، مفضّلين أحدث صف بالتاريخ (الحالة
                    // قد تتغيّر خلال الفترة المعروضة).
                    for (const field of statusFields) {
                        const normalized = normalizeStatusValue(i[field]);
                        if (normalized !== null) {
                            const rowDate = i.date || '';
                            if (!map[name].lastStatusDate || rowDate >= map[name].lastStatusDate) {
                                map[name].realStatus = normalized;
                                map[name].lastStatusDate = rowDate;
                            }
                            break;
                        }
                    }
                });
                return Object.values(map).map(r => {
                    r.ctr = r.impressions > 0 ? (r.clicks / r.impressions) * 100 : 0;
                    r.cpa = r.conv > 0 ? r.spend / r.conv : null;
                    if (r.realStatus !== null) {
                        r.isActive = r.realStatus;
                        r.statusIsReal = true;
                    } else {
                        r.isActive = r.spend > 0; // تقدير احتياطي فقط إن لم تتوفر حالة حقيقية
                        r.statusIsReal = false;
                    }
                    return r;
                });
            }

            function getExplorerRows() {
                const cfg = platformCfg(explorerState.platform);
                const fullList = scopedList(globalData[cfg.dataKey]);
                let rows = [];

                if (explorerState.level === 'campaigns') {
                    rows = aggregateRows(fullList, campaignOf, cfg, 'campaigns');
                } else if (explorerState.level === 'adsets') {
                    let scoped = explorerState.selectedCampaign
                        ? fullList.filter(i => campaignOf(i) === explorerState.selectedCampaign)
                        : fullList;
                    rows = aggregateRows(scoped, groupOf, cfg, 'adsets');
                } else {
                    let scoped = fullList;
                    if (explorerState.selectedCampaign) scoped = scoped.filter(i => campaignOf(i) === explorerState.selectedCampaign);
                    if (explorerState.selectedGroup) scoped = scoped.filter(i => groupOf(i) === explorerState.selectedGroup);
                    rows = aggregateRows(scoped, adOf, cfg, 'ads');
                }

                if (explorerState.search.trim()) {
                    const q = explorerState.search.trim().toLowerCase();
                    rows = rows.filter(r => r.name.toLowerCase().includes(q));
                }

                rows.sort((a, b) => {
                    let av = a[explorerState.sortKey];
                    let bv = b[explorerState.sortKey];
                    if (av === null) av = -1;
                    if (bv === null) bv = -1;
                    if (typeof av === 'string') return av.localeCompare(bv) * explorerState.sortDir;
                    return (av - bv) * explorerState.sortDir;
                });

                return rows;
            }

            function levelCount(levelKey) {
                const prevLevel = explorerState.level;
                explorerState.level = levelKey;
                const rows = getExplorerRows();
                explorerState.level = prevLevel;
                return rows.length;
            }

            // يبني شجرة هرمية للتقرير القابل للنسخ، بناءً على التبويب النشط حالياً:
            // - الحملات: كل حملة معروضة + كل مجموعاتها + كل إعلانات كل مجموعة
            // - المجموعات: كل مجموعة معروضة + كل إعلاناتها
            // - الإعلانات: قائمة الإعلانات المعروضة فقط (بلا تفريع)
            // يعتمد على getExplorerRows() نفسها المستخدمة في رسم الجدول، فتبقى
            // نتيجة النسخ مطابقة تماماً لما يراه المستخدم فعلياً على الشاشة
            // (بعد البحث والفرز والفلاتر النشطة)، ثم يُوسَّع للمستويات الأعمق.
            function buildTreeForReport() {
                const cfg = platformCfg(explorerState.platform);
                const fullList = scopedList(globalData[cfg.dataKey]);

                let baseList = fullList;
                if (explorerState.selectedCampaign) {
                    baseList = baseList.filter(i => campaignOf(i) === explorerState.selectedCampaign);
                }
                if (explorerState.level === 'ads' && explorerState.selectedGroup) {
                    baseList = baseList.filter(i => groupOf(i) === explorerState.selectedGroup);
                }

                const displayedRows = getExplorerRows();

                if (explorerState.level === 'ads') {
                    return { level: 'ads', items: displayedRows };
                }

                if (explorerState.level === 'adsets') {
                    const items = displayedRows.map(adsetRow => {
                        const adsScoped = baseList.filter(i => groupOf(i) === adsetRow.name);
                        const ads = aggregateRows(adsScoped, adOf, cfg, 'ads');
                        return Object.assign({}, adsetRow, { ads });
                    });
                    return { level: 'adsets', items };
                }

                const items = displayedRows.map(campaignRow => {
                    const groupScoped = baseList.filter(i => campaignOf(i) === campaignRow.name);
                    const adsetRows = aggregateRows(groupScoped, groupOf, cfg, 'adsets');
                    const adsets = adsetRows.map(adsetRow => {
                        const adsScoped = groupScoped.filter(i => groupOf(i) === adsetRow.name);
                        const ads = aggregateRows(adsScoped, adOf, cfg, 'ads');
                        return Object.assign({}, adsetRow, { ads });
                    });
                    return Object.assign({}, campaignRow, { adsets });
                });
                return { level: 'campaigns', items };
            }

            // يصيغ سطر مؤشرات موحّد لعنصر واحد (حملة/مجموعة/إعلان) بمسافة بادئة قابلة للتحكم
            function formatMetricsLine(r, cfg, indent) {
                const cpaText = r.cpa !== null ? fmt(r.cpa) + ' ر.س' : '--';
                return `${indent}   💰 ${fmt(r.spend)} ر.س  |  👁️ ${r.impressions.toLocaleString('en-US')}  |  🖱️ ${r.clicks.toLocaleString('en-US')}  |  📈 CTR ${r.ctr.toFixed(1)}%\\n` +
                       `${indent}   🎯 ${cfg.resultLabel}: ${r.conv.toLocaleString('en-US')} (${cfg.resultSub})  |  💵 تكلفة/نتيجة: ${cpaText}`;
            }

            function buildTableReportText() {
                const cfg = platformCfg(explorerState.platform);
                const tree = buildTreeForReport();
                const rangeLabel = buildRangeLabel(dateRangeState.preset, dateRangeState.date_from, dateRangeState.date_to);
                const nowStr = new Date().toLocaleTimeString('en-US');
                const levelTitles = { campaigns: 'الحملات الإعلانية', adsets: 'المجموعات الإعلانية', ads: 'الإعلانات' };

                let lines = [];
                lines.push(`📊 *تقرير ${levelTitles[tree.level]} - ${cfg.label} - elevenz*`);
                lines.push(`🗓️ الفترة: ${rangeLabel}`);
                lines.push(`⏱️ وقت الإنشاء: ${nowStr}`);
                if (explorerState.selectedCampaign) lines.push(`📂 الحملة: ${explorerState.selectedCampaign}`);
                if (explorerState.selectedGroup) lines.push(`📁 المجموعة: ${explorerState.selectedGroup}`);
                lines.push('');

                if (tree.items.length === 0) {
                    lines.push('لا توجد بيانات مطابقة لهذه الفترة أو الفلتر الحالي.');
                    return lines.join('\\n');
                }

                if (tree.level === 'ads') {
                    tree.items.forEach(ad => {
                        const statusEmoji = ad.isActive ? '🟢' : '⚪';
                        lines.push(`📢 ${statusEmoji} *${ad.name}*`);
                        lines.push(formatMetricsLine(ad, cfg, ''));
                        lines.push('');
                    });
                } else if (tree.level === 'adsets') {
                    tree.items.forEach(adset => {
                        const statusEmoji = adset.isActive ? '🟢' : '⚪';
                        lines.push(`📁 ${statusEmoji} *${adset.name}*`);
                        lines.push(formatMetricsLine(adset, cfg, ''));
                        if (adset.ads.length > 0) {
                            lines.push(`   📢 الإعلانات (${adset.ads.length}):`);
                            adset.ads.forEach(ad => {
                                const adEmoji = ad.isActive ? '🟢' : '⚪';
                                lines.push(`      - ${adEmoji} *${ad.name}*`);
                                lines.push(formatMetricsLine(ad, cfg, '      '));
                            });
                        }
                        lines.push('');
                    });
                } else {
                    tree.items.forEach(camp => {
                        const statusEmoji = camp.isActive ? '🟢' : '⚪';
                        lines.push(`📂 ${statusEmoji} *${camp.name}*`);
                        lines.push(formatMetricsLine(camp, cfg, ''));
                        camp.adsets.forEach(adset => {
                            const adsetEmoji = adset.isActive ? '🟢' : '⚪';
                            lines.push(`   📁 ${adsetEmoji} *${adset.name}*`);
                            lines.push(formatMetricsLine(adset, cfg, '   '));
                            adset.ads.forEach(ad => {
                                const adEmoji = ad.isActive ? '🟢' : '⚪';
                                lines.push(`      📢 ${adEmoji} *${ad.name}*`);
                                lines.push(formatMetricsLine(ad, cfg, '      '));
                            });
                        });
                        lines.push('');
                    });
                }

                lines.push('_تم إنشاء هذا التقرير تلقائياً عبر منصة elevenz_');
                return lines.join('\\n');
            }

            function copyTableReport() {
                const text = buildTableReportText();
                const finish = () => showToast('تم نسخ التقرير بنجاح! 📋');
                if (navigator.clipboard && navigator.clipboard.writeText) {
                    navigator.clipboard.writeText(text).then(finish).catch(() => fallbackCopy(text, finish));
                } else {
                    fallbackCopy(text, finish);
                }
            }

            function renderExplorer() {
                renderPlatformTabs();
                renderLevelTabs();
                renderFilterChips();
                renderTableHead();
                renderTableBody();
            }

            function renderPlatformTabs() {
                const el = document.getElementById('platform-tabs');
                el.innerHTML = PLATFORMS.map(p => `
                    <div class="platform-tab ${explorerState.platform === p.key ? 'active' : ''}" onclick="setPlatform('${p.key}')">
                        <span class="pdot" style="background:${p.dot}"></span>${p.label}
                    </div>
                `).join('');
            }

            function renderLevelTabs() {
                const el = document.getElementById('level-tabs');
                el.innerHTML = LEVELS.map(l => `
                    <div class="level-tab ${explorerState.level === l.key ? 'active' : ''}" onclick="setLevel('${l.key}')">
                        <span class="tab-count">${levelCount(l.key)}</span>${l.label}
                    </div>
                `).join('');
            }

            function renderFilterChips() {
                const el = document.getElementById('filter-chips');
                let chips = '';
                if (explorerState.selectedCampaign) {
                    chips += `<span class="filter-chip">الحملة: ${escapeHtml(explorerState.selectedCampaign)}<button onclick="clearCampaign()">✕</button></span>`;
                }
                if (explorerState.selectedGroup) {
                    chips += `<span class="filter-chip">المجموعة: ${escapeHtml(explorerState.selectedGroup)}<button onclick="clearGroup()">✕</button></span>`;
                }
                el.innerHTML = chips;
                el.style.display = chips ? 'flex' : 'none';
            }

            function sortIco(key) {
                if (explorerState.sortKey !== key) return '↕';
                return explorerState.sortDir === 1 ? '↑' : '↓';
            }

            function renderTableHead() {
                const cfg = platformCfg(explorerState.platform);
                const head = document.getElementById('table-head');
                const cols = [
                    { key: null, label: '', sortable: false },
                    { key: 'name', label: 'الاسم', sortable: true },
                    { key: 'isActive', label: 'حالة العرض', sortable: true },
                    { key: 'conv', label: cfg.resultLabel, sortable: true },
                    { key: 'cpa', label: 'التكلفة لكل نتيجة', sortable: true },
                    { key: 'spend', label: 'المبلغ الذي تم إنفاقه', sortable: true },
                    { key: 'impressions', label: 'الظهور', sortable: true },
                    { key: 'clicks', label: 'النقرات', sortable: true },
                    { key: 'ctr', label: 'CTR', sortable: true }
                ];
                head.innerHTML = cols.map(c => {
                    if (!c.sortable) return `<th class="no-sort">${c.label}</th>`;
                    const sorted = explorerState.sortKey === c.key ? 'sorted' : '';
                    return `<th class="${sorted}" onclick="sortBy('${c.key}')">${c.label} <span class="sort-ico">${sortIco(c.key)}</span></th>`;
                }).join('');
            }

            function renderTableBody() {
                const cfg = platformCfg(explorerState.platform);
                const rows = getExplorerRows();
                const body = document.getElementById('table-body');
                const isLeaf = explorerState.level === 'ads';

                if (rows.length === 0) {
                    body.innerHTML = `<tr><td colspan="9"><div class="empty-state">لا توجد بيانات مطابقة لهذه الفترة أو الفلتر الحالي</div></td></tr>`;
                    return;
                }

                const icon = explorerState.level === 'campaigns' ? '📂' : (explorerState.level === 'adsets' ? '📁' : '🎯');

                body.innerHTML = rows.map(r => {
                    const cpaText = r.cpa !== null ? r.cpa.toFixed(2) + ' ر.س' : '--';
                    const badge = isLeaf ? ' ' + tierBadge(r.ctr) : '';
                    const drillAttr = isLeaf ? '' : `data-drill-name="${escapeHtml(r.name)}"`;
                    const rowClass = isLeaf ? 'data-row' : 'data-row clickable';
                    const statusTitle = r.statusIsReal
                        ? 'الحالة الفعلية من المنصة'
                        : 'حالة تقديرية (لا حقل حالة فعلي من المنصة لهذا العنصر) بناءً على الإنفاق خلال الفترة';
                    const statusSuffix = r.statusIsReal ? '' : ' *';

                    return `
                        <tr class="${rowClass}" ${drillAttr}>
                            <td>
                                <label class="toggle-switch" title="${statusTitle}">
                                    <input type="checkbox" ${r.isActive ? 'checked' : ''} disabled>
                                    <span class="toggle-slider"></span>
                                </label>
                            </td>
                            <td>
                                <div class="name-cell">
                                    <span class="name-icon">${icon}</span>
                                    <span>${escapeHtml(r.name)}</span>
                                    ${badge}
                                    ${!isLeaf ? '<span class="drill-arrow">‹</span>' : ''}
                                </div>
                            </td>
                            <td>
                                <span class="status-pill ${r.isActive ? 'status-active' : 'status-paused'}" title="${statusTitle}">
                                    <span class="status-dot"></span>${r.isActive ? 'نشطة' : 'متوقفة'}${statusSuffix}
                                </span>
                            </td>
                            <td>
                                <div class="metric-main">${r.conv.toLocaleString('en-US')}</div>
                                <div class="metric-sub">${cfg.resultSub}</div>
                            </td>
                            <td>${cpaText}</td>
                            <td>${r.spend.toFixed(2)} ر.س</td>
                            <td>${r.impressions.toLocaleString('en-US')}</td>
                            <td>${r.clicks.toLocaleString('en-US')}</td>
                            <td>${r.ctr.toFixed(1)}%</td>
                        </tr>
                    `;
                }).join('');
            }

            // تفويض حدث النقر (Event Delegation) بدل onclick مضمّن بالاسم داخل السلسلة
            // النصية - يُسجَّل مرة واحدة فقط، ويعمل مع كل الصفوف حتى بعد إعادة رسمها،
            // ولا يحمل أي خطر حقن HTML/JS مهما كان محتوى اسم الحملة/المجموعة.
            document.getElementById('table-body').addEventListener('click', (e) => {
                const row = e.target.closest('tr[data-drill-name]');
                if (row) drillInto(row.dataset.drillName);
            });

            function setPlatform(key) {
                explorerState.platform = key;
                explorerState.level = 'campaigns';
                explorerState.selectedCampaign = null;
                explorerState.selectedGroup = null;
                explorerState.search = '';
                document.getElementById('search-input').value = '';
                renderExplorer();
            }

            function setLevel(key) {
                explorerState.level = key;
                if (key === 'campaigns') { explorerState.selectedCampaign = null; explorerState.selectedGroup = null; }
                if (key === 'adsets') { explorerState.selectedGroup = null; }
                renderExplorer();
            }

            function drillInto(name) {
                if (explorerState.level === 'campaigns') {
                    explorerState.selectedCampaign = name;
                    explorerState.selectedGroup = null;
                    explorerState.level = 'adsets';
                } else if (explorerState.level === 'adsets') {
                    explorerState.selectedGroup = name;
                    explorerState.level = 'ads';
                }
                renderExplorer();
            }

            function clearCampaign() {
                explorerState.selectedCampaign = null;
                explorerState.selectedGroup = null;
                explorerState.level = 'campaigns';
                renderExplorer();
            }

            function clearGroup() {
                explorerState.selectedGroup = null;
                explorerState.level = 'adsets';
                renderExplorer();
            }

            function sortBy(key) {
                if (explorerState.sortKey === key) {
                    explorerState.sortDir *= -1;
                } else {
                    explorerState.sortKey = key;
                    explorerState.sortDir = -1;
                }
                renderExplorer();
            }

            function onSearch(val) {
                explorerState.search = val;
                renderExplorer();
            }

            function notImplementedYet() {
                showToast('هذه الميزة قيد التطوير قريباً ⏳');
            }

            function exportCsv() {
                const cfg = platformCfg(explorerState.platform);
                const rows = getExplorerRows();
                if (rows.length === 0) { showToast('لا توجد بيانات لتصديرها'); return; }

                const headers = ['الاسم', 'الحالة', cfg.resultLabel, 'التكلفة لكل نتيجة', 'الإنفاق', 'الظهور', 'النقرات', 'CTR'];
                const lines = [headers.join(',')];
                rows.forEach(r => {
                    const line = [
                        `"${r.name.replace(/"/g, '""')}"`,
                        r.isActive ? 'نشطة' : 'متوقفة',
                        r.conv,
                        r.cpa !== null ? r.cpa.toFixed(2) : '',
                        r.spend.toFixed(2),
                        r.impressions,
                        r.clicks,
                        r.ctr.toFixed(1)
                    ].join(',');
                    lines.push(line);
                });

                const csvContent = '\\ufeff' + lines.join('\\n');
                const blob = new Blob([csvContent], { type: 'text/csv;charset=utf-8;' });
                const url = URL.createObjectURL(blob);
                const a = document.createElement('a');
                a.href = url;
                a.download = `elevenz-${explorerState.platform}-${explorerState.level}-${dateRangeState.date_from}_to_${dateRangeState.date_to}.csv`;
                document.body.appendChild(a);
                a.click();
                document.body.removeChild(a);
                URL.revokeObjectURL(url);
                showToast('تم تصدير الملف بنجاح! 📁');
            }

            function fmt(n) { return (Math.round(n * 100) / 100).toLocaleString('en-US'); }

            function copyReport() {
                if (!lastMetrics) return;
                const m = lastMetrics;
                const text =
`📊 *تقرير أداء الإعلانات - elevenz*
🗓️ الفترة: ${m.timeText}
⏱️ آخر تحديث: ${m.nowStr}

💰 *إجمالي الإنفاق:* ${fmt(m.totalSpend)} ر.س

📘 *Meta Ads*
• الإنفاق: ${fmt(m.metaSpend)} ر.س
• النتائج: ${m.metaConv.toLocaleString('en-US')}
• CTR: ${m.metaCtr.toFixed(1)}%
• تكلفة/نتيجة: ${m.metaCpa !== null ? fmt(m.metaCpa) + ' ر.س' : '--'}

🎵 *TikTok Ads*
• الإنفاق: ${fmt(m.tiktokSpend)} ر.س
• التحويلات: ${m.tiktokConv.toLocaleString('en-US')}
• CTR: ${m.tiktokCtr.toFixed(1)}%
• تكلفة/نتيجة: ${m.tiktokCpa !== null ? fmt(m.tiktokCpa) + ' ر.س' : '--'}

🔍 *Google Ads*
• الإنفاق: ${fmt(m.googleSpend)} ر.س
• الإحالات: ${m.googleConv.toLocaleString('en-US')}
• CTR: ${m.googleCtr.toFixed(1)}%
• تكلفة/نتيجة: ${m.googleCpa !== null ? fmt(m.googleCpa) + ' ر.س' : '--'}

_تم إنشاء هذا التقرير تلقائياً عبر منصة elevenz_`;

                const finish = () => showToast('تم نسخ التقرير بنجاح! 📋');

                if (navigator.clipboard && navigator.clipboard.writeText) {
                    navigator.clipboard.writeText(text).then(finish).catch(() => fallbackCopy(text, finish));
                } else {
                    fallbackCopy(text, finish);
                }
            }

            function fallbackCopy(text, cb) {
                const ta = document.createElement('textarea');
                ta.value = text;
                ta.style.position = 'fixed';
                ta.style.opacity = '0';
                document.body.appendChild(ta);
                ta.select();
                try { document.execCommand('copy'); } catch (e) {}
                document.body.removeChild(ta);
                cb();
            }

            let toastTimer = null;
            function showToast(msg) {
                const toast = document.getElementById('toast');
                toast.innerText = msg;
                toast.classList.add('show');
                if (toastTimer) clearTimeout(toastTimer);
                toastTimer = setTimeout(() => toast.classList.remove('show'), 2600);
            }

            document.addEventListener('DOMContentLoaded', () => {
                applyDatePreset('yesterday');
            });
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)
