"""Local settings UI and lifecycle for the independently installed Hub app."""
import hashlib
import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import sqlite3
import sys
import threading
import time
from urllib.parse import urlsplit

from hub import AlbumPlayer, HubClient
from player import atomic_json, read_json

VERSION = '0.1.0'


def resources():
    return Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent.parent))


def install_inspector(data):
    root = resources()
    source = root / 'binaries' / 'portrait-gate'
    if not source.is_file():
        source = root / 'build' / 'portrait-gate'
    if not source.is_file():
        raise RuntimeError('缺少本地人像检测组件，请重新安装应用包')
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    destination = data / 'bin' / 'portrait-gate'
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.is_file() or hashlib.sha256(destination.read_bytes()).hexdigest() != digest:
        temporary = destination.with_suffix('.tmp')
        shutil.copyfile(source, temporary)
        if hashlib.sha256(temporary.read_bytes()).hexdigest() != digest:
            temporary.unlink(missing_ok=True)
            raise RuntimeError('人像检测组件校验失败')
        os.chmod(temporary, 0o700)
        os.replace(temporary, destination)
    os.chmod(destination, 0o700)
    return str(destination)


class Application:
    def __init__(self, data, hub, inspector):
        self.data, self.hub, self.inspector = Path(data), hub, inspector
        self.data.mkdir(parents=True, exist_ok=True)
        self.settings_path = self.data / 'settings.json'
        self.token = secrets.token_hex(32)
        self.lock = threading.RLock()
        self.engine = self.worker = None
        self.error = None
        self.settings = {'photo_root': '', 'cache_dir': str(self.data / 'cache' / '400x300'),
                         'interval_seconds': 300, 'cache_max_mb': 200,
                         'prefetch_frames': 6, 'autoplay': True}
        self.settings.update(read_json(self.settings_path, {}))

    def start(self):
        with self.lock:
            if not self.settings['photo_root'] or (self.worker and self.worker.is_alive()):
                return
            config = dict(self.settings, data_dir=str(self.data),
                          cache_max_bytes=self.settings['cache_max_mb'] * 1024 * 1024,
                          cache_max_files=1000, rescan_seconds=86400,
                          portrait_gate=self.inspector,
                          require_mount=os.path.ismount(self.settings['photo_root']))
            staging = self.data / 'staging'
            if staging.is_dir():
                for file in staging.glob('*.png'):
                    file.unlink(missing_ok=True)
            self.engine = AlbumPlayer(config, self.hub)
            self.engine.paused = not self.settings['autoplay']
            atomic_json(self.data / 'control.json', {'id': 0, 'command': 'none'})
            self.error = None
            def run():
                try:
                    self.engine.run()
                except Exception:
                    self.error = '相册已停止，请从本地助手重新打开应用'
            self.worker = threading.Thread(target=run, daemon=True)
            self.worker.start()

    def close(self):
        with self.lock:
            if self.engine:
                self.engine.stop.set()
            if self.worker:
                self.worker.join(timeout=55)
                if self.worker.is_alive():
                    raise RuntimeError('相册正在完成后台处理，请稍后重试')
            if self.engine and self.engine.scan_thread and self.engine.scan_thread.is_alive():
                raise RuntimeError('正在等待旧目录扫描结束，请稍后重试')

    def status(self):
        with self.lock:
            player = read_json(self.data / 'status.json', {}) if self.engine else {}
            runtime = dict(self.engine.runtime) if self.engine else {
                'active': False, 'connected': False, 'compatible': True, 'error': None}
            config = dict(self.settings)
            if self.engine:
                config['autoplay'] = not self.engine.paused
                if config['autoplay'] != self.settings['autoplay']:
                    self.settings['autoplay'] = config['autoplay']
                    atomic_json(self.settings_path, self.settings)
            if self.error:
                player['last_error'] = self.error
            return {'version': VERSION, 'config': config, 'player': player, 'runtime': runtime}

    def configure(self, changes):
        allowed = set(self.settings)
        if not isinstance(changes, dict) or set(changes) - allowed:
            raise ValueError('配置字段无效')
        config = dict(self.settings, **changes)
        for name, low, high in [('interval_seconds', 60, 86400), ('cache_max_mb', 10, 2048),
                                ('prefetch_frames', 1, 12)]:
            if type(config[name]) is not int or not low <= config[name] <= high:
                raise ValueError(f'{name} 超出允许范围')
        if type(config['autoplay']) is not bool:
            raise ValueError('自动轮播设置无效')
        for name in ('photo_root', 'cache_dir'):
            if not isinstance(config[name], str):
                raise ValueError('目录必须是绝对路径')
            if name == 'photo_root' and not config[name]:
                continue
            path = Path(config[name]).expanduser()
            if not path.is_absolute():
                raise ValueError('请填写已挂载目录的绝对路径')
            if name == 'photo_root' and not path.is_dir():
                raise ValueError('照片目录不可用，请先在访达连接 NAS')
            config[name] = str(path)
        if config['photo_root'] and Path(config['cache_dir']).resolve().is_relative_to(Path(config['photo_root']).resolve()):
            raise ValueError('小图缓存请放在照片来源目录之外')
        with self.lock:
            if self.engine and (self.engine.outstanding or self.engine.pending):
                raise RuntimeError('当前正在投送照片，请稍后保存设置')
            reset_library = any(config[k] != self.settings[k] for k in ('photo_root', 'cache_dir'))
            self.close()
            if reset_library:
                # Changing libraries must never show a cached photo from the old one.
                with contextlib.closing(sqlite3.connect(self.data / 'catalog.sqlite3')) as db, db:
                    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    if 'cache' in tables:
                        for key, file in db.execute('SELECT key,file FROM cache'):
                            path = Path(file)
                            if (len(key) == 64 and all(c in '0123456789abcdef' for c in key)
                                    and path.name == key + '.png'
                                    and path.parent.resolve() == Path(self.settings['cache_dir']).resolve()):
                                path.unlink(missing_ok=True)
                    for table in ('photos', 'history', 'meta', 'cache'):
                        if table in tables:
                            db.execute('DELETE FROM ' + table)
                (self.data / 'status.json').unlink(missing_ok=True)
            self.settings = config
            atomic_json(self.settings_path, config)
            self.engine = self.worker = None
            self.start()
        return self.status()

    def control(self, command):
        with self.lock:
            if not self.engine or not self.worker.is_alive():
                raise RuntimeError('请先设置照片目录；相册停止后请从本地助手重新打开')
            if command == 'rescan':
                if (self.engine.store.meta('scan') or {}).get('state') != 'scanning':
                    self.engine.rescan.set()
            elif command in ('pause', 'resume'):
                self.engine.paused = command == 'pause'
                self.engine.next_due = time.monotonic() + self.settings['interval_seconds']
                self.settings['autoplay'] = command == 'resume'
                atomic_json(self.settings_path, self.settings)
            elif command in ('next', 'previous'):
                if not self.engine.ready:
                    raise RuntimeError('请在本地助手连接设备并激活 NAS 相册')
                atomic_json(self.data / 'control.json', {'id': time.time_ns(), 'command': command})
            else:
                raise ValueError('不支持的操作')
        return self.status()


def server_for(app, port):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def guard(self, mutation=False):
            if self.headers.get('Host') not in {f'127.0.0.1:{port}', f'localhost:{port}'}:
                self.reply(403, {'error': '仅允许本机访问'})
                return False
            origin = self.headers.get('Origin')
            if origin and origin not in {f'http://127.0.0.1:{port}', f'http://localhost:{port}'}:
                self.reply(403, {'error': '不允许跨站访问'})
                return False
            if mutation and not secrets.compare_digest(self.headers.get('X-App-Token', ''), app.token):
                self.reply(403, {'error': '应用凭证无效，请刷新页面'})
                return False
            return True

        def reply(self, status, value, content_type='application/json; charset=utf-8'):
            body = json.dumps(value, ensure_ascii=False).encode() if isinstance(value, dict) else value
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if not self.guard():
                return
            path = urlsplit(self.path).path
            if path == '/api/boot':
                return self.reply(200, {'token': app.token, 'version': VERSION})
            if path == '/api/status':
                return self.reply(200, app.status())
            if path == '/screen.png':
                state = app.status()['player']
                file = Path(state.get('current_frame') or '/nonexistent')
                if file.is_file():
                    return self.reply(200, file.read_bytes(), 'image/png')
            static = {'/': ('index.html', 'text/html; charset=utf-8'),
                      '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
                      '/style.css': ('style.css', 'text/css; charset=utf-8')}
            if path in static:
                name, mime = static[path]
                return self.reply(200, (resources() / 'web' / name).read_bytes(), mime)
            self.reply(404, {'error': '未找到'})

        def do_POST(self):
            if not self.guard(mutation=True):
                return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 16384:
                    raise ValueError('请求大小无效')
                value = json.loads(self.rfile.read(length))
                if not isinstance(value, dict):
                    raise ValueError('请求必须是配置对象')
                if self.path == '/api/config':
                    return self.reply(200, app.configure(value))
                if self.path == '/api/control':
                    return self.reply(200, app.control(value.get('command')))
                self.reply(404, {'error': '未找到'})
            except ValueError as error:
                self.reply(400, {'error': str(error)})
            except (TypeError, KeyError):
                self.reply(400, {'error': '配置无效，请检查目录和数值范围'})
            except RuntimeError as error:
                self.reply(409, {'error': str(error)})
            except OSError:
                self.reply(400, {'error': '目录不可读写，请检查访问权限'})

    return ThreadingHTTPServer(('127.0.0.1', port), Handler)


def main():
    data = Path(os.environ['ZECTRIX_APP_DATA'])
    client = HubClient(os.environ['ZECTRIX_HUB_URL'], os.environ['ZECTRIX_APP_TOKEN'])
    port = int(os.environ.get('ZECTRIX_APP_PORT') or os.environ['ZECTRIX_VOCAB_PORT'])
    app = Application(data, client, install_inspector(data))
    server = server_for(app, port)
    def close(*_):
        if app.engine:
            app.engine.stop.set()
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, close)
    signal.signal(signal.SIGINT, close)
    threading.Thread(target=app.start, daemon=True).start()
    try:
        server.serve_forever(poll_interval=.2)
    finally:
        app.close()
        server.server_close()
