"""與私有 Hugging Face dataset 同步。

pull：下載 meta、quality、curated（可限年份），raw 預設不下載（只上傳、不需要回讀）。
push：只上傳本次執行後新增或修改的檔案，分批 commit。
需要環境變數 HF_TOKEN（寫入權限）與 TWCHIP_HF_REPO（例如 yourname/twchip-data）。
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

from .config import Config


def _api():
    from huggingface_hub import HfApi
    return HfApi()


def ensure_repo(cfg: Config) -> None:
    _api().create_repo(cfg.hf_repo, repo_type="dataset", private=True, exist_ok=True)


def pull(cfg: Config, years: Optional[list[int]] = None, include_raw: bool = False) -> Path:
    from huggingface_hub import snapshot_download
    pats = ["meta/*", "legacy/*"]
    if years:
        for y in years:
            pats += [f"curated/*/*/{y}/*", f"quality/{y}/*"]
    else:
        pats += ["curated/**", "quality/**"]
    if include_raw:
        pats.append("raw/**")
    from huggingface_hub.utils import RepositoryNotFoundError
    try:
        snapshot_download(cfg.hf_repo, repo_type="dataset", local_dir=str(cfg.data_dir), allow_patterns=pats)
    except RepositoryNotFoundError:
        ensure_repo(cfg)  # 第一次執行：建立私有 dataset，本機從空的開始
    return cfg.data_dir


def changed_since(cfg: Config, since_epoch: float) -> list[Path]:
    out = []
    for p in cfg.data_dir.rglob("*"):
        if p.is_file() and not p.name.endswith(".tmp") and ".cache" not in p.parts \
                and p.stat().st_mtime >= since_epoch:
            out.append(p)
    return sorted(out)


def push(cfg: Config, files: list[Path], message: str, batch: int = 400) -> int:
    from huggingface_hub import CommitOperationAdd
    api = _api()
    ops = [CommitOperationAdd(path_in_repo=str(p.relative_to(cfg.data_dir)), path_or_fileobj=str(p)) for p in files]
    for i in range(0, len(ops), batch):
        for attempt in range(3):
            try:
                api.create_commit(cfg.hf_repo, repo_type="dataset", operations=ops[i:i + batch],
                                  commit_message=f"{message} ({i // batch + 1}/{(len(ops) - 1) // batch + 1})")
                break
            except Exception:  # noqa: BLE001
                if attempt == 2:
                    raise
                time.sleep(10 * (attempt + 1))
    return len(ops)
