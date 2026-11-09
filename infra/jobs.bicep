// The three scheduled jobs, deployed by a v* tag's workflow, or by hand with `scripts/deploy.sh jobs IMAGE`.
// base.bicep must be deployed first.

param image string
param storageAccountName string
param location string = resourceGroup().location

resource writer 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' existing = {
  name: 'galaxy-sync-backup-writer'
}

resource reader 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' existing = {
  name: 'galaxy-sync-backup-reader'
}

resource appEnvironment 'Microsoft.App/managedEnvironments@2024-03-01' existing = {
  name: 'galaxy-sync-backup'
}

// Built rather than read, as the deployer can't read the vault.
var vaultSecrets = 'https://gsb${uniqueString(resourceGroup().id)}${environment().suffixes.keyvaultDns}/secrets'

var azEnv = [
  { name: 'RCLONE_CONFIG_AZ_TYPE', value: 'azureblob' }
  { name: 'RCLONE_CONFIG_AZ_ACCOUNT', value: storageAccountName }
  { name: 'RCLONE_CONFIG_AZ_ENV_AUTH', value: 'true' }
  { name: 'RCLONE_CONFIG_AZ_NO_CHECK_CONTAINER', value: 'true' }
]
var r2Secrets = [for s in ['r2-endpoint', 'r2-access-key-id', 'r2-secret-access-key']: {
  name: s
  keyVaultUrl: '${vaultSecrets}/${s}'
  identity: writer.id
}]
var r2Env = [
  { name: 'RCLONE_CONFIG_R2_TYPE', value: 's3' }
  { name: 'RCLONE_CONFIG_R2_PROVIDER', value: 'Cloudflare' }
  { name: 'RCLONE_CONFIG_R2_ENDPOINT', secretRef: 'r2-endpoint' }
  { name: 'RCLONE_CONFIG_R2_ACCESS_KEY_ID', secretRef: 'r2-access-key-id' }
  { name: 'RCLONE_CONFIG_R2_SECRET_ACCESS_KEY', secretRef: 'r2-secret-access-key' }
]

// UTC. The first copy moves ~25 GB, and one that runs out of time carries on the next day. verify and restore-test
// start after copy's timeout, as they read the snapshot copy takes before it copies the files it names. Disk space
// grows with the CPU asked for, which verify and restore-test need.
var jobs = [
  { name: 'copy', cron: '17 3 * * *', timeout: 14400, cpu: '1.0', memory: '2Gi', writes: true }
  { name: 'verify', cron: '17 8 * * 0', timeout: 7200, cpu: '1.0', memory: '2Gi', writes: false }
  { name: 'restore-test', cron: '17 9 1 1,4,7,10 *', timeout: 7200, cpu: '2.0', memory: '4Gi', writes: false }
]

resource job 'Microsoft.App/jobs@2024-03-01' = [for j in jobs: {
  name: 'backup-${j.name}'
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${j.writes ? writer.id : reader.id}': {} }
  }
  properties: {
    environmentId: appEnvironment.id
    workloadProfileName: 'Consumption'
    configuration: {
      triggerType: 'Schedule'
      replicaTimeout: j.timeout
      replicaRetryLimit: 0 // the next scheduled run is the retry
      scheduleTriggerConfig: {
        cronExpression: j.cron
        parallelism: 1
        replicaCompletionCount: 1
      }
      secrets: concat(j.writes ? r2Secrets : [], [
        { name: 'heartbeat-url', keyVaultUrl: '${vaultSecrets}/heartbeat-${j.name}-url', identity: j.writes ? writer.id : reader.id }
      ])
    }
    template: {
      containers: [
        {
          name: 'backup'
          image: image
          args: [j.name]
          resources: { cpu: json(j.cpu), memory: j.memory }
          env: concat(
            azEnv,
            [{ name: 'AZURE_CLIENT_ID', value: j.writes ? writer.properties.clientId : reader.properties.clientId }],
            j.writes ? r2Env : [],
            [{ name: 'HEARTBEAT_URL', secretRef: 'heartbeat-url' }]
          )
        }
      ]
    }
  }
}]
