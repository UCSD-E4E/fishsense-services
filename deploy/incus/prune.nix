# Reclaim unused docker images after each compose-stack converge.
#
# Kept verbatim from fishsense-lite deploy/incus/prune.nix: the same slot, the
# same disk, and v2 pulls more images per release than v1 did (api,
# orchestrator -- also the cert sync's and the smoke test's -- backup, web).
#
# The slot's root disk is only 20G, and the composeStack force-recreates on
# every converge — pulling each release's new image tag while leaving the old
# ones resident. Without cleanup that steadily fills the disk: we hit 100% on
# 2026-07-16 (postgres `FATAL: could not write init file: No space left on
# device`, superset_worker crash-loop, and every *new* DB connection at risk),
# with ~5.4G of stale image versions reclaimable.
#
# This runs `docker image prune -af` ordered AFTER `fishsense.service` (the
# compose stack) and pulled in whenever it starts — so the reclaim happens as
# part of the deploy, right after `up -d` has the new containers running (their
# images in-use and therefore kept; only the superseded versions are removed).
#
# Deliberately `wantedBy` (not `requiredBy`) and no `bindsTo`: a prune failure
# must never fail or block the stack — worst case the disk isn't reclaimed and
# we notice next time.
{config, ...}: {
  systemd.services.fishsense-image-prune = {
    description = "Reclaim unused docker images after the compose-stack converge";
    after = ["fishsense.service"];
    wantedBy = ["fishsense.service"];
    serviceConfig = {
      Type = "oneshot";
      ExecStart = "${config.virtualisation.docker.package}/bin/docker image prune -af";
    };
  };
}
