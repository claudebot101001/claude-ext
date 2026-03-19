"""Tests for extension-scoped session context helpers."""

from core.session_context import (
    clone_public_context,
    export_legacy_context,
    get_extension_state,
    normalize_extension_state,
    register_legacy_keys,
    set_extension_state,
)


class TestSessionContextHelpers:
    def test_set_extension_state_writes_namespaced_state(self):
        context = {"chat_id": 123}

        set_extension_state(context, "cron", "job_id", "job-2")

        assert context["chat_id"] == 123
        assert context["_extensions"]["cron"]["job_id"] == "job-2"

    def test_normalize_extension_state_migrates_legacy_keys(self):
        context = {"heartbeat_run": True}

        changed = normalize_extension_state(
            context,
            mapping={"heartbeat_run": ("heartbeat", "run")},
        )

        assert changed is True
        assert get_extension_state(context, "heartbeat", "run") is True
        assert "heartbeat_run" not in context

    def test_clone_public_context_drops_extension_namespace(self):
        context = {
            "chat_id": 123,
            "domains": ["test"],
            "_extensions": {"context": {"suppress_delivery": True}},
        }

        cloned = clone_public_context(context)

        assert cloned == {"chat_id": 123, "domains": ["test"]}

    def test_clone_public_context_is_side_effect_free(self):
        context = {"chat_id": 123, "_extensions": {"context": {"suppress_delivery": True}}}

        cloned = clone_public_context(context)

        assert cloned == {"chat_id": 123}
        assert context == {"chat_id": 123, "_extensions": {"context": {"suppress_delivery": True}}}

    def test_export_legacy_context_projects_namespaced_state(self):
        context = {
            "_extensions": {
                "subagent": {
                    "worker": True,
                    "worktree_branch": None,
                }
            }
        }

        exported = export_legacy_context(
            context,
            {
                "subagent_worker": ("subagent", "worker"),
                "subagent_worktree_branch": ("subagent", "worktree_branch"),
            },
        )

        assert exported == {
            "subagent_worker": True,
            "subagent_worktree_branch": None,
        }

    def test_export_legacy_context_is_side_effect_free(self):
        context = {"_extensions": {"subagent": {"worker": True}}}

        exported = export_legacy_context(context, {"subagent_worker": ("subagent", "worker")})

        assert exported == {"subagent_worker": True}
        assert context == {"_extensions": {"subagent": {"worker": True}}}

    def test_register_legacy_keys_accepts_idempotent_reregistration(self):
        mapping = {"__test_idempotent_key__": ("test_ns", "flag")}

        register_legacy_keys(mapping)
        register_legacy_keys(mapping)

        context = {"__test_idempotent_key__": True}
        normalize_extension_state(context)
        assert get_extension_state(context, "test_ns", "flag") is True

    def test_register_legacy_keys_rejects_conflicts(self):
        register_legacy_keys({"__test_conflict_key__": ("first", "flag")})

        try:
            register_legacy_keys({"__test_conflict_key__": ("second", "flag")})
        except ValueError as exc:
            assert "__test_conflict_key__" in str(exc)
        else:
            raise AssertionError("Expected conflicting legacy key registration to fail")
