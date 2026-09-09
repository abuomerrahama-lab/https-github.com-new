const { Client, LocalAuth } = require('whatsapp-web.js');
const qrcode = require('qrcode-terminal');
const express = require('express');
const cron = require('node-cron');

const app = express();
const port = process.env.PORT || 3000;

const client = new Client({
    authStrategy: new LocalAuth(),
    puppeteer: {
        executablePath: '/usr/bin/google-chrome-stable',
        args: [
            '--no-sandbox',
            '--disable-setuid-sandbox',
            '--disable-dev-shm-usage',
            '--disable-accelerated-2d-canvas',
            '--no-first-run',
            '--no-zygote',
            '--single-process',
            '--disable-gpu'
        ]
    }
});

client.on('qr', (qr) => {
    console.log('--- QR CODE READY ---');
    qrcode.generate(qr, {small: true});
});

client.on('ready', () => {
    console.log('تم الربط بواتساب بنجاح 100%!');
    
    cron.schedule('0 9 * * 0-4', () => {
        const myNumber = '966556225251@c.us'; // ضع رقمك هنا مع رمز الدولة بدون +
        const message = `🔔 *صباح الخير أبا بكر!*

مباشرة لمراجعة أداء إعلاناتك اليوم عبر اللوحة:
https://https-github-com-new-6bbl.onrender.com

📅 *جدول مهامك اليومية الموزعة:*
• 09:00 AM: مراجعة أداء الحملات وتعديل الميزانيات
• 09:40 AM: التواصل مع عملاء Meta ومتابعة النتائج
• 10:20 AM: التواصل مع عملاء TikTok ومتابعة الأداء
• 11:00 AM: كتابة وتجهيز أفكار المحتوى والسكريبتات
• 11:40 AM: متابعة إنتاج وصناعة المحتوى
• 12:20 PM: استراحة وتدقيق الجودة للمحتوى
• 01:00 PM: رفع وتجهيز الحملات الجديدة
• 01:40 PM: فحص وتحسين الحملات (Optimizations)
• 02:20 PM: إعداد تقارير الأداء وتدوين الملاحظات`;

        client.sendMessage(myNumber, message);
    });
});

client.initialize();

app.get('/', (req, res) => res.send('WhatsApp Bot is Online!'));
app.listen(port, () => console.log(`Server running on port ${port}`));
