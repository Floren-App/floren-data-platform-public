# JSON configuration schema

Generated schemas describe accepted configuration; server-side authorization and
layer restrictions still apply.

- [Download the fdp.json schema](fdp.schema.json).
- [Readable program configuration guide](../fdp-json.md).
- [HTTP OpenAPI specification](openapi.json), including workload and infrastructure models.

VS Code's FDP extension includes the program schema. Keep it synchronized using
`make schema` when changing the configuration model. Documentation generation does
not connect to the metadata database, IdP, Kubernetes or any data source.
