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

"""Check package versions and CPU/GPU deployment manifests."""

import json
from pathlib import Path
import subprocess
import sys

import pytest
import yaml


PACKAGE = Path(__file__).resolve().parents[3] / "kata-containers"


def configure(*args):
    return subprocess.run(
        [sys.executable, "-B", str(PACKAGE / "configure.py"), *args],
        capture_output=True, text=True, check=False, timeout=10,
    )


@pytest.mark.parametrize("version", ["4.3.1", "v4.3.1\n", "4.4.0-rc.1"])
def test_package_version_matches_kata_payload(tmp_path, version):
    config = json.loads((PACKAGE / "config.json").read_text())
    config["package_version"] = version.strip().removeprefix("v")
    template, payload, output = (tmp_path / name for name in ("config.json", "VERSION", "output.json"))
    template.write_text(json.dumps(config))
    payload.write_text(version)
    result = configure("--template", str(template), "--version-file", str(payload), "--output", str(output))
    assert result.returncode == 0, result.stderr
    generated = json.loads(output.read_text())
    assert generated["package_name"] == "kata-containers"
    assert generated["package_version"] == config["package_version"]


@pytest.mark.parametrize("version", ["latest", "", "4.3", "04.3.1", "4.3.1-01", "4.3.1+local", "4.3.0"])
def test_rejects_invalid_or_mismatched_payload_version(tmp_path, version):
    payload, output = tmp_path / "VERSION", tmp_path / "output.json"
    payload.write_text(version)
    result = configure("--version-file", str(payload), "--output", str(output))
    assert result.returncode != 0
    assert not output.exists()


def test_rejects_mismatched_expected_version(tmp_path):
    payload, output = tmp_path / "VERSION", tmp_path / "output.json"
    payload.write_text("4.3.1")
    result = configure("--version-file", str(payload), "--expected-version", "4.3.0", "--output", str(output))
    assert result.returncode != 0
    assert "differs" in result.stderr
    assert not output.exists()


def test_configuration_reconciles_at_same_version():
    config = json.loads((PACKAGE / "config.json").read_text())
    assert all(step["on_host"] for steps in config["modes"].values() for step in steps)
    assert config["expected_config_files"] == []
    assert config["modes"]["apply"][0]["arguments"] == ["validate"]
    assert config["modes"]["config"][0]["arguments"] == ["install"]
    assert config["modes"]["config"][0]["idempotence"] is True


def test_shared_cpu_manifests():
    resource = yaml.safe_load((PACKAGE / "nodewright.yaml").read_text())
    runtime = yaml.safe_load((PACKAGE / "nvidia-cpu-runtimeclass.yaml").read_text())
    assert resource["kind"] == "NodeWright"
    assert resource["metadata"]["name"] == "kata-containers"
    assert runtime["kind"] == "RuntimeClass"
    assert runtime["metadata"]["name"] == "kata-containers-qemu-nvidia-cpu-runtime-rs"
    assert set(resource["spec"]["packages"]) == {"kata-containers"}
    package = resource["spec"]["packages"]["kata-containers"]
    assert "dependsOn" not in package
    assert "agentImageOverride" not in package
    assert package["version"] == json.loads((PACKAGE / "config.json").read_text())["package_version"]
    assert package["image"] == "ghcr.io/nvidia/nodewright-packages/kata-containers"
    env = {entry["name"]: entry.get("value") for entry in package["env"]}
    assert env["CONTAINERD_CONFIG_FILE_NAME"] == "config.toml"
    assert "CONTAINER_RUNTIME_VERSION" not in env
    assert env["DEBUG"] == "false"
    assert package["configInterrupts"] == {"*": {"type": "noop"}}
    assert runtime["handler"] == "kata-qemu-nvidia-cpu-runtime-rs"


@pytest.mark.parametrize("architecture", ["amd64", "arm64", "s390x", "ppc64le"])
@pytest.mark.parametrize("complete", [False, True])
def test_runtimeclass_requires_installation_on_supported_nodes(architecture, complete):
    resource = yaml.safe_load((PACKAGE / "nodewright.yaml").read_text())
    runtime = yaml.safe_load((PACKAGE / "nvidia-cpu-runtimeclass.yaml").read_text())
    selector = resource["spec"]["nodeSelectors"]
    labels = {"nodewright.nvidia.com/kata-profile": "kata-containers", "kubernetes.io/os": "linux"}
    assert selector["matchLabels"] == labels
    assert selector["matchExpressions"] == [
        {"key": "kubernetes.io/arch", "operator": "In", "values": ["amd64", "arm64"]}
    ]
    status_key = f"nodewright.nvidia.com/status_{resource['metadata']['name']}"
    assert runtime["scheduling"]["nodeSelector"] == {"kubernetes.io/os": "linux", status_key: "complete"}
    node = {**labels, "kubernetes.io/arch": architecture}
    selected = architecture in selector["matchExpressions"][0]["values"]
    # Only selected nodes can receive the operator's completion label.
    if complete and selected:
        node[status_key] = "complete"
    eligible = all(node.get(key) == value for key, value in runtime["scheduling"]["nodeSelector"].items())
    assert eligible == (complete and architecture in ("amd64", "arm64"))


def test_gpu_manifests_keep_cpu_and_gpu_nodes_disjoint():
    cpu = yaml.safe_load((PACKAGE / "nodewright.yaml").read_text())
    gpu = yaml.safe_load((PACKAGE / "nodewright-gpu.yaml").read_text())
    runtime = yaml.safe_load((PACKAGE / "nvidia-gpu-runtimeclass.yaml").read_text())
    plugin = yaml.safe_load((PACKAGE / "kata-device-plugin.values.yaml").read_text())
    pod = yaml.safe_load((PACKAGE / "gpu-pod.yaml").read_text())
    assert gpu["metadata"]["name"] == "kata-containers-gpu"
    assert cpu["spec"]["nodeSelectors"]["matchLabels"]["nodewright.nvidia.com/kata-profile"] != (
        gpu["spec"]["nodeSelectors"]["matchLabels"]["nodewright.nvidia.com/kata-profile"])
    assert gpu["spec"]["nodeSelectors"]["matchExpressions"] == cpu["spec"]["nodeSelectors"]["matchExpressions"]
    package = gpu["spec"]["packages"]["kata-containers"]
    assert package["image"] == cpu["spec"]["packages"]["kata-containers"]["image"]
    assert package["version"] == cpu["spec"]["packages"]["kata-containers"]["version"]
    assert "dependsOn" not in package
    assert "agentImageOverride" not in package
    env = {entry["name"]: entry.get("value") for entry in package["env"]}
    assert env["KATA_RUNTIME"] == "qemu-nvidia-gpu-runtime-rs"
    assert "CONTAINER_RUNTIME_VERSION" not in env
    assert runtime["handler"] == "kata-qemu-nvidia-gpu-runtime-rs"
    assert pod["spec"]["runtimeClassName"] == runtime["metadata"]["name"]
    assert pod["spec"]["containers"][0]["resources"]["limits"] == {"nvidia.com/gpu": 1}
    completion = f"nodewright.nvidia.com/status_{gpu['metadata']['name']}"
    assert runtime["scheduling"]["nodeSelector"] == {"kubernetes.io/os": "linux", completion: "complete",
        "nodewright.nvidia.com/kata-gpu-mode": "off", "nvidia.com/cc.ready.state": "false"}
    assert plugin["nodeSelector"] == {
        "katacontainers.io/kata-runtime": None, "kubernetes.io/os": "linux", completion: "complete",
        "nodewright.nvidia.com/kata-gpu-mode": "off", "nvidia.com/cc.ready.state": "false"}
    assert plugin["resourceNaming"] == "alias"


def test_basic_gpu_requires_nfd_and_verified_cc_off():
    provisioner = yaml.safe_load((PACKAGE / "kata-device-provisioner-gpu.values.yaml").read_text())
    gpu = yaml.safe_load((PACKAGE / "nodewright-gpu.yaml").read_text())
    assert provisioner["node-feature-discovery"]["enabled"] is False
    profile = provisioner["profiles"]["gpu"]
    assert profile["enabled"] is True
    assert profile["ccMode"] == "off"
    assert profile["nodeFeatureRule"]["enabled"] is True
    labels = gpu["spec"]["nodeSelectors"]["matchLabels"]
    assert labels[profile["ccModeStateLabel"]] == "off"
    assert labels["nvidia.com/cc.ready.state"] == "false"
    assert labels["kata.feature.node.kubernetes.io/nvidia-gpu"] == "true"
    assert profile["nodeSelector"]["nodewright.nvidia.com/kata-profile"] == labels["nodewright.nvidia.com/kata-profile"]
    assert profile["nodeSelector"]["kata.feature.node.kubernetes.io/nvidia-gpu"] == "true"


@pytest.mark.parametrize("tee,feature,key", [
    ("snp", "amd.feature.node.kubernetes.io/snp", "sev-snp.amd.com/esids"),
    ("tdx", "intel.feature.node.kubernetes.io/tdx", "tdx.intel.com/keys"),
])
def test_cc_manifests_require_discovery_provisioning_and_runtime_completion(tee, feature, key):
    resource = yaml.safe_load((PACKAGE / f"nodewright-gpu-{tee}.yaml").read_text())
    runtime = yaml.safe_load((PACKAGE / f"nvidia-gpu-{tee}-runtimeclass.yaml").read_text())
    provisioner = yaml.safe_load((PACKAGE / f"kata-device-provisioner-{tee}.values.yaml").read_text())
    plugin = yaml.safe_load((PACKAGE / f"kata-device-plugin-{tee}.values.yaml").read_text())
    pod = yaml.safe_load((PACKAGE / f"gpu-{tee}-pod.yaml").read_text())
    name = f"kata-containers-gpu-{tee}"
    handler = f"qemu-nvidia-gpu-{tee}-runtime-rs"
    mode_key = f"nodewright.nvidia.com/kata-gpu-{tee}-mode"
    assert resource["metadata"]["name"] == name
    assert provisioner["node-feature-discovery"]["enabled"] is False
    profile = provisioner["profiles"][tee]
    assert profile["enabled"] is True
    assert "ccMode" not in profile  # The caller must choose a board-appropriate mode.
    assert profile["ccModeStateLabel"] == mode_key
    assert profile["nodeFeatureRule"]["enabled"] is True
    labels = resource["spec"]["nodeSelectors"]["matchLabels"]
    assert labels == {**profile["nodeSelector"], "nvidia.com/cc.ready.state": "true"}
    assert labels[feature] == "true"
    assert labels["kubernetes.io/arch"] == "amd64"
    assert labels["nodewright.nvidia.com/kata-profile"] == name
    assert resource["spec"]["nodeSelectors"]["matchExpressions"] == [
        {"key": mode_key, "operator": "In", "values": ["on", "ppcie"]}]
    package = resource["spec"]["packages"]["kata-containers"]
    assert package["version"] == json.loads((PACKAGE / "config.json").read_text())["package_version"]
    env = {entry["name"]: entry.get("value") for entry in package["env"]}
    assert env["KATA_RUNTIME"] == handler
    assert "CONTAINER_RUNTIME_VERSION" not in env
    assert "configMap" not in package  # No EROFS host drop-in on a Nydus profile.
    assert "configInterrupts" not in package  # A glob without ConfigMap keys fails admission.
    assert "dependsOn" not in package
    assert runtime["handler"] == f"kata-{handler}"
    assert runtime["overhead"]["podFixed"] == {key: "1"}
    ready = runtime["scheduling"]["nodeSelector"]
    assert ready == {"kubernetes.io/os": "linux", "kubernetes.io/arch": "amd64",
                     feature: "true", f"nodewright.nvidia.com/status_{name}": "complete",
                     "nvidia.com/cc.ready.state": "true"}
    assert plugin["nodeSelector"] == {"katacontainers.io/kata-runtime": None, **ready}
    assert plugin["resourceNaming"] == "alias"
    assert pod["spec"]["runtimeClassName"] == runtime["metadata"]["name"]
    assert pod["spec"]["containers"][0]["resources"]["limits"] == {"nvidia.com/gpu": 1}


def test_nfd_tee_resource_rules_match_runtimeclass_accounting():
    discovery = yaml.safe_load((PACKAGE / "tee-node-feature-rule.yaml").read_text())
    assert discovery["apiVersion"] == "nfd.k8s-sigs.io/v1alpha1"
    assert discovery["kind"] == "NodeFeatureRule"
    expected = {
        "amd.sev-snp": ("snp", "amd.feature.node.kubernetes.io/snp", "sev-snp.amd.com/esids", "sev.snp.enabled", "sev.encrypted_state_ids"),
        "intel.tdx": ("tdx", "intel.feature.node.kubernetes.io/tdx", "tdx.intel.com/keys", "tdx.enabled", "tdx.total_keys"),
    }
    for rule in discovery["spec"]["rules"]:
        tee, label, key, enabled, count = expected.pop(rule["name"])
        assert rule["labels"] == {label: "true"}
        assert rule["matchFeatures"] == [{"feature": "cpu.security", "matchExpressions": {enabled: {"op": "Exists"}}}]
        assert rule["extendedResources"] == {key: f"@cpu.security.{count}"}
        runtime = yaml.safe_load((PACKAGE / f"nvidia-gpu-{tee}-runtimeclass.yaml").read_text())
        assert runtime["overhead"]["podFixed"] == {key: "1"}
    assert not expected
