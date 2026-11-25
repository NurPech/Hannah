#!/usr/bin/env python3
"""Sign a Hannah satellite app image with the Vault Transit key (CI only).

Takes the secure-padded, unsigned app image (built with
CONFIG_SECURE_SIGNED_APPS_NO_SECURE_BOOT=y and CONFIG_SECURE_BOOT_BUILD_SIGNED_BINARIES=n),
lets Vault Transit sign its SHA-256 (RSA-PSS, pre-hashed) and has espsecure assemble
and verify the signed image. The private key never leaves Vault.

  sign_firmware.py <unsigned_padded.bin> <signed_out.bin>

Environment (set by the CI job):
  VAULT_ADDR         base URL of Vault, required
  VAULT_ID_TOKEN     GitLab ID token (JWT) with the audience the Vault role is bound to, required
  VAULT_AUTH_MOUNT   JWT auth mount, default "gitlab"
  VAULT_ROLE         JWT role, default "hannah-firmware-signer"
  VAULT_TRANSIT_KEY  Transit key, default "hannah-satellite-firmware"
  VAULT_KEY_VERSION  key version to sign with, default "1" (the key is never rotated)

Any failure exits non-zero: a release must never fall back to an unsigned image,
a satellite that runs a signed image refuses unsigned ones. Tokens are never printed.
"""
import base64
import hashlib
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

SIGNATURE_BLOCK = 4096
PAD_ALIGN = 64 * 1024
APP_SLOT = 0x300000


def fail(msg):
    print(f"sign_firmware: ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def vault(method, path, body=None, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Vault-Token"] = token
    req = urllib.request.Request(
        os.environ["VAULT_ADDR"].rstrip("/") + "/v1/" + path,
        data=None if body is None else json.dumps(body).encode(),
        method=method,
        headers=headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        # The body of a Vault error carries no secrets, but never echo request data.
        fail(f"Vault {method} {path} returned HTTP {e.code}")
    except urllib.error.URLError as e:
        fail(f"Vault {method} {path} not reachable: {e.reason}")


def main():
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    src, dst = sys.argv[1], sys.argv[2]
    for name in ("VAULT_ADDR", "VAULT_ID_TOKEN"):
        if not os.environ.get(name):
            fail(f"{name} is not set")
    mount = os.environ.get("VAULT_AUTH_MOUNT", "gitlab")
    role = os.environ.get("VAULT_ROLE", "hannah-firmware-signer")
    key = os.environ.get("VAULT_TRANSIT_KEY", "hannah-satellite-firmware")
    # The key is never rotated: a satellite trusts exactly the key in its running app.
    # Pinned, so that an accidental rotate in Vault cannot silently switch the signing key.
    version = os.environ.get("VAULT_KEY_VERSION", "1")

    data = open(src, "rb").read()
    if len(data) % PAD_ALIGN:
        fail(f"{src} is {len(data)} bytes, not a multiple of 64 KiB: not a secure-padded image "
             "(is CONFIG_SECURE_SIGNED_APPS_NO_SECURE_BOOT on?)")
    if len(data) + SIGNATURE_BLOCK > APP_SLOT:
        fail(f"{src} plus the signature block does not fit the app slot ({APP_SLOT} bytes)")

    login = vault("POST", f"auth/{mount}/login", {"role": role, "jwt": os.environ["VAULT_ID_TOKEN"]})
    token = login["auth"]["client_token"]

    signature = vault("POST", f"transit/sign/{key}/sha2-256", {
        "input": base64.b64encode(hashlib.sha256(data).digest()).decode(),
        "prehashed": True,
        "signature_algorithm": "pss",
        "salt_length": "hash",
        "key_version": int(version),
    }, token)["data"]["signature"]
    sig_bytes = base64.b64decode(signature.split(":", 2)[2])
    if len(sig_bytes) != 384:
        fail(f"unexpected signature length {len(sig_bytes)} (RSA-3072 gives 384)")

    keys = vault("GET", f"transit/keys/{key}", token=token)["data"]["keys"]
    if version not in keys:
        fail(f"key {key} has no version {version}")
    pub_pem = keys[version]["public_key"]

    work = os.path.dirname(os.path.abspath(dst))
    sig_file = os.path.join(work, "firmware.sig")
    pub_file = os.path.join(work, "firmware_pub.pem")
    open(sig_file, "wb").write(sig_bytes)
    open(pub_file, "w").write(pub_pem)

    def espsecure(*args):
        subprocess.run([sys.executable, "-m", "espsecure", *args], check=True)

    try:
        espsecure("sign-data", "--version", "2", "--pub-key", pub_file, "--signature", sig_file,
                  "--output", dst, src)
        espsecure("verify-signature", "--version", "2", "--keyfile", pub_file, dst)
    except subprocess.CalledProcessError:
        fail("espsecure refused the signature")
    finally:
        for f in (sig_file, pub_file):
            if os.path.exists(f):
                os.remove(f)

    if os.path.getsize(dst) != len(data) + SIGNATURE_BLOCK:
        fail("signed image has an unexpected size")
    print(f"sign_firmware: signed {os.path.basename(src)} ({len(data)} bytes) -> "
          f"{os.path.basename(dst)} ({os.path.getsize(dst)} bytes), key {key}")


if __name__ == "__main__":
    main()
