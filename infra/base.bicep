// Everything but the jobs, deployed by hand as Owner with `scripts/deploy.sh base`. The jobs are in jobs.bicep,
// which a v* tag deploys as the deployer identity below.

param location string = resourceGroup().location
param storageAccountName string
param githubOwner string = 'alai-sh'
param githubRepository string = 'galaxy-sync-backup'
// The IDs in GitHub's immutable default subject, so a repo made later under the same name can't sign in.
param githubOwnerId string
param githubRepositoryId string
param githubEnvironment string = 'azure'

@secure()
param r2Endpoint string
@secure()
param r2AccessKeyId string
@secure()
param r2SecretAccessKey string
@secure()
param heartbeatCopyUrl string
@secure()
param heartbeatVerifyUrl string
@secure()
param heartbeatRestoreUrl string

resource noDelete 'Microsoft.Authorization/locks@2020-05-01' = {
  name: 'no-delete'
  properties: {
    level: 'CanNotDelete'
    notes: 'The galaxy-sync-backup jobs and storage. Remove this lock to delete anything here.'
  }
}

// LRS, as the backup is already the second copy.
resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageAccountName
  location: location
  kind: 'StorageV2'
  sku: { name: 'Standard_LRS' }
  properties: {
    accessTier: 'Cool'
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    defaultToOAuthAuthentication: true
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: storage
  name: 'default'
  properties: {
    deleteRetentionPolicy: { enabled: true, days: 14 }
    containerDeleteRetentionPolicy: { enabled: true, days: 30 }
    lastAccessTimeTrackingPolicy: {
      enable: true
      name: 'AccessTimeTracking'
      trackingGranularityInDays: 1
      blobType: ['blockBlob']
    }
  }
}

resource containers 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = [for name in ['backup', 'snapshots']: {
  parent: blobService
  name: name
  properties: { publicAccess: 'None' }
}]

resource lifecycle 'Microsoft.Storage/storageAccounts/managementPolicies@2023-05-01' = {
  parent: storage
  name: 'default'
  properties: {
    policy: {
      rules: [
        {
          name: 'expire-snapshots'
          enabled: true
          type: 'Lifecycle'
          definition: {
            filters: { blobTypes: ['blockBlob'], prefixMatch: ['snapshots/'] }
            actions: { baseBlob: { delete: { daysAfterCreationGreaterThan: 90 } } }
          }
        }
        {
          // verify reads every current index file weekly, so only stale ones go unread. 180 days is long past the
          // 90-day snapshots that name them, and copy re-uploads a current one from R2 if verify ever stops.
          name: 'expire-stale-index-files'
          enabled: true
          type: 'Lifecycle'
          definition: {
            filters: { blobTypes: ['blockBlob'], prefixMatch: ['backup/alai-index/index/'] }
            actions: { baseBlob: { delete: { daysAfterLastAccessTimeGreaterThan: 180 } } }
          }
        }
      ]
    }
  }
}

// Only copy writes, with a role that can't delete.
resource writer 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'galaxy-sync-backup-writer'
  location: location
}

resource reader 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'galaxy-sync-backup-reader'
  location: location
}

resource writerRole 'Microsoft.Authorization/roleDefinitions@2022-04-01' = {
  name: guid(resourceGroup().id, 'galaxy-sync-backup-writer')
  properties: {
    roleName: 'galaxy-sync-backup writer (${resourceGroup().name})'
    description: 'Read, list and add blobs, without permission to delete them.'
    type: 'CustomRole'
    permissions: [
      {
        actions: []
        notActions: []
        dataActions: [
          'Microsoft.Storage/storageAccounts/blobServices/containers/blobs/read'
          'Microsoft.Storage/storageAccounts/blobServices/containers/blobs/write'
          'Microsoft.Storage/storageAccounts/blobServices/containers/blobs/add/action'
        ]
        notDataActions: []
      }
    ]
    assignableScopes: [resourceGroup().id]
  }
}

resource writerAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, writer.id, 'writer')
  scope: storage
  properties: {
    roleDefinitionId: writerRole.id
    principalId: writer.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource readerAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, reader.id, 'reader')
  scope: storage
  properties: {
    // Storage Blob Data Reader
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '2a2b9908-6ea1-4ae2-8e65-a410df84e7d1')
    principalId: reader.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// The jobs' secrets live here rather than in GitHub, so a tag deploys the jobs without ever seeing them.
resource vault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: 'gsb${uniqueString(resourceGroup().id)}' // jobs.bicep derives the same name
  location: location
  properties: {
    tenantId: subscription().tenantId
    sku: { family: 'A', name: 'standard' }
    enableRbacAuthorization: true
  }
}

var secrets = {
  'r2-endpoint': r2Endpoint
  'r2-access-key-id': r2AccessKeyId
  'r2-secret-access-key': r2SecretAccessKey
  'heartbeat-copy-url': heartbeatCopyUrl
  'heartbeat-verify-url': heartbeatVerifyUrl
  'heartbeat-restore-test-url': heartbeatRestoreUrl
}

resource vaultSecrets 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = [for s in items(secrets): {
  parent: vault
  name: s.key
  properties: { value: s.value }
}]

var secretsUser = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '4633458b-17de-408a-b874-0445c86b69e6')

resource writerVaultAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(vault.id, writer.id, 'secrets')
  scope: vault
  properties: {
    roleDefinitionId: secretsUser
    principalId: writer.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource readerVaultAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(vault.id, reader.id, 'secrets')
  scope: vault
  properties: {
    roleDefinitionId: secretsUser
    principalId: reader.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: 'galaxy-sync-backup'
  location: location
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
  }
}

resource appEnvironment 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: 'galaxy-sync-backup'
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logs.properties.customerId
        sharedKey: logs.listKeys().primarySharedKey
      }
    }
    workloadProfiles: [{ name: 'Consumption', workloadProfileType: 'Consumption' }]
  }
}

// What a v* tag's workflow signs in as, through GitHub's OIDC token for the environment that requires approval.
resource deployer 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'galaxy-sync-backup-deployer'
  location: location
}

resource githubFederation 'Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials@2023-01-31' = {
  parent: deployer
  name: 'github-${githubEnvironment}'
  properties: {
    issuer: 'https://token.actions.githubusercontent.com'
    subject: 'repo:${githubOwner}@${githubOwnerId}/${githubRepository}@${githubRepositoryId}:environment:${githubEnvironment}'
    audiences: ['api://AzureADTokenExchange']
  }
}

// Only the jobs: nothing here can read or change the storage, the vault, the roles or the lock.
resource deployerRole 'Microsoft.Authorization/roleDefinitions@2022-04-01' = {
  name: guid(resourceGroup().id, 'galaxy-sync-backup-deployer')
  properties: {
    roleName: 'galaxy-sync-backup deployer (${resourceGroup().name})'
    description: 'Deploy the galaxy-sync-backup jobs, and nothing else.'
    type: 'CustomRole'
    permissions: [
      {
        actions: [
          'Microsoft.Resources/deployments/*'
          'Microsoft.Resources/subscriptions/resourceGroups/read'
          'Microsoft.App/jobs/read'
          'Microsoft.App/jobs/write'
          'Microsoft.App/managedEnvironments/read'
          'Microsoft.App/managedEnvironments/join/action'
          'Microsoft.App/locations/*/read'
          'Microsoft.ManagedIdentity/userAssignedIdentities/read'
          'Microsoft.ManagedIdentity/userAssignedIdentities/assign/action'
        ]
        notActions: []
        dataActions: []
        notDataActions: []
      }
    ]
    assignableScopes: [resourceGroup().id]
  }
}

resource deployerAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(resourceGroup().id, deployer.id, 'deployer')
  properties: {
    roleDefinitionId: deployerRole.id
    principalId: deployer.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

output deployerClientId string = deployer.properties.clientId
output tenantId string = subscription().tenantId
output subscriptionId string = subscription().subscriptionId
