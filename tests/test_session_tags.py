"""Tests for SessionManager session tagger registry."""

from core.session import Session, SessionManager
from core.session_tags import AUTO_COMPACT_EXEMPT_TAG, SUBAGENT_RECLAIMABLE_TAG


def _make_session(**kwargs):
    return Session(
        id="sess-1",
        name="demo",
        slot=1,
        user_id="u1",
        working_dir="/tmp",
        **kwargs,
    )


class TestSessionTaggers:
    def test_get_session_tags_unions_all_tagger_results(self, tmp_path):
        sm = SessionManager(
            base_dir=tmp_path,
            engine_config={"permission_mode": "bypassPermissions"},
        )
        sm.add_session_tagger(lambda session: {AUTO_COMPACT_EXEMPT_TAG})
        sm.add_session_tagger(lambda session: {SUBAGENT_RECLAIMABLE_TAG})

        assert sm.get_session_tags(_make_session()) == {
            AUTO_COMPACT_EXEMPT_TAG,
            SUBAGENT_RECLAIMABLE_TAG,
        }

    def test_session_has_tag_false_when_unset(self, tmp_path):
        sm = SessionManager(
            base_dir=tmp_path,
            engine_config={"permission_mode": "bypassPermissions"},
        )

        assert sm.session_has_tag(_make_session(), AUTO_COMPACT_EXEMPT_TAG) is False

    def test_tagger_exception_is_skipped(self, tmp_path):
        sm = SessionManager(
            base_dir=tmp_path,
            engine_config={"permission_mode": "bypassPermissions"},
        )

        def bad(session):
            raise RuntimeError("boom")

        sm.add_session_tagger(bad)
        sm.add_session_tagger(lambda session: {AUTO_COMPACT_EXEMPT_TAG})

        assert sm.get_session_tags(_make_session()) == {AUTO_COMPACT_EXEMPT_TAG}
