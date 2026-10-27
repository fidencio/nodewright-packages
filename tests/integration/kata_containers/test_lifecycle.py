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

"""Lifecycle failure handling inside the repository's Docker test harness."""

from pathlib import Path

import pytest

from tests.helpers.docker_test import DockerTestRunner


STUB = Path(__file__).parent / "fakes/host-command.sh"
EXTRA_FILES = [(STUB, f"test-bin/{name}") for name in ("mount", "mkdir", "touch", "kata-deploy", "uname", "unshare", "chroot", "ctr", "systemctl", "kata-device-provisioner")]
BASE_ENV = {
    "PATH": "/skyhook-package/test-bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    "TRACE": "/tmp/kata-trace", "ENV_TRACE": "/tmp/kata-env", "NODE_NAME": "test-node",
    "TEST_ARCH": "aarch64", "CONTAINER_RUNTIME_VERSION": "containerd://2.4.2",
}


def run_action(base_image, action="install", env=None, script="run-inner.sh", payload=False):
    with DockerTestRunner(package="kata-containers", base_image=base_image) as runner:
        files = EXTRA_FILES + ([(STUB, "skyhook_dir/installer-rootfs/usr/bin/kata-deploy")] if payload else [])
        result = runner.run_script(script=script, script_args=[action],
                                   env_vars={**BASE_ENV, **(env or {})}, extra_files=files)
        trace = runner.container.exec_run(["cat", "/tmp/kata-trace"])
        lines = trace.output.decode().splitlines() if trace.exit_code == 0 else []
        selected = runner.container.exec_run(["cat", "/tmp/kata-env"])
        selection = selected.output.decode().strip() if selected.exit_code == 0 else ""
        return result, lines, selection


@pytest.mark.parametrize("env", [
    {"NODE_NAME": ""}, {"CONTAINER_RUNTIME_VERSION": "REPLACE_ME"},
    {"CONTAINER_RUNTIME_VERSION": "cri-o://1.34.0"}, {"INSTALLATION_PREFIX": "/other"},
    {"MULTI_INSTALL_SUFFIX": "other"}, {"K8S_DISTRIBUTION": "k3s"}, {"TEST_ARCH": "s390x"},
])
def test_invalid_target_fails_before_mounting(base_image, env):
    result, lines, _ = run_action(base_image, env=env)
    assert result.exit_code != 0
    assert lines == []


@pytest.mark.parametrize("action,code", [("validate", 0), ("reset", 2)])
def test_validation_does_not_mount(base_image, action, code):
    result, lines, _ = run_action(base_image, action=action)
    assert result.exit_code == code, result.stdout
    assert lines == []


@pytest.mark.parametrize("architecture", ["x86_64", "aarch64"])
@pytest.mark.parametrize("runtime", ["qemu-nvidia-cpu-runtime-rs", "qemu-nvidia-gpu-runtime-rs"])
def test_installs_selected_runtime_with_private_mounts(base_image, architecture, runtime):
    result, lines, selection = run_action(
        base_image, env={"KATA_RUNTIME": runtime, "TEST_ARCH": architecture,
                         "SHIMS_AARCH64": "qemu-nvidia-gpu-tdx-runtime-rs"},
    )
    assert result.exit_code == 0, result.stdout
    assert selection == f"{runtime}|erofs|{runtime}:erofs|config.toml"
    assert lines[0] == "mount --make-rprivate /"
    for mount in ("mount --bind /root /host", "mount --bind /root/opt/kata /opt/kata",
                  "mount --bind /root/etc/containerd /etc/containerd",
                  "mount --bind /skyhook-package/configmaps /custom-configs"):
        assert lines.index(mount) < lines.index("kata-deploy install-stage-artifacts")


@pytest.mark.parametrize("action,fail,forbidden", [
    ("install", "mount --bind /root /host", "kata-deploy "),
    ("install", "mount --bind /skyhook-package/configmaps /custom-configs", "kata-deploy "),
    ("install", "kata-deploy install-stage-host-check", "kata-deploy install-stage-artifacts"),
    ("uninstall", "kata-deploy cleanup-stage-revert-cri", "kata-deploy cleanup-stage-remove-artifacts"),
])
def test_failure_stops_host_mutation(base_image, action, fail, forbidden):
    result, lines, _ = run_action(base_image, action=action, env={"FAIL_COMMAND": fail})
    assert result.exit_code == 7, result.stdout
    assert not any(line.startswith(forbidden) for line in lines)
    if fail == "kata-deploy install-stage-host-check":
        assert "kata-deploy install-stage-cri" not in lines


def test_uninstall_reverts_cri_before_removing_artifacts(base_image):
    result, lines, _ = run_action(base_image, action="uninstall")
    assert result.exit_code == 0, result.stdout
    assert lines.index("kata-deploy cleanup-stage-revert-cri") < lines.index("kata-deploy cleanup-stage-remove-artifacts")
    assert "kata-deploy cleanup" not in lines


def test_host_wrapper_rejects_missing_installer_before_unshare(base_image):
    result, lines, _ = run_action(base_image, script="run.sh")
    assert result.exit_code != 0
    assert "installer filesystem is missing" in result.stdout
    assert lines == []


def test_host_wrapper_propagates_namespace_failure(base_image):
    namespace = "unshare --mount --propagation private /skyhook-package/skyhook_dir/enter-installer.sh install"
    result, lines, _ = run_action(base_image, script="run.sh", payload=True,
                                 env={"FAIL_COMMAND": namespace})
    assert result.exit_code == 7, result.stdout
    assert lines == [namespace]


def test_installer_mount_failure_prevents_chroot(base_image):
    rootfs = "/skyhook-package/skyhook_dir/installer-rootfs"
    first_mount = f"mount --bind {rootfs} {rootfs}"
    result, lines, _ = run_action(base_image, script="enter-installer.sh",
                                 env={"FAIL_COMMAND": first_mount})
    assert result.exit_code == 7, result.stdout
    assert lines == [first_mount]


def test_installer_mounts_host_before_chroot(base_image):
    result, lines, _ = run_action(base_image, script="enter-installer.sh")
    assert result.exit_code == 0, result.stdout
    rootfs = "/skyhook-package/skyhook_dir/installer-rootfs"
    assert lines == [
        f"mount --bind {rootfs} {rootfs}",
        f"mount --rbind / {rootfs}/root",
        *[f"mount --rbind /{directory} {rootfs}/{directory}" for directory in ("proc", "sys", "dev")],
        f"chroot {rootfs} /bin/bash /usr/local/bin/nodewright-kata-stage install",
    ]


@pytest.mark.parametrize("action", ["install", "uninstall"])
def test_custom_containerd_config_reaches_installer(base_image, action):
    result, _, selection = run_action(base_image, action=action,
                                     env={"CONTAINERD_CONFIG_FILE_NAME": "config-dpu.toml"})
    assert result.exit_code == 0, result.stdout
    assert selection.endswith("|config-dpu.toml")


@pytest.mark.parametrize("name", ["/etc/containerd/dpu.toml", "../dpu.toml", ".", "..", " dpu.toml", "dpu.toml\n"])
def test_invalid_containerd_config_fails_before_mounting(base_image, name):
    result, lines, _ = run_action(base_image, script="run.sh",
                                 env={"CONTAINERD_CONFIG_FILE_NAME": name})
    assert result.exit_code != 0
    assert "simple filename" in result.stdout
    assert lines == []


@pytest.mark.parametrize("version", ["v2.4.2", "v2.2.0", "2.3.1-rc.1"])
def test_host_wrapper_detects_server_version(base_image, version):
    result, lines, selection = run_action(
        base_image, script="run.sh", payload=True,
        env={"CONTAINER_RUNTIME_VERSION": "", "TEST_CONTAINERD_VERSION": version},
    )
    assert result.exit_code == 0, result.stdout
    assert lines[0] == "ctr --address /run/containerd/containerd.sock --timeout 10s version"
    assert selection == f"containerd://{version.removeprefix('v')}"
    assert lines[1].startswith("unshare ")


@pytest.mark.parametrize("env", [
    {"FAIL_COMMAND": "ctr"}, {"TEST_CONTAINERD_VERSION": ""},
    {"TEST_CONTAINERD_VERSION": "unknown"},
])
def test_host_wrapper_rejects_failed_version_detection_before_mounting(base_image, env):
    result, lines, _ = run_action(base_image, script="run.sh", payload=True,
                                env={"CONTAINER_RUNTIME_VERSION": "", **env})
    assert result.exit_code != 0
    assert "containerd server" in result.stdout
    assert len(lines) == 1 and lines[0].startswith("ctr ")


@pytest.mark.parametrize("runtime", ["qemu", "qemu-nvidia-gpu", "qemu-snp-runtime-rs",
                                     "qemu-nvidia-gpu-tdx", "not-a-runtime"])
def test_rejects_unimplemented_profiles_before_mounting(base_image, runtime):
    result, lines, _ = run_action(base_image, script="run.sh", env={"KATA_RUNTIME": runtime})
    assert result.exit_code != 0
    assert "KATA_RUNTIME" in result.stdout
    assert lines == []


def test_gpu_uninstall_selects_the_gpu_profile(base_image):
    result, lines, selection = run_action(base_image, action="uninstall",
                                         env={"KATA_RUNTIME": "qemu-nvidia-gpu-runtime-rs"})
    assert result.exit_code == 0, result.stdout
    assert selection == "qemu-nvidia-gpu-runtime-rs|erofs|qemu-nvidia-gpu-runtime-rs:erofs|config.toml"
    assert lines.index("kata-deploy cleanup-stage-revert-cri") < lines.index("kata-deploy cleanup-stage-remove-artifacts")


@pytest.mark.parametrize("runtime,other", [
    ("qemu-nvidia-cpu-runtime-rs", "qemu-nvidia-gpu-runtime-rs"),
    ("qemu-nvidia-gpu-runtime-rs", "qemu-nvidia-cpu-runtime-rs"),
])
def test_host_check_requires_the_selected_profile_config(base_image, runtime, other):
    with DockerTestRunner(package="kata-containers", base_image=base_image) as runner:
        result = runner.run_script(script="run.sh", script_args=["validate"],
                                   env_vars={**BASE_ENV, "KATA_RUNTIME": runtime}, extra_files=EXTRA_FILES)
        assert result.exit_code == 0, result.stdout
        setup = runner.container.exec_run(["bash", "-ec",
            "/bin/mkdir -p /opt/kata/runtime-rs/bin /opt/kata/share/defaults/kata-containers/runtime-rs; "
            "cp /bin/true /opt/kata/runtime-rs/bin/containerd-shim-kata-v2; "
            f"printf config > /opt/kata/share/defaults/kata-containers/runtime-rs/configuration-{other}.toml"])
        assert setup.exit_code == 0, setup.output
        command = ["bash", "/skyhook-package/skyhook_dir/run.sh", "check-installed"]
        assert runner.container.exec_run(command).exit_code != 0
        installed = runner.container.exec_run(["bash", "-ec",
            f"printf config > /opt/kata/share/defaults/kata-containers/runtime-rs/configuration-{runtime}.toml"])
        assert installed.exit_code == 0, installed.output
        assert runner.container.exec_run(command).exit_code == 0


@pytest.mark.parametrize("runtime", ["qemu-nvidia-gpu-snp-runtime-rs", "qemu-nvidia-gpu-tdx-runtime-rs"])
@pytest.mark.parametrize("action", ["install", "uninstall"])
def test_cc_profiles_select_nydus(base_image, runtime, action):
    result, lines, selection = run_action(base_image, action=action,
                                         env={"KATA_RUNTIME": runtime, "TEST_ARCH": "x86_64"})
    assert result.exit_code == 0, result.stdout
    assert selection == f"{runtime}|nydus|{runtime}:nydus|config.toml"
    if action == "install":
        assert lines.index("kata-deploy install-stage-artifacts") < lines.index("kata-deploy install-stage-cri")
    else:
        assert lines.index("kata-deploy cleanup-stage-revert-cri") < lines.index("kata-deploy cleanup-stage-remove-artifacts")


@pytest.mark.parametrize("runtime", ["qemu-nvidia-gpu-snp-runtime-rs", "qemu-nvidia-gpu-tdx-runtime-rs"])
@pytest.mark.parametrize("architecture", ["aarch64", "arm64"])
def test_cc_rejects_arm_nodes_before_mounting(base_image, runtime, architecture):
    result, lines, _ = run_action(base_image, script="run.sh",
                                 env={"KATA_RUNTIME": runtime, "TEST_ARCH": architecture})
    assert result.exit_code != 0
    assert "require an amd64 node" in result.stdout
    assert lines == []


@pytest.mark.parametrize("runtime", ["qemu-nvidia-gpu-snp-runtime-rs", "qemu-nvidia-gpu-tdx-runtime-rs"])
def test_cc_snapshotter_install_failure_stops_cri_configuration(base_image, runtime):
    result, lines, _ = run_action(base_image, env={
        "KATA_RUNTIME": runtime, "TEST_ARCH": "x86_64",
        "FAIL_COMMAND": "kata-deploy install-stage-artifacts",
    })
    assert result.exit_code == 7
    assert "kata-deploy install-stage-cri" not in lines


@pytest.mark.parametrize("runtime", ["qemu-nvidia-gpu-snp-runtime-rs", "qemu-nvidia-gpu-tdx-runtime-rs"])
def test_cc_host_check_requires_active_nydus(base_image, runtime):
    with DockerTestRunner(package="kata-containers", base_image=base_image) as runner:
        result = runner.run_script(script="run.sh", script_args=["validate"],
                                   env_vars={**BASE_ENV, "KATA_RUNTIME": runtime, "TEST_ARCH": "x86_64"},
                                   extra_files=EXTRA_FILES)
        assert result.exit_code == 0, result.stdout
        setup = runner.container.exec_run(["bash", "-ec",
            "/bin/mkdir -p /opt/kata/runtime-rs/bin /opt/kata/share/defaults/kata-containers/runtime-rs; "
            "cp /bin/true /opt/kata/runtime-rs/bin/containerd-shim-kata-v2; "
            f"printf config > /opt/kata/share/defaults/kata-containers/runtime-rs/configuration-{runtime}.toml"])
        assert setup.exit_code == 0, setup.output
        command = ["bash", "/skyhook-package/skyhook_dir/run.sh", "check-installed"]
        assert runner.container.exec_run(command).exit_code != 0
        setup = runner.container.exec_run(["bash", "-ec",
            "/bin/mkdir -p /opt/kata/nydus-for-kata-tee; "
            "cp /bin/true /opt/kata/nydus-for-kata-tee/containerd-nydus-grpc"])
        assert setup.exit_code == 0, setup.output
        assert runner.container.exec_run(command).exit_code == 0
        result = runner.container.exec_run(command, environment={
            "FAIL_COMMAND": "systemctl is-active --quiet nydus-for-kata-tee.service"})
        assert result.exit_code != 0


@pytest.mark.parametrize("runtime", ["qemu-nvidia-gpu-snp-runtime-rs", "qemu-nvidia-gpu-tdx-runtime-rs"])
@pytest.mark.parametrize("leftover", [
    "/opt/kata/nydus-for-kata-tee/containerd-nydus-grpc",
    "/etc/systemd/system/nydus-for-kata-tee.service",
])
def test_cc_removed_check_rejects_leftover_snapshotter(base_image, runtime, leftover):
    with DockerTestRunner(package="kata-containers", base_image=base_image) as runner:
        result = runner.run_script(script="run.sh", script_args=["validate"],
                                   env_vars={**BASE_ENV, "KATA_RUNTIME": runtime, "TEST_ARCH": "x86_64"},
                                   extra_files=EXTRA_FILES)
        assert result.exit_code == 0, result.stdout
        parent = str(Path(leftover).parent)
        setup = runner.container.exec_run(["bash", "-ec",
            f"/bin/mkdir -p {parent}; printf leftover > {leftover}"])
        assert setup.exit_code == 0, setup.output
        command = ["bash", "/skyhook-package/skyhook_dir/run.sh", "check-removed"]
        assert runner.container.exec_run(command).exit_code != 0
        assert runner.container.exec_run(["/bin/rm", leftover]).exit_code == 0
        assert runner.container.exec_run(command).exit_code == 0


@pytest.mark.parametrize("mode,runtime", [
    ("off", "qemu-nvidia-gpu-runtime-rs"), ("on", "qemu-nvidia-gpu-snp-runtime-rs"),
    ("on", "qemu-nvidia-gpu-tdx-runtime-rs"), ("ppcie", "qemu-nvidia-gpu-tdx-runtime-rs"),
])
def test_stack_provisions_before_runtime_artifacts(base_image, mode, runtime):
    result, lines, _ = run_action(base_image, env={
        "KATA_STACK_MODE": mode, "KATA_RUNTIME": runtime, "TEST_ARCH": "x86_64",
        "KATA_STACK_TOKEN": "must-not-reach-hardware", "KATA_STACK_CA": "test-ca",
    })
    assert result.exit_code == 0, result.stdout
    provision = f"kata-device-provisioner apply --mode {mode} --host-root /root"
    assert lines.index(provision) < lines.index("kata-deploy install-stage-artifacts")


def test_ppcie_snp_is_rejected_before_mounting(base_image):
    result, lines, _ = run_action(base_image, env={
        "KATA_STACK_MODE": "ppcie", "KATA_RUNTIME": "qemu-nvidia-gpu-snp-runtime-rs", "TEST_ARCH": "x86_64"})
    assert result.exit_code != 0
    assert "PPCIE requires TDX" in result.stdout
    assert lines == []


def test_failed_provisioning_stops_runtime_installation(base_image):
    result, lines, _ = run_action(base_image, env={
        "KATA_STACK_MODE": "off", "KATA_RUNTIME": "qemu-nvidia-gpu-runtime-rs",
        "FAIL_COMMAND": "kata-device-provisioner apply --mode off --host-root /root"})
    assert result.exit_code == 7
    assert "kata-deploy install-stage-artifacts" not in lines
