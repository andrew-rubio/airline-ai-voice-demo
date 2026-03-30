param foundryAccountName string
param projectName string
param location string
param tags object = {}

resource foundryAccount 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = {
  name: foundryAccountName
}

resource foundryProject 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' = {
  name: projectName
  parent: foundryAccount
  location: location
  identity: {
    type: 'SystemAssigned'
  }
  tags: tags
  properties: {
    displayName: projectName
    description: 'easyJet Call Center Voice Agent Project'
  }
}

output projectId string = foundryProject.id
output projectName string = projectName
output projectEndpoint string = '${foundryAccount.properties.endpoint}/projects/${projectName}'
