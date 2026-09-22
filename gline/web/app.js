'use strict';

// 受信メールの本文は信用できない入力として扱う。DOM への反映は必ず
// textContent 経由にして、URL は自動リンクせず全文をそのまま見せる。

const $ = (id) => document.getElementById(id);
const PAGE = 300;
const AUTO_SYNC_MS = 3 * 60 * 1000;

function savedKind() {
  try {
    const v = localStorage.getItem('gline.kind');
    if (v === 'people' || v === 'notice') return v;
  } catch (e) { /* プライベートウィンドウ等では読めない */ }
  return 'people';
}

function savedAccount() {
  try { return localStorage.getItem('gline.account') || ''; }
  catch (e) { return ''; }
}

const state = {
  accounts: [],
  account: savedAccount(),   // '' = すべて
  all: [],
  kind: savedKind(),
  conversations: [],
  activeKey: null,
  activeLabel: '',
  messages: [],
  oldestTs: null,
  exhausted: false,
  query: '',
  syncing: false,
};

/* ---------------- 共通 ---------------- */

async function api(path, opts) {
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({ error: 'サーバの応答が読めませんでした' }));
  if (!res.ok || data.error) throw new Error(data.error || ('HTTP ' + res.status));
  return data;
}

let toastTimer = null;
function toast(msg, ms = 4000) {
  const el = $('toast');
  el.textContent = msg;
  el.hidden = false;
  clearTimeout(toastTimer);
  if (ms) toastTimer = setTimeout(() => { el.hidden = true; }, ms);
}

function hueOf(str) {
  let h = 0;
  for (let i = 0; i < str.length; i++) h = (h * 31 + str.charCodeAt(i)) % 360;
  return h;
}

function avatarEl(label, key, cls) {
  const el = document.createElement('div');
  el.className = cls;
  const hue = hueOf(key || label);
  el.style.background = `hsl(${hue} 46% 46%)`;
  const ch = (label || '?').trim();
  el.textContent = ch ? Array.from(ch)[0].toUpperCase() : '?';
  return el;
}

const WD = ['日', '月', '火', '水', '木', '金', '土'];

function dayKey(ts) {
  const d = new Date(ts * 1000);
  return `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`;
}

function dayLabel(ts) {
  const d = new Date(ts * 1000);
  const today = new Date();
  const same = (a, b) => a.toDateString() === b.toDateString();
  const yest = new Date(today.getTime() - 86400000);
  if (same(d, today)) return '今日';
  if (same(d, yest)) return '昨日';
  return `${d.getFullYear()}年${d.getMonth() + 1}月${d.getDate()}日(${WD[d.getDay()]})`;
}

function clockLabel(ts) {
  const d = new Date(ts * 1000);
  return `${d.getHours()}:${String(d.getMinutes()).padStart(2, '0')}`;
}

function listTime(ts) {
  if (!ts) return '';
  const d = new Date(ts * 1000);
  const now = new Date();
  if (d.toDateString() === now.toDateString()) return clockLabel(ts);
  if (d.getFullYear() === now.getFullYear()) return `${d.getMonth() + 1}/${d.getDate()}`;
  return `${d.getFullYear()}/${d.getMonth() + 1}/${d.getDate()}`;
}

const RE_PREFIX = /^\s*((re|fwd?|fw|返信|転送)\s*[:：]|\[[^\]]{1,24}\])\s*/i;
function baseSubject(s) {
  let out = (s || '').trim();
  for (let i = 0; i < 6; i++) {
    const next = out.replace(RE_PREFIX, '');
    if (next === out) break;
    out = next;
  }
  return out;
}

/* ---------------- 会話一覧 ---------------- */

function accountColor(email) {
  return `hsl(${hueOf(email)} 52% 45%)`;
}

function renderAccounts() {
  const box = $('accounts');
  box.hidden = state.accounts.length < 2;
  if (box.hidden) { box.textContent = ''; return; }
  box.textContent = '';
  const items = [{ email: '', label: 'すべて' }].concat(state.accounts);
  for (const a of items) {
    const b = document.createElement('button');
    b.className = 'acct';
    b.setAttribute('aria-pressed', String(state.account === a.email));
    if (a.email) {
      b.style.setProperty('--acct-color', accountColor(a.email));
      const dot = document.createElement('i');
      b.appendChild(dot);
      b.title = a.email;
    }
    b.appendChild(document.createTextNode(a.label));
    b.addEventListener('click', () => switchAccount(a.email));
    box.appendChild(b);
  }
}

function switchAccount(email) {
  if (email === state.account) return;
  state.account = email;
  try { localStorage.setItem('gline.account', email); } catch (e) { /* 保存できなくても動く */ }
  state.activeKey = null;
  renderAccounts();
  loadConversations(false).catch((err) => toast(err.message));
}

async function loadConversations(keepActive = true) {
  const p = new URLSearchParams();
  if (state.query) p.set('q', state.query);
  if (state.account) p.set('account', state.account);
  const q = p.toString() ? '?' + p.toString() : '';
  const data = await api('/api/conversations' + q);
  state.all = data.conversations;
  applyKind();
  const wide = window.matchMedia('(min-width: 761px)').matches;
  if ((!keepActive || !state.activeKey) && wide) {
    if (state.conversations.length) openConversation(state.conversations[0].key);
  }
}

function applyKind() {
  state.conversations = state.all.filter((c) => c.kind === state.kind);
  renderTabs();
  renderConversations();
}

function renderTabs() {
  for (const [kind, tab, cnt] of [['people', 'tabPeople', 'cntPeople'],
                                  ['notice', 'tabNotice', 'cntNotice']]) {
    const list = state.all.filter((c) => c.kind === kind);
    const unread = list.reduce((a, c) => a + c.unread, 0);
    $(cnt).textContent = unread ? String(unread) : '';
    $(tab).setAttribute('aria-selected', String(state.kind === kind));
    $(tab).title = `${list.length} 会話 / 未読 ${unread} 通`;
  }
}

function switchKind(kind) {
  if (kind === state.kind) return;
  state.kind = kind;
  try { localStorage.setItem('gline.kind', kind); } catch (e) { /* 保存できなくても動く */ }
  state.activeKey = null;
  applyKind();
  const wide = window.matchMedia('(min-width: 761px)').matches;
  if (wide && state.conversations.length) openConversation(state.conversations[0].key);
}

function renderConversations() {
  const ul = $('convs');
  ul.textContent = '';
  if (!state.conversations.length) {
    const li = document.createElement('li');
    li.className = 'conv';
    li.style.color = 'var(--text-faint)';
    const acct = state.accounts.find((a) => a.email === state.account);
    li.textContent = state.query
      ? '該当する会話がありません。'
      : state.all.length
        ? 'このタブには会話がありません。'
        : acct
          ? `${acct.label}（${acct.email}）はまだ同期されていません。「同期」を押してください。`
          : 'まだ何も同期されていません。「同期」を押してください。';
    ul.appendChild(li);
    return;
  }
  for (const c of state.conversations) {
    const li = document.createElement('li');
    li.className = 'conv' + (c.key === state.activeKey ? ' active' : '');
    li.tabIndex = 0;
    li.dataset.key = c.key;

    li.appendChild(avatarEl(c.label, c.key, 'avatar' + (c.is_direct ? '' : ' group')));

    // どのアカウントに届いたものか（複数アカウントを一緒に見ているときだけ）
    if (!state.account && state.accounts.length > 1) {
      for (const email of c.accounts) {
        const dot = document.createElement('span');
        dot.className = 'conv-badge';
        dot.style.background = accountColor(email);
        dot.title = email;
        li.appendChild(dot);
      }
    }

    const main = document.createElement('div');
    main.className = 'conv-main';

    const top = document.createElement('div');
    top.className = 'conv-top';
    const name = document.createElement('div');
    name.className = 'conv-name';
    name.textContent = c.label;
    const time = document.createElement('div');
    time.className = 'conv-time';
    time.textContent = listTime(c.last_ts);
    top.append(name, time);
    if (c.unread) {
      const b = document.createElement('div');
      b.className = 'badge';
      b.textContent = c.unread > 99 ? '99+' : String(c.unread);
      top.appendChild(b);
    }

    const snip = document.createElement('div');
    snip.className = 'conv-snip';
    snip.textContent = c.snippet || '(本文なし)';

    main.append(top, snip);
    li.appendChild(main);
    li.addEventListener('click', () => openConversation(c.key));
    li.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') openConversation(c.key);
    });
    ul.appendChild(li);
  }
}

/* ---------------- トーク ---------------- */

async function openConversation(key) {
  state.activeKey = key;
  state.exhausted = false;
  const conv = state.conversations.find((c) => c.key === key);
  state.activeLabel = conv ? conv.label : key;
  $('talkName').textContent = state.activeLabel;
  $('talkSub').textContent = conv
    ? (conv.addrs.join(', ') + `　・　${conv.total} 通`)
    : '';
  $('app').classList.add('show-talk');
  renderConversations();

  const acct = state.account ? `&account=${encodeURIComponent(state.account)}` : '';
  const data = await api(
    `/api/messages?key=${encodeURIComponent(key)}&limit=${PAGE}${acct}`);
  state.messages = data.messages;
  state.oldestTs = state.messages.length ? state.messages[0].ts : null;
  if (state.messages.length < PAGE) state.exhausted = true;
  renderStream(true);
}

async function loadOlder() {
  if (state.exhausted || state.oldestTs == null) return;
  const stream = $('stream');
  const prevHeight = stream.scrollHeight;
  const prevTop = stream.scrollTop;
  const acct = state.account ? `&account=${encodeURIComponent(state.account)}` : '';
  const data = await api(
    `/api/messages?key=${encodeURIComponent(state.activeKey)}&limit=${PAGE}` +
    `&before=${state.oldestTs}${acct}`);
  if (!data.messages.length) {
    state.exhausted = true;
  } else {
    state.messages = data.messages.concat(state.messages);
    state.oldestTs = state.messages[0].ts;
    if (data.messages.length < PAGE) state.exhausted = true;
  }
  renderStream(false);
  stream.scrollTop = prevTop + (stream.scrollHeight - prevHeight);
}

function dividerEl(text, cls) {
  const d = document.createElement('div');
  d.className = 'divider' + (cls ? ' ' + cls : '');
  const s = document.createElement('span');
  s.textContent = text;
  d.appendChild(s);
  return d;
}

function bubbleEl(m, showSender) {
  const row = document.createElement('div');
  row.className = 'row ' + (m.is_me ? 'me' : 'them');

  if (!m.is_me) {
    const who = avatarEl(m.from_name || m.from_addr, m.from_addr, 'who' + (showSender ? '' : ' ghost'));
    row.appendChild(who);
  }

  const wrap = document.createElement('div');
  wrap.className = 'bubble-wrap';

  if (!m.is_me && showSender) {
    const s = document.createElement('div');
    s.className = 'sender';
    s.textContent = m.from_name || m.from_addr;
    wrap.appendChild(s);
  }

  const line = document.createElement('div');
  line.className = 'bubble-line';

  const bubble = document.createElement('div');
  bubble.className = 'bubble';
  const text = (m.body || '').trim();
  if (text) {
    bubble.textContent = text;
  } else {
    bubble.classList.add('empty-body');
    bubble.textContent = m.subject ? `(本文なし) ${m.subject}` : '(本文なし)';
  }

  if (m.attachments && m.attachments.length) {
    const box = document.createElement('div');
    box.className = 'attach';
    for (const name of m.attachments) {
      const i = document.createElement('i');
      i.textContent = '📎 ' + name;
      box.appendChild(i);
    }
    bubble.appendChild(box);
  }

  if (m.has_more) {
    const btn = document.createElement('button');
    btn.className = 'more';
    btn.textContent = '引用を含む全文を表示';
    btn.addEventListener('click', async () => {
      if (btn.dataset.open === '1') {
        bubble.querySelector('.quoted').remove();
        btn.dataset.open = '0';
        btn.textContent = '引用を含む全文を表示';
        return;
      }
      btn.disabled = true;
      try {
        const full = await api(
          `/api/full?uid=${m.uid}&account=${encodeURIComponent(m.account)}`);
        const q = document.createElement('div');
        q.className = 'quoted';
        q.textContent = full.body_full || '(全文が取得できませんでした)';
        bubble.appendChild(q);
        btn.dataset.open = '1';
        btn.textContent = '全文を隠す';
      } catch (e) {
        toast('全文の取得に失敗しました: ' + e.message);
      } finally {
        btn.disabled = false;
      }
    });
    bubble.appendChild(btn);
  }

  const stamp = document.createElement('div');
  stamp.className = 'stamp';
  stamp.textContent = clockLabel(m.ts);
  if (m.unread && !m.is_me) {
    const dot = document.createElement('span');
    dot.className = 'dot';
    dot.textContent = ' ●';
    stamp.appendChild(dot);
  }

  line.append(bubble, stamp);
  wrap.appendChild(line);
  row.appendChild(wrap);
  return row;
}

function renderStream(jump) {
  const stream = $('stream');
  stream.textContent = '';

  if (!state.messages.length) {
    const e = document.createElement('div');
    e.className = 'empty';
    e.textContent = 'このトークにはまだメッセージがありません。';
    stream.appendChild(e);
    return;
  }

  if (!state.exhausted) {
    const box = document.createElement('div');
    box.className = 'loadmore';
    const btn = document.createElement('button');
    btn.textContent = 'これより前を読み込む';
    btn.addEventListener('click', () => loadOlder().catch((e) => toast(e.message)));
    box.appendChild(btn);
    stream.appendChild(box);
  }

  let lastDay = null;
  let lastSubject = null;
  let lastSender = null;
  let unreadMarked = false;
  let firstUnreadEl = null;

  for (const m of state.messages) {
    const dk = dayKey(m.ts);
    if (dk !== lastDay) {
      stream.appendChild(dividerEl(dayLabel(m.ts)));
      lastDay = dk;
      lastSender = null;
    }

    if (!unreadMarked && m.unread && !m.is_me) {
      const d = dividerEl('ここから未読', 'unread');
      stream.appendChild(d);
      firstUnreadEl = d;
      unreadMarked = true;
    }

    const subj = baseSubject(m.subject);
    if (subj && subj !== lastSubject) {
      const s = document.createElement('div');
      s.className = 'subject';
      const span = document.createElement('span');
      span.textContent = subj;
      s.appendChild(span);
      stream.appendChild(s);
      lastSubject = subj;
      lastSender = null;
    }

    const senderKey = m.is_me ? '@me' : m.from_addr;
    stream.appendChild(bubbleEl(m, senderKey !== lastSender));
    lastSender = senderKey;
  }

  if (jump) {
    if (firstUnreadEl) {
      firstUnreadEl.scrollIntoView({ block: 'center' });
    } else {
      stream.scrollTop = stream.scrollHeight;
    }
  }
}

/* ---------------- 同期 ---------------- */

async function refreshStatus() {
  const s = await api('/api/status');
  const known = state.accounts.map((a) => a.email).join(',');
  state.accounts = s.accounts || [];
  if (state.accounts.map((a) => a.email).join(',') !== known) renderAccounts();
  if (state.account && !state.accounts.some((a) => a.email === state.account)) {
    state.account = '';   // 設定から消えたアカウントを選んだままにしない
  }
  $('me').textContent = state.accounts.length > 1
    ? state.accounts.map((a) => a.email).join('  /  ')
    : (state.accounts[0] || {}).email || '';
  const st = s.stats;
  const parts = [`${st.conversations} 会話 / ${st.messages} 通`];
  if (st.duplicates) parts.push(`重複 ${st.duplicates} 通は1通表示`);
  if (st.last_sync) parts.push('最終同期 ' + listTime(st.last_sync) + ' ' + clockLabel(st.last_sync));
  $('stat').textContent = parts.join('　・　');
  return s;
}

async function runSync(silent) {
  if (state.syncing) return;
  state.syncing = true;
  $('sync').disabled = true;
  $('sync').textContent = '同期中';
  try {
    await api('/api/sync', { method: 'POST' });
    let last = '';
    for (;;) {
      await new Promise((r) => setTimeout(r, 1200));
      const s = await refreshStatus();
      const lines = s.sync.lines || [];
      if (lines.length && lines[lines.length - 1] !== last) {
        last = lines[lines.length - 1];
        if (!silent) toast(last, 0);
      }
      if (!s.sync.running) {
        if (s.sync.error) {
          toast('同期に失敗しました:\n' + s.sync.error, 15000);
        } else if (!silent) {
          toast(last || '同期しました');
        } else {
          $('toast').hidden = true;
        }
        break;
      }
    }
    const key = state.activeKey;
    await loadConversations(true);
    if (key) await openConversation(key);
  } catch (e) {
    toast('同期に失敗しました: ' + e.message, 12000);
  } finally {
    state.syncing = false;
    $('sync').disabled = false;
    $('sync').textContent = '同期';
  }
}

/* ---------------- 起動 ---------------- */

let searchTimer = null;
$('search').addEventListener('input', (e) => {
  clearTimeout(searchTimer);
  const v = e.target.value;
  searchTimer = setTimeout(() => {
    state.query = v;
    loadConversations(true).catch((err) => toast(err.message));
  }, 220);
});

for (const id of ['tabPeople', 'tabNotice']) {
  $(id).addEventListener('click', (e) => switchKind(e.currentTarget.dataset.kind));
}
$('sync').addEventListener('click', () => runSync(false));
$('back').addEventListener('click', () => $('app').classList.remove('show-talk'));

document.addEventListener('keydown', (e) => {
  const typing = /^(INPUT|TEXTAREA)$/.test(document.activeElement.tagName);
  if (e.key === '/' && !typing) { e.preventDefault(); $('search').focus(); return; }
  if (e.key === 'Escape') {
    if (typing) document.activeElement.blur();
    else $('app').classList.remove('show-talk');
    return;
  }
  if (typing || !state.conversations.length) return;
  if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
    e.preventDefault();
    const i = state.conversations.findIndex((c) => c.key === state.activeKey);
    const next = Math.min(state.conversations.length - 1,
                          Math.max(0, (i < 0 ? 0 : i) + (e.key === 'ArrowDown' ? 1 : -1)));
    openConversation(state.conversations[next].key).catch((err) => toast(err.message));
  }
});

(async function boot() {
  try {
    const s = await refreshStatus();
    await loadConversations(false);
    if (s.stats.messages === 0 && !s.sync.running) {
      toast('まだメールがありません。「同期」を押すと取り込みを始めます。', 8000);
    }
    setInterval(() => { if (!state.syncing) runSync(true); }, AUTO_SYNC_MS);
  } catch (e) {
    toast('起動に失敗しました: ' + e.message, 0);
  }
})();
