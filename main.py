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
        <title>تسجيل الدخول | Elevenz</title>
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
            <img class="login-logo" src="data:image/png;base64,{LOGO_BASE64}" alt="Elevenz">
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
            <div class="login-footer">Elevenz — منصة تقارير الأداء الإعلاني</div>
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
        # الطبقة الأولى: حقول الحالة + الهدف + مؤشرات جودة ميتا الأصلية الثلاثة
        # (quality/engagement/conversion_rate_ranking) - مؤكدة جميعها فعلياً عبر
        # استعلام حي لحساب Windsor.ai الفعلي (get_fields) أثناء تدقيق المنصة، وليست
        # تخميناً. تُستخدم لاحقاً في التقييم الواعي بالهدف بدل حدّ CTR ثابت.
        "campaign_status,adset_status,effective_status,campaign_objective,quality_ranking,engagement_rate_ranking,conversion_rate_ranking",
        "campaign_status,adset_status,effective_status",
        "",
    ],
    "tiktok": [
        # مؤكدة جميعها عبر get_fields: campaign_operation_status/ad_group_operation_status/
        # ad_operation_status تُرجع Enum نظيف (ENABLE/DISABLE/FROZEN)، والحقول الثلاثة
        # الأخرى نصية عامة (Campaign/Adgroup/Ad Status) كبديل احتياطي إضافي بنفس الطلب.
        # objective_type مؤكد أيضاً - يُستخدم للتقييم الواعي بالهدف.
        "campaign_operation_status,ad_group_operation_status,ad_operation_status,campaign_status,adgroup_status,ad_status,objective_type",
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
            logger.info(f"جاري تحديث بيانات إعلانات Elevenz... (النطاق الزمني: {date_from} إلى {date_to})")

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

app = FastAPI(title="Elevenz | منصة تقارير الأداء الإعلاني", lifespan=lifespan)

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
        <title>Elevenz | منصة تقارير الأداء الإعلاني</title>
        <link rel="preconnect" href="https://fonts.googleapis.com">
        <link href="https://fonts.googleapis.com/css2?family=Cairo:wght@400;500;600;700;800;900&family=Manrope:wght@600;700;800&display=swap" rel="stylesheet">
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
            .brand-home {
                display: block; width: fit-content; cursor: pointer;
                text-decoration: none; border-radius: 10px;
                transition: opacity 0.15s ease, transform 0.15s ease;
            }
            .brand-home:hover { opacity: 0.85; }
            .brand-home:active { transform: scale(0.97); }
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
            .sidebar-menu li a.nav-disabled { cursor: default; justify-content: space-between; opacity: 0.65; }
            .sidebar-menu li a.nav-disabled:hover { background-color: transparent; color: #9fb0c7; }
            .nav-soon {
                font-size: 10px; font-weight: 700; background: rgba(255,255,255,0.1);
                padding: 2px 8px; border-radius: 999px; color: #c3cede;
            }

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
            .top-actions { display: flex; gap: 10px; flex-wrap: wrap; }

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
            .card-compare { margin-top: 8px; min-height: 20px; }
            .card-accounts { margin-top: 8px; display: flex; flex-direction: column; gap: 3px; }
            .card-accounts .account-row {
                display: flex; align-items: center; justify-content: space-between;
                font-size: 11px; font-weight: 600; color: var(--text-muted);
            }
            .card-accounts .account-row .account-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 55%; }
            .card-accounts .account-row b { color: var(--text-dark); font-weight: 800; white-space: nowrap; }
            .card-accounts .account-row .account-stats { display: flex; align-items: baseline; gap: 5px; white-space: nowrap; }
            .card-accounts .account-row .account-conv { color: var(--text-muted); font-weight: 700; font-size: 10.5px; }
            .card-accounts .accounts-caption { font-size: 10.5px; font-weight: 700; color: var(--text-muted); opacity: .85; }
            .compare-pill {
                display: inline-flex; align-items: center; gap: 4px;
                padding: 3px 9px; border-radius: 999px; font-size: 11px; font-weight: 800;
            }
            .compare-pill.up { background: var(--stage-green-bg); color: var(--stage-green-text); }
            .compare-pill.down { background: var(--stage-red-bg); color: var(--stage-red-text); }
            .compare-pill.flat { background: var(--stage-gray-bg); color: var(--stage-gray-text); }
            .compare-pill .compare-label { color: var(--text-muted); font-weight: 600; margin-right: 4px; }
            .card-meta span b { color: var(--text-dark); font-weight: 800; }

            /* ===================== KPI Cards v2 ===================== */
            /* كل بطاقة تأخذ هويتها اللونية من متغيرات --kpi-* المعرفة في كلاس المنصة */
            .kpi-card {
                --kpi-accent: #64748b; --kpi-accent-2: #94a3b8;
                --kpi-tint: rgba(100, 116, 139, 0.06); --kpi-border: rgba(100, 116, 139, 0.18);
                --kpi-border-hover: rgba(100, 116, 139, 0.40); --kpi-glow: rgba(100, 116, 139, 0.10);
                --kpi-shadow: rgba(15, 37, 64, 0.25); --kpi-ink: #334155; --kpi-icon-bg: #ffffff;
                position: relative; overflow: hidden; isolation: isolate;
                background: linear-gradient(180deg, var(--kpi-tint) 0%, rgba(255, 255, 255, 0.88) 58%, rgba(255, 255, 255, 0.96) 100%);
                -webkit-backdrop-filter: blur(10px); backdrop-filter: blur(10px);
                border: 1px solid var(--kpi-border);
                border-radius: 16px;
                padding: 24px 22px 20px;
                box-shadow: 0 1px 1px rgba(15, 37, 64, 0.03), 0 2px 4px -1px rgba(15, 37, 64, 0.04), 0 10px 22px -14px rgba(15, 37, 64, 0.14);
                transition: transform 0.3s cubic-bezier(0.2, 0.8, 0.2, 1), box-shadow 0.3s ease, border-color 0.3s ease;
            }
            .kpi-card::before {
                content: ""; position: absolute; top: 0; right: 0; left: 0; height: 3px;
                background: linear-gradient(90deg, var(--kpi-accent), var(--kpi-accent-2));
            }
            .kpi-card::after {
                content: ""; position: absolute; z-index: -1; pointer-events: none;
                width: 200px; height: 200px; top: -100px; left: -70px; border-radius: 50%;
                background: radial-gradient(circle, var(--kpi-glow) 0%, transparent 70%);
            }
            .kpi-card:hover {
                transform: translateY(-4px);
                border-color: var(--kpi-border-hover);
                box-shadow: 0 0 0 4px var(--kpi-glow), 0 2px 4px -1px rgba(15, 37, 64, 0.05), 0 20px 36px -18px var(--kpi-shadow);
            }
            .kpi-card .card-top { margin-bottom: 16px; }
            .kpi-card .card-title { font-size: 13px; font-weight: 700; color: #64748b; }
            .kpi-card .card-icon {
                width: 40px; height: 40px; border-radius: 12px; color: var(--kpi-accent);
                background: var(--kpi-icon-bg);
                box-shadow: 0 0 0 1px var(--kpi-border), 0 6px 14px -6px var(--kpi-shadow);
            }
            .kpi-card .card-value {
                display: flex; align-items: baseline; gap: 6px; flex-wrap: wrap;
                margin-bottom: 6px; letter-spacing: 0;
            }
            .kpi-num {
                font-family: 'Manrope', 'Cairo', sans-serif; font-size: 32px; font-weight: 800;
                line-height: 1.1; letter-spacing: -1px; color: #0b1b33;
                font-variant-numeric: tabular-nums; direction: ltr; unicode-bidi: isolate;
            }
            .kpi-cur { font-size: 13px; font-weight: 700; color: #94a3b8; }
            .kpi-card .card-sub { color: var(--kpi-ink); font-size: 12.5px; font-weight: 800; }
            .kpi-card .card-compare { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 10px; min-height: 22px; }
            .kpi-card .card-compare:empty { display: none; }
            .kpi-card .card-accounts { margin-top: 12px; padding: 8px 10px; border-radius: 10px; background: rgba(255, 255, 255, 0.7); box-shadow: inset 0 0 0 1px var(--kpi-border); }
            .kpi-card .card-accounts:empty { display: none; }
            .kpi-card .card-meta { border-top: 1px solid var(--kpi-border); margin-top: 14px; padding-top: 12px; }
            .kpi-card .card-meta span b { font-family: 'Manrope', 'Cairo', sans-serif; font-variant-numeric: tabular-nums; }

            /* إجمالي الإنفاق: زمرد */
            .kpi-total {
                --kpi-accent: #059669; --kpi-accent-2: #34d399;
                --kpi-tint: rgba(16, 185, 129, 0.09); --kpi-border: rgba(16, 185, 129, 0.24);
                --kpi-border-hover: rgba(16, 185, 129, 0.50); --kpi-glow: rgba(16, 185, 129, 0.13);
                --kpi-shadow: rgba(5, 150, 105, 0.40); --kpi-ink: #047857; --kpi-icon-bg: #ecfdf5;
            }
            /* Meta: أزرق */
            .kpi-meta {
                --kpi-accent: #0866ff; --kpi-accent-2: #60a5fa;
                --kpi-tint: rgba(8, 102, 255, 0.08); --kpi-border: rgba(8, 102, 255, 0.22);
                --kpi-border-hover: rgba(8, 102, 255, 0.50); --kpi-glow: rgba(8, 102, 255, 0.12);
                --kpi-shadow: rgba(8, 102, 255, 0.38); --kpi-ink: #0550c8; --kpi-icon-bg: #eff6ff;
            }
            /* TikTok: نيون Cyan/Magenta بإطار داكن */
            .kpi-tiktok {
                --kpi-accent: #25f4ee; --kpi-accent-2: #fe2c55;
                --kpi-tint: rgba(37, 244, 238, 0.08); --kpi-border: rgba(15, 23, 42, 0.16);
                --kpi-border-hover: rgba(254, 44, 85, 0.45); --kpi-glow: rgba(254, 44, 85, 0.10);
                --kpi-shadow: rgba(15, 15, 15, 0.38); --kpi-ink: #e11d48; --kpi-icon-bg: #0f0f0f;
                background:
                    radial-gradient(120% 70% at 100% 0%, rgba(37, 244, 238, 0.11), transparent 60%),
                    radial-gradient(90% 60% at 0% 0%, rgba(254, 44, 85, 0.08), transparent 60%),
                    rgba(255, 255, 255, 0.94);
            }
            /* Google: ألوان جوجل + تدرج كهرماني هادئ */
            .kpi-google {
                --kpi-accent: #fbbc04; --kpi-accent-2: #f97316;
                --kpi-tint: rgba(251, 188, 4, 0.10); --kpi-border: rgba(217, 119, 6, 0.22);
                --kpi-border-hover: rgba(217, 119, 6, 0.50); --kpi-glow: rgba(251, 188, 4, 0.16);
                --kpi-shadow: rgba(217, 119, 6, 0.35); --kpi-ink: #b45309; --kpi-icon-bg: #ffffff;
            }
            .kpi-google::before {
                background: linear-gradient(90deg, #4285f4 0 25%, #ea4335 25% 50%, #fbbc04 50% 75%, #34a853 75% 100%);
            }

            /* شارات المقارنة: كبسولات مضيئة */
            .compare-pill {
                display: inline-flex; align-items: center; gap: 5px;
                padding: 3px 10px; border-radius: 999px; font-size: 11px; font-weight: 800;
                line-height: 1.6; font-variant-numeric: tabular-nums; white-space: nowrap;
            }
            .compare-pill.up {
                background: rgba(16, 185, 129, 0.12); color: #047857;
                box-shadow: inset 0 0 0 1px rgba(16, 185, 129, 0.30), 0 0 14px -4px rgba(16, 185, 129, 0.45);
            }
            .compare-pill.down {
                background: rgba(239, 68, 68, 0.10); color: #b91c1c;
                box-shadow: inset 0 0 0 1px rgba(239, 68, 68, 0.28), 0 0 14px -4px rgba(239, 68, 68, 0.40);
            }
            .compare-pill.flat {
                background: rgba(100, 116, 139, 0.10); color: #475569;
                box-shadow: inset 0 0 0 1px rgba(100, 116, 139, 0.22);
            }
            .compare-pill .compare-label { color: inherit; opacity: 0.72; font-weight: 700; margin: 0; }

            @media (prefers-reduced-motion: reduce) {
                .kpi-card { transition: none; }
                .kpi-card:hover { transform: none; }
            }

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

            /* ===== مركز القرار ===== */
            .dc-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 14px; }
            .dc-column { display: flex; flex-direction: column; min-width: 0; }
            .dc-column-header {
                display: flex; align-items: center; justify-content: space-between;
                padding: 10px 12px; border-radius: var(--radius-sm) var(--radius-sm) 0 0;
                font-size: 12.5px; font-weight: 800;
            }
            .dc-column-header.dc-critical { background: var(--stage-red-bg); color: var(--stage-red-text); }
            .dc-column-header.dc-opportunity { background: var(--accent-orange-light); color: var(--accent-orange); }
            .dc-column-header.dc-monitor { background: var(--stage-blue-bg); color: var(--stage-blue-text); }
            .dc-column-header.dc-good { background: var(--stage-green-bg); color: var(--stage-green-text); }
            .dc-count {
                background: rgba(255,255,255,0.6); border-radius: 999px; min-width: 20px; height: 20px;
                display: inline-flex; align-items: center; justify-content: center; font-size: 11px; padding: 0 6px;
            }
            .dc-items {
                display: flex; flex-direction: column; gap: 8px; padding: 10px;
                background: var(--nested-bg); border-radius: 0 0 var(--radius-sm) var(--radius-sm);
                flex: 1; min-height: 80px;
            }
            .dc-card {
                background: var(--card-bg); border: 1px solid var(--border-color); border-radius: var(--radius-sm);
                padding: 10px 12px; box-shadow: var(--shadow-card);
            }
            .dc-card-top { display: flex; align-items: center; gap: 6px; margin-bottom: 4px; }
            .dc-card-name { font-size: 12.5px; font-weight: 800; color: var(--text-dark); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
            .dc-card-stats { font-size: 11px; color: var(--text-muted); font-weight: 600; margin-bottom: 6px; }
            .dc-evidence { font-size: 11px; color: var(--text-dark); font-weight: 600; line-height: 1.6; margin-bottom: 4px; }
            .dc-action { font-size: 11px; color: var(--accent-orange); font-weight: 700; }
            .dc-conf-tag {
                display: inline-block; background: var(--stage-gray-bg); color: var(--stage-gray-text);
                font-size: 9.5px; font-weight: 800; padding: 1px 6px; border-radius: 999px; margin-right: 4px;
            }
            .dc-empty { text-align: center; color: var(--text-faint); font-size: 12px; font-weight: 600; padding: 16px 8px; }

            @media (max-width: 1100px) {
                .dc-grid { grid-template-columns: repeat(2, 1fr); }
            }
            @media (max-width: 860px) {
                .dc-grid { grid-template-columns: 1fr; }
            }

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

            /* ===== إتاحة الوصول: حلقة تركيز للوحة المفاتيح (لم تكن موجودة إلا لحقول النص) ===== */
            button:focus-visible, a:focus-visible, input:focus-visible,
            [tabindex]:focus-visible, .time-btn:focus-visible, .toolbar-btn:focus-visible {
                outline: 2px solid var(--accent-orange);
                outline-offset: 2px;
                border-radius: 6px;
            }

            /* ===== شريط علوي للجوال + قائمة جانبية منزلقة (Off-canvas) ===== */
            .mobile-topbar { display: none; }
            .sidebar-overlay { display: none; }

            /* ===== حالة التحميل: تعتيم لطيف للأرقام أثناء الجلب بدل بقائها ثابتة دون أي إشارة ===== */
            .is-loading { opacity: 0.45; pointer-events: none; transition: opacity 0.2s ease; filter: saturate(0.6); }
            .load-bar {
                position: fixed; top: 0; right: 0; left: 0; height: 3px; z-index: 2000;
                background: var(--accent-orange); transform: scaleX(0); transform-origin: right;
                transition: transform 0.3s ease; opacity: 0;
            }
            .load-bar.active { opacity: 1; animation: loadBarPulse 1.1s ease-in-out infinite; }
            @keyframes loadBarPulse {
                0% { transform: scaleX(0.15); }
                50% { transform: scaleX(0.65); }
                100% { transform: scaleX(0.15); }
            }

            @media (max-width: 860px) {
                body { overflow: visible; height: auto; min-height: 100vh; }
                .mobile-topbar {
                    display: flex; align-items: center; justify-content: space-between;
                    position: sticky; top: 0; z-index: 500;
                    background: var(--sidebar-bg); padding: 14px 18px; gap: 12px;
                }
                .mobile-topbar-logo { color: #fff; font-size: 20px; font-weight: 800; }
                .mobile-topbar-logo span { color: var(--accent-orange); }
                .hamburger-btn {
                    background: rgba(255,255,255,0.08); border: none; border-radius: 10px;
                    width: 40px; height: 40px; display: flex; align-items: center; justify-content: center;
                    cursor: pointer; color: #fff; flex-shrink: 0;
                }
                .hamburger-btn svg { width: 20px; height: 20px; }

                .sidebar {
                    position: fixed; top: 0; bottom: 0; right: 0; width: 84%; max-width: 300px;
                    z-index: 1000; transform: translateX(100%);
                    transition: transform 0.25s ease; box-shadow: -12px 0 32px rgba(0,0,0,0.25);
                }
                .sidebar.open { transform: translateX(0); }
                .sidebar-overlay {
                    display: block; position: fixed; inset: 0; background: rgba(7,28,53,0.5);
                    z-index: 900; opacity: 0; pointer-events: none; transition: opacity 0.2s ease;
                }
                .sidebar-overlay.open { opacity: 1; pointer-events: auto; }

                .main-content { padding: 18px 16px; height: auto; }
                .cards-container { grid-template-columns: 1fr !important; }
                .grid-2 { grid-template-columns: 1fr !important; }
                .top-bar { flex-direction: column; align-items: stretch; }
            }

            @media (max-width: 1100px) {
                .cards-container { grid-template-columns: repeat(2, 1fr); }
                .grid-2 { grid-template-columns: 1fr; }
            }
        </style>
    </head>
    <body>
        <div class="load-bar" id="load-bar"></div>
        <div class="mobile-topbar">
            <button class="hamburger-btn" onclick="toggleSidebar()" aria-label="فتح القائمة">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="3" y1="6" x2="21" y2="6"></line><line x1="3" y1="12" x2="21" y2="12"></line><line x1="3" y1="18" x2="21" y2="18"></line></svg>
            </button>
            <a href="/" class="mobile-topbar-logo brand-home" onclick="goHome(event)" aria-label="الرجوع لأعلى الصفحة وتحديث البيانات">eleven<span>z</span></a>
            <span style="width:40px;"></span>
        </div>
        <div class="sidebar-overlay" id="sidebar-overlay" onclick="toggleSidebar()"></div>
        <div class="sidebar" id="sidebar">

            <div class="sidebar-logo"><a href="/" class="brand-home" id="brandLogo" onclick="goHome(event)" title="الرئيسية" aria-label="الرجوع لأعلى الصفحة وتحديث البيانات"><img src="data:image/png;base64,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" alt="Elevenz" style="max-width:150px; height:auto; display:block;"></a></div>
            <ul class="sidebar-menu">
                <li class="active"><a href="#" onclick="closeSidebarIfMobile()">📊 الإعلانات والتحليلات</a></li>
                <li><a href="javascript:void(0)" class="nav-disabled" onclick="notImplementedYet(); closeSidebarIfMobile();">🎯 الحملات النشطة <span class="nav-soon">قريباً</span></a></li>
                <li><a href="javascript:void(0)" class="nav-disabled" onclick="notImplementedYet(); closeSidebarIfMobile();">📈 تقارير الأداء الأسبوعية <span class="nav-soon">قريباً</span></a></li>
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
                    <h1>Elevenz | منصة تقارير الأداء الإعلاني</h1>
                    <p id="update-time">متابعة لحظية لأداء حملاتك ومؤشراتها الرئيسية | آخر تحديث: --</p>
                </div>
                <div class="top-actions">
                    <button class="btn btn-copy" onclick="copyReportPlain()">
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="12" height="12" rx="3"></rect><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path></svg>
                        نسخ
                    </button>
                    <button class="btn btn-copy" onclick="copyReportForAnalysis()">
                        🤖 نسخ للتحليل
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
                <div class="card kpi-card kpi-total">
                    <div class="card-top">
                        <div class="card-title">إجمالي الإنفاق (الكلي)</div>
                        <div class="card-icon icon-wallet">
                            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12V7H5a2 2 0 0 1 0-4h14v4"></path><path d="M3 5v14a2 2 0 0 0 2 2h16v-5"></path><path d="M18 12a2 2 0 0 0 0 4h4v-4Z"></path></svg>
                        </div>
                    </div>
                    <div class="card-value" id="total-spend"><span class="kpi-num">0.00</span><span class="kpi-cur">ر.س</span></div>
                    <div class="card-sub">جميع حسابات الربط</div>
                    <div class="card-compare"><span id="total-compare"></span><span id="total-conv-compare"></span></div>
                    <div class="card-meta">
                        <span>CPC: <b id="total-cpc">0.00</b></span>
                        <span>CTR: <b id="total-ctr">0.0%</b></span>
                    </div>
                </div>
                <div class="card kpi-card kpi-google">
                    <div class="card-top">
                        <div class="card-title">Google Ads</div>
                        <div class="card-icon">
                            <svg viewBox="0 0 48 48"><path fill="#FFC107" d="M43.611,20.083H42V20H24v8h11.303c-1.649,4.657-6.08,8-11.303,8c-6.627,0-12-5.373-12-12c0-6.627,5.373-12,12-12c3.059,0,5.842,1.154,7.961,3.039l5.657-5.657C34.046,6.053,29.268,4,24,4C12.955,4,4,12.955,4,24c0,11.045,8.955,20,20,20c11.045,0,20-8.955,20-20C44,22.659,43.862,21.35,43.611,20.083z"></path><path fill="#FF3D00" d="M6.306,14.691l6.571,4.819C14.655,15.108,18.961,12,24,12c3.059,0,5.842,1.154,7.961,3.039l5.657-5.657C34.046,6.053,29.268,4,24,4C16.318,4,9.656,8.337,6.306,14.691z"></path><path fill="#4CAF50" d="M24,44c5.166,0,9.86-1.977,13.409-5.192l-6.19-5.238C29.211,35.091,26.715,36,24,36c-5.202,0-9.619-3.317-11.283-7.946l-6.522,5.025C9.505,39.556,16.227,44,24,44z"></path><path fill="#1976D2" d="M43.611,20.083H42V20H24v8h11.303c-0.792,2.237-2.231,4.166-4.087,5.571c0.001-0.001,0.002-0.001,0.003-0.002l6.19,5.238C36.971,39.205,44,34,44,24C44,22.659,43.862,21.35,43.611,20.083z"></path></svg>
                        </div>
                    </div>
                    <div class="card-value" id="google-spend"><span class="kpi-num">0.00</span><span class="kpi-cur">ر.س</span></div>
                    <div class="card-sub" id="google-sub">0 إحالات</div>
                    <div class="card-compare"><span id="google-compare"></span><span id="google-conv-compare"></span></div>
                    <div class="card-accounts" id="google-accounts"></div>
                    <div class="card-meta">
                        <span>CPA: <b id="google-cpa">--</b></span>
                        <span>CTR: <b id="google-ctr">0.0%</b></span>
                    </div>
                </div>
                <div class="card kpi-card kpi-tiktok">
                    <div class="card-top">
                        <div class="card-title">TikTok Ads</div>
                        <div class="card-icon">
                            <svg viewBox="0 0 48 48"><path fill="#25F4EE" d="M33.6,15.4c-2.1-1.4-3.6-3.6-4-6.2c-0.1-0.5-0.1-1-0.1-1.5h-6v24.6c0,2.9-2.4,5.3-5.3,5.3 c-0.9,0-1.8-0.2-2.5-0.7c-1.7-0.9-2.8-2.7-2.8-4.7c0-2.9,2.4-5.3,5.3-5.3c0.5,0,1.1,0.1,1.6,0.3v-6.1c-0.5-0.1-1-0.1-1.6-0.1 c-6.3,0-11.4,5.1-11.4,11.4c0,3.9,1.9,7.3,4.9,9.4c1.9,1.3,4.2,2.1,6.7,2.1c6.3,0,11.4-5.1,11.4-11.4V19.1 c2.4,1.8,5.4,2.8,8.6,2.8v-6C36.9,15.9,35.1,15.8,33.6,15.4z"></path><path fill="#FE2C55" d="M31.6,13.4c-2.1-1.4-3.6-3.6-4-6.2c-0.1-0.5-0.1-1-0.1-1.5h-6v24.6c0,2.9-2.4,5.3-5.3,5.3 c-0.9,0-1.8-0.2-2.5-0.7c-1.5-0.8-2.6-2.4-2.8-4.2c-0.4-2.9,1.7-5.6,4.5-6c0.5-0.1,1.1-0.1,1.6,0v-6.1c-6.2-0.1-11.3,4.9-11.4,11.1 c0,3.9,1.9,7.5,4.9,9.6c1.9,1.3,4.2,2.1,6.7,2.1c6.3,0,11.4-5.1,11.4-11.4V17.1c2.4,1.8,5.4,2.8,8.6,2.8v-6 C34.9,13.9,33.1,13.8,31.6,13.4z"></path><path fill="#ffffff" d="M35,17.9c-3.2,0-6.2-1-8.6-2.8v14.7c0,6.3-5.1,11.4-11.4,11.4c-2.5,0-4.8-0.8-6.7-2.1 c2.1,2.3,5.1,3.7,8.4,3.7c6.3,0,11.4-5.1,11.4-11.4V16.7c2.4,1.8,5.4,2.8,8.6,2.8v-6C36.4,13.5,36.4,13.5,35,17.9z"></path></svg>
                        </div>
                    </div>
                    <div class="card-value" id="tiktok-spend"><span class="kpi-num">0.00</span><span class="kpi-cur">ر.س</span></div>
                    <div class="card-sub" id="tiktok-sub">0 تحويل/نقرة</div>
                    <div class="card-compare"><span id="tiktok-compare"></span><span id="tiktok-conv-compare"></span></div>
                    <div class="card-accounts" id="tiktok-accounts"></div>
                    <div class="card-meta">
                        <span>CPA: <b id="tiktok-cpa">--</b></span>
                        <span>CTR: <b id="tiktok-ctr">0.0%</b></span>
                    </div>
                </div>
                <div class="card kpi-card kpi-meta">
                    <div class="card-top">
                        <div class="card-title">Meta Ads</div>
                        <div class="card-icon">
                            <svg viewBox="0 0 36 36"><path fill="#1877F2" d="M36,18c0-9.94-8.06-18-18-18S0,8.06,0,18c0,8.98,6.58,16.41,15.19,17.76V23.13h-4.57V18h4.57v-3.91 c0-4.51,2.69-7.01,6.8-7.01c1.97,0,4.03,0.35,4.03,0.35v4.43h-2.27c-2.24,0-2.94,1.39-2.94,2.81V18h5.01l-0.8,5.13h-4.21v12.63 C29.42,34.41,36,26.98,36,18z"></path></svg>
                        </div>
                    </div>
                    <div class="card-value" id="meta-spend"><span class="kpi-num">0.00</span><span class="kpi-cur">ر.س</span></div>
                    <div class="card-sub" id="meta-sub">0 محادثة/نتيجة</div>
                    <div class="card-compare"><span id="meta-compare"></span><span id="meta-conv-compare"></span></div>
                    <div class="card-accounts" id="meta-accounts"></div>
                    <div class="card-meta">
                        <span>CPA: <b id="meta-cpa">--</b></span>
                        <span>CTR: <b id="meta-ctr">0.0%</b></span>
                    </div>
                </div>
            </div>

            <div class="chart-section" id="decision-center-section" style="margin-bottom:22px;">
                <div class="chart-section-title">مركز القرار</div>
                <div class="chart-section-sub">تصنيف تلقائي للإعلانات بناءً على تقييم واعٍ بالهدف (مؤشرات ميتا الأصلية حيث تتوفر)، تلميحات السبب الجذري، ورصد هدر الميزانية — كل بند مبني على دليل رقمي واضح</div>
                <div class="dc-grid" id="decision-center-body">
                    <div class="empty-state">جاري تجهيز التحليل...</div>
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

            <div class="chart-section" id="daily-trend-section" style="margin-bottom:22px;">
                <div style="display:flex; align-items:flex-start; justify-content:space-between; flex-wrap:wrap; gap:10px; margin-bottom:16px;">
                    <div>
                        <div class="chart-section-title" style="margin-bottom:4px;">الأداء اليومي خلال الفترة</div>
                        <div class="chart-section-sub">كيف يقارن كل يوم باليوم الذي قبله - لكل منصة على حدة</div>
                    </div>
                    <div class="time-selector" style="margin-bottom:0;">
                        <button class="time-btn" data-metric="spend" onclick="setDailyMetric('spend', this)">الإنفاق</button>
                        <button class="time-btn active" data-metric="conv" onclick="setDailyMetric('conv', this)">النتائج</button>
                    </div>
                </div>
                <div class="chart-wrapper" id="daily-trend-wrapper">
                    <canvas id="dailyTrendChart"></canvas>
                </div>
                <div class="empty-state" id="daily-trend-empty" style="display:none;">اختر فترة أطول من يوم واحد لعرض الاتجاه اليومي</div>
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
                        <div class="date-filter-dropdown" id="columns-dropdown">
                            <button class="toolbar-btn" onclick="toggleColumnsDropdown(event)">
                                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7" height="18"></rect><rect x="14" y="3" width="7" height="10"></rect></svg>
                                الأعمدة
                                <svg class="chevron-down" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"></polyline></svg>
                            </button>
                            <div class="date-filter-panel" style="width:230px;">
                                <div class="date-filter-presets" id="columns-checklist"></div>
                            </div>
                        </div>
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
            let previousMetrics = null;
            let previousRawData = null;
            // حالة إظهار/إخفاء أعمدة الجدول - عمودا المقارنة الجديدان مفعَّلان
            // افتراضياً لأن هذا بالضبط ما طُلب "تفعيله"، وبقية الأعمدة قياسية.
            let columnVisibility = {
                status: true, results: true, cpa: true, spend: true,
                impressions: true, clicks: true, ctr: true,
                compareSpend: true, compareConv: true
            };
            let previousRangeInfo = null;
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
                const loadBar = document.getElementById('load-bar');
                if (loadBar) loadBar.classList.toggle('active', loading);
                document.querySelectorAll('.cards-container, .explorer-card').forEach(el => {
                    el.classList.toggle('is-loading', loading);
                });
            }

            // ===== القائمة الجانبية المنزلقة للجوال (Off-canvas) =====
            function toggleSidebar() {
                const sidebar = document.getElementById('sidebar');
                const overlay = document.getElementById('sidebar-overlay');
                if (!sidebar || !overlay) return;
                const opening = !sidebar.classList.contains('open');
                sidebar.classList.toggle('open', opening);
                overlay.classList.toggle('open', opening);
            }

            function closeSidebarIfMobile() {
                const sidebar = document.getElementById('sidebar');
                const overlay = document.getElementById('sidebar-overlay');
                if (sidebar) sidebar.classList.remove('open');
                if (overlay) overlay.classList.remove('open');
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
                closeColumnsDropdown();
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

            // ===== منتقي الأعمدة (Columns picker) =====
            const COLUMN_DEFS = [
                { key: 'status', label: 'حالة العرض' },
                { key: 'results', label: 'النتائج' },
                { key: 'compareConv', label: 'مقارنة النتائج بالفترة السابقة' },
                { key: 'cpa', label: 'التكلفة لكل نتيجة' },
                { key: 'spend', label: 'المبلغ الذي تم إنفاقه' },
                { key: 'compareSpend', label: 'مقارنة الإنفاق بالفترة السابقة' },
                { key: 'impressions', label: 'الظهور' },
                { key: 'clicks', label: 'النقرات' },
                { key: 'ctr', label: 'CTR' }
            ];

            function renderColumnsChecklist() {
                const el = document.getElementById('columns-checklist');
                if (!el) return;
                el.innerHTML = COLUMN_DEFS.map(c => `
                    <label style="display:flex; align-items:center; gap:8px; padding:7px 4px; font-size:12.5px; font-weight:600; color:var(--text-dark); cursor:pointer;">
                        <input type="checkbox" ${columnVisibility[c.key] ? 'checked' : ''} onchange="toggleColumnVisibility('${c.key}', this.checked)">
                        ${c.label}
                    </label>
                `).join('');
            }

            function toggleColumnVisibility(key, checked) {
                columnVisibility[key] = checked;
                renderTableHead();
                renderTableBody();
            }

            function toggleColumnsDropdown(evt) {
                if (evt) evt.stopPropagation();
                closeDateDropdown();
                const dd = document.getElementById('columns-dropdown');
                const opening = !dd.classList.contains('open');
                dd.classList.toggle('open', opening);
                if (opening) renderColumnsChecklist();
            }

            function closeColumnsDropdown() {
                const dd = document.getElementById('columns-dropdown');
                if (dd) dd.classList.remove('open');
            }

            document.addEventListener('click', (e) => {
                const dd = document.getElementById('date-filter-dropdown');
                if (dd && dd.classList.contains('open') && !dd.contains(e.target)) {
                    dd.classList.remove('open');
                }
                const cdd = document.getElementById('columns-dropdown');
                if (cdd && cdd.classList.contains('open') && !cdd.contains(e.target)) {
                    cdd.classList.remove('open');
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

            // يجمّع بيانات المنصات الثلاث (المحمَّلة أصلاً في الذاكرة - بلا أي طلب
            // إضافي للخادم) حسب التاريخ، بدل حسب الحملة/المجموعة، لبناء اتجاه يومي.
            // كل صف في البيانات الخام يحمل حقل "date" أصلاً (نفس الحقل المستخدم في
            // فلترة/تجميع الجدول)، فهذا تجميع بديل بمفتاح مختلف فقط.
            function computeDailyBreakdown() {
                const byDate = {};
                const addRows = (list, platformKey) => {
                    scopedList(list).forEach(i => {
                        const d = i.date;
                        if (!d) return;
                        if (!byDate[d]) byDate[d] = { date: d, metaSpend: 0, tiktokSpend: 0, googleSpend: 0, metaConv: 0, tiktokConv: 0, googleConv: 0 };
                        const spend = safeNum(i.spend || i.cost);
                        if (platformKey === 'meta') {
                            byDate[d].metaSpend += spend;
                            byDate[d].metaConv += parseMetaConversions(i);
                        } else if (platformKey === 'tiktok') {
                            byDate[d].tiktokSpend += spend;
                            byDate[d].tiktokConv += safeNum(i.conversions || i.conversion || i.results);
                        } else {
                            byDate[d].googleSpend += spend;
                            byDate[d].googleConv += parseGoogleConversions(i);
                        }
                    });
                };
                addRows(globalData.meta_ads, 'meta');
                addRows(globalData.tiktok_ads, 'tiktok');
                addRows(globalData.google_ads, 'google');

                return Object.values(byDate).sort((a, b) => a.date < b.date ? -1 : 1);
            }

            let dailyTrendChartInstance = null;
            let dailyTrendMetric = 'conv';

            function setDailyMetric(metric, btn) {
                dailyTrendMetric = metric;
                document.querySelectorAll('#daily-trend-section .time-btn').forEach(b => b.classList.remove('active'));
                if (btn) btn.classList.add('active');
                renderDailyTrendChart();
            }

            function renderDailyTrendChart() {
                const days = computeDailyBreakdown();
                const wrapper = document.getElementById('daily-trend-wrapper');
                const empty = document.getElementById('daily-trend-empty');

                if (days.length < 2) {
                    wrapper.style.display = 'none';
                    empty.style.display = 'block';
                    return;
                }
                wrapper.style.display = 'block';
                empty.style.display = 'none';

                const labels = days.map(d => formatDisplayDate(d.date));
                const key = dailyTrendMetric === 'spend'
                    ? { meta: 'metaSpend', tiktok: 'tiktokSpend', google: 'googleSpend', fmt: (v) => v.toFixed(2) + ' ر.س' }
                    : { meta: 'metaConv', tiktok: 'tiktokConv', google: 'googleConv', fmt: (v) => v.toLocaleString('en-US') };

                const ctx = document.getElementById('dailyTrendChart').getContext('2d');
                if (dailyTrendChartInstance) dailyTrendChartInstance.destroy();

                dailyTrendChartInstance = new Chart(ctx, {
                    type: 'bar',
                    data: {
                        labels,
                        datasets: [
                            { label: 'Meta Ads', data: days.map(d => d[key.meta]), backgroundColor: '#0284c7', borderRadius: 4, barPercentage: 0.7, categoryPercentage: 0.7 },
                            { label: 'TikTok Ads', data: days.map(d => d[key.tiktok]), backgroundColor: '#f05a28', borderRadius: 4, barPercentage: 0.7, categoryPercentage: 0.7 },
                            { label: 'Google Ads', data: days.map(d => d[key.google]), backgroundColor: '#0f2540', borderRadius: 4, barPercentage: 0.7, categoryPercentage: 0.7 }
                        ]
                    },
                    options: {
                        responsive: true,
                        maintainAspectRatio: false,
                        plugins: {
                            legend: { position: 'top', labels: { font: { family: 'Cairo', weight: '600' }, boxWidth: 10, usePointStyle: true } },
                            datalabels: {
                                anchor: 'end',
                                align: 'top',
                                color: '#0f2540',
                                font: { weight: 'bold', family: 'Cairo', size: 10 },
                                formatter: (val) => val > 0 ? key.fmt(val) : ''
                            },
                            tooltip: {
                                backgroundColor: '#0f2540',
                                titleFont: { family: 'Cairo', weight: '700' },
                                bodyFont: { family: 'Cairo' },
                                padding: 10,
                                cornerRadius: 8,
                                callbacks: { label: (c) => `${c.dataset.label}: ${key.fmt(c.parsed.y)}` }
                            }
                        },
                        scales: {
                            y: { grid: { color: '#eef1f6' }, ticks: { font: { family: 'Cairo' } }, beginAtZero: true },
                            x: { grid: { display: false }, ticks: { font: { family: 'Cairo', weight: '600' } } }
                        }
                    }
                });
            }

            // يحسب كل مؤشرات الأداء المجمّعة (إنفاق/نتائج/CTR/CPA) من بيانات فترة
            // واحدة {meta_ads, tiktok_ads, google_ads}. دالة نقية بلا أي لمس لـ DOM،
            // تُستخدم مرتين: مرة للفترة الحالية المعروضة، ومرة للفترة السابقة عند
            // حساب شريط المقارنة - لضمان أن كلا الحسابين يستخدمان نفس المنطق تماماً.
            // يجمّع صفوف منصة واحدة حسب account_name (حقل موجود أصلاً في كل صف
            // من Windsor.ai لكل المنصات الثلاث) - يكشف حسابات إعلانية متعددة
            // تحت نفس المنصة (مثال: حساب Meta لإيليفنز وحساب Meta لـLuxlin،
            // أو حساب TikTok للمشغلين وحساب TikTok لإيليفنز).
            function computeAccountBreakdown(list, convFn) {
                const byAccount = {};
                scopedList(list).forEach(i => {
                    const name = i.account_name || 'غير معروف';
                    if (!byAccount[name]) byAccount[name] = { name, spend: 0, conv: 0 };
                    byAccount[name].spend += safeNum(i.spend || i.cost);
                    byAccount[name].conv += convFn(i);
                });
                return Object.values(byAccount).sort((a, b) => b.spend - a.spend);
            }

            // يعرض تفصيل الحسابات فقط عند وجود أكثر من حساب واحد فعلياً لهذه
            // المنصة - لا داعي لتكرار الرقم نفسه إن كان هناك حساب واحد فقط.
            // نفس وضعية العرض الأصلية (الإنفاق بارز كما كان)، مع إضافة عدد
            // النتائج/الرسائل بجانبه فقط.
            // أعلى 3 حملات نشطة عبر كل حسابات ميتا مجتمعة (مرتبة بالإنفاق). تُجمَّع
            // الصفوف بمفتاح (الحساب + الحملة) حتى لا تندمج حملتان بنفس الاسم في حسابين
            // مختلفين. الحالة من campaign_status الفعلي، وإلا تقدير بالإنفاق (نفس منطق الجدول).
            function computeTopActiveCampaigns(list, platformKey, limit) {
                const cfg = platformCfg(platformKey);
                const SEP = '\\u0001';
                const rows = aggregateRows(scopedList(list), i => `${i.account_name || ''}${SEP}${campaignOf(i)}`, cfg, 'campaigns');
                const active = rows.filter(r => r.isActive).sort((a, b) => b.spend - a.spend);
                return {
                    top: active.slice(0, limit).map(r => {
                        const [account, campaign] = r.name.split(SEP);
                        return { ...r, account, campaign };
                    }),
                    totalActive: active.length
                };
            }

            function renderTopCampaigns(elId, result, resultWord) {
                const el = document.getElementById(elId);
                if (!el) return;
                if (!result.top.length) {
                    el.innerHTML = '<div class="account-row"><span class="account-name">لا توجد حملات نشطة خلال هذه الفترة</span></div>';
                    return;
                }
                const more = result.totalActive - result.top.length;
                el.innerHTML = `<div class="accounts-caption">أعلى الحملات النشطة إنفاقاً</div>` + result.top.map(c => `
                    <div class="account-row">
                        <span class="account-name" title="${escapeHtml(c.campaign)}${c.account ? ' — ' + escapeHtml(c.account) : ''}">${escapeHtml(c.campaign)}</span>
                        <span class="account-stats"><b>${c.spend.toFixed(2)} ر.س</b> <span class="account-conv">${c.conv.toLocaleString('en-US')} ${resultWord}</span></span>
                    </div>
                `).join('') + (more > 0 ? `<div class="accounts-caption">+ ${more === 1 ? 'حملة نشطة أخرى' : more === 2 ? 'حملتان نشطتان أخريان' : more + ' حملات نشطة أخرى'}</div>` : '');
            }

            function renderAccountBreakdown(elId, accounts, resultWord) {
                const el = document.getElementById(elId);
                if (!el) return;
                if (accounts.length <= 1) { el.innerHTML = ''; return; }
                el.innerHTML = accounts.map(a => `
                    <div class="account-row">
                        <span class="account-name" title="${escapeHtml(a.name)}">${escapeHtml(a.name)}</span>
                        <span class="account-stats"><b>${a.spend.toFixed(2)} ر.س</b> <span class="account-conv">${a.conv.toLocaleString('en-US')} ${resultWord}</span></span>
                    </div>
                `).join('');
            }


            function computeAggregateMetrics(data) {
                const metaList = scopedList(data.meta_ads);
                const tiktokList = scopedList(data.tiktok_ads);
                const googleList = scopedList(data.google_ads);

                const metaSpend = metaList.reduce((s, i) => s + safeNum(i.spend || i.cost), 0);
                const tiktokSpend = tiktokList.reduce((s, i) => s + safeNum(i.spend || i.cost), 0);
                const googleSpend = googleList.reduce((s, i) => s + safeNum(i.spend || i.cost), 0);

                const metaConv = metaList.reduce((s, i) => s + parseMetaConversions(i), 0);
                const tiktokConv = tiktokList.reduce((s, i) => s + safeNum(i.conversions || i.conversion || i.results), 0);
                const googleConv = googleList.reduce((s, i) => s + parseGoogleConversions(i), 0);

                const metaClicks = metaList.reduce((s, i) => s + safeNum(i.clicks), 0);
                const tiktokClicks = tiktokList.reduce((s, i) => s + safeNum(i.clicks), 0);
                const googleClicks = googleList.reduce((s, i) => s + safeNum(i.clicks), 0);

                const metaImpr = metaList.reduce((s, i) => s + safeNum(i.impressions), 0);
                const tiktokImpr = tiktokList.reduce((s, i) => s + safeNum(i.impressions), 0);
                const googleImpr = googleList.reduce((s, i) => s + safeNum(i.impressions), 0);

                const ctrOf = (clicks, impr) => impr > 0 ? (clicks / impr) * 100 : 0;
                const cpaOf = (spend, conv) => conv > 0 ? spend / conv : null;
                const cpcOf = (spend, clicks) => clicks > 0 ? spend / clicks : 0;

                const metaCtr = ctrOf(metaClicks, metaImpr);
                const tiktokCtr = ctrOf(tiktokClicks, tiktokImpr);
                const googleCtr = ctrOf(googleClicks, googleImpr);

                const metaCpa = cpaOf(metaSpend, metaConv);
                const tiktokCpa = cpaOf(tiktokSpend, tiktokConv);
                const googleCpa = cpaOf(googleSpend, googleConv);

                const totalSpend = metaSpend + tiktokSpend + googleSpend;
                const totalClicks = metaClicks + tiktokClicks + googleClicks;
                const totalImpr = metaImpr + tiktokImpr + googleImpr;
                const totalConv = metaConv + tiktokConv + googleConv;

                return {
                    metaSpend, tiktokSpend, googleSpend, totalSpend,
                    metaConv, tiktokConv, googleConv, totalConv,
                    metaClicks, tiktokClicks, googleClicks, totalClicks,
                    metaImpr, tiktokImpr, googleImpr, totalImpr,
                    metaCtr, tiktokCtr, googleCtr, totalCtr: ctrOf(totalClicks, totalImpr),
                    metaCpa, tiktokCpa, googleCpa,
                    totalCpc: cpcOf(totalSpend, totalClicks)
                };
            }

            // يكتب رقم البطاقة بخط عريض والعملة بخط أصغر رمادي
            function setKpiMoney(id, value) {
                const el = document.getElementById(id);
                if (!el) return;
                const num = Number(value || 0).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
                el.innerHTML = `<span class="kpi-num">${num}</span><span class="kpi-cur">ر.س</span>`;
            }

            function updateDashboardUI() {
                const cur = computeAggregateMetrics(globalData);

                renderTopCampaigns('meta-accounts', computeTopActiveCampaigns(globalData.meta_ads, 'meta', 3), 'رسالة');
                renderAccountBreakdown('tiktok-accounts', computeAccountBreakdown(globalData.tiktok_ads, i => safeNum(i.conversions || i.conversion || i.results)), 'تحويل');
                renderAccountBreakdown('google-accounts', computeAccountBreakdown(globalData.google_ads, parseGoogleConversions), 'إحالة');

                setKpiMoney('meta-spend', cur.metaSpend);
                setKpiMoney('tiktok-spend', cur.tiktokSpend);
                setKpiMoney('google-spend', cur.googleSpend);
                setKpiMoney('total-spend', cur.totalSpend);

                document.getElementById('meta-sub').innerText = `${cur.metaConv.toLocaleString('en-US')} محادثة/نتيجة`;
                document.getElementById('tiktok-sub').innerText = `${cur.tiktokConv.toLocaleString('en-US')} تحويل/نقرة`;
                document.getElementById('google-sub').innerText = `${cur.googleConv.toLocaleString('en-US')} إحالات`;

                document.getElementById('total-cpc').innerText = cur.totalCpc.toFixed(2);
                document.getElementById('total-ctr').innerText = cur.totalCtr.toFixed(1) + '%';
                document.getElementById('meta-ctr').innerText = cur.metaCtr.toFixed(1) + '%';
                document.getElementById('tiktok-ctr').innerText = cur.tiktokCtr.toFixed(1) + '%';
                document.getElementById('google-ctr').innerText = cur.googleCtr.toFixed(1) + '%';
                document.getElementById('meta-cpa').innerText = cur.metaCpa !== null ? cur.metaCpa.toFixed(2) : '--';
                document.getElementById('tiktok-cpa').innerText = cur.tiktokCpa !== null ? cur.tiktokCpa.toFixed(2) : '--';
                document.getElementById('google-cpa').innerText = cur.googleCpa !== null ? cur.googleCpa.toFixed(2) : '--';

                let timeText = buildRangeLabel(dateRangeState.preset, dateRangeState.date_from, dateRangeState.date_to);
                let nowStr = new Date().toLocaleTimeString('en-US');
                document.getElementById('update-time').innerText = `تقرير الأداء: ${timeText} | آخر تحديث: ${nowStr}`;
                document.getElementById('range-caption').innerHTML = `الفترة المعروضة: <b>${timeText}</b>`;

                updateChart(cur.metaSpend, cur.tiktokSpend, cur.googleSpend);
                updateDonut(cur.metaConv, cur.tiktokConv, cur.googleConv);
                renderDailyTrendChart();
                renderExplorer();
                renderDecisionCenter();

                lastMetrics = {
                    timeText, nowStr,
                    metaSpend: cur.metaSpend, tiktokSpend: cur.tiktokSpend, googleSpend: cur.googleSpend, totalSpend: cur.totalSpend,
                    metaConv: cur.metaConv, tiktokConv: cur.tiktokConv, googleConv: cur.googleConv, totalConv: cur.totalConv,
                    metaCtr: cur.metaCtr, tiktokCtr: cur.tiktokCtr, googleCtr: cur.googleCtr,
                    metaCpa: cur.metaCpa, tiktokCpa: cur.tiktokCpa, googleCpa: cur.googleCpa
                };

                // شريط الالسابق: طلب إضافي منفصل، لا يُعطّل عرض
                // الأرقام الحالية (يعمل بالخلفية ويملأ الشارات عند وصول النتيجة)
                updateComparisonPills(cur);
            }

            // يحسب "الفترة السابقة" كنافذة مساوية تماماً في عدد الأيام ومباشرة قبل
            // الفترة الحالية - نفس منهج أدوات التحليل الاحترافية (Google Analytics
            // ونحوها)، ويعمل بشكل صحيح ومنصف أياً كان الفلتر المختار (يوم واحد،
            // أسبوع، شهر كامل، أو نطاق مخصص) دون أي حالات استثنائية أو التباس.
            function computePreviousRange(dateFrom, dateTo) {
                const from = new Date(dateFrom + 'T00:00:00');
                const to = new Date(dateTo + 'T00:00:00');
                const spanDays = Math.round((to - from) / 86400000) + 1;

                const prevTo = new Date(from);
                prevTo.setDate(prevTo.getDate() - 1);
                const prevFrom = new Date(prevTo);
                prevFrom.setDate(prevFrom.getDate() - (spanDays - 1));

                return { from: toIsoDate(prevFrom), to: toIsoDate(prevTo) };
            }

            // يبني شارة مقارنة واحدة (▲/▼ + نسبة مئوية) بحسب المؤشر ومعناه: زيادة
            // الإنفاق ليست بالضرورة "جيدة" فتُلوَّن محايدة (رمادية)، بينما زيادة
            // النتائج جيدة (أخضر) وزيادة التكلفة/نتيجة (CPA) سيئة (أحمر) - أي
            // انخفاضها هو التحسّن المطلوب.
            function buildComparePillHtml(current, previous, opts) {
                opts = opts || {};
                const goodDirection = opts.goodDirection || 'up'; // 'up' | 'down' | 'neutral'
                const labelText = opts.label || 'السابق:';
                if (previous === null || previous === undefined) return '';
                if (previous === 0) {
                    if (current === 0) return '';
                    return `<span class="compare-pill up"><span class="compare-label">${labelText}</span>جديد ✨</span>`;
                }
                const change = ((current - previous) / previous) * 100;
                if (Math.abs(change) < 0.1) {
                    return `<span class="compare-pill flat"><span class="compare-label">${labelText}</span>~ 0.0%</span>`;
                }
                const isIncrease = change > 0;
                const arrow = isIncrease ? '▲' : '▼';
                let cls;
                if (goodDirection === 'neutral') {
                    cls = 'flat';
                } else {
                    const isGood = goodDirection === 'up' ? isIncrease : !isIncrease;
                    cls = isGood ? 'up' : 'down';
                }
                return `<span class="compare-pill ${cls}"><span class="compare-label">${labelText}</span>${arrow} ${Math.abs(change).toFixed(1)}%</span>`;
            }

            async function updateComparisonPills(currentMetrics) {
                const prevRange = computePreviousRange(dateRangeState.date_from, dateRangeState.date_to);
                previousRangeInfo = prevRange;
                try {
                    const res = await fetch(`/api/data?date_from=${prevRange.from}&date_to=${prevRange.to}`);
                    const json = await res.json();
                    if (json.status !== 'success') return;

                    const prev = computeAggregateMetrics(json.data || {});
                    previousMetrics = prev;
                    previousRawData = json.data || {}; // للمقارنة على مستوى الصف الواحد في الجدول

                    // الصرف: رمادي محايد (زيادة الصرف ليست جيدة أو سيئة بذاتها)
                    // النتائج: أخضر عند التحسن وأحمر عند التراجع
                    const pillPairs = [
                        ['total', currentMetrics.totalSpend, prev.totalSpend, currentMetrics.totalConv, prev.totalConv],
                        ['meta', currentMetrics.metaSpend, prev.metaSpend, currentMetrics.metaConv, prev.metaConv],
                        ['tiktok', currentMetrics.tiktokSpend, prev.tiktokSpend, currentMetrics.tiktokConv, prev.tiktokConv],
                        ['google', currentMetrics.googleSpend, prev.googleSpend, currentMetrics.googleConv, prev.googleConv]
                    ];
                    pillPairs.forEach(([key, curSpend, prevSpend, curConv, prevConv]) => {
                        const spendEl = document.getElementById(key + '-compare');
                        const convEl = document.getElementById(key + '-conv-compare');
                        if (spendEl) spendEl.innerHTML = buildComparePillHtml(curSpend, prevSpend, { goodDirection: 'neutral', label: 'الصرف' });
                        if (convEl) convEl.innerHTML = buildComparePillHtml(curConv, prevConv, { goodDirection: 'up', label: 'النتائج' });
                    });

                    // أعد رسم الجدول ومركز القرار الآن بعد وصول بيانات المقارنة، حتى تظهر
                    // أعمدة المقارنة وتلميحات السبب الجذري (تحتاج الفترة السابقة)
                    renderExplorer();
                    renderDecisionCenter();
                } catch (e) {
                    console.error('Error fetching comparison period:', e);
                }
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

            // حقول إضافية (بخلاف الحالة) نلتقط أحدث قيمة لها لكل صف مجمّع - تُستخدم
            // لاحقاً في محرك التقييم الواعي بالهدف (مؤشرات جودة ميتا الأصلية + الهدف).
            const INSIGHT_FIELD_MAP = {
                meta: ['campaign_objective', 'quality_ranking', 'engagement_rate_ranking', 'conversion_rate_ranking'],
                tiktok: ['objective_type'],
                google: []
            };

            function aggregateRows(list, keyFn, cfg, level) {
                let map = {};
                const statusFields = (STATUS_FIELD_MAP[cfg.key] || {})[level] || [];
                const insightFields = INSIGHT_FIELD_MAP[cfg.key] || [];

                list.forEach(i => {
                    let name = keyFn(i);
                    if (!map[name]) map[name] = { name, spend: 0, clicks: 0, impressions: 0, conv: 0, realStatus: null, lastStatusDate: null, insight: {}, lastInsightDate: null };
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

                    // التقط أحدث قيم حقول التقييم (هدف الحملة + مؤشرات جودة ميتا)
                    const rowDate2 = i.date || '';
                    if (!map[name].lastInsightDate || rowDate2 >= map[name].lastInsightDate) {
                        let hasAny = false;
                        insightFields.forEach(f => {
                            if (i[f] !== undefined && i[f] !== null && i[f] !== '') {
                                map[name].insight[f] = i[f];
                                hasAny = true;
                            }
                        });
                        if (hasAny) map[name].lastInsightDate = rowDate2;
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

            // يطابق كل صف حالي بنظيره في الفترة السابقة (بنفس النطاق والمستوى
            // والاسم بالضبط) لحساب نسبة تغيّر الإنفاق والنتائج. عنصر غير موجود
            // في الفترة السابقة (جديد) يُترك بلا نسبة (null) بدل قيمة مضلِّلة.
            function attachComparisonData(rows) {
                if (!previousRawData) {
                    rows.forEach(r => { r.spendChangePct = null; r.convChangePct = null; });
                    return rows;
                }
                const cfg = platformCfg(explorerState.platform);
                let scoped = scopedList(previousRawData[cfg.dataKey]);
                if (explorerState.selectedCampaign) scoped = scoped.filter(i => campaignOf(i) === explorerState.selectedCampaign);
                if (explorerState.level === 'ads' && explorerState.selectedGroup) scoped = scoped.filter(i => groupOf(i) === explorerState.selectedGroup);

                let keyFn = campaignOf;
                if (explorerState.level === 'adsets') keyFn = groupOf;
                else if (explorerState.level === 'ads') keyFn = adOf;

                const prevRows = aggregateRows(scoped, keyFn, cfg, explorerState.level);
                const prevByName = {};
                prevRows.forEach(r => { prevByName[r.name] = r; });

                const pctChange = (cur, prev) => {
                    if (prev === null || prev === undefined) return null;
                    if (prev === 0) return cur === 0 ? null : Infinity;
                    return ((cur - prev) / prev) * 100;
                };

                rows.forEach(r => {
                    const prev = prevByName[r.name];
                    r.spendChangePct = prev ? pctChange(r.spend, prev.spend) : null;
                    r.convChangePct = prev ? pctChange(r.conv, prev.conv) : null;
                });
                return rows;
            }

            // ═══════════════════════════════════════════════════════════════
            // مركز القرار (Decision Center) - محرك تحليلي قائم على قواعد حسابية
            // بحتة على بيانات موجودة فعلاً أو مؤكدة الجلب (لا تخمين ولا استنتاج
            // غير مدعوم). ثلاث وحدات: (1) تقييم واعٍ بالهدف، (2) تلميحات السبب
            // الجذري، (3) رصد هدر الميزانية - تُجمَّع في واجهة تصنيفية واحدة.
            // ═══════════════════════════════════════════════════════════════

            // (1) التقييم الواعي بالهدف: يعتمد على مؤشرات ميتا الأصلية الثلاثة
            // (quality/engagement/conversion_rate_ranking) حيث تتوفر - وهي محسوبة
            // من ميتا نفسها بمقارنة حقيقية بالإعلانات المنافسة على نفس الجمهور.
            // حيث لا تتوفر (TikTok، Google، أو صفوف ميتا بلا بيانات تصنيف كافية)
            // يُستخدم بديل احتياطي: مقارنة الأداء بمتوسط الحساب الفعلي لنفس المنصة
            // والفترة - وليس رقماً عالمياً ثابتاً.
            function normalizeMetaRanking(raw) {
                if (raw === null || raw === undefined || raw === '') return null;
                const s = String(raw).toUpperCase();
                if (s.includes('ABOVE_AVERAGE')) return 'good';
                if (s === 'AVERAGE') return 'average';
                if (s.includes('BELOW_AVERAGE')) return 'poor';
                return null; // UNKNOWN / NO_DATA وما شابه - لا حكم بلا دليل
            }

            function computePerformanceTier(row, cfg, baselineCtr) {
                if (cfg.key === 'meta' && row.insight) {
                    const rankings = ['quality_ranking', 'engagement_rate_ranking', 'conversion_rate_ranking']
                        .map(f => normalizeMetaRanking(row.insight[f]))
                        .filter(v => v !== null);
                    if (rankings.length > 0) {
                        // أسوأ تصنيف من الثلاثة هو الحاكم - أي مؤشر ضعيف واحد يستحق الانتباه
                        if (rankings.includes('poor')) return { tier: 'poor', source: 'meta_native' };
                        if (rankings.includes('average')) return { tier: 'average', source: 'meta_native' };
                        return { tier: 'good', source: 'meta_native' };
                    }
                }
                // حماية إحصائية: لا حكم على عينة ظهور صغيرة جداً (أقل من 200 ظهور)
                if (!baselineCtr || baselineCtr <= 0 || row.impressions < 200) {
                    return { tier: null, source: null };
                }
                const ratio = row.ctr / baselineCtr;
                if (ratio >= 1.2) return { tier: 'good', source: 'account_baseline' };
                if (ratio <= 0.7) return { tier: 'poor', source: 'account_baseline' };
                return { tier: 'average', source: 'account_baseline' };
            }

            // (2) تلميحات السبب الجذري: قواعد حسابية بحتة على بيانات موجودة أصلاً
            // (CTR/CPC/CPA الحالية والسابقة). التصنيف: "مرجّح" لإشارة واضحة مفردة،
            // "غير مؤكد" لتغيّر مركّب في أكثر من مؤشر يصعب عزله أو بيانات غير كافية.
            // ممنوع اختراع سبب غير مدعوم بالأرقام.
            function buildRootCauseHint(cur, prev) {
                if (!prev || cur.cpa === null || prev.cpa === null || prev.cpa === 0) return null;
                const cpaChange = ((cur.cpa - prev.cpa) / prev.cpa) * 100;
                if (Math.abs(cpaChange) < 15) return null; // تغيّر غير ملحوظ، لا داعي لتفسير

                const ctrChange = prev.ctr > 0 ? ((cur.ctr - prev.ctr) / prev.ctr) * 100 : null;
                const curCpc = cur.clicks > 0 ? cur.spend / cur.clicks : null;
                const prevCpc = prev.clicks > 0 ? prev.spend / prev.clicks : null;
                const cpcChange = (curCpc !== null && prevCpc !== null && prevCpc > 0)
                    ? ((curCpc - prevCpc) / prevCpc) * 100 : null;
                const convChange = prev.conv > 0 ? ((cur.conv - prev.conv) / prev.conv) * 100 : null;

                const direction = cpaChange > 0 ? 'ارتفاع' : 'انخفاض';
                const ctrMoved = ctrChange !== null && Math.abs(ctrChange) >= 15;
                const cpcMoved = cpcChange !== null && Math.abs(cpcChange) >= 15;
                let reason, confidence;

                // تحت آلية تسعير شائعة (تكلفة قريبة من عدد مرات الظهور CPM)، انخفاض
                // CTR يرفع CPC تلقائياً كنتيجة حسابية طبيعية (نفس الإنفاق على نقرات
                // أقل) - وليس سبباً ثانياً مستقلاً. لذلك عند تحرّك الاثنين معاً بنمط
                // متوقع (CTR ينخفض وCPC يرتفع، أو العكس)، يبقى CTR هو السبب الجذري
                // المرجّح لأنه الأقرب لبداية القمع. فقط إن تحرّكا بنمط غير متوقع
                // (بنفس الاتجاه معاً) يُصنَّف كغير مؤكد - يشير عادة لتغيّر في
                // استراتيجية المزايدة أو الميزانية نفسها وليس أداء الإعلان.
                const movedInExpectedOppositePattern = ctrMoved && cpcMoved &&
                    Math.sign(ctrChange) !== Math.sign(cpcChange);

                if (ctrMoved && (!cpcMoved || movedInExpectedOppositePattern)) {
                    const cpcNote = cpcMoved ? '، وما تبعه من تغيّر تكلفة النقرة نتيجة طبيعية متوقعة لذلك' : '';
                    reason = `${ctrChange < 0 ? 'انخفاض' : 'ارتفاع'} معدل النقر (CTR) بنسبة ${Math.abs(ctrChange).toFixed(0)}% هو المحرّك الأساسي المرجّح${cpcNote}`;
                    confidence = 'مرجّح';
                } else if (cpcMoved && !ctrMoved) {
                    reason = `${cpcChange > 0 ? 'ارتفاع' : 'انخفاض'} تكلفة النقرة (CPC) بنسبة ${Math.abs(cpcChange).toFixed(0)}% دون تغيّر ملحوظ في CTR - يشير لمنافسة أعلى في المزاد كسبب مرجّح`;
                    confidence = 'مرجّح';
                } else if (!ctrMoved && !cpcMoved && convChange !== null && Math.abs(convChange) >= 15) {
                    reason = 'الوصول للنقرات لم يتغيّر بشكل ملحوظ (CTR وCPC ثابتان تقريباً)، لكن معدل تحويل النقرات لنتائج فعلية تغيّر - يستحق مراجعة الصفحة المقصودة أو استمرارية الاستهداف';
                    confidence = 'مرجّح';
                } else if (ctrMoved && cpcMoved) {
                    reason = 'تغيّر غير معتاد في CTR وCPC معاً (بنفس الاتجاه) - يصعب عزل سبب رئيسي واحد بثقة، وقد يرتبط بتغيّر في استراتيجية المزايدة أو الميزانية نفسها وليس أداء الإعلان';
                    confidence = 'غير مؤكد';
                } else {
                    reason = 'البيانات المتاحة غير كافية لتحديد سبب واضح';
                    confidence = 'غير مؤكد';
                }

                return { direction, cpaChangeAbs: Math.abs(cpaChange), reason, confidence };
            }

            // (3) رصد هدر الميزانية: المعيار نسبي لكل حساب ومنصة وفترة (وليس رقماً
            // عالمياً ثابتاً) - إنفاق أعلى من متوسط العنصر، مع نتائج معدومة أو
            // تكلفة/نتيجة أعلى بكثير من متوسط الحساب نفسه.
            function computeWasteFlag(row, platformAvgSpend, platformAvgCpa) {
                if (!platformAvgSpend || row.spend <= platformAvgSpend) return null;
                if (row.conv === 0 && row.spend > 0) {
                    return `إنفاق ${fmt(row.spend)} ر.س بلا أي نتيجة - أعلى من متوسط إنفاق العنصر بالحساب (${fmt(platformAvgSpend)} ر.س)`;
                }
                if (platformAvgCpa !== null && row.cpa !== null && row.cpa > platformAvgCpa * 1.5) {
                    const pct = ((row.cpa / platformAvgCpa - 1) * 100).toFixed(0);
                    return `تكلفة/نتيجة ${fmt(row.cpa)} ر.س أعلى بـ${pct}% من متوسط الحساب (${fmt(platformAvgCpa)} ر.س)`;
                }
                return null;
            }

            // المنسّق: يجمع إعلانات المنصات الثلاث على مستوى الإعلان (بمعزل عن حالة
            // تصفح الجدول الحالية)، ويصنّفها إلى 4 فئات بناءً على الأدلة أعلاه فقط.
            function computeDecisionCenterData() {
                const categories = { critical: [], opportunity: [], monitor: [], good: [] };
                if (!globalData) return categories;

                PLATFORMS.forEach(cfg => {
                    const fullList = scopedList(globalData[cfg.dataKey]);
                    if (fullList.length === 0) return;

                    const adRows = aggregateRows(fullList, adOf, cfg, 'ads');
                    if (adRows.length === 0) return;

                    const totalSpend = adRows.reduce((s, r) => s + r.spend, 0);
                    const platformAvgSpend = totalSpend / adRows.length;
                    const rowsWithConv = adRows.filter(r => r.conv > 0);
                    const platformAvgCpa = rowsWithConv.length > 0
                        ? rowsWithConv.reduce((s, r) => s + r.cpa, 0) / rowsWithConv.length
                        : null;
                    const rowsWithImpr = adRows.filter(r => r.impressions >= 200);
                    const baselineCtr = rowsWithImpr.length > 0
                        ? rowsWithImpr.reduce((s, r) => s + r.ctr, 0) / rowsWithImpr.length
                        : null;

                    let prevByName = {};
                    if (previousRawData) {
                        const prevList = scopedList(previousRawData[cfg.dataKey]);
                        aggregateRows(prevList, adOf, cfg, 'ads').forEach(r => { prevByName[r.name] = r; });
                    }

                    adRows.forEach(row => {
                        const tierInfo = computePerformanceTier(row, cfg, baselineCtr);
                        const wasteReason = computeWasteFlag(row, platformAvgSpend, platformAvgCpa);
                        const prev = prevByName[row.name];
                        const rootCause = prev ? buildRootCauseHint(row, prev) : null;

                        const item = {
                            name: row.name, platform: cfg.label, platformDot: cfg.dot, platformKey: cfg.key,
                            spend: row.spend, conv: row.conv, cpa: row.cpa, ctr: row.ctr,
                            resultSub: cfg.resultSub, tier: tierInfo.tier, tierSource: tierInfo.source,
                            wasteReason, rootCause
                        };

                        if (wasteReason) {
                            categories.critical.push(item);
                        } else if (rootCause && rootCause.direction === 'ارتفاع' && rootCause.cpaChangeAbs >= 25) {
                            categories.critical.push(item);
                        } else if (tierInfo.tier === 'poor') {
                            categories.critical.push(item);
                        } else if (tierInfo.tier === 'good' && platformAvgCpa !== null && row.cpa !== null && row.cpa < platformAvgCpa * 0.8) {
                            categories.opportunity.push(item);
                        } else if (tierInfo.tier === 'average' || (rootCause && rootCause.confidence === 'غير مؤكد')) {
                            categories.monitor.push(item);
                        } else if (tierInfo.tier === 'good') {
                            categories.good.push(item);
                        }
                        // لا يوجد دليل كافٍ (ظهور قليل جداً أو لا مؤشرات متاحة) → لا يُصنَّف إطلاقاً
                    });
                });

                Object.keys(categories).forEach(k => categories[k].sort((a, b) => b.spend - a.spend));
                return categories;
            }

            function renderDecisionCard(it, category) {
                const cpaText = it.cpa !== null ? fmt(it.cpa) + ' ر.س' : '--';
                let evidenceHtml = '';
                let actionHtml = '';

                if (it.wasteReason) {
                    evidenceHtml = `<div class="dc-evidence">${escapeHtml(it.wasteReason)}</div>`;
                    actionHtml = `<div class="dc-action">💡 أعد تقييم الاستهداف/الإبداعي أو أوقف الإعلان</div>`;
                } else if (it.rootCause) {
                    const confTag = it.rootCause.confidence === 'مرجّح' ? '' : ' <span class="dc-conf-tag">غير مؤكد</span>';
                    evidenceHtml = `<div class="dc-evidence">${it.rootCause.direction} CPA بنسبة ${it.rootCause.cpaChangeAbs.toFixed(0)}% — ${escapeHtml(it.rootCause.reason)}${confTag}</div>`;
                    if (category === 'critical') actionHtml = `<div class="dc-action">💡 راجع المؤشر المذكور أعلاه تحديداً</div>`;
                } else if (it.tier === 'poor') {
                    evidenceHtml = `<div class="dc-evidence">${it.tierSource === 'meta_native' ? 'تصنيف ميتا الأصلي للجودة/التفاعل أقل من المتوسط' : 'CTR أقل من متوسط الحساب لهذه المنصة'}</div>`;
                    actionHtml = `<div class="dc-action">💡 راجع الإبداعي أو الاستهداف</div>`;
                } else if (it.tier === 'good' && category === 'opportunity') {
                    evidenceHtml = `<div class="dc-evidence">${it.tierSource === 'meta_native' ? 'تصنيف ميتا الأصلي أعلى من المتوسط' : 'CTR أعلى من متوسط الحساب'}، بتكلفة/نتيجة أقل من المتوسط</div>`;
                    actionHtml = `<div class="dc-action">💡 مرشح جيد لزيادة الميزانية</div>`;
                } else if (it.tier === 'good') {
                    evidenceHtml = `<div class="dc-evidence">${it.tierSource === 'meta_native' ? 'تصنيف ميتا الأصلي أعلى من المتوسط' : 'CTR أعلى من متوسط الحساب'}</div>`;
                } else if (it.tier === 'average') {
                    evidenceHtml = `<div class="dc-evidence">أداء ضمن المتوسط المعتاد للحساب</div>`;
                }

                return `
                    <div class="dc-card">
                        <div class="dc-card-top">
                            <span class="platform-dot" style="background:${it.platformDot}"></span>
                            <span class="dc-card-name" title="${escapeHtml(it.name)}">${escapeHtml(it.name)}</span>
                        </div>
                        <div class="dc-card-stats">${fmt(it.spend)} ر.س · ${it.conv.toLocaleString('en-US')} ${it.resultSub} · CPA ${cpaText}</div>
                        ${evidenceHtml}
                        ${actionHtml}
                    </div>
                `;
            }

            function renderDecisionCenter() {
                const el = document.getElementById('decision-center-body');
                if (!el) return;
                const data = computeDecisionCenterData();
                const totalItems = data.critical.length + data.opportunity.length + data.monitor.length + data.good.length;

                if (totalItems === 0) {
                    el.innerHTML = '<div class="empty-state">لا توجد بيانات كافية بعد للتصنيف (انتظر وصول بيانات المقارنة، أو حجم ظهور أكبر لبعض الإعلانات)</div>';
                    return;
                }

                const sections = [
                    { key: 'critical', label: 'يحتاج انتباه', icon: '🔴', cls: 'dc-critical' },
                    { key: 'opportunity', label: 'فرص توسع', icon: '💰', cls: 'dc-opportunity' },
                    { key: 'monitor', label: 'راقب', icon: '🟡', cls: 'dc-monitor' },
                    { key: 'good', label: 'أداء جيد', icon: '🟢', cls: 'dc-good' }
                ];

                el.innerHTML = sections.map(sec => {
                    const items = data[sec.key].slice(0, 5);
                    const itemsHtml = items.length > 0
                        ? items.map(it => renderDecisionCard(it, sec.key)).join('')
                        : `<div class="dc-empty">لا يوجد حالياً</div>`;
                    return `
                        <div class="dc-column">
                            <div class="dc-column-header ${sec.cls}">
                                <span>${sec.icon} ${sec.label}</span>
                                <span class="dc-count">${data[sec.key].length}</span>
                            </div>
                            <div class="dc-items">${itemsHtml}</div>
                        </div>
                    `;
                }).join('');
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

                return attachComparisonData(rows);
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

            // ===== تقرير واتساب الاحترافي (زر "نسخ التقرير" فوق الجدول) =====
            // عمق التقرير يتبع التبويب المفتوح: الحملات ← مجموعاتها، المجموعات ← إعلاناتها،
            // الإعلانات ← قائمة الإعلانات. العناصر الصغيرة تُدمج في سطر "أخرى".
            const RPT_NL = String.fromCharCode(10);
            const RPT_SEP = '━━━━━━━━━━━━━━━━';
            const RPT_MIN_SPEND = 10;        // عناصر إنفاقها أقل من هذا تُدمج في "أخرى"
            const RPT_MIN_RESULTS_RANK = 1;  // أقل عدد نتائج ليدخل الإعلان في "أفضل الإعلانات أداءً"
            const RPT_TOP_N = 3;
            const RPT_RESULT_WORD = { meta: 'محادثة', tiktok: 'تحويل', google: 'إحالة' };
            const RPT_RESULT_PLURAL = { meta: 'المحادثات', tiktok: 'التحويلات', google: 'الإحالات' };

            function rptMoney(v) {
                return Number(v || 0).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
            }
            function rptCpa(r) { return (r.cpa !== null && r.cpa !== undefined) ? rptMoney(r.cpa) : '—'; }
            function rptHasActivity(r) { return r.spend > 0 || r.conv > 0; }
            function rptStatus(r) { return r.isActive ? '' : ' (متوقفة)'; }
            function rptSum(list) {
                const t = list.reduce((a, r) => {
                    a.spend += r.spend; a.conv += r.conv; a.clicks += r.clicks; a.impressions += r.impressions;
                    return a;
                }, { spend: 0, conv: 0, clicks: 0, impressions: 0 });
                t.ctr = t.impressions > 0 ? (t.clicks / t.impressions) * 100 : 0;
                t.cpa = t.conv > 0 ? t.spend / t.conv : null;
                return t;
            }
            function rptLine(r, word) {
                return `${rptMoney(r.spend)} ر.س  |  ${r.conv.toLocaleString('en-US')} ${word}  |  ${rptCpa(r)} للنتيجة  |  CTR ${r.ctr.toFixed(1)}%`;
            }
            function rptCompact(r, word) {
                return `${rptMoney(r.spend)}  |  ${r.conv.toLocaleString('en-US')} ${word}  |  ${rptCpa(r)}  |  CTR ${r.ctr.toFixed(1)}%`;
            }
            function rptSplit(list) {
                const active = (list || []).filter(rptHasActivity).sort((a, b) => b.spend - a.spend);
                return {
                    main: active.filter(r => r.spend >= RPT_MIN_SPEND),
                    small: active.filter(r => r.spend < RPT_MIN_SPEND)
                };
            }
            function rptOthersLine(small, word) {
                if (!small.length) return null;
                const t = rptSum(small);
                return `▸ أخرى (${small.length}): ${rptMoney(t.spend)}  |  ${t.conv.toLocaleString('en-US')} ${word}`;
            }
            function rptChildrenBlock(L, children, word) {
                const parts = rptSplit(children);
                parts.main.forEach(ch => {
                    L.push('');
                    L.push(`▸ ${ch.name}${rptStatus(ch)}`);
                    L.push('   ' + rptCompact(ch, word));
                });
                const others = rptOthersLine(parts.small, word);
                if (others) { L.push(''); L.push(others); }
            }

            function buildTableReportText() {
                const cfg = platformCfg(explorerState.platform);
                const word = RPT_RESULT_WORD[cfg.key] || 'نتيجة';
                const tree = buildTreeForReport();
                const rangeLabel = buildRangeLabel(dateRangeState.preset, dateRangeState.date_from, dateRangeState.date_to);
                const levelTitles = { campaigns: 'الحملات الإعلانية', adsets: 'المجموعات الإعلانية', ads: 'الإعلانات' };
                const L = [];

                L.push(`*تقرير ${cfg.label} | ${levelTitles[tree.level]}*`);
                L.push(`الفترة: ${rangeLabel}`);
                if (explorerState.selectedCampaign) L.push(`الحملة: ${explorerState.selectedCampaign}`);
                if (explorerState.selectedGroup) L.push(`المجموعة: ${explorerState.selectedGroup}`);

                const topItems = tree.items.filter(rptHasActivity).sort((a, b) => b.spend - a.spend);
                if (topItems.length === 0) {
                    L.push(RPT_SEP);
                    L.push('لا توجد بيانات مطابقة لهذه الفترة أو الفلتر الحالي.');
                    return L.join(RPT_NL);
                }

                // ---- الملخص التنفيذي ----
                const plural = RPT_RESULT_PLURAL[cfg.key] || 'النتائج';
                const t = rptSum(topItems);
                L.push(RPT_SEP);
                L.push('*الملخص التنفيذي*');
                L.push(`إجمالي الصرف: ${rptMoney(t.spend)} ر.س`);
                L.push(`${plural}: ${t.conv.toLocaleString('en-US')}`);
                L.push(`متوسط تكلفة ال${word}: ${rptCpa(t)} ر.س`);
                L.push(`CTR: ${t.ctr.toFixed(1)}%  |  النقرات: ${t.clicks.toLocaleString('en-US')}`);

                // ---- قرارات سريعة: على مستوى الإعلان دائماً ----
                // نجمع كل الإعلانات تحت النطاق المعروض مع اسم مجموعتها لتمييز الأسماء المتكررة
                let ads = [];
                if (tree.level === 'campaigns') {
                    topItems.forEach(c => (c.adsets || []).forEach(s => (s.ads || []).forEach(a => ads.push(Object.assign({ parent: s.name }, a)))));
                } else if (tree.level === 'adsets') {
                    topItems.forEach(s => (s.ads || []).forEach(a => ads.push(Object.assign({ parent: s.name }, a))));
                } else {
                    ads = topItems.map(a => Object.assign({ parent: null }, a));
                }
                ads = ads.filter(rptHasActivity);
                const label = r => r.parent ? `${r.name} (${r.parent})` : r.name;

                const best = ads.filter(r => r.conv >= RPT_MIN_RESULTS_RANK && r.cpa !== null)
                                .sort((a, b) => a.cpa - b.cpa).slice(0, RPT_TOP_N);
                const wasted = ads.filter(r => r.conv === 0 && r.spend >= RPT_MIN_SPEND)
                                  .sort((a, b) => b.spend - a.spend);

                if (best.length) {
                    L.push(RPT_SEP);
                    L.push('*أفضل الإعلانات أداءً*');
                    best.forEach(r => L.push(`• ${label(r)}: ${rptCpa(r)} ر.س لل${word} (${r.conv.toLocaleString('en-US')} ${word})`));
                }
                if (wasted.length) {
                    L.push(RPT_SEP);
                    L.push('*إعلانات تصرف بدون نتائج*');
                    wasted.forEach(r => L.push(`• ${label(r)}: ${rptMoney(r.spend)} ر.س بدون ${word}`));
                    const w = rptSum(wasted);
                    L.push(`الإجمالي: ${rptMoney(w.spend)} ر.س (${(t.spend > 0 ? (w.spend / t.spend) * 100 : 0).toFixed(0)}% من الصرف)`);
                }

                // ---- التفاصيل ----
                if (tree.level === 'campaigns') {
                    topItems.forEach(c => {
                        L.push(RPT_SEP);
                        L.push(`*${c.name}*${rptStatus(c)}`);
                        L.push(rptLine(c, word));
                        rptChildrenBlock(L, c.adsets, word);
                    });
                } else if (tree.level === 'adsets') {
                    topItems.forEach(s => {
                        L.push(RPT_SEP);
                        L.push(`*${s.name}*${rptStatus(s)}`);
                        L.push(rptLine(s, word));
                        rptChildrenBlock(L, s.ads, word);
                    });
                } else {
                    L.push(RPT_SEP);
                    const parts = rptSplit(topItems);
                    parts.main.forEach((a, idx) => {
                        if (idx > 0) L.push('');
                        L.push(`*${a.name}*${rptStatus(a)}`);
                        L.push(rptCompact(a, word));
                    });
                    const others = rptOthersLine(parts.small, word);
                    if (others) { L.push(''); L.push(others); }
                }

                L.push(RPT_SEP);
                L.push('_Elevenz_');
                return L.join(RPT_NL);
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

            // يبني قائمة الأعمدة المرئية حالياً (ثابتة الترتيب) بحسب columnVisibility
            // - تُستخدم من renderTableHead وrenderTableBody معاً، فيستحيل أن يختلف
            // عدد أعمدة الرأس عن عدد أعمدة الصفوف.
            function getVisibleColumnDefs() {
                const cfg = platformCfg(explorerState.platform);
                return [
                    { key: 'toggle', label: '', sortable: false, always: true },
                    { key: 'name', sortKey: 'name', label: 'الاسم', sortable: true, always: true },
                    { key: 'status', sortKey: 'isActive', label: 'حالة العرض', sortable: true, on: columnVisibility.status },
                    { key: 'conv', sortKey: 'conv', label: cfg.resultLabel, sortable: true, on: columnVisibility.results },
                    { key: 'compareConv', label: 'مقارنة النتائج', sortable: false, on: columnVisibility.compareConv },
                    { key: 'cpa', sortKey: 'cpa', label: 'التكلفة لكل نتيجة', sortable: true, on: columnVisibility.cpa },
                    { key: 'spend', sortKey: 'spend', label: 'المبلغ الذي تم إنفاقه', sortable: true, on: columnVisibility.spend },
                    { key: 'compareSpend', label: 'مقارنة الإنفاق', sortable: false, on: columnVisibility.compareSpend },
                    { key: 'impressions', sortKey: 'impressions', label: 'الظهور', sortable: true, on: columnVisibility.impressions },
                    { key: 'clicks', sortKey: 'clicks', label: 'النقرات', sortable: true, on: columnVisibility.clicks },
                    { key: 'ctr', sortKey: 'ctr', label: 'CTR', sortable: true, on: columnVisibility.ctr }
                ].filter(c => c.always || c.on);
            }

            function renderTableHead() {
                const head = document.getElementById('table-head');
                const cols = getVisibleColumnDefs();
                head.innerHTML = cols.map(c => {
                    if (!c.sortable) return `<th class="no-sort">${c.label}</th>`;
                    const sorted = explorerState.sortKey === c.sortKey ? 'sorted' : '';
                    return `<th class="${sorted}" onclick="sortBy('${c.sortKey}')">${c.label} <span class="sort-ico">${sortIco(c.sortKey)}</span></th>`;
                }).join('');
            }

            // يبني خلية <td> واحدة لعمود مقارنة (إنفاق أو نتائج) بحسب نسبة التغيّر
            // المحسوبة مسبقاً في attachComparisonData. عنصر جديد كلياً (غير موجود
            // بالفترة السابقة) يُميَّز بشارة "جديد"، وعنصر بلا بيانات مقارنة (فترة
            // سابقة لم تصل بعد) يُعرض بـ "--" بدل رقم مضلِّل.
            function compareCellHtml(changePct, mode) {
                if (changePct === null || changePct === undefined) {
                    return `<td><span style="color:var(--text-faint); font-size:12px;">--</span></td>`;
                }
                if (changePct === Infinity) {
                    return `<td><span class="compare-pill up" style="font-size:11px;">جديد ✨</span></td>`;
                }
                if (Math.abs(changePct) < 0.1) {
                    return `<td><span class="compare-pill flat" style="font-size:11px;">~ 0.0%</span></td>`;
                }
                const isIncrease = changePct >= 0;
                const arrow = isIncrease ? '▲' : '▼';
                const cls = mode === 'neutral' ? 'flat' : (isIncrease ? 'up' : 'down');
                return `<td><span class="compare-pill ${cls}" style="font-size:11px;">${arrow} ${Math.abs(changePct).toFixed(1)}%</span></td>`;
            }

            function renderTableBody() {
                const cfg = platformCfg(explorerState.platform);
                const rows = getExplorerRows();
                const body = document.getElementById('table-body');
                const isLeaf = explorerState.level === 'ads';
                const cols = getVisibleColumnDefs();

                if (rows.length === 0) {
                    body.innerHTML = `<tr><td colspan="${cols.length}"><div class="empty-state">لا توجد بيانات مطابقة لهذه الفترة أو الفلتر الحالي</div></td></tr>`;
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

                    const cellHtml = {
                        toggle: `<td>
                                <label class="toggle-switch" title="${statusTitle}">
                                    <input type="checkbox" ${r.isActive ? 'checked' : ''} disabled>
                                    <span class="toggle-slider"></span>
                                </label>
                            </td>`,
                        name: `<td>
                                <div class="name-cell">
                                    <span class="name-icon">${icon}</span>
                                    <span>${escapeHtml(r.name)}</span>
                                    ${badge}
                                    ${!isLeaf ? '<span class="drill-arrow">‹</span>' : ''}
                                </div>
                            </td>`,
                        status: `<td>
                                <span class="status-pill ${r.isActive ? 'status-active' : 'status-paused'}" title="${statusTitle}">
                                    <span class="status-dot"></span>${r.isActive ? 'نشطة' : 'متوقفة'}${statusSuffix}
                                </span>
                            </td>`,
                        conv: `<td>
                                <div class="metric-main">${r.conv.toLocaleString('en-US')}</div>
                                <div class="metric-sub">${cfg.resultSub}</div>
                            </td>`,
                        compareConv: compareCellHtml(r.convChangePct, 'good'),
                        cpa: `<td>${cpaText}</td>`,
                        spend: `<td>${r.spend.toFixed(2)} ر.س</td>`,
                        compareSpend: compareCellHtml(r.spendChangePct, 'neutral'),
                        impressions: `<td>${r.impressions.toLocaleString('en-US')}</td>`,
                        clicks: `<td>${r.clicks.toLocaleString('en-US')}</td>`,
                        ctr: `<td>${r.ctr.toFixed(1)}%</td>`
                    };

                    return `<tr class="${rowClass}" ${drillAttr}>${cols.map(c => cellHtml[c.key]).join('')}</tr>`;
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

            // ===== الضغط على شعار elevenz: رجوع للرئيسية + أعلى الصفحة + تحديث =====
            // نبقي فلتر التاريخ والمنصة المختارة كما هي، ونرجع الجدول لمستوى الحملات.
            async function goHome(evt) {
                if (evt) evt.preventDefault();
                closeSidebarIfMobile();
                closeDateDropdown();
                closeColumnsDropdown();

                explorerState.level = 'campaigns';
                explorerState.selectedCampaign = null;
                explorerState.selectedGroup = null;
                explorerState.search = '';
                const searchEl = document.getElementById('search-input');
                if (searchEl) searchEl.value = '';
                renderExplorer();

                const reduceMotion = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
                const behavior = reduceMotion ? 'auto' : 'smooth';
                const main = document.querySelector('.main-content');
                if (main) main.scrollTo({ top: 0, behavior });   // سطح المكتب: المحتوى هو اللي يسكرول
                window.scrollTo({ top: 0, behavior });           // الجوال: الصفحة نفسها هي اللي تسكرول

                await refreshData();
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

            // يبني كتلة الأرقام المشتركة (الفترة + الإجماليات + تفاصيل كل منصة مع
            // مقارنة الفترة السابقة إن توفّرت) - مستخدَمة في كلا زري النسخ، بحيث
            // يبقى الفرق بينهما في "الإطار" فقط (تعليمات AI أو بدونها) لا في الأرقام.
            function buildReportDataBlock() {
                const m = lastMetrics;
                const p = previousMetrics; // قد تكون لا تزال قيد التحميل (null) عند نقرة سريعة - نتعامل مع ذلك بأمان

                const pctChange = (cur, prev) => {
                    if (prev === null || prev === undefined) return null;
                    if (prev === 0) return cur === 0 ? null : Infinity;
                    return ((cur - prev) / prev) * 100;
                };
                const changeTag = (cur, prev) => {
                    const c = pctChange(cur, prev);
                    if (c === null) return '';
                    if (c === Infinity) return ' (جديد ✨)';
                    const arrow = c >= 0 ? '▲' : '▼';
                    return ` (${arrow}${Math.abs(c).toFixed(1)}%)`;
                };

                const prevRangeText = previousRangeInfo
                    ? `${formatDisplayDate(previousRangeInfo.from)} ← ${formatDisplayDate(previousRangeInfo.to)}`
                    : '';

                const comparisonHeader = p
                    ? `📅 *الفترة الحالية:* ${m.timeText}\\n📅 *الفترة السابقة للمقارنة:* ${prevRangeText}\\n`
                    : `📅 *الفترة:* ${m.timeText}\\n`;

                const totalConvChange = p ? changeTag(m.totalConv, p.totalConv) : '';
                const totalSpendChange = p ? changeTag(m.totalSpend, p.totalSpend) : '';
                const metaSpendChange = p ? changeTag(m.metaSpend, p.metaSpend) : '';
                const metaConvChange = p ? changeTag(m.metaConv, p.metaConv) : '';
                const tiktokSpendChange = p ? changeTag(m.tiktokSpend, p.tiktokSpend) : '';
                const tiktokConvChange = p ? changeTag(m.tiktokConv, p.tiktokConv) : '';
                const googleSpendChange = p ? changeTag(m.googleSpend, p.googleSpend) : '';
                const googleConvChange = p ? changeTag(m.googleConv, p.googleConv) : '';

                const prevLine = (label, cur, prev) =>
                    p ? `   (الفترة السابقة → ${label}: ${prev})` : '';

                return (
`${comparisonHeader}⏱️ وقت إنشاء التقرير: ${m.nowStr}

📊 *الأرقام الإجمالية*
💰 الإنفاق الكلي: ${fmt(m.totalSpend)} ر.س${totalSpendChange}${p ? `\\n${prevLine('الإنفاق', m.totalSpend, fmt(p.totalSpend) + ' ر.س')}` : ''}
🎯 إجمالي النتائج: ${m.totalConv.toLocaleString('en-US')}${totalConvChange}${p ? `\\n${prevLine('النتائج', m.totalConv, p.totalConv.toLocaleString('en-US'))}` : ''}

📘 *Meta Ads*
• الإنفاق: ${fmt(m.metaSpend)} ر.س${metaSpendChange}
• النتائج: ${m.metaConv.toLocaleString('en-US')}${metaConvChange}
• CTR: ${m.metaCtr.toFixed(1)}%
• تكلفة/نتيجة: ${m.metaCpa !== null ? fmt(m.metaCpa) + ' ر.س' : '--'}
${p ? `  (الفترة السابقة → الإنفاق: ${fmt(p.metaSpend)} ر.س | النتائج: ${p.metaConv.toLocaleString('en-US')} | CTR: ${p.metaCtr.toFixed(1)}% | تكلفة/نتيجة: ${p.metaCpa !== null ? fmt(p.metaCpa) + ' ر.س' : '--'})` : ''}

🎵 *TikTok Ads*
• الإنفاق: ${fmt(m.tiktokSpend)} ر.س${tiktokSpendChange}
• التحويلات: ${m.tiktokConv.toLocaleString('en-US')}${tiktokConvChange}
• CTR: ${m.tiktokCtr.toFixed(1)}%
• تكلفة/نتيجة: ${m.tiktokCpa !== null ? fmt(m.tiktokCpa) + ' ر.س' : '--'}
${p ? `  (الفترة السابقة → الإنفاق: ${fmt(p.tiktokSpend)} ر.س | التحويلات: ${p.tiktokConv.toLocaleString('en-US')} | CTR: ${p.tiktokCtr.toFixed(1)}% | تكلفة/نتيجة: ${p.tiktokCpa !== null ? fmt(p.tiktokCpa) + ' ر.س' : '--'})` : ''}

🔍 *Google Ads*
• الإنفاق: ${fmt(m.googleSpend)} ر.س${googleSpendChange}
• الإحالات: ${m.googleConv.toLocaleString('en-US')}${googleConvChange}
• CTR: ${m.googleCtr.toFixed(1)}%
• تكلفة/نتيجة: ${m.googleCpa !== null ? fmt(m.googleCpa) + ' ر.س' : '--'}
${p ? `  (الفترة السابقة → الإنفاق: ${fmt(p.googleSpend)} ر.س | الإحالات: ${p.googleConv.toLocaleString('en-US')} | CTR: ${p.googleCtr.toFixed(1)}% | تكلفة/نتيجة: ${p.googleCpa !== null ? fmt(p.googleCpa) + ' ر.س' : '--'})` : ''}`
                );
            }

            function doCopyText(text, toastMsg) {
                const finish = () => showToast(toastMsg);
                if (navigator.clipboard && navigator.clipboard.writeText) {
                    navigator.clipboard.writeText(text).then(finish).catch(() => fallbackCopy(text, finish));
                } else {
                    fallbackCopy(text, finish);
                }
            }

            // زر "نسخ": ملخص نظيف بلا أي إطار أو تعليمات - جاهز للمشاركة المباشرة
            // (واتساب، إيميل، أو لصقه لمديرك) دون أي نص إضافي غير مرغوب.
            function copyReportPlain() {
                if (!lastMetrics) return;
                const text =
`📊 *تقرير أداء الإعلانات | Elevenz*
${buildReportDataBlock()}

_تم إعداد هذا التقرير آلياً عبر منصة Elevenz_`;
                doCopyText(text, 'تم نسخ التقرير بنجاح! 📋');
            }

            // زر "نسخ للتحليل": نفس الأرقام بالضبط، لكن ضمن Prompt جاهز يوجّه أي
            // نموذج ذكاء اصطناعي (Claude/ChatGPT) لتحليلها وإعطاء توصيات فوراً
            // بمجرد اللصق - دون أن يحتاج المستخدم كتابة أي طلب إضافي بنفسه.
            function copyReportForAnalysis() {
                if (!lastMetrics) return;
                const p = previousMetrics;
                const text =
`أنت خبير تسويق رقمي ومحلل أداء إعلانات محترف (Performance Marketing Specialist).
حلّل بيانات أداء الحملات الإعلانية التالية وقدّم رؤى وتوصيات عملية:

${buildReportDataBlock()}

---
🎯 *المطلوب منك في 4 نقاط سريعة ومباشرة:*
1. ملخص الأداء العام${p ? ' والمقارنة بين الفترتين' : ''}.
2. أفضل منصة أداءً، والمنصة الأكثر استهلاكاً للميزانية دون نتائج كافية.
3. السبب الفني المتوقع لأي ارتفاع أو انخفاض ملحوظ في CPA أو CTR.
4. 3 توصيات عملية فورية لتعديل الميزانيات أو الإعلانات وتحسين التكلفة لكل نتيجة.

_تم إعداد هذا التقرير آلياً عبر منصة Elevenz_`;
                doCopyText(text, 'تم نسخ التقرير التحليلي! الصقه في أي ذكاء اصطناعي لتحليل فوري 🤖');
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
