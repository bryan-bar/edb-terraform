# coding: utf-8

import json
import yaml
from pathlib import Path, PurePath
import os
import sys
import shutil
import subprocess
import secrets
import base64
import datetime
from jinja2 import Environment, FileSystemLoader
import textwrap
from typing import List, Dict, Optional

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.backends import default_backend

from edbterraform import __version__, __python_version__, __virtual_env__, __dot_project__
from edbterraform.utils.dict import change_keys
from edbterraform.utils.files import load_yaml_file, render_template
from edbterraform.utils.logs import logger
from edbterraform.CLI import TerraformCLI

def tpl(template_name, dest, csp, vars={}):
    # Renders and saves a jinja2 template based on a given template name and
    # variables.

    try:
        # Templates are located in __file__/data/templates/<cloud-service-provider>
        current_dir = Path(__file__).parent.resolve()
        templates_dir = PurePath.joinpath(
            current_dir, 'data', 'templates', csp
        )

        # Jinja2 rendering
        file_loader = FileSystemLoader(str(templates_dir))
        env = Environment(loader=file_loader, trim_blocks=True)
        template = env.get_template(template_name)

        # Render and save
        content = template.render(**vars)
        with open(dest, 'w') as f:
            f.write(content)

    except Exception as e:
        logger.error("ERROR: could not render template %s (%s)" % (template_name, e))
        sys.exit(1)

def update_terraform_blocks(file, template_vars, infra_vars, cloud_service_provider, remote_state_type='local', blocks=['provider', 'terraform',]):
    '''
    Update terraform blocks from a terraform json configuration file:
    - provider
    - terraform
    '''
    csp_terraform_provider = {
        'aws': 'aws',
        'gcloud': 'google',
        'azure': 'azurerm',
    }
    csp_terraform_backend = {
        'aws': 's3',
        'gcloud': 'gcs',
        'azure': 'azurerm',
        'postgres': 'pg',
        'postgresql': 'pg',
        'hashicorp': 'consul',
    }
    try:
        data = load_yaml_file(file)
        with open(file, 'w') as f:
            for block in blocks:

                # Due to limitations of terraform 1.x,
                # we need to update the provider block for each region that will be used.
                # We must set the region and an alias to avoid conflicts for each provider block.
                if block == 'provider':
                    if block not in data:
                        data[block] = dict()

                    provider_name = csp_terraform_provider.get(cloud_service_provider, cloud_service_provider)
                    for region in infra_vars['spec']['regions']:
                        if provider_name not in data[block]:
                            data[block][provider_name] = list()
                            # Azure requires a generic features block otherwise it fails with '"features" block is required'
                            if cloud_service_provider == 'azure':
                                data[block][provider_name].append({'features': {'resource_group': {'prevent_deletion_if_contains_resources': False}}})

                        items = {'alias': region.replace('-','_')}

                        # Allow force destruction of resources in the resource group
                        if cloud_service_provider == 'azure':
                                items['features'] = {'resource_group': {'prevent_deletion_if_contains_resources': False}}

                        # Azure and Gcloud don't require a region to be set in the provider block,
                        #   but assume region restrictions due to AWS
                        if cloud_service_provider != 'azure':
                            items['region'] = region
                        data[block][provider_name].append(items)


                # https://developer.hashicorp.com/terraform/language/v1.3.x/settings/backends/configuration
                # Update the terraform backend block with the remote state type.
                # Expected as { terraform: { backend: { <remote_state_type>: {} }}
                # Set as an empty configuration to force the user to configure the resources with:
                # - CLI options: `terraform init -backend-config="KEY=VALUE"`
                # - Filepath with recommend filename scheme - `*.<backendname>.tfbackend`: `terraform init -backend-config="PATH"`
                # - Interactive when `--input=false` is not set.
                # Credentials can still be exposed in 2 ways:
                # - terraform.tfstate files
                # - .terraform/ directory files
                if block == 'terraform':
                    if block not in data:
                        data[block] = dict()
                    # Setup/Overwrite the backend block
                    if 'backend' not in data[block] or data[block]['backend']:
                        data[block]['backend'] = dict()
                    # Handle the remote state type with the cloud providers storage service
                    if remote_state_type == 'cloud':
                        remote_state_type = cloud_service_provider

                    data[block]['backend'][csp_terraform_backend.get(remote_state_type, remote_state_type)] = {}

                    # Set the required terraform version
                    data[block]['required_version'] = '>= %s, <= %s' % (TerraformCLI.min_version.to_string(), TerraformCLI.max_version.to_string())

            f.write(json.dumps(data, indent=2, sort_keys=True))
    except Exception as e:
        raise Exception('ERROR: could not update terraform blocks in %s - (%s)' % (file, repr(e)))

def create_project_dir(
        project_directory,
        cloud_service_provider,
        infrastructure_file,
        template_variables,
        infrastructure_variables,
        user_hcl_lock_file
    ):
    '''
    Create new terraform project directory and copy needed files
    - cloud service provider modules
    - edb-terraform backup files
      - infrastructure.yml.j2 template file
      - variables.yml file which is used by the infrastructure.yml.j2 template file
      - terraform.tfvars.yml file with infrastructure variables after being rendered, if needed.
      - system.yml file with:
        - edb-terraform version
        - python version
        - terraform version and used stages
      - hcl lock file
    - hcl lock file
    - empty terraform state file to mark it as a terraform project
    '''
    SCRIPT_DIRECTORY = Path(__file__).parent.resolve()
    TERRAFORM_CLOUD_MODULES_DIRECTORY = SCRIPT_DIRECTORY / 'data' / 'terraform' / cloud_service_provider / 'modules'
    TERRAFORM_BIGANIMAL_MODULES_DIRECTORY = SCRIPT_DIRECTORY / 'data' / 'terraform' / 'biganimal' / 'modules'
    TERRAFORM_PROJECT_MODULES_DIRECTORY = project_directory / 'modules'
    TERRAFORM_PROVIDERS_FILE = SCRIPT_DIRECTORY / 'data' / 'terraform' / 'providers.tf.json'
    TERRAFORM_COMMON_VARS_FILE = SCRIPT_DIRECTORY / 'data' / 'terraform' / 'common_vars.tf'
    TERRAFORM_VERSIONS_FILE = SCRIPT_DIRECTORY / 'data' / 'terraform' / 'versions.tf'
    TERRAFORM_STATE_FILE = project_directory / 'terraform.tfstate'
    PROJECT_PATH_PERMISSIONS = 0o750
    TERRAFORM_STATE_PERMISSIONS = 0o600
    HCL_LOCK_FILE = project_directory / '.terraform.lock.hcl'
    EDB_TERRAFORM_DIRECTORY = project_directory / 'edb-terraform'
    BACKUP_TEMPLATE = EDB_TERRAFORM_DIRECTORY / 'infrastructure.yml.j2'
    BACKUP_TEMPLATE_INPUTS = EDB_TERRAFORM_DIRECTORY / 'variables.yml'
    BACKUP_TEMPLATE_FINAL = EDB_TERRAFORM_DIRECTORY / 'terraform.tfvars.yml'
    BACKUP_SYSTEM = EDB_TERRAFORM_DIRECTORY / 'system.yml'
    if project_directory.exists():
        sys.exit("ERROR: directory %s already exists" % project_directory)

    if not TERRAFORM_CLOUD_MODULES_DIRECTORY.exists():
        sys.exit("ERROR: directory %s does not exist" % TERRAFORM_CLOUD_MODULES_DIRECTORY)

    try:
        logger.info(f'Making directory and copying terraform modules {TERRAFORM_CLOUD_MODULES_DIRECTORY} into {project_directory}')
        shutil.copytree(TERRAFORM_CLOUD_MODULES_DIRECTORY, TERRAFORM_PROJECT_MODULES_DIRECTORY)
        shutil.copytree(TERRAFORM_BIGANIMAL_MODULES_DIRECTORY, TERRAFORM_PROJECT_MODULES_DIRECTORY, dirs_exist_ok=True)
        shutil.copyfile(TERRAFORM_PROVIDERS_FILE, project_directory / TERRAFORM_PROVIDERS_FILE.name)
        shutil.copyfile(TERRAFORM_VERSIONS_FILE, project_directory / TERRAFORM_VERSIONS_FILE.name)
        shutil.copyfile(TERRAFORM_COMMON_VARS_FILE, project_directory / TERRAFORM_COMMON_VARS_FILE.name)
        os.chmod(project_directory, PROJECT_PATH_PERMISSIONS)

        # Create the statefile and change file/folder permissions 
        # since it is not-encrypted by default and may contain secrets
        TERRAFORM_STATE_FILE.touch()
        os.chmod(TERRAFORM_STATE_FILE, TERRAFORM_STATE_PERMISSIONS)

        if user_hcl_lock_file:
            logger.info(f'Copying HCL lock file: {user_hcl_lock_file}')
            shutil.copyfile(user_hcl_lock_file, HCL_LOCK_FILE)
        else:
            logger.info(f'HCL lock file was not provided. Terraform will create one under {HCL_LOCK_FILE} during terraform init')
            HCL_LOCK_FILE.touch()

        logger.info(f'Copying edb-terraform files into {EDB_TERRAFORM_DIRECTORY}')
        EDB_TERRAFORM_DIRECTORY.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(HCL_LOCK_FILE, EDB_TERRAFORM_DIRECTORY / 'terraform.lock.hcl')
        shutil.copyfile(TERRAFORM_PROVIDERS_FILE, EDB_TERRAFORM_DIRECTORY / TERRAFORM_PROVIDERS_FILE.name)
        shutil.copyfile(TERRAFORM_VERSIONS_FILE, EDB_TERRAFORM_DIRECTORY / TERRAFORM_VERSIONS_FILE.name)
        shutil.copyfile(infrastructure_file, BACKUP_TEMPLATE)
        with open(BACKUP_TEMPLATE_INPUTS, 'a') as f:
            f.write(yaml.dump(template_variables))
        with open(BACKUP_TEMPLATE_FINAL, 'a') as f:
            f.write(yaml.dump(infrastructure_variables))
        with open(BACKUP_SYSTEM, 'a') as f:
            system_data = {
                'edb-terraform': {'version': __version__},
                'python': {
                    'version': __python_version__,
                    'venv': __virtual_env__,
                },
            }
            f.write(yaml.dump(system_data))

    except Exception as e:
        logger.error("ERROR: cannot create project directory %s (%s)" % (project_directory, e))

        # Always create the state file to mark the project as a terraform project directory
        if project_directory.exists() and not TERRAFORM_STATE_FILE.exists():
            TERRAFORM_STATE_FILE.touch()
            os.chmod(TERRAFORM_STATE_FILE, TERRAFORM_STATE_PERMISSIONS)

        sys.exit(1)

def destroy_project_dir(dir):
    if not os.path.exists(dir):
        return

    try:
        logger.info(f'Destroying directory: {dir}')
        shutil.rmtree(dir)
    except Exception as e:
        raise("Error: unable to delete project directory %s (%s)" % (dir, e))

def save_terraform_vars(dir, filename, vars):
    # Saves terraform variables as a JSON file.

    dest = dir / filename
    try:
        with open(dest, 'w') as f:
            content = json.dumps(vars, indent=2, sort_keys=True)
            f.write(content)
    except Exception as e:
        logger.error("ERROR: could not write %s (%s)" % (dest, e))
        sys.exit(1)

def save_user_templates(project_path: Path, templates: List[str]):
    '''
    Save any user templates under project/templates
    For reuse during terraform execution and portability of directory
    '''
    logger.info(f'Saving user templates: {templates}')
    directory = "templates"
    basepath = project_path / directory

    try:
        if not basepath.exists():
            logger.info(f'Creating template directory: {basepath}')
            basepath.mkdir(parents=True, exist_ok=True)

        for template in templates:
            template = Path(template)

            if not template.exists():
                raise Exception("templates %s does not exist" % template)

            if template.is_dir():
                for file in template.iterdir():
                    logger.info(f'Copying {file} into {basepath}')
                    shutil.copy2(str(file), str(basepath))

            if template.is_file():
                logger.info(f'Copying {template} into {basepath}')
                shutil.copy2(str(template), str(basepath))

    except Exception as e:
        logger.error("Cannot create template (%s)" % (e))
        logger.error("Current working directory: %s" % (Path.cwd()))
        logger.error("List of templates: %s" % (templates))
        sys.exit(1)

def regions_to_peers(regions):
    # Build a list of peer regions, based on a given list of regions.
    # For example, taking the following region list: [A, B, C, D]
    # List of peers will be: [(A, B), (A, C), (A, D), (B, C), (B, D), (C, D)]

    # At this point, regions is a dict coming directly from the infrastructure
    # file, we need to convert if to a list of regions.
    region_list = list(regions.keys())
    region_list_cpy = region_list.copy()
    peer_list = []
    i = 0

    for r in region_list:
        for p in range(i+1, len(region_list_cpy)):
            peer_list.append((r, region_list_cpy[p]))
        i += 1

    return peer_list


def object_regions(object_type, vars):
    # Returns the region list used by an object type. Object types are:
    # machines or databased

    regions = []

    if object_type not in vars:
        return regions

    for _, value in vars[object_type].items():
        region = value.get('region')

        if not region:
            continue

        if region not in regions:
            regions.append(region)

    return regions

def build_vars(csp: str, infra_vars: Path, server_output_name: str):

    # Based on the infra variables, returns a tuple composed of (terraform
    # variables as a dist, template variables as a dict)

    # Get a spec compatable object
    infra_vars = spec_compatability(infra_vars, csp)

    # Variables used in the template files
    # Build jinja template variable
    template_vars = dict(
        output_name = server_output_name,
        has_region_peering=(infra_vars.get('regions') and len(infra_vars['regions'].keys()) > 1),
        has_regions=('regions' in infra_vars),
        has_machines=('machines' in infra_vars),
        has_databases=('databases' in infra_vars),
        has_biganimal=('biganimal' in infra_vars),
        has_kubernetes=('kubernetes' in infra_vars),
        regions=infra_vars.get('regions',{}).copy(),
        peers=regions_to_peers(infra_vars.get('regions',{})),
        # biganimal regions are not needed since the BigAnimal Provider is not region specific.
        machine_regions=object_regions('machines', infra_vars),
        database_regions=object_regions('databases', infra_vars),
        kubernetes_regions=object_regions('kubernetes', infra_vars),

        # AWS Specific
        has_aurora=('aurora' in infra_vars),
        aurora_regions=object_regions('aurora', infra_vars),

        # GCloud Specific
        has_alloy=('alloy' in infra_vars),
        alloy_regions=object_regions('alloy', infra_vars),
    )

    # Starting with making a copy of infra_vars as our terraform_vars dict
    # Since our terraform modules implement a specification module,
    # it needs the the cloud service provider values from the file as a terraform `spec` variable
    # and the cloud provider name as a terraform `cloud_service_provider` variable
    terraform_vars = dict(
        spec = infra_vars.copy(),
        cloud_service_provider = 'gcp' if csp == 'gcloud' else csp,
    )

    return (terraform_vars, template_vars)

def generate_terraform(
        infra_file: Path,
        project_path: Path,
        csp: str,
        bin_path: Path,
        infra_template_variables: Optional[Dict]={},
        terraform_version: str = TerraformCLI.max_version.to_string(),
        user_templates: Optional[List[Path]]=[],
        hcl_lock_file: Optional[Path]=None,
        run_validation: bool = False,
        apply: bool = False,
        destroy: bool = False,
        remote_state_type: str = 'local',
    ) -> dict:
    """
    Generates the terraform files from jinja templates and terraform modules and
    saves the files into a project_directory for use with 'terraform' commands

    Returns a dictionary with the following keys:
    - terraform_output: usable with terraform outputs command after terraform apply 
    - ssh_filename
    """
    SERVERS_OUTPUT_NAME = 'servers'
    OUTPUT = {
        'project_path': '',
        'terraform_output': '',
        'ssh_filename': '',
    }

    # Destroy existing project before creating a new one
    run_terraform(project_path, bin_path, terraform_version, validate=False, apply=False, destroy=destroy)

    # Get final instrastructure variables after rendering it if it is a jinja2 template
    infra_vars = load_yaml_file(render_template(template_file=infra_file, values=infra_template_variables))

    # Duplicate terraform code into target project directory
    create_project_dir(project_path, csp, infra_file, infra_template_variables, infra_vars, hcl_lock_file)

    # Allow for user supplied templates
    # Terraform does not allow us to copy a template and then reference it within the same run when using templatefile()
    # To get past this, we will need to copy over all the user passed templates into the project directory
    infra_file_templates = infra_vars.get(csp, {}).get('templates', [])
    if not isinstance(infra_file_templates, list):
        raise TypeError("Template variables should pass in a list of strings that represent a path or rely on the CLI passthrough")
    # Remove templates from final terraform variables since save_user_templates will save them into project_name/templates/
    if infra_file_templates:
        del infra_vars[csp]['templates']
    user_templates.extend(infra_file_templates)
    save_user_templates(project_path, user_templates)

    # Transform variables extracted from the infrastructure file into
    # terraform and templates variables.
    (terraform_vars, template_vars) = \
        build_vars(csp, infra_vars, SERVERS_OUTPUT_NAME)

    # Save terraform vars file
    save_terraform_vars(
        project_path, 'terraform.tfvars.json', terraform_vars
    )

    # Generate the main.tf files.
    tpl(
        'main.tf.j2',
        project_path / 'main.tf',
        csp,
        template_vars
    )

    # Generate provider.tf.json
    update_terraform_blocks(
        project_path / 'providers.tf.json',
        template_vars,
        terraform_vars,
        csp,
        remote_state_type,
        ['provider', 'terraform'],
    )

    # terraform_vars holds the spec object for use in terraform
    OUTPUT['terraform_output'] = SERVERS_OUTPUT_NAME
    if 'ssh_key' in terraform_vars['spec'] and 'output_name' in terraform_vars['spec']['ssh_key']:
        OUTPUT['ssh_filename'] = terraform_vars['spec']['ssh_key']['output_name']
    OUTPUT['project_path'] = str(project_path.resolve())

    run_terraform(project_path, bin_path, terraform_version, run_validation, apply)

    logger.info(textwrap.dedent('''
    Success!
    You can use now use terraform and see info about your boxes after creation:
    * cd {project_path}
    * terraform init
    * terraform plan -out terraform.plan
    * terraform apply -auto-approve terraform.plan
    * terraform output -json {output_key}
    * ssh <ssh_user>@<ip-address> -i {ssh_file}
    ''').format(
        project_path = project_path,
        output_key = OUTPUT['terraform_output'],
        ssh_file = OUTPUT['ssh_filename'],
    ))

    return OUTPUT

def run_terraform(cwd, bin_path, version, validate=False, apply=False, destroy=False):
        terraform = TerraformCLI(bin_path, version)
        if destroy:
            try:
                if terraform.destroy_command(cwd):
                    destroy_project_dir(cwd)
                return
            except subprocess.CalledProcessError as e:
                logger.warning(textwrap.dedent('''
                Destroy skipped or failed, check {bin_path}.
                Remove --destroy option or install terraform >= {min_version}
                and rerun edb-terraform.
                Install and manually run:
                1. `terraform state list`
                2. `terraform destroy`
                ''').format(
                    bin_path=bin_path,
                    min_version=terraform.min_version,
                ))
                logger.error(f'Error: ({e.output})')
                sys.exit(e.returncode)
            except Exception as e:
                logger.error(f'Error: ({e})')
                sys.exit(1)

        if validate:
            try:
                terraform.init_command(cwd)
                terraform.plan_command(cwd)
                terraform.apply_command(cwd, validate)
                return
            except subprocess.CalledProcessError as e:
                logger.warning(textwrap.dedent('''
                Validation skipped, check {bin_path}.
                Remove --validate option or install terraform >= {min_version}
                and rerun edb-terraform
                Install and manually run:
                1. `terraform init`
                2. `terraform plan`
                3. `terraform apply -target=null_resource.validation`
                ''').format(
                    bin_path=bin_path,
                    min_version=terraform.min_version,
                ))
                logger.error(f'Error: ({e.output})')
                destroy_project_dir(cwd)
                sys.exit(e.returncode)

        if apply:
            try:
                terraform.init_command(cwd)
                terraform.plan_command(cwd)
                terraform.apply_command(cwd)
                return
            except subprocess.CalledProcessError as e:
                logger.warning(textwrap.dedent('''
                Apply skipped or failed, check {bin_path}.
                Remove --apply option or install terraform >= {min_version}
                and rerun edb-terraform.
                Install and manually run:
                1. `terraform init`
                2. `terraform plan`
                3. `terraform apply`
                ''').format(
                    bin_path=bin_path,
                    min_version=terraform.min_version,
                ))
                logger.error(f'Error: ({e.output})')
                run_terraform(cwd, bin_path, version, validate=False, apply=False, destroy=True)
                sys.exit(e.returncode)

"""
Support backwards compatability to older specs 
since each collection of modules should implement a specification module
with the shape of the data it expects
Anything defined here is depreciated and might be removed in future releases
"""
def spec_compatability(infrastructure_variables, cloud_service_provider):

    SSH_OUT_FILENAME = 'ssh-id_rsa'
    spec_variables = None

    try:
        spec_variables = infrastructure_variables[cloud_service_provider].copy()
    except:
        raise KeyError("ERROR: key '%s' not present in the infrastructure file." % cloud_service_provider)
    
    replace_pairs = {
        # Modules used to expect azs and az
        "azs": "zones",
        "az": "zone",
    }
    spec_variables = change_keys(spec_variables, replace_pairs)

    # Users were able to use 'cluster_name' at the same level as cloud_service_provider before
    if 'tags' not in spec_variables:
        spec_variables['tags'] = dict()
    if 'cluster_name' not in spec_variables['tags'] and \
        'cluster_name' in infrastructure_variables:
        spec_variables['tags']['cluster_name'] = infrastructure_variables['cluster_name']

    # Handle precomputed tags at generation time instead of during 'terraform apply'
    # No longer handled within the terraform spec module and should be passed in as a project wide tag
    # - terraform_hex   = random_id.apply.hex | ex: "a24f8f4e"
    # - terraform_id    = random_id.apply.id | ex: "ok-PTg"
    # - terraform_time  = time_static.first_created.id | ex: "2024-11-26T01:36:28Z"
    while True:
        token = secrets.token_bytes(4)
        spec_variables['tags']['terraform_hex'] = token.hex()
        spec_variables['tags']['terraform_id'] = base64.b64encode(token).decode('utf-8').rstrip("=").replace('+','-').replace('/','-')
        spec_variables['tags']['terraform_time'] = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        if spec_variables['tags']['terraform_id'][0].isalnum() and spec_variables['tags']['terraform_id'][-1].isalnum():
            break

    # if not provided,
    # assign default output name for private/public ssh key filename
    if 'ssh_key' not in spec_variables:
        spec_variables['ssh_key'] = dict()
    if 'ssh_key' in spec_variables and 'output_name' not in spec_variables['ssh_key']:
        spec_variables['ssh_key']['output_name'] = SSH_OUT_FILENAME

    # use 'image_name' to assign an os per instance,
    # which references an operating system from 'images' and includes 'ssh_user' 
    os_default = 'depreciated_default'
    if 'operating_system' in spec_variables:
        if 'images' not in spec_variables:
            spec_variables['images'] = {}
        spec_variables['images'][os_default] = spec_variables['operating_system']
        # 'ssh_user' can vary by image or use case and has been depreciated at the top level
        if 'ssh_user' in spec_variables:
            spec_variables['images'][os_default]['ssh_user'] = spec_variables['ssh_user']

        # update machines with depreciated default, if needed
        if 'machines' in spec_variables:
            for name in spec_variables['machines']:
                if 'image_name' not in spec_variables['machines'][name]:
                    spec_variables['machines'][name]['image_name'] = os_default

    # azure allows for an ssh_user, discarded in terraform spec for aws and gcloud
    # use 'ssh_user' per kubernetes cluster
    if 'ssh_user' in spec_variables and 'kubernetes' in spec_variables:
        for name in spec_variables['kubernetes']:
            if 'ssh_user' not in spec_variables['kubernetes'][name]:
                spec_variables['kubernetes'][name]['ssh_user'] = spec_variables['ssh_user']

    # change to each region zones to handle the same zone defined multple times
    # previously the mappings were defined as zones, ex. us-west-2a: 10.0.0.0/24
    # terraform variable: optional(map(string)) -> optional(map(object))
    # use 'zone_name' with machines and google kubernetes to track the zone wanted for use
    if 'regions' in spec_variables:
        for region in spec_variables['regions']:
            # check if all zones defined with a string
            # compatability skipped otherwise
            if 'zones' in spec_variables['regions'][region] and \
                isinstance(spec_variables['regions'][region]['zones'], dict) and \
                all([isinstance(item, str) for _, item in spec_variables['regions'][region]['zones'].items()]):
                temp = {}
                for zone, cidr in spec_variables['regions'][region]['zones'].items():
                    temp[f'depreciated-{zone}'] = {
                        'zone': zone,
                        'cidr': cidr,
                    }
                spec_variables['regions'][region]['zones'] = temp
            # Handle ports as a single list of ports
            ports = []
            for port in spec_variables['regions'][region].get('service_ports', []):
                ports.append({'defaults': 'service' ,**port})
            if spec_variables['regions'][region].get('service_ports', []):
                del spec_variables['regions'][region]['service_ports']
            for port in spec_variables['regions'][region].get('region_ports', []):
                ports.append({'defaults': 'internal' ,**port})
            if spec_variables['regions'][region].get('region_ports', []):
                del spec_variables['regions'][region]['region_ports']
            if 'ports' not in spec_variables['regions'][region]:
                spec_variables['regions'][region]['ports'] = []
            spec_variables['regions'][region]['ports'].extend(ports)

    if 'machines' in spec_variables:
        for machine in spec_variables['machines']:
            if 'zone_name' not in spec_variables['machines'][machine] and 'zone' in spec_variables['machines'][machine]:
                spec_variables['machines'][machine]['zone_name'] = f'depreciated-{spec_variables["machines"][machine]["zone"]}'

    if 'kubernetes' in spec_variables:
        for cluster in spec_variables['kubernetes']:
            if 'zone_name' not in spec_variables['kubernetes'][cluster] and 'zone' in spec_variables['kubernetes'][cluster]:
                spec_variables['kubernetes'][cluster]['zone_name'] = f'depreciated-{spec_variables["kubernetes"][machine]["zone"]}'

    # BigAnimal supports single, ha, and pgd cluster types
    # PGD uses its own resource and defines a set of data nodes and witness nodes.
    #
    # Handle single and ha under data_nodes
    #   which should be a single item object with a single key
    if 'biganimal' in spec_variables:
        for cluster in spec_variables['biganimal']:
            if 'data_groups' not in spec_variables['biganimal'][cluster]:
                items = spec_variables['biganimal'][cluster].copy()
                spec_variables['biganimal'][cluster]['data_groups'] = {'deprecated': items}

    return spec_variables
