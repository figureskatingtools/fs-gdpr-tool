targetScope = 'subscription'

// Backend-only deployment of the GDPR removal tool.
//
// The UI lives in the figureskatingtools-site repo, served at
// https://figureskatingtools.com/tools/gdpr/, whose router proxies
// /tools/gdpr/api/* to this Function App. This template deploys only the
// Function App and the storage account its host needs (host state + the
// deployment package). There is deliberately no data container, no table and
// no Application Insights: the tool keeps nothing between requests and must
// not log competition data anywhere.

param location string = 'swedencentral'
param resourceGroupName string = ''

// Shared secret between the site router proxy and the Function App
// (see function.bicep / auth.proxy_secret_ok and PROXY-CONTRACT.md).
@secure()
param proxySharedSecret string = ''

// Hosts the backend may fetch results from (comma-separated). Everything else
// is refused, including across redirects.
param allowedResultHosts string = 'www.figureskatingresults.fi,figureskatingresults.fi,fs.lintuala.fi'

// Optional comma-separated list of user emails allowed to use the tool. Empty =
// everyone who passes the site's Entra login.
param allowedUserEmails string = ''

resource rg 'Microsoft.Resources/resourceGroups@2021-04-01' = {
  name: resourceGroupName
  location: location
}

module storage 'modules/storage.bicep' = {
  scope: rg
  name: 'storageDeployment'
  params: {
    location: location
    storageAccountName: 'stfsgdpr${uniqueString(rg.id)}'
  }
}

module function 'modules/function.bicep' = {
  scope: rg
  name: 'functionDeployment'
  params: {
    location: location
    functionAppName: 'func-fs-gdpr-${uniqueString(rg.id)}'
    appServicePlanName: 'asp-fs-gdpr'
    storageAccountName: storage.outputs.storageAccountName
    deploymentContainerUrl: 'https://${storage.outputs.storageAccountName}.blob.${environment().suffixes.storage}/app-package'
    proxySharedSecret: proxySharedSecret
    allowedResultHosts: allowedResultHosts
    allowedUserEmails: allowedUserEmails
  }
}

module roleAssignment 'modules/roleassignment.bicep' = {
  scope: rg
  name: 'roleAssignmentDeployment'
  params: {
    storageAccountName: storage.outputs.storageAccountName
    functionPrincipalId: function.outputs.functionPrincipalId
  }
}

output resourceGroupName string = rg.name
output storageAccountName string = storage.outputs.storageAccountName
output functionAppName string = function.outputs.functionAppName
output functionPrincipalId string = function.outputs.functionPrincipalId
