import unittest
from unittest.mock import Mock, patch

from disk_nap.lab import gated_webdav
from disk_nap.macos import SleepError
from disk_nap.policy import AccessGate


class AccessGateTests(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.backing = Mock()
        self.gate = AccessGate(self.backing, 120, clock=lambda: self.now)

    def test_idle_waits_for_request_completion_and_access_wakes_once(self):
        with self.gate.access():
            self.now = 150
            self.assertFalse(self.gate.idle_check())
        self.now = 269
        self.assertFalse(self.gate.idle_check())
        self.now = 270
        self.assertTrue(self.gate.idle_check())
        self.backing.sleep.assert_called_once_with("idle")
        with self.gate.access(), self.gate.access():
            self.backing.wake.assert_called_once()

    def test_system_sleep_leaves_backing_offline_until_after_wake_and_access(self):
        self.assertTrue(self.gate.will_sleep())
        self.backing.sleep.assert_called_once_with("system-sleep")
        with self.assertRaisesRegex(SleepError, "preparing to sleep"):
            with self.gate.access():
                self.fail("Opened backing storage during system sleep")
        self.gate.did_wake()
        self.backing.wake.assert_not_called()
        with self.gate.access():
            self.backing.wake.assert_called_once()

    def test_system_sleep_does_not_detach_during_active_io(self):
        with self.gate.access():
            self.assertFalse(self.gate.will_sleep())
            self.backing.sleep.assert_not_called()

    def test_root_stat_uses_local_metadata_but_directory_listing_wakes_backing(self):
        self.now = 120
        self.assertTrue(self.gate.idle_check())
        provider = Mock(return_value=[b"files"])
        metadata = Mock(return_value=[b"root"])
        serve = gated_webdav(provider, metadata, self.gate)
        request = {"REQUEST_METHOD": "PROPFIND", "PATH_INFO": "/", "HTTP_DEPTH": "0"}
        respond = Mock()
        self.assertEqual(list(serve(request, respond)), [b"root"])
        self.backing.wake.assert_not_called()
        with patch("disk_nap.lab.log"):
            self.assertEqual(list(serve({**request, "HTTP_DEPTH": "1"}, respond)), [b"files"])
        self.backing.wake.assert_called_once()

    def test_physical_io_restarts_the_idle_window(self):
        changed = Mock(side_effect=[True, False, False])
        gate = AccessGate(self.backing, 120, clock=lambda: self.now, sample_activity=changed)
        self.now = 120
        self.assertFalse(gate.idle_check())
        self.now = 239
        self.assertFalse(gate.idle_check())
        self.now = 240
        self.assertTrue(gate.idle_check())
        self.backing.sleep.assert_called_once_with("idle")

    def test_failed_backing_identity_check_blocks_the_request(self):
        check_access = Mock(side_effect=SleepError("Original volume unavailable"))
        gate = AccessGate(self.backing, 120, check_access=check_access)
        with self.assertRaisesRegex(SleepError, "Original volume unavailable"):
            with gate.access():
                self.fail("An unverified backing path was exposed to an application")
        self.assertEqual(gate.active, 0)

    def test_turning_off_restores_the_backing_and_disables_both_triggers(self):
        self.now = 120
        self.assertTrue(self.gate.idle_check())
        self.gate.configure(False, 60)
        self.backing.wake.assert_called_once()
        self.now = 300
        self.assertFalse(self.gate.idle_check())
        self.assertFalse(self.gate.will_sleep())
        self.backing.sleep.assert_called_once_with("idle")

    def test_battery_only_gates_both_triggers_and_starts_a_new_idle_window_after_unplug(self):
        self.gate.configure(True, 120, battery_only=True)
        self.now = 300
        self.assertFalse(self.gate.idle_check())
        self.assertFalse(self.gate.will_sleep())
        self.backing.sleep.assert_not_called()
        self.gate.did_wake()
        self.now = 360
        self.gate.set_power_source(True)
        self.now = 479
        self.assertFalse(self.gate.idle_check())
        self.now = 480
        self.assertTrue(self.gate.idle_check())
        self.backing.sleep.assert_called_once_with("idle")

    def test_connecting_power_restores_a_sleeping_backing_after_the_mac_wakes(self):
        self.gate.set_power_source(True)
        self.gate.configure(True, 120, battery_only=True)
        self.assertTrue(self.gate.will_sleep())
        self.gate.set_power_source(False)
        self.assertFalse(self.gate.idle_check())
        self.backing.wake.assert_not_called()
        self.gate.did_wake()
        self.assertFalse(self.gate.idle_check())
        self.backing.wake.assert_called_once()
        self.assertFalse(self.gate.will_sleep())
        self.backing.sleep.assert_called_once_with("system-sleep")
