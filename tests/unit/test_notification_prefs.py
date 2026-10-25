from app.core.notification_prefs import DEFAULT_NOTIFICATION_PREFS, effective_prefs, wants_email


class TestEffectivePrefs:
    def test_none_returns_defaults(self):
        assert effective_prefs(None) == DEFAULT_NOTIFICATION_PREFS

    def test_empty_dict_returns_defaults(self):
        assert effective_prefs({}) == DEFAULT_NOTIFICATION_PREFS

    def test_partial_override_merges_over_defaults(self):
        merged = effective_prefs({"vote_cast": False})
        assert merged["vote_cast"] is False
        # Everything else still defaults
        for key, value in DEFAULT_NOTIFICATION_PREFS.items():
            if key != "vote_cast":
                assert merged[key] == value

    def test_unknown_keys_ignored(self):
        merged = effective_prefs({"not_a_real_key": True})
        assert "not_a_real_key" not in merged
        assert merged == DEFAULT_NOTIFICATION_PREFS


class TestWantsEmail:
    def test_default_true_key(self):
        assert wants_email(None, "vote_cast") is True

    def test_default_false_key(self):
        assert wants_email(None, "new_member") is False

    def test_stored_override_wins(self):
        assert wants_email({"new_member": True}, "new_member") is True
        assert wants_email({"vote_cast": False}, "vote_cast") is False
