"""Offline behavioral checks for bounded portrait prefetch; never opens a device."""
import collections
import concurrent.futures
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import time

from PIL import Image

SOURCE = Path(__file__).resolve().parents[2] / 'app' / 'player.py'
spec = importlib.util.spec_from_file_location('nas_player_prefetch_test', SOURCE)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class Input:
    def __init__(self):
        self.messages = []

    def write(self, line):
        self.messages.append(json.loads(line))

    def flush(self):
        pass


class Bridge:
    def __init__(self):
        self.stdin = Input()


def make_player(root, **extra):
    config = dict(data_dir=str(root / 'data'), cache_dir=str(root / 'cache'),
                  photo_root=str(root / 'unmounted-folder'), seed_index=str(root / 'missing-index'),
                  interval_seconds=300, rescan_seconds=86400, cache_max_bytes=200*1024*1024,
                  cache_max_files=1000, require_mount=True, prefetch_frames=6)
    config.update(extra)
    player = module.Player(config)
    player.bridge = Bridge()
    player.ready = True
    return player


def frame(player, number, source=None):
    file = player.staging / ('%s.png' % number)
    Image.new('RGB', (400, 300), (number % 255, 80, 120)).save(file)
    return {'key': str(number), 'source': source or '/NAS/%s.jpg' % number, 'file': str(file),
            'gate': {'accepted': True,
                     'faces': [{'x': .2, 'y': .2, 'width': .3, 'height': .3}],
                     'capture_date': '2020-01-02',
                     'date_stamp_version': 'capture-date-bottom-right-v1',
                     'render_sha256': hashlib.sha256(file.read_bytes()).hexdigest()},
            'cache_hit': False, 'navigation': 'next'}


def ack(player):
    assert player.outstanding
    player.event({'type': 'display_complete', 'id': player.outstanding['id'], 'elapsed_ms': 2,
                  'device_reported': True})


def finish(player):
    player.stop.set()
    if player.future is not None and not player.future.done():
        player.future.cancel()
    player.executor.shutdown(wait=True, cancel_futures=True)


with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)

    # A ready image and cached history are foreground work. A slow producer must
    # not delay either one, even though its future remains in progress.
    player = make_player(root / 'foreground')
    producer = concurrent.futures.Future()
    player.future = producer
    next_frame = frame(player, 1)
    player.prepared.append(next_frame)
    player.request('next', trigger='key', requested_at=time.monotonic())
    assert len(player.bridge.stdin.messages) == 1, 'ready next must bypass background producer'
    assert player.outstanding['frame']['key'] == '1'
    assert not list(player.cache.glob('*.png')), 'prefetch/send must not permanently cache before receipt'
    player.event({'type': 'display_complete', 'id': 999})
    assert not list(player.cache.glob('*.png')), 'wrong receipt must not commit prefetched frame'
    ack(player)
    assert len(list(player.cache.glob('*.png'))) == 1
    second = frame(player, 2)
    player.prepared.append(second)
    player.request('next')
    ack(player)
    # A next intent waiting for preparation must be superseded by a newer Up.
    # Otherwise the old next would dispatch just after the previous-frame ack.
    player.request('next')
    assert player.pending['action'] == 'next'
    messages = len(player.bridge.stdin.messages)
    player.request('previous')
    assert len(player.bridge.stdin.messages) == messages + 1, 'cached previous must bypass background producer'
    assert player.outstanding['frame']['key'] == '1'
    assert player.pending is None, 'new Up must discard an older empty-buffer next intent'
    ack(player)
    producer.cancel()
    player.future = None
    finish(player)

    # A paused player may prepare at most six unique small images. Preparation
    # itself must never send or create a permanent-cache file.
    player = make_player(root / 'bounded')
    player.paused = True
    requested_exclusions = []
    sequence = [10]

    def prepare(extra_exclude=None):
        requested_exclusions.append(set(extra_exclude or ()))
        sequence[0] += 1
        return frame(player, sequence[0])

    player.prepare_next = prepare
    deadline = time.monotonic() + 4
    while len(player.prepared) < player.prefetch_target and time.monotonic() < deadline:
        player.pump_prefetch()
        time.sleep(.005)
    # Let a completed producer, if any, be incorporated before checking cap.
    for _ in range(5):
        player.pump_prefetch()
        time.sleep(.005)
    assert len(player.prepared) == 6, 'prefetch must fill configured bounded queue'
    assert player.future is None, 'full queue must not launch an unnecessary producer'
    assert len({r['source'] for r in player.prepared}) == 6
    for number, excluded in enumerate(requested_exclusions):
        earlier_sources = {'/NAS/%s.jpg' % (11+i) for i in range(number)}
        assert earlier_sources <= excluded, 'producer must exclude already prepared sources'
    assert not player.bridge.stdin.messages, 'background refill/paused pump must never display spontaneously'
    assert not list(player.cache.glob('*.png')), 'unsent prepared frames are staging only'
    for item in player.prepared:
        with Image.open(item['file']) as image:
            assert image.size == (400, 300), 'only small prepared frames may persist'

    player.request('next')
    player.request('next')
    player.request('previous')
    assert player.pending['action'] == 'previous', 'busy device must retain only latest navigation intent'
    messages = len(player.bridge.stdin.messages)
    player.event({'type': 'display_error', 'id': player.outstanding['id'],
                  'error_kind': 'result_unknown'})
    assert player.stop.is_set() and not player.ready
    assert len(player.bridge.stdin.messages) == messages, 'unknown result must not dispatch/replay buffered frames'
    assert not list(player.cache.glob('*.png')), 'failed/unknown sends must not enter permanent cache'
    finish(player)

    # A prepared cache hit must stay protected when a different newly displayed
    # image triggers LRU trimming. This catches a subtle queue→deleted file race.
    player = make_player(root / 'protection', cache_max_files=2)
    first = frame(player, 21)
    queued = frame(player, 22)
    for item in (first, queued):
        player.store.commit(item, player.cache, append_history=False)
    player.prepared.append(queued.copy())
    newest = frame(player, 23)
    player.send(newest)
    ack(player)
    assert Path(queued['file']).is_file(), 'LRU must protect prepared cache-hit files'
    assert player.store.cached(queued['key']), 'prepared cache-hit DB row must remain valid'
    assert not Path(first['file']).exists(), 'old unprotected cache should be evicted instead'
    assert len(list(player.cache.glob('*.png'))) == 2
    finish(player)

print('PASS: bounded6 prefetch, unique source exclusions, no spontaneous/paused display,')
print('      foreground next/history bypass producer, matched ack before cache,')
print('      latest pending action, no unknown replay, ready cache files protected from LRU.')
