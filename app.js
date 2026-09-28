/* Thin terminal client: the Python reducer owns all calculations and persistence. */
const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];
const esc = (x) => String(x ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const pct = (n) => Number.isFinite(n) ? `${(n*100).toFixed(2)}%` : '--';
const num = (n,d=2) => Number.isFinite(n) ? n.toFixed(d) : '--';
const date = (n) => Number.isFinite(n) ? new Date(n).toISOString().slice(5,19).replace('T',' ') : '--';
const gap = (n) => Number.isFinite(n) ? `${n>=0?'+':''}${n.toFixed(2)}` : '--';
const money=n=>Number.isFinite(n)?n.toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2}):'--';
const state = {view:'monitor', data:null, catalog:[], extras:[], rows:[], selected:'', eventId:'', page:0, size:50, search:'', paused:false, detail:false, busy:false, focus:null, catalogTime:0, extraTime:0, request:0, tapeBefore:null};
const views=['monitor','markets','replay','episodes','algos','curves','health','wallets','positions','history'];
const remoteViews=['replay','episodes','algos','wallets','positions','history'];
const chart={data:null,meta:null,series:[],x0:0,x1:1,cursor:null};
async function request(path, payload) {
  const response=await fetch(path, {method:payload?'POST':'GET',headers:payload?{'Content-Type':'application/json'}:{},body:payload?JSON.stringify(payload):undefined,signal:AbortSignal.timeout(12000),cache:'no-store'});
  const data=await response.json(); if(!response.ok) throw new Error(data.error || `HTTP ${response.status}`); return data;
}
function message(text) { $('#status').textContent=text; }
async function poll(force=false) {
  if(state.busy || (state.paused && !force)) return;
  state.busy=true;
  try {
    state.data=await request('/api/state');
    if(Date.now()-state.catalogTime>180000) {state.catalog=(await request('/api/catalog')).rows; state.catalogTime=Date.now();}
    if(remoteViews.includes(state.view) && Date.now()-state.extraTime>3000) await loadExtra();
    if(state.detail&&state.eventId&&(!state.thesisTime||Date.now()-state.thesisTime>10000||state.thesis?.event_id!==state.eventId)){
      state.thesis=(await request('/api/theses?event_id='+encodeURIComponent(state.eventId))).rows[0];state.thesisTime=Date.now();
    }
    if(!state.eventId && state.data.rows.length) state.eventId=state.data.rows[0].event_id;
    render();
    message(state.data.collector.running?'':`RECORDER STOPPED: ${state.data.collector.failure || 'collection disabled'}`);
  } catch(error) { message(`CONNECTION LOST: ${error.message}. Displayed values frozen; retrying.`); $('#sources').textContent='SERVICE UNREACHABLE — cached display only'; $('#summary').textContent='CACHED DISPLAY | recorder status unknown'; }
  finally {state.busy=false;}
}
async function loadExtra() {
  const serial=++state.request, view=state.view;
  if(!remoteViews.includes(view)) return;
  let path='/api/'+view;
  if(view==='replay') {
    const params=new URLSearchParams({limit:'200',tail:'1'});
    if($('#scope').value==='selected' && state.eventId) params.set('event_id',state.eventId);
    if(state.tapeBefore) params.set('before',String(state.tapeBefore));
    path+='?'+params;
  }
  if(['positions','history'].includes(view)) path+='?wallet='+encodeURIComponent($('#paper-wallet').value);
  const data=await request(path);
  if(serial===state.request && view===state.view) {
    state.extras=data.rows; state.extraTime=Date.now();
    if(view==='wallets'){
      chart.meta=data;
      const runSelect=$('#chart-run'),chosen=runSelect.value,selectedWallet=$('#chart-wallet').value;
      const runOptions='<option value="">Current runs</option>'+data.runs.filter(r=>selectedWallet!=='all'&&r.wallet_id===selectedWallet).map(r=>`<option value="${r.id}">R${r.run_number} ${esc(r.strategy_version)}</option>`).join('');
      if(runSelect.innerHTML!==runOptions){runSelect.innerHTML=runOptions;if([...runSelect.options].some(o=>o.value===chosen))runSelect.value=chosen;}
      const params=new URLSearchParams({hours:$('#chart-hours').value});if(runSelect.value)params.set('run_id',runSelect.value);
      const equity=await request('/api/equity?'+params);
      if(serial===state.request && view===state.view)chart.data=equity;
    }
  }
}
function eventFor(row) {return row?.event_id || row?.market_scope || null;}
function rowKey(row,index) {return String(row.ledger_id || row.observation_id || row.gap_event_id || row.wallet_id || row.instrument || (row.algo_name?`${row.market_scope}:${row.algo_name}`:row.event_id) || row.key || index);}
function modelRows() {
  const data=state.data; if(!data) return [];
  let rows=state.view==='monitor'?data.rows:state.view==='markets'?state.catalog:state.view==='curves'?data.curves:state.view==='health'?Object.entries(data.sources).map(([key,value])=>({key,...value})):state.extras;
  if(['episodes','algos'].includes(state.view) && $('#scope').value==='selected' && state.eventId) rows=rows.filter(r=>eventFor(r)===state.eventId);
  const contexts=new Map(state.catalog.map(r=>[r.event_id,r]));
  rows=rows.filter(r=>{const context=contexts.get(eventFor(r));return !state.search || JSON.stringify([r.asset||context?.asset,r.event_text||context?.event_text,r.event_id,r.algo_name,r.explanation,r.key,r.group,r.wallet_id,r.instrument,r.order?.instrument,r.kind]).toLowerCase().includes(state.search);});
  if(state.view==='monitor') {
    const mode=$('#mode').value;
    const assetClass=$('#asset-class').value;
    rows=rows.filter(r=>assetClass==='all'||(assetClass==='crypto')===['BTC','ETH'].includes(r.asset));
    rows=rows.filter(r=>mode==='all' || (mode==='usable' && r.gap_pp!=null) || (mode==='blocked' && r.gap_pp==null) || (mode==='tail' && r.quality_flags.includes('EXTREME_TAIL_MODEL_SENSITIVITY')) || r.event_type===mode);
    rows=[...rows].sort((a,b)=>{
      const missing=Number(a.gap_pp==null)-Number(b.gap_pp==null); if(missing) return missing;
      const sort=$('#sort').value;
      return sort==='rel'?(b.relative_gap??-1)-(a.relative_gap??-1):sort==='end'?a.expiry-b.expiry:sort==='asset'?a.asset.localeCompare(b.asset):Math.abs(b.gap_pp??0)-Math.abs(a.gap_pp??0);
    });
    if($('#rows').contains(document.activeElement)) {
      const order=new Map(state.rows.map((r,i)=>[rKey(r,i),i]));
      rows.sort((a,b)=>(order.get(rKey(a,0))??Infinity)-(order.get(rKey(b,0))??Infinity));
    }
  }
  return rows;
}
const rKey=rowKey;
function columns() {
  return {
    monitor:['#','ASSET','EVENT','END UTC','PM','OPT','GAP pp','REL','SIDE','TREND','BASIS','STATE'],
    markets:['#','ASSET','EVENT','END UTC','PM IND','TYPE','THRESHOLD','REVIEW'],
    replay:['RECEIVED UTC','EVENT','PM','OPT','SPOT','GAP pp','REL','TREND','STATE','RAW'],
    episodes:['EPISODE','EVENT','OPEN UTC','GAP','PEAK pp','DURATION s','PM TRAVEL','OPT TRAVEL','STATE'],
    algos:['FAMILY','ANALYZER','EVENT','AS OF UTC','SCORE','STATE','EXPLANATION'],
    curves:['ASSET','TYPE','DIRECTION','END UTC','STRIKES','PM/OPT VIOLATIONS','CLUSTERS'],
    health:['SOURCE','STATE','LAST CHECK UTC','LAST OK UTC','DETAIL'],
    wallets:['WALLET','RUN','EQUITY USD','ACCT RETURN','MAX DD','ALL COSTS','FILLS','POLICY'],
    positions:['INSTRUMENT','QTY','VALUE USD','UNREALIZED USD','MARK','SETTLEMENT'],
    history:['UTC','ACTION','INSTRUMENT','QTY','FILL USD','FEES USD','REASON'],
  }[state.view];
}
function cells(r,i) {
  const trend=r.features?.trend || '--'; const symbol={'OPENING FAST':'>>','OPENING':'>','STABLE':'=','CLOSING':'<','CLOSING FAST':'<<','REVERSING':'REV'}[trend]||'--';
  if(state.view==='monitor') return [i+1,r.asset,`${r.strike_or_threshold?.toLocaleString('en-US')} ${r.direction==='up'?'↑':'↓'} ${r.event_type==='touch'?'touch':'at end'}`,date(r.expiry).slice(0,11),pct(r.pm_yes),pct(r.opt_yes),gap(r.gap_pp),pct(r.relative_gap),r.side||'--',symbol,r.basis_method||'--',r.source_state];
  if(state.view==='markets') return [i+1,r.asset||'--',r.event_text,date(r.expiry).slice(0,11),pct(r.pm_indicative),r.event_type,r.strike_or_threshold??'--',r.mapping_review];
  if(state.view==='replay') return [date(r.timestamp_wall),r.event_id,pct(r.pm_yes),pct(r.opt_yes),num(r.spot),gap(r.gap_pp),pct(r.relative_gap),symbol,r.source_state,r.raw_id];
  if(state.view==='episodes') return [r.gap_event_id,r.event_id,date(r.opened_at),gap(r.opening_gap_pp),num(r.max_abs_gap_pp),num(r.duration_ms/1000,0),pct(r.pm_travel),pct(r.opt_travel),r.state];
  if(state.view==='algos') return [r.family,r.algo_name,r.market_scope,date(r.timestamp),num(r.raw_score,4),r.status,r.explanation];
  if(state.view==='curves') return [r.group.asset,r.group.event_type,r.group.direction,date(r.group.expiry),r.nodes.length,r.monotonicity_violations.length,r.same_direction_clusters.length];
  if(state.view==='wallets') return [r.wallet_id,r.run_number,money(r.equity),gap(r.account_return_pct??r.return_pct)+'%',pct(r.account_max_drawdown??r.max_drawdown),money(r.account_costs??(r.fees_paid+r.slippage_paid)),r.account_trades??r.trades,r.bankrupt?'BANKRUPT':r.policy_state+(r.stale_marks?` | ${r.stale_marks} STALE MARKS`:'')];
  if(state.view==='positions') return [r.instrument,num(r.quantity,6),money(r.market_value),money(r.unrealized_pnl),r.mark_status,r.settlement_model];
  if(state.view==='history') return [date(r.timestamp),r.kind,r.order?.instrument||r.instrument||'--',r.order?.quantity??'--',money(r.fill?.fill_price),money(r.fill?.fees),r.reason||r.order?.reason||''];
  return [r.key,r.state,date(r.timestamp),date(r.last_success_ms),r.detail];
}
function render() {
  if(!state.data) return;
  const data=state.data;
  $('#sources').innerHTML=Object.entries(data.sources).filter(([k])=>['pm','catalog','spot','yahoo'].includes(k)||k.startsWith('options:')).map(([k,v])=>`<span>${esc(k.toUpperCase())} <b>${esc(v.state)}</b></span>`).join('')+Object.entries(data.spots).map(([k,v])=>`<span>${esc(k)} <b>${num(v.price)}</b></span>`).join('');
  $('#summary').textContent=`${state.paused?'DISPLAY PAUSED':data.collector.running?'RECORDING':'RECORDER STOPPED'} | ${data.rows.length} events | ${data.stats.raw} raw | ${data.stats.observations} observations | ${data.stats.analyzers} diagnostics`;
  const all=modelRows(); state.page=Math.min(state.page,Math.max(0,Math.ceil(all.length/state.size)-1));
  state.rows=all; const start=state.page*state.size, shown=all.slice(start,start+state.size);
  if(!shown.some((r,i)=>rowKey(r,start+i)===state.selected)) state.selected=shown.length?rowKey(shown[0],start):'';
  if(['monitor','markets'].includes(state.view)) {const selected=shown.find((r,i)=>rowKey(r,start+i)===state.selected);if(selected)state.eventId=selected.event_id;}
  $('#context').textContent=state.eventId?`EVENT ${state.eventId}`:'';
  $('#table').dataset.view=state.view;
  $('#head').innerHTML='<tr>'+columns().map(c=>`<th>${esc(c)}</th>`).join('')+'</tr>';
  const current=new Map([...$('#rows').children].map(r=>[r.dataset.key,r]));
  shown.forEach((r,j)=>{
    const key=rowKey(r,start+j), selected=key===state.selected;
    let tr=current.get(key); if(!tr) {tr=document.createElement('tr'); tr.dataset.key=key;}
    const wasFocused=tr.contains(document.activeElement), values=cells(r,start+j), main=state.view==='monitor'||state.view==='markets'?2:0;
    const html=values.map((v,k)=>`<td${k===main?' class="event"':''}${state.view==='wallets'&&k===7?` title="${esc(v)}"`:''}>${k===main?`<button class="row-select" tabindex="${selected?0:-1}" aria-expanded="${selected&&state.detail}" title="${esc(r.event_text||v)}">${esc(v)}</button>`:esc(v)}</td>`).join('');
    if(tr.dataset.html!==html) {tr.innerHTML=html; tr.dataset.html=html;}
    tr.className=selected?'selected':'';
    if(state.view==='monitor' && r.gap_pp!=null && Math.abs(r.gap_pp)>=Number($('#cutoff').value)) tr.classList.add('signal');
    if($('#rows').children[j]!==tr) $('#rows').insertBefore(tr,$('#rows').children[j]||null);
    if(wasFocused) tr.querySelector('button').focus({preventScroll:true}); current.delete(key);
  });
  for(const row of current.values()) row.remove();
  if(!shown.length) $('#rows').innerHTML=`<tr><td colspan="${columns().length}">No records yet for this view/filter. The recorder continues independently.</td></tr>`;
  $('#page-info').textContent=`${shown.length?start+1:0}–${start+shown.length} / ${all.length}`;
  $('#prev').disabled=state.page===0; $('#next').disabled=start+state.size>=all.length;
  renderDetail();
  if(state.view==='wallets')renderEquity();
}
function selectedRow() {return state.rows.find((r,i)=>rowKey(r,i)===state.selected);}
function renderDetail() {
  const row=selectedRow(); $('#detail').hidden=!state.detail||!row; if(!state.detail||!row) return;
  $('#detail-title').textContent=row.event_text||row.algo_name||row.gap_event_id||row.wallet_id||row.instrument||row.kind||row.key||'Curve';
  const refs=Object.entries(row.input_refs||{}).map(([k,id])=>`<a href="/api/raw?id=${id}" target="_blank" rel="noreferrer">${esc(k)} #${id}</a>`).join(' · ');
  const explanation=row.gap_pp!=null?`<p>GAP ${gap(row.gap_pp)} pp · REL ${pct(row.relative_gap)} · conventional exposure ${esc(row.side)} · model ${esc(row.model_confidence)} · mapping ${esc(row.mapping_confidence)}</p><p>${esc(row.features?.trend)} · PM TRAVEL ${pct(row.features?.pm_travel)} · OPT TRAVEL ${pct(row.features?.opt_travel)} · dG/dt ${num(row.features?.velocity_pp_s,4)} pp/s</p>`:'';
  const flags=row.quality_flags?`<p>${esc(row.quality_flags.join(' · '))}</p>`:'';
  const catalog=state.catalog.find(e=>e.event_id===eventFor(row));
  const review=row.performance_review;
  const thesis=state.thesis?.event_id===eventFor(row)?state.thesis:null;
  const thesisCopy=thesis?`<details><summary>Trade thesis — ${esc(thesis.status)}</summary><p>${esc(thesis.proposition)} · proposed ${esc(thesis.proposed_direction)} · ${esc(thesis.rejection_reasons.join('; ')||'Underlying context ready; still requires a diagnostic, PM movement and cost checks')}</p><p>Spot ${money(thesis.facts.spot)} · threshold distance ${num(thesis.facts.threshold_distance_pct)}% · 5m ${pct(thesis.facts.spot_returns['300'])} · 15m ${pct(thesis.facts.spot_returns['900'])} · IV ${pct(thesis.facts.local_iv)} · realized ${pct(thesis.facts.realized_vol_1h)}</p><p>${esc(thesis.interpretation)}</p><pre>${esc(JSON.stringify(thesis,null,2))}</pre></details>`:'';
  const costReview=review?`<p>ALL RUNS: ${review.closes} closes · mid move $${money(review.mid_move_pnl)} − spread $${money(review.spread_cost)} − fees $${money(review.fees)} − slippage $${money(review.slippage)} = net $${money(review.net_pnl)}</p><p>Run ${row.run_number}: ${esc(row.strategy_version)} · started $${money(row.starting_balance)} · run return ${gap(row.return_pct)}% · prior results retained. Mid move is attribution, not executable profit.</p>`:'';
  const html=explanation+flags+costReview+thesisCopy+`<p>${refs}</p>`+(catalog?.description?`<details><summary>Resolution wording</summary><p>${esc(catalog.description)}</p></details>`:'')+`<details><summary>All fields / provenance / rolling changes</summary><pre>${esc(JSON.stringify(row,null,2))}</pre></details>`;
  const body=$('#detail-body'); if(body.dataset.html!==html) {const open=[...body.querySelectorAll('details')].map(x=>x.open);body.innerHTML=html; body.dataset.html=html;body.querySelectorAll('details').forEach((x,i)=>x.open=open[i]||false);}
  $('#detail-map').hidden=!catalog; $('#detail-tape').hidden=!eventFor(row); $('#event-rules').hidden=!catalog?.event_url; $('#event-rules').href=catalog?.event_url||'#';
}
function select(delta=0,focus=true) {
  let index=state.rows.findIndex((r,i)=>rowKey(r,i)===state.selected); index=Math.max(0,Math.min(state.rows.length-1,index+delta));
  const row=state.rows[index]; if(!row) return;
  state.selected=rowKey(row,index); state.page=Math.floor(index/state.size); if(eventFor(row)) state.eventId=eventFor(row); render();
  if(focus) {const node=[...$('#rows').children].find(x=>x.dataset.key===state.selected);node?.querySelector('button')?.focus({preventScroll:true});node?.scrollIntoView({block:'nearest'});}
}
async function switchView(view) {
  state.view=view; state.page=0;state.selected='';state.detail=false;state.extraTime=0;state.tapeBefore=null;state.extras=[];state.request++;
  $$('[data-view]').forEach(b=>{b.classList.toggle('active',b.dataset.view===view);b.setAttribute('aria-pressed',String(b.dataset.view===view));});
  for(const id of ['sort','asset-class','mode','cutoff-label']) $('#'+id).hidden=view!=='monitor';
  $('#scope-label').hidden=!['replay','episodes','algos'].includes(view); $('#more-tape').hidden=view!=='replay';
  $('#wallet-label').hidden=!['positions','history'].includes(view);
  $('#wallet-chart').hidden=view!=='wallets';
  if(['wallets','positions','history'].includes(view)){state.search='';$('#search').value='';}
  try {await loadExtra();render();} catch(e) {message(e.message);}
}
function openModal(id) {state.focus=document.activeElement;$('#'+id).hidden=false;$('.terminal').inert=true;$('#'+id).querySelector('button,input,select')?.focus();}
function closeModal() {$$('.modal-backdrop').forEach(m=>m.hidden=true);$('.terminal').inert=false;if(state.focus?.isConnected) state.focus.focus();}
function fillMapping() {
  const catalog=state.catalog.find(e=>e.event_id===$('#mapping-event').value); if(!catalog) return;
  const row=state.data.rows.find(e=>e.event_id===catalog.event_id)||catalog;
  for(const [id,value] of Object.entries({'asset':row.asset,'strike':row.strike_or_threshold,'type':row.event_type==='unmapped'?'terminal':row.event_type,'direction':row.direction||'up','expiry':row.expiry?new Date(row.expiry).toISOString():'','window':row.window_start?new Date(row.window_start).toISOString():'','source':row.settlement_source||'','review':row.mapping_confidence||row.mapping_review||'UNVERIFIED','inclusive':row.threshold_inclusive==null?'unknown':String(row.threshold_inclusive)})) $('#mapping-'+id).value=value??'';
}
function openMapping() {
  $('#mapping-event').innerHTML=state.catalog.filter(r=>r.yes_token).map(r=>`<option value="${esc(r.event_id)}">${esc(r.event_text)}</option>`).join('');
  if(state.eventId) $('#mapping-event').value=state.eventId; fillMapping(); $('#mapping-message').textContent='';openModal('mapping-modal');
}
async function saveMapping(event) {
  event.preventDefault(); const catalog=state.catalog.find(r=>r.event_id===$('#mapping-event').value); if(!catalog) return;
  const value=id=>$('#mapping-'+id).value;
  try {
    if(!/Z$|[+-]\d\d:\d\d$/.test(value('expiry')) || (value('window') && !/Z$|[+-]\d\d:\d\d$/.test(value('window')))) throw new Error('Cutoff and path window need explicit timezones.');
    const expiry=Date.parse(value('expiry')), start=value('window')?Date.parse(value('window')):null;
    if(!Number.isFinite(expiry)||(start!=null&&!Number.isFinite(start))) throw new Error('Invalid ISO cutoff/window.');
    const mapping={...catalog,asset:value('asset').trim().toUpperCase(),strike_or_threshold:Number(value('strike')),event_type:value('type'),direction:value('direction'),expiry,window_start:start,settlement_source:value('source'),mapping_review:value('review'),threshold_inclusive:value('inclusive')==='unknown'?null:value('inclusive')==='true'};
    await request('/api/mapping',mapping);state.eventId=catalog.event_id;closeModal(); await poll(true);message('Mapping revision recorded; earlier tape preserved.');
  } catch(error) {$('#mapping-message').textContent=error.message;}
}
$('#search').addEventListener('input',e=>{state.search=e.target.value.toLowerCase();state.page=0;render();});
$('#search').addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();select(0);}});
for(const id of ['sort','mode','cutoff']) $('#'+id).addEventListener('change',()=>{state.page=0;render();});
$('#scope').addEventListener('change',async()=>{state.page=0;state.extraTime=0;state.tapeBefore=null;await loadExtra();render();});
$$('[data-view]').forEach(b=>b.addEventListener('click',()=>switchView(b.dataset.view)));
$('#rows').addEventListener('click',e=>{const tr=e.target.closest('[data-key]');if(tr){const same=tr.dataset.key===state.selected;state.selected=tr.dataset.key;const row=selectedRow();if(eventFor(row))state.eventId=eventFor(row);state.detail=same?!state.detail:true;render();}});
$('#prev').onclick=()=>{state.page--;state.selected='';render();}; $('#next').onclick=()=>{state.page++;state.selected='';render();};
$('#more-tape').onclick=async()=>{const oldest=state.extras[0]?.observation_id;if(oldest){state.tapeBefore=oldest;state.page=0;await loadExtra();render();}};
$('#pause').onclick=()=>{state.paused=!state.paused;$('#pause').textContent=state.paused?'[P] Resume':'[P] Pause';$('#pause').setAttribute('aria-pressed',String(state.paused));message(state.paused?'Display paused; recorder still collecting.':'Display resumed.');if(!state.paused)poll(true);else render();};
$('#refresh').onclick=async()=>{try{await request('/api/refresh',{});await poll(true);message('Catalog refresh requested.');}catch(e){message(e.message);}};
$('#help').onclick=()=>openModal('help-modal');$('#link-event').onclick=openMapping;$('#detail-map').onclick=openMapping;
$('#detail-tape').onclick=()=>switchView('replay');$('#close-detail').onclick=()=>{state.detail=false;select(0);};
$$('[data-close]').forEach(b=>b.onclick=closeModal);$('#mapping-event').onchange=fillMapping;$('#mapping-form').onsubmit=saveMapping;
document.addEventListener('keydown',e=>{
  const modal=$('.modal-backdrop:not([hidden])');
  if(modal){if(e.key==='Escape'){e.preventDefault();closeModal();}if(e.key==='Tab'){const nodes=[...modal.querySelectorAll('button,input,select')].filter(n=>!n.disabled&&n.getClientRects().length);if(e.shiftKey&&document.activeElement===nodes[0]){e.preventDefault();nodes.at(-1)?.focus();}else if(!e.shiftKey&&document.activeElement===nodes.at(-1)){e.preventDefault();nodes[0]?.focus();}}return;}
  if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='k'){e.preventDefault();$('#search').focus();return;}
  if(e.ctrlKey||e.metaKey||e.altKey||e.isComposing)return;
  const edit=e.target.matches('input,select,textarea');
  if(e.key==='Escape'){e.preventDefault();if(edit){if(e.target.id==='search'){state.search='';e.target.value='';render();}e.target.blur();}state.detail=false;select(0);return;}
  if(edit)return;const key=e.key.toLowerCase();
  if(key==='/'){e.preventDefault();$('#search').focus();}else if(['j','arrowdown','k','arrowup'].includes(key)){e.preventDefault();select(['j','arrowdown'].includes(key)?1:-1);}
  else if(key==='enter'&&(e.target.matches('.row-select')||e.target===document.body)){e.preventDefault();state.detail=!state.detail;render();}
  else if(/^[0-9]$/.test(key))switchView(views[key==='0'?9:Number(key)-1]);else if(key==='t')openTrade();else if(key==='p')$('#pause').click();else if(key==='r')$('#refresh').click();else if(key==='l')openMapping();else if(key==='?')openModal('help-modal');else if(key==='['&&!$('#prev').disabled)$('#prev').click();else if(key===']'&&!$('#next').disabled)$('#next').click();
});
let paperTicket=null;
async function openTrade(){
  $('#trade-message').textContent='Loading current instruments...';$('#trade-preview').textContent='';$('#trade-submit').disabled=true;paperTicket=null;
  $('#trade-context').textContent=state.eventId?'Context event '+state.eventId:'Choose an instrument explicitly';openModal('trade-modal');
  try{const data=await request('/api/instruments?'+new URLSearchParams({event_id:state.eventId||''}));$('#trade-instrument').innerHTML='<option value="">Choose an instrument</option>'+data.rows.map(q=>`<option value="${esc(q.instrument)}">${esc(q.instrument)} | ${money(q.bid)} / ${money(q.ask)} USD | ×${q.multiplier}${q.estimated?' EST':''}</option>`).join('');$('#trade-message').textContent='';}
  catch(e){$('#trade-message').textContent=e.message;}
}
$('#trade-open').onclick=openTrade;
$('#paper-wallet').onchange=async()=>{state.extraTime=0;await loadExtra();render();};
$('#trade-form').addEventListener('input',()=>{$('#trade-submit').disabled=true;paperTicket=null;});
$('#trade-form').onsubmit=async e=>{
  e.preventDefault();paperTicket={wallet:'OLIVER',instrument:$('#trade-instrument').value,side:$('#trade-side').value,quantity:Number($('#trade-quantity').value),reason:$('#trade-reason').value,basis_event_id:state.eventId,client_order_id:crypto.randomUUID()};
  try{const x=await request('/api/preview',paperTicket),c=x.costs;$('#trade-preview').textContent=`${x.side} ${x.quantity} ${x.quote.instrument}\nQuoted bid / ask: ${money(c.quoted_bid)} / ${money(c.quoted_ask)}\nSlippage per unit: ${money(c.slippage_per_unit)}\nEstimated fill:   ${money(c.fill_price)}\nMultiplier:       ${x.quote.multiplier}\nGross:            ${money(c.gross_premium)}\nFees:             ${money(c.fees)}\n${x.side==='BUY'?'Total debit:      ':'Net credit:       '}${money(c.total_debit??c.net_credit)} USD\n${x.quote.quote_assumption}\n${x.quote.settlement_model}\nFinal fill uses quotes after latency.`;$('#trade-submit').disabled=false;$('#trade-message').textContent='';}
  catch(error){paperTicket=null;$('#trade-submit').disabled=true;$('#trade-message').textContent=error.message;}
};
$('#trade-submit').onclick=async()=>{if(!paperTicket)return;$('#trade-submit').disabled=true;try{const x=await request('/api/trade',paperTicket);$('#trade-message').textContent='Paper order '+x.order.order_id+' queued. Inspect Ledger for fill or rejection.';paperTicket=null;state.extraTime=0;}catch(e){$('#trade-message').textContent=e.message;}};
function renderEquity(){
  const svg=$('#equity-svg');if(!chart.data)return;
  const selected=$('#chart-wallet').value,isReturn=$('#chart-unit').value==='return';
  const series=chart.data.series.filter(s=>selected==='all'||s.wallet_id===selected);chart.series=series;
  const points=series.flatMap(s=>s.points.map(p=>({...p,value:isReturn?(p.equity/s.starting_balance-1)*100:p.equity})));
  if(!points.length){svg.innerHTML='<text x="80" y="80" fill="white">Waiting for equity marks</text>';return;}
  const x0=Math.min(...points.map(p=>p.timestamp_ms)),x1=Math.max(x0+1000,...points.map(p=>p.timestamp_ms));chart.x0=x0;chart.x1=x1;
  let lo=Math.min(...points.map(p=>p.value)),hi=Math.max(...points.map(p=>p.value));const pad=Math.max((hi-lo)*.1,isReturn?.01:1);lo-=pad;hi+=pad;
  const x=t=>78+(t-x0)/(x1-x0)*902,y=v=>148-(v-lo)/(hi-lo)*128;
  let html='';for(let i=0;i<4;i++){const v=lo+(hi-lo)*i/3,py=y(v);html+=`<line x1="78" y1="${py}" x2="980" y2="${py}" stroke="#333"/><text x="70" y="${py+4}" text-anchor="end" fill="#aaa" font-size="10">${esc(isReturn?num(v,2)+'%':money(v))}</text>`;}
  const patterns=['','8 3','2 3','12 3 2 3','5 5','1 3','10 2 1 2','4 2 1 2'];
  series.forEach(s=>{const i=chart.data.series.indexOf(s),d=s.points.map((p,j)=>`${j?'L':'M'}${x(p.timestamp_ms).toFixed(2)},${y(isReturn?(p.equity/s.starting_balance-1)*100:p.equity).toFixed(2)}`).join(' ');html+=`<path d="${d}" fill="none" stroke="${i%2?'#bbb':'#fff'}" stroke-width="1.5" stroke-dasharray="${patterns[i%8]}"><title>${esc(s.wallet_id)} · run ${s.run_number}</title></path>`;});
  html+=`<text x="78" y="171" fill="#aaa" font-size="10">${esc(date(x0))} UTC</text><text x="980" y="171" text-anchor="end" fill="#aaa" font-size="10">${esc(date(x1))} UTC</text>`;
  svg.innerHTML=html;$('#automation-toggle').textContent=chart.meta?.automation_enabled?'Pause algos':'Resume algos';
  const recordingGap=chart.meta?.review_summary?.recording_gaps?.[0];
  $('#chart-note').textContent=chart.meta?.failure?'EXECUTION ERROR: '+chart.meta.failure:'Costs include spread · separate runs'+(recordingGap?` · last recording gap ${(recordingGap.duration_ms/3600000).toFixed(1)}h`:'')+' · ←/→';
  chartReadout(chart.cursor??x1);
}
function chartReadout(timestamp){
  const parts=chart.series.map(s=>{const point=s.points.filter(p=>p.timestamp_ms<=timestamp).at(-1);return point?`${s.wallet_id} R${s.run_number}: $${money(point.equity)}${point.stale_marks?' [STALE]':''}`:'';}).filter(Boolean);
  $('#chart-readout').textContent=date(timestamp)+' UTC | '+parts.join(' · ');
}
$('#equity-svg').onpointermove=e=>{const bounds=e.currentTarget.getBoundingClientRect(),fraction=Math.max(0,Math.min(1,((e.clientX-bounds.left)/bounds.width*1000-78)/902));chart.cursor=chart.x0+fraction*(chart.x1-chart.x0);chartReadout(chart.cursor);};
$('#equity-svg').onpointerleave=()=>{chart.cursor=null;chartReadout(chart.x1);};
$('#equity-svg').onkeydown=e=>{if(!['ArrowLeft','ArrowRight'].includes(e.key))return;e.preventDefault();e.stopPropagation();chart.cursor=Math.max(chart.x0,Math.min(chart.x1,(chart.cursor??chart.x1)+(e.key==='ArrowLeft'?-1:1)*(chart.x1-chart.x0)/100));chartReadout(chart.cursor);};
$('#chart-unit').onchange=()=>{chart.cursor=null;renderEquity();};
$('#chart-wallet').onchange=async()=>{$('#chart-run').value='';chart.cursor=null;state.extraTime=0;await loadExtra();renderEquity();};
$('#chart-run').onchange=async()=>{chart.cursor=null;state.extraTime=0;await loadExtra();renderEquity();};
$('#chart-hours').onchange=async()=>{chart.cursor=null;state.extraTime=0;await loadExtra();renderEquity();};
$('#automation-toggle').onclick=async()=>{try{await request('/api/automation',{enabled:!chart.meta?.automation_enabled});state.extraTime=0;await loadExtra();render();}catch(e){message(e.message);}};
$('#asset-class').onchange=()=>{state.page=0;render();};
setInterval(()=>{$('#clock').textContent=new Date().toISOString().slice(11,19)+' UTC';},1000);
setInterval(()=>poll(),1000);poll();
