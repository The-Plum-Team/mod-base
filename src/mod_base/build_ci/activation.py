"""Closed profile activation data; never transition, rendering or execution authority (MB11)."""

from __future__ import annotations

from typing import Any

from mod_base import readable_schema_versions
from mod_base.model import grammar
from mod_base.model.validators import Const, Int, Obj, Str


ACTIVATION_PATH = 'site/mod-base-build-activation.json'
ACTIVATION_MODES = ('disabled', 'shadow', 'shared-build', 'shared-build-and-e2e', 'reviewed-rollback')

_ACTIVATION = Obj({
    'kind': Const('mod-base.ci.activation'),
    'schema_version': Int(min(readable_schema_versions('mod-base.ci.activation')),
                          max(readable_schema_versions('mod-base.ci.activation'))),
    'repository': Str(grammar.REPOSITORY, max_len=201),
    'profile': Str(choices=('quick-skin', 'block-pops')),
    'mode': Str(choices=ACTIVATION_MODES),
})


def validate_activation(document: Any, *, path: str = '$') -> dict[str, Any]:
    """Validate only data shape; even reviewed-rollback does not establish owner approval.

    Protected admission must bind repository/profile to its original configuration, prove the
    exact current-head transition and native predecessors, and admit fixed caller bytes. No
    kit pin, template/job/permission/secret selection or version/scenario catalog is accepted.
    """
    _ACTIVATION(document, path)
    return document
