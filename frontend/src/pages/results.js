import {plot} from '../components/common/chart.js';
import {feedback} from '../components/common/fields.js';
import {empty, metrics, panel, pill} from '../components/common/panels.js';
import {table} from '../components/common/table.js';
import {REVIEW, STATUS} from '../constants/enums.js';
import {handler, poll} from '../hooks/action.js';
import {workspace} from '../stores/workspace.js';
import {button, el, h, link, p} from '../utils/dom.js';
import {date, value} from '../utils/format.js';

const STATE = {usable:'可预报', isolated:'隔离'};

function ledgerPanel(ledger) {
  if (!ledger) return null;
  const version = `v${ledger.version}`;
  const handovers = ledger.handovers || [];
  const summary = metrics([
    {label:'台账版本', value:version, detail:`指纹 ${ledger.checksum.slice(0,12)}…`},
    {label:'换表次数', value:handovers.length, detail:`${handovers.filter(item=>item.consistent).length} 次已折算`},
    {label:'隔离读数', value:ledger.counts?.isolated ?? 0,
      detail:ledger.isolation ? `${ledger.isolation.date} 起 · ${ledger.isolation.reason}` : '没有隔离'},
  ]);
  const handoverTable = table([
    {key:'date', label:'换表日期'}, {key:'from_meter', label:'原液位计'}, {key:'to_meter', label:'新液位计'},
    {key:'same_day_dual', label:'换表日双表', render:row=>value(row.same_day_dual)},
    {label:'新表偏差（m）', render:row=>value(row.offset_m)},
    {label:'累计折算（m）', render:row=>value(row.cumulative_offset_m)},
    {label:'状态', render:row=>pill(row.consistent ? '已折算' : '已隔离', row.consistent ? 'ok' : 'warning')},
    {label:'说明', render:row=>row.reason || (row.dual_readings?.length ? `${row.dual_readings.length} 组同日双表读数` : '—')},
  ], handovers, {caption:'换表交接偏差', empty:'本版台账没有换表记录。'});
  const segments = table([
    {key:'index', label:'段号'}, {key:'start', label:'起始日'}, {key:'end', label:'截止日'},
    {key:'days', label:'天数'}, {label:'状态', render:row=>pill(STATE[row.state] || row.state, row.state === 'usable' ? 'ok' : 'warning')},
    {key:'reason', label:'说明'},
  ], ledger.segments || [], {caption:'台账分段'});
  return panel(`台账版本 ${version}`, summary,
    p('本次预报固定使用这一版台账；之后补录会生成新版本并重算受影响的后续段，旧预报仍保留当时的读数与偏差。','subtle'),
    handoverTable, segments);
}

export async function results(route, refresh) {
  const {spec,runs,datasets} = workspace;
  const id = route.id || runs[0]?.id;
  if (!id) return panel(spec.pages[3],empty('还没有可查看的运行结果。','#run','前往运行'));
  const job = await workspace.getRun(id);
  const source = datasets.find(item => item.id === job.dataset_id);
  const message = feedback();
  const finished = item => item.status === 'complete' || item.status === 'failed';
  if (!finished(job)) {
    const original = location.hash;
    const status = el('p',{role:'status'},'后台正在计算，完成后会自动展示。');
    poll(()=>workspace.getRun(id),finished,()=>location.hash===original)
      .then(result=>{if (result && status.isConnected && location.hash===original) refresh();
        else if(status.isConnected) status.textContent='仍在执行，请点击刷新检查。';})
      .catch(error=>{if(status.isConnected) status.textContent=error.message;});
    return panel(job.name,pill(STATUS[job.status],job.status),status,button('刷新',{class:'quiet',onClick:refresh}));
  }
  const download = button('下载结果 CSV',{class:'quiet','data-testid':'export',onClick:event=>
    handler(download,message,()=>workspace.download(`/runs/${job.id}/export`,`${spec.short_name}-${job.id}.csv`))(event)});
  const history = el('label',{class:'field run-picker',for:'run-picker'},el('span',{},'切换运行记录'),
    el('select',{id:'run-picker',onChange:event=>{location.hash=`#results/${event.target.value}`;}},
      (runs.some(item=>item.id===job.id) ? runs : [job,...runs]).map(item=>el('option',{value:item.id,selected:item.id===job.id},item.name))));
  const result = job.result;
  const ledger = result ? await workspace.runLedger(job.id).catch(()=>null) : null;
  const analysis = result ? el('section',{class:'analysis','data-testid':'analysis-complete'},
    el('div',{class:'result-head'},p(job.name,'eyeline'),h(2,result.headline),p(result.note,'intro-copy'),
      el('div',{class:'actions'},pill(STATUS[job.status],job.status),download,message)),
    metrics(result.metrics),
    ledgerPanel(ledger),
    result.tables.map(item=>panel(item.title,table(item.columns,item.rows,{caption:item.title}))),
    result.charts.map(plot),
    (result.sections || []).map(item=>panel(item.title,p(item.text))))
    : panel(job.name,pill(STATUS[job.status],job.status),p(job.error,'feedback'),link('返回调整参数',`#run/${job.dataset_id}`,{class:'quiet'}));
  const inputs = spec.params.map(param=>el('div',{},el('dt',{},param.label),el('dd',{},value(job.params[param.key]))));
  return el('div',{},el('div',{class:'two-col'},el('div',{class:'wide-panel'},analysis),
    el('aside',{class:'sidebar-panel'},panel('运行来源',history,
      source && link(source.name,`#inputs/${source.id}`),
      ledger && p(`台账版本 v${ledger.version} · 指纹 ${ledger.checksum.slice(0,12)}…`,'subtle'),
      p(date(job.finished_at),'subtle'),
      el('dl',{class:'dictionary'},inputs)),
    result && panel(spec.decision, pill(REVIEW[job.review],job.review),
      link('打开复核记录',`#review/${job.id}`,{class:'quiet'})))));
}
