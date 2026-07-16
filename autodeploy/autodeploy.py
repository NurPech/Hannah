#!/usr/bin/env python3
"""
hannah-autodeploy — polls Hannah Update Server channels and deploys updates.

Each component is described in the config file. The agent tracks installed
versions in a local state file and restarts the systemd service after
a successful deployment. To update itself, it deploys normally then restarts
its own systemd unit.
"""

import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
from pathlib import Path

import requests
import yaml

log = logging.getLogger("autodeploy")

DEFAULT_CONFIG = "/etc/hannah/autodeploy.yaml"
DEFAULT_STATE = "/var/lib/hannah/autodeploy-state.json"
DEFAULT_DEVICE_ID_FILE = "/var/lib/hannah/autodeploy-device-id"


# ---------------------------------------------------------------------------
# Update-Server API
# ---------------------------------------------------------------------------

def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"} if token else {}


def get_or_create_device_id(path: Path) -> str:
    if path.exists():
        return path.read_text().strip()
    device_id = str(uuid.uuid4())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(device_id)
    log.info("Generated new device ID: %s", device_id)
    return device_id


def get_latest(base_url: str, token: str, channel: str, current_version: str | None = None, device_id: str | None = None) -> dict | None:
    """Return {version, sha256, size} for the latest release on a channel, or None."""
    params: dict = {"channel": channel}
    if current_version:
        params["current"] = current_version
    if device_id:
        params["device"] = device_id
    r = requests.get(
        f"{base_url}/latest",
        params=params,
        headers=_headers(token),
        timeout=30,
    )
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


def download_release(url: str, token: str, dest: Path, device_id: str | None = None) -> None:
    params = {}
    if device_id:
        params["device"] = device_id
    r = requests.get(
        url,
        params=params or None,
        headers=_headers(token),
        timeout=120,
        stream=True,
    )
    r.raise_for_status()
    with open(dest, "wb") as f:
        for chunk in r.iter_content(chunk_size=65536):
            f.write(chunk)


def _verify_sha256(path: Path, expected: str) -> bool:
    if not expected:
        return True
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    actual = h.hexdigest()
    if actual != expected:
        log.error("SHA256 mismatch: expected %s, got %s", expected, actual)
        return False
    return True


# ---------------------------------------------------------------------------
# Deployment
# ---------------------------------------------------------------------------

def _extract_and_copy(archive: Path, install_dir: Path) -> None:
    """Extract tar.gz to a temp dir, then merge into install_dir."""
    with tempfile.TemporaryDirectory(prefix="autodeploy-") as tmp:
        with tarfile.open(archive, "r:gz") as tar:
            tar.extractall(tmp)
        src = Path(tmp)
        install_dir.mkdir(parents=True, exist_ok=True)
        for item in src.iterdir():
            dst = install_dir / item.name
            if item.is_dir():
                if dst.exists():
                    shutil.rmtree(dst)
                shutil.copytree(item, dst)
            else:
                if dst.exists():
                    dst.unlink()
                shutil.copy2(item, dst)


def _restart_service(service: str) -> None:
    """Restart a managed service. `service` is the systemd unit name on Linux,
    or the launchd label (e.g. com.hannah.voiceid) on macOS."""
    if sys.platform == "darwin":
        subprocess.run(["launchctl", "kickstart", "-k", f"system/{service}"], check=True)
    else:
        subprocess.run(["systemctl", "restart", service], check=True)
    log.info("Service %s restarted.", service)


def deploy_component(component: dict, base_url: str, token: str, state: dict, device_id: str | None = None) -> bool:
    """Check for update and deploy if a newer version is available.

    Returns True if an update was deployed.
    """
    name = component["name"]
    channel = component["channel"]
    install_dir = Path(component["install_dir"])
    current = state.get(name)
    current_rev: int = state.get("_revisions", {}).get(name, 0)

    try:
        latest = get_latest(base_url, token, channel, current_version=current, device_id=device_id)
    except requests.RequestException as e:
        log.warning("[%s] Could not fetch latest: %s", name, e)
        return False

    if latest is None:
        log.debug("[%s] No releases on channel '%s'.", name, channel)
        return False

    server_rev: int = latest.get("revision", 0)
    if current == latest["version"] and current_rev >= server_rev:
        log.debug("[%s] Already at %s rev %d.", name, current, current_rev)
        return False

    log.info("[%s] Update: %s rev %d → %s rev %d",
             name, current or "(none)", current_rev, latest["version"], server_rev)

    download_url = latest.get("url", f"{base_url}/releases/{latest['version']}?channel={channel}")
    tmp_archive = Path(tempfile.mktemp(suffix=".tar.gz"))
    try:
        download_release(download_url, token, tmp_archive, device_id=device_id)
        if not _verify_sha256(tmp_archive, latest.get("sha256", "")):
            return False
        _extract_and_copy(tmp_archive, install_dir)
        post_install = component.get("post_install")
        if post_install:
            log.info("[%s] Running post_install: %s", name, post_install)
            result = subprocess.run(post_install, shell=True)
            if result.returncode != 0:
                log.error("[%s] post_install failed with exit code %d", name, result.returncode)
                return False
        state[name] = latest["version"]
        state.setdefault("_revisions", {})[name] = server_rev
        log.info("[%s] Deployed %s rev %d.", name, latest["version"], server_rev)
        return True
    except Exception as e:
        log.error("[%s] Deployment failed: %s", name, e)
        return False
    finally:
        tmp_archive.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

def load_state(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            return {}
    return {}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2))


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    config_path = os.environ.get("AUTODEPLOY_CONFIG", DEFAULT_CONFIG)
    state_path = Path(os.environ.get("AUTODEPLOY_STATE", DEFAULT_STATE))
    device_id_path = Path(os.environ.get("AUTODEPLOY_DEVICE_ID", DEFAULT_DEVICE_ID_FILE))

    try:
        with open(config_path) as f:
            config = yaml.safe_load(f)
    except FileNotFoundError:
        log.error("Config not found: %s", config_path)
        sys.exit(1)

    base_url: str = config["server_url"].rstrip("/")
    token: str = config.get("token", "")
    poll_interval: int = config.get("poll_interval", 300)
    components: list[dict] = config["components"]
    device_id: str = get_or_create_device_id(device_id_path)

    log.info("hannah-autodeploy started. Device ID: %s. Polling every %ds.", device_id, poll_interval)

    while True:
        state = load_state(state_path)
        updated_self = False

        for component in components:
            try:
                updated = deploy_component(component, base_url, token, state, device_id=device_id)
                if updated:
                    save_state(state_path, state)
                    service = component.get("service")
                    if service == config.get("self_service"):
                        updated_self = True
                    elif service:
                        _restart_service(service)
            except Exception as e:
                log.error("[%s] Unexpected error: %s", component.get("name", "?"), e)

        if updated_self:
            # Self-update: systemd/launchd will restart us with the new code.
            log.info("Self-update deployed — restarting service.")
            if sys.platform == "darwin":
                subprocess.run(["launchctl", "kickstart", "-k", f"system/{config['self_service']}"], check=False)
            else:
                subprocess.run(["systemctl", "restart", config["self_service"]], check=False)
            sys.exit(0)

        time.sleep(poll_interval)


if __name__ == "__main__":
    main()
