import platform
import sys
import os
from pathlib import Path
import shutil
from urllib import request as Request
import subprocess
import json
import textwrap
from typing import Union
import venv

from edbterraform import __dot_project__
from edbterraform.utils.logs import logger
from edbterraform.utils.files import checksum_verify
from edbterraform.utils.script import execute_shell, binary_path, Version

class TerraformCLI:
    binary_name = 'terraform'
    min_version = Version("1.3.6")
    # Version temporarily locked to 1.5.5
    # Ref PR: https://github.com/EnterpriseDB/edb-terraform/pull/88
    max_version = Version("1.5.5")
    arch_alias = {
        'x86_64': 'amd64',
    }
    DOT_PATH = __dot_project__
    plan_file = 'terraform.plan'

    def __init__(self, binary_dir=None, version=None):
        self.bin_dir = binary_dir if binary_dir else self.DOT_PATH
        self.version = self.max_version if not version else Version(version)
        self.skip_install = self.version == Version("0")
        self.default_path = Path(self.bin_dir) / self.binary_name / self.version.to_string() / 'bin'
        self.bin_path =  self.default_path
        self.binary_full_path = Path(self.bin_path) / self.binary_name
        self.architecture = self.arch_alias.get(platform.machine().lower(),platform.machine().lower())
        self.operating_system = platform.system().lower()

    def get_binary(self):
        return binary_path(self.binary_name, self.bin_path, self.default_path)

    def get_compatible_terraform(self):
        version = self.check_version()
        binary = self.get_binary()

        if self.min_version > version \
            or version > self.max_version:
            raise subprocess.CalledProcessError(
                cmd=binary,
                output=textwrap.dedent('''
                No compatible version found.
                Min: {min}
                Max: {max}
                Version: {current}
                Binary: {binary}
                ''').format(
                    min = self.min_version.to_string(),
                    max = self.max_version.to_string(),
                    current = version.to_string(),
                    binary = binary
                ),
                returncode=1
            )
        return binary

    def check_version(self):
        try:
            version_keyname = 'terraform_version'
            terraform_path = self.get_binary()
            command = [terraform_path, '--version', '-json']
            output = execute_shell(
                args=command,
                environment=os.environ.copy(),
            )
            result = json.loads(output.decode("utf-8"))

            version = Version(result[version_keyname])
            return version
        except KeyError as e:
            raise e(f'version keyname was not found')
    
    def init_command(self, cwd):
        try:
            terraform_path = self.get_compatible_terraform()
            command = [
                terraform_path,
                'init',
            ]
            output = execute_shell(
                args=command,
                environment=os.environ.copy(),
                cwd=cwd,
            )
        except subprocess.CalledProcessError as e:
            logger.error(f'Error: ({e.output})')
            raise e

    def plan_command(self, cwd):
        try:
            terraform_path = self.get_compatible_terraform()
            command = [
                terraform_path,
                'plan',
                '-input=false',
                f'-out={self.plan_file}'
            ]
            output = execute_shell(
                    args=command,
                    environment=os.environ.copy(),
                    cwd=cwd,
            )
        except subprocess.CalledProcessError as e:
            logger.error(f'Error: ({e.output})')
            raise e

    def apply_command(self, cwd, validate_only=False):
        try:
            terraform_path = self.get_compatible_terraform()
            command = [terraform_path, 'apply', '-input=false', '-auto-approve',]
            if validate_only:
                command.append('-target=null_resource.validation')
            command.append(self.plan_file)
            output = execute_shell(
                    args=command,
                    environment=os.environ.copy(),
                    cwd=cwd,
            )
        except subprocess.CalledProcessError as e:
            logger.error(f'Error: ({e.output})')
            raise e

    def destroy_command(self, cwd):
        '''
        Attempt to destroy resources.
        If previously destroyed, a second attempt will fail with our custom modules,
          and instead requires checking of the state to confirm destruction.
        Some destructions will require manual intervention if state is left incomplete.
        In most cases, we are able to comment out the codesection 
          or use the Terraform cli to attempt to remove the problem resource from the state.
        If a user deletes their statefile, they will need to visit the providers GUI and manually destroy any remaining resources.
        '''
        try:
            cwd = Path(cwd)
            if not cwd.exists():
                logger.info('path does not exist yet, no destruction needed')
                return False

            if not (cwd / 'terraform.tfstate').exists():
                raise IOError('terraform.tfstate not found.')

            terraform_path = self.get_compatible_terraform()
            command = [terraform_path, 'state', 'list',]
            fmt_args = ' '.join([str(x) for x in command])
            logger.info("Executing command: %s", fmt_args)
            process = subprocess.run(
                fmt_args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=True,
                cwd=cwd,
                env=os.environ.copy(),
            )

            if "No state file was found!" in process.stderr.decode("utf-8") \
                    or "Backend initialization required" in process.stderr.decode("utf-8") \
                and (cwd / 'terraform.tfstate').stat().st_size == 0:
                logger.info('Backend not initialized, no resource destruction needed')
                return True

            if len(process.stdout.decode("utf-8").split('\n'))-1 == 0 and process.returncode == 0:
                logger.info('state list return 0 results, no destruction needed')
                return True

            command = [terraform_path, 'destroy', '-input=false', '-auto-approve',]
            output = execute_shell(
                    args=command,
                    environment=os.environ.copy(),
                    cwd=cwd,
            )
        except subprocess.CalledProcessError as e:
            logger.error(f'Error: ({e.output})')
            raise e

        return True

    def install(self):
        if self.skip_install:
            logger.info('Terraform 0 version used, skipping installation')
            return

        terraform_bin = self.get_binary()
        # Skip installation if version is already installed
        if terraform_bin == self.binary_full_path \
            and self.check_version().to_tuple() == self.version.to_tuple():
            logger.info(f'Terraform {self.version} is already installed')
            return

        try:
            logger.info(f'Installing Terraform {self.version} in {self.binary_full_path}')
            full_path = Path(self.binary_full_path)
            full_path.unlink(missing_ok=True)
            bin_path = Path(self.bin_path)
            bin_path.mkdir(parents=True, exist_ok=True)

            base = f'https://releases.hashicorp.com/terraform/{self.version.to_string()}/terraform_{self.version.to_string()}'
            source_url = base + f'_{self.operating_system}_{self.architecture}.zip'
            zip_file = Path(Request.urlretrieve(source_url)[0])

            # Get sha256 checksum file
            checksum_url = base + f'_SHA256SUMS'
            checksum_file = Path(Request.urlretrieve(checksum_url)[0])

            # Verify checksum
            if not checksum_verify(zip_file, checksum_file, 'sha256'):
                checksum_file.unlink(missing_ok=True)
                zip_file.unlink(missing_ok=True)
                raise Exception(f'Failed to verify Terraform {self.version} checksum')
            logger.info(f'Verified Terraform {self.version} checksum')
            checksum_file.unlink(missing_ok=True)

            shutil.unpack_archive(zip_file, bin_path, 'zip')
            zip_file.unlink(missing_ok=True)
            full_path.chmod(0o770)
        except Exception as e:
            raise Exception(f'Failed to install Terraform {self.version} - ({e})') from e

class JqCLI:
    binary_name = 'jq'
    min_version = Version("1.6.0")
    max_version = Version("1.7.1")
    arch_alias = {
        'x86_64': 'amd64',
    }
    DOT_PATH = __dot_project__

    def __init__(self, binary_dir=None, version=None):
        self.bin_dir = binary_dir if binary_dir else self.DOT_PATH
        self.version = self.max_version if not version else Version(version)
        self.skip_install = self.version == Version("0")
        # JQ drops the patch version when it is 0 and may include a tainted patch
        self.format_version = self.version.to_string(tainted=True, include_zero_patch=False)
        self.default_path = Path(self.bin_dir) / self.binary_name / self.format_version / 'bin'
        self.bin_path =  self.default_path
        self.binary_full_path = Path(self.bin_path) / self.binary_name
        self.architecture = self.arch_alias.get(platform.machine().lower(),platform.machine().lower())
        self.operating_system = platform.system().lower()

    def check_version(self):
        try:
            jq_path = self.get_binary()
            jq_prefix = "jq-" # jq --version returns a single string as jq-<version> and newline
            command = [jq_path, '--version']
            output = execute_shell(
                args=command,
                environment=os.environ.copy(),
            )
            result = output.decode("utf-8")
            return Version(result.lstrip(jq_prefix).rstrip("\n\s"))
        except KeyError as e:
            raise e(f'version keyname was not found')

    def get_binary(self):
        return binary_path(self.binary_name, self.bin_path, self.default_path)

    def install(self):

        if self.skip_install:
            logger.info('JQ 0 version used, skipping installation')
            return

        jq_bin = self.get_binary()
        # Skip installation if latest already installed
        if jq_bin == self.binary_full_path \
            and self.check_version().to_tuple() == self.version.to_tuple():
            logger.info(f'JQ {self.version} is already installed')
            return

        try:
            logger.info(f'Installing JQ {self.version} in {self.binary_full_path}')
            full_path = Path(self.binary_full_path)
            full_path.unlink(missing_ok=True)
            bin_path = Path(self.bin_path)
            bin_path.mkdir(parents=True, exist_ok=True)

            # Starting with version jq 1.7, the artifact release names have changed:
            # - jq-linux64 -> jq-linux-amd64
            # - jq-osx-amd64 -> jq-macos-amd64
            # - arm/darwin does not exist -> jq-macos-arm64
            base = f"https://github.com/jqlang/jq/releases/download/jq-{self.format_version}/"
            source_url = base + f'jq-{self.operating_system}-{self.architecture}'
            # Macos release constains macos in the name
            if self.operating_system == "darwin":
                source_url = base + f'jq-macos-{self.architecture}'
            # Handle version 1.6 releases
            if self.version.to_string().startswith('1.6'):
                if self.architecture == "arm64":
                    raise Exception("JQ 1.6 does not support arm64 architecture")

                if self.operating_system == "linux":
                    source_url = base + 'jq-linux64'

                if self.operating_system == "darwin":
                    source_url = base + 'jq-osx-amd64'

            binary_file = Path(Request.urlretrieve(source_url)[0])

            # Get sha256 checksum file
            checksum_url = base + f'sha256sum.txt'
            checksum_file = ""
            # unzip file and get signature froms from sig/v1.6/sha256sum.txt
            if self.version == Version("1.6"):
                checksum_url = base + f'jq-1.6.zip'
                checksum_zip = Path(Request.urlretrieve(checksum_url, '/tmp/jq.zip')[0])
                checksum_file = Path('/tmp/jq-checksums')
                temp_dir = Path('/tmp/jq')
                shutil.unpack_archive(checksum_zip, temp_dir, 'zip')
                shutil.move(temp_dir / 'jq-1.6/sig/v1.6/sha256sum.txt', checksum_file)
                shutil.rmtree(temp_dir)
            else:
                checksum_file = Path(Request.urlretrieve(checksum_url)[0])

            # Verify checksum
            if not checksum_verify(binary_file, checksum_file, 'sha256'):
                binary_file.unlink(missing_ok=True)
                checksum_file.unlink(missing_ok=True)
                raise Exception(f'Failed to verify JQ {self.version} checksum')
            logger.info(f'Verified JQ {self.version} checksum')
            checksum_file.unlink(missing_ok=True)

            binary_file.rename(full_path)
            binary_file.unlink(missing_ok=True)
            full_path.chmod(0o770)
        except Exception as e:
            raise Exception(f'Failed to install JQ {self.version} - ({e})') from e

class AwsCLI:
    binary_name = 'aws'
    min_version = Version("2.11.18")
    max_version = Version("2.15.18")

    DOT_PATH = __dot_project__

    def __init__(self, binary_dir=None, version=None):
        self.bin_dir = binary_dir if binary_dir else self.DOT_PATH
        self.version = self.max_version if not version else Version(version)
        self.skip_install = self.version == Version("0")
        self.default_path = Path(self.bin_dir) / self.binary_name / self.version.to_string() / 'bin'
        self.default_venv = self.default_path.parents[0] / 'venv'
        self.bin_path =  self.default_path
        self.binary_full_path = Path(self.bin_path) / self.binary_name
        self.operating_system = platform.system().lower()

    def check_version(self):
        try:
            path = self.get_binary()
            command = [path, '--version']
            output = execute_shell(
                args=command,
                environment=os.environ.copy(),
            )
            result = output.decode("utf-8")
            return Version(result.lstrip('aws-cli/').rstrip("\n\s"))
        except KeyError as e:
            raise e(f'version keyname was not found')

    def get_binary(self):
        return binary_path(self.binary_name, self.bin_path, self.default_path)

    def install(self):

        if self.skip_install:
            logger.info('AwsCLIv2 0 version used, skipping installation')
            return

        # Skip installation if latest already installed
        if self.get_binary() == self.binary_full_path \
            and self.check_version().to_tuple() == self.version.to_tuple():
            logger.info(f'AwsCLIv2 {self.version} is already installed')
            return

        try:
            logger.info(f'Installing AwsCLIv2 {self.version} in {self.binary_full_path}')
            full_path = Path(self.binary_full_path)
            full_path.unlink(missing_ok=True)
            bin_path = Path(self.bin_path)
            bin_path.mkdir(parents=True, exist_ok=True)

            base = f"https://github.com/aws/aws-cli/archive/refs/tags/{self.version.to_string()}.zip"
            source_url = base

            logger.info(f'AwsCLIv2 {self.version} checksum not available')

            # Create a clean virtual environment with pip available
            builder = venv.EnvBuilder(with_pip=True, system_site_packages=False)
            builder.create(self.default_venv)
            pip_cmd = str(self.default_venv / 'bin' / 'pip')
            aws_cmd = str(self.default_venv / 'bin' / 'aws')
            command = [pip_cmd, 'install', source_url]
            output = execute_shell(
                args=command,
                environment=os.environ.copy(),
            )

            full_path.symlink_to(aws_cmd)
            full_path.chmod(0o770)
        except Exception as e:
            raise Exception(f'Failed to install AwsCLIv2 {self.version} - ({e})') from e

class AzureCLI:
    binary_name = 'az'
    min_version = Version("2.1.0")
    max_version = Version("2.57.0")

    DOT_PATH = __dot_project__

    def __init__(self, binary_dir=None, version=None):
        self.bin_dir = binary_dir if binary_dir else self.DOT_PATH
        self.version = self.max_version if not version else Version(version)
        self.skip_install = self.version == Version("0")
        self.default_path = Path(self.bin_dir) / self.binary_name / self.version.to_string() / 'bin'
        self.default_venv = self.default_path.parents[0] / 'venv'
        self.bin_path =  self.default_path
        self.binary_full_path = Path(self.bin_path) / self.binary_name
        self.operating_system = platform.system().lower()

    def check_version(self):
        try:
            version_keyname='azure-cli'
            path = self.get_binary()
            command = [path, 'version']
            output = execute_shell(
                args=command,
                environment=os.environ.copy(),
            )
            result = json.loads(output.decode("utf-8"))
            return Version(result[version_keyname])
        except KeyError as e:
            raise e(f'version keyname was not found')

    def get_binary(self):
        return binary_path(self.binary_name, self.bin_path, self.default_path)

    def install(self):

        if self.skip_install:
            logger.info('AzCLI 0 version used, skipping installation')
            return

        # Skip installation if latest already installed
        if self.get_binary() == self.binary_full_path \
            and self.check_version().to_tuple() == self.version.to_tuple():
            logger.info(f'AzCLI {self.version} is already installed')
            return

        try:
            logger.info(f'Installing AzCLI {self.version} in {self.binary_full_path}')
            full_path = Path(self.binary_full_path)
            full_path.unlink(missing_ok=True)
            bin_path = Path(self.bin_path)
            bin_path.mkdir(parents=True, exist_ok=True)

            logger.info(f'AzCLI {self.version} checksum not available, pip installation')

            builder = venv.EnvBuilder(with_pip=True, system_site_packages=False)
            builder.create(self.default_venv)
            pip_cmd = str(self.default_venv / 'bin' / 'pip')
            az_cmd = str(self.default_venv / 'bin' / 'az')
            command = [pip_cmd, 'install', f'azure-cli=={self.version.to_string()}']
            output = execute_shell(
                args=command,
                environment=os.environ.copy(),
            )
            full_path.symlink_to(az_cmd)
            full_path.chmod(0o770)
        except Exception as e:
            raise Exception(f'Failed to install AzCLIv2 {self.version} - ({e})') from e

class GoogleCLI:
    binary_name = 'gcloud'
    min_version = Version("442.0.0")
    max_version = Version("463.0.0")
    arch_alias = {
        'amd64': 'x86_64',
    }
    DOT_PATH = __dot_project__

    def __init__(self, binary_dir=None, version=None):
        self.bin_dir = binary_dir if binary_dir else self.DOT_PATH
        self.version = self.max_version if not version else Version(version)
        self.skip_install = self.version == Version("0")
        self.default_path = Path(self.bin_dir) / self.binary_name / self.version.to_string() / 'bin'
        self.default_venv = self.default_path.parents[0]
        self.bin_path =  self.default_path
        self.binary_full_path = Path(self.bin_path) / self.binary_name
        self.architecture = self.arch_alias.get(platform.machine().lower(),platform.machine().lower())
        self.operating_system = platform.system().lower()

    def check_version(self):
        try:
            path = self.get_binary()
            command = [path, '--version']
            output = execute_shell(
                args=command,
                environment=os.environ.copy(),
            )
            result = output.decode("utf-8")
            return Version(result.split('\n')[0].split()[-1])
        except KeyError as e:
            raise e(f'version keyname was not found')

    def get_binary(self):
        return binary_path(self.binary_name, self.bin_path, self.default_path)

    def install(self):

        if self.skip_install:
            logger.info('GcloudCLI 0 version used, skipping installation')
            return

        jq_bin = self.get_binary()
        # Skip installation if latest already installed
        if jq_bin == self.binary_full_path \
            and self.check_version() == self.version:
            logger.info(f'GcloudCLI {self.version} is already installed')
            return

        try:
            logger.info(f'Installing GcloudCLI {self.version} in {self.binary_full_path}')
            full_path = Path(self.binary_full_path)
            full_path.unlink(missing_ok=True)
            bin_path = Path(self.bin_path)
            bin_path.mkdir(parents=True, exist_ok=True)

            base = f"https://dl.google.com/dl/cloudsdk/channels/rapid/downloads/google-cloud-cli-{self.version.to_string()}-{self.operating_system}-{self.architecture}.tar.gz"
            source_url = base

            targz_file = Path(Request.urlretrieve(source_url)[0])
            shutil.unpack_archive(targz_file, self.default_venv, 'gztar')
            targz_file.unlink(missing_ok=True)
            gcloud_cmd = self.default_venv / 'google-cloud-sdk' / 'bin' / 'gcloud'

            # Verify checksum
            logger.info(f'GcloudCLI {self.version} checksum not verified')

            full_path.symlink_to(gcloud_cmd)
            full_path.chmod(0o770)
        except Exception as e:
            raise Exception(f'Failed to install GcloudCLI {self.version} - ({e})') from e

class BigAnimalCLI:
    binary_name = 'biganimal'
    min_version = Version("3.4.0")
    max_version = Version("3.5.1")
    arch_alias = {
        'amd64': 'x86_64',
    }
    DOT_PATH = __dot_project__

    def __init__(self, binary_dir=None, version=None):
        self.bin_dir = binary_dir if binary_dir else self.DOT_PATH
        self.version = self.max_version if not version else Version(version)
        self.skip_install = self.version == Version("0")
        self.default_path = Path(self.bin_dir) / self.binary_name / self.version.to_string() / 'bin'
        self.bin_path =  self.default_path
        self.binary_full_path = Path(self.bin_path) / self.binary_name
        self.architecture = self.arch_alias.get(platform.machine().lower(),platform.machine().lower())
        self.operating_system = platform.system().lower()

    def check_version(self):
        try:
            path = self.get_binary()
            command = [path, '--version']
            output = execute_shell(
                args=command,
                environment=os.environ.copy(),
            )
            result = output.decode("utf-8")
            return Version(result.split('\n')[0].split()[-2])
        except KeyError as e:
            raise e(f'version keyname was not found')

    def get_binary(self):
        return binary_path(self.binary_name, self.bin_path, self.default_path)

    def install(self):

        if self.skip_install:
            logger.info('BigAnimalCLI 0 version used, skipping installation')
            return

        biganimal_bin = self.get_binary()
        # Skip installation if latest already installed
        if biganimal_bin == self.binary_full_path \
            and self.check_version() == self.version:
            logger.info(f'BigAnimalCLI {self.version} is already installed')
            return

        try:
            logger.info(f'Installing BigAnimalCLI {self.version} in {self.binary_full_path}')
            full_path = Path(self.binary_full_path)
            full_path.unlink(missing_ok=True)
            bin_path = Path(self.bin_path)
            bin_path.mkdir(parents=True, exist_ok=True)
            script_path = Path(self.bin_path) / 'get-token.sh'
            script_path.unlink(missing_ok=True)

            base = f"https://cli.biganimal.com/download/{self.operating_system.capitalize()}/{self.architecture}/v{self.version.to_string()}/biganimal"
            get_token = "https://raw.githubusercontent.com/EnterpriseDB/cloud-utilities/main/api/get-token.sh"
            source_url = base

            binary_file = Path(Request.urlretrieve(source_url)[0])
            script_file = Path(Request.urlretrieve(get_token)[0])

            # Verify checksum
            logger.info(f'BigAnimalCLI {self.version} checksum not verified')

            binary_file.rename(full_path)
            binary_file.unlink(missing_ok=True)
            script_file.rename(script_path)
            script_file.unlink(missing_ok=True)
            full_path.chmod(0o770)
            script_path.chmod(0o770)

        except Exception as e:
            raise Exception(f'Failed to install BigAnimalCLI {self.version} - ({e})') from e
