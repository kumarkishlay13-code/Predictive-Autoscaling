terraform {
  required_version = ">= 1.6"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }

  # State is kept locally (terraform.tfstate, git-ignored). For a shared setup, configure an
  # azurerm backend here instead.
}

provider "azurerm" {
  features {}
  subscription_id = var.subscription_id
}
