import tempfile
import unittest
from pathlib import Path


class TestCheckI18nDynamicPrefixes(unittest.TestCase):
    def test_fstring_i18n_prefixes_are_detected_and_expanded(self):
        import scripts.check_i18n as check_i18n

        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "sample.py").write_text(
                "\n".join(
                    [
                        "from app.i18n import _",
                        "",
                        "def f(category, priority):",
                        "    _(f\"email_category_{category}\")",
                        "    _(f\"email_priority_{priority}\")",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            used_keys, key_params, dynamic_prefixes = check_i18n.find_used_keys_and_params(tmp)
            self.assertIn("email_category_", dynamic_prefixes)
            self.assertIn("email_priority_", dynamic_prefixes)

            defined_keys = {
                "email_category_meeting",
                "email_category_other",
                "email_priority_high",
            }
            expanded = check_i18n.expand_keys_for_dynamic_prefixes(
                used_keys, dynamic_prefixes, defined_keys
            )
            self.assertTrue(
                {"email_category_meeting", "email_category_other", "email_priority_high"}.issubset(
                    expanded
                )
            )

