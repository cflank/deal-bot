"use strict";
// 规则见 dealweb/DESIGN.md。没有框架：每个视图是一个返回 HTML 字符串的函数。

const main = document.getElementById("main");
const tabs = document.getElementById("tabs");
const COND = { new: "全新", openbox: "开箱", refurb: "翻新", used: "二手" };
let me = null;

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const money = (v) => "$" + Number(v).toLocaleString("en-CA", { minimumFractionDigits: v % 1 ? 2 : 0, maximumFractionDigits: 2 });

async function api(method, path, body) {
  const res = await fetch(path, {
    method, credentials: "same-origin",
    headers: body === undefined ? {} : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (res.status === 401 && me) { me = null; location.hash = "#/login"; }
  if (!res.ok) throw new Error(data.error || "网络出错了，请稍后再试");
  return data;
}

function toast(text) {
  const el = document.getElementById("toast");
  el.textContent = text;
  el.classList.add("show");
  clearTimeout(toast.t);
  toast.t = setTimeout(() => el.classList.remove("show"), 2600);
}

function ago(iso) {
  if (!iso) return "";
  const min = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
  if (min < 1) return "刚刚更新";
  if (min < 60) return `${min} 分钟前更新`;
  if (min < 1440) return `${Math.round(min / 60)} 小时前更新`;
  return `${Math.round(min / 1440)} 天前更新`;
}

// 表单 → 对象。多选框收成数组。
function formData(form) {
  const out = {};
  for (const el of form.elements) {
    if (!el.name) continue;
    if (el.type === "checkbox") {
      if (el.dataset.single !== undefined) out[el.name] = el.checked;
      else (out[el.name] ||= []), el.checked && out[el.name].push(el.value);
    } else if (el.type === "radio") { if (el.checked) out[el.name] = el.value; }
    else out[el.name] = el.value.trim();
  }
  return out;
}

async function submit(form, fn) {
  const btn = form.querySelector("button[type=submit]");
  const err = form.querySelector(".error");
  if (err) err.textContent = "";
  btn.disabled = true;
  try { await fn(formData(form)); }
  catch (e) { if (err) err.textContent = e.message; else toast(e.message); }
  finally { btn.disabled = false; }
}

// ---------------------------------------------------------------- 登录 / 注册

function viewLogin() {
  let phone = "";
  const stepPhone = (register) => `
    <div class="auth">
      <h1>比价机器人</h1>
      <p class="lead">列出想买的东西和你的心理价，降到价就推到你手机。</p>
      <div class="tag hit demo" aria-hidden="true">${priceHtml(850)}
        <div class="tag-info"><span class="tag-title">iPhone Air 256GB</span>二手 · Kijiji 大多伦多<br><span class="was">$1,599</span>原价的 53%</div>
        <span class="sticker">到价</span></div>
      <form id="f">
        <div class="field"><label for="phone">手机号</label>
          <input id="phone" name="phone" type="tel" inputmode="tel" autocomplete="tel" required value="${esc(phone)}"></div>
        ${register ? `
        <div class="field"><label for="invite">邀请码</label>
          <input id="invite" name="invite" type="text" autocomplete="off" autocapitalize="none" required>
          <p class="hint">向给你网址的人要。</p></div>
        <div class="field"><label for="bark">Bark 推送地址</label>
          <input id="bark" name="bark" type="text" autocomplete="off" autocapitalize="none" required placeholder="https://api.day.app/…">
          <p class="hint">iPhone 装好 Bark，打开后复制首页那行地址贴到这里。验证码会推到这台手机。</p></div>` : ""}
        <p class="error" role="alert"></p>
        <button class="btn primary wide" type="submit">获取验证码</button>
      </form>
      <p style="margin-top:16px"><button class="link" id="swap">${register ? "已经注册过，直接登录" : "第一次用，凭邀请码注册"}</button></p>
      <p><a class="link-row" href="#/help">使用说明：怎么装 Bark、怎么注册、怎么用</a></p>
    </div>`;
  const stepCode = () => `
    <div class="auth">
      <h1>输入验证码</h1>
      <p class="lead">6 位验证码已经推到你的手机（Bark），5 分钟内有效。</p>
      <form id="f">
        <div class="field"><label for="code">验证码</label>
          <input class="code" id="code" name="code" type="text" inputmode="numeric" autocomplete="one-time-code" maxlength="6" required></div>
        <p class="error" role="alert"></p>
        <button class="btn primary wide" type="submit">登录</button>
      </form>
      <p style="margin-top:16px"><button class="link" id="back">换个手机号或重新获取</button></p>
    </div>`;
  const show = (register) => {
    main.innerHTML = stepPhone(register);
    document.getElementById("swap").onclick = () => { phone = document.getElementById("phone").value; show(!register); };
    document.getElementById("f").onsubmit = (ev) => {
      ev.preventDefault();
      submit(ev.target, async (d) => {
        phone = d.phone;
        await api("POST", "/api/auth/start", d);
        main.innerHTML = stepCode();
        document.getElementById("code").focus();
        document.getElementById("back").onclick = () => show(register);
        document.getElementById("f").onsubmit = (ev2) => {
          ev2.preventDefault();
          submit(ev2.target, async (d2) => {
            await api("POST", "/api/auth/verify", { phone, code: d2.code });
            me = await api("GET", "/api/me");
            location.hash = "#/board";
          });
        };
      });
    };
  };
  show(false);
}

// ----------------------------------------------------------------------- 看板

function priceHtml(v) {
  const [whole, cents] = Number(v).toFixed(2).split(".");
  const big = Number(whole).toLocaleString("en-CA");
  return `<div class="tag-price${big.length > 5 ? " long" : ""}"><sup>$</sup>${big}${cents !== "00" ? `<sup>.${cents}</sup>` : ""}</div>`;
}

function tagHtml(o, product) {
  const lines = [`${COND[o.condition] || o.condition} · ${esc(o.source)}`];
  if (o.msrp && o.pct != null) lines.push(`<span class="was">${money(o.msrp)}</span>原价的 ${Math.round(o.pct)}%`);
  else if (o.regular_price) lines.push(`<span class="was">${money(o.regular_price)}</span>商家原标价`);
  if (!o.qualifies && product.alert_below && o.price > product.alert_below) lines.push(`<span class="mute">离提醒价还差 ${money(o.price - product.alert_below)}</span>`);
  if (o.seller) lines.push(esc(o.seller));
  if (o.carrier) lines.push(`标题含 ${esc(o.carrier)}，可能要绑运营商`);
  return `<div class="tag${o.qualifies ? " hit" : ""}">${priceHtml(o.price)}
    <div class="tag-info"><a class="tag-title" href="${esc(o.url)}" target="_blank" rel="noopener noreferrer">${esc(o.title)}</a>${lines.join("<br>")}</div>
    ${o.qualifies ? '<span class="sticker">到价</span>' : ""}</div>`;
}

function trendHtml(days) {
  const pts = Object.keys(days || {}).sort().map((d) => Math.min(...Object.values(days[d])));
  if (pts.length < 2) return "";
  const lo = Math.min(...pts), hi = Math.max(...pts), span = hi - lo || 1;
  const xy = pts.map((p, i) => `${(i / (pts.length - 1) * 100).toFixed(1)},${(4 + (1 - (p - lo) / span) * 36).toFixed(1)}`).join(" ");
  return `<svg class="trend" viewBox="0 0 100 44" preserveAspectRatio="none" role="img"
    aria-label="近 ${pts.length} 天每日最低价，从 ${money(pts[0])} 到 ${money(pts[pts.length - 1])}"><polyline points="${xy}"/></svg>
    <p class="small mute" style="margin-top:-6px">近 ${pts.length} 天最低价：${money(lo)} – ${money(hi)}</p>`;
}

async function viewBoard() {
  const [board, { products }] = await Promise.all([api("GET", "/api/dashboard"), api("GET", "/api/products")]);
  const active = products.filter((p) => p.enabled !== false);
  let html = `<div class="head"><h1>看板</h1><span class="small mute">${esc(ago(board.generated_at))}</span></div>`;
  if (board.push_errors && board.push_errors.length) {
    html += `<div class="notice bad">上次推送有手机没收到。到「设置」里检查 Bark 地址，再发一次测试推送。</div>`;
  }
  const sales = board.store_sales || [];
  if (sales.length) {
    html += `<section class="group"><div class="group-head"><h2>品牌活动</h2><span class="small mute">最近 7 天</span></div>
      <ul class="rows">${sales.map((e) => `<li><a class="row" href="${esc(e.url)}" target="_blank" rel="noopener noreferrer">
        ${esc(e.name)}：${e.dropped} 件商品降价，一半降到之前价格的 ${Math.round(e.ratio * 100)}% 或更低
        <span class="small mute">${new Date(e.at * 1000).toLocaleDateString("zh-CN", { month: "long", day: "numeric" })}发现</span></a></li>`).join("")}</ul>
      <p class="hint">整店商品标价一起下调时会出现在这里。结账时才减的折扣看不到。</p></section>`;
  }
  if (!active.length) {
    html += `<div class="empty"><p>还没有在盯的商品。加一个，半小时内开始盯价，降到你的价就推送。</p>
      <a class="btn primary" href="#/products/new">添加商品</a></div>`;
  }
  for (const p of active) {
    const offers = (board.offers || []).filter((o) => o.product === p.id);
    const side = p.alert_below ? `提醒价 ${money(p.alert_below)}` : "";
    html += `<section class="group"><div class="group-head"><h2>${esc(p.name)}</h2><span class="small mute">${side}</span></div>`;
    if (p.category === "deals") {
      html += `<p class="mute">只盯优惠帖子，不查价格。帖子标题里出现你填的词就推送，最近的帖子列在下面。</p>`;
    } else if (!offers.length) {
      const checked = (board.products || []).some((x) => x.id === p.id);
      html += `<p class="mute">${checked ? "这一轮没找到符合条件的报价。可以放宽价格范围，或减少“必须包含的词”。" : "正在第一次检查，一两分钟后刷新看看。"}</p>`;
    } else {
      html += offers.slice(0, 5).map((o) => tagHtml(o, p)).join("");
      if (offers.length > 5) {
        html += `<details><summary>其余 ${offers.length - 5} 个报价</summary>${offers.slice(5, 40).map((o) => tagHtml(o, p)).join("")}</details>`;
      }
      html += trendHtml((board.history || {})[p.id]);
    }
    const posts = (board.posts || []).filter((x) => x.product === p.id).slice(0, 3);
    if (posts.length) {
      html += `<p class="small mute" style="margin:8px 0 0">论坛里的相关帖子</p><ul class="rows">${posts.map((x) =>
        `<li><a class="row" href="${esc(x.link)}" target="_blank" rel="noopener noreferrer">${esc(x.title)}</a></li>`).join("")}</ul>`;
    }
    html += `</section>`;
  }
  main.innerHTML = html;
}

// ----------------------------------------------------------------------- 商品

async function viewProducts() {
  const { products } = await api("GET", "/api/products");
  const rows = products.map((p) => {
    const conds = (p.conditions && p.conditions.length ? p.conditions : Object.keys(COND)).map((c) => COND[c]).join(" / ");
    return `<li><a class="row" href="#/products/${esc(p.id)}"><span><b>${esc(p.name)}</b><span class="small mute">${esc(conds)}${p.enabled === false ? "，已暂停" : ""}</span></span>
      <span class="side">${p.alert_below ? "提醒价 " + money(p.alert_below) : "未设提醒价"}</span></a></li>`;
  }).join("");
  main.innerHTML = `<div class="head"><h1>商品</h1><span class="small mute">${products.length} / ${me.max_products}</span></div>
    ${products.length ? `<ul class="rows">${rows}</ul>` : `<p class="empty">还没有商品。</p>`}
    ${products.length < me.max_products ? `<a class="btn primary" href="#/products/new">添加商品</a>` : `<p class="mute">已到上限 ${me.max_products} 个。删掉一个才能再加。</p>`}`;
}

function chips(name, options, checked) {
  return `<div class="chips">${options.map(([value, label]) =>
    `<label><input type="checkbox" name="${name}" value="${esc(value)}"${checked.includes(value) ? " checked" : ""}><span>${esc(label)}</span></label>`).join("")}</div>`;
}

// 参考原价那一栏下面的说明：手填的、自动查到的、还没查到的，各说各的。
function refHint(p) {
  const tiers = (m) => Object.entries(m || {}).filter(([k]) => k !== "default")
    .sort((a, b) => a[0] - b[0]).map(([gb, v]) => `${gb >= 1024 ? gb / 1024 + "TB" : gb + "GB"} ${money(v)}`).join("，");
  if (p.msrp && Object.keys(p.msrp).length && !("default" in p.msrp)) return `现在按手填的分档原价算：${tiers(p.msrp)}。在上面填一个数会改成只用这一个原价。`;
  if (p.msrp && p.msrp.default) return "现在按你填的原价算。清空就改回自动查。";
  const a = p.auto_msrp || {};
  const found = tiers(a) || (a.default ? money(a.default) : "");
  if (found) return `自动查到：${found}（Best Buy 自营新品价）。没查到的容量不显示百分比；想用别的数就在这里手填。`;
  return "留空会自动用 Best Buy 自营新品价，或品牌官网自己的标价。查不到的不显示百分比，可以在这里手填。";
}

async function viewProduct(id) {
  const isNew = id === "new";
  const p = isNew ? { category: "electronics", conditions: [], enabled: true }
    : (await api("GET", "/api/products")).products.find((x) => x.id === id);
  if (!p) { location.hash = "#/products"; return; }
  const sourceChips = (cat) => chips("sources",
    me.sources.filter((s) => s.categories.includes(cat)).map((s) => [s.id, s.name]), p.sources || []);
  main.innerHTML = `<div class="head"><h1>${isNew ? "添加商品" : "编辑商品"}</h1><a class="small" href="#/products">返回</a></div>
    <form id="f">
      <div class="field"><label for="name">商品名称</label>
        <input id="name" name="name" type="text" required maxlength="60" value="${esc(p.name)}" placeholder="比如 iPhone Air、Coach Tabby 包"></div>
      <div class="field"><label for="query">搜索关键词</label>
        <input id="query" name="query" type="text" maxlength="80" value="${esc(p.query)}" placeholder="不填就用商品名称">
        <p class="hint">拿去各个网站搜索的词，用英文更容易搜到。</p></div>
      <div class="field"><span class="label" id="l-cat">类别</span>
        <div class="chips" role="radiogroup" aria-labelledby="l-cat">${Object.entries(me.categories).map(([k, v]) =>
          `<label><input type="radio" name="category" value="${k}"${p.category === k ? " checked" : ""}><span>${esc(v)}</span></label>`).join("")}</div>
        <p class="hint">决定去哪些网站找：电子数码查 Best Buy 等，服饰箱包查品牌官网和二手寄卖店。“只盯优惠帖子”不查价格，优惠论坛和资讯站的帖子标题里出现下面的词就推送，适合填品牌名，比如 lululemon。</p></div>
      <div class="field"><label for="include">型号，或标题里必须有的词</label>
        <textarea id="include" name="include" placeholder="用逗号分开，有一个就算。不填就用商品名称">${esc((p.include || []).join(", "))}</textarea>
        <p class="hint">比如 iphone air, iphone 17 air。写得越具体，搜到的越准。</p></div>
      <div class="field"><label for="exclude">不要的词</label>
        <textarea id="exclude" name="exclude" placeholder="比如 case for, screen protector, for parts">${esc((p.exclude || []).join(", "))}</textarea>
        <p class="hint">标题里出现这些词的会被丢掉，用来排除配件、坏机。</p></div>
      <div class="field"><span class="label">成色</span>
        ${chips("conditions", Object.entries(COND), p.conditions || [])}
        <p class="hint">一个都不选等于全要。成色主要靠标题判断，标题没写的会当成全新。</p></div>
      <div class="field"><span class="label">价格范围</span>
        <div class="pair">
          <input name="min_price" type="number" inputmode="decimal" min="0" step="any" value="${esc(p.min_price)}" placeholder="最低 $" aria-label="最低价">
          <input name="max_price" type="number" inputmode="decimal" min="0" step="any" value="${esc(p.max_price)}" placeholder="最高 $" aria-label="最高价"></div>
        <p class="hint">范围外的直接忽略。设个最低价能挡掉大部分配件。</p></div>
      <div class="field"><label for="skip_below">太便宜的当成配件忽略</label>
        <select id="skip_below" name="skip_below">${[["", "不过滤"], ["5", "不到原价的五分之一"], ["4", "不到原价的四分之一"], ["3", "不到原价的三分之一"], ["2", "不到原价的一半"]].map(([v, t]) =>
          `<option value="${v}"${String(isNew ? 4 : p.skip_below || "") === v ? " selected" : ""}>${t}</option>`).join("")}</select>
        <p class="hint">比如手机壳标题里也有 iPhone Air，价格却只有几块钱。低于这条线的报价不上看板、不提醒。原价查不到的商品不受影响，仍要靠最低价。</p></div>
      <div class="field"><label for="alert_below">提醒价</label>
        <input id="alert_below" name="alert_below" type="number" inputmode="decimal" min="0" step="any" value="${esc(p.alert_below)}" placeholder="$">
        <p class="hint">价格低于或等于这个数就推送到你手机。</p></div>
      <div class="field"><label for="msrp">参考原价</label>
        <input id="msrp" name="msrp_default" type="number" inputmode="decimal" min="0" step="any" value="${esc(p.msrp && p.msrp.default)}" placeholder="留空就自动查">
        <p class="hint">${esc(refHint(p))}</p></div>
      <div class="field"><span class="label">低到原价的百分之几就提醒</span>
        <div class="pair">${Object.entries(COND).map(([k, label]) =>
          `<label class="mini">${label} %<input name="pct_${k}" type="number" inputmode="decimal" min="1" max="150" step="any" value="${esc((p.max_pct || {})[k])}" placeholder="不提醒"></label>`).join("")}</div>
        <p class="hint">比如二手填 70：二手价低到原价的 70% 或更低就推送。空着的成色不按百分比提醒。原价查不到也没手填的报价不会按百分比提醒；和提醒价满足一个就推送。</p></div>
      <details${(p.sources || []).length ? " open" : ""}><summary>只搜指定的网站</summary>
        <div id="srcs">${sourceChips(p.category)}</div>
        <p class="hint" style="margin-top:8px">一个都不选等于这个类别的全部网站。</p></details>
      <div class="field"><label><input type="checkbox" name="enabled" data-single${p.enabled !== false ? " checked" : ""}> 正在盯这个商品</label></div>
      <p class="error" role="alert"></p>
      <div class="btn-row">
        <button class="btn primary" type="submit">保存商品</button>
        ${isNew ? "" : `<button class="btn danger" type="button" id="del">删除</button>`}
      </div>
    </form>`;
  const form = document.getElementById("f");
  form.querySelectorAll("input[name=category]").forEach((el) => {
    el.onchange = () => { document.getElementById("srcs").innerHTML = sourceChips(el.value); };
  });
  form.onsubmit = (ev) => {
    ev.preventDefault();
    submit(form, async (d) => {
      const tiers = p.msrp && !("default" in p.msrp) && Object.keys(p.msrp).length ? p.msrp : null;
      d.msrp = d.msrp_default ? { default: d.msrp_default } : (tiers || {});   // 导入的按容量分档参考价原样保留
      delete d.msrp_default;
      d.max_pct = {};
      for (const k of Object.keys(COND)) { d.max_pct[k] = d[`pct_${k}`]; delete d[`pct_${k}`]; }
      await api(isNew ? "POST" : "PUT", isNew ? "/api/products" : `/api/products/${id}`, d);
      toast("已保存，正在检查这个商品");
      location.hash = "#/products";
    });
  };
  const del = document.getElementById("del");
  if (del) del.onclick = async () => {
    if (!confirm(`删除「${p.name}」？它的价格记录也会一起删掉。`)) return;
    try { await api("DELETE", `/api/products/${id}`, {}); toast("已删除"); location.hash = "#/products"; }
    catch (e) { toast(e.message); }
  };
}

// ----------------------------------------------------------------------- 设置

function viewSettings() {
  const hours = [[-1, "不发每日行情"]].concat([...Array(24).keys()].map((h) => [h, `每天 ${h}:00`]));
  main.innerHTML = `<div class="head"><h1>设置</h1><span class="small mute">${esc(me.phone)}</span></div>
    <section class="group"><h2>推送到手机</h2>
      <p class="mute">现在推送到 ${me.devices} 台手机。</p>
      <div class="btn-row"><button class="btn" id="test">发送测试推送</button></div>
      <details><summary>更换或添加手机</summary>
        <form id="fb">
          <div class="field"><label for="bark">Bark 推送地址</label>
            <textarea id="bark" name="bark" required placeholder="https://api.day.app/…"></textarea>
            <p class="hint">每行一台手机，最多 5 台。保存后会替换掉原来的全部地址；这里不会显示已保存的地址。</p></div>
          <p class="error" role="alert"></p>
          <button class="btn primary" type="submit">保存推送地址</button>
        </form></details>
    </section>
    <section class="group"><h2>每日行情</h2>
      <form id="fd">
        <div class="field"><label for="hour">每天汇总一次当前最低价（多伦多时间）</label>
          <select id="hour" name="daily_digest_hour">${hours.map(([v, t]) =>
            `<option value="${v}"${Number(me.settings.daily_digest_hour) === v ? " selected" : ""}>${t}</option>`).join("")}</select></div>
        <p class="error" role="alert"></p>
        <button class="btn" type="submit">保存时间</button>
      </form>
    </section>
    <section class="group"><h2>品牌活动</h2>
      <div class="field"><label><input type="checkbox" id="sales"${me.settings.store_sales !== false ? " checked" : ""}> 品牌整店降价时推送给我</label>
        <p class="hint">每隔几小时看一遍 Matt &amp; Nat、Herschel 等 29 家品牌官网的全部标价，一大批商品同时降价就推送一条，不用事先添加商品。</p></div>
    </section>
    <section class="group"><p><a class="link-row" href="#/help">使用说明</a></p>
      <button class="btn" id="out">退出登录</button></section>`;
  document.getElementById("sales").onchange = async (ev) => {
    try { me = await api("PUT", "/api/settings", { store_sales: ev.target.checked }); toast(ev.target.checked ? "已开启品牌活动推送" : "已关闭品牌活动推送"); }
    catch (e) { ev.target.checked = !ev.target.checked; toast(e.message); }
  };
  document.getElementById("test").onclick = async (ev) => {
    ev.target.disabled = true;
    try { const r = await api("POST", "/api/test-push", {}); toast(`测试推送已发到 ${r.devices} 台手机`); }
    catch (e) { toast(e.message); }
    ev.target.disabled = false;
  };
  document.getElementById("fb").onsubmit = (ev) => {
    ev.preventDefault();
    submit(ev.target, async (d) => { me = await api("PUT", "/api/settings", d); toast("推送地址已保存"); viewSettings(); });
  };
  document.getElementById("fd").onsubmit = (ev) => {
    ev.preventDefault();
    submit(ev.target, async (d) => { me = await api("PUT", "/api/settings", { daily_digest_hour: Number(d.daily_digest_hour) }); toast("时间已保存"); });
  };
  document.getElementById("out").onclick = async () => {
    await api("POST", "/api/auth/logout", {}).catch(() => {});
    me = null;
    location.hash = "#/login";
  };
}

// ------------------------------------------------------------------- 使用说明
// 不登录也能看：新用户要先照着它装 Bark、注册。

function viewHelp() {
  const back = me ? `<a class="small" href="#/settings">返回设置</a>` : `<a class="small" href="#/login">返回登录</a>`;
  main.innerHTML = `<div class="head"><h1>使用说明</h1>${back}</div>
  <div class="doc">
    <p>你列出想买的东西和心理价，它每半小时去各个网站查一遍，降到价就推送到你的 iPhone。</p>

    <h2>开始之前</h2>
    <ul>
      <li>需要一台 iPhone。推送靠 Bark 这个 App，安卓手机用不了。</li>
      <li>需要一个邀请码，找给你网址的人要。每个邀请码只能用一次。</li>
    </ul>

    <h2>第一步：装 Bark</h2>
    <ol>
      <li>在 App Store 搜索 Bark，安装并打开，允许它发通知。</li>
      <li>首页有一行以 https://api.day.app/ 开头的地址，点一下复制。</li>
    </ol>

    <h2>第二步：注册</h2>
    <ol>
      <li>在登录页点“第一次用，凭邀请码注册”。</li>
      <li>手机号：只当账户名用，不会给你发短信或打电话。</li>
      <li>邀请码：直接粘贴，区分大小写。</li>
      <li>Bark 推送地址：粘贴刚才复制的那一行。</li>
      <li>点“获取验证码”。6 位验证码会从 Bark 推到你手机上，5 分钟内有效。填进去，点“登录”。</li>
    </ol>
    <p>以后登录只要填手机号，验证码还是从 Bark 推过来。登录一次管 30 天。</p>
    <p>想用起来像个 App：在 Safari 里点分享按钮，选“添加到主屏幕”。</p>

    <h2>第三步：添加要盯的商品</h2>
    <p>点底部的“商品”，再点“添加商品”。最少填这几项就能用：</p>
    <ul>
      <li><b>商品名称</b>：比如 iPhone Air、Coach Tabby。用英文更容易搜到。</li>
      <li><b>类别</b>：决定去哪些网站找。电子数码查 Best Buy、Walmart、Kijiji 二手；服饰箱包查 29 家品牌官网、4 家二手寄卖店、Walmart、Kijiji；“只盯优惠帖子”不查价格，优惠论坛里有相关帖子就推送。</li>
      <li><b>什么时候提醒</b>，两种填一种就行。提醒价：价格低于这个数就推送。低到原价的百分之几就提醒：按成色分别填，比如二手填 60，二手价低到原价的六成就推送。</li>
    </ul>
    <p>点“保存商品”。大约一分钟后看板上就有结果，之后每半小时自动查一次，品牌官网每 3 小时查一次。每人最多盯 ${me ? me.max_products : 10} 个商品。</p>
    <p>想查得更准，再填这些：</p>
    <ul>
      <li><b>型号，或标题里必须有的词</b>：用逗号分开，有一个就算。写得越具体越准。</li>
      <li><b>不要的词</b>：标题里出现就丢掉，比如 case for, screen protector, for parts。</li>
      <li><b>成色</b>：全新、开箱、翻新、二手，可以多选，都不选等于全要。</li>
      <li><b>价格范围</b>：范围外的直接忽略。填个最低价能挡掉大部分配件。</li>
      <li><b>太便宜的当成配件忽略</b>：默认是不到原价的四分之一就忽略，一般不用改。</li>
      <li><b>参考原价</b>：留空会自动查。Best Buy 自己在卖的东西和品牌官网的商品能查到，查不到的可以手填。</li>
      <li><b>只搜指定的网站</b>：比如只盯某一家品牌店。</li>
    </ul>

    <h2>看板怎么看</h2>
    <ul>
      <li>黄色价签、右上角有红色“到价”：达到了你的提醒条件。</li>
      <li>“原价的 53%”：这个报价是原价的百分之几，越小越划算。</li>
      <li>点价签上的商品标题会打开商家页面。</li>
      <li>盯了两天以上，每个商品下面会出现价格走势，能看出是真降价还是先涨后降。</li>
    </ul>

    <h2>会收到哪些推送</h2>
    <ul>
      <li><b>开始监控</b>：新加商品后的第一条，告诉你找到了多少报价。</li>
      <li><b>到价提醒</b>：有报价达到你的条件。同一个报价只提醒一次，除非它又降了 30 加元以上。</li>
      <li><b>优惠帖</b>：优惠论坛里出现了和你的商品相关的帖子。</li>
      <li><b>每日行情</b>：每天早上 9 点汇总一次当前最低价。可以在“设置”里改时间或关掉。</li>
      <li><b>品牌活动</b>：某家品牌官网一大批商品同时降价。不用事先加商品，可以在“设置”里关掉。</li>
    </ul>

    <h2>黑五怎么用</h2>
    <ul>
      <li>把想买的具体商品现在就加进去，让它先记下平时的价格。</li>
      <li>想知道某个牌子有没有活动：加一个商品，名称填品牌名，比如 lululemon，类别选“只盯优惠帖子”。</li>
      <li>想看所有黑五消息：同样的做法，名称填 black friday。帖子会很多，每轮单独推 3 条，其余合成一条。</li>
    </ul>

    <h2>常见问题</h2>
    <details><summary>收不到验证码</summary><p>先看 Bark 有没有通知权限，再看地址是不是复制完整了。同一个手机号每分钟只能要一次验证码，每小时最多 5 次。</p></details>
    <details><summary>换了手机，或者 Bark 地址变了</summary><p>还能登录的话，到“设置”里点“更换或添加手机”，填新地址。登不上的话，找给你邀请码的人要一个换绑码。</p></details>
    <details><summary>想让两台手机都收到</summary><p>到“设置”里点“更换或添加手机”，把两台的地址都填上，每行一个。保存会替换掉原来的全部地址，所以两台都要填。</p></details>
    <details><summary>提醒太多</summary><p>把提醒价调低，或者把百分比调小。配件混进来的话，填一个最低价，或者在“不要的词”里加上。</p></details>
    <details><summary>为什么有些网站查不到</summary><p>Amazon、eBay、Costco、Lululemon、Aritzia、Uniqlo、Sport Chek 这些网站不让程序访问，查不到。它们的活动只能靠“只盯优惠帖子”间接知道。</p></details>
    <details><summary>成色准不准</summary><p>Kijiji 和寄卖店一律算二手。其它网站靠标题里的字判断，标题没写的会当成全新。</p></details>
    <details><summary>折扣码算不算</summary><p>不算。只能看到商品的标价，结账时才减的折扣和折扣码看不到。</p></details>
    <details><summary>别人能看到我盯了什么吗</summary><p>其他用户看不到。管理员在管理页里能看到你的手机号和你加了几个商品。这是私人搭的网站，数据存在管理员的服务器上。</p></details>

    <p class="mute">价格以商家页面为准，下单前自己再核对一遍。Kijiji 是私人交易，当面验货。</p>
  </div>`;
}

// ----------------------------------------------------------------------- 管理

async function viewAdmin() {
  const a = await api("GET", "/api/admin");
  const date = (t) => new Date(t * 1000).toLocaleDateString("zh-CN");
  const health = Object.entries((a.health && a.health.sources) || {});
  const bad = health.filter(([, h]) => !h.ok);
  main.innerHTML = `<div class="head"><h1>管理</h1></div>
    <section class="group"><h2>邀请码</h2>
      <p class="mute">每个邀请码只能注册一个账户。</p>
      <div class="btn-row"><button class="btn primary" id="inv">生成邀请码</button></div>
      <div id="new"></div>
      ${a.invites.length ? `<ul class="rows">${a.invites.map((i) =>
        `<li><div class="row"><span><b>${esc(i.code)}</b><span class="small mute">${i.kind === "rebind" ? "换绑码 " + esc(i.phone) : i.kind === "admin" ? "管理员邀请码" : "还没用"}</span></span><span class="side mute">${date(i.created_at)}</span></div></li>`).join("")}</ul>` : ""}
    </section>
    <section class="group"><h2>用户</h2>
      <ul class="rows">${a.users.map((u) =>
        `<li><div class="row"><span><b>${esc(u.phone)}</b><span class="small mute">${u.is_admin ? "管理员，" : ""}${date(u.created_at)} 注册</span></span><span class="side">${u.products} 个商品</span></div></li>`).join("")}</ul>
    </section>
    <section class="group"><h2>数据源</h2>
      ${health.length ? `<p class="mute">上一轮 ${health.length - bad.length} / ${health.length} 个正常。</p>` : `<p class="mute">还没跑过完整的一轮。</p>`}
      ${bad.length ? `<ul class="rows">${bad.map(([n, h]) =>
        `<li><div class="row"><span><b>${esc(n)}</b><span class="small error">${esc(h.error || "失败")}</span></span></div></li>`).join("")}</ul>` : ""}
    </section>`;
  document.getElementById("inv").onclick = async () => {
    try {
      const r = await api("POST", "/api/admin/invites", {});
      document.getElementById("new").innerHTML = `<div class="notice">新邀请码：<span class="invite-code">${esc(r.code)}</span><br>
        <span class="small mute">把它和网址一起发给对方。</span></div>`;
    } catch (e) { toast(e.message); }
  };
}

// ----------------------------------------------------------------------- 路由

function renderTabs(current) {
  const items = [["board", "看板"], ["products", "商品"], ["settings", "设置"]];
  if (me && me.is_admin) items.push(["admin", "管理"]);
  tabs.hidden = !me;
  tabs.innerHTML = items.map(([k, t]) => `<a href="#/${k}"${k === current ? ' aria-current="page"' : ""}>${t}</a>`).join("");
}

async function route() {
  const [, view = "board", arg] = location.hash.split("/");
  if (!me) {
    try { me = await api("GET", "/api/me"); } catch { me = null; }
  }
  if (!me) { renderTabs(); view === "help" ? viewHelp() : viewLogin(); window.scrollTo(0, 0); return; }
  if (view === "login") { location.hash = "#/board"; return; }
  renderTabs(view === "help" ? "settings" : view);
  try {
    if (view === "help") viewHelp();
    else if (view === "products" && arg) await viewProduct(arg);
    else if (view === "products") await viewProducts();
    else if (view === "settings") viewSettings();
    else if (view === "admin" && me.is_admin) await viewAdmin();
    else await viewBoard();
  } catch (e) {
    if (me) main.innerHTML = `<div class="notice bad">${esc(e.message)}</div>`;
  }
  window.scrollTo(0, 0);
}

window.addEventListener("hashchange", route);
route();
