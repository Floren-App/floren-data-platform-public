# Quickstart

Choose local Compose for development, or the [Kubernetes installation](kubernetes-install.md)
for an installation with managed layer infrastructure. Both run the same control-plane API.

## Local development

Install Docker with Compose, Node.js 20+, Python 3.9+ and `mkcert`. The optional
desktop shell also needs Rust and Tauri's OS build dependencies. Browser mode does
not require Rust.

```sh
git clone https://github.com/Floren-App/floren-data-platform-public.git
cd floren-data-platform-public
mkcert -install
make images
make dev
make desktop-web
```

The first image build downloads substantial runtime dependencies. `make dev` creates
the local HTTPS certificates and starts the platform. Use the URL printed by the
web development server; its default is `https://localhost:1420`. The platform gateway
is `https://localhost:8090` and the local IdP is `https://localhost:8443`.

The local development bootstrap creates **admin/admin**. This is a disposable
development credential, not the Kubernetes bootstrap password. Keep local ports
private. [Kubernetes first sign-in](kubernetes-install.md#3-first-sign-in) uses a
temporary password from a retained Secret.

The platform starts without example business storage or repositories. An
administrator must configure compute pools, layers and storage before running
programs. Use [OpenTofu](terraform.md) or Administration. To explicitly install the
local finance/lake examples:

```sh
make storage-example
EXAMPLE_STORAGE=1 make seed
```

`make seed` without that opt-in only bootstraps the local administrator. See
[storage integrations](storage-integrations.md) for exact example requirements.

## First program

1. Select an app group that may own your new objects.
2. Create a workspace in the intended layer and wait for **Running**.
3. Open its editor and create `hello.py` and `fdp.json`:

```python title="hello.py"
import fdp

print(f"Hello, {fdp.args().get('name', 'world')}!")
```

```json title="fdp.json"
{
  "version": 1,
  "warehouse": "small",
  "programs": {
    "hello": {
      "entrypoint": "hello.py",
      "args": [{"name": "name", "type": "string", "default": "world"}]
    }
  }
}
```

Use a compute pool that actually exists and that you can execute on; `small` is an
example, not an automatically provisioned pool. Open the FDP runner in VS Code or
use `fdp run hello.py --pool small`. The script runs on compute, with its output in
the run console. Saving code in a workspace does not publish it to other users.

## Next steps

Commit a project to Git, [register its repository and publish it](deploying.md),
then assign its managed identity the specific data/API permissions it needs.
Follow the [Pokemon walkthrough](pokemon.md) for Test/Prod, ingestion, dbt,
Airflow, dashboards and engineering observability.
