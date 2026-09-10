"""Append the current application tree to a Docker save archive without a daemon.

This is a packaging fallback for Windows workstations where Docker Desktop is
unavailable. It preserves the baseline image layers and adds one deterministic
application layer to the API and Worker images.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import shutil
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

APP_TREES = (
    ("extracted-app/src/recovery_service", "app/src/recovery_service"),
    ("extracted-app/scripts", "app/scripts"),
    ("extracted-app/tests", "app/tests"),
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalized(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
    name = PurePosixPath(info.name).as_posix()
    if "__pycache__" in PurePosixPath(name).parts or name.endswith((".pyc", ".pyo")):
        return None
    info.name = name
    info.uid = info.gid = 0
    info.uname = info.gname = "root"
    info.mtime = 0
    if info.isdir() or name.startswith("app/scripts/") and name.endswith(".sh"):
        info.mode = 0o755
    else:
        info.mode = 0o644
    return info


def current_files(root: Path, relative: str) -> set[str]:
    base = root / relative
    if not base.exists():
        return set()
    return {
        path.relative_to(base).as_posix()
        for path in base.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix not in {".pyc", ".pyo"}
    }


def add_whiteouts(
    archive: tarfile.TarFile,
    project_root: Path,
    baseline_root: Path,
) -> None:
    for relative, target in APP_TREES:
        previous = current_files(baseline_root, relative)
        current = current_files(project_root, relative)
        for missing in sorted(previous - current):
            target_path = PurePosixPath(target) / missing
            whiteout = target_path.parent / (".wh." + target_path.name)
            info = tarfile.TarInfo(whiteout.as_posix())
            info.size = 0
            info.mode = 0o000
            info.uid = info.gid = 0
            info.uname = info.gname = "root"
            info.mtime = 0
            archive.addfile(info)


def create_update_layer(
    layer_path: Path,
    project_root: Path,
    baseline_root: Path,
    site_packages: Path,
) -> None:
    with tarfile.open(layer_path, "w", format=tarfile.PAX_FORMAT) as archive:
        add_whiteouts(archive, project_root, baseline_root)
        for relative, target in APP_TREES:
            archive.add(project_root / relative, arcname=target, filter=normalized)
        archive.add(
            project_root / "extracted-app/requirements.txt",
            arcname="app/requirements.txt",
            filter=normalized,
        )
        for dependency in ("sqlglot", "sqlglot-29.0.1.dist-info"):
            archive.add(
                site_packages / dependency,
                arcname=f"usr/local/lib/python3.10/site-packages/{dependency}",
                filter=normalized,
            )


def parse_env_example(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value.strip("'\"")
    return values


def safe_environment(existing: list[str], package_env: dict[str, str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in existing:
        key = item.split("=", 1)[0]
        value = package_env.get(key, item.split("=", 1)[1] if "=" in item else "")
        result.append(f"{key}={value}")
        seen.add(key)
    for key in (
        "OIDC_ENABLED",
        "OIDC_PROVIDER_NAME",
        "OIDC_ISSUER_URL",
        "OIDC_DISCOVERY_URL",
        "OIDC_CLIENT_ID",
        "OIDC_CLIENT_SECRET",
        "OIDC_REDIRECT_URI",
        "OIDC_POST_LOGOUT_REDIRECT_URI",
        "OIDC_SCOPES",
        "OIDC_AUTO_PROVISION_USERS",
        "OIDC_COOKIE_SECURE",
    ):
        if key in package_env and key not in seen:
            result.append(f"{key}={package_env[key]}")
    return result


def update_image(
    image_root: Path,
    entry: dict,
    layer_path: Path,
    diff_id: str,
    tag: str,
    created: str,
    package_env: dict[str, str],
) -> tuple[dict, str, str]:
    old_config_path = image_root / entry["Config"]
    config = json.loads(old_config_path.read_text(encoding="utf-8"))
    previous_layer_id = Path(entry["Layers"][-1]).parts[0]
    new_layer_id = hashlib.sha256(f"{previous_layer_id}:{diff_id}".encode()).hexdigest()
    layer_dir = image_root / new_layer_id
    layer_dir.mkdir()
    shutil.copyfile(layer_path, layer_dir / "layer.tar")
    (layer_dir / "VERSION").write_text("1.0", encoding="ascii")

    layer_metadata = {
        "id": new_layer_id,
        "parent": previous_layer_id,
        "created": created,
        "container_config": config.get("container_config", config.get("config", {})),
    }
    (layer_dir / "json").write_text(
        json.dumps(layer_metadata, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )

    config["created"] = created
    config["rootfs"]["diff_ids"].append(f"sha256:{diff_id}")
    config.setdefault("history", []).append(
        {
            "created": created,
            "created_by": "COPY current application source and sqlglot 29.0.1",
        }
    )
    for section in ("config", "container_config"):
        if section in config and isinstance(config[section].get("Env"), list):
            config[section]["Env"] = safe_environment(config[section]["Env"], package_env)

    config_bytes = json.dumps(config, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    config_digest = hashlib.sha256(config_bytes).hexdigest()
    (image_root / f"{config_digest}.json").write_bytes(config_bytes)

    entry["Config"] = f"{config_digest}.json"
    entry["Layers"].append(f"{new_layer_id}/layer.tar")
    entry["RepoTags"] = [repo.rsplit(":", 1)[0] + ":" + tag for repo in entry["RepoTags"]]
    return entry, config_digest, new_layer_id


def create_archive(source: Path, destination: Path) -> None:
    with (
        destination.open("wb") as raw,
        gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0, compresslevel=6) as compressed,
        tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive,
    ):
        for path in sorted(source.rglob("*"), key=lambda value: value.as_posix()):
            archive.add(path, arcname=path.relative_to(source).as_posix(), recursive=False)


def select_images(image_root: Path, manifest: list[dict], repositories: list[str]) -> list[dict]:
    if not repositories:
        return manifest
    selected = [
        entry
        for entry in manifest
        if any(tag.rsplit(":", 1)[0] in repositories for tag in entry.get("RepoTags", []))
    ]
    missing = sorted(set(repositories) - {
        tag.rsplit(":", 1)[0]
        for entry in selected
        for tag in entry.get("RepoTags", [])
    })
    if missing:
        raise ValueError(f"repositories not found in base archive: {', '.join(missing)}")

    for entry in selected:
        entry["RepoTags"] = [
            tag for tag in entry["RepoTags"] if tag.rsplit(":", 1)[0] in repositories
        ]

    keep = {"manifest.json", "repositories"}
    keep.update(entry["Config"] for entry in selected)
    keep.update(Path(layer).parts[0] for entry in selected for layer in entry["Layers"])
    for path in image_root.iterdir():
        if path.name not in keep:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
    return selected


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-archive", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--baseline-root", type=Path, required=True)
    parser.add_argument("--site-packages", type=Path, required=True)
    parser.add_argument("--env-example", type=Path, required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--repository", action="append", default=[])
    args = parser.parse_args()

    created = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    with tempfile.TemporaryDirectory(prefix="docker-archive-rebase-") as temporary:
        image_root = Path(temporary) / "image"
        image_root.mkdir()
        with tarfile.open(args.base_archive, "r:gz") as archive:
            archive.extractall(image_root, filter="data")

        layer_path = Path(temporary) / "application-layer.tar"
        create_update_layer(
            layer_path,
            args.project_root.resolve(),
            args.baseline_root.resolve(),
            args.site_packages.resolve(),
        )
        diff_id = sha256_file(layer_path)
        package_env = parse_env_example(args.env_example)
        manifest = json.loads((image_root / "manifest.json").read_text(encoding="utf-8"))
        manifest = select_images(image_root, manifest, args.repository)
        repositories: dict[str, dict[str, str]] = {}
        image_results = []
        for entry in manifest:
            entry, config_digest, layer_id = update_image(
                image_root,
                entry,
                layer_path,
                diff_id,
                args.tag,
                created,
                package_env,
            )
            image_results.append((entry, config_digest, layer_id))
            for repo_tag in entry["RepoTags"]:
                repository, tag = repo_tag.rsplit(":", 1)
                repositories.setdefault(repository, {})[tag] = layer_id

        (image_root / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        (image_root / "repositories").write_text(
            json.dumps(repositories, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        create_archive(image_root, args.output)
        print(json.dumps({
            "archive": str(args.output),
            "sha256": sha256_file(args.output),
            "layer_diff_id": diff_id,
            "images": [
                {"tags": entry["RepoTags"], "config": config_digest, "top_layer": layer_id}
                for entry, config_digest, layer_id in image_results
            ],
        }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
