"""Each label kind's labeling config, declared by the slice that owns it.

Ported from fishsense-lite@77e8f8e5 services/fishsense-api-workflow-worker/src/
fishsense_api_workflow_worker/activities/reconcile_labeling_configs_activity.py
(`_CONFIG_BY_SUFFIX`, `_config_for_title`). v1 imported the four stages'
`*_PROJECT_TITLE_SUFFIX` and `*_LABELING_CONFIG_XML` constants into the
reconcile. v2's kinds are ported on their own branches, so each declares its
own instead, and the reconcile discovers them the way the stage registry
discovers stages. **A kind's slice adds** ``<package>/labeling_config.py``::

    from fishsense_services_orchestrator.ops.labeling_configs.registry import (
        LabelingConfig,
    )

    LABELING_CONFIGS = [
        LabelingConfig(
            kind="species",
            title_suffix=SPECIES_PROJECT_TITLE_SUFFIX,  # "Species Labeling"
            xml=SPECIES_LABELING_CONFIG_XML,
        )
    ]

v1's per-dive kinds and suffixes: laser ``Laser Calibration Labeling``, species
``Species Labeling``, head/tail ``HeadTail Labeling``, slate ``Dive Slate
Labeling``. v1 did not reconcile the checkerboard-lattice project (one fixed
title, healed by its own create), and neither does v2.

Discovery runs once, when the worker builds its activities, so a malformed
declaration fails the worker's start rather than a run.
"""

from __future__ import annotations

import importlib
import importlib.util
import pkgutil
from collections.abc import Sequence
from dataclasses import dataclass
from types import ModuleType

from fishsense_services_api.label_project_store import KINDS

__all__ = [
    "InvalidLabelingConfig",
    "LabelingConfig",
    "config_for_title",
    "labeling_configs",
]

#: The module a kind's package declares its configs in, and the name it binds.
MODULE = "labeling_config"
ATTRIBUTE = "LABELING_CONFIGS"


class InvalidLabelingConfig(ValueError):
    """A declaration the reconcile can't use; raised at the worker's start."""


@dataclass(frozen=True)
class LabelingConfig:
    """What a per-dive project of ``kind`` should be configured with. Its
    title ends ``" - {title_suffix}"`` (`labels.populate.build_per_dive_title`),
    which is how the reconcile tells a kind's projects apart."""

    kind: str
    title_suffix: str
    xml: str

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise InvalidLabelingConfig(
                f"unknown Label Studio project kind {self.kind!r}; one of {KINDS}"
            )
        if not self.title_suffix.strip():
            raise InvalidLabelingConfig(f"{self.kind}: an empty title suffix")
        if not self.xml.strip():
            # An empty config pushed onto every project of the kind would wipe
            # the labelers' interface.
            raise InvalidLabelingConfig(f"{self.kind}: an empty labeling config")


def labeling_configs(root: ModuleType | None = None) -> tuple[LabelingConfig, ...]:
    """Every package's declared configs, longest suffix first, so a suffix
    that ends another can't shadow it (v1's ordering)."""
    if root is None:
        import fishsense_services_orchestrator as root  # pylint: disable=import-outside-toplevel

    found: list[LabelingConfig] = []
    for module in sorted(pkgutil.iter_modules(root.__path__), key=lambda m: m.name):
        if not module.ispkg:
            continue
        name = f"{root.__name__}.{module.name}.{MODULE}"
        if importlib.util.find_spec(name) is None:
            continue
        declared = getattr(importlib.import_module(name), ATTRIBUTE, None)
        if not isinstance(declared, Sequence) or not all(
            isinstance(c, LabelingConfig) for c in declared
        ):
            raise InvalidLabelingConfig(
                f"{name} must bind {ATTRIBUTE} to a list of LabelingConfig"
            )
        found.extend(declared)

    suffixes: dict[str, str] = {}
    for config in found:
        if config.title_suffix in suffixes:
            raise InvalidLabelingConfig(
                f"two labeling configs for the title suffix {config.title_suffix!r}: "
                f"{suffixes[config.title_suffix]} and {config.kind}"
            )
        suffixes[config.title_suffix] = config.kind
    return tuple(sorted(found, key=lambda c: -len(c.title_suffix)))


def config_for_title(
    title: str | None, configs: Sequence[LabelingConfig]
) -> LabelingConfig | None:
    """The config owning a project titled ``title``, or None if it isn't a
    per-dive project of a known kind. Matched on the suffix rather than parsed,
    so demo projects and anything else sharing the workspace are left alone.
    ``configs`` is longest suffix first (`labeling_configs`)."""
    if not title:
        return None
    for config in sorted(configs, key=lambda c: -len(c.title_suffix)):
        if title.endswith(config.title_suffix):
            return config
    return None
