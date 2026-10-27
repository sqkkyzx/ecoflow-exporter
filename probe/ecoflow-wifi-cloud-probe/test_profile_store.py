import json
import os
from pathlib import Path
import tempfile
import unittest

from profile_store import load_profile, save_profile


class ProfileTests(unittest.TestCase):
    def test_only_profile_identifiers_are_saved_privately_and_can_be_updated(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profile.json"
            self.assertIsNone(load_profile(path))
            save_profile("+86 13800000000", "r655test1234", path)
            saved = load_profile(path)
            self.assertEqual(saved["phone"], "13800000000")
            self.assertEqual(saved["sn"], "R655TEST1234")
            self.assertEqual(set(json.loads(path.read_text())), {"version", "region", "phone", "sn"})
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            save_profile("13800000001", "R654TEST5678", path)
            self.assertEqual(load_profile(path)["sn"], "R654TEST5678")

    def test_invalid_profiles_are_not_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profile.json"
            for text in ["invalid", "[]", '{"version":1,"region":"cn","phone":"invalid","sn":"R655TEST1234"}']:
                path.write_text(text)
                self.assertIsNone(load_profile(path))


if __name__ == "__main__":
    unittest.main()
