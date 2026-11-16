"""Select the static tool before analysis; never retry using a different backend."""

from .adapters import GhidraAdapter
from pathlib import Path
from .models import resources_root, inspect_binary


class StaticRouter:
    def __init__(self, settings, *, resources=None, cache_dir=None, direct=None, managed=None):
        self.mode = settings.static_backend
        self.direct = (
            direct
            if direct is not None
            else GhidraAdapter(
                home=settings.ghidra_path or None,
                java_home=settings.java_home or None,
                resources=resources or resources_root(),
                cache_dir=cache_dir,
            )
        )
        if managed is None:
            from .static_backend import ContainerStaticAdapter

            managed = ContainerStaticAdapter(
                context=settings.docker_context,
                resources=resources or resources_root(),
                cache_dir=cache_dir,
            )
        self.managed = managed
        self._selected = None

    async def _select(self):
        if self._selected is None:
            if self.mode == 'direct':
                self._selected = ('direct', self.direct)
            elif self.mode == 'rea':
                self._selected = ('rea', self.managed)
            else:
                probe = await self.managed.probe()
                self._selected = (
                    ('rea', self.managed) if probe.get('ready') else ('direct', self.direct)
                )
        return self._selected

    async def probe(self):
        backend, adapter = await self._select()
        return dict(await adapter.probe(), backend=backend)

    async def analyze(self, *args, **kwargs):
        backend, adapter = await self._select()
        binary = args[0] if args else kwargs.get('binary')
        if backend == 'direct' and binary is not None and Path(binary).is_file():
            if inspect_binary(binary).get('analysis_kind') == 'managed':
                return dict(status='blocked', complete=False, functions=[], backend=backend,
                            error='.NET 정적 비교에는 REA 관리 환경이 필요합니다. 환경을 준비하고 REA 또는 자동 선택을 사용하세요.')
        result = await adapter.analyze(*args, **kwargs)
        return dict(result, backend=backend)
