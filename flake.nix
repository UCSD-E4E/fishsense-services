# fishsense-services deploy target — INTERIOR half of the mkTenant contract (ADR 0020).
#
# The repo-root flake that `fishsense-selfupdate` builds on the fishsense Incus
# slot once the admin repoints it at the cutover (docs/cutover.md):
#   nixos-rebuild switch --flake github:UCSD-E4E/fishsense-services#fishsense
# Until then the slot builds fishsense-lite's flake, and NOTHING here converges
# it. This mirrors fishsense-lite's flake.nix: the same tenant, the same
# boundary, the same krg-infra pin -- only the interior (deploy/incus/) and the
# repo the runner is scoped to change, so the platform sees no difference at
# the switch.
{
  description = "fishsense-services — KRG Incus platform tenant `fishsense` (v2)";

  inputs = {
    # Tracks krg-infra `main`; the EXACT rev is pinned in `flake.lock`, not here.
    # That lock rev is our stable contract (ADR 0020 §5): every converge builds from
    # the committed lock, so `main` moving doesn't touch the slot until the lock is
    # advanced — the deliberate act being a merge to our `main`.
    #
    # AT CUTOVER the lock must carry the SAME rev as fishsense-lite's, so the
    # switch changes the interior and nothing of the platform's (docs/cutover.md
    # checks it; this commit copied v1's lock: krg-infra @ ec7e4b60). Both repos'
    # weekly bumps track krg-infra main, so they stay close; re-sync by hand if not.
    #
    # Advancing the pin is "Axis B" (krg-infra docs/tenant-updates.md) and it's OURS:
    # `nixpkgs.follows = "krg-infra/nixpkgs"`, so bumping krg-infra drags in new
    # nixpkgs (kernel / bash / openssl / CVE fixes). `.github/workflows/update-flake.yml`
    # does it weekly — `nix flake update krg-infra`, committed straight to `main` — and
    # the nightly `system.autoUpgrade` (#460) rolls it out. Skip it and the slot freezes
    # on old, unpatched packages. A new kernel needs a manual `incus restart` (allowReboot=false).
    #
    # v1's #496 note holds: the half of a platform fix that keeps a dead runner
    # from staying dead (`Restart = lib.mkForce "on-failure"` in
    # nix/modules/tenant.nix) only reaches the slot when this pin moves.
    krg-infra.url = "github:KastnerRG/krg-infra?dir=nix";
    nixpkgs.follows = "krg-infra/nixpkgs";
  };

  outputs = {
    self,
    krg-infra,
    nixpkgs,
  }: let
    system = "x86_64-linux";
    pkgs = nixpkgs.legacyPackages.${system};

    tenant = krg-infra.lib.mkTenant {
      name = "fishsense"; # Incus project + OpenBao role + runner scope (provisioned) — v1's
      zone = "e4e"; # fronted by the e4e-prod edge (*.e4e.ucsd.edu)
      hostname = "fishsense.e4e.ucsd.edu"; # apex CNAME (published; edge serves a prod LE cert)
      sso.group = "FishSense"; # AD group (per-route auth is in-app — deploy/incus/traefik-dynamic.yml)
      resources = {
        # v1's quota (krg-infra PR #436), unchanged: the boundary is terraform-owned
        # and the cutover asks the admin for nothing new here.
        cpu = 6;
        ram = "12GiB";
      };
      image = "krg-golden"; # slot boots from the hardened template (already applied)
      compose = ./deploy/incus/compose.yml; # YOUR interior — repo-owns-deploy
      # LOAD-BEARING: scopes the auto-provisioned runner (ADR 0022). ADMIN, AT
      # CUTOVER: re-scope the runner to this repo and repoint fishsense-selfupdate
      # at github:UCSD-E4E/fishsense-services#fishsense (docs/cutover.md). Until
      # both happen, deploy.yml here has no runner to land on -- by design.
      repo = "UCSD-E4E/fishsense-services";
      # In-VM vault-agent renders a `fishsense-worker` Temporal client cert to
      # /run/tenant/temporal/{tls.crt,tls.key,ca.crt} (ADR 0023 / krg-infra #435).
      # Required — without it no cert renders and the workers can't reach krg-prod Temporal.
      #
      # `reload` is the tenant half of ROTATION, and it is load-bearing: the leaf is a
      # 7-day cert, and the platform re-renders it before expiry
      # (krg.vaultAgent.renewal, krg-infra #534) — but a process that builds its TLS
      # config once at `Client.connect` keeps the OLD cert for its whole life, so a
      # fresh file on /run recovers nothing on its own. That is exactly how v1's
      # 2026-08-17 outage worked: the cert expired at 11:09 UTC and the api-worker
      # spent ~7h retrying task-queue polls with `CertificateExpired` while every
      # hourly schedule sat idle.
      #
      # These are EXACTLY the compose services that mount /run/tenant/temporal
      # (deploy/tests pins the equality both ways). Leaving the list empty would
      # restart the ENTIRE interior stack on every rotation — postgres, the web
      # and the API included — roughly every 5 days, for a cert three services use.
      # The `smoke` service mounts it too but is profile-gated and run by hand
      # (`run --rm` reads the cert fresh), and the hook's `docker compose restart`
      # knows no profiled service -- so it is not, and must not be, listed.
      #
      # ADD a service HERE when it gains a /run/tenant/temporal mount in
      # deploy/incus/compose.yml; miss it and it silently holds an expired cert
      # until something else restarts the container.
      temporal = {
        namespace = "fishsense";
        reload = [
          # v1's fishsense-api-workflow-worker: every schedule and stage parent.
          "orchestrator"
          # v1's fishsense-backup-worker: the nightly dumps' schedule and worker.
          "backup"
          # Not a consumer of the cert — a FORWARDER of it. The processor runs on
          # NRP, outside vault-agent's reach, and holds the same CN=fishsense-worker
          # identity in a k8s Secret. v1's copy was hand-minted `ttl=720h`, expired
          # 2026-08-14 05:46:26 UTC, and every pod crash-looped on
          # `CertificateExpired` — which is what timed out v1's v2.15.2 rollout.
          #
          # This one-shot re-pushes the rotated leaf and rolls every processor
          # Deployment that is up. `restart` starts an exited container again, so
          # it re-runs each rotation; it no-ops when the leaf is unchanged.
          # deploy/incus/cert-sync-timer.nix also re-runs it every six hours, so a
          # rotation whose run failed is retried well inside the leaf's margin.
          "nrp-temporal-cert-sync"
        ];
      };
    };
  in {
    # The Incus slot (booted at 10.100.0.10) converges to THIS config via our runner.
    # nixosModules.tenant brings the lab baseline (AD-join, firewall, CrowdSec,
    # monitoring) + Docker + the compose runner + the vault-agent cert renders.
    nixosConfigurations.fishsense = nixpkgs.lib.nixosSystem {
      inherit system;
      modules = [
        krg-infra.nixosModules.tenant
        {krg.tenant = tenant;}
        # Incus VM plumbing — the SAME module the krg-golden image builds from
        # (nix/golden): systemd-boot bootloader + ESP/root fileSystems (by label) +
        # incus-agent (keeps `incus exec` working after the switch) + serial console +
        # growPartition. As v1: robust across reprovision (label-based — ADR 0022 §4).
        ({modulesPath, ...}: {
          imports = [(modulesPath + "/virtualisation/incus-virtual-machine.nix")];
        })
        # Ephemeral VM tier is OEC-exempt — matches krg-golden (nix/golden), which
        # forces this off. base.nix hard-enables OEC; nixosModules.tenant sets
        # `isVM = true` but does NOT force OEC off, so a tenant converging via its
        # own flake must (v1's flake; flagged upstream).
        ({lib, ...}: {
          krg.oecQualysTrellix.enable = lib.mkForce false;
        })
        ./deploy/incus/secrets.nix # extends krg.vaultAgent.renders → /run/tenant/secrets/*.env (HANDOFF §9)
        ./deploy/incus/workdir.nix # populate /var/lib/krg/fishsense so the compose's relative binds resolve
        ./deploy/incus/prune.nix # docker image prune after each compose converge — the 20G disk fills otherwise
        ./deploy/incus/cert-sync-timer.nix # re-run the NRP cert sync every 6 h, between rotations
      ];
    };

    # Reproducible boundary projection (admin copies into terraform/incus):
    #   nix eval .#krgTenant.terraformTenant --json
    # Identical to fishsense-lite's: the cutover changes no boundary.
    krgTenant = tenant;

    # Dev shell (`nix develop`). Cluster tooling for NRP, where the orchestrator
    # stands the processor up: `kubectl`, the OIDC `kubelogin` (int128/kubelogin →
    # `kubectl oidc-login`, which NRP's CILogon kubeconfig requires — NOT the Azure
    # `kubelogin`) for the one-time deploy/nrp/deployer-rbac.yaml apply and the
    # processor's Secrets. Pinned to the nixpkgs the slot builds from.
    devShells.${system}.default = pkgs.mkShell {
      packages = [
        pkgs.kubectl
        pkgs.kubelogin-oidc
      ];
      shellHook = ''
        echo "fishsense dev shell: kubectl + kubelogin-oidc (kubectl oidc-login)"
        echo "NRP login: kubectl get pods  (opens a CILogon browser window the first time)"
      '';
    };
  };
}
