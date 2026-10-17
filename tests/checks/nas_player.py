import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import threading
import time
from PIL import Image

path = Path(__file__).resolve().parents[2] / 'app' / 'player.py'
spec = importlib.util.spec_from_file_location('nas_player', path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class Input:
    def __init__(self, owner):
        self.owner = owner
        self.messages = []

    def write(self, value):
        command = json.loads(value)
        self.messages.append(command)
        if command['command'] == 'show' and self.owner.automatic:
            self.owner.player.events.put({'type': 'display_complete', 'id': command['id'],
                                         'elapsed_ms': 1, 'device_reported': True})
        elif command['command'] == 'close':
            self.owner.closed = True

    def flush(self):
        pass


class Bridge:
    def __init__(self, player, automatic=False):
        self.player = player
        self.automatic = automatic
        self.stdin = Input(self)
        self.closed = False

    def poll(self):
        return 0 if self.closed else None

    def wait(self, timeout=None):
        return 0


with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    config = dict(data_dir=str(root/'data'), cache_dir=str(root/'cache'),
                  photo_root=str(root/'unmounted-folder'), seed_index=str(root/'missing-index'),
                  interval_seconds=.3, rescan_seconds=86400, cache_max_bytes=200*1024*1024,
                  cache_max_files=1000, require_mount=True)
    player = module.Player(config)
    player.bridge = Bridge(player)
    player.ready = True

    def frame(number):
        file = player.staging / ('%s.png' % number)
        Image.new('RGB', (400, 300), (number % 255, 80, 120)).save(file)
        return {'key': str(number), 'source': '/NAS/%s.jpg' % number, 'file': str(file),
                'gate': {'accepted': True, 'faces': [{'x': .2, 'y': .2, 'width': .3, 'height': .3}],
                         'capture_date': '2020-01-02',
                         'date_stamp_version': 'capture-date-bottom-right-v1',
                         'render_sha256': hashlib.sha256(file.read_bytes()).hexdigest()},
                'cache_hit': False, 'navigation': 'next'}

    first = frame(1)
    player.send(first)
    assert not list(player.cache.glob('*.png')), 'must not cache before successful receipt'
    player.event({'type': 'display_complete', 'id': 999})
    assert not list(player.cache.glob('*.png')), 'must reject unmatched receipt'
    player.event({'type': 'display_complete', 'id': 1, 'elapsed_ms': 1})
    assert len(list(player.cache.glob('*.png'))) == 1
    assert player.store.fallback(set())
    assert player.prepare_next()['cache_hit'], 'one cached frame must work with NAS offline'

    for which in ['no_face', 'bad_size', 'corrupt', 'missing_stamp', 'missing_date']:
        invalid = frame(2)
        if which == 'no_face':
            invalid['gate']['accepted'] = False
        elif which == 'bad_size':
            Image.new('RGB', (401, 300)).save(invalid['file'])
        elif which == 'corrupt':
            invalid['gate']['render_sha256'] = 'wrong'
        elif which == 'missing_stamp':
            del invalid['gate']['date_stamp_version']
        else:
            del invalid['gate']['capture_date']
        try:
            player.send(invalid)
        except RuntimeError:
            pass
        else:
            raise AssertionError('unsafe frame accepted: ' + which)

    second = frame(3)
    player.send(second)
    player.request('next')
    player.request('previous')
    assert player.pending['action'] == 'previous', 'pending keys should coalesce'
    messages = len(player.bridge.stdin.messages)
    player.event({'type': 'display_error', 'id': 2, 'error_kind': 'result_unknown'})
    assert player.stop.is_set() and not player.ready
    assert len(player.bridge.stdin.messages) == messages, 'unknown result must not replay'

    unmounted = Path(config['photo_root'])
    unmounted.mkdir()
    player.store.add([('/NAS/kept.jpg', '.jpg', 'seed')])
    count = player.store.stats()['catalog']
    player.store.scan(unmounted, threading.Event(), require_mount=True)
    assert player.store.stats()['catalog'] == count, 'unmounted directory must not prune catalog'
    player.executor.shutdown()

    # Exercise the actual controller event loop with a short test-only interval.
    config['data_dir'] = str(root/'timer-data')
    config['cache_dir'] = str(root/'timer-cache')
    timed = module.Player(config)
    number = [10]
    def prepare(extra_exclude=None):
        number[0] += 1
        file = timed.staging / ('%s.png' % number[0])
        Image.new('RGB', (400, 300), (number[0], 90, 120)).save(file)
        return {'key': str(number[0]), 'source': '/test/%s.jpg' % number[0], 'file': str(file),
                'gate': {'accepted': True, 'faces': [{'x': .2, 'y': .2, 'width': .3, 'height': .3}],
                         'capture_date': '2020-01-02',
                         'date_stamp_version': 'capture-date-bottom-right-v1',
                         'render_sha256': hashlib.sha256(file.read_bytes()).hexdigest()},
                'cache_hit': False, 'navigation': 'next'}
    timed.prepare_next = prepare
    def start():
        timed.bridge = Bridge(timed, automatic=True)
        timed.events.put({'type': 'ready'})
    timed.start_bridge = start
    thread = threading.Thread(target=timed.run)
    thread.start()
    deadline = time.monotonic()+5
    while timed.receipts < 2 and time.monotonic() < deadline:
        time.sleep(.05)
    assert timed.receipts >= 2, 'automatic timer must advance without a key'
    before = timed.receipts
    timed.events.put({'type': 'key', 'key': 'Down', 'action': 'Short'})
    deadline = time.monotonic()+3
    while timed.receipts <= before and time.monotonic() < deadline:
        time.sleep(.05)
    assert timed.receipts > before, 'Down key must request a photo'
    timed.stop.set()
    thread.join(5)
    assert not thread.is_alive()
    for file in timed.cache.glob('*.png'):
        with Image.open(file) as im:
            assert im.size == (400, 300)

print('PASS: portrait gate enforcement, size/hash integrity, matched receipt before cache, offline fallback,')
print('      latest pending key, no replay of unknown result, unmounted catalog retention, timer, Down key, shutdown.')
