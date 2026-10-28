# SQL workspace: CloudBeaver Community Edition (Apache-2.0), the web edition of DBeaver.
# Schema browser, SQL editor with completion, result grids and saved .sql scripts; every query goes
# through the Floren Data Platform SQL gateway as the workspace owner. No data leaves the server: export,
# import and AI chat are switched off here, and the gateway blocks CloudBeaver's file transfer paths.
# Build from the repo root: docker build -f images/workspace-sql/Dockerfile -t fdp/workspace-sql:dev .
FROM dbeaver/cloudbeaver:26.2.2

USER root
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 \
 && rm -rf /var/lib/apt/lists/*

# uid 1000 like every other Floren Data Platform image, so the shared /workspace volume stays consistent.
RUN (userdel -r ubuntu 2>/dev/null || true) \
 && groupmod -g 1000 dbeaver && usermod -u 1000 -g 1000 dbeaver \
 && rm -rf /opt/cloudbeaver/workspace \
 && mkdir -p /workspace \
 && chown -R 1000:1000 /opt/cloudbeaver /home/dbeaver /workspace
ENV DBEAVER_UID=1000 DBEAVER_GID=1000

# Sign the workspace owner in from the gateway's authenticated user header (CloudBeaver's reverse-proxy
# provider), so scripts are saved to their own project. The gateway's forward-auth overwrites that header
# on every request; the container is reachable only through the gateway.
RUN sed -i 's|enabledAuthProviders: \[|authConfigurations: [{ id: "fdp", provider: "reverseProxy", displayName: "Floren Data Platform", disabled: false, parameters: { "user-header": "X-FDP-User", "team-header": "X-FDP-Team", "full-name-header": "X-FDP-Display-Name", "auto-user-provisioning": true } }],\n        enabledAuthProviders: [\n            "reverseProxy",|' /opt/cloudbeaver/conf/cloudbeaver.conf \
 && grep -q '"user-header": "X-FDP-User"' /opt/cloudbeaver/conf/cloudbeaver.conf

# Keep data on the server and keep the single managed connection. It lives in the owner's project (which
# CloudBeaver counts as a custom connection), so adding further connections is hidden instead; workspaces
# reach no data except through the governed gateway either way.
RUN sed -i 's|productSettings: {|productSettings: {\n            plugin.data-viewer.export.disabled: true,\n            plugin.data-import.disabled: true,\n            plugin.ai-chat.disabled: true,\n            plugin.datasource-transaction-manager.disabled: true,\n            plugin.connection-custom.disabled: true,|' /opt/cloudbeaver/conf/cloudbeaver.conf \
 && grep -q "plugin.data-viewer.export.disabled: true" /opt/cloudbeaver/conf/cloudbeaver.conf
ENV CLOUDBEAVER_APP_ANONYMOUS_ACCESS_ENABLED=false \
    CLOUDBEAVER_APP_GRANT_CONNECTIONS_ACCESS_TO_ANONYMOUS_TEAM=true \
    CLOUDBEAVER_APP_SUPPORTS_CUSTOM_CONNECTIONS=true \
    CLOUDBEAVER_APP_PUBLIC_CREDENTIALS_SAVE_ENABLED=false

COPY images/workspace-sql/fdp-sql-proxy.py images/workspace-sql/data-sources.json images/workspace-sql/fdp-entrypoint.sh images/workspace-sql/configure.py /opt/fdp/
# Bootstrap the mounted volume, then the entrypoint immediately drops to dbeaver (uid 1000).
USER root
EXPOSE 8978
ENTRYPOINT ["/opt/fdp/fdp-entrypoint.sh"]
