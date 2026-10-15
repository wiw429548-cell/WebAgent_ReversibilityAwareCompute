"""Import-time compatibility shims for the WebArena evaluation harness (imported before `webarena`).

libwebarena imports `text_generation` (HF TGI client) at module level although RACE never uses it. text-generation
0.7 wants huggingface-hub<1.0, which conflicts with recent vLLM/transformers. If the real package cannot be imported,
a stub module is registered that raises on use (so nothing silently changes what is measured).
"""
import sys
import types

try:
    import text_generation  # noqa: F401
except Exception:  # ImportError or an incompatible huggingface_hub
    stub = types.ModuleType("text_generation")

    class _Unavailable:
        def __init__(self, *a, **k):
            raise RuntimeError("text_generation (HF TGI client) is not available; RACE does not use it")

    stub.Client = stub.AsyncClient = _Unavailable
    stub.__version__ = "0.0-race-stub"
    sys.modules["text_generation"] = stub
