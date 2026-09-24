# -*- coding: utf-8 -*-
"""
elevenz — سكربت تطبيق التعديلات على main.py

التعديلات:
  1) شعار elevenz (في القائمة الجانبية + شريط الجوال) يصبح زراً:
     يطلع لأعلى الصفحة + يرجع الجدول لمستوى الحملات + يحدّث البيانات.
  2) إعادة تصميم تقرير "نسخ التقرير" (فوق الجدول) بشكل احترافي بدون إيموجي.

الاستخدام:
    python apply_elevenz_patch.py main.py

- يأخذ نسخة احتياطية تلقائياً: main.py.bak
- يرفض التطبيق مرتين على نفس الملف.
- بعد التطبيق يتحقق: (أ) الملف بايثون سليم، (ب) الـ JavaScript الفعلي الذي
  سيصل للمتصفح (مستخرج عبر ast وليس قراءة نصية) سليم عبر node --check إن توفر node.
"""
import ast
import os
import shutil
import subprocess
import sys
import tempfile

# ---------------------------------------------------------------------------
# 1) CSS: ستايل زر الشعار
# ---------------------------------------------------------------------------
CSS_ANCHOR = '.sidebar-logo span { color: var(--accent-orange); }'
CSS_ADD = r'''
            .brand-home {
                display: block; width: fit-content; cursor: pointer;
                text-decoration: none; border-radius: 10px;
                transition: opacity 0.15s ease, transform 0.15s ease;
            }
            .brand-home:hover { opacity: 0.85; }
            .brand-home:active { transform: scale(0.97); }'''

# ---------------------------------------------------------------------------
# 2) HTML: لف الشعارين بزر
# ---------------------------------------------------------------------------
SIDEBAR_OPEN_OLD = '<div class="sidebar-logo"><img src="data:image/png;base64,'
SIDEBAR_OPEN_NEW = ('<div class="sidebar-logo"><a href="/" class="brand-home" id="brandLogo" '
                    'onclick="goHome(event)" title="الرئيسية" aria-label="الرجوع لأعلى الصفحة وتحديث البيانات">'
                    '<img src="data:image/png;base64,')
SIDEBAR_CLOSE_OLD = 'alt="elevenz" style="max-width:150px; height:auto; display:block;"></div>'
SIDEBAR_CLOSE_NEW = 'alt="elevenz" style="max-width:150px; height:auto; display:block;"></a></div>'

MOBILE_OLD = '<div class="mobile-topbar-logo">eleven<span>z</span></div>'
MOBILE_NEW = ('<a href="/" class="mobile-topbar-logo brand-home" onclick="goHome(event)" '
              'aria-label="الرجوع لأعلى الصفحة وتحديث البيانات">eleven<span>z</span></a>')

# ---------------------------------------------------------------------------
# 3) JS: دالة goHome (تُضاف قبل notImplementedYet)
# ---------------------------------------------------------------------------
GOHOME_ANCHOR = 'function notImplementedYet() {'
GOHOME_JS = r'''            // ===== الضغط على شعار elevenz: رجوع للرئيسية + أعلى الصفحة + تحديث =====
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

'''

# ---------------------------------------------------------------------------
# 4) JS: التقرير الجديد (يستبدل formatMetricsLine + buildTableReportText)
# ملاحظة: لا يوجد '\n' في هذا الكود إطلاقاً - نستخدم RPT_NL لتفادي مشكلة
# التحويل داخل نص بايثون الثلاثي.
# ---------------------------------------------------------------------------
REPORT_START_MARKER = '// يصيغ سطر مؤشرات موحّد لعنصر واحد'
REPORT_END_MARKER = 'function copyTableReport() {'
REPORT_JS = r'''            // ===== تقرير واتساب الاحترافي (زر "نسخ التقرير" فوق الجدول) =====
            // عمق التقرير يتبع التبويب المفتوح: الحملات ← مجموعاتها، المجموعات ← إعلاناتها،
            // الإعلانات ← قائمة الإعلانات. العناصر الصغيرة تُدمج في سطر "أخرى".
            const RPT_NL = String.fromCharCode(10);
            const RPT_SEP = '━━━━━━━━━━━━━━━━';
            const RPT_MIN_SPEND = 10;        // عناصر إنفاقها أقل من هذا تُدمج في "أخرى"
            const RPT_MIN_RESULTS_RANK = 5;  // أقل عدد نتائج ليدخل العنصر في ترتيب التكلفة
            const RPT_TOP_N = 3;
            const RPT_RESULT_WORD = { meta: 'محادثة', tiktok: 'تحويل', google: 'إحالة' };

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

                // ---- الملخص ----
                const t = rptSum(topItems);
                L.push(RPT_SEP);
                L.push('*الملخص*');
                L.push(`الإنفاق: ${rptMoney(t.spend)} ر.س`);
                L.push(`النتائج: ${t.conv.toLocaleString('en-US')} (${cfg.resultSub})`);
                L.push(`تكلفة النتيجة: ${rptCpa(t)} ر.س`);
                L.push(`CTR: ${t.ctr.toFixed(1)}%  |  النقرات: ${t.clicks.toLocaleString('en-US')}`);

                // ---- أبرز الملاحظات (على المستوى الأعمق المعروض) ----
                let pool = [];
                if (tree.level === 'campaigns') topItems.forEach(c => (c.adsets || []).forEach(s => pool.push(s)));
                else if (tree.level === 'adsets') topItems.forEach(s => (s.ads || []).forEach(a => pool.push(a)));
                else pool = topItems.slice();
                pool = pool.filter(rptHasActivity);

                const ranked = pool.filter(r => r.conv >= RPT_MIN_RESULTS_RANK && r.cpa !== null).sort((a, b) => a.cpa - b.cpa);
                const best = ranked.slice(0, RPT_TOP_N);
                const worst = ranked.slice(-RPT_TOP_N).reverse().filter(r => best.indexOf(r) === -1);
                const wasted = pool.filter(r => r.conv === 0 && r.spend >= RPT_MIN_SPEND)
                                   .sort((a, b) => b.spend - a.spend).slice(0, RPT_TOP_N);

                if (best.length || worst.length || wasted.length) {
                    L.push(RPT_SEP);
                    L.push('*أبرز الملاحظات*');
                    if (best.length) {
                        L.push('الأقل تكلفة للنتيجة:');
                        best.forEach(r => L.push(`• ${r.name} — ${rptCpa(r)} ر.س (${r.conv.toLocaleString('en-US')} ${word})`));
                    }
                    if (worst.length) {
                        L.push('الأعلى تكلفة للنتيجة:');
                        worst.forEach(r => L.push(`• ${r.name} — ${rptCpa(r)} ر.س (${r.conv.toLocaleString('en-US')} ${word})`));
                    }
                    if (wasted.length) {
                        L.push('إنفاق بدون نتائج:');
                        wasted.forEach(r => L.push(`• ${r.name} — ${rptMoney(r.spend)} ر.س`));
                    }
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
                L.push('_elevenz_');
                return L.join(RPT_NL);
            }

'''


def line_start(text, idx):
    return text.rfind('\n', 0, idx) + 1


def replace_once(text, old, new, label):
    n = text.count(old)
    if n != 1:
        sys.exit(f'✗ [{label}] النص المرجعي موجود {n} مرة (المطلوب مرة واحدة). لم يتم تعديل أي شيء.')
    return text.replace(old, new)


def extract_index_js(source):
    """يستخرج نص html_content كما سيكون وقت التشغيل (عبر ast) ثم الـ <script> المضمن."""
    tree = ast.parse(source)
    html = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(getattr(t, 'id', None) == 'html_content' for t in node.targets):
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                html = node.value.value
    if html is None:
        return None
    marker = '<script>'
    start = html.rfind(marker)
    end = html.rfind('</script>')
    if start == -1 or end == -1:
        return None
    return html[start + len(marker):end]


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else 'main.py'
    if not os.path.exists(path):
        sys.exit(f'✗ الملف غير موجود: {path}')

    with open(path, 'r', encoding='utf-8') as f:
        src = f.read()

    if 'function goHome(' in src or 'RPT_NL' in src:
        sys.exit('✗ يبدو أن التعديلات مطبقة مسبقاً على هذا الملف. لم يتم تعديل أي شيء.')

    # 1) CSS
    src = replace_once(src, CSS_ANCHOR, CSS_ANCHOR + CSS_ADD, 'CSS الشعار')
    # 2) HTML
    src = replace_once(src, SIDEBAR_OPEN_OLD, SIDEBAR_OPEN_NEW, 'فتح رابط شعار القائمة')
    src = replace_once(src, SIDEBAR_CLOSE_OLD, SIDEBAR_CLOSE_NEW, 'إغلاق رابط شعار القائمة')
    src = replace_once(src, MOBILE_OLD, MOBILE_NEW, 'شعار الجوال')
    # 3) goHome
    if src.count(GOHOME_ANCHOR) != 1:
        sys.exit('✗ [goHome] لم أجد function notImplementedYet() مرة واحدة. لم يتم تعديل أي شيء.')
    gi = line_start(src, src.index(GOHOME_ANCHOR))
    src = src[:gi] + GOHOME_JS + src[gi:]
    # 4) التقرير
    if src.count(REPORT_START_MARKER) != 1 or src.count(REPORT_END_MARKER) != 1:
        sys.exit('✗ [التقرير] لم أجد حدود دالة التقرير القديمة. لم يتم تعديل أي شيء.')
    rs = line_start(src, src.index(REPORT_START_MARKER))
    re_ = line_start(src, src.index(REPORT_END_MARKER))
    if re_ <= rs:
        sys.exit('✗ [التقرير] ترتيب الحدود غير متوقع. لم يتم تعديل أي شيء.')
    src = src[:rs] + REPORT_JS + src[re_:]

    # ---- التحقق قبل الكتابة ----
    try:
        compile(src, path, 'exec')
    except SyntaxError as e:
        sys.exit(f'✗ خطأ بايثون بعد التعديل: {e}. لم يتم تعديل أي شيء.')

    js = extract_index_js(src)
    if js is None:
        sys.exit('✗ لم أستطع استخراج JavaScript الصفحة للتحقق. لم يتم تعديل أي شيء.')
    node_ok = None
    if shutil.which('node'):
        with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False, encoding='utf-8') as tf:
            tf.write(js)
            tmp = tf.name
        res = subprocess.run(['node', '--check', tmp], capture_output=True, text=True)
        os.unlink(tmp)
        if res.returncode != 0:
            sys.exit('✗ خطأ JavaScript بعد التعديل:\n' + res.stderr + '\nلم يتم تعديل أي شيء.')
        node_ok = True

    shutil.copyfile(path, path + '.bak')
    with open(path, 'w', encoding='utf-8') as f:
        f.write(src)

    print('✓ تم تطبيق التعديلات على', path)
    print('✓ نسخة احتياطية:', path + '.bak')
    print('✓ بايثون سليم')
    print('✓ JavaScript سليم (node --check)' if node_ok else '• node غير مثبت - تم تخطي فحص JavaScript')


if __name__ == '__main__':
    main()
