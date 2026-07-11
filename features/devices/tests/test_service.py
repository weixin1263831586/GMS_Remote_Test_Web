import tempfile
import threading
import unittest
from pathlib import Path


class DeviceServiceTests(unittest.TestCase):
    def test_separate_managers_compete_without_database_error(self):
        from features.devices.locks import DeviceLockManager

        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / 'locks.sqlite3'
            managers = [DeviceLockManager(db_path), DeviceLockManager(db_path)]
            barrier = threading.Barrier(2)
            results = []

            def acquire(index):
                barrier.wait()
                results.append(
                    managers[index].lock_device('SERIAL-1', f'client-{index}', f'user-{index}')
                )

            threads = [threading.Thread(target=acquire, args=(index,)) for index in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            self.assertEqual(sorted(success for success, _ in results), [False, True])

    def test_refresh_locks_only_renews_devices_owned_by_client(self):
        from features.devices.locks import DeviceLockManager

        locks = DeviceLockManager()
        locks.lock_device("SERIAL-1", "client-1", "alice")
        locks.lock_device("SERIAL-2", "client-2", "bob")

        renewed = locks.refresh_locks("client-1", ["SERIAL-1", "SERIAL-2", "missing"])

        self.assertEqual(renewed, 1)
        self.assertEqual(locks.get_lock_status("SERIAL-1")["client_id"], "client-1")
        self.assertEqual(locks.get_lock_status("SERIAL-2")["client_id"], "client-2")

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

    def test_force_unlock_releases_other_users_device_lock(self):
        from features.devices.locks import DeviceLockManager

        locks = DeviceLockManager()
        locks.lock_device("SERIAL-1", "client-id-1", "alice")

        status = locks.get_lock_status("SERIAL-1")
        self.assertEqual(status["locked_by"], "alice")
        self.assertEqual(status["client_id"], "client-id-1")

        success, _message = locks.unlock_device("SERIAL-1", "client-id-2")
        self.assertFalse(success)
        self.assertIsNotNone(locks.get_lock_status("SERIAL-1"))

        success, _message = locks.force_unlock_device("SERIAL-1")
        self.assertTrue(success)
        self.assertIsNone(locks.get_lock_status("SERIAL-1"))


if __name__ == "__main__":
    unittest.main()
