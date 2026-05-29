# mgmt/routes_iso.py

ISO-library HTTP routes for the mgmt API. Registers three FastAPI endpoints —
list, upload, delete — that operate on the cluster-wide SeaweedFS FUSE mount at
`/mnt/bedrock/iso`. Writes go through the FUSE mount, so the filer replicates
files per the `/iso/` collection policy
(`installer/lib/seaweedfs.py::init_collections`); a listing on any node shows the
same files and a delete on any node removes them cluster-wide. The mgmt app calls
`register_routes(app, push_log=...)` at startup; `virt-install` reads ISOs from
this same path during VM create.

## Functions / Classes

### `register_routes(app, *, push_log) -> None`
Attach the three ISO endpoints to the FastAPI app.
- **In:** `app` — the FastAPI instance to mount routes on. `push_log` — callable
  injected by the caller (avoids a circular import), invoked as
  `push_log(msg, node=, app=, level=)`.
- **Out:** `None`. Side effect: defines and registers the handlers below.

The registered handlers (closures):

### `GET /api/isos` → `api_list_isos()`
List every `.iso` (case-insensitive) in `ISO_DIR`.
- **In:** none.
- **Out:** JSON array of `{"name", "size_bytes"}`, sorted by filename. Returns
  `[]` if `ISO_DIR` does not exist. Reads the directory only; no writes.

### `POST /api/isos` → `api_upload_iso(file)`
Stream-upload an ISO into `ISO_DIR`.
- **In:** `file` — a multipart `UploadFile`.
- **Out:** JSON `{"status": "uploaded", "name", "size_bytes"}`. Side effects:
  creates `ISO_DIR` if missing; writes the file to `ISO_DIR/<base>.iso`; calls
  `push_log` with the saved name and size in MB. Raises `HTTPException(400)` if
  the uploaded filename does not end in `.iso` (case-insensitive).

### `DELETE /api/isos/{name}` → `api_delete_iso(name)`
Delete a named ISO.
- **In:** `name` — path segment naming the ISO to remove.
- **Out:** JSON `{"status": "deleted", "name"}`. Side effect: unlinks the file
  and calls `push_log`. Raises `HTTPException(404)` if the resolved path is not
  an existing regular file.

### `ISO_DIR`
Module constant `Path("/mnt/bedrock/iso")` — the SeaweedFS FUSE mount, identical
on every cluster node.

## How it works

All three handlers resolve paths under `ISO_DIR`. Because that directory is the
FUSE mount backed by the filer, file operations propagate cluster-wide rather
than living node-local, and a single path serves both the API and `virt-install`.

```
operator → POST /api/isos (multipart)
              │
              ├─ guard: filename must end ".iso" (case-insensitive) else 400
              ├─ ISO_DIR.mkdir(parents=True, exist_ok=True)
              ├─ src  = Path(file.filename).name        # strip any directory
              ├─ base = src[:-4] if len>4 else src       # drop the extension
              ├─ dst  = ISO_DIR / f"{base}.iso"          # extension → lowercase
              └─ loop: read 1 MiB → write → tally total  # chunked, low memory
                      │
                  push_log("ISO uploaded: <name> (<MB> MB)")
                      │
              SeaweedFS filer replicates per /iso/ policy
                      │
           visible to GET /api/isos and to virt-install on every node
```

Upload normalisation: the source filename is reduced to its basename (any
directory component dropped), then the trailing 4 characters are treated as the
extension and replaced with `.iso`, so the saved name always ends in lowercase
`.iso` while the operator's basename is preserved. Reads happen in 1 MiB chunks
so multi-GB Windows ISOs do not balloon memory.

Listing is case-insensitive on the extension (`p.suffix.lower() == ".iso"`), so
files arriving as `.ISO` still appear. Each entry's `stat()` is wrapped in a
try/except that skips a file on error rather than failing the whole listing.

Delete guards path traversal by reducing the supplied `name` to `Path(name).name`
before joining it under `ISO_DIR`, and only unlinks when the resolved path is an
existing regular file.

## Why

Writes target the FUSE mount directly so one path serves both the API and
`virt-install`; uploads are then immediately readable by libvirt on any node
without a second mirror or symlink step.
