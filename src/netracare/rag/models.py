"""Pre-download the embedding and reranker models into MODELS_CACHE_DIR.

CLI: python -m netracare.rag.models download   (dev.ps1 models / make models; CI caches the dir)
"""

from __future__ import annotations

import argparse
import sys
import time

from netracare.config import get_settings
from netracare.rag.embeddings import FastEmbedder, FastReranker


def download() -> None:
    settings = get_settings()
    started = time.perf_counter()
    embedder = FastEmbedder(settings.embedding_model, settings.models_cache_dir)
    vec = embedder.embed_query("warm-up")
    reranker = FastReranker(settings.reranker_model, settings.models_cache_dir)
    reranker.score("warm-up", ["warm-up"])
    print(
        f"models ready in {settings.models_cache_dir}: {settings.embedding_model} "
        f"(dim {len(vec)}), {settings.reranker_model} "
        f"[{time.perf_counter() - started:.1f}s]"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m netracare.rag.models")
    parser.add_argument("command", choices=["download"])
    parser.parse_args(argv)
    download()
    return 0


if __name__ == "__main__":
    sys.exit(main())
