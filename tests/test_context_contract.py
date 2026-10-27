import tempfile
import unittest
from pathlib import Path
from context_contract import ContextStore, Conflict, InvalidContext

class ContextTests(unittest.TestCase):
    def test_provision_and_cas(self):
        with tempfile.TemporaryDirectory() as root:
            store = ContextStore(root)
            store.provision('sandbox', 'one')
            old, etag = store.read('sandbox', 'one', 'MODEL.md')
            store.provision('sandbox', 'one')
            store.write('sandbox', 'one', 'MODEL.md', '# Updated\n', etag)
            with self.assertRaises(Conflict):
                store.write('sandbox', 'one', 'MODEL.md', '# Lost\n', etag)
            self.assertEqual(store.read('sandbox', 'one', 'MODEL.md')[0], '# Updated\n')

    def test_traversal_and_symlink(self):
        with tempfile.TemporaryDirectory() as root:
            store = ContextStore(root)
            with self.assertRaises(InvalidContext):
                store.read('tool', '../bad', 'MODEL.md')
            (Path(root) / 'skill').symlink_to(root, target_is_directory=True)
            with self.assertRaises(InvalidContext):
                store.read('skill', 'x', 'MODEL.md')

if __name__ == '__main__':
    unittest.main()
