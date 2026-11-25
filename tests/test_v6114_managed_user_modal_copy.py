from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "app" / "templates" / "index.html").read_text(encoding="utf-8")
MAIN = (ROOT / "app" / "main.py").read_text(encoding="utf-8")


class ManagedUserModalCopyTests(unittest.TestCase):
    def test_modal_permission_label_has_no_master_explanation(self):
        start = HTML.index('<div class="modalback" id="managedUserEditorBack"')
        end = HTML.index('</div>{% endif %}', start)
        modal = HTML[start:end]
        self.assertIn('<label for="managedUserRole">Permissão</label>', modal)
        self.assertNotIn('Somente Master pode promover ou rebaixar', modal)

    def test_backend_permission_enforcement_is_preserved(self):
        self.assertIn('Somente o Master pode promover ou rebaixar contas existentes.', MAIN)
        self.assertIn('roleSelect.disabled=user.can_change_role!==true;', HTML)
        self.assertIn('if(roleField)roleField.hidden=user.can_change_role!==true;', HTML)

    def test_release_version_is_consistent(self):
        version = (ROOT / 'VERSION').read_text(encoding='utf-8').strip()
        self.assertEqual(version, '6.1.14')
        compose = (ROOT / 'docker-compose.yml').read_text(encoding='utf-8')
        self.assertIn('v=6.1.14', compose)


if __name__ == '__main__':
    unittest.main()
