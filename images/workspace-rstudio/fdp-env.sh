#!/usr/bin/with-contenv bash
# RStudio sessions don't inherit the container environment; write the Floren Data Platform vars to Renviron.site.
for var in FDP_API FDP_SESSION_TOKEN FDP_WORKSPACE_ID FDP_USER GIT_CONFIG_GLOBAL \
           HTTPS_PROXY HTTP_PROXY NO_PROXY https_proxy http_proxy no_proxy \
           FDP_CRAN RENV_CONFIG_REPOS_OVERRIDE PIP_INDEX_URL PIP_TRUSTED_HOST; do
  if [ -n "${!var:-}" ]; then
    echo "${var}=${!var}" >> /usr/local/lib/R/etc/Renviron.site
  fi
done

# Git settings and SSH keys on the workspace volume, template repositories cloned (as the rstudio user, uid 1000;
# -p keeps the container environment: the platform address and token, the proxy, the repositories to clone).
su -p rstudio -s /bin/sh -c "HOME=/home/rstudio /usr/local/bin/fdp-git-setup.sh" \
  || echo "fdp: git setup skipped" >&2
