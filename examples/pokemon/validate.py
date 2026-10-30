#!/usr/bin/env python3
"""Validate prepared configuration offline: no API, Kubernetes backend or live state.

Requires Go and OpenTofu. Builds the provider into a temporary local mirror, copies
only checked-in configuration (no tfvars/secrets), and removes everything afterward.
"""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]
LOCAL_VERSION = '999.0.0'


def run(args, **kwargs):
    subprocess.run(args, check=True, **kwargs)


def main():
    with tempfile.TemporaryDirectory(prefix='pokemon-config-') as directory:
        work = Path(directory)
        platform = '_'.join(subprocess.check_output(['go','env','GOOS','GOARCH'], text=True).split())
        # A local build numbered above every release, so the module's minimum version accepts it.
        target = work/'mirror/registry.opentofu.org/floren-app/fdp'/LOCAL_VERSION/platform
        target.mkdir(parents=True)
        run(['go','build','-ldflags','-X main.version='+LOCAL_VERSION,'-o',str(target/('terraform-provider-fdp_v'+LOCAL_VERSION)),'.'],
            cwd=ROOT/'terraform/provider-fdp')
        config = work/'tofurc'
        config.write_text('provider_installation {\n filesystem_mirror {\n path = "'+str(work/'mirror')+'"\n'
                          ' include = ["registry.opentofu.org/floren-app/fdp"]\n }\n'
                          ' direct { exclude = ["registry.opentofu.org/floren-app/fdp"] }\n}\n')
        folders = ['terraform/modules/pokemon']
        for folder in folders:
            checkout = work/'repo'/folder
            checkout.mkdir(parents=True)
            for source in (ROOT/folder).glob('*.tf'):
                shutil.copyfile(source,checkout/source.name)
        for index, folder in enumerate(folders):
            env = {**os.environ, 'TF_CLI_CONFIG_FILE':str(config), 'TF_DATA_DIR':str(work/('data'+str(index))),
                   'TF_IN_AUTOMATION':'1', 'KUBECONFIG':os.devnull, 'KUBE_CONFIG_PATH':os.devnull}
            print('Validating '+folder,flush=True)
            for args in [('fmt','-check'),('init','-backend=false','-input=false','-no-color'),('validate','-no-color')]:
                run(['tofu',*args],cwd=work/'repo'/folder,env=env)


if __name__ == '__main__':
    main()
