# Non-confidential NVIDIA GPU deployment

This profile installs `qemu-nvidia-gpu-runtime-rs`, registered with containerd as
`kata-qemu-nvidia-gpu-runtime-rs`. CPU remains the package default. Both profiles
use the same Kata-versioned package image and EROFS memory mode with guest
dm-verity. [GPU CC deployment](GPU-CC.md) covers the SNP/TDX profiles and their
Nydus snapshotter.

## Host prerequisites

Use the [common host requirements](README.md). The pinned Kata payload includes
this Rust GPU profile for amd64 and arm64. GPU passthrough has not been tested
on hardware for this integration.

The managed deployment converges GPUs to CC-off mode and persistent VFIO
bindings through kata-device-provisioner. It does not assume a node is still
in the mode left by a previous deployment. The node needs IOMMU/IOMMUFD support,
VFIO-capable kernel drivers and the provisioner's host prerequisites. Board
fabric configuration remains separate. Do not run another device plugin
advertising `nvidia.com/gpu` on these nodes.

The required order is **NFD, device provisioner, Kata runtime, device plugin**.
GPU Operator is not a dependency. NFD is required by this provisioning path,
even though the read-only device plugin itself does not require NFD.

## Install NFD first

If the cluster already has NFD, reuse it after checking its CRDs and workers
are ready and that it supports the labels below. Do not install a second copy.
For a new deployment, from the repository root:

```bash
helm upgrade --install nfd oci://registry.k8s.io/nfd/charts/node-feature-discovery \
  --version 0.20.1 \
  --namespace node-feature-discovery --create-namespace \
  --values kata-containers/node-feature-discovery.values.yaml \
  --wait --timeout 5m
kubectl wait --for=condition=Established \
  crd/nodefeaturerules.nfd.k8s-sigs.io --timeout=60s
kubectl rollout status daemonset/nfd-worker -n node-feature-discovery --timeout=5m
```

Run the following provisioning step only after these checks succeed. The
provisioner values explicitly disable its nested NFD dependency. Its
NodeFeatureRule needs the NFD CRD, and its GPU selector waits for the labels
produced by NFD.

## Provision GPUs with CC off

Label the target nodes, then run the pinned provisioner release:

```bash
kubectl label node <node> nodewright.nvidia.com/kata-profile=kata-containers-gpu
helm upgrade --install kata-device-provisioner \
  oci://ghcr.io/kata-containers/kata-device-provisioner-charts/kata-device-provisioner \
  --version 0.1.0-alpha.1 \
  --namespace kata-system --create-namespace \
  --values kata-containers/kata-device-provisioner-gpu.values.yaml \
  --reuse-values \
  --wait --wait-for-jobs --timeout 40m
kubectl wait --for=jsonpath='{.metadata.labels.nodewright\.nvidia\.com/kata-gpu-mode}'=off \
  node/<node> --timeout=5m
```

This is an upstream prerelease. All GPU profiles share this provisioner release;
`--reuse-values` preserves any SNP/TDX profiles already configured in it. The provisioner refuses busy devices, performs
any required mode change and reset, writes persistent boot configuration, binds
the devices and verifies the hardware before publishing readiness labels.
Already prepared devices converge without an unnecessary mode change.

The release creates temporary privileged per-node Jobs, so its namespace must
allow them under your cluster's Pod Security policy. SELinux-enforcing nodes
also need the provisioner's documented SELinux configuration. A successful
Helm operation is not enough on its own: verify the selected node carries
`nodewright.nvidia.com/kata-gpu-mode=off` and
`nvidia.com/cc.ready.state=false` before applying the runtime.

The dispatcher is one-shot. New nodes joining a pool need another provisioning
run through `helm upgrade`; NFD discovery and NodeWright reconciliation alone
do not provision their devices.

## Install the runtime

The package image must be published before using the default manifests. For a
POC before publication, build the package as described in the [package guide](README.md)
and set the GPU manifest's package `image` to your own repository.

From the repository root:

```bash
kubectl apply -f kata-containers/nodewright-gpu.yaml \
  -f kata-containers/nvidia-gpu-runtimeclass.yaml
kubectl wait --for=condition=Ready nodewright/kata-containers-gpu --timeout=20m
```

Use the same files for amd64 and arm64. An existing node-pool label can replace
the profile selector in both the NodeWright and provisioner values, as
described in the package guide. Keep the mode and NFD hardware checks. The
RuntimeClass and device plugin select the operator's completion label, so the
pool label does not need to be repeated in their configuration.

NodeWright requires the provisioner's CC-off readiness and NFD GPU label before
installing the runtime. CPU and GPU installations use disjoint node groups. Do not select a node with
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
`nodewright.nvidia.com/status_kata-containers-gpu: complete` plus the verified
CC-off state instead. Install
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

The provisioner release is shared with any CC profiles. Uninstall it only
after all of its workloads and Kata installations have been removed:

```bash
helm uninstall kata-device-provisioner --namespace kata-system
```

Uninstalling the provisioner removes persistent binding configuration; it does
not promise to restore a different CC mode or immediately rebind a host driver.
Keep shared NFD running if other node profiles use it.

Remove the NodeWright resource after host cleanup. Do not manually set its
completion label or remove finalizers to skip uninstall.
