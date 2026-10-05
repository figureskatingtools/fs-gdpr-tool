param location string
param functionAppName string
param appServicePlanName string
param storageAccountName string
param deploymentContainerUrl string

// Shared secret the site router proxy sends as X-Proxy-Secret. Empty = the
// function doesn't enforce it (local/dev). See auth.proxy_secret_ok and
// PROXY-CONTRACT.md.
@secure()
param proxySharedSecret string = ''

param allowedResultHosts string
param allowedUserEmails string = ''

resource appServicePlan 'Microsoft.Web/serverfarms@2023-12-01' = {
  name: appServicePlanName
  location: location
  sku: {
    name: 'FC1'
    tier: 'FlexConsumption'
  }
  properties: {
    reserved: true
  }
}

// No Application Insights on purpose: request telemetry would carry nothing but
// route names, but the tool's privacy promise is simplest kept by having no
// telemetry sink at all.
resource functionApp 'Microsoft.Web/sites@2023-12-01' = {
  name: functionAppName
  location: location
  kind: 'functionapp,linux'
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    serverFarmId: appServicePlan.id
    httpsOnly: true
    siteConfig: {
      cors: {
        // Only the site router (server-to-server) ever calls this app.
        allowedOrigins: []
      }
      // Explicitly no inbound IP restrictions: the site router proxy and the CI
      // deploy's sync-triggers must reach the endpoint.
      ipSecurityRestrictions: []
      ipSecurityRestrictionsDefaultAction: 'Allow'
      appSettings: [
        {
          name: 'AzureWebJobsStorage__accountName'
          value: storageAccountName
        }
        {
          name: 'PROXY_SHARED_SECRET'
          value: proxySharedSecret
        }
        {
          name: 'ALLOWED_RESULT_HOSTS'
          value: allowedResultHosts
        }
        {
          name: 'ALLOWED_USER_EMAILS'
          value: allowedUserEmails
        }
      ]
    }
    functionAppConfig: {
      deployment: {
        storage: {
          type: 'blobContainer'
          value: deploymentContainerUrl
          authentication: {
            type: 'SystemAssignedIdentity'
          }
        }
      }
      runtime: {
        name: 'python'
        version: '3.13'
      }
      scaleAndConcurrency: {
        maximumInstanceCount: 10
        instanceMemoryMB: 2048
      }
    }
  }
}

resource authSettings 'Microsoft.Web/sites/config@2022-03-01' = {
  parent: functionApp
  name: 'authsettingsV2'
  properties: {
    // The site router (figureskatingtools.com) handles the real Entra login and
    // proxies requests here with the user's email plus the shared secret. No
    // identity provider is registered here — this app is never browsed to.
    globalValidation: {
      requireAuthentication: false
      unauthenticatedClientAction: 'AllowAnonymous'
    }
    login: {
      tokenStore: {
        enabled: false
      }
    }
  }
}

output functionAppName string = functionApp.name
output functionAppId string = functionApp.id
output functionPrincipalId string = functionApp.identity.principalId
