# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
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

"""Finite full-stack orchestration without GPUs or live API credentials."""

from contextlib import nullcontext
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest
import yaml

PACKAGE = Path(__file__).resolve().parents[3] / 'kata-containers'


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


STACK = load('stack_manager', PACKAGE / 'skyhook_dir/stack-manager.py')
CONFIGURE = load('stack_configure', PACKAGE / 'configure.py')


@pytest.fixture
def cluster():
    env = {'NODE_NAME': 'gpu-node', 'KATA_STACK_NAME': 'kata-gpu-off',
           'KATA_RUNTIME': 'qemu-nvidia-gpu-runtime-rs', 'KATA_STACK_TOKEN': 'not-a-real-token',
           'KATA_STACK_CA': 'test-ca', 'KATA_STACK_API_HOST': '10.96.0.1', 'KATA_STACK_API_PORT': '443'}
    client = STACK.Cluster(env, 'off')
    yield client
    client.close()


@pytest.mark.parametrize('mode,runtime', [
    ('off', 'qemu-nvidia-gpu-runtime-rs'), ('on', 'qemu-nvidia-gpu-snp-runtime-rs'),
    ('on', 'qemu-nvidia-gpu-tdx-runtime-rs'), ('ppcie', 'qemu-nvidia-gpu-tdx-runtime-rs'),
])
def test_supported_mode_runtime_pairs(mode, runtime):
    STACK.validate_profile(mode, runtime)


@pytest.mark.parametrize('mode,runtime', [
    ('ppcie', 'qemu-nvidia-gpu-snp-runtime-rs'), ('on', 'qemu-nvidia-gpu-runtime-rs'),
    ('off', 'qemu-nvidia-gpu-tdx-runtime-rs'), ('unknown', 'qemu-nvidia-gpu-runtime-rs'),
])
def test_unsupported_mode_runtime_pairs(mode, runtime):
    with pytest.raises(ValueError):
        STACK.validate_profile(mode, runtime)


@pytest.mark.parametrize('mode', ['off', 'on', 'ppcie'])
def test_stack_lifecycle_orders_discovery_hardware_runtime_and_plugin(mode):
    config = CONFIGURE.package_config((PACKAGE / 'config.json').read_text(), '4.3.1', stack_mode=mode)
    for stage in ('apply', 'upgrade'):
        assert [s['arguments'] for s in config['modes'][stage]] == [
            ['validate'], ['ensure-nfd'], ['wait-hardware']]
    assert [s['arguments'] for s in config['modes']['config']] == [
        ['clear-ready'], ['install'], ['publish-mode']]
    assert [s['arguments'] for s in config['modes']['post-interrupt']] == [
        ['check-installed'], ['ensure-plugin']]
    assert [s['arguments'] for s in config['modes']['post-interrupt-check']] == [
        ['check-installed'], ['check-plugin']]
    assert [s['arguments'] for s in config['modes']['uninstall']] == [
        ['clear-ready'], ['uninstall'], ['finish-uninstall']]
    assert all(s['on_host'] for steps in config['modes'].values() for s in steps)


def test_credentials_are_private_and_not_in_child_environment(cluster):
    assert 'KATA_STACK_TOKEN' not in cluster.env
    assert 'KATA_STACK_CA' not in cluster.env
    path = Path(cluster.env['KUBECONFIG'])
    assert path.stat().st_mode & 0o777 == 0o600
    config = json.loads(path.read_text())
    assert config['users'][0]['user']['token'] == 'not-a-real-token'
    assert config['clusters'][0]['cluster']['server'] == 'https://10.96.0.1:443'
    cluster.close()
    assert not path.exists()


def test_command_errors_redact_credentials(cluster, monkeypatch):
    monkeypatch.setattr(STACK.subprocess, 'run', lambda *a, **k: SimpleNamespace(
        returncode=1, stderr='rejected not-a-real-token', stdout=''))
    with pytest.raises(RuntimeError) as exc:
        cluster.run('kubectl', 'get', 'nodes')
    assert 'not-a-real-token' not in str(exc.value)
    assert '<redacted>' in str(exc.value)


@pytest.mark.parametrize('existing', [False, True])
def test_nfd_is_ready_before_discovery_rules(cluster, monkeypatch, existing):
    events = []
    ready = {'status': {'desiredNumberScheduled': 1, 'numberReady': 1}}
    monkeypatch.setattr(cluster, 'lock', nullcontext)
    monkeypatch.setattr(cluster, 'get', lambda *a, **k: {'metadata': {'labels': {}}})
    monkeypatch.setattr(cluster, 'labels', lambda values: events.append(('claim', values)))
    calls = iter([[ready] if existing else [], [ready]])
    monkeypatch.setattr(cluster, 'workers', lambda: next(calls))
    monkeypatch.setattr(cluster, 'helm', lambda *args: events.append(('helm', args)))
    monkeypatch.setattr(cluster, 'run', lambda *args, **k: events.append(('run', args)))
    cluster.ensure_nfd()
    commands = [x[1] for x in events if x[0] == 'run']
    assert commands[0][1] == 'wait'
    assert commands[1][1:3] == ('apply', '-f')
    assert any(x[0] == 'helm' for x in events) == (not existing)


def test_existing_stack_owner_is_not_overwritten(cluster, monkeypatch):
    monkeypatch.setattr(cluster, 'lock', nullcontext)
    monkeypatch.setattr(cluster, 'get', lambda *a, **k: {
        'metadata': {'labels': {STACK.OWNER: 'another-stack'}}})
    monkeypatch.setattr(cluster, 'labels', lambda *a: pytest.fail('must not change ownership'))
    with pytest.raises(RuntimeError, match='already owned'):
        cluster.ensure_nfd()


@pytest.mark.parametrize('gpu,tee,expected', [(False, False, False), (True, False, False), (True, True, True)])
def test_cc_waits_for_gpu_and_tee_discovery(cluster, monkeypatch, gpu, tee, expected):
    cluster.mode = 'on'
    cluster.runtime = 'qemu-nvidia-gpu-snp-runtime-rs'
    labels = {'kata.feature.node.kubernetes.io/nvidia-gpu': str(gpu).lower(),
              'amd.feature.node.kubernetes.io/snp': str(tee).lower()}
    monkeypatch.setattr(cluster, 'get', lambda *a, **k: {'metadata': {'labels': labels}})
    assert cluster.hardware_ready() is expected


@pytest.mark.parametrize('ready,count', [(False, '1'), (True, '0'), (True, '1')])
def test_runtime_readiness_is_only_published_after_plugin_and_gpu_capacity(cluster, monkeypatch, ready, count):
    updates = []
    monkeypatch.setattr(cluster, 'plugin_ready', lambda: ready)
    monkeypatch.setattr(cluster, 'get', lambda *a, **k: {'status': {'allocatable': {'nvidia.com/gpu': count}}})
    monkeypatch.setattr(cluster, 'labels', updates.append)
    if ready and count == '1':
        cluster.execute('check-plugin')
        assert updates == [{STACK.RUNTIME_READY: 'kata-qemu-nvidia-gpu-runtime-rs'}]
    else:
        with pytest.raises(RuntimeError):
            cluster.execute('check-plugin')
        assert not updates


def test_ready_labels_are_removed_before_hardware_changes(cluster, monkeypatch):
    updates = []
    monkeypatch.setattr(cluster, 'labels', updates.append)
    cluster.execute('clear-ready')
    assert updates == [{STACK.MODE: None, STACK.READY: None, STACK.RUNTIME_READY: None}]


def test_shared_lock_uses_resource_versions_and_releases_its_own_lease(cluster, monkeypatch):
    state = {'lease': None}
    writes = []
    monkeypatch.setattr(cluster, 'get', lambda *a, **k: deepcopy(state['lease']))
    def run(*args, data=None, **kwargs):
        obj = json.loads(data)
        if state['lease'] is not None:
            assert obj['metadata']['resourceVersion'] == state['lease']['metadata']['resourceVersion']
        obj['metadata']['resourceVersion'] = str(len(writes) + 1)
        state['lease'] = obj
        writes.append(deepcopy(obj))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(cluster, 'run', run)
    with cluster.lock():
        assert state['lease']['spec']['leaseDurationSeconds'] == 60
    assert state['lease']['spec']['renewTime'] == '1970-01-01T00:00:00Z'


def test_shared_lock_does_not_overwrite_an_active_owner(cluster, monkeypatch):
    lease = {'metadata': {'resourceVersion': '1'}, 'spec': {'holderIdentity': 'someone-else',
             'renewTime': STACK.timestamp(), 'leaseDurationSeconds': 60}}
    monkeypatch.setattr(cluster, 'get', lambda *a, **k: lease)
    monkeypatch.setattr(cluster, 'run', lambda *a, **k: pytest.fail('must not overwrite active lease'))
    def wait(predicate, message, **kwargs):
        assert predicate() is False
        raise TimeoutError(message)
    monkeypatch.setattr(cluster, 'wait', wait)
    with pytest.raises(TimeoutError):
        with cluster.lock():
            pytest.fail('lock was not acquired')


def test_plugin_selector_does_not_wait_for_its_own_nodewright_completion(cluster):
    selector = cluster.plugin_values()['nodeSelector']
    assert selector['katacontainers.io/kata-runtime'] is None
    assert selector[STACK.OWNER] == cluster.name
    assert selector[STACK.MODE] == 'off'
    assert not any('status_' in key for key in selector)


def test_stack_manifests_start_before_their_discovery_and_provisioning_outputs():
    for filename in ('cc-off', 'cc-on-snp', 'cc-on-tdx', 'ppcie-tdx'):
        nw, rc = list(yaml.safe_load_all((PACKAGE / 'stack' / f'{filename}.yaml').read_text()))
        selectors = nw['spec']['nodeSelectors']['matchLabels']
        assert not any('feature.node' in key or key in (STACK.MODE, STACK.READY, STACK.OWNER) for key in selectors)
        env = {entry['name']: entry for entry in nw['spec']['packages']['kata-containers']['env']}
        for name in ('KATA_STACK_TOKEN', 'KATA_STACK_CA'):
            assert env[name]['valueFrom']['secretKeyRef']['name'] == 'kata-stack-installer-token'
        assert rc['scheduling']['nodeSelector'][STACK.RUNTIME_READY] == rc['handler']
