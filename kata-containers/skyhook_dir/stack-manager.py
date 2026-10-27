#!/usr/bin/env python3
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

"""Finite cluster operations invoked by the NodeWright package lifecycle."""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
import json
import os
import re
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import uuid

ROOT = Path('/opt/kata-stack')
OWNER = 'nodewright.nvidia.com/kata-stack'
MODE = 'nodewright.nvidia.com/kata-gpu-mode'
READY = 'nvidia.com/cc.ready.state'
RUNTIME_READY = 'nodewright.nvidia.com/kata-runtime-ready'
NFD_VERSION = '0.20.1'
PLUGIN_VERSION = '0.2.0-rc.2'
FEATURES = {
    'qemu-nvidia-gpu-snp-runtime-rs': 'amd.feature.node.kubernetes.io/snp',
    'qemu-nvidia-gpu-tdx-runtime-rs': 'intel.feature.node.kubernetes.io/tdx',
}


def validate_profile(mode, runtime):
    allowed = {
        'off': {'qemu-nvidia-gpu-runtime-rs'},
        'on': set(FEATURES),
        'ppcie': {'qemu-nvidia-gpu-tdx-runtime-rs'},
    }
    if runtime not in allowed.get(mode, set()):
        raise ValueError(f'incompatible GPU mode/runtime: {mode}/{runtime}')


def now():
    return datetime.now(timezone.utc)


def timestamp():
    return now().isoformat(timespec='microseconds').replace('+00:00', 'Z')


class Cluster:
    def __init__(self, env, mode):
        self.node = env['NODE_NAME']
        self.name = env['KATA_STACK_NAME']
        self.runtime = env['KATA_RUNTIME']
        self.mode = mode
        validate_profile(mode, self.runtime)
        if not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,33}[a-z0-9])?', self.name):
            raise ValueError('KATA_STACK_NAME must be a DNS label of at most 35 characters')
        self.namespace = env.get('KATA_STACK_NAMESPACE', 'nodewright')
        self.release = f'kata-plugin-{self.name}'
        self.env = dict(env)
        self.directory = tempfile.TemporaryDirectory(prefix='kata-stack-')
        path = Path(self.directory.name)
        token = self.env.pop('KATA_STACK_TOKEN')
        ca = self.env.pop('KATA_STACK_CA')
        if not token or not ca:
            raise ValueError('bootstrap service-account token and CA must be populated')
        (path / 'ca.crt').write_text(ca)
        server = env.get('KATA_STACK_API_SERVER')
        if not server:
            host = env.get('KATA_STACK_API_HOST') or env['KUBERNETES_SERVICE_HOST']
            if ':' in host:
                host = f'[{host}]'
            port = env.get('KATA_STACK_API_PORT', env.get('KUBERNETES_SERVICE_PORT', '443'))
            server = f'https://{host}:{port}'
        config = {
            'apiVersion': 'v1', 'kind': 'Config',
            'clusters': [{'name': 'cluster', 'cluster': {'server': server, 'certificate-authority': str(path / 'ca.crt')}}],
            'users': [{'name': 'installer', 'user': {'token': token}}],
            'contexts': [{'name': 'cluster', 'context': {'cluster': 'cluster', 'user': 'installer'}}],
            'current-context': 'cluster',
        }
        config_path = path / 'config'
        config_path.write_text(json.dumps(config))
        config_path.chmod(0o600)
        self.env['KUBECONFIG'] = str(config_path)
        self.token = token

    def close(self):
        self.directory.cleanup()

    def run(self, *args, data=None, required=True, timeout=360):
        result = subprocess.run(args, input=data, text=True, capture_output=True,
                                env=self.env, timeout=timeout)
        if required and result.returncode:
            message = result.stderr.replace(self.token, '<redacted>')
            raise RuntimeError(f'{args[0]} {args[1]} failed: {message[-3000:]}')
        return result

    def get(self, kind, name=None, namespace=None, selector=None, optional=False):
        args = ['kubectl', 'get', kind]
        if name:
            args.append(name)
        if namespace:
            args.extend(['-n', namespace])
        if selector:
            args.extend(['-l', selector])
        if optional:
            args.append('--ignore-not-found')
        result = self.run(*args, '-o', 'json')
        return json.loads(result.stdout) if result.stdout.strip() else None

    def apply(self, obj):
        self.run('kubectl', 'apply', '-f', '-', data=json.dumps(obj))

    def labels(self, values):
        patch = {'metadata': {'labels': values}}
        self.run('kubectl', 'patch', 'node', self.node, '--type=merge', '-p', json.dumps(patch))

    def wait(self, predicate, message, seconds=300):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(2)
        raise TimeoutError(message)

    @contextmanager
    def lock(self):
        holder = str(uuid.uuid4())
        lease_name = 'kata-stack-installer'
        stop = threading.Event()
        lost = threading.Event()

        def lease(previous=None):
            metadata = {'name': lease_name, 'namespace': self.namespace}
            if previous:
                metadata['resourceVersion'] = previous['metadata']['resourceVersion']
            return {'apiVersion': 'coordination.k8s.io/v1', 'kind': 'Lease', 'metadata': metadata,
                    'spec': {'holderIdentity': holder, 'leaseDurationSeconds': 60, 'renewTime': timestamp()}}

        def acquire():
            current = self.get('lease', lease_name, self.namespace, optional=True)
            if current:
                spec = current['spec']
                renewed = datetime.fromisoformat(spec['renewTime'].replace('Z', '+00:00'))
                if renewed + timedelta(seconds=spec.get('leaseDurationSeconds', 60)) > now():
                    return False
            result = self.run('kubectl', 'replace' if current else 'create', '-f', '-',
                              data=json.dumps(lease(current)), required=False)
            return result.returncode == 0

        self.wait(acquire, 'timed out waiting for the shared stack installation lease', seconds=600)

        def renew():
            while not stop.wait(10):
                try:
                    current = self.get('lease', lease_name, self.namespace)
                    if current['spec']['holderIdentity'] != holder:
                        lost.set()
                        return
                    self.run('kubectl', 'replace', '-f', '-', data=json.dumps(lease(current)))
                except Exception:
                    lost.set()
                    return

        thread = threading.Thread(target=renew, daemon=True)
        thread.start()
        try:
            yield
            if lost.is_set():
                raise RuntimeError('lost the shared installation lease')
        finally:
            stop.set()
            thread.join(timeout=30)
            current = self.get('lease', lease_name, self.namespace, optional=True)
            if current and current['spec']['holderIdentity'] == holder:
                # Keep the resource version check so a stale owner cannot delete a successor's lease.
                current['spec']['leaseDurationSeconds'] = 1
                current['spec']['renewTime'] = '1970-01-01T00:00:00Z'
                self.run('kubectl', 'replace', '-f', '-', data=json.dumps(current), required=False)

    def chart(self, name, version):
        return str(ROOT / 'charts' / f'{name}-{version}.tgz')

    def helm(self, release, chart, namespace, values):
        path = Path(self.directory.name) / 'values.json'
        path.write_text(json.dumps(values))
        self.run('helm', 'upgrade', '--install', release, chart, '-n', namespace,
                 '--values', str(path), '--wait', '--timeout', '5m', '--history-max', '2')

    def workers(self):
        return json.loads(self.run('kubectl', 'get', 'daemonsets', '--all-namespaces', '-l',
                                   'app.kubernetes.io/name=node-feature-discovery', '-o', 'json').stdout)['items']

    def ensure_nfd(self):
        with self.lock():
            node = self.get('node', self.node)
            owner = node['metadata'].get('labels', {}).get(OWNER)
            if owner and owner != self.name:
                raise RuntimeError(f'node is already owned by stack {owner}; uninstall it first')
            self.labels({OWNER: self.name})
            workers = self.workers()
            if not workers:
                values = {'fullnameOverride': 'nodewright-nfd',
                          'master': {'extraLabelNs': ['kata.feature.node.kubernetes.io',
                                                      'amd.feature.node.kubernetes.io',
                                                      'intel.feature.node.kubernetes.io'],
                                     'tolerations': [{'operator': 'Exists', 'effect': 'NoSchedule'}]},
                          'gc': {'tolerations': [{'operator': 'Exists', 'effect': 'NoSchedule'}]},
                          'worker': {'tolerations': [{'operator': 'Exists', 'effect': 'NoSchedule'}]}}
                self.helm('nodewright-nfd', self.chart('node-feature-discovery', NFD_VERSION),
                          'node-feature-discovery', values)
            def ready():
                workers = self.workers()
                return bool(workers) and all(x.get('status', {}).get('desiredNumberScheduled', 0) > 0
                    and x['status'].get('numberReady', 0) == x['status']['desiredNumberScheduled'] for x in workers)
            self.wait(ready, 'NFD workers are not ready')
            self.run('kubectl', 'wait', '--for=condition=Established',
                     'crd/nodefeaturerules.nfd.k8s-sigs.io', '--timeout=60s')
            self.run('kubectl', 'apply', '-f', str(ROOT / 'gpu-rule.yaml'))
            if self.mode != 'off':
                self.run('kubectl', 'apply', '-f', str(ROOT / 'tee-rule.yaml'))
        print('NFD is ready; discovery rules installed', flush=True)

    def hardware_ready(self):
        labels = self.get('node', self.node)['metadata'].get('labels', {})
        return (labels.get('kata.feature.node.kubernetes.io/nvidia-gpu') == 'true'
                and (self.runtime not in FEATURES or labels.get(FEATURES[self.runtime]) == 'true'))

    def plugin_values(self):
        return {'nodeSelector': {'katacontainers.io/kata-runtime': None,
                                OWNER: self.name, MODE: self.mode, 'kubernetes.io/os': 'linux'},
                'resourceNaming': 'alias'}

    def plugin_ready(self):
        pods = self.get('pods', namespace='kata-device-plugin',
                        selector=f'app.kubernetes.io/instance={self.release}')['items']
        return any(p['spec'].get('nodeName') == self.node and not p['metadata'].get('deletionTimestamp')
                   and any(c['type'] == 'Ready' and c['status'] == 'True'
                           for c in p.get('status', {}).get('conditions', [])) for p in pods)

    def ensure_plugin(self):
        with self.lock():
            self.helm(self.release, self.chart('kata-device-plugin', PLUGIN_VERSION),
                      'kata-device-plugin', self.plugin_values())
        self.wait(self.plugin_ready, 'device plugin is not ready on this node')
        self.wait(lambda: int(self.get('node', self.node).get('status', {}).get('allocatable', {})
                              .get('nvidia.com/gpu', '0')) > 0, 'device plugin advertised no GPUs')
        print('Device plugin ready and GPU capacity advertised', flush=True)

    def execute(self, action):
        if action == 'ensure-nfd':
            self.ensure_nfd()
        elif action == 'wait-hardware':
            self.wait(self.hardware_ready, 'NFD did not discover the required GPU/CPU TEE')
        elif action == 'check-nfd':
            if not self.hardware_ready():
                raise RuntimeError('required NFD hardware discovery is missing')
        elif action == 'clear-ready':
            self.labels({MODE: None, READY: None, RUNTIME_READY: None})
        elif action == 'publish-mode':
            self.labels({MODE: self.mode, READY: 'false' if self.mode == 'off' else 'true'})
        elif action == 'check-mode':
            labels = self.get('node', self.node)['metadata'].get('labels', {})
            if labels.get(MODE) != self.mode or labels.get(READY) != ('false' if self.mode == 'off' else 'true'):
                raise RuntimeError('provisioning state was not published')
        elif action == 'ensure-plugin':
            self.ensure_plugin()
        elif action == 'check-plugin':
            if not self.plugin_ready():
                raise RuntimeError('device plugin is not ready')
            if int(self.get('node', self.node).get('status', {}).get('allocatable', {}).get('nvidia.com/gpu', '0')) <= 0:
                raise RuntimeError('device plugin advertised no GPUs')
            self.labels({RUNTIME_READY: f'kata-{self.runtime}'})
        elif action == 'finish-uninstall':
            with self.lock():
                self.labels({OWNER: None, MODE: None, READY: None, RUNTIME_READY: None})
                remaining = self.get('nodes', selector=f'{OWNER}={self.name}')['items']
                if not remaining:
                    self.run('helm', 'uninstall', self.release, '-n', 'kata-device-plugin', '--ignore-not-found')
                # NFD may be shared with other consumers; retain it on package removal.
        elif action == 'check-uninstalled':
            labels = self.get('node', self.node)['metadata'].get('labels', {})
            if labels.get(OWNER) == self.name or MODE in labels or READY in labels or RUNTIME_READY in labels:
                raise RuntimeError('stack readiness labels remain after uninstall')
        else:
            raise ValueError(f'unknown stack action: {action}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action')
    args = parser.parse_args()
    mode = (ROOT / 'mode').read_text().strip()
    cluster = Cluster(os.environ, mode)
    try:
        cluster.execute(args.action)
    finally:
        cluster.close()


if __name__ == '__main__':
    main()
