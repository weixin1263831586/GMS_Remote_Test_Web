import sqlite3
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from features.auth.service import AuthService


class AuthServiceConcurrencyTests(unittest.TestCase):
    def test_only_one_initial_admin_can_be_created_across_instances(self):
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / 'auth.sqlite3'
            services = [AuthService(db_path), AuthService(db_path)]
            barrier = threading.Barrier(2)
            outcomes = []

            def create(index):
                barrier.wait()
                try:
                    services[index].create_initial_admin(
                        f'admin{index}',
                        'strongpass1',
                    )
                    outcomes.append('created')
                except ValueError:
                    outcomes.append('rejected')

            threads = [threading.Thread(target=create, args=(index,)) for index in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            self.assertEqual(sorted(outcomes), ['created', 'rejected'])
            self.assertEqual(len(services[0].list_users()), 1)

    def test_new_session_purges_revoked_sessions(self):
        with TemporaryDirectory() as tmp:
            service = AuthService(Path(tmp) / 'auth.sqlite3')
            user = service.create_initial_admin('admin', 'strongpass1')
            revoked = service.create_session(user.id)
            service.revoke_session(revoked)

            service.create_session(user.id)

            with sqlite3.connect(service.db_path) as conn:
                count = conn.execute(
                    'SELECT COUNT(*) FROM platform_sessions'
                ).fetchone()[0]
            self.assertEqual(count, 1)


if __name__ == '__main__':
    unittest.main()
