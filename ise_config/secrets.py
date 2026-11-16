"""Encrypted secrets bundle (secrets.json.gpg).

Secret values are encrypted to a public key; export never needs the private
key. gpg output is randomised, so an optional keyed HMAC of the plaintext
lets export skip re-encryption when nothing changed.
"""

import hashlib
import hmac
import os
import subprocess
import tempfile

from .normalize import dumps

BUNDLE = "secrets.json.gpg"
DIGEST = "secrets.digest"


def write_bundle(outdir, secrets, recipient_file, fp_key, log):
    bundle = os.path.join(outdir, BUNDLE)
    digest_path = os.path.join(outdir, DIGEST)
    if not recipient_file or not os.path.exists(recipient_file):
        log(f"WARNING: no public key at {recipient_file!r}; secrets NOT saved. "
            "A rebuild will have to re-enter every shared secret by hand.")
        return
    plain = dumps(secrets).encode()
    digest = None
    if fp_key:
        # Include the public key so replacing it forces re-encryption.
        with open(recipient_file, "rb") as f:
            digest = hmac.new(fp_key.encode(), plain + f.read(), hashlib.sha256).hexdigest()
        try:
            with open(digest_path) as f:
                if f.read().strip() == digest and os.path.exists(bundle):
                    return
        except FileNotFoundError:
            pass
    with tempfile.TemporaryDirectory() as home:
        # Throwaway keyring.
        subprocess.run(
            ["gpg", "--homedir", home, "--batch", "--yes", "--quiet",
             "--trust-model", "always", "--recipient-file", recipient_file,
             "--encrypt", "--output", bundle],
            input=plain, check=True)
    if digest:
        with open(digest_path, "w") as f:
            f.write(digest + "\n")
    log(f"secrets bundle rewritten ({sum(len(v) for v in secrets.values())} values)")


def read_bundle(path):
    """Decrypt with the operator's own keyring (gpg-agent prompts as needed)."""
    import json
    if path.endswith(".gpg"):
        out = subprocess.run(["gpg", "--quiet", "--decrypt", path],
                             check=True, stdout=subprocess.PIPE).stdout
        return json.loads(out)
    with open(path) as f:
        return json.load(f)
