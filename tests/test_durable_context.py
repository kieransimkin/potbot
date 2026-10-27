import multiprocessing
import tempfile
import unittest
from pathlib import Path
from context_contract.core import Conflict, InvalidContext
from context_contract.durable import DurableContextStore, Denied


def race(db_path, queue):
    store = DurableContextStore(db_path)
    try:
        store.write('sandbox', 'demo', 'MODEL.md', 'changed', 1, actor='worker')
        queue.put('ok')
    except Conflict:
        queue.put('conflict')


class DurableTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name) / 'contexts.sqlite3')
        self.store = DurableContextStore(self.path)

    def test_provision_and_revisions(self):
        self.store.provision('sandbox', 'demo', actor='operator')
        old, _, rev = self.store.read('sandbox', 'demo', 'MODEL.md', actor='agent')
        self.assertEqual(rev, 1)
        self.store.provision('sandbox', 'demo', actor='operator')
        self.assertEqual(self.store.read('sandbox', 'demo', 'MODEL.md', actor='agent')[2], 1)
        self.store.write('sandbox', 'demo', 'MODEL.md', 'new', rev, actor='agent', reason='discovered')
        with self.assertRaises(Conflict):
            self.store.write('sandbox', 'demo', 'MODEL.md', 'stale', rev, actor='agent')
        self.store.restore('sandbox', 'demo', 'MODEL.md', 1, 2, actor='operator')
        self.assertEqual(self.store.read('sandbox', 'demo', 'MODEL.md', actor='agent')[0], old)
        self.assertEqual(len(self.store.history('sandbox', 'demo', 'MODEL.md', actor='agent')), 3)

    def test_cross_process_cas(self):
        self.store.provision('sandbox', 'demo', actor='operator')
        ctx = multiprocessing.get_context('spawn')
        q = ctx.Queue()
        processes = [ctx.Process(target=race, args=(self.path, q)) for _ in range(5)]
        for p in processes: p.start()
        for p in processes:
            p.join(20)
            self.assertEqual(p.exitcode, 0)
        self.assertEqual(sorted(q.get(timeout=2) for _ in processes), ['conflict'] * 4 + ['ok'])

    def test_authorization_and_validation(self):
        restricted = DurableContextStore(self.path, lambda actor, action, *args: actor == 'operator')
        with self.assertRaises(Denied):
            restricted.read('sandbox', 'demo', 'MODEL.md', actor='agent')
        with self.assertRaises(InvalidContext):
            self.store.write('sandbox', '../bad', 'MODEL.md', 'x', 0, actor='agent')
        with self.assertRaises(InvalidContext):
            self.store.write('sandbox', 'demo', 'MODEL.md', '\x00', 0, actor='agent')

if __name__ == '__main__':
    unittest.main()
