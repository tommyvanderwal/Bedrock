resource vm-hero-disk0 {
    protocol C;
    net {
        allow-two-primaries no;
        after-sb-0pri  discard-zero-changes;
        after-sb-1pri  discard-secondary;
        after-sb-2pri  disconnect;
    }
    on bedrock-sim-1.bedrock.local {
        node-id 0; device /dev/drbd1010;
        disk /dev/almalinux/vm-hero-disk0;
        address 10.99.0.10:8010;
        meta-disk /dev/almalinux/vm-hero-disk0-meta;
    }
    on bedrock-sim-2.bedrock.local {
        node-id 1; device /dev/drbd1010;
        disk /dev/almalinux/vm-hero-disk0;
        address 10.99.0.11:8010;
        meta-disk /dev/almalinux/vm-hero-disk0-meta;
    }
    on bedrock-sim-3.bedrock.local {
        node-id 2; device /dev/drbd1010;
        disk /dev/almalinux/vm-hero-disk0;
        address 10.99.0.12:8010;
        meta-disk /dev/almalinux/vm-hero-disk0-meta;
    }
    connection-mesh {
        hosts bedrock-sim-1.bedrock.local bedrock-sim-2.bedrock.local bedrock-sim-3.bedrock.local;
    }
}