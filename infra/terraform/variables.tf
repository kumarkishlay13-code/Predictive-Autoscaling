variable "subscription_id" {
  description = "Azure subscription ID (az account show --query id -o tsv)."
  type        = string
}

variable "location" {
  description = "Azure region. Check vCPU quota there first: az vm list-usage -l <region> -o table"
  type        = string
  default     = "centralindia"
}

variable "prefix" {
  description = "Short name used for every resource."
  type        = string
  default     = "pas"

  validation {
    condition     = can(regex("^[a-z][a-z0-9]{1,9}$", var.prefix))
    error_message = "prefix must be 2-10 lowercase letters/digits, starting with a letter."
  }
}

variable "kubernetes_version" {
  description = "AKS Kubernetes version; null picks the region default."
  type        = string
  default     = null
}

variable "system_vm_size" {
  description = "System pool: CoreDNS, KEDA, Prometheus, Grafana, forecaster."
  type        = string
  default     = "Standard_D2s_v5"
}

variable "app_vm_size" {
  description = "App pool(s): demo-api pods only."
  type        = string
  default     = "Standard_D2s_v5"
}

variable "app_node_count" {
  description = "Fixed size of the app pool (Cluster Autoscaler stays off for the main study)."
  type        = number
  default     = 3
}

variable "second_lane_node_count" {
  description = "Nodes in the second app pool 'appb' for parallel lanes. 0 = do not create it."
  type        = number
  default     = 0
}

variable "loadgen_vm_size" {
  description = "Load-generator pool: k6 only."
  type        = string
  default     = "Standard_D2s_v5"
}

variable "budget_amount" {
  description = "Monthly budget for the resource group, in the billing currency. 0 = no budget alert."
  type        = number
  default     = 0
}

variable "budget_emails" {
  description = "Who gets the 50/80/100% budget alerts."
  type        = list(string)
  default     = []
}

variable "budget_start_date" {
  description = "First day of the current month, e.g. 2026-10-01T00:00:00Z."
  type        = string
  default     = "2026-10-01T00:00:00Z"
}

variable "tags" {
  description = "Extra tags for every resource."
  type        = map(string)
  default     = {}
}
