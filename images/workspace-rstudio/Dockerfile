# Editor tier for R / Shiny users: RStudio Server open-source edition (AGPL-3).
# Build from the repo root: docker build -f images/workspace-rstudio/Dockerfile -t fdp/workspace-rstudio:dev .
FROM rocker/rstudio:4.4.1

# System libraries for packages compiled from source (always the case on arm64, where no
# prebuilt Linux binaries exist): curl/httr2 need libcurl+openssl, httpuv (shiny) needs zlib.
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-venv libpq-dev libcurl4-openssl-dev libssl-dev zlib1g-dev curl git \
 && rm -rf /var/lib/apt/lists/*

RUN install2.r --error --skipinstalled --ncpus -1 shiny DBI RPostgres httr2 dplyr ggplot2
# Runner panel dependencies; a separate layer keeps the slow compile above cached.
RUN install2.r --error --skipinstalled jsonlite rstudioapi rappdirs reticulate

# The R streaming API delegates Kafka protocol handling to the same governed Python SDK.
COPY sdk/python /tmp/sdk
RUN python3 -m venv /opt/fdp-python && /opt/fdp-python/bin/pip install --no-cache-dir /tmp/sdk[kafka] && rm -rf /tmp/sdk
ENV PATH="/opt/fdp-python/bin:${PATH}" RETICULATE_PYTHON=/opt/fdp-python/bin/python

COPY sdk/r/fdp /tmp/fdp
RUN R CMD INSTALL /tmp/fdp && rm -rf /tmp/fdp

COPY images/workspace-rstudio/rsession.conf /etc/rstudio/rsession.conf
# Opens the Floren Data Platform runner (Viewer pane) at session start.
# Packages install from the platform's package repository (FDP_CRAN) instead of rocker's default.
COPY images/workspace-rstudio/Rprofile.site /usr/local/lib/R/etc/Rprofile.site.fdp
COPY images/common/Rprofile-packages.site /usr/local/lib/R/etc/Rprofile.site.packages
RUN cat /usr/local/lib/R/etc/Rprofile.site.fdp /usr/local/lib/R/etc/Rprofile.site.packages >> /usr/local/lib/R/etc/Rprofile.site

# The rstudio user is uid 1000, same as every other Floren Data Platform image, so /workspace is shared cleanly.
RUN mkdir -p /workspace && chown 1000:1000 /workspace \
 && ln -s /workspace /home/rstudio/workspace

# rocker's s6 init passes container env only through Renviron; keep the SDK variables visible to sessions.
# The same init step prepares persistent git settings on the workspace volume.
COPY images/common/fdp-git-setup.sh images/common/git-credential-fdp /usr/local/bin/
COPY images/workspace-rstudio/fdp-env.sh /etc/cont-init.d/99-fdp-env
RUN chmod +x /etc/cont-init.d/99-fdp-env /usr/local/bin/fdp-git-setup.sh /usr/local/bin/git-credential-fdp
