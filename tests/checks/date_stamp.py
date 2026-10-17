"""Offline capture-date and small-frame watermark regression checks."""
import copy
import importlib.util
import os
from pathlib import Path
import tempfile

from PIL import Image, ImageChops

SOURCE = Path(__file__).resolve().parents[2] / 'app' / 'player.py'
spec = importlib.util.spec_from_file_location('nas_player_date_test', SOURCE)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def raw_exif_gate(value):
    # Swift/ImageIO reads EXIF DateTimeOriginal natively across input formats and
    # emits a canonical date. Python validates/copies this inspector result.
    return {'accepted': True, 'faces': [{'x': .2, 'y': .2, 'width': .3, 'height': .3}],
            'capture_date': value.split(' ', 1)[0].replace(':', '-'),
            'capture_date_source': 'DateTimeOriginal', 'capture_date_status': 'known'}


with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)

    def photo(name):
        path = root / name
        Image.new('RGB', (400, 300), (95, 125, 145)).save(path)
        # A very different file modification date must never become capture date.
        os.utime(path, (1893456000, 1893456000))  # 2030-01-01 UTC
        return path

    conflict = photo('photo_20240229.jpg')
    original_gate = raw_exif_gate('2019:07:08 10:11:12')
    before = copy.deepcopy(original_gate)
    resolved = module.capture_date_for(conflict, original_gate)
    assert resolved['capture_date'] == '2019-07-08', 'valid EXIF date must win over filename'
    assert original_gate == before, 'date canonicalization must not mutate shared portrait-gate metadata'
    assert resolved is not original_gate, 'date result must be a copied gate'

    for filename, expected in [('IMG_20240229_120000.jpg', '2024-02-29'),
                               ('photo_2024-02-29_120000.jpg', '2024-02-29'),
                               ('photo_20181105.jpg', '2018-11-05')]:
        result = module.capture_date_for(photo(filename), {})
        assert result['capture_date'] == expected, 'explicit filename date fallback failed: ' + filename

    valid_exif = module.capture_date_for(photo('no_filename_date.jpg'), raw_exif_gate('2024:02:29 12:00:00'))
    assert valid_exif['capture_date'] == '2024-02-29'

    for filename, gate in [('IMG_1234.jpg', {}),
                           ('photo_20230229.jpg', {}),
                           ('photo_2024-13-01.jpg', {}),
                           ('photo_2020-01-01_2021-02-02.jpg', {}),
                           ('no_date.jpg', raw_exif_gate('0000:00:00 00:00:00')),
                           ('invalid_original.jpg', raw_exif_gate('2023:02:29 12:00:00'))]:
        result = module.capture_date_for(photo(filename), gate)
        assert result.get('capture_date') in (None, ''), 'invalid/missing date must not invent capture date or use mtime'

    image = Image.new('RGB', (400, 300), (95, 125, 145))
    untouched = image.copy()
    stamped = module.stamp_capture_date(image, '2020-01-02')
    assert stamped is not image and stamped.size == (400, 300), 'watermark must produce a separate400×300 frame'
    assert ImageChops.difference(image, untouched).getbbox() is None, 'stamp must not mutate the caller image'
    bounds = ImageChops.difference(stamped.convert('RGB'), untouched).getbbox()
    assert bounds is not None, 'capture date must visibly change the frame'
    assert bounds[0] >= 200 and bounds[1] >= 225, 'watermark must stay in the lower-right corner: ' + str(bounds)
    assert bounds[2] <= 400 and bounds[3] <= 300
    assert ImageChops.difference(stamped.crop((0, 0, 400, 220)).convert('RGB'),
                                untouched.crop((0, 0, 400, 220))).getbbox() is None
    assert module.STAMP_VERSION == 'capture-date-bottom-right-v1'
    assert module.POLICY != 'camera-face-075-cover-v1', 'new cached stamp policy must invalidate unstamped old frames'

print('PASS: EXIF preference, copied date metadata, validated leap days and filename fallback,')
print('      no filesystem-date invention, lower-right-only stamp, unchanged400×300 and cache policy version.')
