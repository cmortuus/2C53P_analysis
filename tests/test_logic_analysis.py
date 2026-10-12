"""Tests for host/logic_analysis.py stats on synthetic captures (numpy, no hardware)."""
import os, sys, unittest
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "host"))
import numpy as np
import logic_analysis as LA


class Stats(unittest.TestCase):
    def test_frequency_duty_glitch(self):
        rate = 1e6
        n = 100_000
        t = np.arange(n)
        d0 = ((t % 100) < 30).astype(np.uint8)            # 10 kHz, 30 % duty on D0
        d0[5060:5062] = 1                                   # a 2-sample runt high inside a low
        data = d0 | (np.uint8(1) << 3) * 0                  # D3 constant low
        st = LA.channel_stats(data.astype(np.uint8), rate)
        self.assertAlmostEqual(st["D0"]["freq_hz"], 1e4, delta=1)
        self.assertAlmostEqual(st["D0"]["duty"], 0.30, delta=0.01)
        self.assertGreaterEqual(st["D0"]["high_glitches"] + st["D0"]["low_glitches"], 1)
        self.assertEqual(st["D3"]["state"], "low")

    def test_step_dir(self):
        rate = 1e6
        n = 200_000
        t = np.arange(n)
        step = (((t + 40) % 50) < 10).astype(np.uint8)     # 20 kHz steps, starts low
        dirb = (t < 100_000).astype(np.uint8)              # + for the first half
        data = step | (dirb << 1)
        sd = LA.step_dir(data.astype(np.uint8), rate, step=0, dirc=1)
        self.assertEqual(sd["steps"], 4000)
        self.assertEqual(sd["plus"], 2000)
        self.assertEqual(sd["net"], 0)
        self.assertAlmostEqual(sd["median_step_rate_hz"], 2e4, delta=1)
        self.assertEqual(sd["dir_changes"], 1)

    def test_rate_parse(self):
        self.assertEqual(LA.parse_rate("4M"), 4e6)
        self.assertEqual(LA.parse_rate("500k"), 5e5)
        self.assertEqual(LA.rate_arg(24e6), "24M")


if __name__ == "__main__":
    unittest.main()
