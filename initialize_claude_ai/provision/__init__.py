"""Phase 3 machinery: model discovery, settings writing and the inventory."""

from __future__ import annotations

from .inventory import Inventory, InventoryCollector, render
from .models import ModelCatalog, ModelInfo, discover
from .settings_writer import SettingsWriter, SettingsWriteResult, settings_path

#: Which phase-2 auth backend has to have succeeded before a provisioning
#: backend is usable. Empty means "no initialize-claude-ai-managed credential is
#: involved" -- Bedrock is reached through whatever the AWS SDK chain finds.
#:
#: It lives here rather than in phase 3 because phase 2 reads it too, to say
#: which provisioning backends the credentials it verified actually unlock.
AUTH_FOR_BACKEND = {
    "vertex": "google-cloud",
    "anthropic-api": "anthropic-api",
    "bedrock": "",
}

__all__ = [
    "AUTH_FOR_BACKEND",
    "ModelInfo",
    "ModelCatalog",
    "discover",
    "SettingsWriter",
    "SettingsWriteResult",
    "settings_path",
    "Inventory",
    "InventoryCollector",
    "render",
]
