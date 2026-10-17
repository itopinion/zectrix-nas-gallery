"""Backend-only Hub Runtime client. The app never opens a device socket."""
import json
from pathlib import Path
import queue
import threading
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from player import Player


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class HubFailure(RuntimeError):
    def __init__(self, status, payload):
        self.status, self.payload = status, payload
        super().__init__(str(payload.get('error', payload.get('error_kind', 'Hub request failed'))))


class HubClient:
    def __init__(self, url, token):
        parsed = urlsplit(url)
        if (parsed.scheme != 'http' or parsed.hostname not in {'127.0.0.1', 'localhost', '::1'}
                or parsed.username or parsed.password or parsed.path not in ('', '/')
                or parsed.query or parsed.fragment or not token):
            raise ValueError('Hub Runtime must be an authenticated loopback HTTP address')
        self.url = url.rstrip('/')
        self.token = token

    def request(self, path, data=None, headers=None, timeout=5):
        # A new opener per call keeps simultaneous event/image requests independent.
        opener = build_opener(ProxyHandler({}), NoRedirect())
        request = Request(self.url + path, data=data,
                          headers={'Authorization': 'Bearer ' + self.token, **(headers or {})})
        try:
            with opener.open(request, timeout=timeout) as response:
                return json.load(response)
        except HTTPError as error:
            try:
                payload = json.loads(error.read(65536))
            except (OSError, ValueError):
                payload = {'error': 'Hub HTTP ' + str(error.code)}
            raise HubFailure(error.code, payload) from None

    def status(self):
        return self.request('/runtime/status')

    def events(self, cursor):
        return self.request('/runtime/events?after=' + str(int(cursor)))

    def show(self, file, lease):
        png = Path(file).read_bytes()
        if len(png) > 1048576:
            raise ValueError('400×300 PNG exceeds the Hub image limit')
        return self.request('/runtime/image', png, {'Content-Type': 'image/png',
                            'X-Lease': str(lease), 'X-Image-Format': 'gray16'}, timeout=50)


class HubBridge:
    def __init__(self, events, client):
        self.events, self.client = events, client
        self.closed = threading.Event()
        self.state_lock = threading.Lock()
        self.state = {'active': False, 'connected': False, 'compatible': False, 'lease': 0}
        self.requests = queue.Queue()
        self.stdin = self
        threading.Thread(target=self.poll_hub, daemon=True).start()
        threading.Thread(target=self.send_loop, daemon=True).start()

    def write(self, line):
        command = json.loads(line)
        if command['command'] == 'close':
            self.closed.set()
            return
        with self.state_lock:
            command['lease'] = self.state.get('lease', 0)
        self.requests.put(command)

    def flush(self):
        pass

    def poll(self):
        return 0 if self.closed.is_set() else None

    def wait(self, timeout=None):
        return 0

    def terminate(self):
        self.closed.set()

    def poll_hub(self):
        cursor = 0
        primed = False
        while not self.closed.is_set():
            try:
                status = self.client.status()
                formats = status.get('features', {}).get('image', {}).get('formats', [])
                state = {'active': bool(status.get('active')), 'lease': status.get('lease', 0),
                         'connected': bool(status.get('device', {}).get('connected')),
                         'compatible': 'gray16' in formats,
                         'error': None if 'gray16' in formats else '本地助手需要支持 /runtime/image 十六灰阶接口'}
                with self.state_lock:
                    self.state = state
                self.events.put({'type': 'runtime_state', 'state': state})
                batch = self.client.events(cursor)
                cursor = batch.get('cursor', cursor)
                # Existing queued keys belong to the previous application session.
                if primed and all(state[k] for k in ('active', 'connected', 'compatible')):
                    for item in batch.get('events', []):
                        event = item.get('event', {})
                        if item.get('lease') == state['lease'] and event.get('type') == 'key':
                            self.events.put(event)
                primed = True
            except Exception:
                # Do not expose request objects or private bearer tokens in logs/UI.
                state = {'active': False, 'connected': False, 'compatible': False,
                         'lease': 0, 'error': '正在等待本地助手连接'}
                with self.state_lock:
                    self.state = state
                self.events.put({'type': 'runtime_state', 'state': state})
            self.closed.wait(.5)

    def send_loop(self):
        while not self.closed.is_set():
            try:
                request = self.requests.get(timeout=.2)
            except queue.Empty:
                continue
            started = time.monotonic()
            try:
                result = self.client.show(request['path'], request['lease'])
                if (not result.get('device_reported') or result.get('result') != 'Ok(())'
                        or result.get('format') != 'gray16' or result.get('lease') != request['lease']):
                    raise RuntimeError('Hub did not confirm device display completion')
                event = {'type': 'display_complete', 'id': request['id'],
                         'device_reported': True, 'optical_verified': False,
                         'elapsed_ms': result.get('elapsed_ms', round((time.monotonic()-started)*1000))}
            except HubFailure as error:
                rejected = error.payload.get('submitted') is False
                event = {'type': 'display_rejected' if rejected else 'display_error',
                         'id': request['id'], 'error_kind': error.payload.get('error_kind', 'hub_error'),
                         'error': '前台权限已变更，等待应用重新激活' if rejected else '投送失败或结果未确认，请重新打开应用'}
            except Exception:
                event = {'type': 'display_error', 'id': request['id'], 'error_kind': 'result_unknown',
                         'error': '投送结果未确认，已停止发送；请在本地助手重新打开应用'}
            if not self.closed.is_set():
                self.events.put(event)


class AlbumPlayer(Player):
    def __init__(self, config, client):
        super().__init__(config)
        self.client = client
        self.runtime = {'active': False, 'connected': False, 'compatible': False, 'error': None}
        self.ever_ready = False

    def start_bridge(self):
        self.bridge = HubBridge(self.events, self.client)

    def event(self, event):
        if event.get('type') == 'key' and not self.ready:
            return
        if event.get('type') == 'runtime_state':
            self.runtime = event['state']
            available = all(self.runtime.get(k) for k in ('active', 'connected', 'compatible'))
            if available and not self.ready and not self.ever_ready:
                self.ever_ready = True
                super().event({'type': 'ready'})
            else:
                self.ready = available
            return
        if event.get('type') == 'display_rejected':
            if self.outstanding and self.outstanding['id'] == event.get('id'):
                self.prepared.appendleft(self.outstanding['frame'])
                self.outstanding = None
                self.pending = None
                self.ready = False
            return
        super().event(event)
