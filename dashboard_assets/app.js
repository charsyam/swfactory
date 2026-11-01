const byId = id => document.getElementById(id);
function node(tag, value = '', className = '') {
  const element = document.createElement(tag);
  element.textContent = value;
  if (className) element.className = className;
  return element;
}
function show(container, items, render, empty) {
  container.replaceChildren();
  if (!items.length) { container.append(node('p', empty, 'empty')); return; }
  items.forEach(item => container.append(render(item)));
}
function eventRow(event) {
  const row = node('div', '', 'event');
  row.append(node('time', event.at, 'meta'),
             node('span', event.actor + (event.recipient ? ' → ' + event.recipient : ''), 'status'),
             node('span', event.summary, 'summary'));
  return row;
}
const groupInfo = {
  answer: ['답변 필요', 'owner가 질문에 답해야 다음 단계로 진행합니다.'],
  recover: ['실패 복구', '원인을 확인하고 재시도 조건을 충족해야 합니다.'],
  review: ['PR 검토·병합', '구현 결과를 검토하고 owner가 직접 병합합니다.'],
  approve: ['승인 판단', '명세를 확인한 후 accept 라벨로 승인합니다.']
};
function workRow(item) {
  const row = node('div', '', 'work-row');
  const main = node('div');
  const link = node('a', '#' + item.number);
  link.href = '/issue/' + item.number;
  main.append(link, node('span', item.title, 'title'));
  const parts = [];
  if (item.children.length) parts.push('하위 작업 ' + item.child_closed + '/' + item.children.length + ' 닫힘');
  if (item.open_prs.length) parts.push('열린 PR ' + item.open_prs.map(n => '#' + n).join(', '));
  if (item.merged_prs.length) parts.push('병합 PR ' + item.merged_prs.length + '개');
  if (item.local_events) parts.push('로컬 실행 기록 ' + item.local_events + '건');
  if (!parts.length) parts.push('연결된 구현 기록 없음');
  main.append(node('small', parts.join(' · '), 'meta'));
  const action = node('div', '', 'next');
  action.append(node('b', '다음 단계'), node('span', item.next_step));
  row.append(main, action);
  return row;
}
function groupBlock(key, items) {
  const group = node('div', '', 'group ' + key);
  const head = node('div', '', 'group-head');
  head.append(node('h3', groupInfo[key][0]), node('small', items.length + '건 · ' + groupInfo[key][1]));
  group.append(head);
  items.forEach(item => group.append(workRow(item)));
  return group;
}
function renderBoard(board) {
  byId('repo').textContent = board.repo;
  byId('updated').textContent = '화면 갱신 ' + new Date().toLocaleTimeString();
  byId('source-note').textContent = board.synced_at
    ? 'GitHub 확인 ' + board.synced_at + (board.github_error ? ' · 조회 실패, 이전 결과 표시' : '')
    : 'GitHub 조회 불가' + (board.github_error ? ' · ' + board.github_error : '');
  const decisions = ['answer', 'recover', 'review', 'approve'];
  const count = decisions.reduce((sum, key) => sum + board.groups[key].length, 0);
  byId('decision-count').textContent = count;
  byId('automation-count').textContent = board.groups.running.length + board.groups.queued.length;
  const decisionArea = byId('decisions');
  decisionArea.replaceChildren();
  decisions.forEach(key => { if (board.groups[key].length) decisionArea.append(groupBlock(key, board.groups[key])); });
  if (!count) decisionArea.append(node('p', '현재 owner의 결정이 필요한 작업이 없습니다.', 'empty'));
  const run = board.latest_run;
  byId('run-status').textContent = run
    ? '최근 실행 ' + run.started_at + ' · ' + run.status
    : '이 서버에서 기록된 실행이 없습니다.';
  show(byId('active'), board.active_calls, call => {
    const row = node('div', '', 'flow-row');
    row.append(node('span', '진행', 'pill'), node('span', (call.recipient || 'Codex') + ' · 이슈 #' + (call.issue_number || '?') + ' · ' + call.summary),
               node('span', call.at, 'meta status'));
    return row;
  }, '현재 실행 중인 에이전트 호출은 없습니다.');
  const automation = byId('automation');
  automation.replaceChildren();
  for (const key of ['running', 'queued']) board.groups[key].forEach(item => automation.append(workRow(item)));
  if (!board.groups.running.length && !board.groups.queued.length)
    automation.append(node('p', '자동화 대기 또는 진행 중인 이슈가 없습니다.', 'empty'));
  byId('unlinked-section').hidden = !board.unlinked_prs.length;
  show(byId('unlinked'), board.unlinked_prs, pr => {
    const row = node('div', '', 'flow-row');
    const link = node('a', 'PR #' + pr.number);
    link.href = 'https://github.com/' + board.repo + '/pull/' + pr.number;
    row.append(link, node('span', pr.title), node('span', '연결 확인 필요', 'meta status'));
    return row;
  }, '');
  byId('completed-count').textContent = '(' + board.groups.completed.length + ')';
  show(byId('completed'), board.groups.completed, workRow, '완료된 이슈가 없습니다.');
  byId('history-section').hidden = !board.history_count;
  if (board.history_count) fetch('/api/state', {cache: 'no-store'}).then(response => response.json())
    .then(state => show(byId('events'), state.events.slice(0, 20), eventRow, '기록된 전달 이벤트가 없습니다.'))
    .catch(() => { byId('events').textContent = '실행 기록을 읽지 못했습니다.'; });
}
async function refreshBoard() {
  if (location.pathname !== '/') return;
  try {
    const response = await fetch('/api/board', {cache: 'no-store'});
    if (!response.ok) throw Error('HTTP ' + response.status);
    renderBoard(await response.json());
  } catch (error) {
    byId('updated').textContent = '작업 현황을 읽지 못했습니다: ' + error.message;
  }
}
refreshBoard();
setInterval(refreshBoard, 15000);
