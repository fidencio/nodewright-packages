# Non-confidential NVIDIA GPU deployment

This profile installs `qemu-nvidia-gpu-runtime-rs`, registered with containerd as
`kata-qemu-nvidia-gpu-runtime-rs`. CPU remains the package default. Both profiles
use the same Kata-versioned package image and EROFS memory mode with guest
dm-verity. SNP, TDX, confidential GPU provisioning and nydus are outside this
integration.

## Host prerequisites

Use the [common host requirements](README.md). The pinned Kata payload includes
this Rust GPU profile for amd64 and arm64. GPU passthrough has not been tested
on hardware for this integration.

Before selecting a node, prepare its NVIDIA GPUs for non-confidential
passthrough: IOMMU and IOMMUFD enabled, GPUs bound to `vfio-pci`, and VFIO cdevs
available under `/dev/vfio/devices/`. Provision the board's required NVSwitch
and fabric configuration separately. The device plugin only advertises
already prepared devices; it never changes CC mode, binds devices or configures
fabric. This integration does not install a provisioner or host GPU driver.

Do not run another device plugin advertising `nvidia.com/gpu` on these nodes.
GPU Operator and NFD are not required. NFD labels used by SNP/TDX profiles in
kata-deploy's all-GPU chart are not used by this non-confidential profile.

## Install the runtime

The package image must be published before using the default manifests. For a
POC before publication, build the package as described in the [package guide](README.md)
and set the GPU manifest's package `image` to your own repository.

From the repository root:

```bash
kubectl label node <node> nodewright.nvidia.com/kata-profile=kata-containers-gpu
kubectl apply -f kata-containers/nodewright-gpu.yaml \
  -f kata-containers/nvidia-gpu-runtimeclass.yaml
kubectl wait --for=condition=Ready nodewright/kata-containers-gpu --timeout=20m
```

Use the same files for amd64 and arm64. An existing node-pool label can replace
the NodeWright's profile selector, as described in the package guide. The
RuntimeClass and device plugin select the operator's completion label, so the
pool label does not need to be repeated in their configuration.

CPU and GPU installations use disjoint node groups. Do not select a node with
both NodeWrights or run both installations against `/opt/kata`. Uninstall the
old profile before changing profiles; this also removes its containerd handler.
Keep `KATA_RUNTIME` set to the old profile until uninstall completes.

## Install the device plugin

Deploy [kata-device-plugin](https://github.com/kata-containers/kata-device-plugin)
as its own pinned Helm release. It is a cluster workload dependency, not a
NodeWright host package or a `dependsOn` entry. Version `0.2.0-rc.2` is an
upstream prerelease; the chart and plugin versions are released together.

```bash
helm upgrade --install kata-device-plugin \
  oci://ghcr.io/kata-containers/kata-device-plugin-charts/kata-device-plugin \
  --version 0.2.0-rc.2 \
  --namespace kata-device-plugin --create-namespace \
  --values kata-containers/kata-device-plugin.values.yaml \
  --wait --timeout 5m
```

The values file clears the chart's default `katacontainers.io/kata-runtime`
selector, which NodeWright's finite stages do not set, and selects
`nodewright.nvidia.com/status_kata-containers-gpu: complete` instead. Install
NodeWright's runtime first: gating NodeWright completion on a plugin that waits
for that same completion label would create a dependency cycle.

The plugin advertises `nvidia.com/gpu` and, where present, `nvidia.com/nvswitch`.
Its DaemonSet being Ready does not prove that GPUs are available. Check that the
node advertises the expected capacity before running workloads:

```bash
kubectl get node <node> -o jsonpath='{.status.allocatable.nvidia\.com/gpu}'
```

## Run a GPU workload

```bash
kubectl apply -f kata-containers/gpu-pod.yaml
kubectl wait --for=jsonpath='{.status.phase}'=Succeeded pod/kata-gpu-smoke --timeout=10m
kubectl logs kata-gpu-smoke
```

The pod uses `kata-containers-qemu-nvidia-gpu-runtime-rs`, requests one
`nvidia.com/gpu`, and runs `nvidia-smi -L`. Kubernetes waits for device-plugin
capacity before scheduling it. The supplied CUDA image is published for amd64
and arm64. A completed pod listing its GPU is the hardware validation; template
rendering or a Ready plugin on a node with zero devices is not.

Boards requiring additional devices, such as NVSwitches, need a workload sized
for that board; the single-GPU pod does not validate such topologies.

## Uninstall

Delete GPU workloads first. Uninstall the runtime through NodeWright while the
GPU profile remains selected, then remove the device plugin release and
RuntimeClass:

```bash
kubectl delete pod kata-gpu-smoke --ignore-not-found
kubectl patch nodewright kata-containers-gpu --type=merge \
  -p '{"spec":{"packages":{"kata-containers":{"uninstall":{"enabled":true,"apply":true}}}}}'
```

Verify the GPU handler and shim have been removed and the node is Ready before
removing the remaining resources:

```bash
helm uninstall kata-device-plugin --namespace kata-device-plugin
kubectl delete -f kata-containers/nvidia-gpu-runtimeclass.yaml
```

Remove the NodeWright resource after host cleanup. Do not manually set its
completion label or remove finalizers to skip uninstall.
