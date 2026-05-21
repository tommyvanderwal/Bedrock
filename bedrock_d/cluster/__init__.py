"""Cluster-wide runtime operations.

Run-time sagas that touch cluster-wide state but are neither
install-time (``bedrock_d/install/``) nor per-VM
(``bedrock_d/vm/``). Today: ``cluster_rename``. Future home for
``cluster_witness_add`` / ``cluster_witness_remove`` /
``cluster_backup_target_set`` and similar.
"""
