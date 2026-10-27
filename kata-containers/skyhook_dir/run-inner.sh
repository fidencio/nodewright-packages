#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


set -euo pipefail

case "${1:-}" in
    validate|install|uninstall|check-installed|check-removed) action=$1 ;;
    *) echo "usage: $0 {validate|install|uninstall|check-installed|check-removed}" >&2; exit 2 ;;
esac

: "${NODE_NAME:?set NODE_NAME using spec.nodeName in the package env}"
: "${CONTAINER_RUNTIME_VERSION:?set the reported containerRuntimeVersion}"
case "$CONTAINER_RUNTIME_VERSION" in
    containerd://*) ;;
    *) echo "this package supports standalone containerd only" >&2; exit 1 ;;
esac

# This package uses the default installation paths. Reject overrides rather
# than accidentally writing a different installation into the container filesystem.
if [[ -n ${INSTALLATION_PREFIX:-}${MULTI_INSTALL_SUFFIX:-} || ${K8S_DISTRIBUTION:-k8s} != k8s ]]; then
    echo "installation prefixes, multiple installs and distribution overrides are unsupported" >&2
    exit 1
fi
export K8S_DISTRIBUTION=k8s
export CONTAINERD_CONFIG_FILE_NAME="${CONTAINERD_CONFIG_FILE_NAME:-config.toml}"
case "${CONTAINERD_CONFIG_FILE_NAME}" in
    .|..|*/*|*$'\n'*|*$'\r'*|[[:space:]]*|*[[:space:]])
        echo 'CONTAINERD_CONFIG_FILE_NAME must be a simple filename under /etc/containerd' >&2
        exit 1
        ;;
esac

node_arch="$(uname -m)"
case "${node_arch}" in
    x86_64|aarch64|arm64) ;;
    *) echo 'unsupported node architecture' >&2; exit 1 ;;
esac
runtime="${KATA_RUNTIME:-qemu-nvidia-cpu-runtime-rs}"
case "${runtime}" in
    qemu-nvidia-cpu-runtime-rs|qemu-nvidia-gpu-runtime-rs) snapshotter=erofs ;;
    qemu-nvidia-gpu-snp-runtime-rs|qemu-nvidia-gpu-tdx-runtime-rs)
        if [[ "${node_arch}" != x86_64 ]]; then
            echo 'confidential GPU Rust runtimes require an amd64 node' >&2
            exit 1
        fi
        snapshotter=nydus
        ;;
    *) echo 'KATA_RUNTIME must select a supported NVIDIA Rust runtime' >&2; exit 1 ;;
esac
export SHIMS_X86_64="$runtime" SHIMS_AARCH64="$runtime"
export DEFAULT_SHIM_X86_64="$runtime" DEFAULT_SHIM_AARCH64="$runtime"
export SNAPSHOTTER_HANDLER_MAPPING_X86_64="${runtime}:${snapshotter}"
export SNAPSHOTTER_HANDLER_MAPPING_AARCH64="${runtime}:${snapshotter}"
export EXPERIMENTAL_SETUP_SNAPSHOTTER="${snapshotter}"
export EROFS_SNAPSHOTTER_MODE="${EROFS_SNAPSHOTTER_MODE:-memory}"
export EROFS_DMVERITY="${EROFS_DMVERITY:-dmverity}"
if [[ "$action" == validate ]]; then
    exit 0
fi

# NodeWright mounts the host at /root. Keep all the additional bind mounts local
# to the installer namespace, including when a bind mount inherits propagation.
mount --make-rprivate /
mkdir -p /host
mount --bind /root /host

bind_dir() {
    mkdir -p "/root/$1" "$2"
    mount --bind "/root/$1" "$2"
}

bind_dir etc/containerd /etc/containerd
bind_dir etc/crio /etc/crio
bind_dir opt/kata /opt/kata
bind_dir etc/systemd/system /etc/systemd/system
bind_dir run/systemd /run/systemd
bind_dir run/lock /host-run-lock
bind_dir etc/udev/rules.d /host-udev-rules.d
bind_dir etc/modules-load.d /host-modules-load.d
bind_dir boot /boot
bind_dir dev /host-dev
bind_dir usr/bin /host-usr/bin
bind_dir usr/sbin /host-usr/sbin
bind_dir usr/local/bin /host-usr-local/bin
bind_dir usr/local/sbin /host-usr-local/sbin
bind_dir bin /host-bin
bind_dir sbin /host-sbin
touch /host-machine-id
mount --bind /root/etc/machine-id /host-machine-id

if [[ -n ${SKYHOOK_DIR:-} && -d "$SKYHOOK_DIR/configmaps" ]]; then
    mkdir -p /custom-configs
    mount --bind "$SKYHOOK_DIR/configmaps" /custom-configs
    if [[ -f "$SKYHOOK_DIR/configmaps/containerd-user-dropin.toml" ]]; then
        export CONTAINERD_USER_DROP_IN_SOURCE_FILE=/custom-configs/containerd-user-dropin.toml
    fi
fi

case "$action" in
    install)
        kata-deploy install-stage-selinux-policy
        kata-deploy install-stage-load-kernel-modules
        kata-deploy install-stage-host-check
        kata-deploy install-stage-artifacts
        kata-deploy install-stage-cri
        ;;
    uninstall)
        kata-deploy install-stage-selinux-policy
        kata-deploy cleanup-stage-revert-cri
        kata-deploy cleanup-stage-remove-artifacts
        ;;
    check-installed)
        test -x /opt/kata/runtime-rs/bin/containerd-shim-kata-v2
        test -s "/opt/kata/share/defaults/kata-containers/runtime-rs/configuration-$runtime.toml"
        ;;
    check-removed)
        test ! -e /opt/kata/runtime-rs/bin/containerd-shim-kata-v2
        ;;
esac
