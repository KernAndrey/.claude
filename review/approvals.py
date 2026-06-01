"""Canonical diff hash + pre-review approval markers.

The commit-time fast-path (`hook.main`) skips the slow LLM review when the
staged diff was already pre-reviewed CLEAN. The signal is a marker file
``<repo>/.review/approvals/<diff_hash>``: its presence == "this exact diff
passed review". The file is empty; the hash *is* the content.

`diff_hash` is the single source of truth for the hash format — the ``sha256``
of the stripped ``git diff --cached``. Compute the hash through this function
rather than re-implementing the digest, so the format stays consistent across
callers; a mismatch would make the fast-path silently miss.

No HMAC / signing: the Workflow JS sandbox that owns the integrity check has
no ``crypto`` and cannot verify a signature, so a signature here would buy
nothing. Integrity is enforced upstream — the workflow reviews and audits
through *separate* agents, and the committer never produces the audit
evidence. This module only provides the fast/cheap presence channel.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

# A canonical diff hash is exactly a sha256 hex digest. Validating against this
# before touching the filesystem keeps a malformed/hostile key (e.g. a path
# fragment) from ever escaping the approvals directory.
_HASH_RE = re.compile(r"\A[0-9a-f]{64}\Z")


def diff_hash(diff_text: str) -> str:
    """Canonical content hash of a staged diff.

    ``diff_text`` must be the stripped output of ``git diff --cached`` (the
    form produced by ``hook.get_staged_diff``); the same normalization is used
    everywhere this hash is computed.
    """
    return hashlib.sha256(diff_text.encode()).hexdigest()


def approvals_dir(repo_root: Path) -> Path:
    """Directory holding the pre-review approval markers for ``repo_root``."""
    return repo_root / ".review" / "approvals"


def write_approval(repo_root: Path, hash_hex: str) -> None:
    """Record that the diff with hash ``hash_hex`` passed pre-review.

    Idempotent. Raises ``ValueError`` if ``hash_hex`` is not a canonical
    sha256 digest — only genuine diff hashes may become markers.
    """
    if not _HASH_RE.match(hash_hex):
        raise ValueError(f"not a canonical diff hash: {hash_hex!r}")
    d = approvals_dir(repo_root)
    d.mkdir(parents=True, exist_ok=True)
    (d / hash_hex).touch()


def approval_exists(repo_root: Path, hash_hex: str) -> bool:
    """True iff a CLEAN pre-review marker exists for ``hash_hex``.

    Returns ``False`` for any non-canonical key (never resolves outside the
    approvals directory) so a malformed hash fails safe to "review".
    """
    if not _HASH_RE.match(hash_hex):
        return False
    return (approvals_dir(repo_root) / hash_hex).is_file()


def clear_approvals(repo_root: Path) -> None:
    """Remove every approval marker. No-op when the directory is absent.

    Called at the start *and* end of the workflow Land phase so a crashed
    prior run cannot leave a stale marker the fast-path would honor.
    """
    d = approvals_dir(repo_root)
    if not d.is_dir():
        return
    for marker in d.iterdir():
        if marker.is_file():
            marker.unlink()
