:root {
  /* ألوان الصفحة والقائمة */
  --bg-main: #f4f6f9;
  --bg-card: #ffffff;
  --sidebar-bg: #031b33;
  --text-primary: #031b33;
  --text-muted: #64748b;
  --border-color: #e2e8f0;
  
  /* ألوان المراحل والبادجات المتغيرة */
  /* الأخضر (مكتمل / خيارات نجاح) */
  --stage-green-bg: #f0fdf4;
  --stage-green-border: #bbf7d0;
  --stage-green-text: #166534;

  /* الأزرق (مناقشة / اتصال) */
  --stage-blue-bg: #f0f9ff;
  --stage-blue-border: #bae6fd;
  --stage-blue-text: #0369a1;

  /* البنفسجي (عرض السعر / زيارة) */
  --stage-purple-bg: #faf5ff;
  --stage-purple-border: #f5d0fe;
  --stage-purple-text: #86198f;

  /* البرتقالي/الذهبي (متابعة / دفعة أولى) */
  --stage-orange-bg: #fffbeb;
  --stage-orange-border: #fde68a;
  --stage-orange-text: #b45309;

  /* الأحمر (مؤرشف / مرفوض) */
  --stage-red-bg: #fef2f2;
  --stage-red-border: #fecaca;
  --stage-red-text: #991b1b;
}

/* تنسيق بطاقة المرحلة المتغيرة الألوان */
.stage-card {
  border-radius: 10px;
  padding: 12px 16px;
  border: 1px solid var(--border-color);
  background-color: var(--bg-card);
  transition: all 0.2s ease-in-out;
}

/* أمثلة لكلاسات الحالات */
.stage-card.green {
  background-color: var(--stage-green-bg);
  border-color: var(--stage-green-border);
  color: var(--stage-green-text);
}

.stage-card.blue {
  background-color: var(--stage-blue-bg);
  border-color: var(--stage-blue-border);
  color: var(--stage-blue-text);
}

.stage-card.purple {
  background-color: var(--stage-purple-bg);
  border-color: var(--stage-purple-border);
  color: var(--stage-purple-text);
}

.stage-card.orange {
  background-color: var(--stage-orange-bg);
  border-color: var(--stage-orange-border);
  color: var(--stage-orange-text);
}

.stage-card.red {
  background-color: var(--stage-red-bg);
  border-color: var(--stage-red-border);
  color: var(--stage-red-text);
}

/* البادج الصغير مع الأرقام */
.badge-count {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 12px;
  font-size: 12px;
  font-weight: bold;
  background-color: rgba(255, 255, 255, 0.8);
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.05);
}
