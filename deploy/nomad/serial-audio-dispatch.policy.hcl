# Held by the serial_audio web app on containers
# (homelab-nomad-serial-audio-dispatch-token in vaultwarden). It dispatches the
# serial-audio-* jobs in the serial-audio namespace, polls the dispatched
# children and reads their task logs, and reads the node list to see which
# nodes can take a job. It cannot register, change or stop a job.
namespace "serial-audio" {
  capabilities = ["list-jobs", "read-job", "dispatch-job", "read-logs"]
}

node {
  policy = "read"
}
