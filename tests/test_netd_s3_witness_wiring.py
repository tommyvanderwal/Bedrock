"""netd wiring that activates the S3 witness backend: the off-hot-path worker
resolves the lightweight refs (set by the 1Hz tick from the view) into S3 clients
by unsealing each endpoint's secret from rqlite, then runs the slot IO. The secret
is read HERE (worker), never on the tick and never in the snapshot."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "installer"))

from lib import witness                 # noqa: E402
from lib import witness_s3 as w3        # noqa: E402
from lib import bedrock_state as bs     # noqa: E402
from lib import netd                    # noqa: E402


def _ws():
    return witness.WitnessState(cluster_uuid="c" * 16, cluster_key=b"k" * 32,
                                my_node_id=1)


_EP = {"type": "s3", "s3_endpoint": "https://s3.example.com", "s3_bucket": "bk",
       "s3_region": "eu-central-1", "s3_prefix": "wit/", "s3_access_key": "AK"}


def test_drive_resolves_refs_to_clients_and_runs_io(monkeypatch):
    ws = _ws()
    ws.configured_s3_witness_refs = [("w1", "ep1", _EP)]
    # unseal the secret from rqlite (mocked)
    monkeypatch.setattr(bs, "storage_endpoint_secret",
                        lambda eid, which, **k: "SECRET" if which == "s3_secret_key" else "")
    seen = {}
    monkeypatch.setattr(w3, "run_io_cycle",
                        lambda ws_, **k: seen.update(specs=list(ws_.configured_s3_witnesses)))
    netd._drive_s3_witnesses(ws)
    # one resolved (wid, S3Config), built from the endpoint + the unsealed secret
    assert len(ws.configured_s3_witnesses) == 1
    wid, cfg = ws.configured_s3_witnesses[0]
    assert wid == "w1"
    assert isinstance(cfg, w3.S3Config)
    assert cfg.bucket == "bk" and cfg.region == "eu-central-1"
    assert cfg.secret_key == "SECRET"
    assert seen.get("specs")              # run_io_cycle saw the resolved spec


def test_drive_skips_a_witness_whose_secret_cant_be_read(monkeypatch):
    ws = _ws()
    ws.configured_s3_witness_refs = [("good", "ep-ok", _EP),
                                     ("bad", "ep-bad", _EP)]

    def _secret(eid, which, **k):
        if eid == "ep-bad":
            raise RuntimeError("rqlite transient")
        return "S"

    monkeypatch.setattr(bs, "storage_endpoint_secret", _secret)
    monkeypatch.setattr(w3, "run_io_cycle", lambda ws_, **k: None)
    logs = []
    netd._drive_s3_witnesses(ws, log=logs.append)
    ids = [wid for wid, _ in ws.configured_s3_witnesses]
    assert ids == ["good"]                # bad one skipped, good one survives
    assert any("bad" in m for m in logs)  # the skip was logged (fail-loud)
