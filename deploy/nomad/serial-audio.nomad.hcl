# One serial_audio web app job on the cluster. The app dispatches it with
# -meta job=<app job id> -meta key=<per-job key>. bootstrap.py, embedded below,
# fetches the job's bundle from the app and runs the CLI steps it names.
# Register one job per class from the repo root, with the management token:
#   nomad namespace apply -description "serial_audio web app jobs" serial-audio
#   nomad job run -var class=breeze  deploy/nomad/serial-audio.nomad.hcl
#   nomad job run -var class=comfyui deploy/nomad/serial-audio.nomad.hcl
#   nomad job run -var class=llm     deploy/nomad/serial-audio.nomad.hcl
# The app picks the node for a GPU job itself and dispatches a copy pinned to
# it, so register one per GPU class each GPU node offers:
#   nomad job run -var class=breeze -var node=aj-legion5 deploy/nomad/serial-audio.nomad.hcl
# A node takes a class when its client meta `serial_audio` lists it, for
# example serial_audio = "breeze,comfyui", and names its Python in
# `serial_audio_python`. deploy/README.md has the node setup.
variable "class" {
  type        = string
  description = "breeze: Breeze renders through breeze-server. comfyui: anything that queues ComfyUI prompts or reads its output. llm: the Claude passes."
  validation {
    condition     = contains(["breeze", "comfyui", "llm"], var.class)
    error_message = "The class must be breeze, comfyui or llm."
  }
}

variable "app_url" {
  type        = string
  default     = "http://containers.goblin-truck.ts.net:8765"
  description = "The web app's address as the nodes reach it."
}

variable "prefix" {
  type        = string
  default     = "serial-audio"
  description = "Job ids are <prefix>-<class>. A test instance of the app registers its own prefix."
}

variable "node" {
  type        = string
  default     = ""
  description = "A node name pins the job to that node and appends -<node> to its id."
}

locals {
  gpu    = var.class != "llm"
  job_id = var.node == "" ? "${var.prefix}-${var.class}" : "${var.prefix}-${var.class}-${var.node}"
}

job "serial-audio" {
  id        = local.job_id
  namespace = "serial-audio"
  type      = "batch"

  parameterized {
    payload       = "forbidden"
    meta_required = ["job", "key"]
    meta_optional = ["stage"]
  }

  constraint {
    attribute = "${meta.serial_audio}"
    operator  = "set_contains"
    value     = var.class
  }
  constraint {
    attribute = "${meta.busy}"
    value     = "false"
  }
  dynamic "constraint" {
    for_each = var.node == "" ? [] : [var.node]
    content {
      attribute = "${node.unique.name}"
      value     = constraint.value
    }
  }
  # The Claude passes run aj's install of claude, so the task runs as aj on a
  # Linux client.
  dynamic "constraint" {
    for_each = var.class == "llm" ? ["linux"] : []
    content {
      attribute = "${attr.kernel.name}"
      value     = constraint.value
    }
  }

  # The app dispatches pinned copies for GPU work and picks the node itself.
  # This only steers the unpinned job, toward the laptop's faster Vulkan.
  dynamic "affinity" {
    for_each = var.class == "breeze" ? ["nvidia"] : []
    content {
      attribute = "${meta.gpu}"
      value     = affinity.value
      weight    = 100
    }
  }

  group "work" {
    # A failure is reported in the web app, which retries on request.
    restart {
      attempts = 0
    }
    reschedule {
      attempts  = 0
      unlimited = false
    }
    # A laptop that sleeps through part of a render picks it up again.
    disconnect {
      lost_after = "2h"
      replace    = false
      reconcile  = "keep_original"
    }

    # GPU jobs reserve the same static port. Nomad checks it per address, and
    # the nodes have an IPv4 and an IPv6 address, so it doesn't keep two apart.
    # The app's node choice and the worker's GPU lock do.
    dynamic "network" {
      for_each = local.gpu ? [19137] : []
      content {
        port "gpu" {
          static = network.value
        }
      }
    }

    task "work" {
      driver = "raw_exec"
      user   = var.class == "llm" ? "aj" : ""

      config {
        command = "${meta.serial_audio_python}"
        args    = ["-u", "local/bootstrap.py"]
      }

      template {
        data            = file("deploy/nomad/bootstrap.py")
        destination     = "local/bootstrap.py"
        left_delimiter  = "[[["
        right_delimiter = "]]]"
      }

      # The Claude passes sign in with the token in the Nomad variable
      # nomad/jobs/<job id>, item claude_oauth_token (deploy/README.md). A job
      # reads only the variable at its own id.
      dynamic "template" {
        for_each = var.class == "llm" ? ["claude"] : []
        content {
          data        = "CLAUDE_CODE_OAUTH_TOKEN={{ with nomadVar \"nomad/jobs/${local.job_id}\" }}{{ .claude_oauth_token }}{{ end }}\n"
          destination = "secrets/claude.env"
          env         = true
          perms       = "0400"
          # A pass that is running keeps the token it started with.
          change_mode          = "noop"
          error_on_missing_key = true
        }
      }

      env = merge({
        SERIAL_AUDIO_APP    = var.app_url
        SERIAL_AUDIO_NODE   = "${node.unique.name}"
        SERIAL_AUDIO_CONFIG = "${meta.serial_audio_config}"
        PYTHONUNBUFFERED    = "1"
        PYTHONIOENCODING    = "utf-8"
      }, var.class != "llm" ? {} : {
        HOME = "/home/aj"
        USER = "aj"
        PATH = "/home/aj/.local/bin:/home/aj/bin:/usr/local/bin:/usr/bin:/bin"
        # The worker refuses the job when the token did not arrive.
        SERIAL_AUDIO_CLAUDE_AUTH = "token"
      })

      resources {
        cpu    = local.gpu ? 4000 : 1000
        memory = local.gpu ? 4096 : 1024
      }

      kill_timeout = "30s"
    }
  }
}
