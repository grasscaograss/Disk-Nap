import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from disk_nap import observation


def window(seconds, io_ended=True):
    return {"start": 0, "end": seconds, "ioEnded": io_ended}


def lid(mode, drop, *, on_battery=True, same_device=True, hours=8):
    return {
        "start": 0, "end": hours * 3600, "before": 80, "after": 80 - drop,
        "mode": mode, "onBattery": on_battery, "sameDevice": same_device,
    }


class ObservationTests(unittest.TestCase):
    def test_replay_subtracts_eject_time_and_counts_only_io_ended_windows(self):
        windows = [window(125), window(185), window(305, False), window(900)]
        self.assertEqual(observation.replay(windows, [2, 5, 20]), [
            {"minutes": 2, "seconds": 1015, "wakes": 2},
            {"minutes": 5, "seconds": 595, "wakes": 1},
            {"minutes": 20, "seconds": 0, "wakes": 0},
        ])

    def test_recommendation_prefers_fewer_wakes_for_near_equal_sleep(self):
        windows = [window(duration) for duration in [190, 360, 700, 720, 2900, 10200, 14400]]
        self.assertEqual(observation.recommend(windows, observation.replay(windows)), 10)
        single = [window(1000)]
        self.assertIsNone(observation.recommend(single, observation.replay(single)))

    def test_lid_comparison_excludes_charge_disconnection_and_short_sessions(self):
        observed = [lid("observe", 4), lid("observe", 6)]
        managed = [lid("managed", 2), lid("managed", 2)]
        other = [lid("managed", 0, on_battery=False), lid("managed", 0, same_device=False),
                 lid("managed", 1, hours=0.5)]
        self.assertIsNone(observation.lid_comparison(observed + other))
        self.assertEqual(observation.lid_comparison(observed + managed + other), {
            "percentPoints": 3, "hours": 8, "observeCount": 2, "managedCount": 2,
        })

    def test_port_power_requires_the_verified_host_port_pair(self):
        port = {"IORegistryEntryName": "usb-drd0-port-ss", "locationID": 0x200000}
        battery = {"PowerOutDetails": [{"PortIndex": 1, "AdapterVoltage": 5100, "Current": 200}]}
        self.assertAlmostEqual(observation.port_watts("Mac16,12", port, battery), 1.02)
        self.assertIsNone(observation.port_watts("Mac16,13", port, battery))
        self.assertIsNone(observation.port_watts("Mac16,12", {**port, "locationID": 0x100000}, battery))

    def test_battery_energy_uses_full_charge_capacity_for_the_verified_model(self):
        battery = {"BatteryData": {"AppleRawMaxCapacity": 4000, "DesignCapacity": 5000}}
        self.assertAlmostEqual(observation.battery_full_wh("Mac16,12", battery), 43.04)
        self.assertIsNone(observation.battery_full_wh("Mac16,13", battery))
        battery["BatteryData"]["DesignCapacity"] = 0
        self.assertIsNone(observation.battery_full_wh("Mac16,12", battery))

    def test_forecast_can_use_power_samples_older_than_the_overview_window(self):
        with TemporaryDirectory() as directory:
            now = 10 * observation.DAY
            history = observation.History(Path(directory) / "observations.json")
            history.power = [
                {"time": now - 8 * observation.DAY, "phase": "u3", "watts": 0.9},
                {"time": now - 2 * observation.DAY, "phase": "u3", "watts": 0.1},
                {"time": now, "phase": "idle", "watts": 1.1},
            ]
            target = SimpleNamespace(usb=SimpleNamespace(display_name="Test SSD"),
                                     volume=SimpleNamespace(mount_point=Path("/Volumes/Test")))
            observer = observation.Observation(target, history, "Mac16,12", 5)
            summary = observer.snapshot(now, 1.1, False, 43.04)
            self.assertIsNone(summary["u3Watts"])
            self.assertAlmostEqual(summary["sleepSavingsWatts7d"], 1)
            self.assertEqual(summary["batteryFullWh"], 43.04)
            history.power.pop(1)
            self.assertIsNone(observer.snapshot(now, 1.1, False)["sleepSavingsWatts7d"])

    def test_measured_sleep_counts_ongoing_and_closed_intervals_in_each_window(self):
        with TemporaryDirectory() as directory:
            now = 10 * observation.DAY
            cutoff = now - 7 * observation.DAY
            history = observation.History(Path(directory) / "observations.json")
            history.u3 = [
                {"start": cutoff - 60, "end": cutoff - 30},
                {"start": cutoff - 30, "end": cutoff + 30},
                {"start": now - 180, "end": now - 120},
            ]
            target = SimpleNamespace(usb=SimpleNamespace(display_name="Test SSD"),
                                     volume=SimpleNamespace(mount_point=Path("/Volumes/Test")))
            observer = observation.Observation(target, history, "Mac16,12", 5)
            observer.u3_start = now - 60
            summary = observer.snapshot(now, 0.08, True)
            self.assertEqual(summary["u3Seconds"], 120)
            self.assertEqual(summary["u3Seconds7d"], 150)

    def test_energy_accumulates_valid_samples_without_crossing_a_gap(self):
        with TemporaryDirectory() as directory:
            history = observation.History(Path(directory) / "observations.json")
            target = SimpleNamespace(usb=SimpleNamespace(display_name="Test SSD"),
                                     volume=SimpleNamespace(mount_point=Path("/Volumes/Test")))
            observer = observation.Observation(target, history, "Mac16,12", 5)
            with patch.object(observation, "attached_port", return_value={"port-status": 0x1505}), \
                 patch.object(observation, "registry", return_value=[]), \
                 patch.object(observation, "power_snapshot", return_value={}), \
                 patch.object(observation, "port_watts", side_effect=[1, 2, 1, 1]), \
                 patch.object(observation, "storage_sample", return_value=(1, (0, 0, 0, 0))):
                observer.sample(58)
                observer.sample(63)
                observer.sample(120)
                summary = observer.sample(125)
            self.assertEqual(summary["powerCoverageSeconds"], 10)
            self.assertEqual(summary["device"], "Test SSD")
            self.assertAlmostEqual(summary["energyWh24h"], (1.5 * 5 + 1 * 5) / 3600)
            history.save(125)
            self.assertEqual(observation.History(history.path).energy_since(0)[1], 10)

    def test_lid_session_uses_current_system_sleep_and_wake_timestamps(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "observations.json"
            history = observation.History(path)
            observer = observation.Observation(SimpleNamespace(usb=object()), history, "Mac16,12", 5)
            observer.pending_sleep = (100, 80, True, False)
            times = "kern.sleeptime: { sec = 102, usec = 500000 }\nkern.waketime: { sec = 130, usec = 0 }"
            with patch.object(observation, "registry", return_value=[{"Last Sleep Reason": "Clamshell Sleep"}]), \
                 patch.object(observation.subprocess, "check_output", return_value=times), \
                 patch.object(observation, "power_snapshot", return_value={
                     "CurrentCapacity": 79, "MaxCapacity": 100, "ExternalConnected": False,
                 }), patch.object(observation, "same_usb", return_value=object()):
                observer.did_wake(131)
            history.save(131)
            self.assertEqual(observation.History(path).lids, [{
                "start": 102.5, "end": 130.0, "before": 80, "after": 79,
                "onBattery": True, "u3BeforeSleep": False, "mode": "observe", "sameDevice": True,
            }])
