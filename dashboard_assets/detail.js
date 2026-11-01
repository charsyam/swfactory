const issueMatch = location.pathname.match(/^\/issue\/([1-9][0-9]*)$/);
if (issueMatch) {
  byId('overview').hidden = true;
  byId('issue-detail').hidden = false;
  const number = Number(issueMatch[1]);
  let older = [];
  let cursor = null;

  function fact(text) { return node('span', text, 'fact'); }
  function issueLink(repo, issueNumber) {
    const link = node('a', '#' + issueNumber);
    link.href = '/issue/' + issueNumber;
    return link;
  }
  function prLink(repo, pull, label) {
    const link = node('a', label || 'PR #' + pull.number);
    link.href = 'https://github.com/' + repo + '/pull/' + pull.number;
    return link;
  }
  function deliveryRow(repo, issue, pulls) {
    const row = node('div', '', 'flow-row');
    row.append(issueLink(repo, issue.number));
    const middle = node('div');
    middle.append(node('span', issue.title));
    const related = pulls.filter(p => issue.pull_requests.includes(p.number));
    const links = node('small', '', 'meta');
    if (related.length) related.forEach((pull, index) => {
      if (index) links.append(' · ');
      links.append(prLink(repo, pull, 'PR #' + pull.number + ' ' + (pull.mergedAt ? '병합' : pull.state === 'OPEN' ? '검토 중' : '닫힘')));
    });
    else links.textContent = '연결된 PR 없음';
    middle.append(links);
    row.append(middle, node('span', issue.state === 'CLOSED' ? '이슈 닫힘' : '이슈 열림', 'status'));
    return row;
  }
  function renderIssue(gh, local, board) {
    const issue = gh.issues.find(item => item.number === number);
    if (!issue && !local) throw Error('이슈를 찾을 수 없습니다.');
    byId('repo').textContent = gh.repo;
    byId('updated').textContent = '화면 갱신 ' + new Date().toLocaleTimeString();
    byId('github-issue').href = 'https://github.com/' + gh.repo + '/issues/' + number;
    byId('issue-title').textContent = '#' + number + ' ' + (issue ? issue.title : '로컬 실행 기록');
    const work = Object.values(board.groups).flat().find(item => item.number === number);
    byId('issue-action').textContent = work ? '다음 단계 · ' + work.next_step
      : issue && issue.parent ? '하위 작업 · 상위 이슈 #' + issue.parent + '의 흐름을 확인하세요.'
      : issue && issue.state === 'CLOSED' ? 'GitHub 이슈가 닫혔습니다.'
      : '현재 판단 정보가 없습니다.';
    const facts = byId('issue-facts');
    facts.replaceChildren();
    if (issue) {
      facts.append(fact(issue.state === 'CLOSED' ? '이슈 닫힘' : '이슈 열림'));
      issue.labels.forEach(label => facts.append(fact(label)));
      facts.append(fact('GitHub 갱신 ' + issue.updatedAt));
    }
    facts.append(fact(local ? '로컬 실행 기록 ' + local.events.length + '건 이상' : '로컬 실행 기록 없음'));
    if (gh.error) facts.append(fact('GitHub 조회 실패 · 이전 결과'));
    const delivery = byId('delivery');
    delivery.replaceChildren();
    if (issue) {
      if (issue.parent) {
        const parent = gh.issues.find(item => item.number === issue.parent);
        if (parent) delivery.append(deliveryRow(gh.repo, parent, gh.pull_requests));
      }
      delivery.append(deliveryRow(gh.repo, issue, gh.pull_requests));
      issue.children.map(child => gh.issues.find(item => item.number === child)).filter(Boolean)
        .forEach(child => delivery.append(deliveryRow(gh.repo, child, gh.pull_requests)));
    } else delivery.append(node('p', 'GitHub 이슈 상태를 읽지 못했습니다.', 'empty'));

    const verification = byId('verification');
    verification.replaceChildren();
    const results = local ? local.artifacts.filter(item => item.kind === 'tests_verified') : [];
    if (results.length) results.forEach(result => {
      const item = node('div', '', 'evidence');
      const details = result.details;
      item.append(node('strong', 'Task ' + (result.task_number || '?') + ' · 검증 완료'),
                  node('div', '테스트 ' + (details.test_case_count ?? '?') + '건 · ' + (details.categories || []).join(', '), 'meta'),
                  node('small', result.at, 'meta'));
      verification.append(item);
    });
    else verification.append(node('p', '이 서버에 기록된 테스트 결과가 없습니다. 연결된 PR의 테스트 설명을 확인하세요.', 'empty'));
    const calls = local ? local.calls : [];
    byId('agent-section').hidden = !calls.length;
    show(byId('calls'), calls, call => {
      const item = node('div', '', 'call');
      item.append(node('strong', call.role + ' · ' + call.status + ' · ' + (call.elapsed_ms == null ? '진행 중' : call.elapsed_ms + ' ms')),
                  node('small', '호출 ' + call.id.slice(0, 12) + (call.task_number ? ' · Task ' + call.task_number : '')),
                  node('div', '입력 출처: ' + (call.sources.length ? call.sources.join(', ') : '기록 없음')),
                  node('div', '결과: ' + (call.output || '기록 없음')));
      if (call.references.length) item.append(node('small', '참조: ' + call.references.join(', ')));
      return item;
    }, '');
    const measurements = local ? local.performance : [];
    byId('performance-section').hidden = !measurements.length;
    show(byId('performance'), measurements, item => {
      const details = item.details;
      return node('div', (details.metric || '지표') + ' · 목표 ' + (details.target ?? '?') +
        ' · 측정 ' + (details.value ?? '?') + ' ' + (details.unit || ''), 'measurement');
    }, '');
    const trace = byId('trace-section');
    trace.hidden = !local || !local.events.length;
    if (local) {
      const events = [...local.events, ...older].filter((item, index, all) => all.findIndex(other => other.id === item.id) === index)
        .sort((a, b) => b.id - a.id);
      show(byId('issue-events'), events, eventRow, '기록 없음');
      byId('load-older').hidden = !cursor;
    }
  }
  async function loadIssue() {
    try {
      const [ghResponse, localResponse, boardResponse] = await Promise.all([
        fetch('/api/github', {cache: 'no-store'}),
        fetch('/api/issues/' + number, {cache: 'no-store'}),
        fetch('/api/board', {cache: 'no-store'})
      ]);
      if (!ghResponse.ok || !boardResponse.ok || (localResponse.status !== 200 && localResponse.status !== 404))
        throw Error('데이터 요청 실패');
      const [gh, board] = await Promise.all([ghResponse.json(), boardResponse.json()]);
      const local = localResponse.ok ? await localResponse.json() : null;
      if (!older.length) cursor = local?.next_cursor ?? null;
      renderIssue(gh, local, board);
    } catch (error) { byId('issue-title').textContent = '이슈를 읽지 못했습니다: ' + error.message; }
  }
  byId('load-older').addEventListener('click', async () => {
    if (!cursor) return;
    const response = await fetch('/api/issues/' + number + '?before=' + cursor, {cache: 'no-store'});
    if (!response.ok) return;
    const page = await response.json();
    older.push(...page.events);
    cursor = page.next_cursor;
    await loadIssue();
  });
  loadIssue();
  setInterval(loadIssue, 15000);
}
