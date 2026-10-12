"""Regression tests for host/cnc_checks.py on synthetic records (no hardware).

    .venv/bin/python -I -m unittest discover -s tests -v
"""
import os, random, sys, unittest
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "host"))
import cnc_checks as CC

HI, LO = 210, 50
RATE = 5e6                                   # 0.2 us per sample


def step_train(n, starts, width, active_high=True):
    """Pulses of `width` samples starting at each index in `starts`."""
    a, b = (HI, LO) if active_high else (LO, HI)
    x = [b] * n
    for s in starts:
        for i in range(s, min(n, s + width)):
            x[i] = a
    return bytes(x)


def level_steps(n, changes, start_high=False):
    x, st = [], start_high
    ch = set(changes)
    for i in range(n):
        if i in ch:
            st = not st
        x.append(HI if st else LO)
    return bytes(x)


class StepDir(unittest.TestCase):
    def test_widths_rate_setup_hold(self):
        starts = list(range(20, 1000, 100))                    # 100 samples = 20 us = 50 kHz
        step = step_train(1024, starts, 15)                    # 15 samples = 3 us pulses
        dirl = level_steps(1024, [420 - 25])                   # DIR flips 25 samples (5 us) before the step at 420,
        r = CC.step_dir([(step, dirl)], RATE, "rising",        # 60 samples after the step at 320 (+15 width -> hold 75)
                        {"min_pulse": 2.5e-6, "min_space": 2.5e-6, "min_setup": 1e-6, "min_hold": 1e-6, "max_rate": 100e3})
        m = r["metrics"]
        self.assertEqual(m["steps"], 10)
        self.assertAlmostEqual(m["pulse_min_s"], 3e-6, delta=0.25e-6)
        self.assertAlmostEqual(m["rate_max_hz"], 50e3, delta=600)
        self.assertAlmostEqual(m["setup_min_s"], 5e-6, delta=0.25e-6)
        self.assertAlmostEqual(m["hold_min_s"], 75 / RATE, delta=0.25e-6)
        self.assertEqual(r["verdict"], CC.PASS, [CC.fmt_check(c) for c in r["checks"]])

    def test_too_short_pulse_fails_and_close_is_marginal(self):
        step = step_train(1024, list(range(20, 1000, 100)), 5)    # 1 us
        r = CC.step_dir([(step, None)], RATE, "rising", {"min_pulse": 2.5e-6})
        self.assertEqual(r["checks"][0]["result"], CC.FAIL)
        r = CC.step_dir([(step, None)], RATE, "rising", {"min_pulse": 1.0e-6})
        self.assertEqual(r["checks"][0]["result"], CC.MARGINAL)

    def test_active_low(self):
        step = step_train(1024, list(range(20, 1000, 100)), 15, active_high=False)
        r = CC.step_dir([(step, None)], RATE, "falling", {"min_pulse": 2.5e-6})
        self.assertAlmostEqual(r["metrics"]["pulse_min_s"], 3e-6, delta=0.25e-6)
        self.assertEqual(r["verdict"], CC.PASS)

    def test_no_steps(self):
        r = CC.step_dir([(bytes([LO] * 1024), None)], RATE)
        self.assertEqual(r["verdict"], CC.FAIL)


class Encoder(unittest.TestCase):
    def quad(self, per, shift, n=1024):
        a = bytes(HI if (i % per) < per / 2 else LO for i in range(n))
        b = bytes(HI if ((i - shift) % per) < per / 2 else LO for i in range(n))
        return a, b

    def test_healthy_quadrature(self):
        a, b = self.quad(100, 25)
        r = CC.encoder(a, b, RATE, lines=1000)
        self.assertEqual(r["verdict"], CC.PASS, [CC.fmt_check(c) for c in r["checks"]])
        self.assertAlmostEqual(abs(r["metrics"]["phase_deg"]), 90, delta=4)
        self.assertAlmostEqual(r["metrics"]["rpm"], 50e3 / 1000 * 60, delta=50)

    def test_edges_too_close(self):
        a, b = self.quad(100, 1)                                 # B one sample behind A: no quadrature
        r = CC.encoder(a, b, RATE)
        self.assertEqual(r["verdict"], CC.FAIL)


class Levels(unittest.TestCase):
    VPC = 0.04                                                    # 40 mV per count

    def volts(self, v):
        return int(round(128 + v / self.VPC))

    def test_clean_3v3_passes_noisy_fails(self):
        hi, lo = self.volts(3.2), self.volts(0.0)
        clean = bytes(hi if (i // 50) % 2 else lo for i in range(1024))
        self.assertEqual(CC.logic_levels(clean, self.VPC, 0, *CC.FAMILIES["3v3"])["verdict"], CC.PASS)
        noisy = bytearray(clean)
        noisy[75] = self.volts(1.6)                               # a dip in the middle of a high plateau
        r = CC.logic_levels(bytes(noisy), self.VPC, 0, *CC.FAMILIES["3v3"])
        self.assertEqual(r["verdict"], CC.FAIL)
        self.assertLess(r["metrics"]["noise_margin_high_v"], 0)

    def test_static_undefined(self):
        r = CC.logic_levels(bytes([self.volts(1.4)] * 1024), self.VPC, 0, *CC.FAMILIES["3v3"])
        self.assertEqual(r["verdict"], CC.FAIL)


class Rail(unittest.TestCase):
    VPC = 0.05                                                    # 5 V sits at count 228

    def rec(self, v, ripple=0.04, sag=0):
        raw = [int(round(128 + (v + ripple * ((i // 20) % 2)) / self.VPC)) for i in range(1024)]
        for i in range(500, 500 + sag):                       # a dropout lasting `sag` samples
            raw[i] = int(round(128 + (v - 1.5) / self.VPC))
        return {"raw": bytes(raw), "rate": 50e3, "vpc": self.VPC, "zero": 0, "rng": 9}

    def test_clean_rail_passes(self):
        ok = CC.rail([self.rec(5.0) for _ in range(5)], nominal=5.0, ripple_max=0.15)
        self.assertEqual(ok["verdict"], CC.PASS, [CC.fmt_check(c) for c in ok["checks"]])

    def test_sag_fails_but_single_sample_spike_does_not(self):
        bad = CC.rail([self.rec(5.0) for _ in range(4)] + [self.rec(5.0, sag=20)], nominal=5.0, ripple_max=2.0)
        self.assertEqual(bad["checks"][1]["result"], CC.FAIL)
        spike = CC.rail([self.rec(5.0, sag=1)], nominal=5.0, ripple_max=2.0)
        self.assertEqual(spike["checks"][1]["result"], CC.PASS)

    def test_ripple_above_noise_fails(self):
        r = CC.rail([self.rec(5.0, ripple=0.5)], nominal=5.0, ripple_max=0.1)
        self.assertEqual(r["checks"][2]["result"], CC.FAIL)

    def test_limit_below_noise_floor_is_marginal(self):
        r = CC.rail([self.rec(5.0, ripple=0.02)], nominal=5.0, ripple_max=0.03)
        self.assertEqual(r["checks"][2]["result"], CC.MARGINAL)


class Bounce(unittest.TestCase):
    def test_burst(self):
        x = [LO] * 1024
        for s, w in ((100, 10), (120, 5), (140, 30)):
            for i in range(s, s + w):
                x[i] = HI
        for i in range(200, 1024):
            x[i] = HI
        r = CC.bounce(bytes(x), 50e3, debounce=0.005)
        m = r["metrics"]
        self.assertEqual(m["transitions"], 7)
        self.assertEqual(m["final"], "high")
        self.assertAlmostEqual(m["bounce_s"], 100 / 50e3, delta=2 / 50e3)
        self.assertEqual(r["verdict"], CC.PASS)


class TriggeredRecord(unittest.TestCase):
    def test_unrotate_bounce_any_seam(self):
        ordered = [LO] * 1024
        for s, w in ((512, 15), (535, 8), (550, 30), (600, 5), (612, 45), (680, 10)):
            for i in range(s, s + w):
                ordered[i] = HI
        for i in range(700, 1024):
            ordered[i] = HI
        for seam in (0, 37, 300, 511, 622, 900, 1000):
            raw = bytes(ordered[1024 - seam:] + ordered[:1024 - seam])   # firmware layout: seam at `seam`
            u, k = CC.unrotate_triggered(raw, rising=True)
            r = CC.bounce(u, 50e3)
            self.assertEqual(r["metrics"]["final"], "high", seam)
            self.assertEqual(r["metrics"]["transitions"], 13, seam)
            self.assertAlmostEqual(r["metrics"]["first_s"] * 50e3, 512, delta=1)


if __name__ == "__main__":
    unittest.main()
