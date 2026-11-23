"""hermes-quota agent half.

This plugin is backend-only: its dashboard/plugin_api.py serves the snapshot
contract to the desktop renderer via ctx.rest('/snapshot'). No agent tools,
hooks or commands are registered on purpose.
"""


def register(ctx):
    pass
