"""Shared session tag vocabulary.

Session tags are optional policy markers computed by extensions for a given
session. They let other extensions ask policy questions without reading a
foreign extension's private context schema directly.
"""

AUTO_COMPACT_EXEMPT_TAG = "auto_compact_exempt"
HEARTBEAT_RECLAIMABLE_TAG = "heartbeat_reclaimable"
SUBAGENT_RECLAIMABLE_TAG = "subagent_reclaimable"
