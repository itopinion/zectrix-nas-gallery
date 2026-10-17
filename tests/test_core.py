"""Behavioral regression suites from the first standalone portrait player."""
from pathlib import Path
import runpy
import unittest


class CoreRegressionTests(unittest.TestCase):
    def run_check(self, name):
        runpy.run_path(str(Path(__file__).parent / 'checks' / (name + '.py')))

    def test_date_metadata_and_corner_stamp(self):
        self.run_check('date_stamp')

    def test_receipts_cache_timer_and_unknown_result(self):
        self.run_check('nas_player')

    def test_bounded_prefetch_and_foreground_navigation(self):
        self.run_check('prefetch')
