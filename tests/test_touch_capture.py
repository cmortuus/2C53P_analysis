"""TouchCapture state machine against a simulated scope (no hardware).

    .venv/bin/python -I -m unittest discover -s tests -v
"""
import json, os, sys, tempfile, time, unittest
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "host"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "host", "pi_bridge"))
import scope_server as S


class FakeScope:
    """Applies vdiv/timebase commands; frames reflect the current settings and the 'probe' state."""
    def __init__(self):
        self.rng = {1: 6, 2: 6}; self.tb = 0x0D; self.cmds = []
        self.signal = None                     # None = probe off; else volts amplitude of a 1 kHz square on CH1

    def cmd(self, line, timeout=3.0):
        self.cmds.append(line)
        w = line.split()
        if w[:3] == ["fpga", "scope", "vdiv"]:
            self.rng[int(w[3])] = int(w[4])
        elif w[:3] == ["fpga", "scope", "timebase"]:
            self.tb = int(w[3], 16)
        return ""

    def frame(self):
        h = {"rng1": self.rng[1], "rng2": self.rng[2], "tb": self.tb, "prb1": 0, "prb2": 0, "cpl1": 0, "cpl2": 0,
             "vpc1_nv": int(S.MV_PER_COUNT[1][self.rng[1]] * 1e6), "vpc2_nv": int(S.MV_PER_COUNT[2][self.rng[2]] * 1e6),
             "rate_hz": int(S.TIMEBASE_RATE.get(self.tb, 0))}
        rate = S.TIMEBASE_RATE[self.tb]
        flat = bytes([128 + (i % 2) for i in range(1000)])
        if self.signal is None:
            return h, flat, flat
        mv = S.MV_PER_COUNT[1][self.rng[1]]
        hi = min(250, int(128 + self.signal * 1000 / mv))
        per = rate / 1000.0
        sq = bytes([hi if ((i / per) % 1) < 0.5 else 128 for i in range(1000)])
        return h, sq, flat


class FakeHub:
    def __init__(self, scope):
        self.scope = scope


class TouchCaptureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        S.recorder = None; S.cnc = None
        self.tc = S.TouchCapture(self.tmp)
        self.scope = FakeScope(); self.hub = FakeHub(self.scope)

    def step(self, n=1):
        tags = []
        for _ in range(n):
            h, c1, c2 = self.scope.frame()
            tags.append(self.tc.on_frame(self.hub, h, c1, c2))
        return tags

    def test_touch_runs_burst_and_saves_analysis(self):
        self.step(3)                                   # init -> ready (applies the touch-ready settings)
        self.assertEqual(self.tc.state, "ready")
        self.assertEqual((self.scope.rng[1], self.scope.tb), (S.TouchCapture.READY_RANGE, S.TouchCapture.READY_TB))
        self.step(3)                                   # baseline
        self.scope.signal = 3.2                        # probe touches a 3.2 V logic signal
        tags = self.step(60)
        self.assertTrue(any(t and t.get("step", "").startswith("tb 0x08") for t in tags))
        self.assertEqual(self.tc.state, "holding")
        self.assertLess(self.scope.rng[1], S.TouchCapture.READY_RANGE)          # zoomed in for 3.2 V
        seen_tbs = [c for c in self.scope.cmds if "timebase" in c]
        for tb, _ in S.TouchCapture.PLAN:
            self.assertIn("fpga scope timebase 0x%02X" % tb, seen_tbs)
        for _ in range(50):                            # analysis thread
            if os.listdir(self.tc.dir):
                break
            time.sleep(0.05)
        with open(os.path.join(self.tc.dir, os.listdir(self.tc.dir)[0])) as f:
            d = json.load(f)
        self.assertEqual(d["touched"], [1])
        self.assertIn("ch1", d["summary"])
        self.assertIn("square", d["summary"]["ch1"])
        self.assertTrue(any(v.get("freq_hz") and abs(v["freq_hz"] - 1000) < 20 for v in d["timebases"]["ch1"].values()))
        self.scope.signal = None                       # probe lifted: back to touch-ready after ~1 s flat
        self.step(2); time.sleep(1.1); self.step(2)
        self.assertEqual(self.tc.state, "ready")

    def test_manual_commands_suspend(self):
        self.step(3)
        self.tc.note_manual()
        self.scope.signal = 3.2
        self.step(10)
        self.assertEqual(self.tc.state, "suspended")
        self.assertFalse(os.listdir(self.tc.dir))

    def test_flat_probe_does_not_trigger(self):
        self.step(30)
        self.assertEqual(self.tc.state, "ready")


if __name__ == "__main__":
    unittest.main()
