#!/bin/bash
set -e

LOCATION="swedencentral"
TEMPLATE_FILE="infra/main.bicep"

# Backend-only infrastructure (host storage + Function App). The UI is served
# by the figureskatingtools-site repo at /tools/gdpr/.

PROXY_SECRET=""
RESOURCE_GROUP=""

while [[ "$#" -gt 0 ]]; do
    case $1 in
        -s|--proxy-secret) PROXY_SECRET="$2"; shift ;;
        -g|--resource-group) RESOURCE_GROUP="$2"; shift ;;
        *) echo "Unknown parameter passed: $1"; exit 1 ;;
    esac
    shift
done

if [ -z "$RESOURCE_GROUP" ]; then
    echo "Error: --resource-group (-g) is required (rg-fs-gdpr-test or rg-fs-gdpr-prod)."
    exit 1
fi

# NB: the Bicep appSettings array is authoritative, so omitting --proxy-secret
# sets the Function App's PROXY_SHARED_SECRET to empty, disabling the proxy gate
# until the next CI deploy (which injects it from the GitHub environment secret).
if [ -z "$PROXY_SECRET" ]; then
    echo "Warning: --proxy-secret not provided; the router -> Function proxy gate will be disabled until the next CI deploy."
fi

echo "Deploying infrastructure to subscription scope in $LOCATION..."
az deployment sub create \
  --location "$LOCATION" \
  --template-file "$TEMPLATE_FILE" \
  --name "deploy-fs-gdpr-$(date +%s)" \
  --parameters resourceGroupName="$RESOURCE_GROUP" proxySharedSecret="$PROXY_SECRET"

echo "Infrastructure deployment complete."
