# `mgmt/tasks.py`

**Module purpose.** Long-running background tasks the dashboard
needs to track: ISO import (qemu-img convert + virtio-win drivers
inject), VM export (kopia restore + virt-v2v), import jobs from
external hypervisors. Each task gets a `job_id`, gets persisted
in rqlite under `jobs`, and emits log lines that the dashboard
streams via WebSocket.

## Functions

- `start_import_job(file_path, target_name) -> str` — kick off
  ISO/qcow2 import; returns job_id.
- `start_export_job(vm_name, target_id) -> str` — kick off
  export to a backup target.
- `start_v2v_job(source, target_host) -> str` — `virt-v2v`
  conversion from VMware/Hyper-V.
- `cancel_job(job_id) -> bool` — best-effort SIGTERM.
- `job_status(job_id) -> dict` — current state + last log.
- `subscribe_logs(job_id) -> Iterator[str]` — WebSocket-friendly
  generator that yields new log lines as they appear.

Internal: jobs run as subprocesses with stdout/stderr piped into
the job's log file under `/var/lib/bedrock/jobs/<id>.log`.
mgmt's WS handler streams that file with tail-f semantics.
