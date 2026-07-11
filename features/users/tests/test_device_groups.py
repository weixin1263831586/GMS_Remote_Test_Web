import tempfile
import threading
import unittest
from pathlib import Path

from features.users import device_groups


class DeviceGroupPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.previous_data_root = device_groups.runtime.data_root
        self.runtime_dir = tempfile.TemporaryDirectory()
        device_groups.runtime.data_root = Path(self.runtime_dir.name)

    def tearDown(self):
        device_groups.runtime.data_root = self.previous_data_root
        self.runtime_dir.cleanup()

    def test_user_groups_use_injected_data_root(self):
        groups = [
            {
                'id': 'lab',
                'name': 'Lab',
                'color': '#000000',
                'device_ids': ['SERIAL1'],
                'followed': False,
            }
        ]

        self.assertTrue(device_groups.save_device_groups('alice', groups))

        self.assertEqual(device_groups.load_device_groups('alice'), groups)
        self.assertTrue(
            (Path(self.runtime_dir.name) / 'user_prefs/alice/device_groups.json').is_file()
        )

    def test_unsafe_owner_names_cannot_escape_or_collide(self):
        first = device_groups._device_groups_path('../alice')
        second = device_groups._device_groups_path('..\\alice')
        root = (Path(self.runtime_dir.name) / 'user_prefs').resolve()

        self.assertIn(root, first.resolve().parents)
        self.assertIn(root, second.resolve().parents)
        self.assertNotEqual(first, second)

    def test_assign_devices_preserves_request_and_existing_order(self):
        groups = [
            {'id': 'lab', 'device_ids': ['B', 'A']},
            {'id': 'other', 'device_ids': ['C', 'D']},
        ]

        self.assertIsNone(
            device_groups._assign_devices(
                groups,
                {'id': 'lab', 'mode': 'add', 'device_ids': ['C', 'A', 'E']},
            )
        )
        self.assertEqual(groups[0]['device_ids'], ['B', 'A', 'C', 'E'])
        self.assertEqual(groups[1]['device_ids'], ['D'])

        self.assertIsNone(
            device_groups._assign_devices(
                groups,
                {'id': 'lab', 'mode': 'remove', 'device_ids': ['A', 'C']},
            )
        )
        self.assertEqual(groups[0]['device_ids'], ['B', 'E'])

    def test_invalid_persisted_shape_is_treated_as_empty(self):
        path = device_groups._device_groups_path('alice')
        path.write_text('{"groups": {"not": "a list"}}', encoding='utf-8')

        self.assertEqual(device_groups.load_device_groups('alice'), [])

    def test_concurrent_updates_do_not_lose_groups(self):
        barrier = threading.Barrier(2)

        def create(name):
            barrier.wait()
            device_groups._mutate_device_groups(
                'alice', {'name': name}, 'create'
            )

        threads = [threading.Thread(target=create, args=(name,)) for name in ('A', 'B')]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(
            {group['name'] for group in device_groups.load_device_groups('alice')},
            {'A', 'B'},
        )


if __name__ == '__main__':
    unittest.main()
