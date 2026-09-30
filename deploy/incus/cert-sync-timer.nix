# Re-run the NRP Temporal cert sync every six hours, between rotations.
#
# New in v2; fishsense-lite had only the two triggers the one-shot still has:
#   1. `temporal.reload` (flake.nix) restarts it when vault-agent re-renders the
#      leaf -- the rotation itself;
#   2. every converge starts it.
# Both fire ONCE. If that one run fails -- NRP's apiserver is down, the
# kubeconfig's token has just expired, the network blips -- nothing runs it
# again until the next rotation, ~5 days later, while the leaf the processor's
# pods hold expires in 2 (a 7-day leaf renewed at ~5 days). That is the
# 2026-08-14 outage again (every pod crash-looping on `CertificateExpired`),
# reached through a transient failure instead of a missing forwarder.
#
# So this re-runs it every six hours: at most six hours after NRP is reachable
# again, the Secret holds the current leaf and every processor Deployment that
# is up has been rolled onto it. The sync is a no-op when the leaf is unchanged
# and every pod is already on it (ops/cert_sync.py), so a healthy run costs two
# GETs per Deployment.
#
# `docker start --attach` re-runs the exited compose container with its own
# configuration (compose.yml gives it this container_name; deploy/tests pins the
# pairing), so there is no second copy of its settings here, and its output
# lands in this unit's journal. A failed run fails the unit, visibly, until the
# next one succeeds. Not `wantedBy` the stack: the timer is independent of
# converges, which already run the sync themselves.
{config, ...}: {
  systemd.services.fishsense-nrp-cert-sync = {
    description = "Forward the rotated Temporal leaf to the NRP processor (re-run)";
    after = ["fishsense.service"];
    serviceConfig = {
      Type = "oneshot";
      ExecStart = "${config.virtualisation.docker.package}/bin/docker start --attach fishsense-nrp-temporal-cert-sync";
    };
  };
  systemd.timers.fishsense-nrp-cert-sync = {
    wantedBy = ["timers.target"];
    timerConfig = {
      OnCalendar = "*-*-* 00/6:17:00";
      # A slot that was off at a firing runs it at boot instead.
      Persistent = true;
      RandomizedDelaySec = "5m";
    };
  };
}
