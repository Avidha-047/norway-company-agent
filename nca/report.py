"""Self-contained HTML report (works on laptop and phone):
  * find a company  – search by name, organisation number, municipality or industry; #<orgnr> deep links
  * compare         – tick two or more companies and press Compare
  * provenance      – every fact and news item has "Where this came from": source, link, retrieval time,
                      SHA-256 of the retrieved content and the exact quoted text
"""
import html
import json

TEMPLATE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Norway Company Profiles</title>
<style>
:root{--bg:#f7f7f5;--card:#fff;--ink:#1b1d21;--mute:#5d636d;--line:#e3e4e8;--acc:#1f5fbf;--ok:#1e7a46;--warn:#9a5c00;--bad:#b3261e;--code:#f1f2f4}
@media (prefers-color-scheme:dark){:root{--bg:#121417;--card:#1b1e23;--ink:#e8eaed;--mute:#a0a6ae;--line:#2c3037;--acc:#8ab8ff;--ok:#5cc28a;--warn:#f0b35a;--bad:#ff8a80;--code:#25292f}}
*{box-sizing:border-box}html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
header,main{max-width:1100px;margin:auto;padding:0 16px}header{padding-top:20px}h1{margin:0 0 4px;font-size:22px}
.sub{color:var(--mute);font-size:14px}a{color:var(--acc);word-break:break-word}
.bar{position:sticky;top:0;z-index:5;background:var(--bg);padding:10px 0 6px}
input[type=search]{width:100%;padding:12px 14px;border:1px solid var(--line);border-radius:10px;background:var(--card);color:var(--ink);font-size:16px}
.stats{display:flex;gap:8px;flex-wrap:wrap;margin:10px 0}.stat{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:8px 12px;font-size:13px}
.stat b{display:block;font-size:18px}
button{min-height:44px;padding:8px 14px;border-radius:10px;border:1px solid var(--line);background:var(--card);color:var(--ink);font-size:15px;cursor:pointer}
button.pri{background:var(--acc);color:#fff;border-color:var(--acc)}
#cmpbar{display:none;gap:8px;align-items:center;flex-wrap:wrap;margin-top:8px}
.co{background:var(--card);border:1px solid var(--line);border-radius:12px;margin:8px 0}
.co>summary{cursor:pointer;padding:12px 14px;display:flex;gap:8px 12px;align-items:center;flex-wrap:wrap;list-style:none;min-height:48px}
.co>summary::-webkit-details-marker{display:none}.nm{font-weight:650}.org{color:var(--mute);font-variant-numeric:tabular-nums;font-size:14px}
.pill{font-size:12px;border-radius:99px;padding:2px 9px;border:1px solid var(--line);color:var(--mute)}
.pill.active{color:var(--ok);border-color:var(--ok)}.pill.bad{color:var(--bad);border-color:var(--bad)}
.sel{margin-left:auto;display:flex;align-items:center;gap:6px;font-size:14px;color:var(--mute);min-height:44px;padding:0 4px}
.sel input{width:22px;height:22px}
.body{padding:0 14px 14px;border-top:1px solid var(--line)}
h3{font-size:13px;text-transform:uppercase;letter-spacing:.04em;color:var(--mute);margin:16px 0 6px}
ul{margin:0;padding-left:20px}li{margin:4px 0}.sev-high{color:var(--bad)}.sev-medium{color:var(--warn)}
.fact{display:grid;grid-template-columns:minmax(140px,32%) 1fr;gap:4px 12px;padding:8px 0;border-top:1px solid var(--line)}
.fact .k{color:var(--mute);font-size:14px}.fact .v{word-break:break-word}
@media (max-width:640px){.fact{grid-template-columns:1fr}.fact .k{font-size:13px}}
details.src{margin-top:4px}details.src>summary{cursor:pointer;color:var(--acc);font-size:14px;min-height:32px;display:inline-flex;align-items:center}
.prov{background:var(--code);border-radius:8px;padding:8px 10px;margin-top:4px;font-size:13px}
.prov dt{color:var(--mute);margin-top:4px}.prov dd{margin:0;word-break:break-all}
.prov pre{white-space:pre-wrap;word-break:break-word;margin:2px 0 0;font:12px/1.45 ui-monospace,Menlo,Consolas,monospace}
.cmpwrap{overflow-x:auto;-webkit-overflow-scrolling:touch;margin:10px 0;border:1px solid var(--line);border-radius:12px;background:var(--card)}
.cmp{border-collapse:collapse;font-size:14px;min-width:100%}.cmp th,.cmp td{padding:8px 10px;border-top:1px solid var(--line);text-align:left;vertical-align:top;min-width:150px}
.cmp th:first-child,.cmp td:first-child{position:sticky;left:0;background:var(--card);color:var(--mute);min-width:120px;z-index:1}
.more{margin:12px 0 40px;text-align:center}#cmp,.co{scroll-margin-top:140px}.cmp tr:first-child th{position:sticky;top:0;background:var(--card);z-index:2}
</style></head><body>
<header><h1>Norway Company Profiles</h1>
<div class="sub">Every fact shows where it came from: tap <b>Where this came from</b> to see the source, the time it was retrieved, the SHA-256 hash of the retrieved content and the exact quoted text. Generated __GENERATED__.</div></header>
<main>
<div class="stats" id="stats"></div>
<div class="bar"><input id="q" type="search" placeholder="Find a company: name, organisation number, municipality or industry" autocomplete="off" aria-label="Find a company">
<div id="cmpbar"><span class="sub"><b id="cmpn">0</b> selected</span><button class="pri" id="cmpgo">Compare</button><button id="cmpclear">Clear</button></div></div>
<div id="cmp"></div>
<div id="list"></div><div class="more"><button id="more">Show more</button></div>
</main>
<script id="data" type="application/json">__DATA__</script>
<script>
const D=JSON.parse(document.getElementById('data').textContent);
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const num=a=>Number(a).toLocaleString('nb-NO').replace(/ /g,' ');
const fmtv=v=>{if(v&&typeof v==='object'&&'amount'in v)return (v.currency?v.currency+' '+num(v.amount):num(v.amount)+' (currency not stated)')+' ('+v.period+')';
 if(Array.isArray(v))return v.map(x=>x&&typeof x==='object'?(x.name||x.title||x.text||x.exchange||JSON.stringify(x)):x).slice(0,10).join('; ');
 if(v&&typeof v==='object')return v.name||Object.entries(v).map(([k,x])=>k+': '+x).join(', ');
 if(v===true)return 'Yes';if(v===false)return 'No';return String(v)};
function prov(ref,extra){if(!ref)return '';
 return `<details class="src"><summary>Where this came from</summary><dl class="prov">
 ${extra||''}<dt>Retrieved content</dt><dd><a href="${esc(ref.url)}" target="_blank" rel="noopener">${esc(ref.url)}</a></dd>
 <dt>Retrieved at</dt><dd>${esc(ref.retrieved_at||'')}</dd>
 <dt>SHA-256 of retrieved content</dt><dd>${esc(ref.content_sha256||'not retained')}</dd>
 <dt>Exact quoted text (${esc((ref.quote_status||'').replace(/_/g,' '))})</dt><dd><pre>${esc(ref.quote||'—')}</pre></dd>
 ${ref.basis?`<dt>How it was derived</dt><dd>${esc(ref.basis)}</dd>`:''}</dl></details>`}
const factRow=f=>`<div class="fact"><div class="k">${esc(f.label)}</div><div class="v">${esc(fmtv(f.value))}${f.as_of?` <span class="org">· as of ${esc(f.as_of)}</span>`:''}
 ${prov(f.reference,`<dt>Source</dt><dd>${esc(f.source_name||'')}</dd><dt>Public page</dt><dd><a href="${esc(f.evidence_url)}" target="_blank" rel="noopener">${esc(f.evidence_url)}</a></dd>${f.match?`<dt>Why it is this company</dt><dd>${esc(f.match)}</dd>`:''}${f.note?`<dt>Note</dt><dd>${esc(f.note)}</dd>`:''}`)}</div></div>`;
const st=document.getElementById('stats');
const n=D.length,withNews=D.filter(p=>(p.news||[]).length).length,facts=D.reduce((a,p)=>a+(p.facts||[]).length,0);
st.innerHTML=[['Companies',n],['Avg. facts',n?(facts/n).toFixed(1):0],['With dated news',withNews]].map(([k,v])=>`<div class="stat"><b>${v}</b>${k}</div>`).join('');
let shown=30,rows=D;const SEL=new Set();
function card(p,open){const s=p.status||'';const cls=s==='active'?'active':(['bankrupt','deleted','under_liquidation','compulsory_liquidation'].includes(s)?'bad':'');
 const news=(p.news||[]).slice(0,12).map(i=>`<li>${esc(i.published)} · <a href="${esc(i.url)}" target="_blank" rel="noopener">${esc(i.title)}</a>${i.title_en?` <span class="org">(English: ${esc(i.title_en)})</span>`:''} <span class="org">— ${esc(i.source)}</span>
   ${prov(i.reference,`<dt>Why it is this company</dt><dd>${esc(i.company_match)}</dd>`)}</li>`).join('');
 const risks=(p.risk_signals||[]).map(r=>`<li class="sev-${r.severity}">${esc(r.signal)} <span class="org">(${esc(r.evidence)})</span></li>`).join('');
 const kp=(p.key_points||[]).map(k=>`<li>${esc(k)}</li>`).join('');
 const fs=(p.facts||[]).filter(f=>f.category!=='news');
 const gaps=(p.gaps||[]).map(g=>`<li><b>${esc(g.topic)}:</b> ${esc(g.reason)}</li>`).join('');
 const con=p.conclusion?`<h3>Conclusion — ${esc(p.conclusion.verdict)}</h3><ul>${p.conclusion.claims.map(c=>`<li>${esc(c.claim)} <span class="org">[based on: ${esc(c.supported_by.map(x=>x.field).join(', '))}]</span></li>`).join('')}</ul>`:'';
 return `<details class="co" id="c${esc(p.organisation_number)}" ${open?'open':''}><summary><span class="nm">${esc(p.name||'(unknown)')}</span><span class="org">${esc(p.organisation_number||'')}</span><span class="pill ${cls}">${esc(s.replace(/_/g,' '))}</span>${(p.news||[]).length?`<span class="pill">${p.news.length} dated items</span>`:''}<label class="sel" onclick="event.stopPropagation()"><input type="checkbox" data-org="${esc(p.organisation_number)}" ${SEL.has(p.organisation_number)?'checked':''}> Compare</label></summary>
 <div class="body">${con}<h3>Summary</h3><p>${esc(p.summary)}</p>${kp?`<h3>Key points</h3><ul>${kp}</ul>`:''}${risks?`<h3>Risk signals</h3><ul>${risks}</ul>`:''}
 ${news?`<h3>Dated news &amp; official announcements</h3><ul>${news}</ul>`:''}<h3>Facts (${fs.length}) — tap “Where this came from” on any fact</h3>${fs.map(factRow).join('')}
 ${gaps?`<h3>Not found, and why</h3><ul>${gaps}</ul>`:''}</div></details>`}
function render(){const one=rows.length===1;document.getElementById('list').innerHTML=rows.slice(0,shown).map(p=>card(p,one)).join('')||'<p class="sub">No company matches that search.</p>';
 document.getElementById('more').style.display=rows.length>shown?'':'none'}
const norm=s=>String(s||'').toLowerCase().replace(/\s/g,'');
function search(q){q=norm(q);shown=30;rows=!q?D:D.filter(p=>norm((p.name||'')+(p.organisation_number||'')+JSON.stringify((p.profile||{}).location||'')+JSON.stringify((p.profile||{}).activity||'')).includes(q));render()}
document.getElementById('q').addEventListener('input',e=>search(e.target.value));
document.getElementById('more').onclick=()=>{shown+=30;render()};
const SELBAR=document.getElementById('cmpbar');
function updSel(){document.getElementById('cmpn').textContent=SEL.size;SELBAR.style.display=SEL.size?'flex':'none'}
document.getElementById('list').addEventListener('change',e=>{const o=e.target.dataset&&e.target.dataset.org;if(!o)return;e.target.checked?SEL.add(o):SEL.delete(o);updSel()});
const fact=(p,f)=>(p.facts||[]).find(x=>x.field===f);
const cell=(p,fields)=>{for(const f of fields){const x=fact(p,f);if(x)return `${esc(fmtv(x.value))}<br><a class="org" href="#c${esc(p.organisation_number)}" onclick="openFact('${esc(p.organisation_number)}')">source ›</a>`}return '<span class="org">not found</span>'};
const ROWS=[['Status',p=>esc((p.status||'').replace(/_/g,' '))],['Verdict',p=>esc(p.conclusion?p.conclusion.verdict:'—')],
 ['Legal form',p=>cell(p,['legal_form'])],['Municipality',p=>cell(p,['municipality'])],['Industry',p=>cell(p,['industry_code_1'])],
 ['Registered',p=>cell(p,['registered_date','start_date'])],['Employees',p=>cell(p,['employees','employee_count_registered'])],
 ['Accounting period',p=>cell(p,['accounts_period'])],['Revenue',p=>cell(p,['revenue'])],['Net income',p=>cell(p,['net_income'])],
 ['Equity',p=>cell(p,['equity'])],['Total assets',p=>cell(p,['total_assets'])],['CEO',p=>cell(p,['ceo'])],['Board chair',p=>cell(p,['board_chair'])],
 ['Auditor',p=>cell(p,['auditor'])],['Website',p=>cell(p,['website','website_discovered'])],
 ['Dated items',p=>(p.news||[]).length],['Most recent dated item',p=>p.news&&p.news[0]?esc(p.news[0].published+' · '+p.news[0].title):'—'],
 ['Risk signals',p=>esc((p.risk_signals||[]).map(r=>r.signal).join('; ')||'none found')]];
function openFact(org){const el=document.getElementById('c'+org);if(!el){search(org);document.getElementById('q').value=org;setTimeout(()=>openFact(org),0);return}el.open=true}
document.getElementById('cmpgo').onclick=()=>{const ps=D.filter(p=>SEL.has(p.organisation_number));
 if(ps.length<2){document.getElementById('cmp').innerHTML='<p class="sub">Tick at least two companies to compare.</p>';return}
 document.getElementById('cmp').innerHTML=`<div class="cmpwrap"><table class="cmp"><tr><th>Compare</th>${ps.map(p=>`<th>${esc(p.name)}<br><span class="org">${esc(p.organisation_number)}</span></th>`).join('')}</tr>`+
 ROWS.map(([k,f])=>`<tr><td>${k}</td>${ps.map(p=>`<td>${f(p)}</td>`).join('')}</tr>`).join('')+'</table></div><p class="sub">Every value is a sourced fact; tap “source ›” to open that company and see where it came from.</p>';
 document.getElementById('cmp').scrollIntoView({behavior:'smooth',block:'start'})};
document.getElementById('cmpclear').onclick=()=>{SEL.clear();document.getElementById('cmp').innerHTML='';updSel();render()};
function fromHash(){const h=decodeURIComponent(location.hash.slice(1)).replace(/^c/,'');if(/^\d{9}$/.test(h)){document.getElementById('q').value=h;search(h)}}
window.addEventListener('hashchange',fromHash);fromHash();render();
</script></body></html>"""

KEEP = ("organisation_number", "name", "status", "summary", "conclusion", "key_points", "risk_signals", "news", "gaps", "profile")
FACT_KEYS = ("field", "label", "category", "value", "as_of", "evidence_url", "source_name", "match", "note", "reference")


def write_report(path, profiles, generated):
    slim = []
    for p in profiles:
        d = {k: p.get(k) for k in KEEP}
        d["facts"] = [{k: f.get(k) for k in FACT_KEYS if f.get(k) is not None} for f in p.get("facts", [])]
        slim.append(d)
    data = json.dumps(slim, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(TEMPLATE.replace("__GENERATED__", html.escape(generated)).replace("__DATA__", data))
