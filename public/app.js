const bootHint=document.getElementById('boot-hint');if(bootHint)bootHint.remove();
const $ = id => document.getElementById(id);
const fmt = new Intl.NumberFormat('zh-CN');
const n = v => v == null ? '未知' : fmt.format(v);
const pct = v => v == null ? '未知' : new Intl.NumberFormat('zh-CN',{style:'percent',maximumFractionDigits:1}).format(v);
const short = v => v >= 1e8 ? `${Number((v/1e8).toFixed(1))}亿` : v >= 1e4 ? `${Number((v/1e4).toFixed(1))}万` : n(v);
const labels = {today:'今天', '7d':'近 7 天','30d':'近 30 天',all:'全部历史',custom:'所选期间'};
let data, reconciliationData, page=1, activeTab='overview', pending=false, again=false, chartKey='', selectedBucket='', softwareKey='', modelKey='';
let activeQuery='';
const viewNames=['overview','details','reconciliation','sources'];
const dayNow = () => new Intl.DateTimeFormat('sv-SE',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date());
$('start').value=$('end').value=dayNow(); $('start').max=$('end').max=dayNow();
function query() {
  const params=new URLSearchParams({period:$('period').value,software:$('software').value,model:$('model').value});
  if ($('period').value==='custom') {params.set('start',$('start').value);params.set('end',$('end').value);}
  return params.toString();
}
function tab(name, focus=false) {
  activeTab=name;syncViewContext();
  $('view-title').textContent={overview:'用量概览',details:'用量明细',reconciliation:'Codex 对账',sources:'数据来源'}[name];
  for (const id of viewNames) {
    $(id).hidden=id!==name; $('tab-'+id).setAttribute('aria-selected',String(id===name)); $('tab-'+id).tabIndex=id===name ? 0 : -1;
  }
  $('usage-filters').hidden=$('usage-scope').hidden=name==='reconciliation';
  if (focus) $('tab-'+name).focus();
  if(name==='reconciliation')refreshReconciliation();else refresh();
  if (name==='overview' && data) renderChart();
}
for (const name of viewNames) $('tab-'+name).addEventListener('click',()=>tab(name));
document.querySelector('.tabs').addEventListener('keydown',e=>{
  const names=viewNames;let index=names.indexOf(activeTab);
  const vertical=document.querySelector('.tabs').getAttribute('aria-orientation')==='vertical';
  if(e.key===(vertical?'ArrowDown':'ArrowRight'))index=(index+1)%names.length;else if(e.key===(vertical?'ArrowUp':'ArrowLeft'))index=(index+names.length-1)%names.length;else if(e.key==='Home')index=0;else if(e.key==='End')index=names.length-1;else return;
  e.preventDefault();tab(names[index],true);
});
const navMedia=matchMedia('(max-width:900px)');
function syncNavOrientation(){document.querySelector('.tabs').setAttribute('aria-orientation',navMedia.matches?'horizontal':'vertical');}
navMedia.addEventListener('change',syncNavOrientation);syncNavOrientation();
function filterChanged(){ page=1;selectedBucket='';chartKey='';$('date-error').textContent='';refresh(); }
$('software').addEventListener('change',()=>{$('model').value='';$('speed-basis').value=$('software').value==='Codex'?'task':$('software').value==='DeepSeek Harness'?'decode':'request';filterChanged();});
$('model').addEventListener('change',filterChanged);
$('period').addEventListener('change',()=>{$('custom-range').hidden=$('period').value!=='custom';if($('period').value==='custom')$('start').focus();else filterChanged();});
$('custom-range').addEventListener('submit',e=>{e.preventDefault();if($('start').value>$('end').value||$('end').value>dayNow()){$('date-error').textContent='请选择开始不晚于结束、结束不晚于今天的日期。';return;}filterChanged();});
$('reset').addEventListener('click',()=>{$('software').value=$('model').value='';$('period').value='7d';$('speed-basis').value='request';$('custom-range').hidden=true;$('search').value='';filterChanged();});
$('refresh').addEventListener('click',()=>refresh());
$('see-details').addEventListener('click',()=>tab('details',true));$('see-sources').addEventListener('click',()=>tab('sources',true));
function cell(row,text,cls=''){const td=document.createElement('td');td.textContent=text;if(cls)td.className=cls;row.append(td);return td;}
const decimal=v=>v==null?'未知':new Intl.NumberFormat('zh-CN',{maximumFractionDigits:1}).format(v);
const seconds=v=>v==null?'未知':decimal(v/1000)+' 秒';
function renderPerformance(){
  const basis=$('speed-basis').value,t=basis==='task'?data.codex_tasks:data.totals;
  const value=basis==='decode'?t.decode_tps:t.output_tps;
  $('speed-rate-label').textContent=basis==='task'?'任务输出速度（含工具）':basis==='decode'?'生成区间输出速度':'端到端输出速度';
  $('speed-duration-label').textContent=basis==='task'?'平均任务耗时':'平均请求耗时';
  $('speed-p95-label').textContent=basis==='task'?'P95 任务耗时':'P95 请求耗时';
  $('speed-tps').textContent=value==null?'未知':decimal(value)+' tok/s';$('speed-duration').textContent=seconds(t.mean_request_ms);$('speed-p95').textContent=seconds(t.p95_request_ms);$('speed-ttft').textContent=seconds(t.mean_ttft_ms);
  $('speed-coverage').textContent=basis==='task'?`${n(t.records)} 轮有任务耗时 · ${n(t.verified_output_turns)} 轮可核对完整用量 · 包含工具时间`:basis==='decode'?`${n(t.decode_records)} / ${n(t.records)} 条有生成区间 · ${n(t.ttft_records)} 条有首 Token 时间 · 耗时列仍为完整请求`:`${n(t.timed_records)} / ${n(t.records)} 条记录有耗时 · ${n(t.ttft_records)} 条有首 Token 时间 · 仅统计有效样本`;
}
$('speed-basis').addEventListener('change',()=>{if(data){renderPerformance();renderRows();}});
$('see-speed').addEventListener('click',()=>{$('show-speed').checked=true;renderRows();tab('details',true);});
function rate(row){return row.records ? pct(row.cache_hit_ratio) : '—';}
function render() {
  const t=data.totals,c=data.collector;
  const softwareSignature=JSON.stringify(data.capabilities.map(x=>x.software));
  if(softwareSignature!==softwareKey){const selected=$('software').value;$('software').replaceChildren(new Option('全部软件',''),...data.capabilities.map(x=>new Option(x.software,x.software)));$('software').value=selected;softwareKey=softwareSignature;}
  const modelSignature=JSON.stringify(data.models);
  if(modelSignature!==modelKey){const selected=$('model').value;const options=[new Option('全部模型',''),...data.models.map(x=>new Option(x,x))];if(selected&&!data.models.includes(selected))options.push(new Option(selected,selected));$('model').replaceChildren(...options);$('model').value=selected;modelKey=modelSignature;}
  $('today-total').textContent=n(data.today.total_tokens);
  $('today-note').textContent=`${n(data.today.records)} 条计数记录 · 今天截至目前`;
  $('range-title').textContent=`${labels[data.period]}用量`;$('total').textContent=n(t.total_tokens);
  $('token-split').textContent=`输入 ${n(t.input_tokens)} · 输出 ${n(t.output_tokens)}`;
  $('hit-rate').textContent=rate(t);$('uncached').textContent=t.records ? n(t.uncached_input_tokens) : '—';
  $('cache-note').textContent=t.cache_unknown_records ? `${n(t.cache_unknown_records)} 条记录缺少缓存字段` : `缓存读取 ${n(t.cached_input_tokens)} Token`;
  $('miss-note').textContent=t.cache_hit_ratio==null ? '缓存字段不足时无法计算' : `未命中比例 ${pct(t.cache_miss_ratio)} · 仅针对输入`;
  $('scope').textContent=`${data.range.start} 至 ${data.range.end} · ${data.software||'全部软件'} · ${$('model').value||'全部模型'}`;
  $('status').textContent=c.phase==='ready' ? '本地同步中 · 每 3 秒更新' : c.phase==='scanning' ? `正在索引 ${c.indexed_files}/${c.files}` : c.phase==='error' ? '本地索引暂不可用' : '正在建立本地索引';
  $('updated').textContent=c.last_scan ? '最近扫描 '+new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}).format(new Date(c.last_scan)) : '等待首次扫描';
  $('error').hidden=!c.error;$('error').textContent=c.error||'';
  $('coverage').textContent=`${n(t.records)} 条计数记录 · 旧格式推算占 ${pct(t.total_tokens ? t.legacy_tokens/t.total_tokens : 0)} · 本机记录可能不完整`;
  
  const unavailable=data.capabilities.find(c=>c.software===data.software&&['unverified','not_found','pending','error'].includes(c.status)&&data.totals.records===0);
  const codexScope=!data.software||data.software==='Codex';
  $('scope-notice').hidden=!unavailable&&!codexScope;$('scope-notice').textContent=unavailable?'该来源暂不可用，请在数据来源中查看状态。':codexScope?'Codex 仅计本机 GPT；账户差额见“Codex 对账”。':'';
  if(unavailable){$('today-total').textContent=$('total').textContent='—';$('today-note').textContent=$('token-split').textContent='等待可靠的用量来源';}
  renderPerformance();renderChart();renderRanking();renderRows();renderSources();
}
function svg(tag, attrs={}, text=''){const e=document.createElementNS('http://www.w3.org/2000/svg',tag);for(const[k,v]of Object.entries(attrs))e.setAttribute(k,v);if(text)e.textContent=text;return e;}
function metricValue(row,key){if(!row)return key==='cache_hit_ratio'?null:0;return row.records===0&&key==='uncached_input_tokens'?0:row[key];}
function chartData(){return data.trend||data;}
function niceScale(values,key){
  if(key==='cache_hit_ratio')return {max:1,ticks:[0,.25,.5,.75,1],unit:'缓存命中率',format:pct};
  const peak=Math.max(0,...values),target=Math.max(1,peak*1.05)/5;
  const power=10**Math.floor(Math.log10(target));
  const step=Math.max(1,([1,2,5,10].find(v=>v>=target/power)||10)*power);
  const max=Math.max(step,Math.ceil(peak*1.05/step)*step);
  return {max,ticks:Array.from({length:Math.round(max/step)+1},(_,i)=>i*step),
    unit:'Token',format:short};
}
function chartSeries(){const key=$('metric').value;if(!$('compare').checked)return [{name:'所选合计',rows:chartData().series,cls:'series0'}];return chartData().software_rows.slice(0,6).map((r,i)=>({name:r.software,cls:'series'+i,rows:chartData().series.map(d=>{const match=chartData().software_series.find(s=>s.software===r.software&&s.bucket===d.bucket);return match||{bucket:d.bucket,records:0,[key]:key==='cache_hit_ratio'?null:0};})}));}
function renderChart(){
  const key=$('metric').value,width=Math.min(960,Math.max(240,$('chart').clientWidth||960)),signature=JSON.stringify([chartData().series,chartData().software_series,chartData().range,data.range,data.software,$('model').value,key,$('compare').checked,width]);
  if(signature===chartKey){if(!chartData().series.some(r=>r.bucket===selectedBucket))selectPoint(chartData().series.length-1);else updatePoint();return;}chartKey=signature;
  const focusedBucket=document.activeElement?.closest('#chart .hit')?.getAttribute('data-bucket');const chart=$('chart');chart.setAttribute('viewBox',`0 0 ${width} 270`);chart.replaceChildren(svg('title',{},'悬浮到日期查看精确用量，可用左右方向键移动。'));
  const list=chartSeries();const legend=document.createDocumentFragment();for(const item of list){const span=document.createElement('span'),i=document.createElement('i');i.className=item.cls;span.append(i,document.createTextNode(item.name));legend.append(span);}$('legend').replaceChildren(legend);
  $('trend-note').textContent=`${chartData().range.start} 至 ${chartData().range.end} · ${chartData().range.grain==='day'?'按日':'按月'}${chartData().expanded?' · 短范围保留 7 天对比':''}`;
  const values=list.flatMap(s=>s.rows.map(r=>metricValue(r,key))).filter(v=>v!=null);const scale=niceScale(values,key),max=scale.max;
  const left=Math.max(64,...scale.ticks.map(v=>scale.format(v).length*7+(scale.format(v).match(/[万亿]/g)||[]).length*4+16)),right=width-12,span=right-left;chart.dataset.plotLeft=String(left);const x=i=>chartData().series.length===1?(left+right)/2:left+i*span/(chartData().series.length-1),y=v=>215-v/max*185;
  chart.append(svg('text',{x:8,y:14,class:'axis-unit'},scale.unit));for(const value of scale.ticks){const py=y(value);chart.append(svg('line',{x1:left,x2:right,y1:py,y2:py,class:'grid','data-value':value}),svg('text',{x:left-8,y:py+4,'text-anchor':'end',class:'axis-tick'},scale.format(value)));}
  for(const item of list){let d='';let open=false;item.rows.forEach((r,i)=>{const v=metricValue(r,key);if(v==null){open=false;return;}d+=`${open?'L':'M'}${x(i)} ${y(v)} `;open=true;});chart.append(svg('path',{d,class:'plot '+item.cls}));if(item.rows.length<=62)item.rows.forEach((r,i)=>{const v=metricValue(r,key);if(v!=null)chart.append(svg('circle',{cx:x(i),cy:y(v),r:3.5,class:'dot '+item.cls}));});}
  const count=chartData().series.length,spacing=count>1?span/(count-1):70;
  for(let i=0;i<count;i++){
    const row=chartData().series[i];const rect=svg('rect',{x:Math.max(left,x(i)-spacing/2),y:22,width:count===1?70:Math.min(spacing,right-Math.max(left,x(i)-spacing/2)),height:197,class:'hit','data-bucket':row.bucket,tabindex:i===count-1?'0':'-1',role:'button','aria-label':`${row.bucket}，总 Token ${n(row.total_tokens)}，缓存命中率 ${rate(row)}`});
    if(count===1)rect.setAttribute('x',x(i)-35);
    rect.addEventListener('pointerenter',()=>selectPoint(i));rect.addEventListener('click',()=>selectPoint(i));rect.addEventListener('focus',()=>selectPoint(i));
    rect.addEventListener('keydown',e=>{if(e.key==='ArrowLeft'||e.key==='ArrowRight'){e.preventDefault();const next=Math.max(0,Math.min(count-1,i+(e.key==='ArrowRight'?1:-1)));selectPoint(next);chart.querySelectorAll('.hit')[next].focus();}else if(e.key==='Enter'||e.key===' '){e.preventDefault();drill();}});chart.append(rect);
    const step=Math.max(1,Math.ceil(count/(width<500?4:7)));if(i===0||i===count-1||(i%step===0&&count-1-i>=step/2))chart.append(svg('text',{x:x(i),y:250,'text-anchor':i===count-1?'end':'middle'},chartData().range.grain==='day'?row.bucket.slice(5).replace('-','/'):row.bucket));
  }
  chart.append(svg('line',{id:'chart-selection',x1:0,x2:0,y1:22,y2:220,class:'selection'}));
  const preferred=selectedBucket||(data.range.start===data.range.end?data.range.end:'');const index=chartData().series.findIndex(s=>s.bucket===preferred);selectPoint(index>=0?index:count-1);if(focusedBucket){const hits=[...chart.querySelectorAll('.hit')];(hits.find(e=>e.getAttribute('data-bucket')===focusedBucket)||hits[count-1])?.focus();}
  const known=list.some(s=>s.rows.some(r=>r.records>0&&metricValue(r,key)!=null));
  $('chart-empty').hidden=chartData().totals.records>0&&known;
  $('chart-empty').textContent=chartData().totals.records&&!known?'当前指标缺少可靠字段，无法绘制；未知值没有按零处理。':'这个范围没有已记录用量。可切换时间或查看数据来源。';
  const daily=document.createDocumentFragment();for(const row of chartData().series){const tr=document.createElement('tr');cell(tr,row.bucket);cell(tr,n(row.total_tokens));cell(tr,n(row.input_tokens));cell(tr,n(row.output_tokens));cell(tr,rate(row));daily.append(tr);}$('daily-rows').replaceChildren(daily);
}
function selectPoint(index){if(!data)return;selectedBucket=chartData().series[index]?.bucket||'';updatePoint();}
function updatePoint(){const i=chartData().series.findIndex(s=>s.bucket===selectedBucket);if(i<0)return;const r=chartData().series[i],key=$('metric').value;const caption=$('metric').selectedOptions[0].textContent;
  $('point-title').textContent=r.bucket;let text=`${caption} ${key==='cache_hit_ratio'?rate(r):n(metricValue(r,key))} · ${n(r.records)} 条记录`;
  if($('compare').checked)text=chartSeries().map(s=>`${s.name} ${key==='cache_hit_ratio'?rate(s.rows[i]):n(metricValue(s.rows[i],key))}`).join(' · ');
  $('point-value').textContent=text;if(hoverPosition)renderTooltip();$('point-prev').disabled=i===0;$('point-next').disabled=i===chartData().series.length-1;$('drill').textContent=chartData().range.grain==='day'?'查看当天明细':'查看当月明细';
  const line=$('chart-selection');if(line){const width=Number($('chart').viewBox.baseVal.width),left=Number($('chart').dataset.plotLeft)||80,right=width-12;const x=chartData().series.length===1?(left+right)/2:left+i*(right-left)/(chartData().series.length-1);line.setAttribute('x1',x);line.setAttribute('x2',x);}
  document.querySelectorAll('#chart .hit').forEach((e,index)=>e.tabIndex=index===i?0:-1);
}
let hoverPosition=null;
function renderTooltip(){
  if(!data||!hoverPosition)return;
  const index=chartData().series.findIndex(r=>r.bucket===selectedBucket);
  if(index<0)return;
  const row=chartData().series[index],key=$('metric').value,tip=$('chart-tooltip');
  const date=document.createElement('strong');date.textContent=row.bucket;
  const lines=$('compare').checked?chartSeries().map(series=>({name:series.name,row:series.rows[index]})):
    [{name:$('metric').selectedOptions[0].textContent,row}];
  const nodes=lines.map(item=>{const line=document.createElement('span');line.textContent=`${item.name}：${key==='cache_hit_ratio'?rate(item.row):n(metricValue(item.row,key))}${key==='cache_hit_ratio'?'':' Token'}`;return line;});
  tip.replaceChildren(date,...nodes);tip.hidden=false;
  const frame=$('chart-frame').getBoundingClientRect();
  const x=hoverPosition.x-frame.left,y=hoverPosition.y-frame.top;
  tip.style.left=Math.max(4,Math.min(x+12,frame.width-tip.offsetWidth-4))+'px';
  tip.style.top=Math.max(4,Math.min(y-tip.offsetHeight-12,frame.height-tip.offsetHeight-4))+'px';
}
$('chart').addEventListener('pointermove',event=>{
  if(!data)return;
  const chart=$('chart'),bounds=chart.getBoundingClientRect(),width=chart.viewBox.baseVal.width;
  const x=(event.clientX-bounds.left)*width/bounds.width;
  const left=Number(chart.dataset.plotLeft)||80,right=width-12,count=chartData().series.length;
  hoverPosition={x:event.clientX,y:event.clientY};
  const index=count===1?0:Math.max(0,Math.min(count-1,Math.round((x-left)/(right-left)*(count-1))));
  selectPoint(index);
});
$('chart').addEventListener('pointerleave',()=>{hoverPosition=null;$('chart-tooltip').hidden=true;});
$('chart').addEventListener('pointercancel',()=>{hoverPosition=null;$('chart-tooltip').hidden=true;});
$('point-prev').addEventListener('click',()=>selectPoint(Math.max(0,chartData().series.findIndex(s=>s.bucket===selectedBucket)-1)));
$('point-next').addEventListener('click',()=>selectPoint(Math.min(chartData().series.length-1,chartData().series.findIndex(s=>s.bucket===selectedBucket)+1)));
function drill(){if(!data||!selectedBucket)return;let start=selectedBucket,end=selectedBucket;if(chartData().range.grain==='month'){start+='-01';const[y,m]=selectedBucket.split('-').map(Number);end=`${selectedBucket}-${String(new Date(Date.UTC(y,m,0)).getUTCDate()).padStart(2,'0')}`;end=end>dayNow()?dayNow():end;start=start<chartData().range.start?chartData().range.start:start;end=end>chartData().range.end?chartData().range.end:end;}
  $('period').value='custom';$('start').value=start;$('end').value=end;$('custom-range').hidden=false;$('search').value='';tab('details',true);filterChanged();
}
$('drill').addEventListener('click',drill);for(const id of ['metric','compare'])$(id).addEventListener('change',()=>{chartKey='';if(data)renderChart();});
function renderRanking(){const group=$('rank-by').value,all=data[group==='software'?'software_rows':'model_rows'];$('rank-label').textContent=group==='software'?'软件':'模型';document.querySelector('.ranking').classList.toggle('model-ranking',group==='model');if($('rank-scroll-hint'))$('rank-scroll-hint').hidden=all.length===0;const fragment=document.createDocumentFragment();for(const r of all.slice(0,8)){const tr=document.createElement('tr'),first=cell(tr,''),b=document.createElement('button');b.type='button';b.className='row-link';b.textContent=r[group];b.addEventListener('click',()=>{if(group==='software'){$('software').value=r.software;$('model').value='';}else{$('model').value=r.model;}$('search').value='';tab('details',true);filterChanged();});first.append(b);cell(tr,n(r.total_tokens));const share=data.totals.total_tokens?r.total_tokens/data.totals.total_tokens:0;const shareLabel=share>0&&share<.001?'<0.1%':pct(share);const shareCell=cell(tr,shareLabel);const bar=document.createElement('progress');bar.className='share-bar';bar.max=1;bar.value=share;bar.setAttribute('aria-label',r[group]+' 用量占比 '+shareLabel);shareCell.append(bar);cell(tr,rate(r));fragment.append(tr);}$('rank-rows').replaceChildren(fragment);$('rank-empty').hidden=all.length>0;$('rank-note').textContent=all.length>8?`显示前 8 个，共 ${all.length} 个；更多请查看明细`:`共 ${all.length} 个${group==='software'?'软件':'模型'}`;}
$('rank-by').addEventListener('change',()=>data&&renderRanking());
function renderRows(){if(!data)return;const search=$('search').value.trim().toLocaleLowerCase(),sort=$('sort').value;let rows=data.rows.filter(r=>(r.software+' '+r.model).toLocaleLowerCase().includes(search));rows=[...rows].sort((a,b)=>sort==='model'?a.model.localeCompare(b.model):sort==='cache_hit_ratio'?(a[sort]??Infinity)-(b[sort]??Infinity):(b[sort]??-1)-(a[sort]??-1));
  const size=Number($('page-size').value),pages=Math.max(1,Math.ceil(rows.length/size));page=Math.min(page,pages);
  const columns=[['total_tokens','总 Token'],['input_tokens','输入'],['output_tokens','输出'],['cache_hit_ratio','缓存命中率']];if($('show-speed').checked){const basis=$('speed-basis').value;columns.push([basis==='decode'?'decode_tps':basis==='task'?'task_output_tps':'output_tps',basis==='decode'?'生成速度 tok/s':basis==='task'?'任务速度 tok/s':'输出速度 tok/s'],[basis==='task'?'task_mean_request_ms':'mean_request_ms',basis==='task'?'平均任务耗时':'平均请求耗时'],[basis==='task'?'task_mean_ttft_ms':'mean_ttft_ms','首 Token 等待'],[basis==='decode'?'decode_records':basis==='task'?'task_timed_records':'timed_records',basis==='task'?'任务样本':'有耗时记录']);}if($('extended').checked)columns.push(['cached_input_tokens','缓存读取'],['uncached_input_tokens','未缓存输入'],['records','计数记录'],['legacy_tokens','旧格式推算'],['reasoning_output_tokens','推理（已知）'],['cache_write_input_tokens','缓存写入（已知）']);
  const head=document.createElement('tr');const first=document.createElement('th');first.textContent='模型 / 软件';first.scope='col';head.append(first);for(const[,label]of columns){const th=document.createElement('th');th.textContent=label;th.scope='col';head.append(th);}$('detail-head').replaceChildren(head);
  const fragment=document.createDocumentFragment();for(const r of rows.slice((page-1)*size,page*size)){const tr=document.createElement('tr'),td=cell(tr,''),model=document.createElement('span'),software=document.createElement('span');model.textContent=r.model;model.className='model-name';software.textContent=r.software;software.className='software-name';td.append(model,software);for(const[key]of columns){let text=key==='cache_hit_ratio'?rate(r):key.endsWith('_tps')?decimal(r[key]):key.endsWith('_ms')?seconds(r[key]):n(r[key]);if(key==='cached_input_tokens'&&r.cache_unknown_records)text=r.cache_unknown_records===r.records?'未知':text+'（部分）';if(key==='reasoning_output_tokens'&&r.reasoning_unknown_records)text=r.reasoning_unknown_records===r.records?'未知':text+'（部分）';if(key==='cache_write_input_tokens'&&r.write_unknown_records)text=r.write_unknown_records===r.records?'未知':text+'（部分）';cell(tr,text);}fragment.append(tr);}$('rows').replaceChildren(fragment);$('empty').hidden=rows.length>0;$('row-count').textContent=`${rows.length} 行 / 共 ${data.rows.length} 行`;$('page-number').textContent=`${page} / ${pages}`;$('prev-page').disabled=page===1;$('next-page').disabled=page>=pages;
}
for(const id of ['search','sort','page-size','extended','show-speed'])$(id).addEventListener(id==='search'?'input':'change',()=>{page=1;renderRows();});$('prev-page').addEventListener('click',()=>{page--;renderRows();});$('next-page').addEventListener('click',()=>{page++;renderRows();});
const sourceInfo={Codex:'本机日志逐次请求去重，旧格式推算；仅计明确 GPT。任务完成事件另提供整轮耗时与首 Token 等待，含工具时间，不当作生成速度。账户热力图来自服务端汇总，尚未对账；OpenAI 未分类原始请求在 Codex 对账另列，概览仍仅计明确 GPT。',WorkBuddy:'合并 trace 响应与会话 transcript 的原始 usage；共用请求标识去重。trace 标签优先；缺失缓存保留未知，计数冲突不相加。',ZCode:'读取 model_usage；重试按请求标识与尝试次数区分；不重复读取聊天记录。','MiMo Desktop':'读取 MiMoCode 引擎 step-finish；统一缓存与推理口径，排除可识别的导入与继承记录。桌面与 CLI 来源可能无法区分。','DeepSeek Harness':'只读 v3/v4 会话及 Zstandard 压缩日志；输入包含缓存读写，原始 usage 去重。首 Token 从流式片段还原，生成速度与请求速度分开；继承记录不重复计入。'};
let accountSelectedDate='', accountFollowLatest=true, accountRequestPending=false, accountUiError='', accountErrorCheckedAt=null, reconciliationPending=false;
const officialDateTime = value => value ? new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}).format(new Date(value)) : '尚未读取';
function accountReference(){return reconciliationData?.account_reference||{};}
function accountBuckets(){return accountReference().buckets||[];}
const basisLabel=()=>$('reconcile-basis').value==='utc'?'UTC':'北京时间';
function reconciliationRows(){
  const rows=accountReference().rows||[],period=$('reconcile-period').value;
  if(period==='all')return rows;
  const end=dayNow(),start=new Date(end+'T00:00:00Z');start.setUTCDate(start.getUTCDate()-(period==='7d'?6:29));
  const first=start.toISOString().slice(0,10);return rows.filter(r=>r.date>=first&&r.date<=end);
}
function localForRow(row,basis=$('reconcile-basis').value){return row?.[basis==='utc'?'local_gpt_utc_tokens':'local_gpt_tokens']??0;}
function auxiliaryForRow(row,basis=$('reconcile-basis').value){return row?.[basis==='utc'?'utc_auxiliary_tokens':'beijing_auxiliary_tokens']??0;}
function observedForRow(row,basis=$('reconcile-basis').value){return row?.[basis==='utc'?'utc_observed_tokens':'beijing_observed_tokens']??localForRow(row,basis);}
function renderReconciliationTable(){
  const rows=reconciliationRows(),basis=$('reconcile-basis').value,fragment=document.createDocumentFragment();
  $('reconcile-local-heading').textContent='明确 GPT · '+basisLabel();
  for(const row of [...rows].reverse()){
    const tr=document.createElement('tr'),dateCell=cell(tr,''),button=document.createElement('button');button.className='row-link';button.type='button';button.textContent=row.date;
    button.addEventListener('click',()=>{accountSelectedDate=row.date;accountFollowLatest=false;renderAccountReference();$('account-reference').scrollIntoView({block:'start'});$('account-day').focus({preventScroll:true});});dateCell.append(button);
    if(row.date===accountSelectedDate){tr.setAttribute('aria-current','date');tr.className='reconcile-selected';}
    const official=row.official_tokens,local=localForRow(row),observed=observedForRow(row),auxiliary=auxiliaryForRow(row),records=row[basis==='utc'?'utc_records':'beijing_records']||0;
    cell(tr,official==null?'未提供':n(official));const localCell=cell(tr,n(local));const detail=document.createElement('span');detail.className='reconcile-cell-note';detail.textContent=records?n(records)+' 条记录':'无本机记录';localCell.append(detail);
    cell(tr,n(auxiliary));const observedCell=cell(tr,n(observed));observedCell.className='observed-total-cell';cell(tr,official==null?'—':n(official-observed));cell(tr,official==null||official===0?'—':pct((official-observed)/official));
    const freshness=cell(tr,official==null?'—':officialDateTime(row.official_checked_at));
    if(official!=null&&!row.returned_in_latest_read){const note=document.createElement('span');note.className='reconcile-cell-note';note.textContent='历史缓存';freshness.append(note);}
    fragment.append(tr);
  }
  $('reconcile-rows').replaceChildren(fragment);$('reconcile-empty').hidden=rows.length>0;
  $('reconcile-history-note').textContent=`${n(rows.length)} 个日期 · 按 ${basisLabel()} 分组本机记录 · 点击日期查看单日数据`;
  $('export-reconciliation').disabled=!reconciliationData||rows.length===0;
}
function syncViewContext(){
  const recon=activeTab==='reconciliation';
  $('sidebar-context').innerText=recon?'账户 / 本机对照\n日界线可切换':'本地记录\n北京时间';
  $('footer-context').textContent=recon?'官方接口 / 本机可观察用量 · 本机日界线：'+basisLabel():'本机记录 · 北京时间 · 统计口径见数据来源';
}
function renderAccountReference(){
  syncViewContext();
  const reference=accountReference(),buckets=accountBuckets(),sync=reference.sync||{},basis=$('reconcile-basis').value;
  if(accountUiError&&reference.checked_at!==accountErrorCheckedAt){accountUiError='';accountErrorCheckedAt=null;}
  const latest=buckets.at(-1)?.date;
  if(accountFollowLatest&&latest)accountSelectedDate=latest;
  if(!accountSelectedDate)accountSelectedDate=dayNow();
  const selected=accountSelectedDate;
  if(document.activeElement!==$('account-day'))$('account-day').value=selected;
  $('account-day').max=latest&&latest>dayNow()?latest:dayNow();
  const row=reference.rows?.find(b=>b.date===selected),official=row?.official_tokens,local=localForRow(row),auxiliary=auxiliaryForRow(row),observed=observedForRow(row);
  const previous=buckets.findLast(b=>b.date<selected),next=buckets.find(b=>b.date>selected);
  $('account-prev').disabled=!previous;$('account-next').disabled=!next;$('account-latest').disabled=!latest||selected===latest;
  $('account-empty').hidden=official!=null;
  $('account-empty').textContent=buckets.length?'官方尚未提供 '+selected+' 的记录；未提供不代表用量为零。':sync.phase==='syncing'?'正在读取官方账户用量；本机统计可以继续查看。':'当前没有可靠的官方每日记录。';
  const items=[['官方接口日统计',official==null?'未提供':n(official)],['本机可观察合计（'+basisLabel()+' 日）',reconciliationData?n(observed):'正在读取'],['未解释差额（接口 − 本机合计）',official==null?'无法比较':n(official-observed)]];
  const fragment=document.createDocumentFragment();for(const[label,value]of items){const dt=document.createElement('dt'),dd=document.createElement('dd');dt.textContent=label;dd.textContent=value;fragment.append(dt,dd);}$('account-quality').replaceChildren(fragment);
  $('observed-equation').textContent=`明确 GPT ${n(local)} + OpenAI 未分类 ${n(auxiliary)} = 本机合计 ${n(observed)} Token`;
  const auxiliaryRecords=row?.[basis==='utc'?'utc_auxiliary_records':'beijing_auxiliary_records']??0;
  const conflicts=row?.[basis==='utc'?'utc_auxiliary_conflicts':'beijing_auxiliary_conflicts']??0;
  $('observed-records').textContent=`OpenAI 未分类 ${n(auxiliaryRecords)} 条原始计数；概览与明细的 GPT 总量保持独立。`+(conflicts?` ${n(conflicts)} 条辅助计数存在冲突，未计入合计。`:'');
  const aliases=row?.[basis==='utc'?'utc_auxiliary_models':'beijing_auxiliary_models']||[],aliasRows=document.createDocumentFragment();
  for(const alias of aliases){const tr=document.createElement('tr');cell(tr,alias.model);cell(tr,n(alias.tokens));cell(tr,n(alias.records));aliasRows.append(tr);}
  $('auxiliary-rows').replaceChildren(aliasRows);$('auxiliary-details').hidden=aliases.length===0;
  $('account-direction').textContent=official==null?'缺少接口日记录时无法计算差额。':official===observed?'当天数值相同；账户归属与服务端统计范围仍未完全核实。':official>observed?'接口值较大；剩余部分没有可核实的逐请求用量，保留差额。':'本机合计较大；需要继续核查日期归属与统计范围。';
  const bj=observedForRow(row,'beijing'),utc=observedForRow(row,'utc'),match=official!=null&&bj!==utc&&(official===bj||official===utc);
  const records=row?.[basis==='utc'?'utc_records':'beijing_records']??0,legacy=row?.[basis==='utc'?'utc_legacy_tokens':'beijing_legacy_tokens']??0;
  const check=official==null?'先等待官方日记录，再比较两边用量。':official===bj&&official===utc?'北京时间和 UTC 两种日界线下，本机与官方数值均相同；仍需分别确认账户与模型范围。':match?`改用 ${official===utc?'UTC':'北京时间'} 后，本机与官方数值相同；两种日界线相差 ${n(Math.abs(bj-utc))} Token。这说明日期归属可以解释该日的数值差异，但未证明官方所有日期的时区规则。`:`北京时间与 UTC 的本机总量相差 ${n(Math.abs(bj-utc))} Token；当前日界线未完全对齐接口；剩余差额不做估算补齐。`;
  $('account-check').textContent=check+(reconciliationData?` 明确 GPT ${n(records)} 条计数记录，旧格式推算 ${n(legacy)} Token。`:'');
  const state=accountUiError||(sync.phase==='syncing'?'正在更新官方数据…':sync.phase==='error'?'官方更新失败：'+(sync.last_error||'稍后重试，已有数据保留'):'');
  $('account-sync-state').textContent=(state?state+' · ':'')+'接口读取时间：'+officialDateTime(reference.checked_at)+(latest?' · 最新有记录日期：'+latest:'');
  const minutes=Math.ceil((sync.retry_after_seconds||0)/60),busy=accountRequestPending||sync.phase==='syncing';
  $('account-refresh').disabled=busy||minutes>0||!reconciliationData;
  $('account-refresh').textContent=busy?'正在更新…':minutes>0?'更新冷却 '+minutes+' 分钟':'更新接口数据';
  const dailyUnavailable=reference.daily_buckets_available===false?'本次官方响应未提供每日明细；已有记录按各自读取时间展示。 ':'';
  const rowTime=row?.official_checked_at&&row.official_checked_at!==reference.checked_at?'所选日期为历史缓存，读取于 '+officialDateTime(row.official_checked_at)+'。 ':'';
  const schedule=sync.next_refresh?'下次官方更新尝试：'+officialDateTime(sync.next_refresh)+'。 ':'';
  $('account-note').textContent=dailyUnavailable+rowTime+schedule+'服务统计截止时间、每日时区未由接口提供；接口读取成功不代表日统计已完整。概览仍按北京时间统计。';
  renderReconciliationTable();
}
$('account-day').addEventListener('change',()=>{if(!$('account-day').value||!$('account-day').checkValidity())return;accountSelectedDate=$('account-day').value;accountFollowLatest=false;renderAccountReference();});
$('account-day').addEventListener('blur',()=>{if(!$('account-day').value||!$('account-day').checkValidity())$('account-day').value=accountSelectedDate;});
function accountStep(direction){const buckets=accountBuckets(),row=direction<0?buckets.findLast(b=>b.date<accountSelectedDate):buckets.find(b=>b.date>accountSelectedDate);if(row){accountSelectedDate=row.date;accountFollowLatest=false;renderAccountReference();}}
$('account-prev').addEventListener('click',()=>accountStep(-1));$('account-next').addEventListener('click',()=>accountStep(1));
$('account-latest').addEventListener('click',()=>{accountFollowLatest=true;renderAccountReference();});
$('reconcile-basis').addEventListener('change',renderAccountReference);$('reconcile-period').addEventListener('change',renderReconciliationTable);
async function refreshReconciliation(){
  if(reconciliationPending)return;reconciliationPending=true;$('refresh').disabled=true;
  try{
    const response=await fetch('/api/reconciliation',{cache:'no-store',signal:AbortSignal.timeout(15000)});
    if(!response.ok)throw new Error('暂时无法读取本地对账记录，已有数据保留。');
    reconciliationData=await response.json();renderAccountReference();
    if(activeTab==='reconciliation'){$('error').hidden=!reconciliationData.collector?.error;$('error').textContent=reconciliationData.collector?.error||'';$('status').textContent='本机记录每 3 秒更新 · 官方每小时读取';$('updated').textContent='本机最近扫描 '+officialDateTime(reconciliationData.collector?.last_scan);}
  }catch(error){if(activeTab==='reconciliation'){$('error').hidden=false;$('error').textContent=error.name==='TimeoutError'?'本机对账读取超时，已有数据保留。':error.message;}}
  finally{reconciliationPending=false;$('refresh').disabled=pending;}
}
$('account-refresh').addEventListener('click',async()=>{
  if(accountRequestPending)return;accountRequestPending=true;accountUiError='';renderAccountReference();
  try{
    const response=await fetch('/api/account-usage/refresh',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}',signal:AbortSignal.timeout(5000)});
    const state=await response.json();
    if(response.status===429||response.ok){accountReference().sync=state;renderAccountReference();await refreshReconciliation();}
    else throw new Error('官方更新暂不可用，请稍后重试。');
  }catch(error){accountUiError=error.name==='TimeoutError'?'官方更新请求超时，已有记录保留。':'官方更新请求失败，已有记录保留。';accountErrorCheckedAt=accountReference().checked_at;}
  finally{accountRequestPending=false;renderAccountReference();}
});
$('export-reconciliation').addEventListener('click',async()=>{
  const button=$('export-reconciliation');button.disabled=true;
  try{
    const response=await fetch('/api/reconciliation/export?'+new URLSearchParams({basis:$('reconcile-basis').value,period:$('reconcile-period').value}),{signal:AbortSignal.timeout(15000)});
    if(!response.ok)throw new Error('对账导出失败，请重试。');
    const url=URL.createObjectURL(await response.blob()),a=document.createElement('a');a.href=url;a.download='Codex-对账-'+$('reconcile-basis').value+'-'+dayNow()+'.csv';document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),30000);
  }catch(error){$('error').hidden=false;$('error').textContent=error.message;}
  finally{button.disabled=reconciliationRows().length===0;}
});
const statuses={connected:'已接入',error:'读取异常',not_found:'未找到记录',pending:'等待读取',unverified:'尚未接入'};
function renderSources(){const fragment=document.createDocumentFragment();for(const c of data.capabilities){const item=document.createElement('div');item.className='source-item';const name=document.createElement('strong'),status=document.createElement('span'),desc=document.createElement('p');name.textContent=c.software;status.textContent=statuses[c.status]||'待核验';status.className='source-status '+(c.status==='connected'?'connected':'muted');desc.textContent=sourceInfo[c.software]||'读取本地可用的计数记录。';item.append(name,status,desc);fragment.append(item);}$('support').replaceChildren(fragment);
  const t=data.totals,quality=[['记录内加总核对',`${n(t.input_tokens)} 输入 + ${n(t.output_tokens)} 输出 = ${n(t.total_tokens)} Token`],['逐次请求记录',`${n(t.records-t.legacy_records)} 条 · ${n(t.request_tokens)} Token`],['旧格式推算',`${n(t.legacy_records)} 条 · ${n(t.legacy_tokens)} Token（已包含在总量中）`],['缓存字段覆盖',`${n(t.records-t.cache_unknown_records)} / ${n(t.records)} 条；未知 ${n(t.cache_unknown_records)} 条`],['推理字段覆盖',`${n(t.records-t.reasoning_unknown_records)} / ${n(t.records)} 条`],['缓存写入字段覆盖',`${n(t.records-t.write_unknown_records)} / ${n(t.records)} 条`],['Codex 子代理标记',`${n(t.subagent_records)} 条计数记录（已去重并包含在总量中）`]];
  const q=document.createDocumentFragment();for(const[label,value]of quality){const dt=document.createElement('dt'),dd=document.createElement('dd');dt.textContent=label;dd.textContent=value;q.append(dt,dd);}$('quality').replaceChildren(q);
  const diagnostics=document.createDocumentFragment(),d=data.diagnostics;
  const lines=[`Codex：检查 ${n(d.indexed_files)} 个文件，${n(d.baseline_gaps)} 处历史基线不完整，${n(d.resets)} 处旧计数重置，${n(d.mixed_gaps)} 处新旧记录未完整对齐，${n(d.invalid+d.malformed)} 条无效记录，${n(d.conflicts)} 处计数冲突。无法确认的部分已跳过。`];
  for(const[name,s]of Object.entries(data.adapter_diagnostics))lines.push(`${name}：${n(s.records??s.candidates??0)} 条候选记录，${n(s.missing_usage??0)} 条未提供用量，${n(s.invalid??0)} 条无效计数，${n(s.conflicts??0)} 处计数冲突${s.rollup_mismatch?`；${n(s.rollup_mismatch)} 处顶层汇总不一致（未采用）`:''}${s.invalid_detail?`；${n(s.invalid_detail)} 条推理明细不合理（保留有效主计数）`:''}。`);
  for(const text of lines){const p=document.createElement('p');p.textContent=text;diagnostics.append(p);}$('diagnostics').replaceChildren(diagnostics);
}
async function refresh(){if(activeTab==='reconciliation')return refreshReconciliation();if(pending){again=true;return;}const requested=query();pending=true;$('refresh').disabled=true;document.querySelector('main').setAttribute('aria-busy','true');try{const response=await fetch('/api/stats?'+requested,{cache:'no-store',signal:AbortSignal.timeout(15000)});if(!response.ok)throw new Error(response.status===400?'日期或筛选条件无效，请检查后重试。':'暂时无法读取本地统计，请稍后刷新。');const next=await response.json();if(requested!==query()){again=true;return;}data=next;activeQuery=requested;render();}catch(error){$('error').hidden=false;$('error').textContent=error.name==='TimeoutError'?'本地读取超时，请稍后刷新。':error.message;}finally{pending=false;$('refresh').disabled=false;document.querySelector('main').setAttribute('aria-busy','false');if(again){again=false;refresh();}}}
async function download(view){if(!data||pending||activeQuery!==query())return;const params=new URLSearchParams(activeQuery);if(view==='series'){params.set('period','custom');params.set('start',chartData().range.start);params.set('end',chartData().range.end);}params.set('view',view);if(view==='rows')params.set('search',$('search').value.trim());const button=$(view==='rows'?'export-rows':'export-trend');button.disabled=true;try{const response=await fetch('/api/export?'+params,{signal:AbortSignal.timeout(15000)});if(!response.ok)throw new Error('导出失败，请重试。');const url=URL.createObjectURL(await response.blob()),a=document.createElement('a');a.href=url;const range=view==='series'?chartData().range:data.range;a.download=`Token-${view==='rows'?'明细':'趋势'}-${range.start}-${range.end}.csv`;document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),30000);}catch(error){$('error').hidden=false;$('error').textContent=error.message;}finally{button.disabled=false;}}
$('export-rows').addEventListener('click',()=>download('rows'));$('export-trend').addEventListener('click',()=>download('series'));
refresh();setInterval(()=>{if(!document.hidden&&(activeTab==='reconciliation'||$('period').value!=='custom'||activeQuery===query()))refresh();},3000);

new ResizeObserver(()=>{if(data&&!$('overview').hidden)requestAnimationFrame(renderChart);}).observe($('chart'));
