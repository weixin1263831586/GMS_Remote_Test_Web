import unittest


class DeviceServiceTests(unittest.TestCase):
    def test_operation_failure_releases_acquired_device_locks(self):
        from features.devices.locks import DeviceLockManager
        from features.devices.service import DeviceService

        locks = DeviceLockManager()
        service = DeviceService(lock_manager=locks)

        def fail(_device_id):
            raise RuntimeError("operation failed")

        with self.assertRaisesRegex(RuntimeError, "operation failed"):
            service.run_locked(
                ["SERIAL-1", "SERIAL-2"],
                client_id="client-1",
                username="alice",
                operation=fail,
            )

        self.assertIsNone(locks.get_lock_status("SERIAL-1"))
        self.assertIsNone(locks.get_lock_status("SERIAL-2"))

    def test_lock_conflict_rolls_back_locks_acquired_in_same_request(self):
        from features.devices.locks import DeviceLockManager
        from features.devices.service import DeviceService

        locks = DeviceLockManager()
        locks.lock_device("SERIAL-2", "other-client", "bob")
        service = DeviceService(lock_manager=locks)

        result = service.run_locked(
            ["SERIAL-1", "SERIAL-2"],
            client_id="client-1",
            username="alice",
            operation=lambda device_id: device_id,
        )

        self.assertFalse(result["success"])
        self.assertIsNone(locks.get_lock_status("SERIAL-1"))
        self.assertEqual(
            locks.get_lock_status("SERIAL-2")["client_id"],
            "other-client",
        )


if __name__ == "__main__":
    unittest.main()
