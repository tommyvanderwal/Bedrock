#!/usr/bin/env python3
"""Run a Bedrock Echo witness on the dev box for local testing.

Generates (or reuses) a witness X25519 keypair at
/var/lib/bedrock-witness-dev/priv.key and prints the public key
(hex) so the Rust client can pin it.
"""
import sys
from pathlib import Path

sys.path.insert(0, "/home/tommy/Bedrock-Echo/python")

from echo import witness, crypto

KEY_DIR = Path("/var/lib/bedrock-witness-dev")
KEY_DIR.mkdir(parents=True, exist_ok=True)
priv = witness.load_or_generate_priv(KEY_DIR / "priv.key")
pub = crypto.x25519_pub_from_priv(priv)
print(f"witness_pubkey (hex): {pub.hex()}", flush=True)

w = witness.Witness(priv)
print("running on UDP 12321 (Ctrl-C to stop)...", flush=True)
witness.run_forever(w, bind="0.0.0.0", port=12321)
