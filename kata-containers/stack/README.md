# NodeWright-managed GPU stack POC

These manifests let NodeWright deploy the complete GPU stack on an existing
Kubernetes cluster. NodeWright installs or reuses NFD first, waits for hardware
discovery, runs kata-device-provisioner on the drained node, installs Kata,
and deploys the kata-device-plugin Helm release. GPU Operator is not required.
There is no kata-deploy DaemonSet or separate provisioner dispatcher.

The three package images contain Kata 4.3.1, kata-device-provisioner
0.1.0-alpha.1, NFD chart 0.20.1 and kata-device-plugin chart 0.2.0-rc.2.
The manifests pin published test images by digest. SNP and TDX CC-on use the
same image with different runtime selections. PPCIE supports TDX only.

## Published POC images

All images use repository `ghcr.io/fidencio/kata-containers/test-images`.
The manifests pin their immutable digests rather than relying on tags.

| Mode | Tag | Architectures |
| --- | --- | --- |
| CC-off | `nodewright-4.3.1-cc-off-4bcf9b7` | amd64, arm64 |
| CC-on | `nodewright-4.3.1-cc-on-4bcf9b7` | amd64 |
| PPCIE | `nodewright-4.3.1-ppcie-4bcf9b7` | amd64 |

## Requirements

Start with Kubernetes and a running containerd service. The NodeWright
operator must be installed in namespace `nodewright`. If it is not installed:

```bash
helm upgrade --install nodewright \
  oci://ghcr.io/nvidia/nodewright/charts/nodewright \
  --version v0.20.0 --namespace nodewright --create-namespace --wait
```

For a single-node POC, the controller must tolerate the cordoned node so it
can keep managing the drain. Keep two replicas, as required by the chart's
PodDisruptionBudget, and apply these operator settings:

```bash
cat > /tmp/nodewright-single-node.yaml <<'EOF'
controllerManager:
  replicas: 2
  tolerations:
    - key: node.kubernetes.io/unschedulable
      operator: Exists
      effect: NoSchedule
    - key: node-role.kubernetes.io/control-plane
      operator: Exists
      effect: NoSchedule
EOF
helm upgrade nodewright \
  oci://ghcr.io/nvidia/nodewright/charts/nodewright \
  --version v0.20.0 --namespace nodewright --reuse-values \
  -f /tmp/nodewright-single-node.yaml --wait --timeout 5m
```

Use the [common host requirements](../README.md#install), including
KVM and IOMMU/IOMMUFD support. CC-off uses the EROFS snapshotter and needs
containerd 2.2 or newer and erofs-utils 1.8.2 or newer. CC-on and PPCIE install
the bundled Nydus snapshotter. Their hosts must have SNP or TDX enabled as
selected below. Firmware, kernel and board configuration must support the
requested GPU mode. Fabric setup remains a host prerequisite.

GPUs must be idle and available for passthrough. Remove GPU workloads and
conflicting device plugins from the target nodes first. Do not run another
plugin advertising `nvidia.com/gpu` on the same nodes. Do not select a node
already managed by another Kata NodeWright; uninstall that profile first.
The NodeWright namespace must allow privileged host-management pods.

## Deploy

Clone this test branch on the machine where `kubectl` accesses the cluster:

```bash
git clone --branch feat/kata-containers-gpu-test \
  https://github.com/fidencio/nodewright-packages.git
cd nodewright-packages
kubectl apply -f kata-containers/stack/bootstrap.yaml
```

Bootstrap creates dedicated installer permissions and a Kubernetes-generated
service-account token Secret. The package receives that Secret through
`secretKeyRef`; credentials are never part of the image. Existing namespace
default service accounts receive no additional permissions.

Choose one profile for each node or pool:

| GPU mode | CPU TEE | Architectures | Profile | Manifest |
| --- | --- | --- | --- | --- |
| off | none | amd64, arm64 | kata-gpu-off | cc-off.yaml |
| on | SNP | amd64 | kata-gpu-snp-on | cc-on-snp.yaml |
| on | TDX | amd64 | kata-gpu-tdx-on | cc-on-tdx.yaml |
| ppcie | TDX | amd64 | kata-gpu-tdx-ppcie | ppcie-tdx.yaml |

For CC-off:

```bash
kubectl label node <node> nodewright.nvidia.com/kata-profile=kata-gpu-off
kubectl apply -f kata-containers/stack/cc-off.yaml
kubectl get nodewright kata-gpu-off -w
```

For another profile, use its profile label and manifest from the table.
A pool can be selected with `kubectl label nodes -l <existing-pool-label>=<value>`
in place of `kubectl label node <node>`. New matching nodes are provisioned
through NodeWright reconciliation; no additional Helm command is needed.

Wait until NodeWright reports `complete` for every selected node. Each stage attempt
has a 45-minute timeout; retries can extend the total time. The node
is drained before hardware changes. NFD and the device plugin tolerate the
cordoned node. Runtime readiness is published only after the plugin is Ready
and the node advertises GPU capacity:

```bash
kubectl get nodes -l nodewright.nvidia.com/kata-profile=kata-gpu-off
kubectl get pods -n node-feature-discovery
kubectl get pods -n kata-device-plugin
kubectl get node <node> -o jsonpath='{.status.allocatable.nvidia\.com/gpu}{"\n"}'
```

No manual image replacement, architecture substitution, containerd version
parameter or dependency Helm installation is needed. Containerd's running
version is detected automatically. For a different config filename under
`/etc/containerd`, set the manifest's `CONTAINERD_CONFIG_FILE_NAME` environment
entry to that basename before applying it. The service remains `containerd`.

## Run a workload

CC-off:

```bash
kubectl apply -f kata-containers/gpu-pod.yaml
kubectl wait --for=jsonpath='{.status.phase}'=Succeeded pod/kata-gpu-smoke --timeout=10m
kubectl logs kata-gpu-smoke
```

For SNP use `gpu-snp-pod.yaml` and `pod/kata-gpu-snp-smoke`. For TDX CC-on
or PPCIE use `gpu-tdx-pod.yaml` and `pod/kata-gpu-tdx-smoke`. RuntimeClass names
are independent of node architecture and GPU mode. TDX CC-on and PPCIE can
share their RuntimeClass while using disjoint node groups.

The sample requests one GPU and runs `nvidia-smi -L`. Boards requiring
NVSwitch resources or whole-board allocation need a workload sized for the
board. CC workloads also need your attestation and secret-release policies;
this stack does not configure an attestation service.

## Uninstall

Remove workloads first. Keep the node profile label and runtime selection
until NodeWright finishes host cleanup. For CC-off:

```bash
kubectl delete pod kata-gpu-smoke --ignore-not-found
kubectl patch nodewright kata-gpu-off --type=merge \
  -p '{"spec":{"packages":{"kata-containers":{"uninstall":{"enabled":true,"apply":true}}}}}'
kubectl get nodewright kata-gpu-off -w
```

After cleanup completes, remove the NodeWright and its RuntimeClass using
the selected manifest. Do not delete the shared TDX RuntimeClass while another
TDX profile still uses it. NodeWright removes the device-plugin release after
the last node in that stack is uninstalled. Shared NFD remains installed.
Provisioner cleanup removes persistent binding configuration; it does not
promise to reset CC mode or immediately restore a host driver.

Delete bootstrap resources only after all managed GPU stacks have been
removed. This revokes the installer token. Keep NFD if other consumers use it.

## Verification scope

The package lifecycle and orchestration tests pass in containers. Image builds
verify the ELF architecture of native tools independently of binfmt emulation. Fresh NFD
installation through the dedicated bootstrap identity ran on an Ubuntu 26.04
arm64 Kubernetes VM, where the existing CPU Kata workload remained complete.
GPU passthrough, GPU mode changes, SNP and TDX have not run on hardware for
this integration. The published images are POC artifacts, not official NVIDIA
package releases.
