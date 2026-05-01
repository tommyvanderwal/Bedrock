# dev-witness/

Local Bedrock-Echo witness for development. **Not** for production —
the real witness target is the ESP32 firmware in
[Bedrock-Echo](https://github.com/tommyvanderwal/Bedrock-Echo) under
`firmware/`, with the MikroTik podman witness as the LAN fallback.

## Usage

```bash
# One-time: clone Bedrock-Echo next to Bedrock and install Python deps
git clone https://github.com/tommyvanderwal/Bedrock-Echo /home/tommy/Bedrock-Echo
pip3 install --user --break-system-packages cryptography

# Start the dev witness
python3 /home/tommy/projects/Bedrock/dev-witness/run.py
# Prints: witness_pubkey (hex): <64-char hex>
# Listens on UDP 0.0.0.0:12321
```

## Note

The witness writes its X25519 private key to `/var/lib/bedrock-witness-dev/priv.key`
on first run; subsequent runs reuse it (so `witness_pubkey` is stable).
Wipe that file to rotate keys.
