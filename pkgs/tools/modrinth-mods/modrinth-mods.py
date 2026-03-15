#!/usr/bin/env python3
"""
Manage Modrinth mods declared in a TOML manifest and a JSON lock file.

Manifest format (mods.toml):

    [options]
    minecraft = "1.21.1"
    loader = "fabric"

    loader-version = "0.18.4"  # optional, used for mrpack export

    [mods]
    fabric-api = "*"
    lithium = "*"
    ferritecore = "6.0.0"

Lock file format (mods.lock.json):

    {
      "fabric-api": {
        "version": "0.115.0+1.21.1",
        "versionId": "9YVrKY0Z",
        "projectId": "P7dR8mSH",
        "url": "https://cdn.modrinth.com/data/...",
        "hash": "sha512-...",
        "filename": "fabric-api-0.115.0+1.21.1.jar"
      }
    }
"""

import argparse
import base64
import json
import re
import sys
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, NoReturn, cast

API_BASE = "https://api.modrinth.com/v2"
USER_AGENT = "skulldogged/nix-minecraft (nix-minecraft mod manager)"

JSONDict = dict[str, Any]

PROJECT_CACHE: dict[str, JSONDict] = {}
VERSION_CACHE: dict[str, JSONDict] = {}
VERSION_LIST_CACHE: dict[tuple[str, str, str], list[JSONDict]] = {}

LOADER_DEPENDENCY_KEYS = {
    "fabric": "fabric-loader",
    "legacy-fabric": "fabric-loader",
    "quilt": "quilt-loader",
    "forge": "forge",
    "neoforge": "neoforge",
}


def fatal(message: str) -> NoReturn:
    print(f"Error: {message}", file=sys.stderr)
    raise SystemExit(1)


def api_get(path: str, params: dict[str, str] | None = None) -> Any:
    url = f"{API_BASE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)

    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as err:
        body = err.read().decode(errors="replace")
        print(
            f"Error: API request to {url} failed: {err.code} {err.reason}",
            file=sys.stderr,
        )
        print(f"  Response: {body}", file=sys.stderr)
        raise SystemExit(1)


def parse_project_slug(value: str) -> str:
    match = re.match(r"https?://modrinth\.com/[^/]+/([^/?#]+)", value)
    if match:
        return match.group(1)
    return value


def expect_dict(value: Any, context: str) -> JSONDict:
    if not isinstance(value, dict):
        fatal(f"Unexpected API response for {context}.")
    return cast(JSONDict, value)


def expect_list(value: Any, context: str) -> list[JSONDict]:
    if not isinstance(value, list):
        fatal(f"Unexpected API response for {context}.")
    return [expect_dict(item, context) for item in value]


def fetch_project(project_ref: str) -> JSONDict:
    project_ref = parse_project_slug(project_ref)
    cached = PROJECT_CACHE.get(project_ref)
    if cached is not None:
        return cached

    project = expect_dict(
        api_get(f"/project/{urllib.parse.quote(project_ref, safe='')}"),
        f"project {project_ref}",
    )
    PROJECT_CACHE[project_ref] = project
    PROJECT_CACHE[project["id"]] = project
    if project.get("slug"):
        PROJECT_CACHE[project["slug"]] = project
    return project


def fetch_version(version_id: str) -> JSONDict:
    cached = VERSION_CACHE.get(version_id)
    if cached is not None:
        return cached

    version = expect_dict(
        api_get(f"/version/{urllib.parse.quote(version_id, safe='')}"),
        f"version {version_id}",
    )
    VERSION_CACHE[version_id] = version
    return version


def fetch_project_versions(
    project_ref: str, minecraft: str, loader: str
) -> list[JSONDict]:
    project_ref = parse_project_slug(project_ref)
    cache_key = (project_ref, minecraft, loader)
    cached = VERSION_LIST_CACHE.get(cache_key)
    if cached is not None:
        return cached

    versions = expect_list(
        api_get(
            f"/project/{urllib.parse.quote(project_ref, safe='')}/version",
            {
                "loaders": json.dumps([loader]),
                "game_versions": json.dumps([minecraft]),
            },
        ),
        f"project versions for {project_ref}",
    )

    for version in versions:
        VERSION_CACHE[version["id"]] = version

    VERSION_LIST_CACHE[cache_key] = versions
    return versions


def lock_entry_from_version(version_data: JSONDict) -> JSONDict:
    files = version_data["files"]
    primary = next((file for file in files if file["primary"]), files[0])

    sha512_hex = primary["hashes"]["sha512"]
    sha512_bytes = bytes.fromhex(sha512_hex)
    sri_hash = "sha512-" + base64.b64encode(sha512_bytes).decode()

    return {
        "version": version_data["version_number"],
        "versionId": version_data["id"],
        "projectId": version_data["project_id"],
        "url": primary["url"],
        "hash": sri_hash,
        "filename": primary["filename"],
    }


def find_version_file(
    version_data: JSONDict, lock_entry: JSONDict | None = None
) -> JSONDict:
    files = cast(list[JSONDict], version_data["files"])
    if lock_entry is not None:
        for file in files:
            if file.get("filename") == lock_entry.get("filename"):
                return file
        for file in files:
            if file.get("url") == lock_entry.get("url"):
                return file

    primary = next((file for file in files if file["primary"]), None)
    if primary is None:
        fatal(f"Version {version_data['id']} does not contain any downloadable files.")
    return primary


def select_version(
    versions: list[JSONDict], project_ref: str, version_constraint: str
) -> JSONDict:
    if not versions:
        fatal(f"No compatible versions found for '{project_ref}'.")

    if version_constraint != "*":
        matching = [
            version
            for version in versions
            if version["version_number"] == version_constraint
        ]
        if matching:
            return matching[0]

        available = ", ".join(version["version_number"] for version in versions[:10])
        suffix = "..." if len(versions) > 10 else ""
        fatal(
            f"No version '{version_constraint}' found for '{project_ref}'. "
            f"Available: {available}{suffix}"
        )

    releases = [version for version in versions if version["version_type"] == "release"]
    return releases[0] if releases else versions[0]


def resolve_version(
    project_ref: str, version_constraint: str, minecraft: str, loader: str
) -> JSONDict:
    versions = fetch_project_versions(project_ref, minecraft, loader)
    return select_version(versions, project_ref, version_constraint)


def read_manifest(path: Path) -> tuple[str, str, str | None, dict[str, str]]:
    if not path.exists():
        print(f"Error: Manifest file '{path}' not found.", file=sys.stderr)
        print("", file=sys.stderr)
        print("Create a manifest with:", file=sys.stderr)
        print("", file=sys.stderr)
        print(
            '[options]\nminecraft = "1.21.1"\nloader = "fabric"\nloader-version = "0.18.4"\n\n'
            '[mods]\nfabric-api = "*"\nlithium = "*"',
            file=sys.stderr,
        )
        print("", file=sys.stderr)
        print(
            "Or import from a Modrinth modpack with `modrinth-mods import-modpack <slug-or-url>`.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    with open(path, "rb") as handle:
        manifest = tomllib.load(handle)

    options = manifest.get("options", {})
    minecraft = options.get("minecraft")
    loader = options.get("loader")
    loader_version = options.get("loader-version", options.get("loader_version"))
    if not minecraft or not loader:
        fatal("Manifest must define [options].minecraft and [options].loader.")

    mods = manifest.get("mods", {})
    normalized_mods = {slug: str(constraint) for slug, constraint in mods.items()}
    return (
        str(minecraft),
        str(loader),
        str(loader_version) if loader_version else None,
        normalized_mods,
    )


def write_manifest(
    path: Path,
    minecraft: str,
    loader: str,
    loader_version: str | None,
    mods: dict[str, str],
):
    lines = [
        "[options]",
        f'minecraft = "{minecraft}"',
        f'loader = "{loader}"',
    ]

    if loader_version:
        lines.append(f'loader-version = "{loader_version}"')

    lines.extend(["", "[mods]"])

    for slug in sorted(mods):
        lines.append(f'{slug} = "{mods[slug]}"')

    lines.append("")
    with open(path, "w") as handle:
        handle.write("\n".join(lines))


def read_lock(path: Path) -> dict[str, JSONDict]:
    if not path.exists():
        return {}

    with open(path) as handle:
        return cast(dict[str, JSONDict], json.load(handle))


def write_lock(path: Path, lock: dict[str, JSONDict]):
    with open(path, "w") as handle:
        json.dump(dict(sorted(lock.items())), handle, indent=2)
        handle.write("\n")


def prompt_yes_no(question: str, default: bool = False) -> bool:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print(
            f"  Skipping: {question} [non-interactive default: {'yes' if default else 'no'}]"
        )
        return default

    suffix = "[Y/n]" if default else "[y/N]"
    while True:
        reply = input(f"{question} {suffix} ").strip().lower()
        if not reply:
            return default
        if reply in {"y", "yes"}:
            return True
        if reply in {"n", "no"}:
            return False
        print("Please answer 'y' or 'n'.")


def dependency_constraint(version_data: JSONDict | None) -> str:
    if version_data is None:
        return "*"
    return str(version_data["version_number"])


def maybe_add_mod(
    slug: str,
    constraint: str,
    version_data: JSONDict,
    source: str,
    planned: dict[str, JSONDict],
    queue: list[str],
    manifest_mods: dict[str, str],
    lock: dict[str, JSONDict],
    missing_lock: set[str],
) -> bool:
    if slug in manifest_mods:
        if slug not in lock:
            missing_lock.add(slug)
        print(f"  Keeping existing manifest entry for {slug}.")
        return False

    existing = planned.get(slug)
    if existing is not None:
        if existing["constraint"] == constraint:
            return False
        if existing["constraint"] != version_data["version_number"]:
            print(
                f"  Keeping existing planned version for {slug} ({existing['constraint']}) over {constraint}."
            )
            return False
        existing["constraint"] = constraint
        existing["version_data"] = version_data
        return False

    planned[slug] = {
        "constraint": constraint,
        "version_data": version_data,
        "source": source,
    }
    queue.append(slug)
    print(f"  Added {slug} ({version_data['version_number']}) from {source}.")
    return True


def process_dependencies(
    slug: str,
    version_data: JSONDict,
    minecraft: str,
    loader: str,
    planned: dict[str, JSONDict],
    queue: list[str],
    manifest_mods: dict[str, str],
    lock: dict[str, JSONDict],
    missing_lock: set[str],
    optional_answers: dict[str, bool],
):
    for dependency in version_data.get("dependencies", []):
        dependency_type = dependency.get("dependency_type")
        if dependency_type not in {"required", "optional"}:
            continue

        dependency_version = None
        dependency_version_id = dependency.get("version_id")
        if dependency_version_id:
            dependency_version = fetch_version(dependency_version_id)

        project_id = dependency.get("project_id")
        if project_id is None and dependency_version is not None:
            project_id = dependency_version.get("project_id")
        if project_id is None:
            continue

        project = fetch_project(project_id)
        dependency_slug = project["slug"]
        if project["project_type"] != "mod":
            print(
                f"  Skipping {dependency_slug}: dependency is a {project['project_type']}."
            )
            continue

        if (
            dependency_type == "optional"
            and project.get("server_side") == "unsupported"
        ):
            print(f"  Skipping optional client-only dependency {dependency_slug}.")
            continue

        if dependency_slug in manifest_mods:
            if dependency_slug not in lock:
                missing_lock.add(dependency_slug)
            print(
                f"  Optional dependency {dependency_slug} already present; skipping prompt."
            )
            continue

        if dependency_slug in planned:
            continue

        if dependency_type == "optional":
            answer = optional_answers.get(dependency_slug)
            if answer is None:
                title = project.get("title") or dependency_slug
                answer = prompt_yes_no(
                    f"Add optional dependency {dependency_slug} ({title}) for {slug}?"
                )
                optional_answers[dependency_slug] = answer
            if not answer:
                continue

        if dependency_version is None:
            dependency_version = resolve_version(
                dependency_slug, "*", minecraft, loader
            )

        maybe_add_mod(
            dependency_slug,
            dependency_constraint(dependency_version),
            dependency_version,
            f"{dependency_type} dependency of {slug}",
            planned,
            queue,
            manifest_mods,
            lock,
            missing_lock,
        )


def cmd_update(manifest_path: Path, lock_path: Path, requested_slugs: list[str]):
    minecraft, loader, loader_version, manifest_mods = read_manifest(manifest_path)
    old_lock = read_lock(lock_path)

    if requested_slugs:
        target_slugs = [parse_project_slug(slug) for slug in requested_slugs]
        missing = sorted({slug for slug in target_slugs if slug not in manifest_mods})
        if missing:
            fatal(
                f"These mods are not present in {manifest_path}: {', '.join(missing)}"
            )
        lock = dict(old_lock)
    else:
        target_slugs = sorted(manifest_mods)
        lock = {}

    if not target_slugs:
        print("Warning: No mods declared in manifest.", file=sys.stderr)

    for slug in target_slugs:
        constraint = manifest_mods[slug]
        old_version = old_lock.get(slug, {}).get("version")

        print(f"  Resolving {slug} ({constraint})...", end=" ", flush=True)
        version_data = resolve_version(slug, "*", minecraft, loader)
        lock[slug] = lock_entry_from_version(version_data)
        manifest_mods[slug] = str(version_data["version_number"])

        new_version = lock[slug]["version"]
        if old_version and old_version != new_version:
            print(f"{old_version} -> {new_version}")
        elif old_version:
            print(f"{new_version} (unchanged)")
        else:
            print(new_version)

    for slug in sorted(manifest_mods):
        if slug in lock:
            continue
        if slug in old_lock:
            lock[slug] = old_lock[slug]
            continue

        constraint = manifest_mods[slug]
        print(
            f"  Resolving missing lock entry for {slug} ({constraint})...",
            end=" ",
            flush=True,
        )
        version_data = resolve_version(slug, constraint, minecraft, loader)
        lock[slug] = lock_entry_from_version(version_data)
        print(lock[slug]["version"])

    write_manifest(manifest_path, minecraft, loader, loader_version, manifest_mods)
    write_lock(lock_path, lock)
    print(f"\nWrote {manifest_path} with {len(manifest_mods)} mod(s).")
    print(f"\nWrote {lock_path} with {len(lock)} mod(s).")


def cmd_add(manifest_path: Path, lock_path: Path, requested_slugs: list[str]):
    minecraft, loader, loader_version, manifest_mods = read_manifest(manifest_path)
    lock = read_lock(lock_path)

    slugs = []
    seen = set()
    for value in requested_slugs:
        slug = parse_project_slug(value)
        if slug not in seen:
            slugs.append(slug)
            seen.add(slug)

    planned: dict[str, dict] = {}
    queue: list[str] = []
    missing_lock: set[str] = set()
    optional_answers: dict[str, bool] = {}

    for slug in slugs:
        project = fetch_project(slug)
        if project["project_type"] != "mod":
            fatal(f"'{slug}' is a {project['project_type']}, not a mod.")

        version_data = resolve_version(slug, "*", minecraft, loader)
        maybe_add_mod(
            slug,
            str(version_data["version_number"]),
            version_data,
            "requested mod",
            planned,
            queue,
            manifest_mods,
            lock,
            missing_lock,
        )

    processed_version_ids: set[str] = set()
    while queue:
        slug = queue.pop(0)
        version_data = planned[slug]["version_data"]
        version_id = version_data["id"]
        if version_id in processed_version_ids:
            continue
        processed_version_ids.add(version_id)
        process_dependencies(
            slug,
            version_data,
            minecraft,
            loader,
            planned,
            queue,
            manifest_mods,
            lock,
            missing_lock,
            optional_answers,
        )

    if not planned and not missing_lock:
        print("No manifest changes were needed.")
        return

    for slug, data in planned.items():
        manifest_mods[slug] = data["constraint"]
        lock[slug] = lock_entry_from_version(data["version_data"])

    for slug in sorted(missing_lock):
        constraint = manifest_mods[slug]
        print(f"  Resolving missing lock entry for {slug} ({constraint})...")
        version_data = resolve_version(slug, constraint, minecraft, loader)
        lock[slug] = lock_entry_from_version(version_data)

    write_manifest(manifest_path, minecraft, loader, loader_version, manifest_mods)
    write_lock(lock_path, lock)

    print(f"\nWrote {manifest_path} with {len(manifest_mods)} mod(s).")
    print(f"Wrote {lock_path} with {len(lock)} mod(s).")


def import_modpack(
    slug: str, minecraft: str | None, include_client_only: bool = False
) -> tuple[str, str, dict[str, str], dict[str, dict]]:
    slug = parse_project_slug(slug)
    project = fetch_project(slug)
    if project["project_type"] != "modpack":
        fatal(f"'{slug}' is a {project['project_type']}, not a modpack.")

    print(f"Importing modpack: {project['title']} ({slug})")

    params = {"limit": "1"}
    if minecraft:
        params["game_versions"] = json.dumps([minecraft])

    versions = expect_list(
        api_get(f"/project/{urllib.parse.quote(slug, safe='')}/version", params),
        f"modpack versions for {slug}",
    )
    if not versions:
        if minecraft:
            fatal(f"No versions found for modpack '{slug}' on Minecraft {minecraft}.")
        fatal(f"No versions found for modpack '{slug}'.")

    pack_version = versions[0]
    mc_version = pack_version["game_versions"][0]
    loader = pack_version["loaders"][0] if pack_version["loaders"] else "fabric"

    print(f"  Pack version: {pack_version['version_number']}")
    print(f"  Minecraft: {mc_version}, Loader: {loader}")

    deps = pack_version.get("dependencies", [])
    if not deps:
        print("Warning: Modpack has no dependencies listed.", file=sys.stderr)
        return mc_version, loader, {}, {}

    project_ids = [dep["project_id"] for dep in deps if dep.get("project_id")]
    version_ids = [dep["version_id"] for dep in deps if dep.get("version_id")]

    id_to_slug: dict[str, str] = {}
    id_to_type: dict[str, str] = {}
    id_to_server_side: dict[str, str] = {}
    if project_ids:
        for fetched_project in expect_list(
            api_get("/projects", {"ids": json.dumps(project_ids)}),
            f"projects for modpack {slug}",
        ):
            id_to_slug[fetched_project["id"]] = fetched_project["slug"]
            id_to_type[fetched_project["id"]] = fetched_project["project_type"]
            id_to_server_side[fetched_project["id"]] = fetched_project.get(
                "server_side", "unknown"
            )
            PROJECT_CACHE[fetched_project["id"]] = fetched_project
            PROJECT_CACHE[fetched_project["slug"]] = fetched_project

    version_id_to_data: dict[str, dict] = {}
    if version_ids:
        for version_data in expect_list(
            api_get("/versions", {"ids": json.dumps(version_ids)}),
            f"versions for modpack {slug}",
        ):
            version_id_to_data[version_data["id"]] = version_data
            VERSION_CACHE[version_data["id"]] = version_data

    mods: dict[str, str] = {}
    lock: dict[str, dict] = {}
    skipped_non_mod: list[str] = []
    skipped_client_only: list[str] = []

    for dep in deps:
        project_id = dep.get("project_id")
        version_id = dep.get("version_id")
        if not project_id:
            continue

        mod_slug = id_to_slug.get(project_id)
        project_type = id_to_type.get(project_id, "unknown")
        server_side = id_to_server_side.get(project_id, "unknown")
        if not mod_slug:
            continue

        if project_type != "mod":
            skipped_non_mod.append(f"{mod_slug} ({project_type})")
            continue

        if not include_client_only and server_side == "unsupported":
            skipped_client_only.append(mod_slug)
            continue

        version_data = version_id_to_data.get(version_id) if version_id else None
        if version_data is None:
            mods[mod_slug] = "*"
            continue

        mods[mod_slug] = version_data["version_number"]
        lock[mod_slug] = lock_entry_from_version(version_data)

    if skipped_non_mod:
        print(
            f"  Skipped {len(skipped_non_mod)} non-mod dependencies: "
            f"{', '.join(skipped_non_mod)}"
        )

    if skipped_client_only:
        print(
            f"  Skipped {len(skipped_client_only)} client-only mods: "
            f"{', '.join(skipped_client_only)}"
        )

    print(f"  Found {len(mods)} mods")
    return mc_version, loader, mods, lock


def cmd_import(
    modpack_slug: str,
    minecraft: str | None,
    include_client_only: bool,
    manifest_path: Path,
    lock_path: Path,
):
    mc_version, loader, mods, lock = import_modpack(
        modpack_slug,
        minecraft,
        include_client_only=include_client_only,
    )

    if not mods:
        fatal("No mods to write.")

    unresolved = [slug for slug in mods if slug not in lock]
    if unresolved:
        print(f"\nResolving {len(unresolved)} mod(s) without pinned versions...")
        for slug in unresolved:
            constraint = mods[slug]
            print(f"  Resolving {slug} ({constraint})...", end=" ", flush=True)
            version_data = resolve_version(slug, constraint, mc_version, loader)
            mods[slug] = str(version_data["version_number"])
            lock[slug] = lock_entry_from_version(version_data)
            print(version_data["version_number"])

    write_manifest(manifest_path, mc_version, loader, None, mods)
    print(
        f"\nWrote {manifest_path} with {len(mods)} mods for Minecraft {mc_version} ({loader})."
    )
    write_lock(lock_path, lock)
    print(f"\nWrote {lock_path} with {len(lock)} mod(s).")


def mrpack_dependencies(
    minecraft: str, loader: str, loader_version: str | None
) -> dict[str, str]:
    dependencies = {"minecraft": minecraft}
    dependency_key = LOADER_DEPENDENCY_KEYS.get(loader)
    if dependency_key is None:
        fatal(f"Unsupported loader '{loader}' for mrpack export.")

    if loader_version is None:
        print(
            f"Warning: no loader version provided in the manifest or via --loader-version; omitting {dependency_key} from mrpack dependencies.",
            file=sys.stderr,
        )
        return dependencies

    dependencies[dependency_key] = loader_version
    return dependencies


def build_mrpack_index(
    minecraft: str,
    loader: str,
    loader_version: str | None,
    lock: dict[str, JSONDict],
    name: str,
    version_id: str,
    summary: str | None,
) -> JSONDict:
    files: list[JSONDict] = []
    for slug in sorted(lock):
        lock_entry = lock[slug]
        version_data = fetch_version(str(lock_entry["versionId"]))
        project = fetch_project(str(lock_entry["projectId"]))
        version_file = find_version_file(version_data, lock_entry)
        hashes = expect_dict(version_file["hashes"], f"file hashes for {slug}")

        file_entry: JSONDict = {
            "path": f"mods/{lock_entry['filename']}",
            "hashes": {
                "sha1": hashes["sha1"],
                "sha512": hashes["sha512"],
            },
            "downloads": [lock_entry["url"]],
            "fileSize": version_file["size"],
        }

        client_side = project.get("client_side")
        server_side = project.get("server_side")
        if client_side is not None or server_side is not None:
            file_entry["env"] = {
                "client": client_side or "unknown",
                "server": server_side or "unknown",
            }

        files.append(file_entry)

    index: JSONDict = {
        "formatVersion": 1,
        "game": "minecraft",
        "versionId": version_id,
        "name": name,
        "files": files,
        "dependencies": mrpack_dependencies(minecraft, loader, loader_version),
    }
    if summary:
        index["summary"] = summary
    return index


def cmd_export_mrpack(
    manifest_path: Path,
    lock_path: Path,
    output_path: Path,
    name: str | None,
    version_id: str,
    summary: str | None,
    loader_version: str | None,
):
    minecraft, loader, manifest_loader_version, manifest_mods = read_manifest(
        manifest_path
    )
    lock = read_lock(lock_path)

    if loader_version is None:
        loader_version = manifest_loader_version

    missing = sorted(slug for slug in manifest_mods if slug not in lock)
    if missing:
        fatal(
            f"Lock file is missing entries for: {', '.join(missing)}. Run `modrinth-mods update` first."
        )

    pack_name = name or output_path.stem
    index = build_mrpack_index(
        minecraft,
        loader,
        loader_version,
        lock,
        pack_name,
        version_id,
        summary,
    )

    with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("modrinth.index.json", json.dumps(index, indent=2) + "\n")

    print(f"Wrote {output_path} with {len(lock)} mod(s).")


def preprocess_argv(argv: list[str]) -> list[str]:
    if not argv:
        return ["update"]

    if argv[0] in {"-h", "--help"}:
        return argv

    command_names = {"update", "add", "import-modpack", "export-mrpack"}

    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg in {"--manifest", "--lock"}:
            index += 2
            continue
        if arg.startswith("--manifest=") or arg.startswith("--lock="):
            index += 1
            continue
        break

    if index < len(argv) and argv[index] in command_names:
        return argv

    for legacy_index, arg in enumerate(argv):
        if arg == "--import-modpack":
            if legacy_index + 1 >= len(argv):
                fatal("--import-modpack requires an argument.")
            return [
                *argv[:legacy_index],
                "import-modpack",
                argv[legacy_index + 1],
                *argv[legacy_index + 2 :],
            ]
        if arg.startswith("--import-modpack="):
            slug = arg.split("=", 1)[1]
            if not slug:
                fatal("--import-modpack requires an argument.")
            return [
                *argv[:legacy_index],
                "import-modpack",
                slug,
                *argv[legacy_index + 1 :],
            ]

    if index < len(argv):
        return [*argv[:index], "update", *argv[index:]]

    return [*argv, "update"]


def main():
    common_parser = argparse.ArgumentParser(add_help=False)
    common_parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("mods.toml"),
        help="Path to the mods manifest file (default: mods.toml)",
    )
    common_parser.add_argument(
        "--lock",
        type=Path,
        default=Path("mods.lock.json"),
        help="Path to the lock file (default: mods.lock.json)",
    )

    subcommand_common_parser = argparse.ArgumentParser(add_help=False)
    subcommand_common_parser.add_argument(
        "--manifest",
        type=Path,
        default=argparse.SUPPRESS,
        help="Path to the mods manifest file (default: mods.toml)",
    )
    subcommand_common_parser.add_argument(
        "--lock",
        type=Path,
        default=argparse.SUPPRESS,
        help="Path to the lock file (default: mods.lock.json)",
    )

    parser = argparse.ArgumentParser(
        description="Manage Modrinth mods via a TOML manifest and JSON lock file.",
        parents=[common_parser],
    )

    subparsers = parser.add_subparsers(dest="command")

    update_parser = subparsers.add_parser(
        "update",
        help="Update all mods or only the named mods.",
        parents=[subcommand_common_parser],
    )
    update_parser.add_argument(
        "mods",
        nargs="*",
        help="Optional list of mod slugs to update.",
    )

    add_parser = subparsers.add_parser(
        "add",
        help="Add mods to the manifest and lock file.",
        parents=[subcommand_common_parser],
    )
    add_parser.add_argument(
        "mods",
        nargs="+",
        help="One or more Modrinth mod slugs or URLs to add.",
    )

    import_parser = subparsers.add_parser(
        "import-modpack",
        help="Import mods from a Modrinth modpack.",
        parents=[subcommand_common_parser],
    )
    import_parser.add_argument(
        "modpack",
        help="Modrinth modpack slug or URL.",
    )
    import_parser.add_argument(
        "--minecraft",
        metavar="VERSION",
        help="Minecraft version to target when importing a modpack.",
    )
    import_parser.add_argument(
        "--include-client-only",
        action="store_true",
        help="Include mods marked unsupported on servers.",
    )

    export_parser = subparsers.add_parser(
        "export-mrpack",
        help="Generate an mrpack from the manifest and lock file.",
        parents=[subcommand_common_parser],
    )
    export_parser.add_argument(
        "--output",
        type=Path,
        default=Path("mods.mrpack"),
        help="Path to the output mrpack file (default: mods.mrpack)",
    )
    export_parser.add_argument(
        "--name",
        help="Pack name stored in the mrpack (default: output filename stem)",
    )
    export_parser.add_argument(
        "--version-id",
        default="1.0.0",
        help="Pack version identifier stored in the mrpack (default: 1.0.0)",
    )
    export_parser.add_argument(
        "--summary",
        help="Optional pack summary stored in the mrpack.",
    )
    export_parser.add_argument(
        "--loader-version",
        help="Loader version to include in mrpack dependencies (overrides mods.toml).",
    )

    args = parser.parse_args(preprocess_argv(sys.argv[1:]))

    if args.command == "add":
        cmd_add(args.manifest, args.lock, args.mods)
    elif args.command == "import-modpack":
        cmd_import(
            args.modpack,
            args.minecraft,
            args.include_client_only,
            args.manifest,
            args.lock,
        )
    elif args.command == "export-mrpack":
        cmd_export_mrpack(
            args.manifest,
            args.lock,
            args.output,
            args.name,
            args.version_id,
            args.summary,
            args.loader_version,
        )
    else:
        cmd_update(args.manifest, args.lock, getattr(args, "mods", []))


if __name__ == "__main__":
    main()
