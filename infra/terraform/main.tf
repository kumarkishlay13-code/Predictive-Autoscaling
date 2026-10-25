locals {
  tags = merge({ project = "predictive-autoscaling", managed_by = "terraform" }, var.tags)
}

resource "random_string" "suffix" {
  length  = 6
  special = false
  upper   = false
}

resource "azurerm_resource_group" "rg" {
  name     = "rg-${var.prefix}"
  location = var.location
  tags     = local.tags
}

# Images are built in Azure with `az acr build`, so no local Docker is needed.
resource "azurerm_container_registry" "acr" {
  name                = "${var.prefix}acr${random_string.suffix.result}"
  resource_group_name = azurerm_resource_group.rg.name
  location            = azurerm_resource_group.rg.location
  sku                 = "Basic"
  admin_enabled       = false
  tags                = local.tags
}

resource "azurerm_kubernetes_cluster" "aks" {
  name                = "aks-${var.prefix}"
  location            = azurerm_resource_group.rg.location
  resource_group_name = azurerm_resource_group.rg.name
  dns_prefix          = "aks-${var.prefix}"
  kubernetes_version  = var.kubernetes_version
  sku_tier            = "Free"

  default_node_pool {
    name        = "system"
    vm_size     = var.system_vm_size
    node_count  = 1
    node_labels = { "pas.dev/pool" = "system" } # same label as the local kind cluster

    upgrade_settings {
      max_surge = "10%"
    }
  }

  identity {
    type = "SystemAssigned"
  }

  network_profile {
    network_plugin      = "azure"
    network_plugin_mode = "overlay"
  }

  # KEDA add-on: AKS installs and upgrades KEDA for us.
  workload_autoscaler_profile {
    keda_enabled = true
  }

  # Container Insights / Log Analytics are deliberately not enabled: they cost money and
  # Prometheus covers every metric the study needs.

  tags = local.tags
}

# Fixed-size pools with a label and a matching taint, so only the intended pods land there.
resource "azurerm_kubernetes_cluster_node_pool" "app" {
  name                  = "app"
  kubernetes_cluster_id = azurerm_kubernetes_cluster.aks.id
  vm_size               = var.app_vm_size
  node_count            = var.app_node_count
  mode                  = "User"
  node_labels           = { "pas.dev/pool" = "app" }
  node_taints           = ["pas.dev/pool=app:NoSchedule"]
  tags                  = local.tags

  upgrade_settings {
    max_surge = "10%"
  }
}

resource "azurerm_kubernetes_cluster_node_pool" "appb" {
  count                 = var.second_lane_node_count > 0 ? 1 : 0
  name                  = "appb"
  kubernetes_cluster_id = azurerm_kubernetes_cluster.aks.id
  vm_size               = var.app_vm_size
  node_count            = var.second_lane_node_count
  mode                  = "User"
  node_labels           = { "pas.dev/pool" = "appb" }
  node_taints           = ["pas.dev/pool=appb:NoSchedule"]
  tags                  = local.tags

  upgrade_settings {
    max_surge = "10%"
  }
}

resource "azurerm_kubernetes_cluster_node_pool" "loadgen" {
  name                  = "loadgen"
  kubernetes_cluster_id = azurerm_kubernetes_cluster.aks.id
  vm_size               = var.loadgen_vm_size
  node_count            = 1
  mode                  = "User"
  node_labels           = { "pas.dev/pool" = "loadgen" }
  node_taints           = ["pas.dev/pool=loadgen:NoSchedule"]
  tags                  = local.tags

  upgrade_settings {
    max_surge = "10%"
  }
}

resource "azurerm_role_assignment" "aks_acr_pull" {
  scope                            = azurerm_container_registry.acr.id
  role_definition_name             = "AcrPull"
  principal_id                     = azurerm_kubernetes_cluster.aks.kubelet_identity[0].object_id
  skip_service_principal_aad_check = true
}

resource "azurerm_consumption_budget_resource_group" "budget" {
  count             = var.budget_amount > 0 && length(var.budget_emails) > 0 ? 1 : 0
  name              = "budget-${var.prefix}"
  resource_group_id = azurerm_resource_group.rg.id
  amount            = var.budget_amount
  time_grain        = "Monthly"

  time_period {
    start_date = var.budget_start_date
  }

  dynamic "notification" {
    for_each = [50, 80, 100]
    content {
      enabled        = true
      threshold      = notification.value
      operator       = "GreaterThanOrEqualTo"
      threshold_type = "Actual"
      contact_emails = var.budget_emails
    }
  }
}
