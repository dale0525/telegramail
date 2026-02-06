import unittest


class TestAccountStepsProviderFlow(unittest.TestCase):
    def test_custom_provider_selection_keeps_manual_server_steps(self):
        from app.bot.handlers.account_steps import ADD_ACCOUNT_STEPS, handle_provider_selection
        from app.i18n import _

        context = {}
        handle_provider_selection(context, _("add_addcount_provider_custom"))

        email_step = next(step for step in ADD_ACCOUNT_STEPS if step.get("key") == "email")
        email_step["post_process"](context, "someone@gmail.com")

        self.assertFalse(context.get("use_common_provider", False))
        self.assertNotIn("smtp_server", context)
        self.assertNotIn("imap_server", context)

    def test_alias_step_is_optional_for_quicker_onboarding(self):
        from app.bot.handlers.account_steps import ADD_ACCOUNT_STEPS

        alias_step = next(step for step in ADD_ACCOUNT_STEPS if step.get("key") == "alias")
        self.assertTrue(alias_step.get("optional", False))
