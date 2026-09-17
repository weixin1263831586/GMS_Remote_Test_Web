import json
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

    def test_legacy_agent_key_groups_migrate_to_account_key(self):
        """ADR 0010 切换后，agent 合成 actor id 落盘的旧分组应惰性迁入账号 key。"""
        from unittest.mock import patch

        legacy_groups = [
            {
                'id': 'lab',
                'name': 'Lab',
                'color': '#000000',
                'device_ids': ['SERIAL1'],
                'followed': False,
            }
        ]
        token_id = 'agt_legacy01'
        legacy_dir = (
            Path(self.runtime_dir.name)
            / 'user_prefs'
            / device_groups._owner_storage_key(f'agent:{token_id}')
        )
        legacy_dir.mkdir(parents=True)
        (legacy_dir / 'device_groups.json').write_text(
            json.dumps({'groups': legacy_groups}, ensure_ascii=False),
            encoding='utf-8',
        )

        account_path = device_groups._device_groups_path('account-1')
        self.assertFalse(account_path.is_file())

        with patch.object(
            device_groups, '_agent_token_ids_for_owner', return_value=[token_id]
        ):
            self.assertEqual(
                device_groups.load_device_groups('account-1'), legacy_groups
            )

        # 迁移是复制而非移动：目标 key 就位后不再读旧目录（幂等），旧文件
        # 保持原样以便回滚；随后保存只写账号 key。
        self.assertTrue(account_path.is_file())
        self.assertTrue((legacy_dir / 'device_groups.json').is_file())
        updated = [dict(legacy_groups[0], device_ids=['SERIAL2'])]
        self.assertTrue(device_groups.save_device_groups('account-1', updated))
        with patch.object(
            device_groups, '_agent_token_ids_for_owner', return_value=[token_id]
        ):
            self.assertEqual(device_groups.load_device_groups('account-1'), updated)

    def test_migration_without_registry_records_stays_empty(self):
        from unittest.mock import patch

        with patch.object(
            device_groups, '_agent_token_ids_for_owner', return_value=[]
        ):
            self.assertEqual(device_groups.load_device_groups('account-2'), [])
        self.assertFalse(
            device_groups._device_groups_path('account-2').is_file()
        )

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
