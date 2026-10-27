# Non-confidential NVIDIA GPU deployment

Use the [NodeWright-managed stack POC](stack/README.md) on this test branch.
Its CC-off image installs NFD, provisions idle GPUs into CC-off mode, installs
`qemu-nvidia-gpu-runtime-rs`, and deploys kata-device-plugin through NodeWright.
The same manifest supports amd64 and arm64. GPU Operator is not a dependency.

The earlier standalone manifests and Helm values remain available for reviewing
the underlying package changes, but they are not the full-stack POC entry point.
Follow the stack guide for installation, workloads and cleanup.
