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
    validate|install|uninstall|check-installed|check-removed) action="$1" ;;
    *) echo "usage: $0 {validate|install|uninstall|check-installed|check-removed}" >&2; exit 2 ;;
esac

# Resolve the running server on the host, before entering the bundled rootfs.
# Keep the native override available for deployments that supply it themselves.
if [[ -z ${CONTAINER_RUNTIME_VERSION:-} ]]; then
    if ! containerd_version="$(ctr --address /run/containerd/containerd.sock --timeout 10s version |
        awk '/^Server:/ { server = 1; next }
             server && $1 == "Version:" { version = $2 }
             END { sub(/^v/, "", version); if (version != "") print version }')"; then
        echo 'could not query the running containerd server' >&2
        exit 1
    fi
    if [[ ! ${containerd_version} =~ ^[0-9]+\.[0-9]+\.[0-9]+([-+][0-9A-Za-z.-]+)?$ ]]; then
        echo 'could not determine the running containerd server version' >&2
        exit 1
    fi
    export CONTAINER_RUNTIME_VERSION="containerd://${containerd_version}"
fi

if [[ -f "${STEP_ROOT:?}/stack-mode" ]]; then
    KATA_STACK_MODE="$(cat "${STEP_ROOT}/stack-mode")"
    export KATA_STACK_MODE
    case "${KATA_STACK_MODE}" in
        off) export KATA_RUNTIME="${KATA_RUNTIME:-qemu-nvidia-gpu-runtime-rs}" ;;
        on) : "${KATA_RUNTIME:?select the SNP or TDX GPU Rust runtime}" ;;
        ppcie) export KATA_RUNTIME="${KATA_RUNTIME:-qemu-nvidia-gpu-tdx-runtime-rs}" ;;
        *) echo 'unsupported stack mode' >&2; exit 1 ;;
    esac
fi

bash "${STEP_ROOT:?}/run-inner.sh" validate
runtime="${KATA_RUNTIME:-qemu-nvidia-cpu-runtime-rs}"
case "${action}" in
    validate) exit 0 ;;
    check-installed)
        test -x /opt/kata/runtime-rs/bin/containerd-shim-kata-v2
        test -s "/opt/kata/share/defaults/kata-containers/runtime-rs/configuration-${runtime}.toml"
        case "${runtime}" in
            qemu-nvidia-gpu-snp-runtime-rs|qemu-nvidia-gpu-tdx-runtime-rs)
                test -x /opt/kata/nydus-for-kata-tee/containerd-nydus-grpc
                systemctl is-active --quiet nydus-for-kata-tee.service
                ;;
        esac
        exit 0
        ;;
    check-removed)
        test ! -e /opt/kata/runtime-rs/bin/containerd-shim-kata-v2
        case "${runtime}" in
            qemu-nvidia-gpu-snp-runtime-rs|qemu-nvidia-gpu-tdx-runtime-rs)
                test ! -e /opt/kata/nydus-for-kata-tee/containerd-nydus-grpc
                test ! -e /etc/systemd/system/nydus-for-kata-tee.service
                ;;
        esac
        exit 0
        ;;
esac

if [[ ! -x "${STEP_ROOT}/installer-rootfs/usr/bin/kata-deploy" ]]; then
    echo 'bundled Kata installer filesystem is missing or incomplete' >&2
    exit 1
fi
exec unshare --mount --propagation private "${STEP_ROOT}/enter-installer.sh" "${action}"
