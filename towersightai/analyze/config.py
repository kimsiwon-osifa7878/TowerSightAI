"""Analysis-side configuration: NAS sites and local data layout.

Sites live in ``data/analysis/sites.json`` (gitignored — it carries the SFTP password just like
``.env`` does). Several sites can be registered because each parking machine may archive to a
different NAS location.
"""

from __future__ import annotations

import json
import os
import re
import socket
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping

from towersightai.config.env_loader import parse_env_file

SITES_SCHEMA_VERSION = 1
HOST_ROLES = ("field", "dev")
HOST_ROLE_LABELS = {"field": "현장", "dev": "개발"}
DEFAULT_ANALYSIS_ROOT = Path("data/analysis")
_SITE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_HOST_DIR = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


@dataclass(frozen=True)
class SiteConfig:
    name: str
    label: str = ""
    nas_host: str = ""
    nas_port: int = 22
    nas_username: str = ""
    nas_password: str = field(default="", repr=False)
    nas_folder: str = ""
    known_hosts_path: Path = Path("~/.ssh/known_hosts")
    timezone_name: str = "Asia/Seoul"
    raw_subdir: str = "raw"
    host_roles: Mapping[str, str] = field(default_factory=dict)  # source_host → "field" | "dev"
    # Owner of days uploaded without a host folder and without a manifest (legacy uploader that
    # cannot be redeployed): the field device of this site. Manifests still win when present.
    default_host: str = ""
    # Per-day owner overrides for legacy ``raw/<day>`` folders (day → host): the one escape hatch
    # for a manifest-less day that the default-host rule attributes to the wrong machine.
    day_owners: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        roles = {str(k): str(v) for k, v in dict(self.host_roles or {}).items() if str(v) in HOST_ROLES}
        object.__setattr__(self, "host_roles", roles)
        owners = {str(k): str(v) for k, v in dict(self.day_owners or {}).items() if _DAY.match(str(k)) and _HOST_DIR.match(str(v))}
        object.__setattr__(self, "day_owners", owners)
        if not _SITE_NAME.match(self.name):
            raise ValueError("site name must be 1-64 chars of letters, digits, '_', '.', '-'")
        if not 1 <= int(self.nas_port) <= 65535:
            raise ValueError("nas_port must be between 1 and 65535")
        host = self.nas_host.strip()
        if host and ("/" in host or ":" in host or "@" in host):
            raise ValueError("nas_host must be a bare hostname (no scheme, port, path or user)")
        object.__setattr__(self, "nas_host", host)
        object.__setattr__(self, "default_host", re.sub(r"[^A-Za-z0-9_.-]+", "-", self.default_host or "").strip("-.")[:128])
        object.__setattr__(self, "known_hosts_path", Path(self.known_hosts_path).expanduser())
        if not self.label:
            object.__setattr__(self, "label", self.name)

    @property
    def configured(self) -> bool:
        return bool(self.nas_host and self.nas_username and self.nas_folder)

    @property
    def missing(self) -> tuple[str, ...]:
        missing = []
        if not self.nas_host:
            missing.append("nas_host")
        if not self.nas_username:
            missing.append("nas_username")
        if not self.nas_password:
            missing.append("nas_password")
        if not self.nas_folder:
            missing.append("nas_folder")
        return tuple(missing)

    @property
    def remote_raw_dir(self) -> str:
        return f"{self.nas_folder.rstrip('/')}/{self.raw_subdir.strip('/')}"

    def host_role(self, host: str) -> str:
        """Explicit role, else: this machine's own hostname is the development box, anything
        else is treated as a field device. ``unknown-host`` (no manifest) is never assumed field."""
        explicit = self.host_roles.get(host)
        if explicit:
            return explicit
        if self.default_host and host == self.default_host:
            return "field"
        if host == "unknown-host":
            return "unknown"
        return "dev" if host == socket.gethostname() else "field"

    def with_host_role(self, host: str, role: str | None) -> "SiteConfig":
        roles = dict(self.host_roles)
        if role in HOST_ROLES:
            roles[host] = role
        else:
            roles.pop(host, None)
        return replace(self, host_roles=roles)

    def with_day_owner(self, day: str, host: str | None) -> "SiteConfig":
        owners = dict(self.day_owners)
        if host:
            owners[day] = host
        else:
            owners.pop(day, None)
        return replace(self, day_owners=owners)

    def to_public_dict(self) -> dict[str, Any]:
        """Dictionary safe for the dashboard/API: password replaced by a presence flag."""
        data = asdict(self)
        data["host_roles"] = dict(self.host_roles)
        data["day_owners"] = dict(self.day_owners)
        data.pop("nas_password", None)
        data["known_hosts_path"] = str(self.known_hosts_path)
        data["has_password"] = bool(self.nas_password)
        data["configured"] = self.configured
        data["missing"] = list(self.missing)
        return data

    def to_stored_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["host_roles"] = dict(self.host_roles)
        data["day_owners"] = dict(self.day_owners)
        data["known_hosts_path"] = str(self.known_hosts_path)
        return data

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "SiteConfig":
        return cls(
            name=str(data.get("name") or ""),
            label=str(data.get("label") or ""),
            nas_host=str(data.get("nas_host") or ""),
            nas_port=int(data.get("nas_port") or 22),
            nas_username=str(data.get("nas_username") or ""),
            nas_password=str(data.get("nas_password") or ""),
            nas_folder=str(data.get("nas_folder") or ""),
            known_hosts_path=Path(str(data.get("known_hosts_path") or "~/.ssh/known_hosts")),
            timezone_name=str(data.get("timezone_name") or "Asia/Seoul"),
            raw_subdir=str(data.get("raw_subdir") or "raw"),
            host_roles=data.get("host_roles") if isinstance(data.get("host_roles"), Mapping) else {},
            default_host=str(data.get("default_host") or ""),
            day_owners=data.get("day_owners") if isinstance(data.get("day_owners"), Mapping) else {},
        )


def site_from_env(env_path: Path, *, name: str, label: str = "") -> SiteConfig:
    """Seed a site from a deployment ``.env`` (only the SYNOLOGY_NAS_* keys are read)."""
    values = parse_env_file(env_path)
    return SiteConfig(
        name=name,
        label=label or name,
        nas_host=values.get("SYNOLOGY_NAS_HOST", ""),
        nas_port=int(values.get("SYNOLOGY_NAS_PORT", "22") or 22),
        nas_username=values.get("SYNOLOGY_NAS_ID", ""),
        nas_password=values.get("SYNOLOGY_NAS_PW", ""),
        nas_folder=values.get("SYNOLOGY_NAS_FOLDER", ""),
        known_hosts_path=Path(values.get("SYNOLOGY_NAS_KNOWN_HOSTS", "~/.ssh/known_hosts")),
        timezone_name=values.get("RAW_DATA_TIMEZONE", "Asia/Seoul"),
    )


class AnalysisPaths:
    """Local layout under ``data/analysis/`` (all gitignored)."""

    def __init__(self, root: Path = DEFAULT_ANALYSIS_ROOT) -> None:
        self.root = Path(root).expanduser()

    @property
    def sites_file(self) -> Path:
        return self.root / "sites.json"

    def site_root(self, site: str) -> Path:
        return self.root / "sites" / _safe_segment(site)

    def cache_dir(self, site: str, host: str, day: str) -> Path:
        return self.site_root(site) / "cache" / _safe_segment(host) / _safe_day(day)

    def digest_path(self, site: str, host: str, day: str) -> Path:
        return self.site_root(site) / "index" / _safe_segment(host) / f"{_safe_day(day)}.json"

    def labels_path(self, site: str) -> Path:
        return self.site_root(site) / "labels.jsonl"

    def mp4_cache_dir(self, site: str, host: str, day: str) -> Path:
        return self.site_root(site) / "mp4" / _safe_segment(host) / _safe_day(day)


def _safe_segment(value: str) -> str:
    if not _HOST_DIR.match(value):
        raise ValueError(f"unsafe path segment: {value!r}")
    return value


def _safe_day(value: str) -> str:
    if not _DAY.match(value):
        raise ValueError(f"invalid day: {value!r}")
    return value


class SitesStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def load(self) -> list[SiteConfig]:
        if not self.path.is_file():
            return []
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"sites file is unreadable: {self.path}") from exc
        sites = payload.get("sites") if isinstance(payload, dict) else None
        if not isinstance(sites, list):
            raise ValueError(f"sites file has no 'sites' list: {self.path}")
        return [SiteConfig.from_mapping(item) for item in sites if isinstance(item, dict)]

    def save(self, sites: list[SiteConfig]) -> None:
        names = [site.name for site in sites]
        if len(names) != len(set(names)):
            raise ValueError("duplicate site names")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"schema_version": SITES_SCHEMA_VERSION, "sites": [site.to_stored_dict() for site in sites]}
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        temporary.replace(self.path)

    def get(self, name: str) -> SiteConfig | None:
        return next((site for site in self.load() if site.name == name), None)

    def upsert(self, site: SiteConfig, *, keep_password_if_blank: bool = True) -> SiteConfig:
        sites = self.load()
        existing = next((item for item in sites if item.name == site.name), None)
        if existing is not None and keep_password_if_blank and not site.nas_password:
            site = replace(site, nas_password=existing.nas_password)
        sites = [item for item in sites if item.name != site.name] + [site]
        sites.sort(key=lambda item: item.name)
        self.save(sites)
        return site

    def remove(self, name: str) -> bool:
        sites = self.load()
        remaining = [item for item in sites if item.name != name]
        if len(remaining) == len(sites):
            return False
        self.save(remaining)
        return True


__all__ = ["AnalysisPaths", "DEFAULT_ANALYSIS_ROOT", "HOST_ROLES", "HOST_ROLE_LABELS", "SiteConfig", "SitesStore", "site_from_env"]
