#!/usr/bin/env python3
"""Local NAS portrait player. Originals stay on the NAS; only displayed small frames persist."""
from collections import deque
import concurrent.futures
import contextlib
from datetime import date
import hashlib
import json
import logging
import os
from pathlib import Path
import queue
import random
import re
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time

from PIL import Image, ImageDraw, ImageFont, ImageOps

APP = Path(__file__).resolve().parent
STAMP_VERSION = 'capture-date-bottom-right-v1'
POLICY = 'camera-face-075-cover-date-v2'
EXTENSIONS = {'.jpg', '.jpeg', '.png', '.heic', '.heif', '.webp', '.tif', '.tiff',
              '.cr2', '.cr3', '.nef', '.arw', '.dng', '.raf', '.orf', '.rw2'}
SKIP_DIRS = {'@eadir', '#recycle', '@recycle', '$recycle.bin', '#snapshot'}
SCREEN = re.compile(r'screen[ _-]?(?:shot|capture)|截[屏图]|屏幕快照|屏幕截图', re.I)


def valid_capture_date(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', value):
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        return None


def capture_date_for(source, gate):
    result = dict(gate)
    capture_date = valid_capture_date(gate.get('capture_date'))
    if capture_date:
        result['capture_date'] = capture_date
        return result
    # Only explicit dates in the filename can supplement missing camera metadata.
    # Parent-folder names and filesystem timestamps may describe an import/edit.
    candidates = set()
    for match in re.finditer(r'(?<![0-9])([0-9]{4})(-?)([0-9]{2})\2([0-9]{2})(?![0-9])', Path(source).stem):
        candidate = valid_capture_date(f'{match[1]}-{match[3]}-{match[4]}')
        if candidate:
            candidates.add(candidate)
    if len(candidates) == 1:
        result.update(capture_date=candidates.pop(), capture_date_source='filename',
                      capture_date_status='filename_fallback')
    else:
        result.update(capture_date=None, capture_date_source=None,
                      capture_date_status='missing_date')
    return result


def stamp_capture_date(image, date_string):
    if not valid_capture_date(date_string):
        raise ValueError('拍摄日期无效')
    frame = image.copy()
    draw = ImageDraw.Draw(frame)
    font = ImageFont.load_default(size=15)
    left, top, right, bottom = draw.textbbox((0, 0), date_string, font=font, stroke_width=1)
    position = (frame.width - 8 - right, frame.height - 7 - bottom)
    draw.text(position, date_string, font=font, fill='white', stroke_width=1, stroke_fill='black')
    return frame


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    os.replace(temp, path)


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return default


class Store:
    def __init__(self, path):
        self.path = path
        with self.db() as db:
            db.executescript('''
              PRAGMA journal_mode=WAL;
              CREATE TABLE IF NOT EXISTS photos (
                path TEXT PRIMARY KEY, suffix TEXT, seen TEXT,
                size INTEGER, mtime INTEGER, gate TEXT, policy TEXT,
                fail_until REAL NOT NULL DEFAULT 0);
              CREATE TABLE IF NOT EXISTS cache (
                key TEXT PRIMARY KEY, source TEXT, file TEXT, bytes INTEGER,
                used REAL, gate TEXT, policy TEXT);
              CREATE TABLE IF NOT EXISTS history (
                id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT, shown REAL);
              CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
            ''')

    @contextlib.contextmanager
    def db(self):
        db = sqlite3.connect(str(self.path), timeout=30)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA busy_timeout=30000')
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def meta(self, key, value=None):
        with self.db() as db:
            if value is not None:
                db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (key, json.dumps(value)))
            row = db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
            return json.loads(row[0]) if row else None

    def seed(self, index, root):
        if self.meta('seed_done'):
            return
        batch = []
        if index.is_file():
            with index.open(encoding='utf-8', errors='replace') as handle:
                for line in handle:
                    nas = line.rstrip('\n').partition('|')[2]
                    if not nas.startswith('/volume1/photo/'):
                        continue
                    relative = Path(nas).relative_to('/volume1/photo')
                    if '..' in relative.parts or any(p.startswith('.') or p.lower() in SKIP_DIRS for p in relative.parts):
                        continue
                    p = root / relative
                    if p.suffix.lower() in EXTENSIONS:
                        batch.append((str(p), p.suffix.lower(), 'seed'))
                    if len(batch) >= 1000:
                        self.add(batch)
                        batch = []
        self.add(batch)
        self.meta('seed_done', True)

    def add(self, rows):
        if rows:
            with self.db() as db:
                db.executemany('''INSERT INTO photos(path,suffix,seen) VALUES (?,?,?)
                  ON CONFLICT(path) DO UPDATE SET seen=excluded.seen, suffix=excluded.suffix''', rows)

    def scan(self, root, stop, require_mount=True):
        if not root.is_dir() or (require_mount and not os.path.ismount(root)):
            self.meta('scan', {'state': 'nas_unavailable', 'at': time.time()})
            return
        generation = str(time.time_ns())
        volume = root.stat().st_dev
        count = 0
        errors = 0
        stack = [root]
        batch = []
        self.meta('scan', {'state': 'scanning', 'found': 0, 'at': time.time()})
        while stack and not stop.is_set():
            directory = stack.pop()
            try:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        if stop.is_set():
                            break
                        name = entry.name
                        if name.startswith('.') or name.lower() in SKIP_DIRS:
                            continue
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        elif Path(name).suffix.lower() in EXTENSIONS and entry.is_file(follow_symlinks=False):
                            batch.append((entry.path, Path(name).suffix.lower(), generation))
                            count += 1
                        if len(batch) >= 1000:
                            self.add(batch)
                            batch = []
                            self.meta('scan', {'state': 'scanning', 'found': count, 'at': time.time()})
            except OSError:
                errors += 1
                logging.exception('NAS folder unavailable: %s', directory)
        self.add(batch)
        complete = (not stop.is_set() and root.is_dir() and errors == 0 and count > 0
                    and (not require_mount or os.path.ismount(root)) and root.stat().st_dev == volume)
        if complete:
            with self.db() as db:
                db.execute('DELETE FROM photos WHERE seen != ?', (generation,))
        self.meta('scan', {'state': 'complete' if complete else 'partial', 'found': count,
                           'errors': errors, 'at': time.time()})

    def choose(self, exclude):
        with self.db() as db:
            for _ in range(12):
                row = db.execute('SELECT * FROM photos WHERE fail_until < ? ORDER BY random() LIMIT 1',
                                 (time.time(),)).fetchone()
                if row is None or row['path'] not in exclude:
                    return dict(row) if row else None
        return None

    def photo(self, source):
        with self.db() as db:
            row = db.execute('SELECT * FROM photos WHERE path=?', (source,)).fetchone()
            return dict(row) if row else None

    def fail(self, path, seconds=86400):
        with self.db() as db:
            db.execute('UPDATE photos SET fail_until=? WHERE path=?', (time.time() + seconds, path))

    def gate(self, path, stat, result):
        with self.db() as db:
            db.execute('''UPDATE photos SET size=?,mtime=?,gate=?,policy=?,fail_until=? WHERE path=?''',
                       (stat.st_size, stat.st_mtime_ns, json.dumps(result), POLICY,
                        0 if result['accepted'] else time.time() + 86400, path))

    def cached(self, key):
        with self.db() as db:
            row = db.execute('SELECT * FROM cache WHERE key=? AND policy=?', (key, POLICY)).fetchone()
            return dict(row) if row and Path(row['file']).is_file() else None

    def fallback(self, exclude):
        with self.db() as db:
            rows = list(db.execute('SELECT * FROM cache WHERE policy=? ORDER BY random() LIMIT 30', (POLICY,)))
        for row in rows:
            if row['source'] not in exclude and Path(row['file']).is_file():
                return dict(row)
        return None

    def history(self):
        with self.db() as db:
            rows = list(db.execute('''SELECT cache.* FROM history JOIN cache USING(key)
                       WHERE cache.policy=? ORDER BY history.id DESC LIMIT 100''', (POLICY,)))
        return [dict(row) for row in reversed(rows) if Path(row['file']).is_file()]

    def commit(self, frame, cache_dir, append_history=True):
        destination = cache_dir / (frame['key'] + '.png')
        if Path(frame['file']) != destination:
            os.replace(frame['file'], destination)
        size = destination.stat().st_size
        with self.db() as db:
            db.execute('INSERT OR REPLACE INTO cache VALUES (?,?,?,?,?,?,?)',
                       (frame['key'], frame['source'], str(destination), size, time.time(),
                        json.dumps(frame['gate']), POLICY))
            if append_history:
                db.execute('INSERT INTO history(key,shown) VALUES (?,?)', (frame['key'], time.time()))
                db.execute('DELETE FROM history WHERE id NOT IN (SELECT id FROM history ORDER BY id DESC LIMIT 1000)')
        frame.update(file=str(destination), cache_hit=True)

    def trim(self, max_bytes, max_files, protected):
        with self.db() as db:
            rows = list(db.execute('SELECT * FROM cache ORDER BY used DESC'))
            total = sum(r['bytes'] for r in rows)
            count = len(rows)
            for row in reversed(rows):
                if total <= max_bytes and count <= max_files:
                    break
                if row['key'] in protected:
                    continue
                with contextlib.suppress(FileNotFoundError):
                    Path(row['file']).unlink()
                db.execute('DELETE FROM cache WHERE key=?', (row['key'],))
                total -= row['bytes']
                count -= 1
            return {'files': count, 'bytes': total, 'max_bytes': max_bytes,
                    'dimensions': [400, 300], 'directory': str(Path(rows[0]['file']).parent) if rows else None}

    def stats(self):
        with self.db() as db:
            return {'catalog': db.execute('SELECT count(*) FROM photos').fetchone()[0],
                    'portrait_checked': db.execute('SELECT count(*) FROM photos WHERE gate IS NOT NULL').fetchone()[0]}


class Player:
    def __init__(self, config):
        self.config = config
        self.data = Path(config['data_dir']).expanduser()
        self.cache = Path(config['cache_dir']).expanduser()
        self.staging = self.data / 'staging'
        for p in [self.data, self.cache, self.staging]:
            p.mkdir(parents=True, exist_ok=True)
        self.store = Store(self.data / 'catalog.sqlite3')
        self.stop = threading.Event()
        self.scan_thread = None
        self.rescan = threading.Event()
        self.events = queue.Queue()
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        self.future = None
        self.prepared = deque()
        self.prefetch_target = max(1, min(12, int(config.get('prefetch_frames', 6))))
        self.prefetch_retry_at = 0
        self.prefetch_error = None
        self.bridge = None
        self.ready = False
        self.outstanding = None
        self.pending = None
        self.request_id = 0
        self.history = self.store.history()
        self.cursor = len(self.history) - 1
        self.current = None
        self.next_due = time.monotonic() + config['interval_seconds']
        self.paused = False
        self.last_error = None
        self.stats_cache = {'files': 0, 'bytes': 0}
        self.receipts = 0
        self.last_navigation = None
        self.last_control = None
        self.root = Path(config['photo_root'])
        self.store.seed(Path(config.get('seed_index', str(self.data / 'no-seed-index'))), self.root)
        previous = read_json(self.data / 'status.json', {})
        self.paused = previous.get('paused', False)
        self.startup_source = None
        for position, frame in enumerate(self.history):
            if frame['file'] == previous.get('current_frame'):
                self.current = dict(frame)
                self.current['gate'] = json.loads(frame['gate']) if isinstance(frame['gate'], str) else frame['gate']
                self.cursor = position
        if self.current:
            remaining = previous.get('next_change_in_seconds')
            if remaining is not None:
                elapsed = max(0, time.time() - previous.get('updated_at', time.time()))
                self.next_due = time.monotonic() + max(0, remaining - elapsed)
        else:
            candidate = Path(previous.get('current_source') or '')
            if candidate.is_absolute() and candidate.is_relative_to(self.root) and '..' not in candidate.parts:
                self.startup_source = str(candidate)

    def snapshot(self):
        value = {
            'running': not self.stop.is_set(), 'pid': os.getpid(), 'ready': self.ready,
            'player_version': '0.1.0',
            'mode': 'Gray16', 'interval_seconds': self.config['interval_seconds'],
            'paused': self.paused, 'portrait_policy': POLICY,
            'photo_root': str(self.root), 'cache': self.stats_cache,
            'scan': self.store.meta('scan'), 'stats': self.store.stats(),
            'current_source': self.current['source'] if self.current else None,
            'current_frame': self.current['file'] if self.current else None,
            'date_stamp': {'version': STAMP_VERSION, 'position': 'bottom_right',
                           'format': 'YYYY-MM-DD',
                           'capture_date': (json.loads(self.current['gate']) if isinstance(self.current['gate'], str) else self.current['gate']).get('capture_date') if self.current else None},
            'display_receipts': self.receipts,
            'busy': self.pending is not None or self.outstanding is not None,
            'prefetch': {'ready': len(self.prepared), 'target': self.prefetch_target,
                         'preparing': self.future is not None,
                         'temporary_bytes': sum(Path(r['file']).stat().st_size for r in self.prepared
                                                if not r.get('cache_hit') and Path(r['file']).is_file()),
                         'last_error': self.prefetch_error},
            'last_navigation': self.last_navigation,
            'last_error': self.last_error, 'updated_at': time.time(),
            'next_change_in_seconds': None if self.paused else max(0, round(self.next_due-time.monotonic())),
        }
        atomic_json(self.data / 'status.json', value)
        return value

    def inspect(self, row, stat):
        if row['policy'] == POLICY and row['size'] == stat.st_size and row['mtime'] == stat.st_mtime_ns:
            old = json.loads(row['gate']) if row['gate'] else None
            if old:
                return old
        if SCREEN.search(Path(row['path']).name):
            result = {'accepted': False, 'reason': 'screenshot_filename', 'faces': []}
        else:
            process = subprocess.run([self.config['portrait_gate'], row['path']], capture_output=True,
                                     text=True, timeout=45)
            if process.returncode:
                raise RuntimeError('Portrait inspection failed: ' + process.stderr[-500:])
            result = json.loads(process.stdout)
        self.store.gate(row['path'], stat, result)
        return result

    def render(self, source, gate, key):
        destination = self.staging / (key + '.png')
        with tempfile.TemporaryDirectory(prefix='zectrix-photo-convert-') as temporary:
            input_path = source
            if source.suffix.lower() not in {'.jpg', '.jpeg', '.png', '.webp', '.tif', '.tiff'}:
                input_path = Path(temporary) / 'decoded.png'
                result = subprocess.run(['/usr/bin/sips', '-s', 'format', 'png', '-Z', '1200',
                                         str(source), '--out', str(input_path)], capture_output=True, timeout=90)
                if result.returncode:
                    raise RuntimeError('Image decode failed')
            with Image.open(input_path) as original:
                if original.format == 'JPEG':
                    original.draft('RGB', (1600, 1200))
                im = ImageOps.exif_transpose(original).convert('RGB')
                w, h = im.size
                face = max(gate['faces'], key=lambda f: f['width'] * f['height'])
                fx = (face['x'] + face['width'] / 2) * w
                fy = (face['y'] + face['height'] / 2) * h
                cw, ch = min(w, h * 4 / 3), min(h, w * 3 / 4)
                left = max(0, min(w - cw, fx - cw * .5))
                top = max(0, min(h - ch, fy - ch * .4))
                frame = im.crop((round(left), round(top), round(left+cw), round(top+ch)))
                frame = frame.resize((400, 300), Image.Resampling.LANCZOS)
                frame = stamp_capture_date(frame, gate['capture_date'])
                frame.save(destination, optimize=True)
        return str(destination)

    def prepare_next(self, extra_exclude=None):
        started = time.monotonic()
        reserved = set(extra_exclude or ())
        exclude = {r['source'] for r in self.history[-20:]} | reserved
        online = self.root.is_dir() and (not self.config.get('require_mount', True) or os.path.ismount(self.root))
        for _ in range(30 if online else 0):
            if self.stop.is_set():
                return None
            preferred, self.startup_source = self.startup_source, None
            row = self.store.photo(preferred) if preferred and preferred not in reserved else None
            if row is None:
                row = self.store.choose(exclude)
            if not row:
                break
            source = Path(row['path'])
            try:
                if not source.resolve().is_relative_to(self.root.resolve()):
                    raise RuntimeError('Photo index path is outside the selected library')
                stat = source.stat()
                gate = self.inspect(row, stat)
                if not gate.get('accepted') or not gate.get('faces'):
                    continue
                gate = capture_date_for(source, gate)
                if not gate.get('capture_date'):
                    raise RuntimeError('照片缺少可靠拍摄日期，跳过')
                current_stat = source.stat()
                if (stat.st_size, stat.st_mtime_ns) != (current_stat.st_size, current_stat.st_mtime_ns):
                    raise RuntimeError('Source changed during portrait verification')
                material = '%s|%s|%s|%s' % (source, stat.st_size, stat.st_mtime_ns, POLICY)
                key = hashlib.sha256(material.encode()).hexdigest()
                cached = self.store.cached(key)
                if cached:
                    return {'key': key, 'source': str(source), 'file': cached['file'],
                            'gate': json.loads(cached['gate']), 'cache_hit': True, 'navigation': 'next',
                            'prepare_ms': round((time.monotonic()-started)*1000, 1)}
                file = self.render(source, gate, key)
                current_stat = source.stat()
                if (stat.st_size, stat.st_mtime_ns) != (current_stat.st_size, current_stat.st_mtime_ns):
                    Path(file).unlink(missing_ok=True)
                    raise RuntimeError('Source changed during rendering')
                gate = dict(gate, date_stamp_version=STAMP_VERSION,
                            render_sha256=hashlib.sha256(Path(file).read_bytes()).hexdigest())
                logging.info('Portrait approved: %s, faces=%s', source, len(gate['faces']))
                return {'key': key, 'source': str(source), 'file': file,
                        'gate': gate, 'cache_hit': False, 'navigation': 'next',
                        'prepare_ms': round((time.monotonic()-started)*1000, 1)}
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
                logging.exception('Skipping unverified or unreadable photo: %s', source)
                self.store.fail(str(source), 3600)
        cached = self.store.fallback(exclude)
        if not cached:
            cached = self.store.fallback(reserved | ({self.current['source']} if self.current else set()))
        if not cached:
            cached = self.store.fallback(reserved)
        if cached:
            return {'key': cached['key'], 'source': cached['source'], 'file': cached['file'],
                    'gate': json.loads(cached['gate']), 'cache_hit': True, 'navigation': 'next',
                    'prepare_ms': round((time.monotonic()-started)*1000, 1)}
        raise RuntimeError('暂未找到通过人像及非截屏检查的照片；等待 NAS 或下一次重试。')

    def scan_loop(self):
        while not self.stop.is_set():
            try:
                self.store.scan(self.root, self.stop, self.config.get('require_mount', True))
                complete = (self.store.meta('scan') or {}).get('state') == 'complete'
            except Exception:
                logging.exception('NAS scan interrupted')
                complete = False
            until = time.monotonic() + (self.config.get('rescan_seconds', 86400) if complete else 60)
            while not self.stop.is_set() and time.monotonic() < until:
                if self.rescan.wait(min(1, max(0, until-time.monotonic()))):
                    self.rescan.clear()
                    break

    def start_bridge(self):
        raise NotImplementedError('The application must supply a Hub Runtime transport')

    def reserved_sources(self):
        sources = {frame['source'] for frame in self.prepared}
        if self.current:
            sources.add(self.current['source'])
        if self.outstanding:
            sources.add(self.outstanding['frame']['source'])
        return sources

    def discard_prepared(self, frame):
        if not frame or frame.get('cache_hit'):
            return
        file = Path(frame['file'])
        if file.parent.resolve() == self.staging.resolve():
            file.unlink(missing_ok=True)

    def pump_prefetch(self):
        if self.stop.is_set() or not self.ready:
            return
        if self.future is not None and self.future.done():
            done, self.future = self.future, None
            try:
                frame = done.result()
                if frame and frame['source'] not in self.reserved_sources():
                    self.prepared.append(frame)
                    self.prefetch_error = None
                    logging.info('PREFETCH_READY depth=%s prepare_ms=%s source=%s',
                                 len(self.prepared), frame.get('prepare_ms'), frame['source'])
                else:
                    self.discard_prepared(frame)
                    self.prefetch_retry_at = time.monotonic() + 5
            except Exception as error:
                self.prefetch_error = str(error)
                self.prefetch_retry_at = time.monotonic() + 15
                if not self.prepared and not self.current:
                    self.last_error = str(error)
                logging.exception('Could not refill prepared portraits')
        if (self.future is None and len(self.prepared) < self.prefetch_target
                and time.monotonic() >= self.prefetch_retry_at):
            self.future = self.executor.submit(self.prepare_next, self.reserved_sources())
            self.future.add_done_callback(lambda _: self.events.put({'type': 'prefetch_done'}))

    def request(self, action, trigger='key', requested_at=None):
        requested_at = time.monotonic() if requested_at is None else requested_at
        request = {'action': action, 'trigger': trigger, 'requested_at': requested_at}
        if self.outstanding is not None:
            self.pending = request
            return
        # A new immediate intent supersedes an older wait for an empty buffer.
        self.pending = None
        if action == 'previous':
            position = self.cursor - 1
            while position >= 0:
                old = self.history[position]
                if Path(old['file']).is_file():
                    frame = dict(old)
                    if isinstance(frame['gate'], str):
                        frame['gate'] = json.loads(frame['gate'])
                    frame.update(navigation='previous', history_position=position, cache_hit=True)
                    self.send(frame, requested_at=requested_at, trigger=trigger, prefetched=True)
                    return
                position -= 1
            return
        if not self.prepared:
            self.pending = request
            self.prefetch_retry_at = min(self.prefetch_retry_at, time.monotonic())
            return
        frame = self.prepared.popleft()
        try:
            self.send(frame, requested_at=requested_at, trigger=trigger, prefetched=True)
        except (OSError, RuntimeError):
            self.discard_prepared(frame)
            self.pending = request
            logging.exception('Prepared frame failed validation; skipping')

    def send(self, frame, requested_at=None, trigger='key', prefetched=False):
        if not frame or not self.ready:
            return
        with Image.open(frame['file']) as im:
            if im.size != (400, 300):
                raise RuntimeError('只允许发送400×300小图')
        if not frame['gate'].get('accepted') or not frame['gate'].get('faces'):
            raise RuntimeError('人像筛选未通过，禁止发送')
        if (not valid_capture_date(frame['gate'].get('capture_date'))
                or frame['gate'].get('date_stamp_version') != STAMP_VERSION):
            raise RuntimeError('拍摄日期角标校验失败，禁止发送')
        if hashlib.sha256(Path(frame['file']).read_bytes()).hexdigest() != frame['gate'].get('render_sha256'):
            raise RuntimeError('缓存图像校验失败，禁止发送')
        self.request_id += 1
        submitted = time.monotonic()
        requested_at = submitted if requested_at is None else requested_at
        self.outstanding = {'id': self.request_id, 'frame': frame, 'requested_at': requested_at,
                            'submitted_at': submitted, 'trigger': trigger, 'prefetched': prefetched}
        self.bridge.stdin.write(json.dumps({'id': self.request_id, 'command': 'show',
                                           'path': frame['file'], 'format': 'gray16'}) + '\n')
        self.bridge.stdin.flush()
        logging.info('SEND_SUBMITTED id=%s trigger=%s prepared=%s request_to_submit_ms=%.1f queue_remaining=%s',
                     self.request_id, trigger, prefetched, (submitted-requested_at)*1000, len(self.prepared))

    def event(self, event):
        kind = event.get('type')
        if kind == 'ready':
            self.ready = True
            self.last_error = None
            logging.info('Device ready with real palette: %s', event.get('palette'))
            if self.current:
                frame = dict(self.current, navigation='resume', history_position=self.cursor, cache_hit=True)
                try:
                    self.send(frame, trigger='resume', prefetched=True)
                except (OSError, RuntimeError):
                    logging.exception('Cannot resume cached frame; preparing a verified portrait')
                    self.current = None
                    self.request('next', trigger='startup')
            else:
                self.request('next', trigger='startup')
        elif kind == 'key' and event.get('action') == 'Short':
            if event['key'] == 'Down':
                self.request('next')
            elif event['key'] == 'Up':
                self.request('previous')
            elif event['key'] == 'Confirm':
                self.paused = not self.paused
                self.next_due = time.monotonic() + self.config['interval_seconds']
        elif kind == 'display_complete' and self.outstanding and event.get('id') == self.outstanding['id']:
            frame = self.outstanding['frame']
            previous = frame.get('navigation') in {'previous', 'resume'}
            self.store.commit(frame, self.cache, append_history=not previous)
            if previous:
                self.cursor = frame['history_position']
            else:
                self.history = self.history[:self.cursor+1]
                self.history.append(frame.copy())
                self.history = self.history[-100:]
                self.cursor = len(self.history) - 1
            self.current = frame
            self.last_navigation = {
                'trigger': self.outstanding['trigger'], 'prepared': self.outstanding['prefetched'],
                'request_to_submit_ms': round((self.outstanding['submitted_at']-self.outstanding['requested_at'])*1000, 1),
                'sdk_ms': event.get('elapsed_ms'),
                'request_to_complete_ms': round((time.monotonic()-self.outstanding['requested_at'])*1000, 1),
            }
            self.receipts += 1
            self.last_error = None
            if frame.get('navigation') != 'resume':
                self.next_due = time.monotonic() + self.config['interval_seconds']
            self.outstanding = None
            protect = {r['key'] for r in self.history[-20:]}
            protect.update(r['key'] for r in self.prepared)
            if self.future is not None and self.future.done():
                with contextlib.suppress(Exception):
                    candidate = self.future.result()
                    if candidate:
                        protect.add(candidate['key'])
            protect.add(frame['key'])
            self.stats_cache = self.store.trim(self.config['cache_max_bytes'],
                                                self.config['cache_max_files'], protect)
            logging.info('DISPLAY_CONFIRMED id=%s elapsed_ms=%s source=%s cache=%s',
                         event['id'], event.get('elapsed_ms'), frame['source'], frame['file'])
            logging.info('NAVIGATION_TIMING %s', json.dumps(self.last_navigation))
        elif kind in {'fatal', 'bridge_eof', 'display_error', 'disconnected', 'event_error', 'request_error'}:
            logging.error('Device operation stopped: %s', event)
            self.last_error = event
            # A result_unknown must never cause a replay or an automatic new show.
            self.ready = False
            self.stop.set()

    def run(self):
        self.scan_thread = threading.Thread(target=self.scan_loop, daemon=True)
        self.scan_thread.start()
        self.start_bridge()
        last_snapshot = 0
        try:
            while not self.stop.is_set():
                try:
                    self.event(self.events.get(timeout=.2))
                except queue.Empty:
                    pass
                control = read_json(self.data / 'control.json')
                if control and control.get('id') != self.last_control:
                    self.last_control = control.get('id')
                    command = control.get('command')
                    if command == 'stop':
                        self.stop.set()
                    elif command in {'next', 'previous'} and self.ready:
                        self.request(command, trigger='control')
                    elif command == 'pause':
                        self.paused = not self.paused
                        self.next_due = time.monotonic() + self.config['interval_seconds']
                if self.stop.is_set():
                    break
                self.pump_prefetch()
                if (self.pending and self.outstanding is None and self.ready
                        and (self.pending['action'] == 'previous' or self.prepared)):
                    pending, self.pending = self.pending, None
                    self.request(**{'action': pending['action'], 'trigger': pending['trigger'],
                                    'requested_at': pending['requested_at']})
                if (self.ready and not self.paused and self.pending is None and self.outstanding is None
                        and time.monotonic() >= self.next_due):
                    self.request('next', trigger='timer')
                if time.monotonic() - last_snapshot >= 1:
                    self.snapshot()
                    last_snapshot = time.monotonic()
        finally:
            self.stop.set()
            if self.bridge and self.bridge.poll() is None:
                with contextlib.suppress(BrokenPipeError, OSError):
                    self.bridge.stdin.write('{"command":"close","id":0}\n')
                    self.bridge.stdin.flush()
                try:
                    self.bridge.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    self.bridge.terminate()
                    self.bridge.wait(timeout=8)
            self.executor.shutdown(wait=True)
            self.scan_thread.join(timeout=10)
            self.future = None
            self.prepared.clear()
            for file in self.staging.glob('*.png'):
                file.unlink()
            self.ready = False
            self.snapshot()
