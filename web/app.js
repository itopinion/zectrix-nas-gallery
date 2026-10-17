'use strict';

const $ = id => document.getElementById(id);
const form = $('settingsForm');
const fieldIds = ['photoRoot', 'cacheDir', 'autoplay', 'intervalMinutes', 'cacheLimit', 'prefetchFrames'];
let uiToken = '', state = null, formLoaded = false, dirty = false, refreshing = false;
let previewKey = '', previewRevision = 0, noticeTimer = null, saving = false, actionError = '';
const pending = new Set();

function setText(id, value) { $(id).textContent = value; }
function showNotice(id, message) { $(id).hidden = !message; $(id).textContent = message || ''; }
function toast(message) {
  setText('toast', message); $('toast').hidden = false;
  clearTimeout(noticeTimer); noticeTimer = setTimeout(() => { $('toast').hidden = true; }, 4200);
}
async function responseData(response) {
  const text = await response.text();
  let value; try { value = JSON.parse(text); } catch { value = null; }
  if (!response.ok) {
    if (response.status === 403) uiToken = '';
    throw new Error(value?.error || value?.message || '操作未完成，请稍后重试。');
  }
  return value;
}
async function boot() {
  const value = await responseData(await fetch('/api/boot', {cache: 'no-store', signal: AbortSignal.timeout(8000)}));
  uiToken = value?.token || '';
  if (value?.version) setText('appVersion', `ZECTRIX NAS Gallery · ${value.version}`);
}
async function api(path, body) {
  if (!uiToken) await boot();
  if (!uiToken) throw new Error('相册尚未就绪，请刷新页面后重试。');
  return responseData(await fetch(path, {
    method: 'POST', credentials: 'same-origin',
    headers: {'Content-Type': 'application/json', 'X-App-Token': uiToken},
    body: JSON.stringify(body), signal: AbortSignal.timeout(30000)
  }));
}
function errorText(value) {
  if (!value) return '';
  if (typeof value === 'string') return value;
  return value.error || value.message || value.reason || '设备暂时不可用，请在本地助手中检查连接。';
}
function count(value) { return Number.isFinite(Number(value)) ? new Intl.NumberFormat('zh-CN').format(Number(value)) : '—'; }
function megabytes(value) { return Number.isFinite(Number(value)) ? `${(Number(value) / 1048576).toFixed(1)} MB` : '0 MB'; }
function timeRemaining(seconds) {
  if (seconds === null || seconds === undefined) return '—';
  const value = Math.max(0, Math.round(Number(seconds) || 0));
  if (value < 60) return `${value} 秒`;
  const minutes = Math.floor(value / 60), rest = value % 60;
  return rest ? `${minutes} 分 ${rest} 秒` : `${minutes} 分钟`;
}
function fileName(path) { return typeof path === 'string' ? path.split(/[\\/]/).pop() : ''; }
function isPaused() { return typeof state?.player?.paused === 'boolean' ? state.player.paused : state?.config?.autoplay === false; }
function loadForm(config) {
  $('photoRoot').value = config.photo_root || '';
  $('cacheDir').value = config.cache_dir || '';
  $('autoplay').checked = config.autoplay !== false;
  $('intervalMinutes').value = Math.max(1, Math.round((config.interval_seconds || 300) / 60));
  $('cacheLimit').value = config.cache_max_mb || 200;
  $('prefetchFrames').value = config.prefetch_frames || 6;
  formLoaded = true; dirty = false;
  fieldIds.forEach(id => $(id).classList.remove('dirty'));
  setText('saveState', '');
}
function renderControls() {
  const runtime = state?.runtime || {}, player = state?.player || {};
  const playable = runtime.active === true && runtime.connected === true && runtime.compatible !== false && player.running !== false;
  $('previousButton').disabled = !playable || pending.has('control');
  $('nextButton').disabled = !playable || pending.has('control');
  $('pauseButton').disabled = !playable || pending.has('control');
  $('pauseButton').textContent = isPaused() ? '继续轮播' : '暂停轮播';
  $('rescanButton').disabled = pending.has('rescan') || saving || !state?.config?.photo_root;
  $('saveButton').disabled = saving || !formLoaded;
  $('saveButton').textContent = saving ? '正在保存…' : '保存设置';
  $('intervalMinutes').disabled = !$('autoplay').checked || saving;
}
function render() {
  const config = state.config || {}, player = state.player || {}, runtime = state.runtime || {};
  if (!formLoaded || !dirty) loadForm(config);
  const badge = $('connectionBadge'); badge.className = 'status-badge';
  let notice = '';
  if (!config.photo_root) {
    badge.classList.add('inactive'); badge.textContent = '等待设置';
    notice = '先连接 NAS，再选择照片文件夹。保存后会在后台建立照片索引。';
  } else if (player.running === false) {
    badge.classList.add('error'); badge.textContent = '已停止';
    notice = '相册已停止，请在 ZECTRIX 本地助手中重新打开「NAS 相册」。';
  } else if (runtime.compatible === false) {
    badge.classList.add('error'); badge.textContent = '需要更新助手';
    notice = '当前本地助手需要更新，才能显示十六灰阶照片。';
  } else if (runtime.active === false) {
    badge.classList.add('inactive'); badge.textContent = '等待启用';
    notice = '在 ZECTRIX 本地助手中启用「NAS 相册」，照片会接着播放。';
  } else if (!runtime.connected) {
    badge.classList.add('inactive'); badge.textContent = '设备未连接';
    notice = '请在 ZECTRIX 本地助手中连接设备，并让设备进入本地推送模式。';
  } else {
    badge.classList.add('connected'); badge.textContent = isPaused() ? '已暂停轮播' : '正在播放';
  }
  if (player.running !== false && player.scan?.state === 'nas_unavailable') notice = '暂时无法访问 NAS，已缓存的小图仍可继续浏览。';
  showNotice('notice', notice);
  const issue = player.running === false && config.photo_root ? '相册已停止，请在 ZECTRIX 本地助手中重新打开「NAS 相册」。' : actionError || errorText(runtime.error) || errorText(player.last_error);
  showNotice('errorNotice', issue);
  const source = player.current_source || player.current?.source || '';
  const frame = player.current_frame || player.current?.file || '';
  const revision = player.frame_revision ?? player.display_receipts ?? '';
  const key = `${frame}|${revision}`;
  if (frame && key !== previewKey) {
    previewKey = key;
    $('photoPreview').src = `/screen.png?v=${++previewRevision}`;
  }
  if (!frame) {
    $('photoPreview').hidden = true; $('previewEmpty').hidden = false;
    setText('emptyMessage', config.photo_root ? '正在寻找有可靠拍摄日期的人像照片。' : '选择照片文件夹，开始你的相册。');
  }
  setText('currentName', fileName(source) || '等待第一张照片');
  const date = player.date_stamp?.capture_date || player.current_capture_date || player.capture_date || player.current?.capture_date;
  setText('captureDate', date || '拍摄日期 · 右下角');
  setText('catalogCount', count(player.stats?.catalog ?? player.catalog_count));
  const depth = player.prefetch?.ready ?? 0, target = player.prefetch?.target || config.prefetch_frames || 6;
  setText('preparedCount', `${count(depth)} / ${count(target)}`);
  setText('nextChange', player.running === false ? '已停止' : isPaused() ? '已暂停' : timeRemaining(player.next_change_in_seconds));
  const files = player.cache?.files || 0;
  setText('cacheSummary', `已缓存 ${count(files)} 张小图`);
  setText('cacheUsage', `${megabytes(player.cache?.bytes || 0)} / ${config.cache_max_mb || 200} MB · 每张 400×300`);
  const scanning = player.scan?.state === 'scanning';
  $('rescanButton').textContent = scanning ? '正在扫描…' : '重新扫描照片';
  setText('playbackHint', player.busy ? '正在刷新墨水屏，按键会保留最近一次操作。' : '设备上键回看，下键随机换图，确认键暂停或继续。');
  renderControls();
}
async function refresh() {
  if (refreshing) return;
  refreshing = true;
  try {
    if (!uiToken) await boot();
    const value = await responseData(await fetch('/api/status', {cache: 'no-store', signal: AbortSignal.timeout(8000)}));
    if (value) { state = value; render(); }
  } catch (error) {
    showNotice('errorNotice', error.message || '暂时无法连接相册，请在本地助手中重新打开应用。');
  } finally { refreshing = false; }
}
async function control(command) {
  const group = command === 'rescan' ? 'rescan' : 'control';
  if (pending.has(group)) return;
  actionError = ''; pending.add(group); renderControls();
  try {
    await api('/api/control', {command});
    if (command === 'rescan') toast('已开始重新扫描照片。');
    await refresh();
  } catch (error) { actionError = error.message; showNotice('errorNotice', actionError); }
  finally { pending.delete(group); renderControls(); }
}
fieldIds.forEach(id => {
  $(id).addEventListener('input', () => {
    dirty = true; $(id).classList.add('dirty'); setText('saveState', '尚未保存'); renderControls();
  });
});
form.addEventListener('submit', async event => {
  event.preventDefault();
  if (saving || !form.reportValidity()) return;
  const config = {
    photo_root: $('photoRoot').value.trim(), cache_dir: $('cacheDir').value.trim(),
    interval_seconds: Number($('intervalMinutes').value) * 60,
    cache_max_mb: Number($('cacheLimit').value), prefetch_frames: Number($('prefetchFrames').value),
    autoplay: $('autoplay').checked
  };
  actionError = ''; saving = true; renderControls();
  try {
    await api('/api/config', config);
    dirty = false; formLoaded = false;
    toast('设置已保存。'); await refresh();
  } catch (error) { actionError = error.message; showNotice('errorNotice', actionError); }
  finally { saving = false; renderControls(); }
});
$('previousButton').addEventListener('click', () => control('previous'));
$('nextButton').addEventListener('click', () => control('next'));
$('pauseButton').addEventListener('click', () => control(isPaused() ? 'resume' : 'pause'));
$('rescanButton').addEventListener('click', () => control('rescan'));
$('photoPreview').addEventListener('load', () => { $('photoPreview').hidden = false; $('previewEmpty').hidden = true; });
$('photoPreview').addEventListener('error', () => { $('photoPreview').hidden = true; $('previewEmpty').hidden = false; });
refresh();
setInterval(refresh, 2000);
