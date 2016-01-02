#!/usr/bin/env python
# -*- coding: utf-8 -*-

from __future__ import print_function, unicode_literals
import os
from tempfile import mkdtemp
from shutil import rmtree
from json import load, dump
from .process import run_command, ProcessException
import re
from string import Template
import json
import logging


PRESEED_FILE_NAME = 'preseed.cfg'
PACKER_CONFIG_FILE_NAME = 'packer.json'
EXTRACTED_OVF_FILE_NAME = 'box.ovf'
REPACKAGED_VAGRANT_BOX_FILE_NAME = 'package.box'


logger = logging.getLogger('wamopacker.command')


__all__ = ['Builder', 'BuilderException']


class TempDir(object):

    def __init__(self, root_dir = None):
        self.path = None
        self._root_dir = root_dir

    def __enter__(self):
        if self.path:
            raise IOError('temp dir exists')

        self.path = mkdtemp(dir = self._root_dir)

        return self

    def __exit__(self, type, value, traceback):
        if self.path and os.path.isdir(self.path):
            rmtree(self.path)
            self.path = None


class BuilderException(Exception):
    pass


class Builder(object):

    def __init__(self, config, target_list, dry_run = False, dump_packer = False):
        self._config = config
        self._target_list = target_list
        self._dry_run = dry_run
        self._dump_packer = dump_packer

        self._data_path = self._get_data_path()

    @staticmethod
    def _get_data_path():
        return os.path.join(os.path.dirname(__file__), 'data')

    def _get_template(self, file_name):
        template_path = os.path.join(self._data_path, 'templates')
        template_file_name = os.path.join(template_path, '{}.template'.format(file_name))
        with open(template_file_name, 'rb') as file_object:
            return Template(file_object.read())

    def build(self):
        packer_config = {
            "builders": [],
            "provisioners": [],
            "post-processors": []
        }

        temp_dir_root = self._config.temp_dir
        with TempDir(temp_dir_root) as temp_dir:
            if 'virtualbox' in self._target_list:
                self._build_virtualbox(packer_config, temp_dir)

            if 'aws' in self._target_list:
                self._build_aws(packer_config, temp_dir)

            self._add_provisioners(packer_config)

            self._add_vagrant_export(packer_config)

            self._run_packer(packer_config, temp_dir)

    def _load_json(self, name):
        file_name = os.path.join(self._data_path, '{}.json'.format(name))
        with open(file_name, 'r') as file_object:
            return load(file_object)

    def _parse_parameters(self, config_key_list, output_lookup):
        for config_item in config_key_list:
            config_key, output_key, output_type = map(
                    lambda default, val: val if val is not None else default,
                    (None, None, basestring),
                    config_item
            )
            if config_key in self._config:
                val = getattr(self._config, config_key)

                if not isinstance(val, output_type):
                    raise BuilderException('Parameter type mismatch: name={} expected={} received={}'.format(config_key, output_type, type(val)))

                output_lookup[output_key] = val

    def _build_virtualbox(self, packer_config, temp_dir):
        if self._config.virtualbox_iso_url and self._config.virtualbox_iso_checksum:
            logger.info('Bulding VirtualBox ISO configuration')

            self._build_virtualbox_iso(packer_config, temp_dir)

        else:
            logger.info('Bulding VirtualBox OVF configuration')

            if self._config.virtualbox_vagrant_box_name and self._config.virtualbox_vagrant_box_version:
                self._build_virtualbox_vagrant_box(temp_dir)

            if self._config.virtualbox_vagrant_box_file:
                self._build_virtualbox_vagrant_box_file(temp_dir)

            if self._config.virtualbox_ovf_input_file:
                self._build_virtualbox_ovf_file(packer_config, temp_dir)

    def _build_virtualbox_iso(self, packer_config, temp_dir):
        packer_virtualbox_iso = self._load_json('packer_virtualbox_iso')

        config_key_list = (
            ('virtualbox_ovf_output', 'vm_name'),
            ('virtualbox_iso_url', 'iso_url'),
            ('virtualbox_iso_checksum', 'iso_checksum'),
            ('virtualbox_iso_checksum_type', 'iso_checksum_type'),
            ('virtualbox_guest_os_type', 'guest_os_type'),
            ('virtualbox_disk_mb', 'disk_size'),
            ('virtualbox_user', 'ssh_username'),
            ('virtualbox_password', 'ssh_password'),
            ('virtualbox_shutdown_command', 'shutdown_command'),
            ('virtualbox_output_directory', 'output_directory'),
        )
        self._parse_parameters(config_key_list, packer_virtualbox_iso)

        vboxmanage_list = packer_virtualbox_iso.setdefault('vboxmanage', [])
        for vboxmanage_attr, vboxmanage_cmd in (
                ('virtualbox_memory_mb', '--memory'),
                ('virtualbox_cpus', '--cpus'),
        ):
            if vboxmanage_attr in self._config:
                vboxmanage_list.append(['modifyvm', '{{ .Name }}', vboxmanage_cmd, getattr(self._config, vboxmanage_attr)])

        self._write_virtualbox_iso_preseed(packer_virtualbox_iso, temp_dir)

        # add to the builder list
        packer_config['builders'].append(packer_virtualbox_iso)

    def _write_virtualbox_iso_preseed(self, virtualbox_config, temp_dir):
        # create the packer_http directory
        packer_http_dir = self._config.virtualbox_packer_http_dir
        packer_http_path = os.path.join(temp_dir.path, packer_http_dir)
        virtualbox_config['http_directory'] = packer_http_path
        os.mkdir(packer_http_path)

        # generate the preseed text
        preseed_template = self._get_template(PRESEED_FILE_NAME)
        preseed_text = preseed_template.substitute(
            user_account = virtualbox_config['ssh_username'],
            user_password = virtualbox_config['ssh_password']
        )

        # write the preseed
        preseed_file_name = os.path.join(packer_http_path, PRESEED_FILE_NAME)
        with open(preseed_file_name, 'w') as file_object:
            file_object.write(preseed_text)

    def _build_virtualbox_ovf_file(self, packer_config, temp_dir):
        packer_virtualbox_ovf = self._load_json('packer_virtualbox_ovf')

        config_key_list = (
            ('virtualbox_ovf_output', 'vm_name'),
            ('virtualbox_user', 'ssh_username'),
            ('virtualbox_password', 'ssh_password'),
            ('virtualbox_private_key_file', 'ssh_key_path'),  # https://github.com/mitchellh/packer/issues/2428
            ('virtualbox_ovf_input_file', 'source_path'),
            ('virtualbox_output_directory', 'output_directory'),
        )
        self._parse_parameters(config_key_list, packer_virtualbox_ovf)

        packer_config['builders'].append(packer_virtualbox_ovf)

    def _build_virtualbox_vagrant_box_file(self, temp_dir):
        logger.info('Extracting VirtualBox OVF file from Vagrant box')

        extract_command = "tar -xzvf '{}' -C '{}'".format(self._config.virtualbox_vagrant_box_file, temp_dir.path)
        run_command(extract_command)

        self._config.virtualbox_ovf_input_file = os.path.join(temp_dir.path, EXTRACTED_OVF_FILE_NAME)

    def _build_virtualbox_vagrant_box(self, temp_dir):
        logger.info('Extracting installed VirtualBox Vagrant box')

        extract_command = "vagrant box repackage '{}' virtualbox '{}'".format(
            self._config.virtualbox_vagrant_box_name,
            self._config.virtualbox_vagrant_box_version
        )
        run_command(extract_command, working_dir = temp_dir.path)

        self._config.virtualbox_vagrant_box_file = os.path.join(temp_dir.path, REPACKAGED_VAGRANT_BOX_FILE_NAME)

    def _build_aws(self, packer_config, temp_dir):
        logger.info('Building AWS configuration')

        if self._config.aws_vagrant_box_name and self._config.aws_vagrant_box_version:
            self._build_aws_vagrant_box(temp_dir)

        if self._config.aws_vagrant_box_file:
            self._build_aws_vagrant_box_file(packer_config, temp_dir)

        if self._config.aws_ami_id:
            self._build_aws_ami_id(packer_config)

    def _build_aws_ami_id(self, packer_config):
        packer_amazon_ebs = {
            'type': 'amazon-ebs'
        }

        config_key_list = (
            ('aws_access_key', 'access_key'),
            ('aws_secret_key', 'secret_key'),
            ('aws_ami_id', 'source_ami'),
            ('aws_region', 'region'),
            ('aws_ami_name', 'ami_name'),
            ('aws_ami_force_deregister', 'force_deregister', bool),
            ('aws_instance_type', 'instance_type'),
            ('aws_user', 'ssh_username'),
            ('aws_keypair_name', 'ssh_keypair_name'),
            ('aws_private_key_file', 'ssh_private_key_file'),
            ('aws_disk_gb', 'volume_size'),
            ('aws_disk_type', 'volume_type'),
            ('aws_ami_tags', 'tags', dict),
            ('aws_ami_builder_tags', 'run_tags', dict),
            ('aws_iam_instance_profile', 'iam_instance_profile'),
        )
        self._parse_parameters(config_key_list, packer_amazon_ebs)

        packer_config['builders'].append(packer_amazon_ebs)

    def _build_aws_vagrant_box_file(self, packer_config, temp_dir):
        logger.info('Extracting AWS AMI id from Vagrant box')

        extract_command = 'tar -xzvf {} -C {} Vagrantfile'.format(self._config.aws_vagrant_box_file, temp_dir.path)
        run_command(extract_command)

        vagrant_file_name = os.path.join(temp_dir.path, 'Vagrantfile')
        found_ami_id = None
        with open(vagrant_file_name, 'r') as file_object:
            for line in file_object:
                match = re.search('ami:\s*\"([^\"]+)\"', line)
                if match:
                    found_ami_id = match.group(1)

                    break

        if not found_ami_id:
            raise BuilderException('Unable to extract AWS AMI id from Vagrant box file')

        self._config.aws_ami_id = found_ami_id

    def _build_aws_vagrant_box(self, temp_dir):
        logger.info('Extracting installed AWS Vagrant box')

        extract_command = "vagrant box repackage '{}' aws '{}'".format(
            self._config.aws_vagrant_box_name,
            self._config.aws_vagrant_box_version
        )
        run_command(extract_command, working_dir = temp_dir.path)

        self._config.aws_vagrant_box_file = os.path.join(temp_dir.path, REPACKAGED_VAGRANT_BOX_FILE_NAME)

    def _add_provisioners(self, packer_config):
        if self._config.provisioners:
            if not isinstance(self._config.provisioners, list):
                raise BuilderException('Provisioners must be a list')

            value_definition_lookup = {
                'file': (
                    {'name': 'source' },
                    {'name': 'destination' },
                    {'name': 'direction', 'required': False},
                ),
                'shell': (
                    {'name': 'inline', 'type': list, 'required': False},
                    {'name': 'script', 'required': False},
                    {'name': 'scripts', 'type': list, 'required': False},
                    {'name': 'execute_command', 'required': False, 'default': '(( shell_command ))'},
                    {'name': 'environment_vars', 'type': list, 'required': False},
                ),
                'ansible-local': (
                    {'name': 'playbook_file'},
                    {'name': 'playbook_dir', 'required': False},
                    {'name': 'command', 'required': False},
                    {'name': 'extra_arguments', 'type': list, 'required': False},
                    {'name': 'extra_vars', 'type': dict, 'required': False, 'func': self._to_expanded_json},
                ),
            }
            value_parse_lookup = {
                'ansible-local': self._parse_provisioner_ansible_local,
            }

            provisioner_list = self._config.provisioners
            for provisioner_lookup in provisioner_list:
                provisioner_type = provisioner_lookup.get('type')
                if provisioner_type in value_definition_lookup:
                    provisioner_values = self._parse_provisioner(
                        provisioner_type,
                        provisioner_lookup,
                        value_definition_lookup[provisioner_type]
                    )

                    value_parse_func = value_parse_lookup.get(provisioner_type)
                    if callable(value_parse_func):
                        value_parse_func(provisioner_values)

                    packer_config['provisioners'].append(provisioner_values)

                else:
                    raise BuilderException("Unknown provision type: type='{}'".format(provisioner_type))

    def _parse_provisioner(self, provisioner_type, provisioner_lookup, value_definition):
        provisioner_values = {
            'type': provisioner_type
        }

        provisioner_val_name_set = set(provisioner_lookup.keys())
        provisioner_val_name_set.remove('type')
        for val_items in value_definition:
            val_name = val_items['name']
            val_type = val_items.get('type', basestring)
            val_required = val_items.get('required', True)
            val_default = self._config.expand_parameters(val_items.get('default'))
            val_func = val_items.get('func')

            val = provisioner_lookup.get(val_name, val_default)
            if not isinstance(val, val_type):
                if val or val_required:
                    raise BuilderException("Invalid provision value: name='{}' type='{}' type_expected='{}'".format(
                        val_name,
                        '' if val is None else val.__class__.__name__,
                        val_type.__name__
                    ))

            if callable(val_func):
                val = val_func(val)

            if val:
                provisioner_values[val_name] = val
                provisioner_val_name_set.discard(val_name)

        if provisioner_val_name_set:
            raise BuilderException("Invalid provision value: name='{}'".format(','.join(provisioner_val_name_set)))

        return provisioner_values

    def _to_expanded_json(self, val):
        val_expanded = self._config.expand_parameters(val)
        return json.dumps(val_expanded, indent = None)

    def _parse_provisioner_ansible_local(self, provisioner_values):
        extra_vars = provisioner_values.get('extra_vars')
        if extra_vars:
            extra_arguments_list = provisioner_values.setdefault('extra_arguments', [])
            extra_arguments_list.append("-e '{}'".format(extra_vars))

            del(provisioner_values['extra_vars'])

    def _add_vagrant_export(self, packer_config):
        if self._config.vagrant:
            vagrant_config = {
                'type': 'vagrant'
            }

            if self._config.vagrant_output:
                vagrant_config['output'] = self._config.vagrant_output

            if self._config.vagrant_keep_inputs:
                vagrant_config['keep_input_artifact'] = True

            packer_config['post-processors'].append(vagrant_config)

    def _run_packer(self, packer_config, temp_dir):
        if self._dump_packer:
            logger.info("Dumping packer config to '{}'".format(PACKER_CONFIG_FILE_NAME))
            self._write_packer_config(packer_config, PACKER_CONFIG_FILE_NAME)

        packer_config_file_name = os.path.join(temp_dir.path, PACKER_CONFIG_FILE_NAME)
        self._write_packer_config(packer_config, packer_config_file_name)

        try:
            logger.info('Validating packer config')
            run_command('{} validate {}'.format(self._config.packer_command, packer_config_file_name))

        except (ProcessException, OSError) as e:
            raise BuilderException('Failed to validate packer config: {}'.format(e))

        if not self._dry_run:
            try:
                logger.info('Building packer configuration')
                run_command('{} build {}'.format(self._config.packer_command, packer_config_file_name))

            except (ProcessException, OSError) as e:
                raise BuilderException('Failed to build packer config: {}'.format(e))

    @staticmethod
    def _write_packer_config(packer_config, file_name):
        with open(file_name, 'w') as file_object:
            dump(packer_config, file_object, indent = 4)
