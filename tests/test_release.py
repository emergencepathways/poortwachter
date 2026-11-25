"""The version in the code and the newest CHANGELOG entry must agree, so a tag
always installs what its changelog says."""
import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


class ReleaseTests(unittest.TestCase):
    def test_version_matches_changelog(self):
        import poortwachter
        with open(os.path.join(ROOT, "CHANGELOG.md")) as f:
            m = re.search(r"^## \[(\d+\.\d+\.\d+)\]", f.read(), re.M)
        self.assertIsNotNone(m, "CHANGELOG.md needs a '## [x.y.z] - date' section")
        self.assertEqual(poortwachter.__version__, m.group(1))


if __name__ == "__main__":
    unittest.main()
