"""Self-contained HTML report: searchable company list with summary, news, risks and sourced facts."""
import html
import json

TEMPLATE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Norway Company Profiles</title>
<style>
:root{--bg:#f7f7f5;--card:#fff;--ink:#1b1d21;--mute:#626872;--line:#e3e4e8;--acc:#1f5fbf;--ok:#1e7a46;--warn:#b26a00;--bad:#b3261e}
@media (prefers-color-scheme:dark){:root{--bg:#121417;--card:#1b1e23;--ink:#e8eaed;--mute:#9aa0a8;--line:#2c3037;--acc:#7fb0ff;--ok:#5cc28a;--warn:#f0b35a;--bad:#ff8a80}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
header{padding:20px 16px 8px;max-width:1100px;margin:auto}h1{margin:0 0 4px;font-size:22px}
.sub{color:var(--mute);font-size:13px}main{max-width:1100px;margin:auto;padding:0 16px 40px}
input{width:100%;padding:10px 12px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--ink);font-size:15px;margin:12px 0}
.stats{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:8px}.stat{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:8px 12px;font-size:13px}
.stat b{display:block;font-size:18px}
details{background:var(--card);border:1px solid var(--line);border-radius:8px;margin:6px 0}
summary{cursor:pointer;padding:10px 12px;display:flex;gap:10px;align-items:baseline;flex-wrap:wrap;list-style:none}
summary::-webkit-details-marker{display:none}.nm{font-weight:600}.org{color:var(--mute);font-variant-numeric:tabular-nums;font-size:13px}
.pill{font-size:12px;border-radius:99px;padding:1px 8px;border:1px solid var(--line);color:var(--mute)}
.pill.active{color:var(--ok);border-color:var(--ok)}.pill.bad{color:var(--bad);border-color:var(--bad)}
.body{padding:0 12px 12px;border-top:1px solid var(--line)}h3{font-size:13px;text-transform:uppercase;letter-spacing:.04em;color:var(--mute);margin:14px 0 6px}
ul{margin:0;padding-left:18px}li{margin:2px 0}a{color:var(--acc)}
table{width:100%;border-collapse:collapse;font-size:13px}td{border-top:1px solid var(--line);padding:5px 6px;vertical-align:top}
td:first-child{color:var(--mute);width:34%}.wrap{overflow-x:auto}.sev-high{color:var(--bad)}.sev-medium{color:var(--warn)}
.more{margin:12px 0;text-align:center}.sel{margin-left:auto;font-size:13px;color:var(--mute)}
#cmp table{background:var(--card);border:1px solid var(--line);border-radius:8px;margin:8px 0}#cmp th{text-align:left;padding:6px;font-size:13px}button{padding:8px 14px;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--ink);cursor:pointer}
</style></head><body>
<header><h1>Norway Company Profiles</h1><div class="sub">Every fact links to its public source. Generated __GENERATED__.</div></header>
<main>
<div class="stats" id="stats"></div>
<input id="q" placeholder="Search by name, organisation number, municipality or industry…" autocomplete="off">
<div id="cmpbar" class="stats" style="display:none"><div class="stat"><b id="cmpn">0</b>selected</div><button id="cmpgo">Compare selected</button><button id="cmpclear">Clear</button></div>
<div id="cmp" class="wrap"></div>
<div id="list"></div><div class="more"><button id="more">Show more</button></div>
</main>
<script id="data" type="application/json">__DATA__</script>
<script>
const D=JSON.parse(document.getElementById('data').textContent);
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const fmtv=v=>{if(v&&typeof v==='object'&&'amount'in v)return Number(v.amount).toLocaleString('en-US')+' '+v.currency+' ('+v.period+')';
 if(Array.isArray(v))return v.map(x=>x&&typeof x==='object'?(x.name||x.title||x.exchange||JSON.stringify(x)):x).slice(0,8).join('; ');
 if(v&&typeof v==='object')return v.name||Object.entries(v).map(([k,x])=>k+': '+x).join(', ');return String(v)};
const st=document.getElementById('stats');
const n=D.length,withNews=D.filter(p=>(p.news||[]).length).length,facts=D.reduce((a,p)=>a+(p.facts||[]).length,0);
st.innerHTML=[['Companies',n],['Avg. facts',n?(facts/n).toFixed(1):0],['With dated news',withNews]].map(([k,v])=>`<div class="stat"><b>${v}</b>${k}</div>`).join('');
let shown=50,rows=D;const SEL=new Set();
function card(p){const s=p.status||'';const cls=s==='active'?'active':(['bankrupt','deleted','under_liquidation','compulsory_liquidation'].includes(s)?'bad':'');
 const news=(p.news||[]).slice(0,10).map(i=>`<li>${esc(i.published)} · <a href="${esc(i.url)}" target="_blank" rel="noopener">${esc(i.title)}</a> <span class="org">— ${esc(i.source)}</span></li>`).join('');
 const risks=(p.risk_signals||[]).map(r=>`<li class="sev-${r.severity}">${esc(r.signal)} <span class="org">(${esc(r.evidence)})</span></li>`).join('');
 const kp=(p.key_points||[]).map(k=>`<li>${esc(k)}</li>`).join('');
 const facts=(p.facts||[]).filter(f=>f.category!=='news').map(f=>`<tr><td>${esc(f.label)}</td><td>${esc(fmtv(f.value))}${f.as_of?` <span class="org">as of ${esc(f.as_of)}</span>`:''} · <a href="${esc(f.evidence_url)}" target="_blank" rel="noopener">source</a></td></tr>`).join('');
 const gaps=(p.gaps||[]).map(g=>`<li><b>${esc(g.topic)}:</b> ${esc(g.reason)}</li>`).join('');
 return `<details><summary><span class="nm">${esc(p.name||'(unknown)')}</span><span class="org">${esc(p.organisation_number||'')}</span><span class="pill ${cls}">${esc(s.replace(/_/g,' '))}</span>${(p.news||[]).length?`<span class="pill">${p.news.length} news</span>`:''}<label class="sel" onclick="event.stopPropagation()"><input type="checkbox" data-org="${esc(p.organisation_number)}" ${SEL.has(p.organisation_number)?'checked':''}> compare</label></summary>
 <div class="body"><p>${esc(p.summary)}</p>${kp?`<h3>Key points</h3><ul>${kp}</ul>`:''}${risks?`<h3>Risk signals</h3><ul>${risks}</ul>`:''}
 ${news?`<h3>Dated news &amp; announcements</h3><ul>${news}</ul>`:''}<h3>Facts with sources</h3><div class="wrap"><table>${facts}</table></div>
 ${gaps?`<h3>Not found, and why</h3><ul>${gaps}</ul>`:''}</div></details>`}
function render(){document.getElementById('list').innerHTML=rows.slice(0,shown).map(card).join('')||'<p class="sub">No matches.</p>';
 document.getElementById('more').style.display=rows.length>shown?'':'none'}
document.getElementById('q').addEventListener('input',e=>{const q=e.target.value.toLowerCase().replace(/\s/g,'');shown=50;
 rows=!q?D:D.filter(p=>((p.name||'')+(p.organisation_number||'')+JSON.stringify(p.profile&&p.profile.location||'')+JSON.stringify(p.profile&&p.profile.activity||'')).toLowerCase().replace(/\s/g,'').includes(q));render()});
document.getElementById('more').onclick=()=>{shown+=50;render()};
const SELBAR=document.getElementById('cmpbar');
function updSel(){document.getElementById('cmpn').textContent=SEL.size;SELBAR.style.display=SEL.size?'flex':'none'}
document.getElementById('list').addEventListener('change',e=>{const o=e.target.dataset&&e.target.dataset.org;if(!o)return;e.target.checked?SEL.add(o):SEL.delete(o);updSel()});
const pick=(p,c,f)=>{const v=((p.profile||{})[c]||{})[f];return v==null?'—':fmtv(v)};
const ROWS=[['Status',p=>esc((p.status||'').replace(/_/g,' '))],['Legal form',p=>esc(pick(p,'identity','legal_form'))],
 ['Municipality',p=>esc(pick(p,'location','municipality'))],['Industry',p=>esc(pick(p,'activity','industry_code_1'))],
 ['Employees',p=>esc(pick(p,'size','employees'))],['Accounting period',p=>esc(pick(p,'financials','accounts_period'))],
 ['Revenue',p=>esc(pick(p,'financials','revenue'))],['Net income',p=>esc(pick(p,'financials','net_income'))],
 ['Equity',p=>esc(pick(p,'financials','equity'))],['CEO',p=>esc(pick(p,'people','ceo'))],['Board chair',p=>esc(pick(p,'people','board_chair'))],
 ['Website',p=>esc(pick(p,'contact','website')!=='—'?pick(p,'contact','website'):pick(p,'contact','website_discovered'))],
 ['Dated news items',p=>(p.news||[]).length],['Risk signals',p=>esc((p.risk_signals||[]).map(r=>r.signal).join('; ')||'—')]];
document.getElementById('cmpgo').onclick=()=>{const ps=D.filter(p=>SEL.has(p.organisation_number));
 document.getElementById('cmp').innerHTML=`<table><tr><th></th>${ps.map(p=>`<th>${esc(p.name)}<br><span class="org">${esc(p.organisation_number)}</span></th>`).join('')}</tr>`+
 ROWS.map(([k,f])=>`<tr><td>${k}</td>${ps.map(p=>`<td>${f(p)}</td>`).join('')}</tr>`).join('')+'</table><p class="sub">All values come from the sourced facts in each profile; "—" means not found.</p>'};
document.getElementById('cmpclear').onclick=()=>{SEL.clear();document.getElementById('cmp').innerHTML='';updSel();render()};
render();
</script></body></html>"""

KEEP = ("organisation_number", "name", "status", "summary", "key_points", "risk_signals", "news", "gaps", "profile")


def write_report(path, profiles, generated):
    slim = []
    for p in profiles:
        d = {k: p.get(k) for k in KEEP}
        d["facts"] = [{k: f.get(k) for k in ("label", "category", "value", "as_of", "evidence_url")} for f in p.get("facts", [])]
        slim.append(d)
    data = json.dumps(slim, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(TEMPLATE.replace("__GENERATED__", html.escape(generated)).replace("__DATA__", data))
