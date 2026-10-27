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


# Pin the multi-platform index, not an individual architecture manifest.
# Keep the Kata payload independent of the operator-managed agent.
ARG KATA_DEPLOY_IMAGE=ghcr.io/kata-containers/kata-deploy:4.3.1@sha256:9f6bbd0972e6fa8c99f5a86b24503032cb87be602d22e830d1c917ba31b4db9f
ARG KATA_PROVISIONER_IMAGE=ghcr.io/kata-containers/kata-device-provisioner:0.1.0-alpha.1@sha256:a3d9206ad05ed667eb8cd4fc0bb066d2a402ced0ffb3226fc8b18dfb49f422ff
FROM ${KATA_DEPLOY_IMAGE} AS kata
FROM ${KATA_PROVISIONER_IMAGE} AS provisioner

FROM alpine:3.22@sha256:2e0b81d60597f449baa56dc25d355367869d2dca4cd57e866f1e2b52af3eb55f AS package-config
RUN apk add --no-cache python3
COPY --from=kata /opt/kata-artifacts/VERSION /tmp/KATA_VERSION
COPY configure.py config.json /package/
ARG KATA_VERSION
ARG GPU_STACK_MODE
RUN set -- --version-file /tmp/KATA_VERSION --output /package/config.json; \
    if [ -n "$KATA_VERSION" ]; then set -- "$@" --expected-version "$KATA_VERSION"; fi; \
    if [ -n "$GPU_STACK_MODE" ]; then set -- "$@" --stack-mode "$GPU_STACK_MODE"; fi; \
    python3 /package/configure.py "$@"

FROM alpine:3.22@sha256:2e0b81d60597f449baa56dc25d355367869d2dca4cd57e866f1e2b52af3eb55f AS stack-tools
ARG TARGETARCH
RUN apk add --no-cache curl tar
RUN mkdir -p /out/charts && \
    curl -fsSL "https://get.helm.sh/helm-v4.3.1-linux-${TARGETARCH}.tar.gz" -o /tmp/helm.tgz && \
    case "$TARGETARCH" in \
        amd64) checksum=0403a88ae20913d11b10a2467b50087721fea7581736da6b9d3992f5357fe54b ;; \
        arm64) checksum=8cf8f66ecbc252ac8f7f06cca68aba88508ddfde3a6a34feb1678ad1694b4f88 ;; \
        *) exit 1 ;; \
    esac && \
    echo "$checksum  /tmp/helm.tgz" | sha256sum -c - && \
    tar -xzf /tmp/helm.tgz -C /tmp && \
    cp "/tmp/linux-${TARGETARCH}/helm" /out/helm
RUN curl -fsSL "https://dl.k8s.io/release/v1.37.1/bin/linux/${TARGETARCH}/kubectl" -o /out/kubectl && \
    case "$TARGETARCH" in \
        amd64) checksum=65691ff77eb6fa44c908b77a1082c9f092c3b9733b5cefabec0d1104890e21a8 ;; \
        arm64) checksum=ff749f4b78d9c4f1ec87307df9b50119ed819e2094aa9810cb9acffc3286c8c7 ;; \
        *) exit 1 ;; \
    esac && \
    echo "$checksum  /out/kubectl" | sha256sum -c - && chmod +x /out/kubectl
RUN /out/helm pull oci://registry.k8s.io/nfd/charts/node-feature-discovery \
        --version 0.20.1 --destination /out/charts && \
    /out/helm pull oci://ghcr.io/kata-containers/kata-device-plugin-charts/kata-device-plugin \
        --version 0.2.0-rc.2 --destination /out/charts && \
    echo 'fb6e0bcc17eacc0dc7be0422204a6d50d19a06a3a06b40c61c856ff34e88d510  /out/charts/node-feature-discovery-0.20.1.tgz' | sha256sum -c - && \
    echo '7e8f6cbe792700b475c642d8d28ba4bc07ba3f5bc46429d878cb4dff17ce763c  /out/charts/kata-device-plugin-0.2.0-rc.2.tgz' | sha256sum -c -

FROM alpine:3.22@sha256:2e0b81d60597f449baa56dc25d355367869d2dca4cd57e866f1e2b52af3eb55f AS installer
ARG TARGETARCH
RUN case "${TARGETARCH}" in amd64|arm64) ;; *) exit 1 ;; esac
RUN apk add --no-cache bash util-linux python3
COPY --from=provisioner /kata-device-provisioner /usr/local/bin/kata-device-provisioner
COPY --from=stack-tools /out/helm /out/kubectl /usr/local/bin/
COPY --from=stack-tools /out/charts /opt/kata-stack/charts
COPY skyhook_dir/stack-manager.py /usr/local/libexec/kata-stack.py
COPY stack-gpu-rule.yaml /opt/kata-stack/gpu-rule.yaml
COPY tee-node-feature-rule.yaml /opt/kata-stack/tee-rule.yaml
COPY --from=kata /usr/bin/kata-deploy /usr/bin/kata-deploy
COPY check-architecture.py /tmp/check-architecture.py
# Execution under binfmt emulation can hide a foreign-architecture payload.
RUN python3 /tmp/check-architecture.py "${TARGETARCH}" \
        /usr/bin/kata-deploy /usr/local/bin/kata-device-provisioner \
        /usr/local/bin/helm /usr/local/bin/kubectl && rm /tmp/check-architecture.py
COPY --from=kata /opt/kata-artifacts /opt/kata-artifacts
COPY skyhook_dir/run-inner.sh /usr/local/bin/nodewright-kata-stage
RUN mkdir -p /root /host /proc /sys /dev /custom-configs

# Fail the build for older kata-deploy images that lack the finite stage API.
RUN help="$(/usr/bin/kata-deploy --help)" && \
    for action in install-stage-selinux-policy install-stage-load-kernel-modules \
        install-stage-host-check install-stage-artifacts install-stage-cri \
        cleanup-stage-revert-cri cleanup-stage-remove-artifacts; do \
        echo "$help" | grep -q "$action" || exit 1; \
    done

FROM alpine:3.22@sha256:2e0b81d60597f449baa56dc25d355367869d2dca4cd57e866f1e2b52af3eb55f
COPY --from=installer / /skyhook-package/skyhook_dir/installer-rootfs/
COPY --from=package-config /package/config.json /skyhook-package/config.json
COPY skyhook_dir /skyhook-package/skyhook_dir

ARG GPU_STACK_MODE
ARG SOURCE_REVISION=unknown
LABEL org.opencontainers.image.source="https://github.com/fidencio/nodewright-packages" \
      org.opencontainers.image.revision="${SOURCE_REVISION}"
RUN if [ -n "$GPU_STACK_MODE" ]; then \
        printf '%s\n' "$GPU_STACK_MODE" > /skyhook-package/skyhook_dir/stack-mode; \
        printf '%s\n' "$GPU_STACK_MODE" > /skyhook-package/skyhook_dir/installer-rootfs/opt/kata-stack/mode; \
    fi
