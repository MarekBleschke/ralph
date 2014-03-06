#!/usr/bin/env python
# -*- coding: utf-8 -*-

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

import collections

from django.conf import settings
from django.utils.encoding import force_unicode

from ralph.util import parse
from ralph.util.network import check_tcp_port, connect_ssh
from ralph.discovery.hardware import get_disk_shares
from ralph.discovery.models import DeviceType
from ralph.scan.plugins import get_base_result_template
from ralph.scan.errors import (
    ConnectionError,
    Error,
    NoMatchError,
)


SETTINGS = settings.SCAN_PLUGINS.get(__name__, {})


def _connect_ssh(ip_address, user, password):
    if not check_tcp_port(ip_address, 22):
        raise ConnectionError('Port 22 closed on a XEN server.')
    return connect_ssh(ip_address, user, password)


def _ssh_lines(ssh, command):
    stdin, stdout, stderr = ssh.exec_command(command)
    for line in stdout.readlines():
        yield line


def _sanitize_line(line):
    line = force_unicode(line, errors='ignore').strip()
    line = line.replace(
        '( RO)', '',
    ).replace(
        '( RW)', '',
    ).replace(
        '(MRO)', '',
    )
    return line


def _get_current_host_uuid(ssh, ip_address):
    uuid = ''
    match = False
    for line in _ssh_lines(
        ssh,
        'sudo xe host-list params=address,name-label,uuid',
    ):
        parts = _sanitize_line(line).split(':')
        try:
            param_name, param_value = parts[0].strip(), parts[1].strip()
        except IndexError:
            continue
        if param_name == 'uuid':
            uuid = param_value
            continue
        if param_name == 'address' and param_value == ip_address:
            match = True
            break
    return uuid if match else None


def _get_running_vms(ssh, uuid):
    stdin, stdout, stderr = ssh.exec_command(
        'sudo xe vm-list resident-on={} '
        'params=uuid,name-label,power-state,VCPUs-number,memory-actual'.format(
            uuid,
        )
    )
    data = stdout.read()
    vms = set()
    for vm_data in data.split('\n\n'):
        info = parse.pairs(lines=[
            line.replace('( RO)', '')
                .replace('( RW)', '')
                .replace('(MRO)', '').strip()
            for line in vm_data.splitlines()])
        if not info:
            continue
        label = info['name-label']
        if (
            label.startswith('Transfer VM for') or
            label.startswith('Control domain on host:')
        ):
            # Skip the helper virtual machines
            continue
        power = info['power-state']
        if power not in {'running'}:
            # Only include the running virtual machines
            continue
        cores = int(info['VCPUs-number'])
        memory = int(int(info['memory-actual']) / 1024 / 1024)
        uuid = info['uuid']
        vms.add((label, uuid, cores, memory))
    return vms


def _get_macs(ssh):
    """Get a dict of sets of macs of all the virtual machines."""

    macs = collections.defaultdict(set)
    label = ''
    for line in _ssh_lines(ssh, 'sudo xe vif-list params=vm-name-label,MAC'):
        line = _sanitize_line(line)
        if not line:
            continue
        if line.startswith('vm-name-label'):
            label = line.split(':', 1)[1].strip()
        if label.startswith('Transfer VM for'):
            continue
        if line.startswith('MAC'):
            mac = line.split(':', 1)[1].strip()
            macs[label].add(mac)
    return macs


def _get_disks(ssh):
    """Get a dict of lists of disks of virtual machines."""

    stdin, stdout, stderr = ssh.exec_command(
        'sudo xe vm-disk-list '
        'vdi-params=sr-uuid,uuid,virtual-size '
        'vbd-params=vm-name-label,type,device '
        '--multiple'
    )
    disks = collections.defaultdict(list)
    vm = None
    sr_uuid = None
    device = None
    type_ = None
    device = None
    uuid = None
    for line in stdout:
        if not line.strip():
            continue
        key, value = (x.strip() for x in line.split(':', 1))
        if key.startswith('vm-name-label '):
            vm = value
        elif key.startswith('sr-uuid '):
            sr_uuid = value
        elif key.startswith('type '):
            type_ = value
        elif key.startswith('device '):
            device = value
        elif key.startswith('uuid '):
            uuid = value
        elif key.startswith('virtual-size '):
            if type_ in {'Disk'}:
                disks[vm].append(
                    (uuid, sr_uuid, int(int(value) / 1024 / 1024), device),
                )
    return disks


def _ssh_xen(ip_address, user, password):
    ssh = _connect_ssh(ip_address, user, password)
    try:
        uuid = _get_current_host_uuid(ssh, ip_address)
        if not uuid:
            raise Error('Could not find this host UUID.')
        vms = _get_running_vms(ssh, uuid)
        macs = _get_macs(ssh)
        disks = _get_disks(ssh)
        shares = get_disk_shares(ssh)
    finally:
        ssh.close()
    device_info = {
        'subdevices': [],
        'type': DeviceType.unknown.raw,
        'system_ip_addresses': [ip_address],
    }
    for vm_name, vm_uuid, vm_cores, vm_memory in vms:
        vm_device = {
            'model_name': 'XEN Virtual Server',
        }
        vm_device['mac_addresses'] = [
            mac for i, mac in enumerate(macs.get(vm_name, []))
        ]
        vm_device['serial_number'] = vm_uuid
        vm_device['hostname'] = vm_name
        vm_device['processors'] = [
            {
                'family': 'XEN Virtual',
                'name': 'XEN Virtual CPU',
                'label': 'CPU %d' % i,
                'model_name': 'XEN Virtual',
                'cores': 1,
                'index': i,
            } for i in xrange(vm_cores)
        ]
        vm_device['memory'] = [
            {
                'family': 'Virtual',
                'size': vm_memory,
                'label': 'XEN Virtual',
            },
        ]
        vm_disks = disks.get(vm_name, [])
        for uuid, sr_uuid, size, device in vm_disks:
            wwn, mount_size = shares.get('VHD-%s' % sr_uuid, (None, None))
            if wwn:
                share = {
                    'serial_number': wwn,
                    'is_virtual': True,
                    'size': mount_size,
                    'volume': device,
                }
                if not 'disk_shares' in vm_device:
                    vm_device['disk_shares'] = []
                vm_device['disk_shares'].append(share)
            else:
                storage = {
                    'size': size,
                    'label': device,
                    'family': 'XEN Virtual Disk',
                }
                if not 'disks' in vm_device:
                    vm_device['disks'] = []
                vm_device['disks'].append(storage)
        device_info['subdevices'].append(vm_device)
    return device_info


def scan_address(ip_address, **kwargs):
    snmp_name = kwargs.get('snmp_name', '') or ''
    if 'nx-os' in snmp_name.lower():
        raise NoMatchError("Incompatible Nexus found.")
    if 'xen' not in snmp_name:
        raise NoMatchError("XEN not found.")
    user = SETTINGS.get('xen_user')
    password = SETTINGS.get('xen_password')
    messages = []
    result = get_base_result_template('ssh_xen', messages)
    if not user or not password:
        result['status'] = 'error'
        messages.append(
            'Not configured. Set XEN_USER and XEN_PASSWORD in your '
            'configuration file.',
        )
    else:
        try:
            device_info = _ssh_xen(ip_address, user, password)
        except (Error, ConnectionError) as e:
            result['status'] = 'error'
            messages.append(unicode(e))
        else:
            result.update({
                'status': 'success',
                'device': device_info,
            })
    return result
