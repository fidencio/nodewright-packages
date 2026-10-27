# Confidential NVIDIA GPU deployment

Use the [NodeWright-managed stack POC](stack/README.md) on this test branch.
CC-on offers `qemu-nvidia-gpu-snp-runtime-rs` and
`qemu-nvidia-gpu-tdx-runtime-rs`. PPCIE supports TDX only. Both require amd64
and install the bundled Nydus snapshotter through Kata's finite lifecycle.

NodeWright installs or reuses NFD before discovery and provisioning, runs
kata-device-provisioner on the drained node, then installs Kata and the device
plugin. GPU Operator is not a dependency. Follow the stack guide for the four
ready manifests, host requirements, verification scope and cleanup.

This integration has not been tested on GPU or confidential-computing hardware.
Attestation services and workload secret-release policies are outside this stack.
