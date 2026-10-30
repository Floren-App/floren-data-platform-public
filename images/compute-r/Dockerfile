# Compute tier for R: Rscript jobs and Shiny apps.
# Build from the repo root: docker build -f images/compute-r/Dockerfile -t fdp/compute-r:dev .
# Other versions for the runtime catalog: make runtime KIND=r VERSION=4.3
ARG R_VERSION=4.4.1
FROM rocker/r-ver:${R_VERSION}

# Same build dependencies as the RStudio image (see there).
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-venv libpq-dev libcurl4-openssl-dev libssl-dev zlib1g-dev \
 && rm -rf /var/lib/apt/lists/*

# arrow is left out: building it from source on arm64 is slow and fragile. Add it in an amd64-only image if needed.
RUN install2.r --error --skipinstalled --ncpus -1 shiny bslib DBI RPostgres httr2 dplyr ggplot2 data.table
# jsonlite (fdp::args) comes with httr2; a separate layer keeps the slow compile above cached.
RUN install2.r --error --skipinstalled jsonlite rappdirs reticulate

# The R streaming API delegates Kafka protocol handling to the same governed Python SDK.
COPY sdk/python /tmp/sdk
RUN python3 -m venv /opt/fdp-python && /opt/fdp-python/bin/pip install --no-cache-dir /tmp/sdk[kafka] && rm -rf /tmp/sdk
ENV PATH="/opt/fdp-python/bin:${PATH}" RETICULATE_PYTHON=/opt/fdp-python/bin/python

COPY sdk/r/fdp /tmp/fdp
RUN R CMD INSTALL /tmp/fdp && rm -rf /tmp/fdp

# install.packages() in programs uses the platform's package repository (FDP_CRAN), never the internet.
COPY images/common/Rprofile-packages.site /tmp/Rprofile-packages.site
RUN cat /tmp/Rprofile-packages.site >> /usr/local/lib/R/etc/Rprofile.site && rm /tmp/Rprofile-packages.site

RUN useradd --uid 1000 --create-home analyst && mkdir -p /workspace && chown 1000:1000 /workspace

# Deployed programs download their source (from the control plane) into /app.
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/* \
 && mkdir -p /app && chown 1000:1000 /app
USER analyst
WORKDIR /workspace
