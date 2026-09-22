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

    Retained as a log/back-compat field only. The fast-path marker, the
    commit-time match, and the post-Land audit all key on :func:`content_hash`
    instead — a textual diff hash is base-dependent (it embeds the pre-image
    blob and diff formatting), so a diff that is byte-identical in *content* but
    reconstructed from a different base would miss. See ``pre_review`` and
    ``hook._maybe_fastpath``.
    """
    return hashlib.sha256(diff_text.encode()).hexdigest()


def content_hash(entries: dict[str, str]) -> str:
    """Canonical content key of a staged group: ``sha256`` of its file contents.

    ``entries`` maps each changed path to its full (40-hex) staged blob sha, or
    the sentinel ``"deleted"`` for a removed path. This is the single source of
    truth for the content key — the pre-review write side, the commit-time match
    side, and the post-Land audit must all build ``entries`` the same way (via
    ``git diff --name-status --no-renames`` + ``git rev-parse``) and hash through
    here, so the three sides cannot drift.

    Unlike :func:`diff_hash` this is base-INDEPENDENT: it depends only on the
    final content of the group's files, not on the pre-image or diff text. Two
    commits with identical final bytes therefore share a key even if their diffs
    differ (different base, stash reconstruction, rename detection) — which is
    exactly when the textual hash silently missed.
    """
    canonical = "\n".join(f"{path}\0{entries[path]}" for path in sorted(entries))
    return hashlib.sha256(canonical.encode()).hexdigest()


def entries_from_raw(raw_z: str) -> dict[str, str]:
    """Parse ``git diff --raw --full-index -z --no-renames`` into {path -> key}.

    Each NUL-separated record is ``:<srcmode> <dstmode> <srcsha> <dstsha> <status>``
    followed by the literal path. The value kept is the *destination* (post-image)
    blob sha — the final content — or the sentinel ``"deleted"`` for a removed
    path. ``--full-index`` guarantees the full 40-hex sha (raw output abbreviates
    by default); ``--no-renames`` keeps a rename as delete+add so the key depends
    only on final content. Lives here (not in a caller) so the pre-review write
    side, the commit-time match side, and the audit all parse identically.
    """
    toks = raw_z.split("\0")
    entries: dict[str, str] = {}
    i = 0
    while i + 1 < len(toks):
        meta, path = toks[i], toks[i + 1]
        i += 2
        if not meta or not path:
            continue
        parts = meta.split()
        status = parts[-1] if parts else ""
        dst_sha = parts[3] if len(parts) >= 4 else ""
        entries[path] = "deleted" if status.startswith("D") else dst_sha
    return entries


def content_hash_from_raw(raw_z: str) -> str:
    """Content key straight from raw git diff output — parse + hash, single-sourced.

    Every side (pre-review write, commit-time match, post-Land audit) runs its
    own ``git diff --raw --full-index -z --no-renames`` (the selector differs:
    ``--cached`` vs a ``C~1 C`` range) and passes the raw stdout here, so the key
    is computed one way only and the three sides cannot drift.
    """
    return content_hash(entries_from_raw(raw_z))


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
