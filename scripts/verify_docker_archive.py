"""Validate a daemonless Docker save archive produced for this project."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import tarfile
from pathlib import Path

APP_TREES = (
    ("extracted-app/src/recovery_service", "app/src/recovery_service"),
    ("extracted-app/scripts", "app/scripts"),
    ("extracted-app/tests", "app/tests"),
)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def expected_files(project_root: Path) -> dict[str, str]:
    expected: dict[str, str] = {}
    for relative, target in APP_TREES:
        source = project_root / relative
        for path in source.rglob("*"):
            if not path.is_file() or "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}:
                continue
            name = f"{target}/{path.relative_to(source).as_posix()}"
            expected[name] = digest(path.read_bytes())
    requirements = project_root / "extracted-app/requirements.txt"
    expected["app/requirements.txt"] = digest(requirements.read_bytes())
    return expected


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--repository", action="append", default=[])
    args = parser.parse_args()

    expected = expected_files(args.project_root.resolve())
    with tarfile.open(args.archive, "r:gz") as archive:
        names = set(archive.getnames())
        manifest = json.load(archive.extractfile("manifest.json"))
        repositories = json.load(archive.extractfile("repositories"))
        tags = [tag for entry in manifest for tag in entry["RepoTags"]]
        expected_count = len(args.repository) if args.repository else 11
        assert len(tags) == expected_count, f"expected {expected_count} tags, got {len(tags)}"
        assert all(tag.endswith(":" + args.tag) for tag in tags)
        if args.repository:
            assert {tag.rsplit(":", 1)[0] for tag in tags} == set(args.repository)

        results = []
        for entry in manifest:
            config_bytes = archive.extractfile(entry["Config"]).read()
            assert digest(config_bytes) == Path(entry["Config"]).stem
            config = json.loads(config_bytes)
            assert len(entry["Layers"]) == len(config["rootfs"]["diff_ids"])
            assert all(layer in names for layer in entry["Layers"])

            layer_file = archive.extractfile(entry["Layers"][-1])
            layer_bytes = layer_file.read()
            assert digest(layer_bytes) == config["rootfs"]["diff_ids"][-1].removeprefix("sha256:")
            with tarfile.open(fileobj=io.BytesIO(layer_bytes), mode="r:") as layer:
                actual = {
                    member.name: digest(layer.extractfile(member).read())
                    for member in layer.getmembers()
                    if member.isfile() and not Path(member.name).name.startswith(".wh.")
                }
            missing = sorted(set(expected) - set(actual))
            mismatched = sorted(name for name, value in expected.items() if actual.get(name) != value)
            assert not missing, f"missing current source files: {missing[:5]}"
            assert not mismatched, f"source hash mismatch: {mismatched[:5]}"
            assert "usr/local/lib/python3.10/site-packages/sqlglot/__init__.py" in actual

            env = {
                item.split("=", 1)[0]: item.split("=", 1)[1]
                for item in config["config"].get("Env", [])
                if "=" in item
            }
            for key in (
                "ORACLE_DOCKER_SSH_PASSWORD",
                "ORACLE_DOCKER_SUDO_PASSWORD",
                "SQLSERVER_DOCKER_SSH_PASSWORD",
                "SQLSERVER_DOCKER_SUDO_PASSWORD",
                "MYSQL_RESTORE_DOCKER_SSH_PASSWORD",
                "MYSQL_RESTORE_DOCKER_SUDO_PASSWORD",
                "OIDC_CLIENT_SECRET",
            ):
                assert env.get(key, "") == "", f"embedded credential is not empty: {key}"

            top_layer = Path(entry["Layers"][-1]).parts[0]
            for repo_tag in entry["RepoTags"]:
                repository, tag = repo_tag.rsplit(":", 1)
                assert repositories[repository][tag] == top_layer
            results.append(
                {
                    "tags": entry["RepoTags"],
                    "config": Path(entry["Config"]).stem,
                    "top_layer": top_layer,
                    "source_files": len(expected),
                }
            )

    print(json.dumps({"archive": str(args.archive), "verified_images": results}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
