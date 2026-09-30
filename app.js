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
state.connection={status:'loading'};state.catalogState={status:'loading'};state.viewState={status:'loading'};state.runs=[];
const views=['monitor','markets','replay','episodes','algos','curves','health','wallets','positions','history'];
const remoteViews=['replay','episodes','algos','wallets','positions','history'];
const chart={data:null,meta:null,series:[],x0:0,x1:1,cursor:null,status:'loading',request:0};
async function request(path, payload) {
  const response=await fetch(path, {method:payload?'POST':'GET',headers:payload?{'Content-Type':'application/json'}:{},body:payload?JSON.stringify(payload):undefined,signal:AbortSignal.timeout(12000),cache:'no-store'});
  let data;try{data=await response.json();}catch{const error=new Error(`HTTP ${response.status}: response is not JSON`);error.status=response.status;throw error;}
  if(!response.ok){const error=new Error(data.error || `HTTP ${response.status}`);error.status=response.status;error.code=data.code;throw error;}return data;
}
function message(text) { $('#status').textContent=text; }
async function poll(force=false) {
  if(state.busy || (state.paused && !force)) return;
  state.busy=true;
  try {
    state.data=await request('/api/state');
    state.connection={status:'data'};
  } catch(error) {state.connection={status:'error',error:(error.status?'SERVICE ERROR: ':'CONNECTION LOST: ')+error.message};}
  finally {state.busy=false;render();}
  // These requests have their own state and must never hold up /api/state.
  if(Date.now()-state.catalogTime>180000)void loadCatalog();
  if(remoteViews.includes(state.view) && Date.now()-state.extraTime>3000)void loadExtra();
  if(state.detail&&state.eventId&&(!state.thesisTime||Date.now()-state.thesisTime>10000||state.thesis?.event_id!==state.eventId))void loadThesis();
}
async function loadCatalog(){
  if(state.catalogBusy)return;state.catalogBusy=true;
  try{state.catalog=(await request('/api/catalog')).rows;state.catalogTime=Date.now();state.catalogState={status:'data'};}
  catch(error){state.catalogState={status:'error',error:error.message};state.catalogTime=Date.now()-170000;}
  finally{state.catalogBusy=false;render();}
}
async function loadThesis(){
  if(state.thesisBusy)return;state.thesisBusy=true;const eventId=state.eventId;
  try{const data=await request('/api/theses?event_id='+encodeURIComponent(eventId));if(eventId===state.eventId){state.thesis=data.rows[0];state.thesisError='';}}
  catch(error){if(eventId===state.eventId){state.thesis=null;state.thesisError=error.message;}}
  finally{state.thesisBusy=false;state.thesisTime=Date.now();renderDetail();}
}
function extraKey(){
  if(['replay','episodes','algos'].includes(state.view))return [state.view,$('#scope').value,$('#scope').value==='selected'?state.eventId:'',state.tapeBefore].join('|');
  return state.view==='wallets'?[state.view,$('#chart-wallet').value,$('#chart-run').value].join('|'):[state.view,$('#paper-wallet').value,$('#paper-run').value].join('|');
}
function setRunOptions(select,runs,wallet){
  const chosen=select.value,html='<option value="">Current run</option>'+runs.filter(r=>r.wallet_id===wallet).map(r=>`<option value="${r.id}">R${r.run_number} ${esc(r.strategy_version)}</option>`).join('');
  if(select.innerHTML!==html){select.innerHTML=html;if([...select.options].some(o=>o.value===chosen))select.value=chosen;}
}
function chooseWallet(wallet,run=''){$('#paper-wallet').value=wallet;setRunOptions($('#paper-run'),state.runs,wallet);$('#paper-run').value=String(run);}
async function loadExtra(force=false) {
  const view=state.view,key=extraKey();
  if(!remoteViews.includes(view)) return;
  if(state.extraLoadingKey===key&&!force)return;
  const serial=++state.request;state.extraLoadingKey=key;
  if(state.viewState.key!==key){state.extras=[];state.viewState={status:'loading',key};render();}
  const params=new URLSearchParams();
  if(['replay','episodes','algos'].includes(view)&&$('#scope').value==='selected')params.set('event_id',state.eventId);
  if(view==='replay'){params.set('limit','200');params.set('tail','1');if(state.tapeBefore)params.set('before',String(state.tapeBefore));}
  const wallet=view==='wallets'?$('#chart-wallet').value:$('#paper-wallet').value;
  const run=view==='wallets'?$('#chart-run').value:$('#paper-run').value;
  if(['wallets','positions','history'].includes(view)){if(wallet!=='all')params.set('wallet',wallet);if(run)params.set('run_id',run);}
  try{
    const data=await request('/api/'+view+'?'+params);
    if(serial!==state.request||key!==extraKey())return;
    state.extras=data.rows;state.viewState={status:'data',key,meta:data};
    if(data.runs){state.runs=data.runs;setRunOptions(view==='wallets'?$('#chart-run'):$('#paper-run'),data.runs,wallet);}
    if(view==='wallets'){chart.meta=data;void loadChart();}
  }catch(error){if(serial===state.request){state.extras=[];state.viewState={status:'error',key,error:error.message};}}
  finally{if(serial===state.request){state.extraLoadingKey=null;state.extraTime=Date.now();render();}}
}
async function loadChart(force=false){
  const key=[$('#chart-wallet').value,$('#chart-run').value,$('#chart-hours').value].join('|');
  if(chart.loadingKey===key&&!force)return;
  const serial=++chart.request;chart.loadingKey=key;
  if(chart.key!==key){chart.status='loading';chart.data=null;chart.key=key;renderEquity();}
  const params=new URLSearchParams({hours:$('#chart-hours').value});if($('#chart-run').value)params.set('run_id',$('#chart-run').value);
  try{const data=await request('/api/equity?'+params);if(serial===chart.request){chart.data=data;chart.status='data';chart.error='';}}
  catch(error){if(serial===chart.request){chart.status='error';chart.error=error.message;chart.data=null;}}
  finally{if(serial===chart.request){chart.loadingKey=null;if(state.view==='wallets')renderEquity();}}
}
function eventFor(row) {return row?.event_id || row?.market_scope || null;}
function rowKey(row,index) {return String(row.ledger_id || row.observation_id || row.gap_event_id || row.wallet_id || row.instrument || (row.algo_name?`${row.market_scope}:${row.algo_name}`:row.event_id) || row.key || index);}
function modelRows() {
  const data=state.data||{rows:[],curves:[],sources:{}};
  let rows=state.view==='monitor'?data.rows:state.view==='markets'?state.catalog:state.view==='curves'?data.curves:state.view==='health'?Object.entries(data.sources).map(([key,value])=>({key,...value})):state.extras;
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
function viewStatus(){
  if(remoteViews.includes(state.view))return state.viewState.key===extraKey()?state.viewState:{status:'loading'};
  return state.view==='markets'?state.catalogState:state.connection;
}
function emptyReason(){
  if(state.search)return `No ${state.view} rows match “${state.search}”. Clear the filter to see this scope.`;
  const selected=['replay','episodes','algos'].includes(state.view)&&$('#scope').value==='selected';
  const meta=remoteViews.includes(state.view)?state.viewState.meta||{}:{},global=meta.global_count;
  if(selected)return `${state.eventId?'Selected event '+state.eventId+':':'No selected event. Select a market or choose All events.'} ${meta.empty_reason||'No records yet.'}${global?` ${global.toLocaleString()} records exist globally.`:''}`;
  return meta.empty_reason||({monitor:'No monitored events match these filters. Check Markets and Health for mapping/source reasons.',markets:'The catalog has no markets yet. Check Health for discovery errors.',curves:'No comparable strike groups yet; curves require matching event semantics.',health:'No source health reports yet.',wallets:'No wallets in this selection.',positions:'No positions in the selected wallet/run.',history:'No order or trade activity in the selected wallet/run.'}[state.view])||'No records in this scope yet.';
}
function render() {
  const data=state.data;
  if(data){
    $('#sources').innerHTML=Object.entries(data.sources).filter(([k])=>['pm','catalog','spot','yahoo'].includes(k)||k.startsWith('options:')).map(([k,v])=>`<span>${esc(k.toUpperCase())} <b>${esc(v.state)}</b></span>`).join('')+Object.entries(data.spots).map(([k,v])=>`<span>${esc(k)} <b>${num(v.price)}</b></span>`).join('');
    $('#summary').textContent=`${state.connection.status==='error'?'CACHED STATE':state.paused?'DISPLAY PAUSED':data.collector.running?'RECORDING':'RECORDER STOPPED'} | ${data.rows.length} events | ${data.stats.raw} raw | ${data.stats.observations} observations | ${data.stats.analyzers} diagnostics`;
  }
  $('#connection-status').textContent=state.connection.status==='error'?state.connection.error:state.connection.status==='loading'?'CONNECTING':data&&!data.collector.running?`RECORDER STOPPED: ${data.collector.failure||'collection disabled'}`:'';
  const status=viewStatus(),all=status.status==='data'?modelRows():[];
  state.page=Math.min(state.page,Math.max(0,Math.ceil(all.length/state.size)-1));
  state.rows=all; const start=state.page*state.size, shown=all.slice(start,start+state.size);
  if(!shown.some((r,i)=>rowKey(r,start+i)===state.selected)) state.selected=shown.length?rowKey(shown[0],start):'';
  if(['monitor','markets'].includes(state.view)) {const selected=shown.find((r,i)=>rowKey(r,start+i)===state.selected);if(selected)state.eventId=selected.event_id;}
  $('#context').textContent=state.eventId&&['monitor','markets','replay','episodes','algos'].includes(state.view)?`EVENT ${state.eventId}`:'';
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
  const phase=status.status==='loading'?'LOADING':status.status==='error'?'ERROR':shown.length?'DATA':'EMPTY';
  $('#table').dataset.state=phase;$('#view-state').textContent=phase;
  if(!shown.length)$('#rows').innerHTML=`<tr><td class="view-message" colspan="${columns().length}">${esc(phase==='LOADING'?'LOADING '+state.view+'…':phase==='ERROR'?'ERROR: '+status.error:emptyReason())}</td></tr>`;
  $('#show-all').hidden=!(phase==='EMPTY'&&['replay','episodes','algos'].includes(state.view)&&$('#scope').value==='selected'&&state.viewState.meta?.global_count>0);
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
  const walletCopy=state.view==='wallets'?`<p>${esc(row.wallet_id)} R${row.run_number} · ${esc(row.strategy_version)} · ${row.archived?'ARCHIVED — retained marks':'CURRENT RUN'}</p><p>Cash $${money(row.cash)} · positions $${money(row.market_value)} · equity $${money(row.equity)} · run return ${gap(row.return_pct)}%</p><p>Run costs: spread $${money(row.spread_paid)} · slippage $${money(row.slippage_paid)} · fees $${money(row.fees_paid)} · ${row.open_positions} open positions · ${row.trades} fills</p><p>${esc(row.policy_state)}. Positions and Ledger will use this wallet/run.</p>`:'';
  const thesis=state.thesis?.event_id===eventFor(row)?state.thesis:null;
  const thesisCopy=thesis?`<details><summary>Trade thesis — ${esc(thesis.status)}</summary><p>${esc(thesis.proposition)} · proposed ${esc(thesis.proposed_direction)} · ${esc(thesis.rejection_reasons.join('; ')||'Underlying context ready; still requires a diagnostic, PM movement and cost checks')}</p><p>Spot ${money(thesis.facts.spot)} · threshold distance ${num(thesis.facts.threshold_distance_pct)}% · 5m ${pct(thesis.facts.spot_returns['300'])} · 15m ${pct(thesis.facts.spot_returns['900'])} · IV ${pct(thesis.facts.local_iv)} · realized ${pct(thesis.facts.realized_vol_1h)}</p><p>${esc(thesis.interpretation)}</p><pre>${esc(JSON.stringify(thesis,null,2))}</pre></details>`:'';
  const costReview=review?`<p>ALL RUNS: ${review.closes} closes · mid move $${money(review.mid_move_pnl)} − spread $${money(review.spread_cost)} − fees $${money(review.fees)} − slippage $${money(review.slippage)} = net $${money(review.net_pnl)}</p><p>Run ${row.run_number}: ${esc(row.strategy_version)} · started $${money(row.starting_balance)} · run return ${gap(row.return_pct)}% · prior results retained. Mid move is attribution, not executable profit.</p>`:'';
  const html=explanation+flags+walletCopy+costReview+thesisCopy+(state.thesisError&&eventFor(row)?`<p>Thesis error: ${esc(state.thesisError)}</p>`:'')+`<p>${refs}</p>`+(catalog?.description?`<details><summary>Resolution wording</summary><p>${esc(catalog.description)}</p></details>`:'')+`<details><summary>All fields / provenance / rolling changes</summary><pre>${esc(JSON.stringify(row,null,2))}</pre></details>`;
  const body=$('#detail-body'); if(body.dataset.html!==html) {const open=[...body.querySelectorAll('details')].map(x=>x.open);body.innerHTML=html; body.dataset.html=html;body.querySelectorAll('details').forEach((x,i)=>x.open=open[i]||false);}
  $('#detail-map').hidden=!catalog; $('#detail-tape').hidden=!eventFor(row); $('#event-rules').hidden=!catalog?.event_url; $('#event-rules').href=catalog?.event_url||'#';
}
function select(delta=0,focus=true) {
  let index=state.rows.findIndex((r,i)=>rowKey(r,i)===state.selected); index=Math.max(0,Math.min(state.rows.length-1,index+delta));
  const row=state.rows[index]; if(!row) return;
  state.selected=rowKey(row,index); state.page=Math.floor(index/state.size); if(eventFor(row)) state.eventId=eventFor(row); render();
  if(state.view==='wallets')chooseWallet(row.wallet_id,$('#chart-run').value);
  if(focus) {const node=[...$('#rows').children].find(x=>x.dataset.key===state.selected);node?.querySelector('button')?.focus({preventScroll:true});node?.scrollIntoView({block:'nearest'});}
}
async function switchView(view) {
  state.view=view; state.page=0;state.selected='';state.detail=false;state.extraTime=0;state.tapeBefore=null;state.extras=[];state.request++;
  state.viewState={status:'loading',key:extraKey()};state.extraLoadingKey=null;
  $$('[data-view]').forEach(b=>{b.classList.toggle('active',b.dataset.view===view);b.setAttribute('aria-pressed',String(b.dataset.view===view));});
  for(const id of ['sort','asset-class','mode','cutoff-label']) $('#'+id).hidden=view!=='monitor';
  $('#scope-label').hidden=!['replay','episodes','algos'].includes(view); $('#more-tape').hidden=view!=='replay';
  $('#wallet-label').hidden=!['positions','history'].includes(view);
  $('#wallet-chart').hidden=view!=='wallets';
  if(['wallets','positions','history'].includes(view)){state.search='';$('#search').value='';}
  render();
  if(view==='markets')void loadCatalog();
  if(remoteViews.includes(view))void loadExtra();
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
function scopeChanged(){state.page=0;state.extraTime=0;state.tapeBefore=null;state.detail=false;void loadExtra();}
$('#scope').addEventListener('change',scopeChanged);
$('#show-all').onclick=()=>{$('#scope').value='all';state.search='';$('#search').value='';scopeChanged();};
$$('[data-view]').forEach(b=>b.addEventListener('click',()=>switchView(b.dataset.view)));
$('#rows').addEventListener('click',e=>{const tr=e.target.closest('[data-key]');if(tr){const same=tr.dataset.key===state.selected;state.selected=tr.dataset.key;const row=selectedRow();if(eventFor(row))state.eventId=eventFor(row);if(state.view==='wallets')chooseWallet(row.wallet_id,$('#chart-run').value);state.detail=same?!state.detail:true;render();}});
$('#prev').onclick=()=>{state.page--;state.selected='';render();}; $('#next').onclick=()=>{state.page++;state.selected='';render();};
$('#more-tape').onclick=()=>{const oldest=state.extras[0]?.observation_id;if(oldest){state.tapeBefore=oldest;state.page=0;void loadExtra();}};
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
function tradePhase(phase,detail=''){
  paperTicket.phase=phase;$('#trade-modal').dataset.state=phase;
  $('#trade-message').textContent=phase.replaceAll('_',' ')+(detail?' — '+detail:'');
  const editable=['READY','PREVIEWED','PREVIEWING'].includes(phase);
  for(const id of ['trade-instrument','trade-side','trade-quantity','trade-reason'])$('#'+id).disabled=!editable;
  $('#trade-preview-button').disabled=!['READY','PREVIEWED'].includes(phase);
  $('#trade-submit').disabled=phase!=='PREVIEWED';
  $('#trade-position').hidden=phase!=='FILLED';
  $('#trade-ledger').hidden=!['FILLED','REJECTED','CANCELLED'].includes(phase);
  $('#trade-retry').hidden=true;
}
function showTradeContext(event){
  $('#trade-context').textContent=event?`${event.event_text||event.event_id}\n${event.asset} · ${event.strike_or_threshold??'--'} ${event.direction||''} · ${event.event_type} · end ${date(event.expiry)} UTC\nPM ${pct(event.pm_yes??event.pm_indicative)} · OPT ${pct(event.opt_yes)} · GAP ${gap(event.gap_pp)} pp · SIDE ${event.side||'unavailable'}\nEvent ${event.event_id}`:'No current market event selected.';
}
async function openTrade(){
  if(paperTicket&&['SUBMITTING','PENDING'].includes(paperTicket.phase)){openModal('trade-modal');return;}
  const row=['monitor','markets','replay','episodes','algos'].includes(state.view)&&viewStatus().status==='data'?selectedRow():null;
  const ticket=paperTicket={eventId:eventFor(row),context:row,revision:0,phase:'LOADING_INSTRUMENTS',payload:null};
  $('#trade-instrument').innerHTML='<option value="">Choose an instrument</option>';
  $('#trade-preview').textContent='';$('#trade-exclusions').textContent='';$('#trade-quote').textContent='';
  $('#trade-side').value='BUY';$('#trade-quantity').value='1';$('#trade-reason').value='';
  showTradeContext(row);tradePhase('LOADING_INSTRUMENTS');openModal('trade-modal');
  if(!ticket.eventId){tradePhase('ERROR','Select a market event in Monitor, Markets, Tape, Gaps or Algos before opening a ticket.');return;}
  try{
    const data=await request('/api/instruments?'+new URLSearchParams({event_id:ticket.eventId}));
    if(ticket!==paperTicket)return;
    ticket.context=data.event;ticket.instruments=data.rows;showTradeContext(data.event);
    $('#trade-instrument').innerHTML='<option value="">Choose an instrument</option>'+data.rows.map(q=>`<option value="${esc(q.instrument)}">${esc(q.instrument)} | ${q.option_type||q.kind} | ${money(q.bid)} / ${money(q.ask)} USD | ×${q.multiplier}${q.estimated?' DELAYED/EST':''}</option>`).join('');
    const reasons=data.exclusions.map(x=>`${x.code} (${x.count}): ${x.reason}`).join('\n');$('#trade-exclusions').textContent=reasons;
    tradePhase(data.rows.length?'READY':'EMPTY',data.rows.length?'Choose an actual instrument, quantity and reason.':reasons||'No executable instruments in this context.');
  }catch(error){if(ticket===paperTicket)tradePhase('ERROR',error.message);}
}
function executionCopy(order,quote,cost,final=false){
  return `${order.side} ${order.quantity} ${quote.instrument}\nQuoted bid / ask: ${money(cost.quoted_bid)} / ${money(cost.quoted_ask)} USD\n${final?'Actual fill:     ':'Estimated fill:  '}${money(cost.fill_price)} USD × ${quote.multiplier}\nSpread vs mid:   ${money(cost.spread??((cost.quoted_ask-cost.quoted_bid)/2*order.quantity*quote.multiplier))} USD (included in bid/ask)\nSlippage:        ${money(cost.slippage)} USD\nFees:            ${money(cost.fees)} USD\nGross:           ${money(cost.gross_premium)} USD\n${order.side==='BUY'?'Total debit:     ':'Net credit:      '}${money(cost.total_debit??cost.net_credit)} USD\n${quote.quote_assumption}\nReceived ${date(quote.received_ms)} UTC · source ${date(quote.source_ms)} UTC${quote.underlying_source_ms?` · underlying ${date(quote.underlying_source_ms)} UTC`:''}\n${quote.freshness_basis||''} · retrieval limit ${(quote.receipt_max_age_ms||0)/1000}s`;
}
$('#trade-open').onclick=openTrade;
$('#paper-wallet').onchange=()=>{chooseWallet($('#paper-wallet').value);state.page=0;state.extraTime=0;void loadExtra();};
$('#paper-run').onchange=()=>{state.page=0;state.extraTime=0;void loadExtra();};
function editTicket(){
  if(!paperTicket||!['READY','PREVIEWING','PREVIEWED'].includes(paperTicket.phase))return;
  paperTicket.revision++;paperTicket.payload=null;$('#trade-preview').textContent='';tradePhase('READY','Preview the current inputs before submitting.');
  const q=paperTicket.instruments?.find(x=>x.instrument===$('#trade-instrument').value);
  $('#trade-quote').textContent=q?`${q.quote_assumption}\nReceived ${date(q.received_ms)} UTC · source ${date(q.source_ms)} UTC · ${q.freshness_basis} (${q.receipt_max_age_ms/1000}s)` : '';
}
$('#trade-form').addEventListener('input',editTicket);$('#trade-instrument').addEventListener('change',editTicket);
$('#trade-form').onsubmit=async e=>{
  e.preventDefault();const ticket=paperTicket;if(!ticket||!['READY','PREVIEWED'].includes(ticket.phase))return;
  const revision=ticket.revision,payload={wallet:'OLIVER',instrument:$('#trade-instrument').value,side:$('#trade-side').value,quantity:Number($('#trade-quantity').value),reason:$('#trade-reason').value.trim(),basis_event_id:ticket.eventId,client_order_id:crypto.randomUUID()};
  if(!payload.reason){tradePhase('READY','A paper trade needs a reason.');return;}
  tradePhase('PREVIEWING');
  try{
    const preview=await request('/api/preview',payload);if(ticket!==paperTicket||revision!==ticket.revision)return;
    ticket.payload=payload;$('#trade-preview').textContent=executionCopy(preview,preview.quote,preview.costs)+`\nFinal execution rechecks quotes and risk after at least ${preview.latency_ms/1000}s latency.`;tradePhase('PREVIEWED');
  }catch(error){if(ticket===paperTicket&&revision===ticket.revision){ticket.payload=null;tradePhase('READY','Preview rejected: '+error.message);}}
};
async function trackOrder(ticket){
  if(ticket!==paperTicket||ticket.phase!=='PENDING'||ticket.checking)return;ticket.checking=true;
  try{
    const result=await request('/api/order?'+new URLSearchParams({order_id:ticket.payload.client_order_id}));
    if(ticket!==paperTicket)return;ticket.result=result;
    if(result.status==='PENDING'){tradePhase('PENDING',result.execution_error?'Execution error: '+result.execution_error:'Order '+result.order_id+'; awaiting execution after latency. Closing this ticket does not cancel it.');}
    else{
      tradePhase(result.status,result.reason||('Order '+result.order_id));
      if(result.status==='FILLED')$('#trade-preview').textContent=executionCopy(result.order,result.quote,result.fill,true)+`\nOLIVER cash after fill: ${money(result.cash_after)} USD\nResulting position: ${result.position_after?result.position_after.quantity+' '+result.order.instrument+' · cost basis '+money(result.position_after.cost_basis)+' USD':'none'}\nLedger #${result.ledger_id}`;
      state.extraTime=0;if(remoteViews.includes(state.view))void loadExtra(true);void poll(true);
    }
  }catch(error){if(ticket===paperTicket){tradePhase('PENDING','Order status unavailable: '+error.message+'; retrying.');if(error.status===404)$('#trade-retry').hidden=false;}}
  finally{ticket.checking=false;if(ticket===paperTicket&&ticket.phase==='PENDING')setTimeout(()=>trackOrder(ticket),750);}
}
async function submitTicket(){
  const ticket=paperTicket;if(!ticket?.payload||!['PREVIEWED','PENDING'].includes(ticket.phase))return;
  tradePhase('SUBMITTING');
  try{await request('/api/trade',ticket.payload);tradePhase('PENDING','Awaiting final execution.');}
  catch(error){if(error.status&&error.status<500){tradePhase('REJECTED',error.message);return;}tradePhase('PENDING','Submission response unavailable; checking the same order ID.');}
  void trackOrder(ticket);
}
$('#trade-submit').onclick=submitTicket;$('#trade-retry').onclick=submitTicket;
for(const [id,view] of [['trade-position','positions'],['trade-ledger','history']])$('#'+id).onclick=()=>{const run=paperTicket?.result?.run_id;chooseWallet('OLIVER');if(run){setRunOptions($('#paper-run'),[{wallet_id:'OLIVER',id:run,run_number:'order',strategy_version:''}],'OLIVER');$('#paper-run').value=String(run);}closeModal();switchView(view);};
function renderEquity(){
  const svg=$('#equity-svg');
  if(chart.status!=='data'||!chart.data){svg.innerHTML='';chart.series=[];$('#chart-note').textContent=chart.status==='error'?'CHART ERROR: '+chart.error:'LOADING CHART';$('#chart-readout').textContent=chart.status==='error'?'Wallet table remains available. Chart will retry.':'';return;}
  const selected=$('#chart-wallet').value,isReturn=$('#chart-unit').value==='return';
  const series=chart.data.series.filter(s=>selected==='all'||s.wallet_id===selected);chart.series=series;
  const points=series.flatMap(s=>s.points.map(p=>({...p,value:isReturn?(p.equity/s.starting_balance-1)*100:p.equity})));
  if(!points.length){svg.innerHTML='';$('#chart-note').textContent='EMPTY — No equity marks in this period.';$('#chart-readout').textContent='Choose Full run to inspect earlier history; new runs need their first mark.';return;}
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
$('#chart-wallet').onchange=()=>{$('#chart-run').value='';chart.cursor=null;state.page=0;state.extraTime=0;if($('#chart-wallet').value!=='all')chooseWallet($('#chart-wallet').value);void loadExtra();void loadChart();};
$('#chart-run').onchange=()=>{chart.cursor=null;state.extraTime=0;if($('#chart-run').value)$('#chart-hours').value='0';if($('#chart-wallet').value!=='all')chooseWallet($('#chart-wallet').value,$('#chart-run').value);void loadExtra();void loadChart();};
$('#chart-hours').onchange=()=>{chart.cursor=null;void loadChart();};
$('#automation-toggle').onclick=async()=>{try{await request('/api/automation',{enabled:!chart.meta?.automation_enabled});state.extraTime=0;await loadExtra();render();}catch(e){message(e.message);}};
$('#asset-class').onchange=()=>{state.page=0;render();};
setInterval(()=>{$('#clock').textContent=new Date().toISOString().slice(11,19)+' UTC';},1000);
setInterval(()=>poll(),1000);poll();
