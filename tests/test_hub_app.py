"""Offline integration against fake loopback Hub and browser HTTP clients.

No device is contacted, no real library is read, and all photos are generated.
"""
from contextlib import contextmanager
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import queue
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

from PIL import Image

APP = Path(__file__).resolve().parents[1] / 'app'
sys.path.insert(0, str(APP))
import backend
from hub import AlbumPlayer, HubBridge, HubClient, HubFailure
import player


def wait_until(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(.01)
    raise AssertionError('Timed out waiting for fake Hub condition')


@contextmanager
def fake_hub():
    state = {'token': 'test-hub-backend-secret', 'active': True, 'lease': 7,
             'mode': 'ok', 'images': [], 'requests': []}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, code, payload, headers=None):
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def auth(self):
            state['requests'].append((self.path, dict(self.headers)))
            if self.headers.get('Authorization') != 'Bearer ' + state['token']:
                self.reply(401, {'error': 'Unauthenticated'})
                return False
            return True

        def do_GET(self):
            if not self.auth():
                return
            if self.path == '/runtime/status':
                return self.reply(200, {'active': state['active'], 'lease': state['lease'],
                                       'device': {'connected': True},
                                       'features': {'image': {'formats': ['mono', 'gray16']}}})
            if self.path.startswith('/runtime/events'):
                return self.reply(200, {'cursor': 0, 'events': []})
            if self.path == '/redirect':
                return self.reply(302, {}, {'Location': '/runtime/status'})
            self.reply(404, {})

        def do_POST(self):
            if not self.auth():
                return
            data = self.rfile.read(int(self.headers.get('Content-Length', 0)))
            state['images'].append({'path': self.path, 'data': data, 'headers': dict(self.headers)})
            if state['mode'] == 'reject':
                return self.reply(409, {'error_kind': 'inactive_lease', 'submitted': False,
                                       'device_reported': False})
            if state['mode'] == 'unknown':
                return self.reply(500, {'error_kind': 'result_unknown', 'submitted': True,
                                       'device_reported': False})
            if state['mode'] == 'unconfirmed':
                return self.reply(200, {'ok': True, 'device_reported': False, 'result': 'Ok(())'})
            self.reply(200, {'ok': True, 'device_reported': True, 'result': 'Ok(())',
                             'format': 'mono' if state['mode'] == 'wrong_format' else 'gray16',
                             'lease': state['lease'] + (1 if state['mode'] == 'wrong_lease' else 0),
                             'elapsed_ms': 4})

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    state['url'] = 'http://127.0.0.1:' + str(server.server_address[1])
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        worker.join(2)


def config_for(root):
    return {'data_dir': str(root / 'data'), 'cache_dir': str(root / 'cache'),
            'photo_root': str(root / 'photos'), 'interval_seconds': 300,
            'cache_max_bytes': 209715200, 'cache_max_files': 1000,
            'require_mount': False, 'prefetch_frames': 6}


def make_frame(engine, number=1):
    file = engine.staging / (str(number) + '.png')
    image = player.stamp_capture_date(Image.new('RGB', (400, 300), (60, 95, 110)), '2020-01-02')
    image.save(file)
    return {'key': str(number), 'source': str(engine.root / (str(number) + '.jpg')),
            'file': str(file), 'cache_hit': False, 'navigation': 'next',
            'gate': {'accepted': True, 'faces': [{'x': .2, 'y': .2, 'width': .3, 'height': .3}],
                     'capture_date': '2020-01-02', 'date_stamp_version': player.STAMP_VERSION,
                     'render_sha256': hashlib.sha256(file.read_bytes()).hexdigest()}}


class HubClientTests(unittest.TestCase):
    def test_credentials_loopback_only_and_redirect_not_followed(self):
        for url in ['https://127.0.0.1:1234', 'http://example.com',
                    'http://user:secret@127.0.0.1', 'http://127.0.0.1/runtime/status']:
            with self.assertRaises(ValueError):
                HubClient(url, 'token')
        with fake_hub() as state:
            client = HubClient(state['url'], state['token'])
            # Proxy environment must never intercept the private Hub bearer.
            with patch.dict(os.environ, {'http_proxy': 'http://127.0.0.1:1',
                                         'HTTP_PROXY': 'http://127.0.0.1:1',
                                         'NO_PROXY': '', 'no_proxy': ''}):
                self.assertTrue(client.status()['active'])
            previous = len(state['requests'])
            with self.assertRaises(HubFailure) as caught:
                client.request('/redirect')
            self.assertEqual(caught.exception.status, 302)
            self.assertEqual(len(state['requests']), previous + 1)

    def test_raw_gray16_png_and_backend_lease_headers(self):
        with fake_hub() as state, tempfile.TemporaryDirectory() as folder:
            image = Path(folder) / 'small.png'
            Image.new('RGB', (400, 300)).save(image)
            result = HubClient(state['url'], state['token']).show(image, 7)
            self.assertTrue(result['device_reported'])
            request = state['images'][0]
            self.assertEqual(request['path'], '/runtime/image')
            self.assertEqual(request['data'], image.read_bytes())
            self.assertEqual(request['headers']['X-Lease'], '7')
            self.assertEqual(request['headers']['X-Image-Format'], 'gray16')
            self.assertEqual(request['headers']['Content-Type'], 'image/png')


class HubPlayerTests(unittest.TestCase):
    def test_disconnected_keys_cannot_consume_prefetch_or_toggle_pause(self):
        with tempfile.TemporaryDirectory() as folder:
            engine = AlbumPlayer(config_for(Path(folder)), object())
            frame = make_frame(engine)
            engine.prepared.append(frame)
            engine.paused = True
            try:
                engine.event({'type': 'runtime_state', 'state': {
                    'active': True, 'connected': False, 'compatible': True, 'lease': 7}})
                engine.event({'type': 'key', 'key': 'Down', 'action': 'Short'})
                engine.event({'type': 'key', 'key': 'Confirm', 'action': 'Short'})
                self.assertEqual(len(engine.prepared), 1)
                self.assertTrue(Path(frame['file']).is_file())
                self.assertTrue(engine.paused)
                self.assertIsNone(engine.pending)
            finally:
                engine.executor.shutdown(wait=True)

    def test_old_catalog_entry_cannot_escape_current_photo_root(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = config_for(root)
            Path(config['photo_root']).mkdir()
            outside = root / 'other-library.jpg'
            Image.new('RGB', (400, 300)).save(outside)
            engine = AlbumPlayer(config, object())
            engine.store.add([(str(outside), '.jpg', 'old')])
            try:
                with patch.object(engine, 'inspect') as inspect:
                    with self.assertLogs(level='ERROR'):
                        with self.assertRaises(RuntimeError):
                            engine.prepare_next()
                    inspect.assert_not_called()
            finally:
                engine.executor.shutdown(wait=True)

    def run_result(self, mode):
        with fake_hub() as state, tempfile.TemporaryDirectory() as folder:
            state['mode'] = mode
            root = Path(folder)
            engine = AlbumPlayer(config_for(root), HubClient(state['url'], state['token']))
            bridge = engine.bridge = HubBridge(engine.events, engine.client)
            try:
                runtime = wait_until(lambda: bridge.state.get('compatible') and dict(bridge.state))
                engine.event({'type': 'runtime_state', 'state': runtime})
                frame = make_frame(engine)
                engine.prepared.append(frame)
                engine.request('next')
                self.assertEqual(list(engine.cache.glob('*.png')), [])
                while True:
                    event = engine.events.get(timeout=3)
                    if event.get('id') == 1 and event.get('type') in {
                            'display_complete', 'display_rejected', 'display_error'}:
                        break
                # Wrong request id cannot commit the pending frame.
                engine.event({'type': 'display_complete', 'id': 999})
                self.assertEqual(list(engine.cache.glob('*.png')), [])
                engine.event(event)
                if mode == 'ok':
                    self.assertEqual(len(list(engine.cache.glob('*.png'))), 1)
                    self.assertEqual(engine.receipts, 1)
                    engine.event({'type': 'runtime_state', 'state': dict(runtime, active=False)})
                    self.assertFalse(engine.ready)
                    count = len(state['images'])
                    engine.event({'type': 'runtime_state', 'state': runtime})
                    time.sleep(.05)
                    self.assertEqual(len(state['images']), count,
                                     'Hub activation should not duplicate a retained frame')
                elif mode == 'reject':
                    self.assertEqual(list(engine.cache.glob('*.png')), [])
                    self.assertFalse(engine.stop.is_set())
                    self.assertIsNone(engine.outstanding)
                    self.assertEqual(len(engine.prepared), 1)
                else:
                    self.assertEqual(list(engine.cache.glob('*.png')), [])
                    self.assertTrue(engine.stop.is_set())
                    time.sleep(.1)
                    self.assertEqual(len(state['images']), 1, 'unknown send must never replay')
            finally:
                bridge.terminate()
                engine.stop.set()
                engine.executor.shutdown(wait=True)

    def test_confirmed_receipt_enters_cache_and_activation_does_not_replay(self):
        self.run_result('ok')

    def test_inactive_lease_preserves_staging_without_cache(self):
        self.run_result('reject')

    def test_unknown_result_stops_without_cache_or_replay(self):
        self.run_result('unknown')

    def test_unconfirmed_http_success_is_not_a_display_receipt(self):
        self.run_result('unconfirmed')

    def test_wrong_ack_format_is_not_a_display_receipt(self):
        self.run_result('wrong_format')

    def test_wrong_ack_lease_is_not_a_display_receipt(self):
        self.run_result('wrong_lease')


class BackendTests(unittest.TestCase):
    def test_stale_navigation_control_is_reset_when_process_starts(self):
        class FakeEngine:
            def __init__(self, *args):
                self.stop = threading.Event()
                self.rescan = threading.Event()
                self.paused = False
                self.scan_thread = None
            def run(self):
                self.stop.wait(2)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            photos = root / 'photos'; photos.mkdir()
            app = backend.Application(root / 'data', object(), '/unused')
            app.settings['photo_root'] = str(photos)
            player.atomic_json(app.data / 'control.json', {'id': 123, 'command': 'next'})
            with patch.object(backend, 'AlbumPlayer', FakeEngine):
                try:
                    app.start()
                    control = player.read_json(app.data / 'control.json')
                    self.assertEqual(control['command'], 'none')
                    self.assertEqual(control['id'], 0)
                finally:
                    app.close()

    def test_helper_copy_is_verified_repaired_and_executable(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'bundle/binaries').mkdir(parents=True)
            source = root / 'bundle/binaries/portrait-gate'
            source.write_bytes(b'trusted bundled inspector fixture')
            data = root / 'data'
            with patch.object(backend, 'resources', return_value=root / 'bundle'):
                target = Path(backend.install_inspector(data))
                self.assertEqual(target.read_bytes(), source.read_bytes())
                self.assertEqual(target.stat().st_mode & 0o777, 0o700)
                target.write_bytes(b'changed helper')
                repaired = Path(backend.install_inspector(data))
                self.assertEqual(repaired.read_bytes(), source.read_bytes())

    def test_configuration_validation_and_source_change_isolation(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            old = root / 'old-photos'; old.mkdir()
            new = root / 'new-photos'; new.mkdir()
            app = backend.Application(root / 'data', object(), '/unused-inspector')
            with patch.object(app, 'start'):
                app.configure({'photo_root': str(old)})
                store = player.Store(app.data / 'catalog.sqlite3')
                store.add([(str(old / 'old.jpg'), '.jpg', 'old')])
                cache = Path(app.settings['cache_dir']); cache.mkdir(parents=True)
                key = 'a' * 64
                retained = cache / (key + '.png'); Image.new('RGB', (400, 300)).save(retained)
                unrelated = cache / 'unrelated.txt'; unrelated.write_text('keep')
                with store.db() as db:
                    db.execute('INSERT INTO cache VALUES (?,?,?,?,?,?,?)',
                               (key, str(old / 'old.jpg'), str(retained), retained.stat().st_size,
                                time.time(), '{}', player.POLICY))
                app.configure({'photo_root': str(new)})
                self.assertEqual(store.stats()['catalog'], 0)
                self.assertIsNone(store.fallback(set()))
                self.assertFalse(retained.exists(), 'old tracked cache must not become unbounded orphan data')
                self.assertTrue(unrelated.exists(), 'only owned/tracked cache files may be removed')
                for change in [{'interval_seconds': 0}, {'cache_max_mb': 1},
                               {'prefetch_frames': 0}, {'autoplay': 'yes'},
                               {'photo_root': 'relative/path'}, {'photo_root': str(root / 'missing')},
                               {'cache_dir': str(new / 'generated')}, {'hub_token': 'secret'}]:
                    with self.assertRaises((ValueError, RuntimeError)):
                        app.configure(change)

    def test_http_boot_token_is_separate_from_hub_and_mutations_guarded(self):
        with tempfile.TemporaryDirectory() as folder:
            app = backend.Application(Path(folder), HubClient('http://127.0.0.1:1', 'hub-private-token'), '/unused')
            with socket.socket() as probe:
                probe.bind(('127.0.0.1', 0)); port = probe.getsockname()[1]
            server = backend.server_for(app, port)
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            opener = build_opener(ProxyHandler({}))
            url = f'http://127.0.0.1:{port}'
            def call(path, payload=None, headers=None):
                data = None if payload is None else json.dumps(payload).encode()
                return opener.open(Request(url + path, data=data, headers=headers or {}), timeout=3)
            try:
                with call('/api/boot') as response:
                    boot = json.load(response)
                self.assertNotEqual(boot['token'], 'hub-private-token')
                with call('/api/status') as response:
                    status = response.read().decode()
                self.assertNotIn('hub-private-token', status)
                self.assertNotIn(boot['token'], status)
                with self.assertRaises(HTTPError) as error:
                    call('/api/config', {'interval_seconds': 600})
                self.assertEqual(error.exception.code, 403)
                with self.assertRaises(HTTPError) as error:
                    call('/api/status', headers={'Host': 'external.example'})
                self.assertEqual(error.exception.code, 403)
                with self.assertRaises(HTTPError) as error:
                    call('/api/config', {'interval_seconds': 600},
                         {'X-App-Token': boot['token'], 'Origin': 'https://external.example'})
                self.assertEqual(error.exception.code, 403)
                with call('/api/config', {'interval_seconds': 600}, {'X-App-Token': boot['token']}) as response:
                    self.assertEqual(json.load(response)['config']['interval_seconds'], 600)
            finally:
                server.shutdown(); server.server_close(); thread.join(2)


if __name__ == '__main__':
    unittest.main()
