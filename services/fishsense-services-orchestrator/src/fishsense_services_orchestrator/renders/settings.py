"""The render stage's settings, from ``FISHSENSE_*``."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["RenderSettings"]


class RenderSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FISHSENSE_")

    #: Each tenant's own bucket and key, as the API reads them
    #: (`fishsense_services_api.tenant_stores`). Unset, no tenant has a bucket
    #: and no render is made: there is no shared fallback.
    tenant_stores_file: Optional[Path] = None
    #: Garage as the processor reaches it: presigned URLs are signed for it.
    object_store_public_url: Optional[str] = None
    #: The hourly backlog schedule (`RenderBacklogWorkflow`); off, renders run
    #: on demand only.
    renders_enabled: bool = False
