"""The experiment's infra classes, one per tech; its variants are the
*.toml files beside them (one per variant: what the agent is given, how it
is judged, which of these classes provisions its infra).

A tech's class owns the infra every variant of that tech shares (the dind
sidecar for pulumi, the kind tooling for keda), what the agent may
touch while it authors when its variant has access (`access_infra`: a
cluster, the sidecar's daemon), and the two verify-side hooks
that bring one arrangement's world up inside the verify container for the
service the verifier starts, and reset it after (`verify_setup` /
`verify_teardown`; the pieces they share with the verifier are
`service.py`). The interface they answer is fae/cell/infra/base.py.
"""

from fae.cell.infra.dind import DindSidecar  # noqa: E402,F401
from .keda import Keda, KindCluster  # noqa: E402,F401
from .pulumi import Pulumi  # noqa: E402,F401
