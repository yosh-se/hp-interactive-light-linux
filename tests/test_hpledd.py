import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import hpledd  # noqa: E402
from hpledd import Led, OFF, Sensors, decide  # noqa: E402


class FakeLed:
    def __init__(self):
        self.calls = []

    def set(self, led):
        self.calls.append(led)


def daemon(sensors=Sensors(), low=20):
    d = hpledd.Daemon(FakeLed(), low_battery=low, sense=lambda: sensors)
    d.sensors = sensors
    return d


class DecideTests(unittest.TestCase):
    def test_idle_is_off(self):
        self.assertEqual(decide(False, "none", Sensors(), 20), (OFF, "idle"))

    def test_internet_is_solid_white(self):
        self.assertEqual(decide(False, "none", Sensors(internet=True), 20)[0], Led("on", "white"))

    def test_mode_beats_internet(self):
        led = decide(False, "test", Sensors(internet=True), 20)[0]
        self.assertEqual(led, Led("on", "green"))

    def test_quiz_colours(self):
        for answer, colour in (("a", "white"), ("b", "green"), ("c", "red")):
            self.assertEqual(decide(False, f"quiz:{answer}", Sensors(), 20)[0], Led("on", colour))

    def test_low_battery_only_when_unplugged(self):
        self.assertEqual(decide(False, "none", Sensors(15, True, True), 20)[0], Led("blink", "red"))
        self.assertEqual(decide(False, "none", Sensors(15, False, True), 20)[0], Led("on", "white"))
        self.assertEqual(decide(False, "none", Sensors(21, True, True), 20)[0], Led("on", "white"))
        self.assertEqual(decide(False, "none", Sensors(20, True, True), 20)[0], Led("blink", "red"))

    def test_low_battery_beats_mode(self):
        led, reason = decide(False, "test", Sensors(10, True, True), 20)
        self.assertEqual((led, reason), (Led("blink", "red"), "low-battery"))

    def test_help_beats_everything(self):
        led, reason = decide(True, "quiz:a", Sensors(5, True, True), 20)
        self.assertEqual((led, reason), (Led("blink", "red"), "help"))

    def test_no_battery(self):
        self.assertEqual(decide(False, "none", Sensors(None, True, False), 20), (OFF, "idle"))

    def test_colour_pages_suppress_internet_white(self):
        for page in ("quiz", "group"):
            self.assertEqual(decide(False, "none", Sensors(internet=True), 20, page),
                             (OFF, f"{page}-page"))

    def test_other_pages_do_not_suppress_internet(self):
        for page in (None, "help", "test", "about"):
            self.assertEqual(decide(False, "none", Sensors(internet=True), 20, page)[0],
                             Led("on", "white"))

    def test_selection_beats_colour_page(self):
        self.assertEqual(decide(False, "quiz:b", Sensors(internet=True), 20, "quiz")[0], Led("on", "green"))
        self.assertEqual(decide(False, "group:white", Sensors(internet=True), 20, "group")[0],
                         Led("on", "white"))
        self.assertEqual(decide(False, "test", Sensors(internet=True), 20, "quiz")[0], Led("on", "green"))

    def test_help_and_low_battery_beat_colour_page(self):
        self.assertEqual(decide(True, "none", Sensors(internet=True), 20, "quiz")[1], "help")
        self.assertEqual(decide(False, "none", Sensors(10, True, True), 20, "group")[1], "low-battery")


class HandleTests(unittest.TestCase):
    def test_help_toggle_writes_led(self):
        d = daemon(Sensors(internet=True))
        d.apply()
        self.assertTrue(d.handle("help on").startswith("ok "))
        self.assertEqual(d.backend.calls[-1], Led("blink", "red"))
        d.handle("help off")
        self.assertEqual(d.backend.calls[-1], Led("on", "white"))

    def test_unchanged_led_is_not_rewritten(self):
        d = daemon(Sensors(internet=True))
        d.apply()
        d.apply()
        d.handle("off")
        self.assertEqual(len(d.backend.calls), 1)

    def test_last_mode_wins_and_off_clears_only_matching_kind(self):
        d = daemon()
        d.handle("test on")
        d.handle("quiz b")
        self.assertEqual(d.mode, "quiz:b")
        d.handle("test off")
        self.assertEqual(d.mode, "quiz:b")
        d.handle("quiz off")
        self.assertEqual(d.mode, "none")

    def test_group_and_off(self):
        d = daemon()
        d.handle("group red")
        self.assertEqual(d.backend.calls[-1], Led("on", "red"))
        d.handle("off")
        self.assertEqual(d.backend.calls[-1], OFF)

    def test_bad_commands(self):
        d = daemon()
        for line in ("", "help", "help maybe", "quiz d", "group blue", "rm -rf /"):
            self.assertTrue(d.handle(line).startswith("error"), line)
        self.assertEqual(d.backend.calls, [])

    def test_case_and_whitespace_insensitive(self):
        d = daemon()
        d.handle("  QUIZ   C \n")
        self.assertEqual(d.mode, "quiz:c")

    def test_page_commands(self):
        d = daemon(Sensors(internet=True))
        d.apply()
        self.assertEqual(d.backend.calls[-1], Led("on", "white"))
        d.handle("page quiz")
        self.assertEqual(d.backend.calls[-1], OFF)
        d.handle("quiz b")
        self.assertEqual(d.backend.calls[-1], Led("on", "green"))
        d.handle("quiz off")
        self.assertEqual(d.backend.calls[-1], OFF)
        d.handle("page group")
        self.assertEqual(d.desired(), (OFF, "group-page"))
        d.handle("page none")
        self.assertEqual(d.backend.calls[-1], Led("on", "white"))

    def test_page_expires_without_heartbeat(self):
        d = daemon(Sensors(internet=True))
        d.handle("page quiz")
        self.assertEqual(d.desired(), (OFF, "quiz-page"))
        d.gui_until = time.monotonic() - 1  # GUI stopped refreshing (closed or crashed)
        self.assertEqual(d.desired()[0], Led("on", "white"))
        d.apply()
        self.assertEqual(d.backend.calls[-1], Led("on", "white"))

    def test_bad_page(self):
        d = daemon()
        for line in ("page", "page help", "page quiz group", "quiz open"):
            self.assertTrue(d.handle(line).startswith("error"), line)

    def test_status(self):
        d = daemon(Sensors(42, True, True))
        self.assertEqual(d.handle("status"),
                         "ok led=white reason=internet help=off mode=none battery=42% (discharging) internet=yes")

    def test_failed_write_is_retried(self):
        class Flaky(FakeLed):
            fail = True

            def set(self, led):
                if self.fail:
                    raise OSError("Error: AE_TEST")
                super().set(led)

        d = hpledd.Daemon(Flaky(), sense=lambda: Sensors(internet=True))
        d.sensors = Sensors(internet=True)
        d.apply()
        self.assertIsNone(d.applied)
        d.backend.fail = False
        d.apply()
        self.assertEqual(d.applied, Led("on", "white"))


class SensorTests(unittest.TestCase):
    def test_battery(self):
        with tempfile.TemporaryDirectory() as base:
            os.makedirs(os.path.join(base, "AC"))
            bat = os.path.join(base, "BAT0")
            os.makedirs(bat)
            for name, value in (("capacity", "17\n"), ("status", "Discharging\n")):
                with open(os.path.join(bat, name), "w") as f:
                    f.write(value)
            self.assertEqual(hpledd.read_battery(base), (17, True))
            with open(os.path.join(bat, "status"), "w") as f:
                f.write("Charging\n")
            self.assertEqual(hpledd.read_battery(base), (17, False))

    def test_no_battery(self):
        with tempfile.TemporaryDirectory() as base:
            self.assertEqual(hpledd.read_battery(base), (None, False))

    def test_default_route(self):
        header = "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\tMTU\tWindow\tIRTT\n"
        default = "wlp0\t00000000\t0102A8C0\t0003\t0\t0\t600\t00000000\t0\t0\t0\n"
        local = "wlp0\t0002A8C0\t00000000\t0001\t0\t0\t600\t00FFFFFF\t0\t0\t0\n"
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write(header + local + default)
        try:
            self.assertTrue(hpledd.has_default_route(f.name))
        finally:
            os.unlink(f.name)
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write(header + local)
        try:
            self.assertFalse(hpledd.has_default_route(f.name))
        finally:
            os.unlink(f.name)
        self.assertFalse(hpledd.has_default_route("/nonexistent"))


if __name__ == "__main__":
    unittest.main()
