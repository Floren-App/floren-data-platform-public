# Maintaining the documentation

All authored documentation lives in Markdown under `docs/`. MkDocs Material builds
navigation, search, code blocks and HTML. There is no separate documentation
database or hand-written duplicate of the site inside FDP.

## Preview and build

```sh
make docs-install
make docs-serve
```

`make docs-build` builds with strict internal-link/anchor validation and checks
that operational material is absent. `make docs-reference` regenerates HTTP API,
program schema, Python signatures and Terraform resource tables from source.
That target needs the control-plane Python environment and Go. The ordinary
Markdown preview uses the checked-in generated references.

## Add or update a page

1. Edit a Markdown guide or add a new one under `docs/`.
2. Add it to `mkdocs.yml` navigation. Only explicitly selected pages are published;
   progress logs and arbitrary new files are excluded from pages and search.
3. Use relative Markdown links for other guides. Repository source links such as
   `../sdk/python/fdp/__init__.py` are rewritten to public source URLs during build.
4. Verify example names, CLI flags and fields against the implementation.
5. Regenerate references if schemas/signatures changed, then run `make docs-build`.

Describe current behavior separately from future plans. Record limitations when a
feature requires a particular backend, integration, privilege or cluster capability.
Never present a manual installation check as a test of every supported environment.

## Publishing

The `documentation` GitHub Actions workflow builds on pull requests and main pushes.
Only the public FDP repository deploys to its GitHub Pages environment. Deployment
uses GitHub's Pages artifact/OIDC workflow, without a personal access token.
The site currently follows `main`; historical guides remain accessible at source commits.

Application help links use the site's stable page routes. `VITE_FDP_DOCS_URL` can
override the base address when building a private/offline distribution. The default
points to the public FDP Pages site. Help opens documentation without passing FDP
credentials, resource IDs or signed workload links.

## Private material

Keep environment hostnames, secrets, state, signed URLs, diagnostic dumps and
installation runbooks in private configuration repositories. A private Git repo is
not by itself a guarantee that a Pages deployment is private. The site builder
publishes only navigation-selected pages and generated schema attachments.
