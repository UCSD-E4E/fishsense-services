# Partner tenants

How an external partner org gets its own tenant, and how its people get into it
without an operator per person (PLAN.md §9.10, decided 2026-10-07).

## How it works

1. **krg-infra** (`terraform/authentik/collaborator_invites.tf`, PR #565) mints one
   reusable, expiring Authentik invite per partner org. Every account created
   through it is an Authentik-local `external` account with `attributes.org = <org>`,
   pinned server-side; the `org` scope puts it on the token as the `org` claim.
2. **Here**, a tenant that *claims* that org (`tenants.org_claim`) takes such a
   caller in as a **`member`** on their first API request
   (`join_claimed_tenant()`, migration 0037). Nothing else changes: they get a 404
   from every other tenant, and anything above `member` is still granted by an
   operator.

One org, one tenant (`org_claim` is unique). The lab claims no org.

## Onboarding a partner org

1. **krg-infra**: add the org to `local.collaborator_invites` (its key is the org,
   `tenant = "fishsense"`, an `expires`). CD mints the invite; the link is in
   OpenBao:

   ```bash
   bao kv get -field=url secret/krg-prod/authentik-managed/collaborator-invites/<org>
   ```

2. **Here**, open the tenant, as the schema owner, in a root shell on the slot
   (`dc` as defined at the top of [cutover.md](cutover.md)):

   ```bash
   dc run --rm migrate fishsense-services-api add-tenant <slug> \
     --name "<Display Name>" --org-claim <org>
   ```

   Idempotent; re-running updates the name (and the claim, if given). Use the
   org as the slug unless there's a reason not to. An org already claimed by
   another tenant is refused, and nothing changes.

3. Send the org the link. Their people enroll, verify their email, and sign in.

Do step 2 before the link goes out: until a tenant claims the org, its accounts
sign in but belong to no tenant (they join on their next request once it does).

### Live

| org | tenant | invite expires |
|---|---|---|
| `conservation-angler` | `conservation-angler` — *run step 2 after this ships* | 2027-01-05 |

## Promoting, renewing, offboarding

- **A partner admin:** `UPDATE memberships SET role = 'admin' …` as the owner; the
  next login keeps it (the join never touches an existing membership).
- **Renew the link:** bump `expires` in krg-infra (same link).
- **Revoke the link:** delete the krg-infra entry. Existing accounts stay.
- **Offboard a person:** deactivate their Authentik account (Directory → Users,
  filter on attribute `org`). Deleting their membership alone isn't enough: their
  claim would re-join them on their next request.
- **Stop an org joining:** `UPDATE tenants SET org_claim = NULL WHERE slug = '<slug>'`.
  Existing members stay until removed.
