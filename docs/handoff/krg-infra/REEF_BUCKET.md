# A bucket for the `reef` tenant

For krg-infra. fishsense-services is moving the REEF data out of the `lab`
tenant and into `reef` (the owner, 2026-11-11). Before the move, every file
for those missions has to be copied into REEF's own bucket, because each
tenant reads only its own bucket with its own key
([MOBILE_SYNC.md §2](MOBILE_SYNC.md#2-a-garage-bucket-and-key-per-tenant)).
The `reef` tenant exists in the database; it has no bucket yet.

## What's needed

1. **A bucket and key for `reef`**, as for any tenant (MOBILE_SYNC.md,
   "Adding a tenant later"):
   - bucket `fishsense-tenant-reef`;
   - key `fishsense-tenant-reef`, granted `read` and `write` on that bucket
     **only**;
   - its entry in the `stores_json` secret (#566 derives it from
     `spec/e4e-nas/garage.yml`);
   - then restart `openbao-agent` on the slot, and recreate the `api` and
     `orchestrator` containers, which read the file at start.

   No extra grant is needed for the copy. The orchestrator reads each file
   with lab's key and writes it with REEF's, so the two keys stay scoped to
   their own buckets.

2. **Room for a second copy.** Until the owner approves deleting lab's
   copies, both buckets hold the same files:

   | What | Files | Size |
   |---|---|---|
   | Raw frames | 56,071 | 793 GB |
   | Renders | 65,981 | 442 GB |
   | Thumbnails | 64,309 | 4.4 GB |
   | Masks | 41,198 | 0.7 GB |
   | **Total** | | **about 1.24 TB** |

   Measured from the database on 2026-11-11. Please confirm the e4e Garage
   has that much free, plus headroom, before the copy starts.

## Acceptance check

With the `reef` key, using the acceptance script in MOBILE_SYNC.md with the
bucket set to `fishsense-tenant-reef`:

- a PUT with the right checksum is accepted (200), and a wrong one refused
  (400);
- the `reef` key gets 403 on `fishsense-tenant-lab`, and the `lab` key gets
  403 on `fishsense-tenant-reef`.
