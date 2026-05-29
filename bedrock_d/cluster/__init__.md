# bedrock_d/cluster/__init__.py

Package marker for `bedrock_d.cluster`: the home for run-time sagas that touch cluster-wide state but are neither install-time (`bedrock_d/install/`) nor per-VM (`bedrock_d/vm/`).

Contains `rename.py` (the `cluster_rename` saga). The `__init__.py` is a docstring-only marker — it defines no symbols and re-exports nothing; importers reach the saga modules directly.
