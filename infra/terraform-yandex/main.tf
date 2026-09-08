# Yandex Cloud развёртывание RAG2 (Фаза 3)
# Развертывает: сеть, Managed K8s, Managed PostgreSQL, Object Storage, GPU node group.
# Требует: yc CLI (инициализированный), terraform >= 1.9

terraform {
  required_providers {
    yandex = { source = "yandex-cloud/yandex", version = "~> 0.120" }
  }
}

provider "yandex" {
  cloud_id  = var.cloud_id
  folder_id = var.folder_id
  zone      = "ru-central1-a"
}

variable "cloud_id" { type = string }
variable "folder_id" { type = string }
variable "k8s_version" { default = "1.29" }
variable "gpu_nodes" { default = 2 }

# --- Сеть с сегментацией (DMZ / Internal / Data) ---

resource "yandex_vpc_network" "rag2" { name = "rag2-net" }

resource "yandex_vpc_subnet" "dmz" {
  name           = "dmz"
  zone           = "ru-central1-a"
  network_id     = yandex_vpc_network.rag2.id
  v4_cidr_blocks = ["10.10.1.0/24"]
}

resource "yandex_vpc_subnet" "internal" {
  name           = "internal"
  zone           = "ru-central1-a"
  network_id     = yandex_vpc_network.rag2.id
  v4_cidr_blocks = ["10.10.2.0/24"]
}

resource "yandex_vpc_subnet" "data" {
  name           = "data"
  zone           = "ru-central1-a"
  network_id     = yandex_vpc_network.rag2.id
  v4_cidr_blocks = ["10.10.3.0/24"]
}

# --- Managed Kubernetes (Control Plane + приложения) ---

resource "yandex_kubernetes_cluster" "rag2" {
  name       = "rag2-k8s"
  network_id = yandex_vpc_network.rag2.id

  master {
    version = var.k8s_version
    regional {
      region = "ru-central1"
      master_location {
        zone      = "ru-central1-a"
        subnet_id = yandex_vpc_subnet.internal.id
      }
    }
    public_ip = true
  }

  service_account_id      = yandex_iam_service_account.k8s.id
  node_service_account_id = yandex_iam_service_account.k8s.id
}

# GPU node group для vLLM / embeddings
resource "yandex_kubernetes_node_group" "gpu" {
  cluster_id = yandex_kubernetes_cluster.rag2.id
  name       = "gpu-nodes"
  instance_template {
    resources {
      memory = 256
      cores  = 28
    }
    gpus = 1
    # H100/NVIDIA GPU preset — уточняется по availability
  }
  scale_policy {
    fixed_scale { size = var.gpu_nodes }
  }
}

# --- Managed PostgreSQL (Data Plane) ---

resource "yandex_mdb_postgresql_cluster" "rag2" {
  name        = "rag2-pg"
  environment = "PRODUCTION"
  network_id  = yandex_vpc_network.rag2.id

  config {
    version = "16"
    resources {
      resource_preset_id = "s3-c2-m8"
      disk_size          = 100
      disk_type_id       = "network-ssd"
    }
  }

  host {
    zone      = "ru-central1-a"
    subnet_id = yandex_vpc_subnet.data.id
  }
}

# --- Object Storage (вместо MinIO) ---

resource "yandex_storage_bucket" "documents" {
  name       = "rag2-documents"
  access_key = yandex_iam_service_account_static_access_key.s3.access_key
  secret_key = yandex_iam_service_account_static_access_key.s3.secret_key
  versioning { enabled = true }
}

resource "yandex_storage_bucket" "audio" {
  name       = "rag2-audio"
  access_key = yandex_iam_service_account_static_access_key.s3.access_key
  secret_key = yandex_iam_service_account_static_access_key.s3.secret_key
}

# --- Lockbox (вместо Vault) ---

resource "yandex_lockbox_secret" "rag2" {
  name = "rag2-secrets"
}

# --- Service accounts ---

resource "yandex_iam_service_account" "k8s" { name = "rag2-k8s-sa" }
resource "yandex_iam_service_account" "s3" { name = "rag2-s3-sa" }

resource "yandex_iam_service_account_static_access_key" "s3" {
  service_account_id = yandex_iam_service_account.s3.id
}

output "k8s_endpoint" {
  value = yandex_kubernetes_cluster.rag2.master[0].external_v4_endpoint
}
