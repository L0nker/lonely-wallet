from pathlib import Path
import struct
import unittest

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "app" / "static"
HTML = (ROOT / "app" / "templates" / "index.html").read_text(encoding="utf-8")
README = (ROOT / "README.md").read_text(encoding="utf-8")
COMPOSE = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")


class LightThemeBrandAssetsTests(unittest.TestCase):
    def test_light_variants_exist_and_are_valid_pngs(self):
        pairs = (
            ("lonely-wallet-logotype.png", "lonely-wallet-logotype-light.png"),
            ("lonely-wallet-logo.png", "lonely-wallet-logo-light.png"),
            ("lonely-wallet-favicon.png", "lonely-wallet-favicon-light.png"),
        )
        for original, light in pairs:
            original_path = STATIC / original
            light_path = STATIC / light
            self.assertTrue(original_path.is_file(), original)
            self.assertTrue(light_path.is_file(), light)
            payload = light_path.read_bytes()
            self.assertTrue(payload.startswith(b"\x89PNG\r\n\x1a\n"), light)
            self.assertGreater(len(payload), 1000, light)
            width, height = struct.unpack(">II", payload[16:24])
            self.assertGreater(width, 0)
            self.assertGreater(height, 0)
            original_payload = original_path.read_bytes()
            self.assertEqual(struct.unpack(">II", original_payload[16:24]), (width, height))

    def test_theme_switch_updates_header_loading_logo_and_browser_icons(self):
        self.assertIn('id="appFavicon"', HTML)
        self.assertIn('id="appAppleTouchIcon"', HTML)
        self.assertIn("function setThemeBrandAssets(light)", HTML)
        self.assertIn("/static/lonely-wallet-logotype${suffix}.png", HTML)
        self.assertIn("/static/lonely-wallet-logo${suffix}.png", HTML)
        self.assertIn("/static/lonely-wallet-favicon${suffix}.png", HTML)
        self.assertIn("setThemeBrandAssets(!!light);", HTML)
        self.assertIn("setThemeBrandAssets(document.body.classList.contains('light-theme')", HTML)

    def test_release_metadata_and_readme_are_consistent(self):
        self.assertEqual((ROOT / "VERSION").read_text(encoding="utf-8").strip(), "6.1.17")
        self.assertIn('version: "6.1.17"', COMPOSE)
        self.assertIn('v=6.1.17', COMPOSE)
        self.assertIn("Destaques da versão 6.1.17", README)
        self.assertIn("lonely-wallet-logotype-light.png", README)
        self.assertIn("lonely-wallet-favicon-light.png", README)


if __name__ == "__main__":
    unittest.main()
