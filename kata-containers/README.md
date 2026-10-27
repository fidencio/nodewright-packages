# Kata Containers package

The `kata-containers` package defaults to `qemu-nvidia-cpu-runtime-rs`,
registered with containerd as `kata-qemu-nvidia-cpu-runtime-rs`. It also supports
the non-confidential `qemu-nvidia-gpu-runtime-rs` profile described in
[GPU deployment](GPU.md). It uses standalone
containerd with `containerd.service`, EROFS memory mode and dm-verity, and the
default `/opt/kata` paths. The tested Kubernetes setup uses kubeadm; the package
does not invoke kubeadm. K3s/RKE2, MicroK8s and CRI-O are not supported here.

The node needs Linux amd64/arm64, systemd, KVM, containerd >= 2.2 and
`erofs-utils >= 1.8.2`. The CPU profile has no GPU dependencies. The GPU profile
requires NFD first, then kata-device-provisioner to establish CC-off passthrough
devices, and kata-device-plugin to advertise them. GPU Operator is not a
dependency. CPU and basic GPU profiles do not require nydus.
CPU end-to-end validation covers Ubuntu 26.04 on arm64 with erofs-utils 1.9.1.
GPU passthrough and amd64 execution have not been validated on hardware.

Stock Ubuntu 24.04 is insufficient: its
[erofs-utils 1.7.1 package](https://packages.ubuntu.com/noble/erofs-utils)
lacks the required `mkfs.erofs` options. Use Ubuntu 26.04 for the validated path.
Using 24.04 requires separately provisioning compatible erofs-utils and
containerd versions. This package checks prerequisites through kata-deploy's
host-check stage; it does not upgrade the host's erofs-utils or containerd.

## Package versions are Kata versions

```bash
docker build kata-containers \
  -t <your-registry>/nodewright-packages/kata-containers:4.3.1
docker push <your-registry>/nodewright-packages/kata-containers:4.3.1
```

The checked-in `config.json` uses the Kata release as `package_version`, so
the repository's validation and release tooling can read it directly. The build
checks this version against `/opt/kata-artifacts/VERSION` from the pinned Kata
image. A mismatch fails the build. There is no independent adapter version.

For a new Kata release, update `config.json` and the pinned Kata image together.
Release tags follow the repository convention: `kata-containers/4.3.1`. Released
images use `ghcr.io/nvidia/nodewright-packages/kata-containers:<kata-version>`.
The deployment manifests below use this official repository.
The initial package has not been released yet. For a POC before publication,
build and publish to your own registry and set `image` in the NodeWright manifest.

Use the Kata version for the published image tag and NodeWright package
`version`. Rebuilds of the same release can retain that version and use
NodeWright's `containerSHA` to pin the package image. Configuration changes still
reconcile at the same version.

The package uses the agent configured by the installed NodeWright chart. The
Dockerfile checks that kata-deploy exposes every finite command used by the
adapter.

## Install

Use [nodewright.yaml](nodewright.yaml) and
[nvidia-cpu-runtimeclass.yaml](nvidia-cpu-runtimeclass.yaml) for both amd64 and
arm64 nodes. The manifests use the official multi-architecture package image
and need no edits for the default setup once that image is published. Before
publication, set `image` to your own published package repository. A mixed
architecture group needs an image published for both amd64 and arm64.

The package detects the running containerd server version through the host's
`ctr` command using `/run/containerd/containerd.sock`. It passes that version
to kata-deploy without requiring Kubernetes API access. `CONTAINER_RUNTIME_VERSION`
remains an optional override. Set `CONTAINERD_CONFIG_FILE_NAME` only when using
a different configuration filename, as described below.

From the repository root, label each target node and apply the two files:

```bash
kubectl label node <node> nodewright.nvidia.com/kata-profile=kata-containers
kubectl apply -f kata-containers/nodewright.yaml \
  -f kata-containers/nvidia-cpu-runtimeclass.yaml
kubectl wait --for=condition=Ready nodewright/kata-containers --timeout=20m
```

Workloads on either architecture use
`runtimeClassName: kata-containers-qemu-nvidia-cpu-runtime-rs`, backed by the
`kata-qemu-nvidia-cpu-runtime-rs` containerd handler. The NodeWright selects
Linux amd64 and arm64 nodes. The RuntimeClass requires the operator-managed
`nodewright.nvidia.com/status_kata-containers: complete` label. This prevents
workloads from selecting a node before installation completes, including an
unsupported architecture that was accidentally given the group label. Do not
set the completion label manually. Only arm64 has been validated end to end.

An existing node pool works too: replace only the NodeWright's
`nodewright.nvidia.com/kata-profile` selector with the pool's existing label,
keeping the Linux selector and architecture expression. For example:

```yaml
nodeSelectors:
  matchLabels:
    kubernetes.io/os: linux
    your-domain.example/node-pool: kata-workers
  matchExpressions:
    - key: kubernetes.io/arch
      operator: In
      values: [amd64, arm64]
```

Use your cluster's actual pool label key and value. Existing and future nodes
with that label are selected automatically; no extra Kata label is needed.
The RuntimeClass stays unchanged because it selects nodes by NodeWright's
completion label.

Nodes in one group share the package version and configuration. The containerd
version is detected separately on each node. To create another group, copy
both manifests and change the NodeWright name, RuntimeClass name, and
`nodewright.nvidia.com/kata-profile` value in the NodeWright selector. Update the RuntimeClass's `status_<NodeWright-name>`
label key to match the new NodeWright name. A single-valued group label keeps
installations disjoint. Uninstall before moving an already installed node to
another group. Do not run two installations managing `/opt/kata` on the same
node.

Keep `fieldRef.apiVersion: v1` on `NODE_NAME`: API defaulting can otherwise
cause a pod-comparison mismatch with the tested operator. The operator already
supplies `COPY_RESOLV=false`; the package does not override it.

## Containerd configuration file

The default is `/etc/containerd/config.toml`. To use another file in the same
directory, including a DPU-specific configuration, set its filename in the
NodeWright package's `env` entry:

```yaml
- name: CONTAINERD_CONFIG_FILE_NAME
  value: config-dpu.toml
```

Kata uses it for installation and uninstall. Absolute paths and directory
components are rejected; the configuration directory remains `/etc/containerd`.
Use the file that the node's `containerd.service` actually loads. This setting
does not change containerd's startup arguments, service name or socket.
Before changing this value on an installed node, uninstall with the old value
so its configuration can be cleaned up, then install with the new value.

## Configuration and lifecycle

The adapter selects one NVIDIA Rust shim and EROFS. `KATA_RUNTIME` defaults to
`qemu-nvidia-cpu-runtime-rs`; the GPU manifest selects
`qemu-nvidia-gpu-runtime-rs`. Other runtimes, including SNP and TDX, are rejected. The
`containerd-user-dropin.toml` ConfigMap key disables host fs-verity for broad
host filesystem compatibility, matching Kata's NVIDIA profile; guest dm-verity
remains enabled. Runtime overrides use
`dropin-qemu-nvidia-cpu-runtime-rs.toml` or
`dropin-qemu-nvidia-gpu-runtime-rs.toml`, matching the selected profile.

Apply/upgrade validates the configuration. The config stage runs the finite
installer, including when ConfigMap values change without a Kata version bump.
Keep both the package's `noop` interrupt and its wildcard `configInterrupts`
entry so host changes run through NodeWright's cordon/drain flow. Kata-deploy
performs the actual containerd restart. The interruption budget defaults to
one node.

The package checks for the Rust shim and CPU configuration. A completed Kata
pod is the end-to-end verification of the runtime, guest agent and snapshotter.
For autoscaling, provision nodes with the
[runtime-required taint](https://github.com/NVIDIA/nodewright/blob/main/docs/user-guide/runtime-required.md) and enable `runtimeRequired: true`
to hold workloads until installation finishes. The group label exists before
installation completes.

## Uninstall

Delete workloads using this RuntimeClass before requesting uninstall:

```bash
kubectl patch nodewright <group-name> --type=merge \
  -p '{"spec":{"packages":{"kata-containers":{"uninstall":{"enabled":true,"apply":true}}}}}'
```

Kata-deploy reverts CRI configuration and restarts containerd before removing
artifacts. Verify the handler and Rust shim are absent and the node remains
Ready. Uninstall is also enabled on resource deletion. The RuntimeClass is
managed separately and should be removed after cleanup. Reapplying with
`uninstall.apply: false` reinstalls the package.

## Implementation and checks

The package follows NodeWright's standard host-step mechanism (`on_host: true`).
It contains no agent binary and does not override the operator's agent image.
NodeWright copies the scripts, installer filesystem and compressed Kata artifacts
into its package directory on the host. Allow disk space for that staged payload
in addition to the installed runtime.

Host steps require Bash, `unshare`, `mount`, `chroot`, `awk` and containerd's `ctr`. The wrapper enters a
private mount namespace and runs the bundled installer in a chroot, with the host
and ConfigMap files mounted at kata-deploy's expected paths. The namespace's
mounts disappear when the step exits. The privileged package retains full host
access; the chroot is an execution environment, not a security boundary.
The finite stages need no extra Kubernetes RBAC.

Run from the repository root:

```bash
make validate-standalone PACKAGE=kata-containers
make test-package PACKAGE=kata-containers
```

Tests in `tests/integration/kata_containers` use the existing pytest and
`DockerTestRunner` harness. Lifecycle tests stub host commands inside test
containers; they do not modify the host or boot a Kata guest. The repository's
existing package discovery includes this package in validation, tests and builds.

Tests cover Kata version agreement and mismatch rejection, CPU-only manifests,
configuration reconciliation, and lifecycle failure handling.
