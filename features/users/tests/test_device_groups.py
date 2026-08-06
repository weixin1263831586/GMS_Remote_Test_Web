import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

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

    def test_cluster_properties_use_namespaced_ids_and_worker_names(self):
        repository = SimpleNamespace(
            list_devices=lambda: [
                {
                    'id': 'worker-a:ABC',
                    'worker_id': 'worker-a',
                    'state': 'available',
                    'properties': {'product': 'rk', 'android_version': '14'},
                },
                {
                    'id': 'worker-a:OFF',
                    'worker_id': 'worker-a',
                    'state': 'offline',
                    'properties': {'model': 'Offline'},
                },
                {
                    'id': 'ats-worker-controller:LOCAL',
                    'worker_id': 'ats-worker-controller',
                    'state': 'available',
                    'properties': {'model': 'Local'},
                },
            ]
        )
        service = SimpleNamespace(
            effective_enabled=True,
            config=SimpleNamespace(local_worker_id='ats-worker-controller'),
            repository=repository,
            list_workers=lambda: [{'id': 'worker-a', 'name': 'Lab A'}],
        )

        properties = device_groups.cluster_device_properties(service)

        self.assertEqual(set(properties), {'worker-a:ABC'})
        self.assertEqual(properties['worker-a:ABC']['model'], 'rk')
        self.assertEqual(properties['worker-a:ABC']['source_host'], 'Lab A')

    def test_remote_devices_are_continuously_added_to_automatic_groups(self):
        groups = [{
            'id': 'auto_model_rk',
            'name': 'model: rk',
            'color': '#000000',
            'device_ids': [],
            'followed': False,
        }]
        self.assertTrue(device_groups.save_device_groups('alice', groups))

        updated = device_groups.auto_assign_new_devices(
            'alice', {'worker-a:ABC': {'model': 'rk'}}
        )

        self.assertEqual(updated[0]['device_ids'], ['worker-a:ABC'])

    def test_new_worker_gets_group_after_host_auto_grouping_is_enabled(self):
        groups = [{
            'id': 'auto_worker_lab-a',
            'name': 'worker: Lab A',
            'color': '#000000',
            'device_ids': ['worker-a:ABC'],
            'followed': False,
        }]
        self.assertTrue(device_groups.save_device_groups('alice', groups))

        updated = device_groups.auto_assign_new_devices(
            'alice', {'worker-b:XYZ': {'source_host': 'Lab B'}}
        )

        lab_b = next(group for group in updated if group['name'] == 'worker: Lab B')
        self.assertEqual(lab_b['device_ids'], ['worker-b:XYZ'])


if __name__ == '__main__':
    unittest.main()
