import sqlite3
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from features.auth.service import DEFAULT_SESSION_ABSOLUTE_HOURS, AuthService


class AuthServiceConcurrencyTests(unittest.TestCase):
    def test_default_absolute_session_lifetime_does_not_interrupt_browser_use(self):
        self.assertEqual(DEFAULT_SESSION_ABSOLUTE_HOURS, 100 * 365 * 24)

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

    def test_concurrent_last_admin_demotions_never_leave_zero_admins(self):
        """两个独立 AuthService 实例（独立 SQLite 连接，模拟多 Uvicorn
        worker）同时降级仅剩的两位管理员：数据库事务必须保证至少一位
        存活。线程锁只保护单进程，若依赖它则两个 demotion 都会通过
        admin count=2 检查并留下 0 位管理员。"""
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / 'auth.sqlite3'
            services = [AuthService(db_path), AuthService(db_path)]
            services[0].create_initial_admin('admin1', 'strongpass1')
            services[0].create_user('admin2', 'strongpass1', role='admin')
            self.assertEqual(
                sorted(str(user.get('id') or user.get('username')) for user in services[0].list_users()),
                ['admin1', 'admin2'],
            )
            barrier = threading.Barrier(2)
            outcomes = []

            def demote(index):
                barrier.wait()
                try:
                    services[index].update_user(f'admin{index + 1}', role='user')
                    outcomes.append('demoted')
                except ValueError:
                    outcomes.append('rejected')

            threads = [threading.Thread(target=demote, args=(index,)) for index in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            self.assertEqual(sorted(outcomes), ['demoted', 'rejected'])
            with sqlite3.connect(db_path) as conn:
                active_admins = conn.execute(
                    "SELECT COUNT(*) FROM platform_users"
                    " WHERE role='admin' AND disabled=0"
                ).fetchone()[0]
            self.assertGreaterEqual(active_admins, 1)

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
