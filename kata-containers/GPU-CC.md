# Confidential NVIDIA GPU deployment

This adds `qemu-nvidia-gpu-snp-runtime-rs` and
`qemu-nvidia-gpu-tdx-runtime-rs` to the same Kata-versioned package image. Both
CC profiles require amd64. Their containerd snapshotter is Nydus, installed
and removed through Kata's finite stages; CPU and basic GPU profiles retain
EROFS. GPU Operator is not required.

The required order is **NFD ready, TEE discovery rules, device provisioning,
Kata runtime with Nydus, device plugin, workloads**. The manifests gate runtime
installation on discovery and verified provisioning labels, rather than
assuming that applying resources together makes their dependencies ready.

Neither confidential runtime nor GPU passthrough has been tested on hardware
for this integration. NFD deployment, Helm rendering, lifecycle tests and
Kubernetes validation do not replace a CC hardware test.

## Prepare the hosts

The host must support and have enabled the selected CPU TEE, IOMMU/IOMMUFD and
GPU passthrough. Firmware, host kernel and GPU board configuration must support
the requested mode. Choose `on` or `ppcie` explicitly for the board; these are
not interchangeable. See the provisioner's
[hardware profiles](https://github.com/kata-containers/kata-device-provisioner/tree/main/deploy/helm/kata-device-provisioner/profiles).
The provisioner verifies idle devices before a mode change or reset. Remove
GPU workloads before changing modes; a refusal is a failure to resolve, not a
reason to bypass the idle check. Fabric setup remains a separate prerequisite.

This deployment installs the runtime and prepares devices. It does not configure
an attestation service, appraisal policy or secret-release policy. Supply those
for workloads whose confidentiality depends on verified measurements or
protected image keys. A successful `nvidia-smi` pod alone does not demonstrate
attestation or key-release policy.

## Install NFD first

Complete [Install NFD first](GPU.md#install-nfd-first) before running any
provisioner release. It pins NFD 0.20.1 and waits for the release, its
NodeFeatureRule CRD and the worker DaemonSet. Reuse an existing NFD installation
only after the same readiness checks; do not install a second copy.

Then apply the TEE rules from the repository root:

```bash
kubectl apply -f kata-containers/tee-node-feature-rule.yaml
```

NFD publishes `amd.feature.node.kubernetes.io/snp=true` or
`intel.feature.node.kubernetes.io/tdx=true` on hosts where that TEE is enabled,
and advertises `sev-snp.amd.com/esids` or `tdx.intel.com/keys`. Each CC RuntimeClass
accounts for one key through pod overhead so the scheduler cannot oversubscribe
the TEE key slots. Keep NFD and the rule present while CC workloads use them.

## Deploy an SNP GPU group

Label the intended nodes and wait for NFD to confirm SNP support. Select the
GPU mode explicitly; the example uses `on`, while boards requiring PPCIE must
use `ppcie` instead:

```bash
kubectl label node <node> nodewright.nvidia.com/kata-profile=kata-containers-gpu-snp
kubectl wait --for=jsonpath='{.metadata.labels.amd\.feature\.node\.kubernetes\.io/snp}'=true \
  nodes -l nodewright.nvidia.com/kata-profile=kata-containers-gpu-snp --timeout=5m
KATA_CC_MODE=on
helm upgrade --install kata-device-provisioner \
  oci://ghcr.io/kata-containers/kata-device-provisioner-charts/kata-device-provisioner \
  --version 0.1.0-alpha.1 \
  --namespace kata-system --create-namespace \
  --values kata-containers/kata-device-provisioner-snp.values.yaml \
  --set-string profiles.snp.ccMode="$KATA_CC_MODE" \
  --reuse-values --wait --wait-for-jobs --timeout 40m
kubectl wait --for=jsonpath='{.metadata.labels.nodewright\.nvidia\.com/kata-gpu-snp-mode}'="$KATA_CC_MODE" \
  nodes -l nodewright.nvidia.com/kata-profile=kata-containers-gpu-snp --timeout=5m
kubectl apply -f kata-containers/nodewright-gpu-snp.yaml \
  -f kata-containers/nvidia-gpu-snp-runtimeclass.yaml
kubectl wait --for=condition=Ready nodewright/kata-containers-gpu-snp --timeout=20m
helm upgrade --install kata-device-plugin-snp \
  oci://ghcr.io/kata-containers/kata-device-plugin-charts/kata-device-plugin \
  --version 0.2.0-rc.2 --namespace kata-device-plugin --create-namespace \
  --values kata-containers/kata-device-plugin-snp.values.yaml --wait --timeout 5m
```

The provisioner chart is an upstream alpha and the plugin chart an upstream
release candidate. Their versions are independently pinned from Kata 4.3.1.
The values disable the provisioner's nested NFD chart: NFD must already be
running. The provisioner creates GPU discovery rules and temporary privileged,
token-free per-node Jobs; its namespace must permit them under cluster policy.
SELinux-enforcing hosts need the provisioner's documented SELinux settings.

The package image must be published before using the default NodeWright
manifest. Before publication, point its package `image` at your POC registry,
as described in the [package guide](README.md).

## Deploy a TDX GPU group

Use the same order. The corresponding commands are:

```bash
kubectl label node <node> nodewright.nvidia.com/kata-profile=kata-containers-gpu-tdx
kubectl wait --for=jsonpath='{.metadata.labels.intel\.feature\.node\.kubernetes\.io/tdx}'=true \
  nodes -l nodewright.nvidia.com/kata-profile=kata-containers-gpu-tdx --timeout=5m
KATA_CC_MODE=on
helm upgrade --install kata-device-provisioner \
  oci://ghcr.io/kata-containers/kata-device-provisioner-charts/kata-device-provisioner \
  --version 0.1.0-alpha.1 \
  --namespace kata-system --create-namespace \
  --values kata-containers/kata-device-provisioner-tdx.values.yaml \
  --set-string profiles.tdx.ccMode="$KATA_CC_MODE" \
  --reuse-values --wait --wait-for-jobs --timeout 40m
kubectl wait --for=jsonpath='{.metadata.labels.nodewright\.nvidia\.com/kata-gpu-tdx-mode}'="$KATA_CC_MODE" \
  nodes -l nodewright.nvidia.com/kata-profile=kata-containers-gpu-tdx --timeout=5m
kubectl apply -f kata-containers/nodewright-gpu-tdx.yaml \
  -f kata-containers/nvidia-gpu-tdx-runtimeclass.yaml
kubectl wait --for=condition=Ready nodewright/kata-containers-gpu-tdx --timeout=20m
helm upgrade --install kata-device-plugin-tdx \
  oci://ghcr.io/kata-containers/kata-device-plugin-charts/kata-device-plugin \
  --version 0.2.0-rc.2 --namespace kata-device-plugin --create-namespace \
  --values kata-containers/kata-device-plugin-tdx.values.yaml --wait --timeout 5m
```

Choose `ppcie` instead of `on` where the GPU board requires it. Both provisioner
values files intentionally omit `ccMode`; an initial render without an explicit
mode fails. NodeWright accepts only verified `on` or `ppcie` states for these
CC profiles. `off` and `devtools` are not CC deployment modes here.

## Pools, dependencies and runtime selection

All GPU groups share the single `kata-device-provisioner` Helm release in
`kata-system`. `--reuse-values` preserves already enabled profiles when adding
another. Separate provisioner releases would conflict over their shared GPU
NodeFeatureRule. The device plugin has a distinct release per disjoint group.
CPU nodes need neither NFD nor either GPU dependency.

An existing pool label can replace the profile label in both the provisioner
and NodeWright selectors. Keep the GPU and TEE discovery checks and the
provisioning-state gates. The RuntimeClass and plugin selectors remain based
on installation completion and readiness. Future pool members require another
provisioner `helm upgrade`; the provisioner is a one-shot dispatcher, not an
autoscaling controller.

Only one Kata profile may manage `/opt/kata` on a node. Uninstall the old
runtime before moving a node between CPU, basic GPU, SNP and TDX groups, and
complete the required provisioning before installing the new runtime. Runtime
checks also require the Nydus executable and service to be active for CC.
The plugin waits for runtime completion, so NodeWright cannot also wait for
that plugin without creating a dependency cycle. Workload GPU requests wait
for the plugin to advertise real device capacity.

## Run a validation workload

For SNP:

```bash
kubectl apply -f kata-containers/gpu-snp-pod.yaml
kubectl wait --for=jsonpath='{.status.phase}'=Succeeded pod/kata-gpu-snp-smoke --timeout=10m
kubectl logs kata-gpu-snp-smoke
```

For TDX, use `gpu-tdx-pod.yaml` and `pod/kata-gpu-tdx-smoke`. The pods request one
`nvidia.com/gpu`; their RuntimeClass consumes one TEE key. Boards requiring
NVSwitch resources or whole-board allocation need a workload sized for that
board. Validate attestation and protected-data handling separately for your
actual workload and policy.

## Uninstall

Remove workloads first. Keep the current `KATA_RUNTIME` until its uninstall
completes. For SNP:

```bash
kubectl delete pod kata-gpu-snp-smoke --ignore-not-found
kubectl patch nodewright kata-containers-gpu-snp --type=merge \
  -p '{"spec":{"packages":{"kata-containers":{"uninstall":{"enabled":true,"apply":true}}}}}'
```

Verify its containerd handler, shim, Nydus executable and systemd unit have
been removed and the node is Ready. Nydus snapshot data is intentionally
preserved by Kata's cleanup. Then remove this group's plugin and RuntimeClass:

```bash
helm uninstall kata-device-plugin-snp --namespace kata-device-plugin
kubectl delete -f kata-containers/nvidia-gpu-snp-runtimeclass.yaml
```

Use the TDX names for TDX cleanup. Do not uninstall the shared provisioner while
other profiles or workloads still need its bindings. Retire all GPU runtimes
and workloads before removing that release; its cleanup removes persistent
binding configuration but does not promise to reset CC mode or immediately
restore host driver bindings. Remove the shared TEE rule and NFD only after
all CC workloads and GPU provisioning dependencies have been retired.
