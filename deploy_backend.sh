#!/bin/bash
set -e

RESOURCE_GROUP=""
while [[ "$#" -gt 0 ]]; do
    case $1 in
        -g|--resource-group) RESOURCE_GROUP="$2"; shift ;;
        *) echo "Unknown parameter passed: $1"; exit 1 ;;
    esac
    shift
done

if [ -z "$RESOURCE_GROUP" ]; then
    echo "Error: --resource-group (-g) is required."
    echo "Usage: ./deploy_backend.sh --resource-group <resource-group-name>"
    exit 1
fi

cd infra/functions

echo "Running tests..."
python -m pytest tests -q

BUILD_DIR="../../backend_build"
echo "Preparing build in $BUILD_DIR..."
rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR"
cp -r . "$BUILD_DIR"
cd "$BUILD_DIR"

rm -rf .venv __pycache__ .pytest_cache .git .vscode *.pyc local.settings.json tests requirements-dev.txt

echo "Installing dependencies to .python_packages..."
mkdir -p .python_packages/lib/site-packages
pip install -r requirements.txt --only-binary=:all: --target .python_packages/lib/site-packages

echo "Creating backend.zip..."
zip -r ../infra/backend.zip .
cd ..

FUNC_APP_NAME=$(az functionapp list --resource-group "$RESOURCE_GROUP" --query "[?contains(name, 'func-fs-gdpr')].name" -o tsv | head -n 1)
if [ -z "$FUNC_APP_NAME" ]; then
    echo "Error: Could not find a Function App matching 'func-fs-gdpr' in $RESOURCE_GROUP."
    exit 1
fi

echo "Deploying to Function App: $FUNC_APP_NAME (RG: $RESOURCE_GROUP)"
az functionapp deployment source config-zip -g "$RESOURCE_GROUP" -n "$FUNC_APP_NAME" --src infra/backend.zip
rm -rf backend_build infra/backend.zip

echo "Backend deployment complete."
