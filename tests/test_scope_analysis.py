"""Regression tests for host/scope_analysis.py on synthetic records (no hardware).

    .venv/bin/python -I -m unittest discover -s tests -v
"""
import math, os, random, sys, unittest
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "host"))
import scope_analysis as SA


def square(n, per, duty=0.5, ph=0.0, hi=210, lo=50):
    return [hi if ((i + ph) / per) % 1 < duty else lo for i in range(n)]


def ring_with_junk(s, r):
    """Hardware model: a ring buffer rotated by r with a 24-sample junk block at the wrap."""
    return bytes(s[r:] + [210] * 15 + [50] * 9 + s[:r])


class Levels(unittest.TestCase):
    def test_plateaus_subcount_and_overshoot_robust(self):
        rnd = random.Random(1)
        raw = [(138 if (i // 25) % 2 else 122) + rnd.choice((-1, 0, 1)) for i in range(1024)]
        for i in range(0, 1024, 50):                     # overshoot spikes on every rising edge
            raw[i] = 160
        top, base = SA.plateau_levels(bytes(raw))
        self.assertAlmostEqual(top, 138, delta=0.3)
        self.assertAlmostEqual(base, 122, delta=0.3)

    def test_levels_volts(self):
        lv = SA.levels(bytes(square(1024, 100)), vpc=0.02, zero=0.0)
        self.assertAlmostEqual(lv["vtop"], (210 - 128) * 0.02, places=6)
        self.assertAlmostEqual(lv["vamp"], 160 * 0.02, places=6)


class Frequency(unittest.TestCase):
    def test_square_and_sine_with_junk(self):
        rnd = random.Random(2)
        for _ in range(40):
            per = rnd.uniform(20, 200)
            s = square(1000, per, 0.5, rnd.random() * per)
            f = SA.frequency(SA.clean_record(ring_with_junk(s, rnd.randint(0, 999))), 1e5)
            self.assertAlmostEqual(f / (1e5 / per), 1.0, delta=0.01)
        sine = bytes(int(128 + 60 * math.sin(2 * math.pi * i / 73.3)) for i in range(1024))
        self.assertAlmostEqual(SA.frequency(sine, 1e5) / (1e5 / 73.3), 1.0, delta=0.005)

    def test_flat_has_no_frequency(self):
        self.assertIsNone(SA.frequency(bytes([128] * 1024), 1e5))


class Junk(unittest.TestCase):
    def test_no_false_glitches_after_cleaning(self):
        rnd = random.Random(3)
        for _ in range(200):
            per = rnd.choice([50, 62.5, 125, 250]); duty = rnd.choice([0.5, 0.25, 0.15])
            s = square(1000, per, duty, rnd.random() * per)
            pm = SA.pulse_metrics(SA.clean_record(ring_with_junk(s, rnd.randint(0, 999))), 125e3)
            self.assertIsNotNone(pm)
            self.assertEqual(pm["glitches"], 0)

    def test_real_glitch_survives(self):
        rnd = random.Random(4)
        found = 0
        for _ in range(50):
            s = square(1000, 125); gp = rnd.randint(100, 800)
            s[gp:gp + 4] = [50 if s[gp] == 210 else 210] * 4
            pm = SA.pulse_metrics(SA.clean_record(ring_with_junk(s, rnd.randint(0, 999))), 125e3)
            found += pm["glitches"] >= 1
        self.assertGreaterEqual(found, 45)


class Pulses(unittest.TestCase):
    def test_duty_and_width_at_mid_level(self):
        # 25 % duty with 2-sample linear edges: mid-level widths must not be biased
        n, per = 1024, 100.0
        raw = []
        for i in range(n):
            ph = (i % per)
            v = 50 if ph >= 27 else 210
            if ph < 2: v = 50 + (210 - 50) * ph / 2         # rising ramp
            elif 25 <= ph < 27: v = 210 - (210 - 50) * (ph - 25) / 2
            raw.append(int(v))
        pm = SA.pulse_metrics(bytes(raw), 1e6)
        self.assertAlmostEqual(pm["high_samples"], 25.0, delta=0.6)
        self.assertAlmostEqual(pm["duty"], 0.25, delta=0.01)


class Decoders(unittest.TestCase):
    def test_steps(self):
        rate = 250e3
        step = bytes(200 if (i * 10e3 / rate) % 1 < 0.3 else 60 for i in range(1024))
        d = bytes(200 if i < 512 else 60 for i in range(1024))
        r = SA.decode_steps(step, d, rate)
        self.assertAlmostEqual(r["step_rate_hz"], 1e4, delta=20)
        self.assertEqual(r["plus"] + r["minus"], r["steps"])

    def test_uart(self):
        rate, baud = 125e3, 9600
        spb = rate / baud; lvl = [1] * 30
        for ch in b"OK\n":
            for bt in [0] + [(ch >> b) & 1 for b in range(8)] + [1]:
                lvl += [bt] * int(round(spb))
        lvl += [1] * (1024 - len(lvl))
        r = SA.decode_uart(bytes(200 if x else 60 for x in lvl[:1024]), rate, baud)
        self.assertEqual(bytes(r["bytes"]), b"OK\n")
        self.assertEqual(r["framing_errors"], 0)


class Compare(unittest.TestCase):
    def test_same_signal_different_rotation(self):
        s = square(1000, 125)
        a, b = ring_with_junk(s, 137), ring_with_junk(s, 700)
        r = SA.compare_to_reference(a, b, 125e3, 0.02)
        self.assertLess(r["rms_diff"], 0.05)


if __name__ == "__main__":
    unittest.main()
