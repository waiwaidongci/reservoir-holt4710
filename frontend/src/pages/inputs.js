import {feedback, field} from '../components/common/fields.js';
import {empty, panel} from '../components/common/panels.js';
import {table} from '../components/common/table.js';
import {handler} from '../hooks/action.js';
import {session} from '../stores/session.js';
import {workspace} from '../stores/workspace.js';
import {button, el, link, p} from '../utils/dom.js';
import {date} from '../utils/format.js';

export async function inputs(route, refresh) {
  const {spec,datasets} = workspace;
  const selected = datasets.find(item => item.id === route.id) || datasets[0];
  const error = feedback();
  const sample = button('下载示例 CSV', {class:'quiet',onClick:event=>handler(sample,error,()=>workspace.download('/template',spec.sample_name))(event)});
  const handoverSample = button('下载换表示例 CSV', {class:'quiet',onClick:event=>handler(handoverSample,error,()=>workspace.download('/template/handover','澄湾_RW-02_换表交接示例.csv'))(event)});
  const detail = selected && await workspace.getDataset(selected.id,route.offset);
  const columns = spec.fields.map(field => ({key:field.key,label:field.label}));
  const ledger = detail?.ledger;
  const ledgerInfo = ledger && el('div',{class:'ledger-summary','data-testid':'ledger-summary'},
    el('p',{class:'eyeline'},`台账版本 v${ledger.version} · 指纹 ${ledger.checksum.slice(0,12)}…`),
    el('ul',{class:'method-list'},
      el('li',{},`换表 ${ledger.handovers.length} 次，已折算 ${ledger.handovers.filter(item=>item.consistent).length} 次`),
      ledger.isolation
        ? el('li',{},`自 ${ledger.isolation.date} 起隔离：${ledger.isolation.reason}；此前分段仍可预报`)
        : el('li',{},'没有隔离读数，全部读数已折回原刻度'),
      el('li',{},`分段：${ledger.segments.map(seg=>`${seg.start}–${seg.end}（${seg.state === 'usable' ? '可预报' : '隔离'}）`).join('；') || '—'}`)));
  const preview = detail ? panel('批次明细', el('div',{class:'actions'},
    el('label',{for:'dataset-id',class:'sr'}, '选择数据批次'),
    el('select',{id:'dataset-id','aria-label':'选择数据批次',onChange:event=>{location.hash=`#inputs/${event.target.value}`;}},
      datasets.map(item=>el('option',{value:item.id,selected:item.id === selected.id}, item.name))),
    link('使用此批次运行',`#run/${detail.id}`,{class:'quiet'})),
    p(`${detail.filename} · ${detail.row_count} 行 · ${date(detail.created_at)}`,'filename'),
    ledgerInfo,
    table(columns,detail.rows,{caption:'批次明细',pageSize:16}),
    el('div',{class:'pagination'},
      route.offset > 0 && link('上一批明细',`#inputs/${detail.id}/${Math.max(0,route.offset-16)}`,{class:'quiet'}),
      p(`${detail.rows.length ? route.offset+1 : route.offset}–${route.offset+detail.rows.length} / ${detail.row_count}`),
      route.offset+16 < detail.row_count && link('下一批明细',`#inputs/${detail.id}/${route.offset+16}`,{class:'quiet'})))
    : empty('没有可用的批次。请导入示例或业务 CSV。');
  const submit = button('校验并导入', {type:'submit',class:'primary'});
  const upload = session.canWrite && el('form',{class:'fields',onSubmit:handler(submit,error,async event=>{
    const data = new FormData(event.target);
    const file = data.get('source');
    if (!file?.size) throw new Error('请选择一个 CSV 文件');
    if (file.size > 2_000_000) throw new Error('CSV 最多 2 MB');
    const item = await workspace.upload({name:data.get('batch_name'),filename:file.name,csv_text:await file.text()});
    const target = `#inputs/${item.id}`;
    const note = item.ledger_changed === false ? '这批读数已入账，未产生新台账版本。'
      : item.recomputed?.length ? `台账已更新，重算 ${item.recomputed.length} 条受影响的后续预报。`
      : '台账已更新。';
    if (location.hash === target) { await refresh(); error.textContent = note; }
    else location.hash = target;
  })},field({key:'batch_name',label:'批次名称',type:'string',max_length:120}),
    el('label',{class:'field',for:'source'}, el('span',{},'业务 CSV'),el('input',{id:'source',type:'file',name:'source',accept:'.csv,text/csv',required:true})), submit);
  return el('div',{class:'two-col'},el('div',{class:'wide-panel'},preview),el('aside',{class:'sidebar-panel'},
    panel('数据导入',p('导入后保留原批次和校验指纹，同一份数据重复导入不会重复入账；补录会生成新台账版本。','subtle'),el('div',{class:'actions'},sample,handoverSample),upload,error),
    panel('字段说明',el('dl',{class:'dictionary'},spec.fields.map(item=>el('div',{},el('dt',{},item.label),
      el('dd',{},item.key,item.required === false ? ' · 可空' : ' · 必填',item.options ? ` · ${item.options.join(' / ')}` : '')))))));
}
