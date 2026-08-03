import json
import logging
from pathlib import Path

from .models import InstanceConfig

logger = logging.getLogger(__name__)


class InstanceRegistry:
    """Holds the per-instance configs, keyed by exact `iss`.

    The allowlist of in-scope issuers is exactly the set of keys. Filenames are a
    lookup convenience only; the in-file `iss` is the source of truth.
    """

    def __init__(self, by_iss: dict[str, InstanceConfig]):
        self._by_iss = by_iss

    @classmethod
    def load(cls, instances_dir: str | Path) -> "InstanceRegistry":
        directory = Path(instances_dir)
        by_iss: dict[str, InstanceConfig] = {}
        if not directory.is_dir():
            logger.warning("instances dir %s does not exist; no instances loaded", directory)
            return cls(by_iss)
        for path in sorted(directory.glob("*.json")):
            try:
                data = json.loads(path.read_text())
                cfg = InstanceConfig.model_validate(data)
            except Exception:
                logger.exception("failed to load instance file %s; skipping", path)
                continue
            if cfg.iss in by_iss:
                logger.error("duplicate iss %s (file %s); keeping first", cfg.iss, path)
                continue
            by_iss[cfg.iss] = cfg
            logger.info("loaded instance iss=%s from %s", cfg.iss, path.name)
        return cls(by_iss)

    def get(self, iss: str) -> InstanceConfig | None:
        """Exact-match lookup. Returns None if `iss` is not in scope."""
        return self._by_iss.get(iss)

    @property
    def allowlist(self) -> frozenset[str]:
        return frozenset(self._by_iss)

    def __len__(self) -> int:
        return len(self._by_iss)
