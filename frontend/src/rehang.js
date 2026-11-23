const token = () => localStorage.getItem('cdt_token');
const api = '/api';
const STATUS = {calculated: '已试算', confirmed: '已确认改挂', superseded: '已被新试算替代', invalidated: '证据变化已失效'};

const element = (tag, text) => {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  return node;
};

async function request(path, options = {}) {
  const response = await fetch(api + path, {
    method: options.body ? 'POST' : 'GET',
    headers: {'Authorization': `Bearer ${token() || ''}`, 'Content-Type': 'application/json'},
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = data.detail;
    const message = typeof detail === 'string' ? detail : detail?.message || `操作失败 (${response.status})`;
    const error = new Error(message);
    error.code = typeof detail === 'object' ? detail?.code : undefined;
    error.status = response.status;
    throw error;
  }
  return data;
}

const launcher = element('button', '改挂工作台');
launcher.type = 'button';
launcher.id = 'rehang-open';
launcher.hidden = true;
const dialog = element('dialog');
dialog.id = 'rehang';
dialog.setAttribute('aria-labelledby', 'rehang-title');
const header = element('header');
const title = element('h2', '改挂工作台');
title.id = 'rehang-title';
const close = element('button', '关闭');
close.addEventListener('click', () => dialog.close());
header.append(title, close);

const status = element('p');
status.setAttribute('role', 'status');
const body = element('div');
body.className = 'rehang-body';
dialog.append(header, status, body);
document.body.append(launcher, dialog);

let catalog = {vessels: [], containers: [], role: 'viewer', user: ''};
let active = 0;
let current = null;

function showLauncher() {
  try { launcher.hidden = !token(); } catch { launcher.hidden = true; }
  if (launcher.hidden && dialog.open) dialog.close();
}
showLauncher();
window.addEventListener('storage', showLauncher);
setInterval(showLauncher, 600);

const isSupervisor = () => catalog.role === 'supervisor';
const pct = value => value === undefined || value === null ? '—' : `${(value * 100).toFixed(2)}%`;
const signed = value => value === undefined || value === null ? '—'
  : `${value > 0 ? '+' : ''}${(value * 100).toFixed(2)} pp`;

function newKey(id) {
  if (globalThis.crypto?.randomUUID) return `rehang-${id}-${crypto.randomUUID().replaceAll('-', '').slice(0, 24)}`;
  const random = Math.random().toString(36).slice(2) + Date.now().toString(36);
  return `rehang-${id}-${random.slice(0, 24)}`;
}

function indentKey(id) {
  // One key per trial attempt, kept across a retry (lost response / double
  // click / page reload) so the server can replay instead of moving twice.
  const name = `rehang-confirm-${id}`;
  let key = sessionStorage.getItem(name);
  if (!key) {
    key = newKey(id);
    sessionStorage.setItem(name, key);
  }
  return key;
}

function dropKey(id) { sessionStorage.removeItem(`rehang-confirm-${id}`); }

function sideTable(side, label) {
  const table = element('table');
  const head = table.createTHead().insertRow();
  for (const text of ['船次', '改挂前严重延误概率', '改挂后严重延误概率', '变化']) {
    const th = element('th', text);
    th.scope = 'col';
    head.append(th);
  }
  const row = table.createTBody().insertRow();
  row.append(
    element('td', `${label} ${side.call_no}`),
    element('td', pct(side.before.severe_probability)),
    element('td', pct(side.after.severe_probability)),
    element('td', signed(side.delta)),
  );
  return table;
}

function renderEvaluation(trial) {
  const evaluation = trial.evaluation || {};
  const section = element('section');
  section.className = 'rehang-evaluation';
  section.append(element('h3', `试算 ${trial.trial_no}`));
  if (trial.stale) {
    const warn = element('p', '箱单或有效闸口记录在试算后已变化，本次试算失效，需重新试算后再确认。');
    warn.className = 'rehang-warn';
    section.append(warn);
  }
  const verdict = element('p', evaluation.eligible
    ? '规则满足：原船次下降且接收船次未上升，可确认改挂。'
    : '规则不满足：需原船次严重延误概率下降且接收船次不上升，本批不能确认改挂。');
  verdict.className = evaluation.eligible ? 'rehang-ok' : 'rehang-block';
  section.append(
    verdict,
    sideTable(evaluation.source, '原船次'),
    sideTable(evaluation.target, '接收船次'),
  );
  const boxes = element('p', `本批箱号（${evaluation.containers?.length || 0}）：`
    + (evaluation.containers || []).map(c => c.container_no).join('、'));
  section.append(boxes);
  if (evaluation.rule) section.append(element('small', evaluation.rule));
  return section;
}

function renderTrial(trial) {
  const wrapper = element('section');
  wrapper.className = 'rehang-current';
  wrapper.append(renderEvaluation(trial));
  if (trial.result) renderResult(wrapper, trial.result);
  wrapper.append(renderActions(trial));
  body.append(wrapper);
}

function renderActions(trial) {
  const actions = element('div');
  actions.className = 'rehang-actions';
  if (trial.status !== 'calculated' || !isSupervisor()) {
    if (!isSupervisor()) actions.append(element('small', '仅主管可发起、重算和确认改挂。'));
    return actions;
  }
  const note = element('input');
  note.placeholder = '确认说明（可选）';
  note.maxLength = 500;
  note.setAttribute('aria-label', '确认说明');
  const recalc = element('button', '重新试算');
  const confirm = element('button', trial.stale ? '确认改挂（需先重算）' : '确认改挂');
  confirm.className = 'primary';
  if (trial.stale) {
    confirm.disabled = true;
    note.disabled = true;
  }
  recalc.addEventListener('click', async () => {
    recalc.disabled = true; confirm.disabled = true;
    status.textContent = '正在按当前箱单与闸口记录重算…';
    try {
      const fresh = await request(`/rehang/trials/${trial.id}/recalculate`, {body: {}});
      dropKey(trial.id);
      current = fresh;
      await refreshList();
      renderBody();
      status.textContent = `已生成新试算 ${fresh.trial_no}`;
    } catch (error) {
      status.textContent = error.message;
      recalc.disabled = false; confirm.disabled = false;
    }
  });
  confirm.addEventListener('click', async () => {
    recalc.disabled = true; confirm.disabled = true;
    status.textContent = '正在按箱单逐笔核对并提交确认…';
    try {
      const result = await request(`/rehang/trials/${trial.id}/confirm`, {
        body: {idempotency_key: indentKey(trial.id), note: note.value},
      });
      dropKey(trial.id);
      await refreshList();
      await loadTrial(trial.id);
      status.textContent = result.replayed
        ? `该次确认此前已办成，返回原结果：${result.moved_count} 箱已改挂至 ${result.target_call_no}`
        : `已确认改挂：${result.moved_count} 箱 → ${result.target_call_no}`;
    } catch (error) {
      status.textContent = error.message;
      recalc.disabled = false; confirm.disabled = false;
      if (['EVIDENCE_CHANGED', 'TRIAL_INVALID', 'RULE_NOT_MET', 'BATCH_CONFLICT'].includes(error.code)) {
        await loadTrial(trial.id).catch(() => {});
      }
    }
  });
  actions.append(note, recalc, confirm);
  return actions;
}

function renderResult(wrapper, result) {
  const box = element('div');
  box.className = 'rehang-result';
  box.append(
    element('h4', '办理结果'),
    element('p', `${result.trial_no} · ${STATUS[result.status] || result.status} · ${result.confirmed_by} · ${result.confirmed_at}`),
    element('p', `箱号：${(result.container_nos || []).join('、')}`),
    element('p', `原船次严重延误概率 ${pct(result.before?.source_severe_probability)} → ${pct(result.after?.source_severe_probability)}；`
      + `接收船次 ${pct(result.before?.target_severe_probability)} → ${pct(result.after?.target_severe_probability)}`),
  );
  wrapper.append(box);
}

function renderCreate() {
  const section = element('section');
  section.className = 'rehang-create';
  section.append(element('h3', '新建试算'));
  if (!isSupervisor()) {
    section.append(element('p', '当前角色为只读，可在下方查看试算与办理结果。'));
    return section;
  }
  const form = element('form');
  const sourceLabel = element('label', '原船次');
  const source = element('select');
  const targetLabel = element('label', '接收船次');
  const target = element('select');
  for (const vessel of catalog.vessels) {
    source.add(new Option(`${vessel.call_no} · ${vessel.vessel_name}`, vessel.id));
    target.add(new Option(`${vessel.call_no} · ${vessel.vessel_name}`, vessel.id));
  }
  if (catalog.vessels.length > 1) target.value = catalog.vessels[1].id;
  sourceLabel.append(source);
  targetLabel.append(target);
  const boxField = element('fieldset');
  boxField.append(element('legend', '选择要改挂的整批箱号'));
  const list = element('div');
  list.className = 'rehang-boxes';
  const renderBoxes = () => {
    list.replaceChildren();
    const boxes = catalog.containers.filter(c => String(c.vessel_id) === source.value);
    if (!boxes.length) list.append(element('small', '该船次没有箱单记录'));
    for (const box of boxes) {
      const label = element('label');
      const input = element('input');
      input.type = 'checkbox';
      input.value = box.id;
      input.name = 'container';
      label.append(input, element('span', `${box.container_no} · ${box.cargo_class} · 查验 ${box.customs_status}`));
      list.append(label);
    }
  };
  source.addEventListener('change', renderBoxes);
  renderBoxes();
  boxField.append(list);
  const submit = element('button', '试算改挂影响');
  submit.type = 'submit';
  submit.className = 'primary';
  form.append(sourceLabel, targetLabel, boxField, submit);
  form.addEventListener('submit', async event => {
    event.preventDefault();
    const ids = [...list.querySelectorAll('input[name=container]:checked')].map(input => Number(input.value));
    if (!ids.length) { status.textContent = '请至少选择一个箱号'; return; }
    submit.disabled = true;
    status.textContent = '正在用同一模型计算两船次改挂前后概率…';
    try {
      const trial = await request('/rehang/trials', {
        body: {source_vessel_id: Number(source.value), target_vessel_id: Number(target.value), container_ids: ids},
      });
      current = trial;
      status.textContent = trial.evaluation.eligible
        ? '试算完成，规则满足，可确认改挂。'
        : '试算完成，规则不满足，本批不能确认改挂。';
      await refreshList();
      renderBody();
    } catch (error) {
      status.textContent = error.message;
    } finally {
      submit.disabled = false;
    }
  });
  section.append(form);
  return section;
}

function renderHistory(trials) {
  const section = element('section');
  section.className = 'rehang-history';
  section.append(element('h3', '试算与办理记录'));
  if (!trials.length) {
    section.append(element('p', '暂无试算记录'));
    return section;
  }
  const table = element('table');
  const head = table.createTHead().insertRow();
  for (const text of ['试算', '原船次', '接收船次', '原船次概率', '接收船次概率', '状态', '办理结果']) {
    const th = element('th', text);
    th.scope = 'col';
    head.append(th);
  }
  const tbody = table.createTBody();
  for (const trial of trials) {
    const row = tbody.insertRow();
    const link = element('button', trial.trial_no);
    link.className = 'link';
    link.addEventListener('click', () => loadTrial(trial.id));
    const nameCell = row.insertCell();
    nameCell.append(link);
    const evaluation = trial.evaluation || {};
    const source = evaluation.source || {};
    const target = evaluation.target || {};
    const probability = side => `${pct(side.before?.severe_probability)} → ${pct(side.after?.severe_probability)}`;
    row.append(
      element('td', trial.source_call_no || trial.source_vessel_id),
      element('td', trial.target_call_no || trial.target_vessel_id),
      element('td', probability(source)),
      element('td', probability(target)),
    );
    const stateCell = row.insertCell();
    stateCell.textContent = trial.stale ? '已失效（证据变化）' : (STATUS[trial.status] || trial.status);
    row.append(element('td', trial.result
      ? `${trial.result.moved_count} 箱 → ${trial.result.target_call_no}`
      : '—'));
  }
  section.append(table);
  return section;
}

function renderBody() {
  body.replaceChildren(renderCreate());
  if (current) renderTrial(current);
  body.append(renderHistory(catalog.trials || []));
}

let trials = [];
async function refreshList() {
  trials = await request('/rehang/trials');
  catalog.trials = trials;
}

async function loadTrial(id) {
  const sequence = ++active;
  try {
    const trial = await request(`/rehang/trials/${id}`);
    if (sequence !== active) return;
    current = trial;
    renderBody();
  } catch (error) {
    if (sequence === active) status.textContent = error.message;
  }
}

launcher.addEventListener('click', async () => {
  dialog.showModal();
  status.textContent = '正在加载改挂工作台…';
  body.replaceChildren();
  current = null;
  try {
    const [me, vessels, containers] = await Promise.all([
      request('/me'), request('/vessels'), request('/containers'),
    ]);
    catalog = {role: me.role, user: me.display_name || me.username, vessels, containers};
    await refreshList();
    status.textContent = isSupervisor()
      ? '选择原船次、接收船次与整批箱号后试算。'
      : '只读模式：可查看试算与办理结果。';
    renderBody();
  } catch (error) {
    status.textContent = error.message;
  }
});
