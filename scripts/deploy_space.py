"""Create and update the Hugging Face Space that serves this project.

Why a script and not a list of git commands. The git route - force-add the
gitignored bundle, push to a Space remote - puts a 14.7 MB build artefact into
a branch of this repository, and the obvious spelling of it commits that onto
``main``, because a commit belongs to a branch and not to a remote. The Hub
API uploads files without any of that: nothing is committed here, ``main``
never carries the bundle, and the Space's README is the short page in
``deploy/space-README.md`` rather than this repository's.

It never handles the token itself. ``huggingface_hub`` reads the credential
stored by ``hf auth login`` (or ``HF_TOKEN``), so the value is never passed on
a command line, written to a file, or printed here.

Usage::

    python -u scripts/deploy_space.py <user>/<space-name> [--private] [--dry-run]

``--dry-run`` lists exactly what would be uploaded and contacts nothing.
Re-running it updates the Space in place, which is also how to ship a new
schedule revision: rebuild the bundle, then run this again.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / "data" / "deploy"
SPACE_PAGE = ROOT / "deploy" / "space-README.md"

#: Only what the image needs to build and run. An allow-list rather than an
#: ignore-list: a new top-level directory in this repository should not reach a
#: public Space because nobody remembered to exclude it.
UPLOAD = (
    "Dockerfile",
    "pyproject.toml",
    ".dockerignore",
    "src",
    "scripts",
    "data/manifests",
    "data/deploy",
)

#: Never, under any pattern above. ``.env`` is gitignored and so is not in a
#: git push, but this path does not go through git at all, so it is excluded
#: explicitly rather than by inheritance.
NEVER = ("*.env", ".env", ".env.*", "*.pyc", "__pycache__/*", "*.key", "*.pem")


def say(line: str) -> None:
    sys.stdout.buffer.write((line + "\n").encode("utf-8", "replace"))
    sys.stdout.flush()


def collect() -> list[tuple[Path, str]]:
    """Every file to upload, as (local path, path in the Space)."""
    out: list[tuple[Path, str]] = []
    for entry in UPLOAD:
        local = ROOT / entry
        if not local.exists():
            continue
        if local.is_file():
            out.append((local, entry))
            continue
        for path in sorted(local.rglob("*")):
            if not path.is_file():
                continue
            name = path.name
            relative = path.relative_to(ROOT).as_posix()
            if any(part == "__pycache__" for part in path.parts):
                continue
            if name.endswith(".pyc") or name == ".env" or name.startswith(".env."):
                continue
            out.append((path, relative))
    return out


def main() -> int:
    args = sys.argv[1:]
    private = "--private" in args
    dry_run = "--dry-run" in args
    positional = [a for a in args if not a.startswith("-")]

    if len(positional) != 1 or "/" not in positional[0]:
        say("usage: python -u scripts/deploy_space.py <user>/<space> [--private] [--dry-run]")
        return 2
    repo_id = positional[0]

    if not (BUNDLE / "bundle.json").exists():
        say(f"no bundle at {BUNDLE.relative_to(ROOT)}")
        say("build it first: python -u scripts/build_deploy_bundle.py")
        return 1
    if not SPACE_PAGE.exists():
        say(f"missing {SPACE_PAGE.relative_to(ROOT)}, which becomes the Space README")
        return 1

    files = collect()
    total = sum(path.stat().st_size for path, _ in files)
    say(f"{len(files)} files, {total / 1_000_000:.1f} MB")
    for path, name in files:
        if path.parent == BUNDLE or path.stat().st_size > 200_000:
            say(f"  {name}  {path.stat().st_size / 1_000_000:.2f} MB")
    say(f"  README.md  <- {SPACE_PAGE.relative_to(ROOT).as_posix()}")

    leaked = [name for _, name in files if name.endswith(".env") or "/.env" in name]
    if leaked:
        say(f"refusing to upload: {leaked}")
        return 1

    if dry_run:
        say("")
        say(f"dry run: nothing uploaded, {repo_id} not contacted")
        return 0

    from huggingface_hub import HfApi
    from huggingface_hub.errors import HfHubHTTPError
    from huggingface_hub.utils import LocalTokenNotFoundError

    api = HfApi()
    try:
        who = api.whoami()
    except (LocalTokenNotFoundError, HfHubHTTPError, OSError):
        say("not logged in to Hugging Face.")
        say("run this yourself, so the token stays yours:")
        say('  .\\.venv\\Scripts\\python.exe -c "from huggingface_hub.cli.hf '
            'import main; main()" auth login')
        return 1
    say(f"logged in as {who.get('name')}")

    api.create_repo(
        repo_id=repo_id,
        repo_type="space",
        space_sdk="docker",
        private=private,
        exist_ok=True,
    )
    say(f"space {repo_id} ready ({'private' if private else 'public'})")

    # The short page, as the Space's README: its YAML frontmatter is what tells
    # the Hub to build the Dockerfile and which port to expose.
    api.upload_file(
        path_or_fileobj=str(SPACE_PAGE),
        path_in_repo="README.md",
        repo_id=repo_id,
        repo_type="space",
        commit_message="Space page and build configuration",
    )

    api.upload_folder(
        folder_path=str(ROOT),
        repo_id=repo_id,
        repo_type="space",
        allow_patterns=[f"{e}/**" if (ROOT / e).is_dir() else e for e in UPLOAD],
        ignore_patterns=list(NEVER),
        commit_message="Service, bundle and Dockerfile",
    )

    page = f"https://huggingface.co/spaces/{repo_id}"
    # The Space's own host, which is what answers /health and /classify. The
    # Hub builds it from owner and name, lowercased, with anything that is not
    # alphanumeric turned into a hyphen.
    host = "".join(c if c.isalnum() else "-" for c in repo_id.lower())
    direct = f"https://{host}.hf.space"
    say("")
    say(f"uploaded. the Space is building the Dockerfile: {page}")
    say(f"once it is running, the service is at {direct}")
    say(f"score it the same way as the container: "
        f"python -u scripts/measure_served.py {direct}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
