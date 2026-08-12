import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from features.auth import CurrentUser
from features.firmware import apk_api, runtime


class ApkUploadSecurityTests(unittest.TestCase):
    def setUp(self):
        self.runtime_directory = TemporaryDirectory()
        runtime.configure_runtime(
            global_state=SimpleNamespace(
                apk_analysis_tasks={},
                apk_analysis_tasks_lock=threading.RLock(),
                apk_upload_locks={},
                apk_upload_locks_lock=threading.RLock(),
                background_tasks=set(),
            ),
            get_client_id_from_request=lambda _request: 'owner',
            apk_max_tasks=20,
            apk_max_file_size=500 * 1024 * 1024,
            apk_max_source_file_size=2 * 1024 * 1024,
            apk_upload_dir=self.runtime_directory.name,
        )
        app = FastAPI()

        @app.middleware('http')
        async def authenticate_test_request(request, call_next):
            owner_id = request.headers.get('x-test-owner', 'owner')
            request.state.current_user = CurrentUser(
                id=owner_id, username=owner_id, role='user'
            )
            return await call_next(request)

        app.include_router(apk_api.router)
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        self.runtime_directory.cleanup()

    def test_rejects_excessive_chunk_count(self):
        with TemporaryDirectory() as tmp, patch.object(runtime, 'apk_upload_dir', tmp):
            response = self.client.post('/api/apk/upload', data={
                'upload_id': '00000000-0000-0000-0000-000000000001',
                'file_name': 'app.apk', 'chunk_index': '0',
                'total_chunks': str(apk_api.MAX_APK_UPLOAD_CHUNKS + 1),
            }, files={'file': ('app.apk', b'x')})
        self.assertEqual(response.status_code, 400)

    def test_binds_chunk_session_metadata(self):
        with TemporaryDirectory() as tmp, patch.object(runtime, 'apk_upload_dir', tmp):
            common = {'upload_id': '00000000-0000-0000-0000-000000000002'}
            first = self.client.post('/api/apk/upload', data={
                **common, 'file_name': 'app.apk', 'chunk_index': '0',
                'total_chunks': '2',
            }, files={'file': ('app.apk', b'12')})
            changed = self.client.post('/api/apk/upload', data={
                **common, 'file_name': 'other.apk', 'chunk_index': '1',
                'total_chunks': '3',
            }, files={'file': ('other.apk', b'34')})
        self.assertEqual(first.status_code, 200)
        self.assertEqual(changed.status_code, 400)
        self.assertIn('metadata', changed.json()['error'])

    def test_chunk_upload_session_cannot_be_taken_over_by_another_owner(self):
        with TemporaryDirectory() as tmp, patch.object(runtime, 'apk_upload_dir', tmp):
            common = {
                'upload_id': '00000000-0000-0000-0000-000000000008',
                'file_name': 'app.apk',
                'total_chunks': '2',
            }
            first = self.client.post('/api/apk/upload', headers={'x-test-owner': 'alice'}, data={
                **common, 'chunk_index': '0',
            }, files={'file': ('app.apk', b'alice')})
            takeover = self.client.post('/api/apk/upload', headers={'x-test-owner': 'bob'}, data={
                **common, 'chunk_index': '1',
            }, files={'file': ('app.apk', b'bob')})

        self.assertEqual(first.status_code, 200)
        self.assertEqual(takeover.status_code, 400)
        self.assertIn('metadata', takeover.json()['error'])

    def test_enforces_cumulative_size_before_completion(self):
        with TemporaryDirectory() as tmp, patch.object(runtime, 'apk_upload_dir', tmp), patch.object(runtime, 'apk_max_file_size', 5):
            common = {
                'upload_id': '00000000-0000-0000-0000-000000000003',
                'file_name': 'app.apk', 'total_chunks': '3',
            }
            first = self.client.post('/api/apk/upload', data={
                **common, 'chunk_index': '0',
            }, files={'file': ('app.apk', b'123')})
            oversized = self.client.post('/api/apk/upload', data={
                **common, 'chunk_index': '1',
            }, files={'file': ('app.apk', b'456')})
            self.assertEqual(first.status_code, 200)
            self.assertEqual(oversized.status_code, 413)
            self.assertEqual(list(Path(tmp).rglob('*.part*')), [])

    def test_capacity_never_evicts_analyzing_task(self):
        existing_id = '00000000-0000-0000-0000-000000000004'
        runtime.global_state.apk_analysis_tasks[existing_id] = {
            'status': 'analyzing', 'timestamp': 1,
        }
        with TemporaryDirectory() as tmp, patch.object(runtime, 'apk_upload_dir', tmp), patch.object(runtime, 'apk_max_tasks', 1):
            response = self.client.post('/api/apk/upload', data={
                'upload_id': '00000000-0000-0000-0000-000000000005',
                'file_name': 'new.apk',
            }, files={'file': ('new.apk', b'apk')})
        self.assertEqual(response.status_code, 429)
        self.assertIn(existing_id, runtime.global_state.apk_analysis_tasks)

    def test_capacity_cleanup_never_evicts_another_owners_completed_task(self):
        existing_id = '00000000-0000-0000-0000-000000000009'
        runtime.global_state.apk_analysis_tasks[existing_id] = {
            'status': 'completed', 'timestamp': 1, 'owner_id': 'bob',
        }
        with TemporaryDirectory() as tmp, patch.object(runtime, 'apk_upload_dir', tmp), patch.object(runtime, 'apk_max_tasks', 1):
            response = self.client.post('/api/apk/upload', headers={'x-test-owner': 'alice'}, data={
                'upload_id': '00000000-0000-0000-0000-000000000010',
                'file_name': 'new.apk',
            }, files={'file': ('new.apk', b'apk')})

        self.assertEqual(response.status_code, 429)
        self.assertIn(existing_id, runtime.global_state.apk_analysis_tasks)

    def test_tasks_are_isolated_by_owner(self):
        task_id = '00000000-0000-0000-0000-000000000006'
        with TemporaryDirectory() as tmp, patch.object(runtime, 'apk_upload_dir', tmp), patch.object(
            runtime, 'get_client_id_from_request',
            side_effect=lambda request: request.headers.get('x-test-owner'),
        ):
            uploaded = self.client.post('/api/apk/upload', headers={'x-test-owner': 'alice'}, data={
                'upload_id': task_id, 'file_name': 'app.apk',
            }, files={'file': ('app.apk', b'apk')})
            alice = self.client.get(f'/api/apk/status/{task_id}', headers={'x-test-owner': 'alice'})
            bob = self.client.get(f'/api/apk/status/{task_id}', headers={'x-test-owner': 'bob'})
            bob_delete = self.client.delete(f'/api/apk/task/{task_id}', headers={'x-test-owner': 'bob'})
        self.assertEqual(uploaded.status_code, 200)
        self.assertEqual(alice.status_code, 200)
        self.assertEqual(bob.status_code, 404)
        self.assertEqual(bob_delete.status_code, 404)

    def test_running_task_cannot_be_deleted_or_reused(self):
        task_id = '00000000-0000-0000-0000-000000000007'
        runtime.global_state.apk_analysis_tasks[task_id] = {
            'status': 'analyzing', 'timestamp': 1, 'owner_id': 'owner',
        }
        with TemporaryDirectory() as tmp, patch.object(runtime, 'apk_upload_dir', tmp):
            reused = self.client.post('/api/apk/upload', data={
                'upload_id': task_id, 'file_name': 'replacement.apk',
            }, files={'file': ('replacement.apk', b'apk')})
            deleted = self.client.delete(f'/api/apk/task/{task_id}')

        self.assertEqual(reused.status_code, 409)
        self.assertEqual(deleted.status_code, 409)
        self.assertIn(task_id, runtime.global_state.apk_analysis_tasks)


if __name__ == '__main__':
    unittest.main()
