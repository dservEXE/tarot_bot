const fs = require('fs');
const path = require('path');
const { chromium } = require('C:/Users/AKIM/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const data = JSON.parse(fs.readFileSync(path.join(__dirname, 'interface-preview.json'), 'utf8'));
const escape = s => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
const keyboard = rows => rows.map(row => `<div class="row">${row.map(b => `<div class="button ${b.style || ''}">${escape(b.text)}</div>`).join('')}</div>`).join('');
const html = `<!doctype html><html lang="ru"><meta charset="utf-8"><style>
*{box-sizing:border-box}body{margin:0;background:#eff1f6;color:#1e2331;font-family:'Segoe UI',sans-serif;padding:34px 28px}
h1{font-size:30px;margin:0 0 7px}header p{margin:0 0 24px;color:#60697c;font-size:16px}.cards{display:flex;gap:22px}.label{font-size:18px;font-weight:600;margin:0 0 12px 8px}
.phone{width:382px;height:960px;border:7px solid #242735;border-radius:34px;overflow:hidden;display:flex;flex-direction:column;background:#151720;color:#f5f5fa;box-shadow:0 12px 35px #17233a18}
.bar{padding:17px 17px 14px;background:#21232e;font-size:17px;font-weight:600;border-bottom:1px solid #343640}.sub{font-size:12px;font-weight:400;color:#aeb1c1;margin:3px 0 0 25px}
.chat{padding:13px 10px;flex:1}.bubble{background:#292c3a;padding:14px;border-radius:16px 16px 16px 5px;font-size:15px;line-height:1.45}.bubble b{font-weight:650;color:#fff}
.keys{margin-top:7px}.row{display:flex;gap:5px;margin:5px 0}.button{flex:1;min-width:0;text-align:center;background:#303340;border-radius:8px;padding:11px 5px;font-size:14px;line-height:1.28;color:white;display:flex;align-items:center;justify-content:center;font-weight:500}
.primary{background:#3a73c2}.success{background:#279d46}.danger{background:#d95356}.entry{color:#9da3b9;background:#22242f;padding:12px 14px;font-size:14px}.reply{padding:6px 8px 10px;background:#1b1e29}.reply .button{padding:13px 5px;font-size:15px}.footnote{font-size:14px;color:#677086;margin-top:20px}
</style><header><h1>ТарологИИя · новый интерфейс</h1><p>Синий — личные материалы. Зелёный — пополнение. Остальное — спокойная навигация.</p></header><div class="cards">${data.cards.map(c => `<section><div class="label">${c.title}</div><div class="phone"><div class="bar">‹ &nbsp; 🔮 ТарологИИя<div class="sub">бот · персональные расклады</div></div><div class="chat"><div class="bubble">${c.text.replace(/\n/g,'<br>')}</div><div class="keys">${keyboard(c.keyboard.inline_keyboard)}</div></div><div class="entry">Сообщение…</div><div class="reply">${keyboard(data.reply.keyboard)}</div></div></section>`).join('')}</div><div class="footnote">Предпросмотр на основе текстов и кнопок бота. Точные оттенки и размеры зависят от темы и версии Telegram.</div></html>`;
fs.writeFileSync(path.join(__dirname, 'interface-preview.html'), html);
(async () => {
 const browser = await chromium.launch({executablePath:'C:/Program Files/BraveSoftware/Brave-Browser/Application/brave.exe',headless:true});
 try {
  const page = await browser.newPage({viewport:{width:1250,height:1150},deviceScaleFactor:1});
  await page.setContent(html);
  await page.screenshot({path:path.join(__dirname,'interface-preview.png'),fullPage:true});
  const overflow = await page.locator('.phone').evaluateAll(nodes => nodes.map(n => ({width:n.scrollWidth>n.clientWidth,height:n.scrollHeight>n.clientHeight})));
  console.log(JSON.stringify({overflow}));
 } finally { await browser.close(); }
})();
