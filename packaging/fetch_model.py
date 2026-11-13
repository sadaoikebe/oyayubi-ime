"""Download the LM model at a pinned revision (docs/INSTALL.md).

Run with the installation's own Python (the one that will run the
server), so the only network dependency is HuggingFace:

  python packaging/fetch_model.py <dest-dir>

The revision is a full commit hash: what gets installed never floats
with the upstream repository.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ID = "rinna/japanese-gpt2-small"
REVISION = "f7fdefe2941d9629a7b2894564435e0e035df6a6"


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    dest = Path(sys.argv[1])
    from huggingface_hub import snapshot_download

    snapshot_download(repo_id=REPO_ID, revision=REVISION, local_dir=dest)
    print(f"model {REPO_ID}@{REVISION[:8]} -> {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
