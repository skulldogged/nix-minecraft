#!/usr/bin/env python3
"""
Resolve Modrinth mods declared in a TOML manifest and produce a JSON lock file.

Manifest format (mods.toml):

    [options]
    minecraft = "1.21.1"
    loader = "fabric"

    [mods]
    fabric-api = "*"           # latest compatible version
    lithium = "*"
    ferritecore = "6.0.0"      # pinned to a specific version number

Lock file format (mods.lock.json):

    {
      "fabric-api": {
        "version": "0.115.0+1.21.1",
        "versionId": "9YVrKY0Z",
        "projectId": "P7dR8mSH",
        "url": "https://cdn.modrinth.com/data/...",
        "hash": "sha512-...",
        "filename": "fabric-api-0.115.0+1.21.1.jar"
      },
      ...
    }

Usage:
    update-modrinth-mods [--manifest mods.toml] [--lock mods.lock.json]
    update-modrinth-mods --import-modpack <slug-or-url> [--minecraft <version>] [--manifest mods.toml] [--lock mods.lock.json]
"""

import argparse
import json
import re
import sys
import tomllib
import urllib.error
import urllib.request
import urllib.parse
import base64
from pathlib import Path

API_BASE = "https://api.modrinth.com/v2"
USER_AGENT = "Infinidoge/nix-minecraft (nix-minecraft mod updater)"


def api_get(path: str, params: dict | None = None) -> dict | list:
    """Make a GET request to the Modrinth API."""
    url = f"{API_BASE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        print(f"Error: API request to {url} failed: {e.code} {e.reason}", file=sys.stderr)
        print(f"  Response: {body}", file=sys.stderr)
        sys.exit(1)


def resolve_mod(slug: str, version_constraint: str, minecraft: str, loader: str) -> dict:
    """Resolve a single mod to a specific version from the Modrinth API."""
    params = {
        "loaders": json.dumps([loader]),
        "game_versions": json.dumps([minecraft]),
    }

    versions = api_get(f"/project/{urllib.parse.quote(slug, safe='')}/version", params)

    if not versions:
        print(
            f"Error: No versions found for '{slug}' compatible with "
            f"{loader} on Minecraft {minecraft}",
            file=sys.stderr,
        )
        sys.exit(1)

    if version_constraint != "*":
        matching = [v for v in versions if v["version_number"] == version_constraint]
        if not matching:
            available = [v["version_number"] for v in versions[:10]]
            print(
                f"Error: No version '{version_constraint}' found for '{slug}'. "
                f"Available: {', '.join(available)}{'...' if len(versions) > 10 else ''}",
                file=sys.stderr,
            )
            sys.exit(1)
        version = matching[0]
    else:
        releases = [v for v in versions if v["version_type"] == "release"]
        version = releases[0] if releases else versions[0]

    files = version["files"]
    primary = next((f for f in files if f["primary"]), files[0])

    sha512_hex = primary["hashes"]["sha512"]
    sha512_bytes = bytes.fromhex(sha512_hex)
    sri_hash = "sha512-" + base64.b64encode(sha512_bytes).decode()

    return {
        "version": version["version_number"],
        "versionId": version["id"],
        "projectId": version["project_id"],
        "url": primary["url"],
        "hash": sri_hash,
        "filename": primary["filename"],
    }


def resolve_mod_by_version_id(version_id: str) -> dict:
    """Resolve a mod directly by its Modrinth version ID."""
    version = api_get(f"/version/{urllib.parse.quote(version_id, safe='')}")

    files = version["files"]
    primary = next((f for f in files if f["primary"]), files[0])

    sha512_hex = primary["hashes"]["sha512"]
    sha512_bytes = bytes.fromhex(sha512_hex)
    sri_hash = "sha512-" + base64.b64encode(sha512_bytes).decode()

    return {
        "version": version["version_number"],
        "versionId": version["id"],
        "projectId": version["project_id"],
        "url": primary["url"],
        "hash": sri_hash,
        "filename": primary["filename"],
    }


def parse_modpack_slug(input_str: str) -> str:
    """Extract a Modrinth project slug from a URL or return the input as-is."""
    # Handle URLs like https://modrinth.com/modpack/fabulously-optimized
    match = re.match(r"https?://modrinth\.com/modpack/([^/?#]+)", input_str)
    if match:
        return match.group(1)
    return input_str


def version_to_lock_entry(version_data: dict) -> dict:
    """Convert a Modrinth version API response into a lock file entry."""
    files = version_data["files"]
    primary = next((f for f in files if f["primary"]), files[0])

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


def import_modpack(
    slug: str, minecraft: str | None
) -> tuple[str, str, dict[str, str], dict[str, dict]]:
    """
    Fetch a modpack from Modrinth and extract its mod list.

    Returns (minecraft_version, loader, mods_dict, lock_dict) where:
    - mods_dict maps slug -> version_number for the manifest
    - lock_dict maps slug -> lock entry (with url, hash, etc.) built directly
      from the modpack's pinned version IDs, avoiding re-resolution issues
    """
    slug = parse_modpack_slug(slug)

    # Verify it's actually a modpack
    project = api_get(f"/project/{urllib.parse.quote(slug, safe='')}")
    if project["project_type"] != "modpack":
        print(
            f"Error: '{slug}' is a {project['project_type']}, not a modpack.",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"Importing modpack: {project['title']} ({slug})")

    # Get the modpack version, optionally filtered by MC version
    params = {"limit": "1"}
    if minecraft:
        params["game_versions"] = json.dumps([minecraft])

    versions = api_get(
        f"/project/{urllib.parse.quote(slug, safe='')}/version", params
    )
    if not versions:
        msg = f"Error: No versions found for modpack '{slug}'"
        if minecraft:
            msg += f" on Minecraft {minecraft}"
        print(msg, file=sys.stderr)
        sys.exit(1)

    pack_version = versions[0]
    mc_version = pack_version["game_versions"][0]
    loader = pack_version["loaders"][0] if pack_version["loaders"] else "fabric"

    print(f"  Pack version: {pack_version['version_number']}")
    print(f"  Minecraft: {mc_version}, Loader: {loader}")

    deps = pack_version.get("dependencies", [])
    if not deps:
        print("Warning: Modpack has no dependencies listed.", file=sys.stderr)
        return mc_version, loader, {}, {}

    # Collect project IDs and version IDs from dependencies
    project_ids = [d["project_id"] for d in deps if d.get("project_id")]
    version_ids = [d["version_id"] for d in deps if d.get("version_id")]

    # Bulk-fetch project info to get slugs and types
    id_to_slug: dict[str, str] = {}
    id_to_type: dict[str, str] = {}
    if project_ids:
        # API accepts up to ~200 IDs at once
        projects = api_get("/projects", {"ids": json.dumps(project_ids)})
        for p in projects:
            id_to_slug[p["id"]] = p["slug"]
            id_to_type[p["id"]] = p["project_type"]

    # Bulk-fetch full version data (includes files, hashes, URLs)
    version_id_to_data: dict[str, dict] = {}
    if version_ids:
        fetched_versions = api_get("/versions", {"ids": json.dumps(version_ids)})
        for v in fetched_versions:
            version_id_to_data[v["id"]] = v

    # Build the mod list and lock entries, filtering to only actual mods
    mods: dict[str, str] = {}
    lock: dict[str, dict] = {}
    skipped: list[str] = []
    for dep in deps:
        pid = dep.get("project_id")
        vid = dep.get("version_id")
        if not pid:
            continue

        mod_slug = id_to_slug.get(pid)
        project_type = id_to_type.get(pid, "unknown")

        if not mod_slug:
            continue

        if project_type != "mod":
            skipped.append(f"{mod_slug} ({project_type})")
            continue

        version_data = version_id_to_data.get(vid) if vid else None
        if version_data:
            mods[mod_slug] = version_data["version_number"]
            lock[mod_slug] = version_to_lock_entry(version_data)
        else:
            mods[mod_slug] = "*"

    if skipped:
        print(f"  Skipped {len(skipped)} non-mod dependencies: {', '.join(skipped)}")

    print(f"  Found {len(mods)} mods")

    return mc_version, loader, mods, lock


def write_manifest(path: Path, minecraft: str, loader: str, mods: dict[str, str]):
    """Write a mods.toml manifest file."""
    lines = [
        "[options]",
        f'minecraft = "{minecraft}"',
        f'loader = "{loader}"',
        "",
        "[mods]",
    ]

    for slug in sorted(mods):
        version = mods[slug]
        if version == "*":
            lines.append(f'{slug} = "*"')
        else:
            lines.append(f'{slug} = "{version}"')

    lines.append("")

    with open(path, "w") as f:
        f.write("\n".join(lines))


def cmd_update(manifest_path: Path, lock_path: Path):
    """Resolve mods from an existing manifest and write the lock file."""
    if not manifest_path.exists():
        print(f"Error: Manifest file '{manifest_path}' not found.", file=sys.stderr)
        print(
            f"\nCreate a {manifest_path} file with the following format:\n",
            file=sys.stderr,
        )
        print(
            '[options]\nminecraft = "1.21.1"\nloader = "fabric"\n\n'
            '[mods]\nfabric-api = "*"\nlithium = "*"',
            file=sys.stderr,
        )
        print(
            "\nOr import from a Modrinth modpack:\n"
            "  update-modrinth-mods --import-modpack <slug-or-url>",
            file=sys.stderr,
        )
        sys.exit(1)

    with open(manifest_path, "rb") as f:
        manifest = tomllib.load(f)

    options = manifest.get("options", {})
    minecraft = options.get("minecraft")
    loader = options.get("loader")

    if not minecraft or not loader:
        print(
            "Error: Manifest must have [options] with 'minecraft' and 'loader' fields.",
            file=sys.stderr,
        )
        sys.exit(1)

    mods = manifest.get("mods", {})
    if not mods:
        print("Warning: No mods declared in manifest.", file=sys.stderr)

    # Load existing lock file to show what changed
    old_lock = {}
    if lock_path.exists():
        with open(lock_path) as f:
            old_lock = json.load(f)

    lock = {}
    for slug, constraint in mods.items():
        if not isinstance(constraint, str):
            constraint = str(constraint)

        old_version = old_lock.get(slug, {}).get("version", None)

        print(f"  Resolving {slug} ({constraint})...", end=" ", flush=True)
        entry = resolve_mod(slug, constraint, minecraft, loader)
        lock[slug] = entry

        new_version = entry["version"]
        if old_version and old_version != new_version:
            print(f"{old_version} -> {new_version}")
        elif old_version:
            print(f"{new_version} (unchanged)")
        else:
            print(f"{new_version}")

    # Sort by slug for deterministic output
    lock = dict(sorted(lock.items()))

    with open(lock_path, "w") as f:
        json.dump(lock, f, indent=2)
        f.write("\n")

    print(f"\nWrote {lock_path} with {len(lock)} mod(s).")


def cmd_import(modpack_slug: str, minecraft: str | None, manifest_path: Path, lock_path: Path):
    """Import a Modrinth modpack into a manifest and write the lock file."""
    mc_version, loader, mods, lock = import_modpack(modpack_slug, minecraft)

    if not mods:
        print("No mods to write.", file=sys.stderr)
        sys.exit(1)

    write_manifest(manifest_path, mc_version, loader, mods)
    print(f"\nWrote {manifest_path} with {len(mods)} mods for Minecraft {mc_version} ({loader}).")

    # Any mods without a version_id in the modpack need to be resolved normally
    unresolved = [slug for slug in mods if slug not in lock]
    if unresolved:
        print(f"\nResolving {len(unresolved)} mod(s) without pinned versions...")
        for slug in unresolved:
            constraint = mods[slug]
            if not isinstance(constraint, str):
                constraint = str(constraint)
            print(f"  Resolving {slug} ({constraint})...", end=" ", flush=True)
            entry = resolve_mod(slug, constraint, mc_version, loader)
            lock[slug] = entry
            print(entry["version"])

    # Sort by slug for deterministic output
    lock = dict(sorted(lock.items()))

    with open(lock_path, "w") as f:
        json.dump(lock, f, indent=2)
        f.write("\n")

    print(f"\nWrote {lock_path} with {len(lock)} mod(s).")


def main():
    parser = argparse.ArgumentParser(
        description="Manage Modrinth mods via a TOML manifest and JSON lock file.",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("mods.toml"),
        help="Path to the mods manifest file (default: mods.toml)",
    )
    parser.add_argument(
        "--lock",
        type=Path,
        default=Path("mods.lock.json"),
        help="Path to the lock file (default: mods.lock.json)",
    )
    parser.add_argument(
        "--import-modpack",
        metavar="SLUG_OR_URL",
        help="Import mods from a Modrinth modpack (slug or URL, e.g. "
        "'fabulously-optimized' or 'https://modrinth.com/modpack/fabulously-optimized')",
    )
    parser.add_argument(
        "--minecraft",
        metavar="VERSION",
        help="Minecraft version to target when importing a modpack "
        "(default: latest version the modpack supports)",
    )

    args = parser.parse_args()

    if args.import_modpack:
        cmd_import(args.import_modpack, args.minecraft, args.manifest, args.lock)
    else:
        if args.minecraft:
            parser.error("--minecraft can only be used with --import-modpack")
        cmd_update(args.manifest, args.lock)


if __name__ == "__main__":
    main()
