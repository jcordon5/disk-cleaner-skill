"""Safety tests for disk_cleaner.

These lock in the guarantees that matter most: protected data is never
classified as deletable, the delete-time backstop refuses anything sensitive or
structurally unsafe, and the risk levels behave as documented.

Run with:  python3 -m unittest discover -s tests -v
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import disk_cleaner as dc  # noqa: E402


def risk(path):
    return dc.risk_of(dc.classify(path)[0])


def category(path):
    return dc.classify(path)[0]


class TestClassifyProtected(unittest.TestCase):
    """Sensitive things must always come back as protected — never deletable."""

    PROTECTED = [
        "/Users/x/.ssh/id_rsa",
        "/Users/x/.ssh/id_ed25519",
        "/Users/x/.gnupg/secring.gpg",
        "/Users/x/Library/Keychains/login.keychain-db",
        "/Users/x/Library/Application Support/OpenVPN Connect/profiles/work.ovpn",
        "/Users/x/Pictures/Photos Library.photoslibrary",
        "/Users/x/Library/Mail/V10",
        "/Users/x/Library/CloudStorage/OneDrive-Personal/big",
        "/Users/x/Library/Group Containers/UBF8T346G9.OneDriveStandaloneSuite",
        "/Users/x/Dropbox/stuff",
        "/Users/x/Documents/taxes",
        "/Users/x/.password-store/site.gpg",
        "/Users/x/Library/Application Support/Google/Chrome/Default/Cookies",
        "/home/x/.config/some.kdbx",
    ]

    def test_all_protected(self):
        for p in self.PROTECTED:
            with self.subTest(path=p):
                self.assertEqual(risk(p), "protected",
                                 f"{p} -> {category(p)} (expected protected)")
                self.assertEqual(dc.action_of(dc.classify(p)[0]), dc.RISK_NONE)


class TestClassifyReview(unittest.TestCase):
    """Things only the user can judge must be 'review', not auto-safe."""

    REVIEW = [
        "/Users/x/.rustup/toolchains/stable-aarch64/bin/rustc",
        "/Users/x/.pyenv/versions/3.12.0",
        "/Users/x/miniconda3/envs/ml",
        "/Users/x/.nvm/versions/node/v20",
        "/Users/x/Downloads/ubuntu.iso",
        "/Users/x/VMs/disk.qcow2",
        "/Users/x/Library/Application Support/Google/Chrome/Default/Service Worker/db",
        "/Users/x/Movies/clip.mp4",
    ]

    def test_all_review(self):
        for p in self.REVIEW:
            with self.subTest(path=p):
                self.assertEqual(risk(p), "review",
                                 f"{p} -> {category(p)} (expected review)")


class TestClassifySafeLevels(unittest.TestCase):
    def test_pure_safe(self):
        self.assertEqual(risk("/Users/x/Library/Caches/com.app/blob"), "safe")
        self.assertEqual(risk("/Users/x/Library/Logs/app.log"), "safe")

    def test_rebuildable_not_pure_safe(self):
        # Models are recoverable but costly — must be safe_redownload, never "safe".
        self.assertEqual(risk("/Users/x/.cache/huggingface/models/m.bin"),
                         "safe_redownload")
        self.assertEqual(category("/Users/x/.cache/huggingface/models/m.bin"),
                         "downloaded models or generated data")

    def test_browser_cache_is_rebuildable_but_cookies_protected(self):
        base = "/Users/x/Library/Application Support/Google/Chrome/Default"
        self.assertEqual(risk(base + "/Cache/data"), "safe_redownload")
        self.assertEqual(risk(base + "/Cookies"), "protected")


class TestValidateTarget(unittest.TestCase):
    """The delete-time backstop: re-derives safety from the path alone."""

    def setUp(self):
        self.home = os.path.realpath(os.path.expanduser("~"))
        self.tmp = tempfile.mkdtemp(dir=self.home, prefix=".dc-test-")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make(self, rel):
        path = os.path.join(self.tmp, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write("x")
        return path

    def test_allows_real_cache(self):
        p = self._make("Library/Caches/com.app/blob")
        ok, why = dc.validate_target(os.path.dirname(p), dc.RISK_DELETE)
        self.assertTrue(ok, why)

    def test_refuses_reclassified_protected(self):
        # A credential-looking file inside an allowed area is still refused.
        p = self._make(".ssh/id_rsa")
        ok, why = dc.validate_target(p, dc.RISK_DELETE)
        self.assertFalse(ok)
        self.assertIn("protected", why)

    def test_refuses_home_root(self):
        ok, _ = dc.validate_target(self.home, dc.RISK_DELETE)
        self.assertFalse(ok)

    def test_refuses_filesystem_root(self):
        ok, _ = dc.validate_target("/", dc.RISK_DELETE)
        self.assertFalse(ok)

    def test_refuses_outside_roots(self):
        ok, _ = dc.validate_target("/etc/hosts", dc.RISK_DELETE)
        self.assertFalse(ok)

    def test_refuses_symlink(self):
        target = self._make("Library/Caches/real/x")
        link = os.path.join(self.tmp, "Library", "Caches", "link")
        os.symlink(os.path.dirname(target), link)
        ok, why = dc.validate_target(link, dc.RISK_DELETE)
        self.assertFalse(ok)
        self.assertIn("symlink", why)

    def test_action_mismatch_refused(self):
        # A review item (would go to Trash) must not be deletable as a direct delete.
        p = self._make("Downloads/movie.iso")
        ok, why = dc.validate_target(p, dc.RISK_DELETE)
        self.assertFalse(ok)


class TestRiskModelInvariants(unittest.TestCase):
    def test_protected_category_action_is_none(self):
        self.assertEqual(dc.action_of("protected, not touched"), dc.RISK_NONE)

    def test_review_categories_go_to_trash(self):
        for cat in ("review required", "virtual machines or containers",
                    "games or media", "large personal files"):
            self.assertEqual(dc.action_of(cat), dc.RISK_TRASH, cat)

    def test_safe_categories_delete_directly(self):
        for cat in ("safe cache", "temporary files", "logs"):
            self.assertEqual(dc.action_of(cat), dc.RISK_DELETE, cat)

    def test_unknown_defaults_to_review(self):
        # Anything we can't confidently classify must never be "safe".
        cat = dc.classify("/Users/x/SomeMysteryBigFolder/data")[0]
        self.assertIn(dc.risk_of(cat), ("review", "protected"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
