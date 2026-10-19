# Processor workers off NRP

A machine deployed by hand that runs every processor role and polls the same
Temporal queues as the NRP pods, so it takes work just by running:
- one GPU worker per card (two frames at once on a card of 22 GiB or more);
- a per-image worker sized to its CPUs;
- a light worker.

Each worker gets its NRP pod's own environment (`render.py` reads
`deploy/nrp/*.yaml`) and the released images. A GPU worker names itself
`…/gpu/cuda`. While one is polling, the orchestrator's GPU wake uses it and
stands no NRP GPU up.

Requirements:
- Docker with the NVIDIA CDI devices (`docker info` lists `nvidia.com/gpu=N`);
- outbound access to `krg-prod.ucsd.edu:7233` (Temporal) and `s3.e4e.ucsd.edu` (Garage).

## Bring a machine up

From a checkout, with SSH to krg-nat and to the machine:

```sh
# 1. Credentials into your account on the machine (0700/0600, never printed).
deploy/workers/fetch_credentials.sh you@machine

# 2. A compose file for its GPUs and CPUs, at the release production runs.
#    DATA is local disk, never the (NFS) home: /scratch/fishsense on
#    kastner-ml, /scratch/krg/<user> on waiter.
ssh you@machine 'nvidia-smi -L | wc -l; nproc; id -u; id -g; echo $HOME'
uv run python deploy/workers/render.py --gpus 4 --cpus 128 --version v0.13.2 \
    --uid <uid> --gid <gid> --home <HOME>/.fishsense-worker --data <DATA> \
    > /tmp/workers.yml
scp /tmp/workers.yml you@machine:<DATA>/compose.yml

# 3. Make the cache and scratch directories, and start the workers.
ssh you@machine 'cd <DATA> \
  && for s in $(docker compose config --services); do mkdir -p cache/$s scratch/$s; done \
  && docker compose up -d'
```

To check it: `(in <DATA>) docker compose logs gpu-0 | grep "GPU memory"` shows the card and its cap. The GPU queue's pollers (`temporal task-queue describe --task-queue fishsense_processor_gpu`) list `…/gpu/cuda`.

## Keep it up

- **The Temporal leaf is a 7-day cert.** After it rotates on the slot, re-run step 1, then `docker compose restart`. A worker reads its cert once, when it connects.
- **On a new release**, re-render at the new version (step 2), then run `docker compose up -d`.

## Take it down

```sh
ssh you@machine 'cd <DATA> && docker compose down && rm -rf cache scratch compose.yml \
  && rm -rf ~/.fishsense-worker'
```

This removes the credentials along with the workers.
