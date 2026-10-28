# Secrets and certificates

FDP stores secret values in the secret store you choose: the bundled OpenBao, your own
OpenBao or HashiCorp Vault, Kubernetes Secrets in a dedicated namespace, AWS Secrets Manager,
Azure Key Vault or Google Secret Manager. See
[bring your own infrastructure](architecture/bring-your-own.md#secret-store). The metadata database stores configuration and secret references, not a
substitute backup of every credential.

## Ownership boundaries

| Material | Management |
|---|---|
| Platform signing keys and database connection | Retained installation Secrets, managed by the cluster operator |
| Bundled OpenBao seal key | The `fdp-openbao-seal` Secret (Kubernetes model), or your KMS; back it up with the Raft snapshots |
| IdP automation credential | IdP plus an installation Secret; inject into OpenTofu's environment |
| Git integration read credential | FDP secret backend; use a technical identity with repository read access |
| Program API keys and certificates | FDP secrets referenced by authorized programs |
| Layer RustFS credentials | Issued for managed identities under their layer and resource permissions |
| Public CA bundle | Trust configuration; public certificates only, never private keys |

Do not commit credentials to Markdown examples, `fdp.json`, Helm values or Terraform
variables files. Use the provider's write-only/ephemeral credential inputs where
available. Protect Terraform state even when write-only fields are used: it still
contains sensitive configuration and identity information.

## Public and internal TLS

Configure public endpoints with a certificate trusted by clients. The chart can
reference an existing TLS Secret or use cert-manager. A custom CA must be trusted
by the browser/laptop and by the relevant platform components; setting a trust
bundle does not make an incorrect hostname valid.

Managed layers have their own TLS/trust configuration for their services.
API management can use JWT and mutual TLS. Keep incoming client trust, outgoing
server trust and client identity certificates distinct. See
[API management](api-management.md) and the [layer infrastructure reference](layer-infrastructure.md).

## Rotation, expiry and recovery

Certificates need renewal, deployment reload and monitoring. FDP's certificate
monitoring is useful evidence, but does not replace testing that the renewed
certificate is actually served. Use the documented
[operations and rotation procedures](kubernetes-operations.md), including retained
Secrets and backup restore. Signing-key and database credential rotation require
coordinating readers and active sessions; do not regenerate all installation keys
as a routine Helm upgrade.
