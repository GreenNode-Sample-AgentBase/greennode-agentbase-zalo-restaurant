/* =========================================================================
 * app.js — Web Simulator cho sample repo
 *         "Zalo Restaurant Bot — bot quán ăn nhớ khách quen"
 *
 * Mô phỏng giao diện chat Zalo OA để dev kiểm thử agent mà không cần Zalo thật.
 * - Thuần vanilla JS, không framework, không build step.
 * - Backend Python SDK serve các file này tĩnh tại GET / (same origin),
 *   nên mọi fetch() đều dùng URL tương đối → không gặp vấn đề CORS.
 *
 * Hợp đồng API (same origin):
 *   POST /invocations        gửi tin nhắn cho agent
 *   GET  /api/info           thông tin agent (model, memory, zalo_configured…)
 *   GET  /api/memory?actor=  hồ sơ khách theo memory strategy
 *   GET  /api/history?actor=&session=  lịch sử hội thoại (tăng dần theo thời gian)
 *   GET  /api/actors         danh sách khách hàng + các session
 *   GET  /api/bookings       danh sách đặt bàn hiện có
 * ========================================================================= */
'use strict';

/* ----- Hằng số endpoint (khớp chính xác với backend) ----- */
const API = {
  INVOCATIONS: '/invocations',
  INFO:        '/api/info',
  MEMORY:      '/api/memory',
  HISTORY:     '/api/history',
  ACTORS:      '/api/actors',
  BOOKINGS:    '/api/bookings',
};

/* Lớp CSS tương ứng với trạng thái đặt bàn (badge màu) */
const STATUS_CLASS = { CONFIRMED: 'st-confirmed', CANCELLED: 'st-cancelled' };

/* ----- Trạng thái toàn cục của simulator ----- */
const state = {
  currentActor: null,   // actorId đang chọn (chính là Zalo user id)
  currentSession: null, // session id đang dùng cho actor đó
  actors: [],           // cache kết quả /api/actors
  sending: false,       // chặn gửi đôi khi bot đang trả lời
  timerId: null,        // interval đếm thời gian chờ bot trả lời
  pendingStart: 0,      // mốc thời gian bắt đầu chờ
};

/* ----- Tiện ích DOM ngắn gọn ----- */
const $ = (id) => document.getElementById(id);

/* escapeHtml: luôn escape nội dung đến từ API/người dùng trước khi nhét vào innerHTML */
function escapeHtml(value) {
  return String(value ?? '')
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

/* renderMarkdown: render tối giản **in đậm**, `code`, [link](url) trên chuỗi ĐÃ escape.
   Chỉ chấp nhận link http/https để tránh injected javascript: URI. */
function renderMarkdown(raw) {
  let html = escapeHtml(raw);
  html = html.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
    '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
  html = html.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
  html = html.replace(/`([^`\n]+)`/g, '<code>$1</code>');
  html = html.replace(/^\s*[-*]\s+(.*)$/gm, '&bull; $1'); // gạch đầu dòng → bullet
  return html.replace(/\n/g, '<br>');
}

/* relativeTime: "vừa xong", "5 phút trước"… từ chuỗi ISO createdAt */
function relativeTime(iso) {
  if (!iso) return '';
  const time = new Date(iso).getTime();
  if (Number.isNaN(time)) return String(iso);
  const diff = Date.now() - time;
  if (diff < 60_000) return 'vừa xong';
  const minutes = Math.floor(diff / 60_000);
  if (minutes < 60) return `${minutes} phút trước`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} giờ trước`;
  const days = Math.floor(hours / 24);
  if (days < 7) return `${days} ngày trước`;
  return new Date(iso).toLocaleDateString('vi-VN');
}

/* ===== Toast lỗi — dải đỏ trên cùng, tự ẩn sau 8s hoặc bấm ✕ để tắt ===== */
let toastTimer = null;

function showToast(message) {
  $('toast-message').textContent = message;
  $('toast-strip').classList.remove('hidden');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(hideToast, 8000);
}

function hideToast() {
  $('toast-strip').classList.add('hidden');
  clearTimeout(toastTimer);
}

/* fetchJson: wrapper cho fetch — bắn lỗi tiếng Việt dễ hiểu để đưa lên toast */
async function fetchJson(url, options = {}) {
  let res;
  try {
    res = await fetch(url, options);
  } catch (err) {
    throw new Error(`Không gọi được ${url} — backend đã chạy chưa?`);
  }
  let data = null;
  try { data = await res.json(); } catch { /* body rỗng hoặc không phải JSON */ }
  if (!res.ok) throw new Error((data && data.error) || `Lỗi HTTP ${res.status} từ ${url}`);
  return data;
}

/* ===== Khung chat — render bubble ===== */
function scrollToBottom() {
  const box = $('chat-messages');
  box.scrollTop = box.scrollHeight;
}

/* appendMessage: thêm 1 bubble vào vùng chat.
   role: 'user' | 'assistant'. memories: mảng facts bot vừa dùng (nếu có). */
function appendMessage(role, text, memories) {
  const wrap = document.createElement('div');
  wrap.className = role === 'user' ? 'msg msg-user' : 'msg msg-bot';

  const bubble = document.createElement('div');
  bubble.className = 'bubble';
  bubble.innerHTML = renderMarkdown(text);
  wrap.appendChild(bubble);

  // Callout "✨ Bot nhớ: ..." ngay dưới bubble của bot khi có memories_used
  if (role !== 'user' && Array.isArray(memories) && memories.length > 0) {
    const note = document.createElement('div');
    note.className = 'memory-callout';
    note.innerHTML = `✨ Bot nhớ: ${memories.map((m) => `<em>${escapeHtml(m)}</em>`).join(', ')}`;
    wrap.appendChild(note);
  }

  $('chat-messages').appendChild(wrap);
  scrollToBottom();
}

/* Chỉ báo "bot đang gõ..." với 3 chấm nhấp nháy + đồng hồ đếm giây trôi */
function showTyping() {
  const el = document.createElement('div');
  el.className = 'msg msg-bot typing-msg';
  el.id = 'typing-bubble'; // ID động — phần tử này tự tạo rồi tự tra cứu lại
  el.innerHTML = `
    <div class="bubble typing">
      <span class="typing-dots"><i></i><i></i><i></i></span>
      <span class="typing-timer">0.0s</span>
    </div>`;
  $('chat-messages').appendChild(el);
  scrollToBottom();

  state.pendingStart = Date.now();
  state.timerId = setInterval(() => {
    const seconds = (Date.now() - state.pendingStart) / 1000;
    const label = document.querySelector('#typing-bubble .typing-timer');
    if (label) label.textContent = `${seconds.toFixed(1)}s`;
  }, 100);
}

function hideTyping() {
  clearInterval(state.timerId);
  state.timerId = null;
  const el = $('typing-bubble');
  if (el) el.remove();
}

/* ===== Tải dữ liệu từ backend ===== */

/* loadAgentInfo: GET /api/info → dot trạng thái + dòng thông tin ở sidebar */
async function loadAgentInfo() {
  try {
    const info = await fetchJson(API.INFO);
    const configured = info && info.zalo_configured === true;
    $('agent-status').textContent = configured
      ? 'Đang hoạt động'
      : 'Chế độ giả lập · OA chưa cấu hình';
    $('status-dot').className = configured ? 'status-dot' : 'status-dot warn';
    $('info-line').innerHTML =
      `🤖 <strong>${escapeHtml(info.agent || 'agent')}</strong>` +
      ` · model <code>${escapeHtml(info.llm_model || '—')}</code>` +
      `<br>memory: <code>${escapeHtml(info.memory_id || '—')}</code>`;
  } catch (err) {
    showToast(err.message);
  }
}

/* loadActors: GET /api/actors → danh sách khách; tự chọn khách đầu tiên lần đầu mở */
async function loadActors() {
  try {
    const data = await fetchJson(API.ACTORS);
    state.actors = (data && data.actors) || [];
    renderActorList();
    // Lần đầu mở trang (chưa chọn ai) → chọn khách đầu tiên để có sẵn hội thoại
    if (!state.currentActor && state.actors.length > 0) {
      const first = state.actors[0];
      selectActor(first.actorId, (first.sessions || [])[0] || null);
    }
  } catch (err) {
    showToast(err.message);
  }
}

/* renderActorList: vẽ danh sách khách hàng ở cột trái */
function renderActorList() {
  const list = $('actor-list');
  list.innerHTML = '';

  if (!state.actors.length) {
    list.innerHTML = '<p class="empty-note">Chưa có khách nào.<br>Thêm khách giả lập bên dưới 👇</p>';
    return;
  }

  for (const actor of state.actors) {
    const sessions = actor.sessions || [];
    const isActive = actor.actorId === state.currentActor;

    const item = document.createElement('button');
    item.type = 'button';
    item.className = isActive ? 'actor-item active' : 'actor-item';
    item.title = `actorId: ${actor.actorId}`;
    item.innerHTML = `
      <span class="avatar">${escapeHtml(avatarLabel(actor.actorId))}</span>
      <span class="actor-meta">
        <span class="actor-id">${escapeHtml(actor.actorId)}</span>
        <span class="actor-sub">${sessions.length} session</span>
      </span>`;
    // Chọn khách: dùng session có sẵn nếu có, không thì tạo session mới
    item.addEventListener('click', () => selectActor(actor.actorId, sessions[0] || null));
    list.appendChild(item);
  }
}

/* avatarLabel: với id kiểu số điện thoại, lấy 2 chữ số cuối làm "chữ cái đầu" trên avatar */
function avatarLabel(actorId) {
  const str = String(actorId || '');
  return str.slice(-2) || '?';
}

/* newSessionId: session mới cho khách giả lập (backend tự ghi nhận ở tin nhắn đầu) */
function newSessionId() {
  return `web-${Date.now()}`;
}

/* selectActor: đổi khách hàng đang chat → tải lại lịch sử + hồ sơ memory */
function selectActor(actorId, sessionId) {
  state.currentActor = actorId;
  state.currentSession = sessionId || newSessionId();
  renderActorList();
  $('chat-messages').innerHTML = '';
  loadHistory();
  loadMemory();
}

/* loadHistory: GET /api/history?actor=&session= → vẽ lại toàn bộ bubble (tăng dần) */
async function loadHistory() {
  const box = $('chat-messages');
  box.innerHTML = '<p class="history-loading">Đang tải lịch sử hội thoại…</p>';
  try {
    const url = `${API.HISTORY}?actor=${encodeURIComponent(state.currentActor)}` +
      `&session=${encodeURIComponent(state.currentSession)}`;
    const data = await fetchJson(url);
    const events = (data && data.events) || [];

    box.innerHTML = '';
    if (!events.length) {
      // Chưa có tin nhắn nào → hiện khối chào mừng thân thiện
      box.innerHTML = `
        <div class="welcome">
          <p>👋 Xin chào <strong>${escapeHtml(state.currentActor)}</strong>!</p>
          <p>Đây là Zalo OA giả lập của <strong>Quán Ngon 123</strong>.<br>
             Hãy nhắn tin để đặt bàn, hoặc kể sở thích để bot ghi nhớ bạn.</p>
        </div>`;
      return;
    }
    for (const ev of events) {
      appendMessage(ev.role === 'user' ? 'user' : 'assistant', ev.message || '');
    }
  } catch (err) {
    box.innerHTML = '';
    showToast(err.message);
  }
}

/* loadMemory: GET /api/memory?actor= → nhóm theo memory strategy, card cho từng record */
async function loadMemory() {
  if (!state.currentActor) return;
  try {
    const url = `${API.MEMORY}?actor=${encodeURIComponent(state.currentActor)}`;
    const data = await fetchJson(url);
    renderMemory((data && data.groups) || []);
  } catch (err) {
    showToast(err.message);
  }
}

function renderMemory(groups) {
  const panel = $('memory-panel');
  panel.innerHTML = '';

  if (!groups.length) {
    panel.innerHTML = '<p class="empty-note">Chưa có hồ sơ nào được ghi nhớ.<br>' +
      'Hãy trò chuyện — bot sẽ tự học sở thích của khách.</p>';
    return;
  }

  for (const group of groups) {
    const records = group.records || [];
    const block = document.createElement('div');
    block.className = 'memory-group';
    block.innerHTML = `
      <div class="memory-group-head">
        <span class="strategy-name">${escapeHtml(group.strategy || 'unknown')}</span>
        <span class="strategy-id">${escapeHtml(group.strategy_id || '')}</span>
      </div>`;

    if (!records.length) {
      block.insertAdjacentHTML('beforeend', '<p class="empty-note">Chưa có bản ghi nào trong nhóm này.</p>');
    }
    for (const record of records) {
      const card = document.createElement('div');
      card.className = 'memory-card';
      card.innerHTML = `
        <p class="memory-text">${escapeHtml(record.memory || '')}</p>
        <span class="memory-time">${escapeHtml(relativeTime(record.createdAt))}</span>`;
      block.appendChild(card);
    }
    panel.appendChild(block);
  }
}

/* loadBookings: GET /api/bookings → bảng đặt bàn ở cột phải */
async function loadBookings() {
  try {
    const data = await fetchJson(API.BOOKINGS);
    renderBookings((data && data.bookings) || []);
  } catch (err) {
    showToast(err.message);
  }
}

function renderBookings(bookings) {
  const tbody = $('bookings-table-body');
  tbody.innerHTML = '';

  if (!bookings.length) {
    tbody.innerHTML = '<tr><td colspan="6" class="empty-note">Chưa có lượt đặt bàn nào.</td></tr>';
    return;
  }

  for (const booking of bookings) {
    const statusClass = STATUS_CLASS[booking.status] || 'st-other';
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td>${escapeHtml(booking.date || '—')}</td>
      <td>${escapeHtml(booking.time || '—')}</td>
      <td title="${escapeHtml(booking.notes || '')}">${escapeHtml(booking.customer || '—')}</td>
      <td>${escapeHtml(booking.party_size ?? '—')}</td>
      <td>${escapeHtml(booking.table || '—')}</td>
      <td><span class="status-badge ${statusClass}">${escapeHtml(booking.status || '?')}</span></td>`;
    tbody.appendChild(tr);
  }
}

/* ===== Gửi tin nhắn: POST /invocations với header User-Id / Session-Id ===== */
async function submitMessage() {
  const input = $('composer-input');
  const text = input.value.trim();

  if (!text) return;
  if (!state.currentActor) {
    showToast('Hãy chọn hoặc thêm một khách hàng trước khi nhắn tin.');
    return;
  }
  if (state.sending) return; // đang chờ phản hồi trước đó

  state.sending = true;
  $('send-btn').disabled = true;

  appendMessage('user', text);
  input.value = '';
  autoResizeTextarea();
  showTyping();

  try {
    const data = await fetchJson(API.INVOCATIONS, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        // Đúng hợp đồng backend: định danh khách (Zalo user id) và phiên chat
        'X-GreenNode-AgentBase-User-Id': state.currentActor,
        'X-GreenNode-AgentBase-Session-Id': state.currentSession,
      },
      body: JSON.stringify({ message: text }),
    });

    if (!data || data.status !== 'success') {
      throw new Error((data && data.error) || 'Agent trả về trạng thái không hợp lệ.');
    }

    hideTyping();
    appendMessage('assistant', data.response || '(Bot trả về nội dung rỗng)', data.memories_used);

    // Tự làm mới sau mỗi lượt bot trả lời: memory có thể vừa được trích xuất,
    // đặt bàn có thể vừa được tạo, session mới có thể xuất hiện
    loadMemory();
    loadBookings();
    loadActors();
  } catch (err) {
    hideTyping();
    appendMessage('assistant', `⚠️ **Lỗi:** ${err.message}`);
    showToast(err.message);
  } finally {
    state.sending = false;
    $('send-btn').disabled = false;
    $('composer-input').focus();
  }
}

/* ===== Thêm khách giả lập mới ===== */

/* normalizeActorId: chuẩn hoá số điện thoại thành actorId.
   - Bỏ mọi ký tự không phải chữ số (kể cả dấu +).
   - Số nội địa bắt đầu bằng 0 (VD 0901234567) → tự thay 0 bằng 84 → 84901234567.
   - Đã ở dạng quốc tế (84901…) hoặc dạng khác → giữ nguyên bản. */
function normalizeActorId(raw) {
  const digits = String(raw || '').replace(/\D/g, '');
  if (!digits) return null;
  if (digits.startsWith('0')) return '84' + digits.slice(1);
  return digits;
}

function submitNewCustomer(event) {
  event.preventDefault();
  const input = $('new-customer-input');
  const actorId = normalizeActorId(input.value);

  if (!actorId || actorId.length < 9) {
    showToast('ActorId phải là số điện thoại hợp lệ (ít nhất 9 chữ số).');
    return;
  }

  // Khách đã có trong danh sách → chỉ chọn lại, không thêm trùng
  const existing = state.actors.find((a) => a.actorId === actorId);
  if (existing) {
    selectActor(actorId, (existing.sessions || [])[0] || null);
    input.value = '';
    return;
  }

  // Thêm tạm vào danh sách local (backend sẽ tự biết actor này sau tin nhắn đầu tiên)
  state.actors.unshift({ actorId, sessions: [] });
  input.value = '';
  selectActor(actorId, null); // khách mới → session hoàn toàn mới
}

/* ===== Gắn sự kiện & khởi động ===== */
function autoResizeTextarea() {
  const ta = $('composer-input');
  ta.style.height = 'auto';
  ta.style.height = Math.min(ta.scrollHeight, 120) + 'px';
}

function bindEvents() {
  // Gửi tin nhắn: submit form hoặc phím Enter (Shift+Enter vẫn xuống dòng)
  $('composer').addEventListener('submit', (e) => { e.preventDefault(); submitMessage(); });
  $('composer-input').addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      submitMessage();
    }
  });
  $('composer-input').addEventListener('input', autoResizeTextarea);

  // Chip gợi ý: chỉ điền nội dung vào ô soạn tin, người dùng tự bấm Gửi
  document.querySelectorAll('#suggestion-chips .chip').forEach((chip) => {
    chip.addEventListener('click', () => {
      const input = $('composer-input');
      input.value = chip.textContent.trim();
      autoResizeTextarea();
      input.focus();
    });
  });

  // Form thêm khách giả lập mới + nút làm mới bảng đặt bàn + nút tắt toast
  $('new-customer-form').addEventListener('submit', submitNewCustomer);
  $('bookings-refresh').addEventListener('click', loadBookings);
  $('toast-close').addEventListener('click', hideToast);
}

/* init: nạp song song thông tin agent, danh sách khách, bảng đặt bàn */
async function init() {
  bindEvents();
  await Promise.allSettled([loadAgentInfo(), loadActors(), loadBookings()]);
}

document.addEventListener('DOMContentLoaded', init);
