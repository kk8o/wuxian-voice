"""voice/player.py：假后端测 顺序、同时只放一句、打断、间隔、stop、排太久的不放、on_start / on_end、音量（不出声音）。"""
import threading
import time
import unittest

import numpy as np

from fakes import FakeBackend

from voice.config import Settings
from voice.player import Clip, Line, SoundPlayer

SR = 1000


def clip(ms, ln=None, amp=0.5, index=0):
    """时长 ms 毫秒；样本数 = ms（SR 1000），用来认是哪一段"""
    return Clip(line=ln or Line(text=str(ms)), audio=np.full(ms, amp, dtype=np.float32), sr=SR, index=index)


class Playing(unittest.TestCase):
    def setUp(self):
        self.backend = FakeBackend()
        self.started, self.ended = [], []
        self.player = SoundPlayer(Settings(), backend=self.backend, on_start=self.started.append,
                                  on_end=lambda c, cut: self.ended.append((c, cut)))
        self.addCleanup(self.player.close)

    def test_plays_in_order_one_at_a_time_and_reports_each(self):
        a, b, c = clip(30), clip(40), clip(50)
        for x in (a, b, c):
            self.player.play(x)
        self.assertTrue(self.player.busy())
        self.assertTrue(self.player.wait(3))
        self.assertEqual(self.backend.names(), [30, 40, 50])
        log = self.backend.log
        self.assertTrue(all(log[i + 1]["start"] >= log[i]["end"] for i in range(2)))   # 不重叠
        self.assertEqual(self.started, [a, b, c])
        self.assertEqual(self.ended, [(a, False), (b, False), (c, False)])
        self.assertAlmostEqual(a.line.seconds, 0.03)

    def test_leaves_a_gap_between_lines_and_a_short_one_between_parts(self):
        ln = Line(text="x", parts=2)
        for x in (clip(20), clip(21), clip(22, ln), clip(23, ln, index=1)):
            self.player.play(x)
        self.assertTrue(self.player.wait(3))
        log = self.backend.log
        gaps = [log[i + 1]["start"] - log[i]["end"] for i in range(3)]
        self.assertGreaterEqual(gaps[0], 0.14)
        self.assertGreaterEqual(gaps[1], 0.14)
        self.assertGreaterEqual(gaps[2], 0.04)
        self.assertLess(gaps[2], 0.14)

    def test_interrupt_stops_the_current_line_drops_the_queue_and_plays_at_once(self):
        long, queued, urgent = clip(2000), clip(31), clip(32)
        self.player.play(long)
        self.player.play(queued)
        time.sleep(0.1)
        t = time.monotonic()
        self.player.play(urgent, interrupt=True)
        self.assertTrue(self.player.wait(3))
        self.assertEqual(self.backend.names(), [2000, 32])
        first, second = self.backend.log
        self.assertTrue(first["cut"])
        self.assertLess(second["start"] - t, 0.1)                 # 不等间隔
        self.assertTrue(long.line.cancelled and queued.line.cancelled)
        self.assertEqual(self.ended, [(long, True), (urgent, False)])

    def test_the_rest_of_a_cut_line_is_refused(self):
        ln = Line(text="两句", parts=2)
        self.player.play(clip(2000, ln))
        time.sleep(0.05)
        self.player.play(clip(33), interrupt=True)
        self.assertFalse(self.player.play(clip(34, ln, index=1)))   # 后半句合成好时已被打断
        self.assertTrue(self.player.wait(3))
        self.assertEqual(self.backend.names(), [2000, 33])

    def test_stop_cuts_the_current_line_and_clears_the_queue(self):
        self.player.play(clip(2000))
        self.player.play(clip(33))
        time.sleep(0.1)
        self.assertEqual(self.player.stop(), 2)
        self.assertTrue(self.player.wait(1))
        self.assertEqual(self.backend.names(), [2000])
        self.player.play(clip(34))                                # 停了还能接着放
        self.assertTrue(self.player.wait(3))
        self.assertEqual(self.backend.names(), [2000, 34])

    def test_current_and_queued(self):
        a, b = clip(500), clip(35)
        self.player.play(a)
        self.player.play(b)
        time.sleep(0.05)
        self.assertIs(self.player.current, a)
        self.assertEqual(self.player.queued(), [b.line])
        self.player.stop()

    def test_a_line_waiting_past_its_deadline_is_dropped(self):
        self.player.play(clip(300))
        late = Line(text="晚了", deadline=time.monotonic() + 0.1)
        self.player.play(clip(36, late))
        self.player.play(clip(37))
        self.assertTrue(self.player.wait(3))
        self.assertEqual(self.backend.names(), [300, 37])
        self.assertTrue(late.cancelled)

    def test_failing_callbacks_do_not_stop_playback(self):
        player = SoundPlayer(Settings(), backend=self.backend, on_start=lambda c: 1 / 0, on_end=lambda c, cut: 1 / 0)
        self.addCleanup(player.close)
        with self.assertLogs("voice.player", "ERROR"):
            player.play(clip(20))
            player.play(clip(21))
            self.assertTrue(player.wait(3))
        self.assertEqual(self.backend.names(), [20, 21])

    def test_loudness_evens_out_quiet_and_loud_lines(self):
        player = SoundPlayer(Settings(loudness=0.9), backend=self.backend)
        self.addCleanup(player.close)
        for x in (clip(30, amp=0.3), clip(31, amp=0.6), clip(32, amp=0.01)):   # 很轻的：最多放大 4 倍
            player.play(x)
        self.assertTrue(player.wait(5))
        peaks = [x["peak"] for x in self.backend.log]
        self.assertAlmostEqual(peaks[0], 0.9, places=4)
        self.assertAlmostEqual(peaks[1], 0.9, places=4)
        self.assertAlmostEqual(peaks[2], 0.04, places=4)

    def test_volume_is_read_live(self):
        s = Settings(volume=0.5, loudness=0)
        player = SoundPlayer(s, backend=self.backend)
        self.addCleanup(player.close)
        player.play(clip(20, amp=0.8))
        self.assertTrue(player.wait(3))
        s.volume = 0.0
        player.play(clip(21, amp=0.8))
        self.assertTrue(player.wait(3))
        self.assertAlmostEqual(self.backend.log[0]["peak"], 0.4, places=5)
        self.assertAlmostEqual(self.backend.log[1]["peak"], 0.0, places=5)


if __name__ == "__main__":
    unittest.main()
