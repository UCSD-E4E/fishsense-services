# Handoff to krg-infra: a self-hosted geocoder for capture place names

**From:** fishsense-services. **For:** whoever owns the krg / e4e hosts and their networking.
**Why:** FishSense data is often collected in the field with no internet, so the mobile app frequently can't name the place when a capture is taken. A committed capture is immutable (fishsense-mobile #72), so the app can't add a name later. fishsense-services now derives the name from each capture's coordinates. The owner decided on 2026-10-16 that the geocoder is **self-hosted**, so a partner's capture locations (their fishing spots) never leave our infrastructure. Please don't point this at a public geocoder.

The services side is built (`place_names.py`, migration 0041). Until a geocoder is running and `FISHSENSE_GEOCODER_URL` is set, the API simply derives no names. Nothing breaks.

## What we need

A **[Photon](https://github.com/komoot/photon)** instance: komoot's OpenStreetMap geocoder, which serves reverse geocoding over HTTP.

- **Data:**
  - **Coverage:** the planet, because partners dive worldwide. A regional extract would leave some captures unnamed, so it isn't enough.
  - **Index:** Photon's prebuilt planet index (from GraphHopper's download server). It needs roughly 100 GB of disk, more while an update is extracting.
  - **Updates:** refreshing it a few times a year is plenty, because place names change slowly.
- **Memory:** 8–16 GB RAM for the JVM and its OpenSearch is comfortable at our load. Our load is a few requests per synced capture, with at most 20 per API request.
- **API used:** reverse only, `GET /reverse?lat=&lon=&limit=1&lang=en&radius=<km>`. Forward search isn't needed.
- **Network:**
  - **reachable from the fishsense slot's `api` container**, over the interior network or krg's private network;
  - **not exposed publicly**, since it has no auth, and keeping it private is the point;
  - plain HTTP is fine inside the private network.
- **Where it runs:** krg-infra's call. It's a stateless service apart from its index, so any host with the disk will do.

## Then: tell fishsense-services the URL

Give us the base URL, for example `http://photon.krg.internal:2322`. It isn't a secret: it goes into `deploy/incus/compose.yml` as `FISHSENSE_GEOCODER_URL` on the `api` service, as a one-line fishsense-services PR.

## Acceptance

From the fishsense slot:

```bash
curl -s 'http://<photon>/reverse?lat=32.8506&lon=-117.2717&limit=1&lang=en&radius=25' | jq '.features[0].properties | {name, city, state, country}'
# a feature near La Jolla Cove, San Diego, California, United States
```

From outside the krg network, the same URL must **not** answer.
